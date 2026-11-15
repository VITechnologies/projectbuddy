using System.Net;
using System.Net.Sockets;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace ProjectBuddy.PeerRedact;

public sealed class RedactionException(string message) : Exception(message);

public sealed record Redaction(string Text, IReadOnlyDictionary<string, int> Counts)
{
    public int Total => Counts.Values.Sum();
}

// Deterministic redaction of peer evidence before a model sees it and before anything leaves the peer (#210 D5,
// #290). Rules run in a fixed order and the output is idempotent: redacting redacted text changes nothing.
public sealed partial class Redactor
{
    public const string Schema = "projectbuddy/redaction-denylist@1";
    public const int MinTermLength = 3;
    public const int MaxTermLength = 200;
    public const int MaxTerms = 1000;

    // Every placeholder the rules emit; deny-list terms may not occur inside them, which keeps redaction idempotent.
    public static readonly string[] Placeholders = ["<private>", "<secret>", "<email>", "<url>", "<path>", "<user>", "<ip>", "<mac>"];

    public static readonly string[] DefaultAllowedHosts = ["github.com", "githubusercontent.com", "gitlab.com", "ollama.com", "registry.ollama.ai"];

    // Loopback, the unspecified address and the peer-link cable /30.
    public static readonly string[] DefaultAllowedNetworks = ["127.0.0.0/8", "0.0.0.0/32", "10.211.0.0/30"];

    // Absolute paths kept verbatim; anything else becomes <path>. Under a user profile only these folders are kept.
    public static readonly string[] DefaultAllowedPaths = [@"C:\Windows\", @"C:\Program Files\", @"C:\Program Files (x86)\", @"C:\ProgramData\ProjectBuddy\"];
    public static readonly string[] DefaultAllowedProfilePaths = [@"AppData\Local\Programs\", @"AppData\Local\ProjectBuddy\", @".ollama\"];

    private readonly Regex? deny;
    private readonly string[] hosts;
    private readonly (uint Network, uint Mask)[] networks;
    private readonly string[] paths;
    private readonly string[] profilePaths;

    public Redactor(IEnumerable<string> denyTerms, IEnumerable<string>? allowedHosts = null, IEnumerable<string>? allowedNetworks = null,
        IEnumerable<string>? allowedPaths = null, IEnumerable<string>? allowedProfilePaths = null)
    {
        var terms = denyTerms.Select(term => term.Trim()).Distinct(StringComparer.OrdinalIgnoreCase).ToList();
        if (terms.Count > MaxTerms)
        {
            throw new RedactionException($"at most {MaxTerms} deny-list terms");
        }

        foreach (var term in terms)
        {
            if (term.Length < MinTermLength || term.Length > MaxTermLength || term.IndexOfAny(['\r', '\n']) >= 0)
            {
                throw new RedactionException($"deny-list terms must be {MinTermLength}-{MaxTermLength} characters on one line");
            }

            if (Placeholders.Any(placeholder => placeholder.Contains(term, StringComparison.OrdinalIgnoreCase)))
            {
                throw new RedactionException("a deny-list term may not occur inside a placeholder");
            }
        }

        // Longest first, so a term never leaves a fragment of a longer one; non-backtracking keeps large lists linear.
        deny = terms.Count == 0 ? null : new Regex(
            string.Join('|', terms.OrderByDescending(term => term.Length).ThenBy(term => term, StringComparer.Ordinal).Select(Regex.Escape)),
            RegexOptions.IgnoreCase | RegexOptions.CultureInvariant | RegexOptions.NonBacktracking);
        hosts = (allowedHosts ?? DefaultAllowedHosts).Select(host => host.ToLowerInvariant()).ToArray();
        networks = (allowedNetworks ?? DefaultAllowedNetworks).Select(ParseNetwork).ToArray();
        paths = (allowedPaths ?? DefaultAllowedPaths).ToArray();
        profilePaths = (allowedProfilePaths ?? DefaultAllowedProfilePaths).ToArray();
    }

    // The peer's own machine and account names are always private, whatever the operator's list says.
    public static Redactor ForHost(IEnumerable<string> denyTerms, string machineName, string userName) =>
        new(denyTerms.Concat(new[] { machineName, userName }.Where(name => name.Trim().Length >= MinTermLength)));

    public static IReadOnlyList<string> ParseDenyList(byte[] bytes)
    {
        try
        {
            using var document = JsonDocument.Parse(bytes, new JsonDocumentOptions { AllowDuplicateProperties = false });
            var root = document.RootElement;
            if (root.ValueKind != JsonValueKind.Object || root.EnumerateObject().Count() != 2
                || !root.TryGetProperty("schema", out var schema) || schema.ValueKind != JsonValueKind.String || schema.GetString() != Schema
                || !root.TryGetProperty("terms", out var terms) || terms.ValueKind != JsonValueKind.Array
                || terms.EnumerateArray().Any(term => term.ValueKind != JsonValueKind.String))
            {
                throw new RedactionException($"the deny list must be exactly {{schema: {Schema}, terms: [strings]}}");
            }

            return terms.EnumerateArray().Select(term => term.GetString()!).ToList();
        }
        catch (JsonException)
        {
            throw new RedactionException("the deny list is not valid JSON");
        }
    }

    public Redaction Redact(string text)
    {
        var counts = new Dictionary<string, int>(StringComparer.Ordinal);
        string Apply(string input, string rule, Regex pattern, MatchEvaluator evaluator) => pattern.Replace(input, match =>
        {
            var replaced = evaluator(match);
            if (replaced != match.Value)
            {
                counts[rule] = counts.GetValueOrDefault(rule) + 1;
            }

            return replaced;
        });

        var result = Apply(text, "secret", PrivateKey(), _ => "<secret>");
        result = Apply(result, "secret", Token(), _ => "<secret>");
        result = Apply(result, "secret", Bearer(), match => match.Groups["scheme"].Value + "<secret>");
        result = Apply(result, "secret", Assignment(), match => match.Groups["key"].Value + match.Groups["separator"].Value + "<secret>");
        result = Apply(result, "url", Url(), RedactUrl);
        result = Apply(result, "url", ScpUrl(), _ => "<url>");
        result = Apply(result, "email", Email(), _ => "<email>");
        result = Apply(result, "path", UncPath(), _ => "<path>");
        result = Apply(result, "path", WindowsPath(), RedactWindowsPath);
        result = Apply(result, "path", PosixHome(), match => match.Groups["root"].Value + "<user>" + (match.Groups["rest"].Value.Length > 1 ? "/<path>" : match.Groups["rest"].Value));
        result = Apply(result, "mac", Mac(), _ => "<mac>");
        result = Apply(result, "ip", IPv4(), match => AllowedIPv4(match.Value) ? match.Value : "<ip>");
        result = Apply(result, "ip", IPv6(), match => IPAddress.TryParse(match.Value, out var address) && address.AddressFamily == AddressFamily.InterNetworkV6
            && !IPAddress.IsLoopback(address) && !address.Equals(IPAddress.IPv6Any) ? "<ip>" : match.Value);
        if (deny is not null)
        {
            result = Apply(result, "private", deny, _ => "<private>");
        }

        return new Redaction(result, counts);
    }

    private string RedactUrl(Match match)
    {
        var host = match.Groups["host"].Value.ToLowerInvariant();
        if (!hosts.Any(allowed => host == allowed || host.EndsWith("." + allowed, StringComparison.Ordinal)))
        {
            return "<url>";
        }

        // Never the path or query: on an allowed host they can still name a customer repository.
        var rest = match.Groups["rest"].Value;
        return $"{match.Groups["scheme"].Value}://{host}{match.Groups["port"].Value}{(rest.Length > 1 ? "/<path>" : rest)}";
    }

    private string RedactWindowsPath(Match match)
    {
        var path = match.Value;
        if (paths.Any(prefix => path.StartsWith(prefix, StringComparison.OrdinalIgnoreCase)))
        {
            return path;
        }

        var profile = ProfilePath().Match(path);
        if (!profile.Success)
        {
            return "<path>";
        }

        // An already redacted profile ("C:\Users\" then a placeholder) stays as it is.
        if (profile.Groups["user"].Value.Length == 0)
        {
            return path;
        }

        var inside = profile.Groups["inside"].Value;
        var kept = inside.Length == 0 || profilePaths.Any(prefix => inside.StartsWith(prefix, StringComparison.OrdinalIgnoreCase)) ? inside : "<path>";
        return $"{profile.Groups["root"].Value}<user>{(inside.Length == 0 && !profile.Groups["slash"].Success ? "" : "\\")}{kept}";
    }

    private bool AllowedIPv4(string text)
    {
        var parts = text.Split('.').Select(int.Parse).ToArray();
        if (parts.Any(part => part > 255))
        {
            return true;
        }

        var value = (uint)(parts[0] << 24 | parts[1] << 16 | parts[2] << 8 | parts[3]);
        return networks.Any(network => (value & network.Mask) == network.Network);
    }

    private static (uint Network, uint Mask) ParseNetwork(string cidr)
    {
        var slash = cidr.Split('/');
        if (slash.Length != 2 || !IPAddress.TryParse(slash[0], out var address) || address.AddressFamily != AddressFamily.InterNetwork
            || !int.TryParse(slash[1], out var bits) || bits is < 0 or > 32)
        {
            throw new RedactionException($"allowed network {cidr} is not an IPv4 CIDR");
        }

        var bytes = address.GetAddressBytes();
        var mask = bits == 0 ? 0u : uint.MaxValue << (32 - bits);
        return ((uint)(bytes[0] << 24 | bytes[1] << 16 | bytes[2] << 8 | bytes[3]) & mask, mask);
    }

    [GeneratedRegex(@"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(-----END [A-Z ]*PRIVATE KEY-----|\z)")]
    private static partial Regex PrivateKey();

    [GeneratedRegex(@"\b(gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|glpat-[A-Za-z0-9_-]{20,}|glrt-[A-Za-z0-9_-]{20,}|GR1348941[A-Za-z0-9_-]{20,}|(AKIA|ASIA)[0-9A-Z]{16}|xox[abprs]-[A-Za-z0-9-]{10,}|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}|npm_[A-Za-z0-9]{36}|AIza[0-9A-Za-z_-]{35})")]
    private static partial Regex Token();

    [GeneratedRegex(@"(?<scheme>\b(?:Bearer|Basic)\s+)(?!<)[A-Za-z0-9._~+/=-]{8,}", RegexOptions.IgnoreCase)]
    private static partial Regex Bearer();

    [GeneratedRegex(@"(?<key>\b(?:password|passwd|pwd|secret|client[_-]?secret|token|api[_-]?key|access[_-]?key|private[_-]?key)\b""?)(?<separator>\s*[:=]\s*)(?:""[^""\r\n]*""|'[^'\r\n]*'|[^\s,;]+)", RegexOptions.IgnoreCase)]
    private static partial Regex Assignment();

    [GeneratedRegex(@"\b(?<scheme>[A-Za-z][A-Za-z0-9+.-]*)://(?:[^\s/@""'<>]+@)?(?<host>[^\s/:""'<>?#]+)(?<port>:[0-9]+)?(?<rest>[^\s""'<>]*)")]
    private static partial Regex Url();

    [GeneratedRegex(@"\b[A-Za-z0-9._-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}:[^\s""'<>]+")]
    private static partial Regex ScpUrl();

    [GeneratedRegex(@"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")]
    private static partial Regex Email();

    [GeneratedRegex(@"(?<![A-Za-z0-9\\])\\\\[^\\\s""'<>]+\\[^\s""'<>|]*")]
    private static partial Regex UncPath();

    // A drive path runs on through spaces, so "D:\Acme Projects\x" never leaks its tail, but stops before the next
    // drive or UNC path, so a kept system path can never carry another path along with it.
    [GeneratedRegex(@"(?<![A-Za-z0-9])[A-Za-z]:\\(?:(?![A-Za-z]:\\|\\\\)[^""'<>|\r\n\t*?])*")]
    private static partial Regex WindowsPath();

    [GeneratedRegex(@"^(?<root>[A-Za-z]:\\Users\\)(?<user>[^\\]*)(?<slash>\\)?(?<inside>.*)$", RegexOptions.IgnoreCase | RegexOptions.Singleline)]
    private static partial Regex ProfilePath();

    [GeneratedRegex(@"(?<root>(?<![\w.~-])/(?:home|Users)/)(?!<)[^/\s""'<>]+(?<rest>/[^\s""'<>]*)?")]
    private static partial Regex PosixHome();

    [GeneratedRegex(@"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b")]
    private static partial Regex Mac();

    [GeneratedRegex(@"(?<![0-9.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9.])")]
    private static partial Regex IPv4();

    [GeneratedRegex(@"(?<![0-9A-Za-z:])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![0-9A-Za-z:])")]
    private static partial Regex IPv6();
}
