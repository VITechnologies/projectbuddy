using System.Globalization;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace ProjectBuddy.PeerRedact;

public sealed class RedactionException(string message) : Exception(message);

public sealed record Redaction(string Text, IReadOnlyDictionary<string, int> Counts)
{
    public int Total => Counts.Values.Sum();
}

// A drive-rooted path that is kept up to Depth further segments; Depth 0 cuts right after the prefix.
public sealed record AllowedPath(string Prefix, int Depth);

// Deterministic redaction of peer evidence before a model sees it and before anything leaves the peer (#210 D5,
// #290). Rules run in a fixed order and the output is idempotent: redacting redacted text changes nothing.
public sealed partial class Redactor
{
    public const string Schema = "projectbuddy/redaction-denylist@1";
    public const int MinTermLength = 3;
    public const int MaxTermLength = 200;
    public const int MaxTerms = 1000;
    public const int MaxInputChars = 4_000_000;

    public static readonly string[] Placeholders = ["<private>", "<secret>", "<email>", "<url>", "<host>", "<path>", "<user>", "<ip>", "<mac>"];

    public static readonly string[] DefaultAllowedHosts = ["github.com", "githubusercontent.com", "gitlab.com", "ollama.com", "registry.ollama.ai"];

    // Loopback, the unspecified address and the peer-link cable /30.
    public static readonly string[] DefaultAllowedNetworks = ["127.0.0.0/8", "0.0.0.0/32", "10.211.0.0/30"];

    // Kept to a depth that names a product and its file; subtrees that hold arbitrary data are cut at once.
    public static readonly AllowedPath[] DefaultAllowedPaths =
    [
        new(@"C:\Windows", 4), new(@"C:\Windows\Temp", 0), new(@"C:\Windows\ServiceProfiles", 0),
        new(@"C:\Windows\System32\config", 0), new(@"C:\Windows\SysWOW64\config", 0),
        new(@"C:\Program Files", 3), new(@"C:\Program Files (x86)", 3), new(@"C:\ProgramData\ProjectBuddy", 2),
    ];

    // Under a user profile (C:\Users\<user>\...), only these folders are kept.
    public static readonly AllowedPath[] DefaultAllowedProfilePaths = [new(@"AppData\Local\Programs", 2), new(@"AppData\Local\ProjectBuddy", 2), new(@".ollama", 2)];

    private readonly Regex? deny;
    private readonly string[] hosts;
    private readonly (uint Network, uint Mask)[] networks;
    private readonly (string Drive, string[] Segments, int Depth)[] paths;
    private readonly (string[] Segments, int Depth)[] profilePaths;

    public Redactor(IEnumerable<string> denyTerms, IEnumerable<string>? allowedHosts = null, IEnumerable<string>? allowedNetworks = null,
        IEnumerable<AllowedPath>? allowedPaths = null, IEnumerable<AllowedPath>? allowedProfilePaths = null)
    {
        var terms = denyTerms.Select(term => Clean(term).Trim()).Distinct(StringComparer.OrdinalIgnoreCase).ToList();
        if (terms.Count > MaxTerms)
        {
            throw new RedactionException($"at most {MaxTerms} deny-list terms");
        }

        if (terms.Any(term => term.Length < MinTermLength || term.Length > MaxTermLength || term.IndexOfAny(['\r', '\n']) >= 0))
        {
            throw new RedactionException($"deny-list terms must be {MinTermLength}-{MaxTermLength} characters on one line");
        }

        // Longest first, words joined by any whitespace (wrapped lines, doubled or non-breaking spaces); non-backtracking.
        deny = terms.Count == 0 ? null : new Regex(
            string.Join('|', terms.OrderByDescending(term => term.Length).ThenBy(term => term, StringComparer.Ordinal)
                .Select(term => string.Join(@"\s+", term.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries).Select(Regex.Escape)))),
            RegexOptions.IgnoreCase | RegexOptions.CultureInvariant | RegexOptions.NonBacktracking);
        hosts = (allowedHosts ?? DefaultAllowedHosts).Select(host => host.ToLowerInvariant()).ToArray();
        networks = (allowedNetworks ?? DefaultAllowedNetworks).Select(ParseNetwork).ToArray();
        paths = (allowedPaths ?? DefaultAllowedPaths).Select(ParseAllowed).OrderByDescending(entry => entry.Segments.Length).ToArray();
        profilePaths = (allowedProfilePaths ?? DefaultAllowedProfilePaths)
            .Select(entry => (Split(entry.Prefix), entry.Depth)).OrderByDescending(entry => entry.Item1.Length).ToArray();
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
        if (text.Length > MaxInputChars)
        {
            throw new RedactionException($"evidence over {MaxInputChars} characters is refused, not partly redacted");
        }

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

        var result = Clean(text);
        result = Apply(result, "secret", PrivateKey(), _ => "<secret>");
        result = Apply(result, "secret", Token(), _ => "<secret>");
        result = Apply(result, "secret", Authorization(), match => match.Groups["key"].Value + match.Groups["quote"].Value + match.Groups["scheme"].Value + "<secret>");
        result = Apply(result, "secret", Bearer(), match => match.Groups["scheme"].Value + "<secret>");
        result = Apply(result, "secret", XmlSecret(), match => match.Groups["key"].Value + "\"<secret>\"");
        result = Apply(result, "secret", FlagSecret(), match => match.Groups["key"].Value + match.Groups["separator"].Value + "<secret>");
        result = Apply(result, "secret", Assignment(), match => match.Groups["key"].Value + match.Groups["separator"].Value + "<secret>");
        result = Apply(result, "secret", SasSignature(), _ => "sig=<secret>");
        result = Apply(result, "url", Url(), match => KeepHost(match.Groups["host"].Value, match.Groups["rest"].Value, match.Groups["scheme"].Value + "://", match.Groups["port"].Value, "<url>"));
        result = Apply(result, "url", ScpUrl(), _ => "<url>");
        result = Apply(result, "email", Email(), _ => "<email>");
        result = Apply(result, "path", DrivePath(), RedactDrivePath);
        result = Apply(result, "path", UncPath(), _ => "<path>");
        result = Apply(result, "path", PosixDrive(), _ => "<path>");
        result = Apply(result, "path", PosixHome(), match => match.Groups["root"].Value + "<user>" + (match.Groups["rest"].Value.Length > 1 ? "/<path>" : match.Groups["rest"].Value));
        result = Apply(result, "mac", Mac(), _ => "<mac>");
        result = Apply(result, "ip", IPv4(), match => AllowedIPv4(match.Value) ? match.Value : "<ip>");
        result = Apply(result, "ip", IPv6(), match => IPAddress.TryParse(match.Value, out var address) && address.AddressFamily == AddressFamily.InterNetworkV6
            && !IPAddress.IsLoopback(address) && !address.Equals(IPAddress.IPv6Any) ? "<ip>" : match.Value);
        result = Apply(result, "host", Host(), match => KeepHost(match.Groups["host"].Value, match.Groups["rest"].Value, "", "", "<host>"));
        if (deny is not null)
        {
            // Placeholders are skipped, so a term such as "User" never rewrites "<user>" and redaction stays idempotent.
            result = string.Concat(PlaceholderSplit().Split(result).Select(part =>
                Placeholders.Contains(part) ? part : Apply(part, "private", deny, _ => "<private>")));
        }

        return new Redaction(result, counts);
    }

    // NFC, no invisible format characters (zero-width joiners and spaces, soft hyphens) and no lone surrogates,
    // so a term or address cannot be split by characters a reader never sees.
    private static string Clean(string text)
    {
        var clean = new StringBuilder(text.Length);
        for (var index = 0; index < text.Length; index++)
        {
            var character = text[index];
            if (char.IsHighSurrogate(character) && index + 1 < text.Length && char.IsLowSurrogate(text[index + 1]))
            {
                clean.Append(character).Append(text[++index]);
            }
            else if (char.IsSurrogate(character))
            {
                clean.Append('\uFFFD');
            }
            else if (CharUnicodeInfo.GetUnicodeCategory(character) != UnicodeCategory.Format)
            {
                clean.Append(character);
            }
        }

        return clean.ToString().Normalize(NormalizationForm.FormC);
    }

    // Allowed hosts keep their name but never a path or query: on github.com a path can still name a customer repository.
    private string KeepHost(string host, string rest, string scheme, string port, string replacement)
    {
        var name = host.ToLowerInvariant();
        return hosts.Any(allowed => name == allowed || name.EndsWith("." + allowed, StringComparison.Ordinal))
            ? $"{scheme}{name}{port}{(rest.Length > 1 ? "/<path>" : rest)}"
            : replacement;
    }

    // Segments are split on every separator spelling, so mixing them (C:\Windows\Temp/x) cannot dodge a depth limit,
    // and the kept part is copied from the original text, so "cmd.exe /c" keeps its switch as written.
    private string RedactDrivePath(Match match)
    {
        var value = match.Value;
        var prefix = match.Groups["long"].Value;
        var body = value[prefix.Length..];
        var drive = body[..1].ToUpperInvariant();
        var root = prefix + body[..(body[1] == ':' ? 2 : 4)];
        var rest = value[root.Length..];
        var separators = Separator().Matches(rest).Select(separator => (separator.Index, separator.Value)).ToArray();
        var segments = separators.Select((separator, index) =>
            rest[(separator.Index + separator.Value.Length)..(index + 1 < separators.Length ? separators[index + 1].Index : rest.Length)]).ToArray();
        if (segments.Any(segment => segment.Trim() == ".."))
        {
            return "<path>";
        }

        // A bare drive root (C:\) names nothing.
        if (segments.All(segment => segment.Length == 0))
        {
            return value;
        }

        string Build(int keep, bool user) =>
            root + string.Concat(Enumerable.Range(0, keep).Select(index => separators[index].Value + (user && index == 1 ? "<user>" : segments[index])))
            + (segments.Skip(keep).Any(segment => segment.Length > 0) ? separators[keep].Value + "<path>" : string.Concat(separators.Skip(keep).Select(separator => separator.Value)));

        if (drive == "C" && segments[0].Equals("Users", StringComparison.OrdinalIgnoreCase))
        {
            // Already redacted ("C:\Users\" before a placeholder) or the bare profiles folder: nothing to hide.
            if (segments.Length == 1 || segments[1].Length == 0)
            {
                return value;
            }

            var inside = segments[2..];
            foreach (var (allowed, depth) in profilePaths)
            {
                if (StartsWith(inside, allowed))
                {
                    return Build(Math.Min(segments.Length, 2 + allowed.Length + depth), true);
                }
            }

            return Build(2, true);
        }

        foreach (var (allowedDrive, allowed, depth) in paths)
        {
            if (drive == allowedDrive && StartsWith(segments, allowed))
            {
                return Build(Math.Min(segments.Length, allowed.Length + depth), false);
            }
        }

        return "<path>";
    }

    private static bool StartsWith(string[] segments, string[] prefix) =>
        segments.Length >= prefix.Length && prefix.Select((segment, index) => segment.Equals(segments[index].Trim(), StringComparison.OrdinalIgnoreCase)).All(same => same);

    private static string[] Split(string path) => path.Split(['\\', '/'], StringSplitOptions.RemoveEmptyEntries);

    private static (string Drive, string[] Segments, int Depth) ParseAllowed(AllowedPath entry)
    {
        if (entry.Depth < 0 || !DriveRoot().IsMatch(entry.Prefix))
        {
            throw new RedactionException($"allowed path {entry.Prefix} must start with a drive and have a depth of zero or more");
        }

        return (entry.Prefix[..1].ToUpperInvariant(), Split(entry.Prefix[3..]), entry.Depth);
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

    private const string AuthSchemes = "Bearer|Basic|Digest|token|Negotiate|NTLM|SharedKey|SharedKeyLite|ApiKey|SSWS|AWS4-HMAC-SHA256";

    [GeneratedRegex(@"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(-----END [A-Z ]*PRIVATE KEY-----|\z)")]
    private static partial Regex PrivateKey();

    [GeneratedRegex(@"\b(gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|glpat-[A-Za-z0-9_-]{20,}|glrt-[A-Za-z0-9_-]{20,}|GR1348941[A-Za-z0-9_-]{20,}|(AKIA|ASIA)[0-9A-Z]{16}|xox[abprs]-[A-Za-z0-9-]{10,}|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}|npm_[A-Za-z0-9]{36}|AIza[0-9A-Za-z_-]{35})")]
    private static partial Regex Token();

    // Any scheme, and the whole value: to the closing quote when quoted, else to the end of the line, so multi-part
    // values (AWS4-HMAC-SHA256 Credential=..., Signature=...) never leak a part. A well-known scheme name stays.
    [GeneratedRegex(@"(?<key>\b(?:Proxy-)?Authorization""?[ \t]*[:=][ \t]*)(?:(?<quote>"")(?<scheme>(?:" + AuthSchemes + @")[ \t]+)?[^""\r\n]+|(?<scheme>(?:" + AuthSchemes + @")[ \t]+)?[^""\r\n][^\r\n]*)", RegexOptions.IgnoreCase)]
    private static partial Regex Authorization();

    // A standalone credential only when it looks like one (16+ characters with a digit), so "Basic authentication" stays.
    [GeneratedRegex(@"(?<scheme>\b(?:Bearer|Basic)\s+)(?=[A-Za-z0-9._~+/=-]*[0-9])[A-Za-z0-9._~+/=-]{16,}", RegexOptions.IgnoreCase)]
    private static partial Regex Bearer();

    [GeneratedRegex(@"(?<key>\bkey\s*=\s*""[^""\r\n]*(?:password|passwd|pwd|secret|token|key)[^""\r\n]*""\s+value\s*=\s*)""[^""\r\n]*""", RegexOptions.IgnoreCase)]
    private static partial Regex XmlSecret();

    // Command-line flags: "--token x", "--password=x", "-p:x" style secrets.
    [GeneratedRegex(@"(?<key>(?<![\w-])--?[A-Za-z0-9_-]*(?:token|password|passwd|pwd|secret|pat|api-?key)\b)(?<separator>\s+|[=:])(?!-)(?:""[^""\r\n]*""|'[^'\r\n]*'|[^\s""',;]+)", RegexOptions.IgnoreCase)]
    private static partial Regex FlagSecret();

    // Name with any prefix (ACTIONS_RUNNER_INPUT_TOKEN, DB_PASSWORD, "api_key"), then ":"/"=" or a table gap of 2+ spaces.
    // In a ";"-delimited string the value runs to the ";" (multi-word connection-string passwords).
    [GeneratedRegex(@"(?<key>(?<![A-Za-z0-9_.-])[A-Za-z0-9_.-]*(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|account[_-]?key|shared[_-]?access[_-]?key|private[_-]?key|client[_-]?secret|credentials?)""?)(?<separator>\s*[:=]\s*|[ \t]{2,}(?=\S))(?:""[^""\r\n]*""|'[^'\r\n]*'|[^;\r\n]{1,256}?(?=;)|[^\s,;]+)", RegexOptions.IgnoreCase)]
    private static partial Regex Assignment();

    [GeneratedRegex(@"(?<=[?&;])sig=[^&\s""'<>;]+", RegexOptions.IgnoreCase)]
    private static partial Regex SasSignature();

    [GeneratedRegex(@"\b(?<scheme>[A-Za-z][A-Za-z0-9+.-]*)://(?:[^\s/@""'<>]+@)?(?<host>[^\s/:""'<>?#]+)(?<port>:[0-9]+)?(?<rest>[^\s""'<>]*)", RegexOptions.NonBacktracking)]
    private static partial Regex Url();

    // Any user@host:path remote, including SSH host aliases with no TLD (git@github.com-customer:org/repo.git), and a
    // user-less host:path remote when it names a repository (build01:acme/repo.git).
    [GeneratedRegex(@"\b[A-Za-z0-9._-]+@[A-Za-z0-9._-]+:[^\s""'<>]+|\b[A-Za-z0-9][A-Za-z0-9._-]+:[A-Za-z0-9_~.-][^\s""'<>\\]*\.git\b", RegexOptions.NonBacktracking)]
    private static partial Regex ScpUrl();

    [GeneratedRegex(@"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", RegexOptions.NonBacktracking)]
    private static partial Regex Email();

    // One rule for every drive-path spelling: X:\, X:/, JSON-escaped X:\\, file:///X:/, X%3A/, \\?\X:\ and \\.\X:\.
    // The match runs on through spaces (so "D:\Acme Projects\x" never leaks its tail) and through apostrophes inside
    // names (O'Brien), but stops at a colon, which no Windows path component can hold (file.cs:line 42, https://), and
    // before the next drive or UNC path, so a kept path never carries another one along.
    [GeneratedRegex(@"(?<![A-Za-z0-9])(?<long>file:/{2,3}|\\\\\\\\[?.]\\\\|\\\\[?.]\\)?[A-Za-z](?::|%3[Aa])(?:\\\\|\\|/|%2[Ff]|%5[Cc])(?:(?!(?<![A-Za-z0-9])[A-Za-z](?::|%3[Aa])(?:\\\\|\\|/|%2[Ff]|%5[Cc])|(?<![A-Za-z0-9\\/])\\\\)(?:[^""'<>|:\r\n\t*?]|'(?=[A-Za-z0-9])))*")]
    private static partial Regex DrivePath();

    [GeneratedRegex(@"(?<![A-Za-z0-9\\/]|(?<![A-Za-z0-9])[A-Za-z]:)(?:\\\\\\\\|\\\\)(?![?.](?:\\\\|\\)[A-Za-z](?::|%3[Aa]))(?:[A-Za-z0-9._$-]+|\?)(?:(?!(?<![A-Za-z0-9])[A-Za-z](?::|%3[Aa])(?:\\\\|\\|/|%2[Ff]|%5[Cc])|(?<![A-Za-z0-9\\/])\\\\)(?:[^""'<>|:\r\n\t*?]|'(?=[A-Za-z0-9])))*")]
    private static partial Regex UncPath();

    // Git Bash (/c/...) and WSL (/mnt/c/...) spellings of a drive path.
    [GeneratedRegex(@"(?<![\w.~-])(?:/mnt)?/[A-Za-z]/[^\s""'<>]+")]
    private static partial Regex PosixDrive();

    [GeneratedRegex(@"(?<root>(?<![\w.~-])/(?:home|Users)/)(?!<)[^/\s""'<>]+(?<rest>/[^\s""'<>]*)?", RegexOptions.IgnoreCase)]
    private static partial Regex PosixHome();

    [GeneratedRegex(@"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b")]
    private static partial Regex Mac();

    // An address at the end of a sentence still counts; a version (Version=13.0.0.0, v1.2.3.4) does not.
    [GeneratedRegex(@"(?<![0-9]|[0-9]\.|[Vv]ersion[=: ]\s?|\bv)(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?!\.?[0-9])")]
    private static partial Regex IPv4();

    [GeneratedRegex(@"(?<![0-9A-Za-z:])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![0-9A-Za-z:])")]
    private static partial Regex IPv6();

    // Bare host names with a known top-level domain (fs01.corp.example.com, www.example.com/x); file names such as
    // main.vi or ollama.exe and .NET namespaces are not hosts.
    [GeneratedRegex(@"(?<![\w.@/-])(?<host>(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+(?:com|net|org|io|dev|app|cloud|biz|info|local|lan|corp|internal|intranet|home|arpa|us|uk|de|mx|ca|eu))(?<rest>/[^\s""'<>]*)?(?![\w-]|\.[\w-])", RegexOptions.IgnoreCase)]
    private static partial Regex Host();

    [GeneratedRegex(@"(<(?:private|secret|email|url|host|path|user|ip|mac)>)")]
    private static partial Regex PlaceholderSplit();

    [GeneratedRegex(@"\\\\\\\\|\\\\|\\|/|%2[Ff]|%5[Cc]")]
    private static partial Regex Separator();

    [GeneratedRegex(@"^[A-Za-z]:\\")]
    private static partial Regex DriveRoot();
}
