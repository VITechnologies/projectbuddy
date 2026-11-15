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
// #290). Rules run in a fixed order, and the whole order repeats until the text settles, so the output is
// idempotent by construction: redacting redacted text changes nothing.
public sealed partial class Redactor
{
    public const string Schema = "projectbuddy/redaction-denylist@1";
    public const int MinTermLength = 3;
    public const int MaxTermLength = 200;
    public const int MaxTerms = 1000;
    public const int MaxInputChars = 4_000_000;

    // A placeholder is at most three times the shortest text it replaces (abc -> <private>), so redacted output fits
    // here, and redacting it again is accepted when it is already settled.
    public const int MaxOutputChars = 3 * MaxInputChars;
    // More than the number of rules: a later pass can only be needed when a rule unlocks an earlier one, so a
    // deliberate chain through every rule still settles. Passes after the text settles never run.
    public const int MaxPasses = 32;

    public static readonly string[] Placeholders = ["<private>", "<secret>", "<email>", "<url>", "<host>", "<path>", "<user>", "<ip>", "<mac>"];

    public static readonly string[] DefaultAllowedHosts = ["github.com", "githubusercontent.com", "gitlab.com", "ollama.com", "registry.ollama.ai"];

    // Loopback, the unspecified address and the peer-link cable /30.
    public static readonly string[] DefaultAllowedNetworks = ["127.0.0.0/8", "0.0.0.0/32", "10.211.0.0/30"];

    // Kept to a depth that names a product and its file; folders that hold arbitrary data are cut at once.
    public static readonly AllowedPath[] DefaultAllowedPaths =
    [
        new(@"C:\Windows", 4), new(@"C:\Windows\Temp", 0), new(@"C:\Windows\SystemTemp", 0), new(@"C:\Windows\ServiceProfiles", 0),
        new(@"C:\Windows\Prefetch", 0), new(@"C:\Windows\Tasks", 0), new(@"C:\Windows\CSC", 0),
        new(@"C:\Windows\System32\config", 0), new(@"C:\Windows\System32\Tasks", 0), new(@"C:\Windows\System32\spool", 0),
        new(@"C:\Windows\System32\Microsoft\Protect", 0), new(@"C:\Windows\SysWOW64\config", 0),
        new(@"C:\Program Files", 3), new(@"C:\Program Files (x86)", 3), new(@"C:\ProgramData\ProjectBuddy", 2),
    ];

    // Under a user profile (C:\Users\<user>\...), only these folders are kept.
    public static readonly AllowedPath[] DefaultAllowedProfilePaths = [new(@"AppData\Local\Programs", 2), new(@"AppData\Local\ProjectBuddy", 2), new(@".ollama", 2)];

    private readonly Regex? deny;
    private readonly Regex? words;
    private readonly string[] hosts;
    private readonly (uint Network, uint Mask)[] networks;
    private readonly (string Drive, string[] Segments, int Depth)[] paths;
    private readonly (string[] Segments, int Depth)[] profilePaths;

    // Operator terms (denyTerms) match anywhere, even inside a longer word; whole-word terms, such as the host's own
    // machine and account names, match only between letters and digits, so an account "lab" leaves LabVIEW alone.
    public Redactor(IEnumerable<string> denyTerms, IEnumerable<string>? allowedHosts = null, IEnumerable<string>? allowedNetworks = null,
        IEnumerable<AllowedPath>? allowedPaths = null, IEnumerable<AllowedPath>? allowedProfilePaths = null, IEnumerable<string>? wholeWordTerms = null)
    {
        deny = TermPattern(denyTerms, false);
        words = TermPattern(wholeWordTerms ?? [], true);
        hosts = (allowedHosts ?? DefaultAllowedHosts).Select(host => host.ToLowerInvariant()).ToArray();
        networks = (allowedNetworks ?? DefaultAllowedNetworks).Select(ParseNetwork).ToArray();
        paths = (allowedPaths ?? DefaultAllowedPaths).Select(ParseAllowed).OrderByDescending(entry => entry.Segments.Length).ToArray();
        profilePaths = (allowedProfilePaths ?? DefaultAllowedProfilePaths)
            .Select(entry => (Split(entry.Prefix), entry.Depth)).OrderByDescending(entry => entry.Item1.Length).ToArray();
    }

    internal int Passes { get; init; } = MaxPasses;

    internal int OutputLimit { get; init; } = MaxOutputChars;

    // The peer's own machine and account names are always private, whatever the operator's list says.
    public static Redactor ForHost(IEnumerable<string> denyTerms, string machineName, string userName) =>
        new(denyTerms, wholeWordTerms: new[] { machineName, userName }.Where(name => name.Trim().Length >= MinTermLength));

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
        // Over the input cap, only text that is already redacted (settled) is accepted, never partly redacted.
        var settledOnly = text.Length > MaxInputChars;
        if (text.Length > OutputLimit)
        {
            throw new RedactionException($"evidence over {MaxInputChars} characters is refused, not partly redacted");
        }

        var counts = new Dictionary<string, int>(StringComparer.Ordinal);
        var result = text;
        for (var pass = 0; pass < Passes; pass++)
        {
            var next = Pass(Clean(result), counts);
            if (next == result)
            {
                return new Redaction(result, counts);
            }

            if (settledOnly || next.Length > OutputLimit)
            {
                throw new RedactionException($"evidence over {MaxInputChars} characters is refused, not partly redacted");
            }

            result = next;
        }

        throw new RedactionException($"redaction did not settle within {Passes} passes");
    }

    private string Pass(string text, Dictionary<string, int> counts)
    {
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
        result = Apply(result, "secret", Authorization(), match => match.Groups["key"].Value + match.Groups["quote"].Value + match.Groups["scheme"].Value + "<secret>");
        result = Apply(result, "secret", Bearer(), match => match.Groups["scheme"].Value + "<secret>");
        result = Apply(result, "secret", XmlSecret(), match => match.Groups["key"].Value + (match.Groups["value"].Value.StartsWith('\\') ? "\\\"<secret>\\\"" : "\"<secret>\""));
        result = Apply(result, "secret", Command(), RedactCommand);
        result = Apply(result, "secret", SecureString(), match => match.Groups["key"].Value + "<secret>");
        result = Apply(result, "secret", Credential(), match => match.Groups["head"].Value
            + (match.Groups["user"].Value.StartsWith('$') ? match.Groups["user"].Value : "<user>") + match.Groups["separator"].Value + "<secret>");
        result = Apply(result, "secret", FlagSecret(), match => match.Groups["key"].Value + "<secret>");
        result = Apply(result, "secret", Registry(), match => match.Groups["key"].Value + "<secret>");
        result = Apply(result, "secret", Assignment(), match => match.Groups["key"].Value + match.Groups["separator"].Value + "<secret>");
        result = Apply(result, "secret", SasSignature(), _ => "sig=<secret>");
        result = Apply(result, "user", UserFlag(), match => match.Groups["key"].Value + "<user>");
        result = Apply(result, "url", Url(), match => KeepHost(match.Groups["host"].Value, match.Groups["rest"].Value, match.Groups["scheme"].Value + match.Groups["separator"].Value, match.Groups["port"].Value, "<url>"));
        result = Apply(result, "url", ScpUrl(), match => DigestReference().IsMatch(match.Value) ? match.Value : "<url>");
        result = Apply(result, "email", Email(), _ => "<email>");
        result = Apply(result, "path", DrivePath(), RedactDrivePath);
        result = Apply(result, "path", UncPath(), match => Cut(match.Value));
        result = Apply(result, "path", UncForward(), match => Cut(match.Value));
        result = Apply(result, "path", PosixDrive(), match => Cut(match.Value));
        result = Apply(result, "path", PosixHome(), match => match.Groups["root"].Value + "<user>" + (match.Groups["rest"].Value.Length > 1 ? "/<path>" : match.Groups["rest"].Value));
        result = Apply(result, "mac", Mac(), _ => "<mac>");
        result = Apply(result, "ip", IPv4(), match => AllowedIPv4(match.Value) ? match.Value : "<ip>");
        result = Apply(result, "ip", IPv6(), match => IPAddress.TryParse(match.Value, out var address) && address.AddressFamily == AddressFamily.InterNetworkV6
            && !IPAddress.IsLoopback(address) && !address.Equals(IPAddress.IPv6Any) ? "<ip>" : match.Value);
        result = Apply(result, "private", DirectoryName(), RedactDirectoryName);
        result = Apply(result, "host", Host(), match => KeepHost(match.Groups["host"].Value, match.Groups["rest"].Value, "", "", "<host>"));
        result = Apply(result, "user", AccountAt(), _ => "<user>@");
        foreach (var terms in new[] { deny, words })
        {
            if (terms is not null)
            {
                // Placeholders are skipped, so a term such as "User" never rewrites "<user>".
                result = string.Concat(PlaceholderSplit().Split(result).Select(part =>
                    Placeholders.Contains(part) ? part : Apply(part, "private", terms, _ => "<private>")));
            }
        }

        return result;
    }

    private static Regex? TermPattern(IEnumerable<string> source, bool wholeWord)
    {
        var terms = source.Select(term => Clean(term).Trim()).Distinct(StringComparer.OrdinalIgnoreCase).ToList();
        if (terms.Count > MaxTerms)
        {
            throw new RedactionException($"at most {MaxTerms} deny-list terms");
        }

        if (terms.Any(term => term.Length < MinTermLength || term.Length > MaxTermLength || term.IndexOfAny(['\r', '\n']) >= 0))
        {
            throw new RedactionException($"deny-list terms must be {MinTermLength}-{MaxTermLength} characters on one line");
        }

        if (terms.Count == 0)
        {
            return null;
        }

        // Longest first, words joined by any whitespace (wrapped lines, doubled or non-breaking spaces).
        // A character other than an ASCII letter or digit may also be spelled as a JSON escape (M\u00FCller, O\u0027Brien).
        static string Character(char character) => char.IsAsciiLetterOrDigit(character) ? character.ToString()
            : $@"(?:{Regex.Escape(character.ToString())}|\\+u{(int)character:x4})";
        var alternatives = string.Join('|', terms.OrderByDescending(term => term.Length).ThenBy(term => term, StringComparer.Ordinal)
            .Select(term => string.Join(@"\s+", term.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries).Select(word => string.Concat(word.Select(Character))))));
        return wholeWord
            ? new Regex(@"(?:(?<![\p{L}\p{N}])|" + AfterEscape + @")(?:" + alternatives + @")(?![\p{L}\p{N}])", RegexOptions.IgnoreCase | RegexOptions.CultureInvariant)
            : new Regex(alternatives, RegexOptions.IgnoreCase | RegexOptions.CultureInvariant | RegexOptions.NonBacktracking);
    }

    // Inside a valid JSON string literal, \uXXXX escapes become their shortest JSON spelling: printable characters
    // literal, a quote and a backslash as \" and \\. JSON meaning is unchanged, but an escaped apostrophe or umlaut can
    // no longer hide a path or a term; escapes of invisible characters at a deeper level (\\u200B, JSON inside JSON)
    // are dropped. Text outside a valid literal, such as a raw Windows path (C:\Temp\uadd1), is never decoded.
    // Invisible format characters (zero-width joiners and spaces, soft hyphens, tag characters) are removed before and
    // after decoding, lone surrogates become U+FFFD, and the result is NFC, so a term or address cannot be split by
    // characters a reader never sees. Every pass starts here, so redaction settles on clean text.
    private static string Clean(string text) =>
        Visible(JsonString().Replace(Visible(text), literal => EscapedInvisible().Replace(JsonEscape().Replace(literal.Value, Decode), ""))).Normalize(NormalizationForm.FormC);

    private static string Decode(Match escape)
    {
        var code = Convert.ToInt32(escape.Groups["hex"].Value, 16);
        var literal = escape.Groups["low"].Success ? char.ConvertFromUtf32(char.ConvertToUtf32((char)code, (char)Convert.ToInt32(escape.Groups["low"].Value, 16)))
            : code is < 0x20 or 0x7F || char.IsSurrogate((char)code) ? null
            : code == '"' ? "\\\"" : code == '\\' ? "\\\\" : ((char)code).ToString();
        return literal is null ? escape.Value : escape.Groups["pairs"].Value + literal;
    }

    private static string Visible(string text)
    {
        var clean = new StringBuilder(text.Length);
        for (var index = 0; index < text.Length; index++)
        {
            var character = text[index];
            if (char.IsHighSurrogate(character) && index + 1 < text.Length && char.IsLowSurrogate(text[index + 1]))
            {
                if (CharUnicodeInfo.GetUnicodeCategory(char.ConvertToUtf32(character, text[index + 1])) != UnicodeCategory.Format)
                {
                    clean.Append(character).Append(text[index + 1]);
                }

                index++;
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

        return clean.ToString();
    }

    private bool IsAllowedHost(string name) => hosts.Any(allowed => name == allowed || name.EndsWith("." + allowed, StringComparison.Ordinal));

    // Allowed hosts keep their name but never a path or query: on github.com a path can still name a customer repository.
    private string KeepHost(string host, string rest, string scheme, string port, string replacement)
    {
        var name = host.ToLowerInvariant();
        return IsAllowedHost(name) ? $"{scheme}{name}{port}{(rest.Length > 1 ? "/<path>" : rest)}" : replacement;
    }

    // Tools whose password (and account) arguments follow a switch, or are glued to it (sqlcmd -PSecret, 7z -pSecret).
    // A general "-p" is a port or a path flag, so these are matched only after their own command.
    private sealed record Switches(StringComparison Case, string[] Secret, string[] User, bool Next, bool Attached);

    private static readonly Switches Schtasks = new(StringComparison.OrdinalIgnoreCase, ["/P", "/RP"], ["/U", "/RU"], true, false);
    private static readonly Switches Sql = new(StringComparison.Ordinal, ["-P"], ["-U"], true, true);
    private static readonly Switches PsExec = new(StringComparison.OrdinalIgnoreCase, ["-p"], ["-u"], true, false);
    private static readonly Switches Docker = new(StringComparison.Ordinal, ["-p"], ["-u"], true, true);
    private static readonly Switches CertUtil = new(StringComparison.OrdinalIgnoreCase, ["-p", "/p"], [], true, false);
    private static readonly Switches SevenZip = new(StringComparison.Ordinal, ["-p"], [], false, true);

    // net use takes its password as the first plain argument and net user as the second (after the account name).
    private static string RedactCommand(Match match)
    {
        var command = match.Groups["command"].Value;
        var name = command.Split([' ', '\t'])[0].ToLowerInvariant().Replace(".exe", "", StringComparison.Ordinal);
        var switches = name switch
        {
            "schtasks" => Schtasks,
            "sqlcmd" or "bcp" or "osql" => Sql,
            "psexec" or "psexec64" => PsExec,
            "docker" => Docker,
            "certutil" => CertUtil,
            "7z" or "7za" or "7zr" => SevenZip,
            _ => null,
        };
        var user = switches is null && command.EndsWith("user", StringComparison.OrdinalIgnoreCase);
        var positional = 0;
        string? next = null;
        return command + Argument().Replace(match.Groups["args"].Value, argument =>
        {
            var value = argument.Value;
            if (value[0] is '/' or '-')
            {
                next = null;
                if (switches is null)
                {
                    return value;
                }

                if (switches.Next)
                {
                    next = switches.Secret.Contains(value, StringComparer.FromComparison(switches.Case)) ? "<secret>"
                        : switches.User.Contains(value, StringComparer.FromComparison(switches.Case)) ? "<user>" : null;
                }

                var glued = switches.Attached ? switches.Secret.Concat(switches.User)
                    .FirstOrDefault(flag => value.Length > flag.Length && value.StartsWith(flag, switches.Case) && !value[flag.Length..].StartsWith('<')) : null;
                return glued is null ? value : glued + (switches.Secret.Contains(glued) ? "<secret>" : "<user>");
            }

            if (next is not null)
            {
                var replacement = next;
                next = null;
                return replacement;
            }

            var bare = value.TrimStart('"');
            if (switches is not null || bare.StartsWith(@"\\", StringComparison.Ordinal) || bare.StartsWith("<path>", StringComparison.Ordinal) || NetDevice().IsMatch(value))
            {
                return value;
            }

            // An argument already redacted (<user>, <secret>) keeps its place in the count.
            positional++;
            return bare.StartsWith('<') ? value : user && positional == 1 ? "<user>" : positional == (user ? 2 : 1) ? "<secret>" : value;
        });
    }

    // Segments are split on every separator spelling and run, so mixing or doubling them (C:\Windows\Temp/x,
    // C:/Users//x) cannot dodge a limit, and the kept part is copied from the original text.
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

        // "." and ".." (and Windows' trailing-dot spellings of them) move the path, and a blank name before a real one
        // hides where it leads, so neither can be judged by prefix.
        var lastNamed = Array.FindLastIndex(segments, segment => !Blank(segment));
        if (segments.Where((segment, index) => Name(segment).Length == 0 && (!Blank(segment) || index < lastNamed)).Any())
        {
            return Cut(value);
        }

        // A bare drive root (C:\) names nothing.
        if (lastNamed < 0)
        {
            return value;
        }

        // What follows the kept segments stays verbatim when it names nothing (trailing separators or spaces).
        string Build(int keep, bool user) =>
            root + string.Concat(Enumerable.Range(0, keep).Select(index => separators[index].Value + (user && index == 1 ? "<user>" : segments[index])))
            + (segments.Skip(keep).All(Blank) ? (keep < separators.Length ? rest[separators[keep].Index..] : "") : separators[keep].Value + Cut(rest[separators[keep].Index..]));

        if (drive == "C" && Name(segments[0]).Equals("Users", StringComparison.OrdinalIgnoreCase))
        {
            // Already redacted ("C:\Users\" before a placeholder) or the bare profiles folder: nothing to hide.
            if (lastNamed == 0)
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

        return Cut(value);
    }

    // A sequence is a name only with CN, OU or DC, with O beside another attribute, or after a Subject:/Issuer: label,
    // so "retries=3, s=5" and "setup.exe /s=1 /L=1033" stay. Countries (C=US), allowed hosts and public issuers stay.
    private string RedactDirectoryName(Match match)
    {
        static string Key(Match part) => part.Groups["key"].Value.Split('=')[0].Trim().ToUpperInvariant();
        var label = match.Groups["label"].Length;
        bool Public(Match part)
        {
            var value = part.Groups["value"].Value.Trim().Trim('"');
            return IsAllowedHost(value.ToLowerInvariant()) || PublicIssuer().IsMatch(value);
        }

        var parts = DirectoryPart().Matches(match.Value[label..]);
        var name = label > 0 || parts.Any(part => Key(part) is "CN" or "OU" or "DC") || (parts.Count > 1 && parts.Any(part => Key(part) == "O"));

        // A public issuer's own name (CN=Microsoft Code Signing PCA 2011, O=Microsoft Corporation) keeps its locality too.
        var issuer = parts.Where(part => Key(part) is "CN" or "O").DefaultIfEmpty().All(part => part is not null && Public(part));
        return !name ? match.Value : match.Value[..label] + DirectoryPart().Replace(match.Value[label..], part =>
            Key(part) == "C" || Public(part) || (issuer && Key(part) is "L" or "S" or "ST") ? part.Value : part.Groups["key"].Value + "<private>");
    }

    // A path match runs on through spaces, so the space before the next word is given back.
    private static string Cut(string value) => "<path>" + value[value.TrimEnd().Length..];

    private static bool Blank(string segment) => segment.Trim().Length == 0;

    // Windows ignores trailing dots and spaces in a name, so "Temp." and "Temp " are the Temp folder.
    private static string Name(string segment) => segment.Trim().TrimEnd('.', ' ');

    private static bool StartsWith(string[] segments, string[] prefix) =>
        segments.Length >= prefix.Length && prefix.Select((segment, index) => segment.Equals(Name(segments[index]), StringComparison.OrdinalIgnoreCase)).All(same => same);

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

    // A quoted value at any JSON nesting: "x" (with \" escapes inside), \"x\" (JSON), \\\"x\\\" and \\u0022x\\u0022
    // (JSON inside JSON), and a raw \u0022x\u0022. Each closes only with its own spelling.
    private const string Quoted = @"(?:\\\\\\""(?:(?!\\\\\\"")[^\r\n])*\\\\\\""|\\\\u0022(?:(?!\\\\u0022)[^\r\n])*\\\\u0022|\\u0022(?:(?!\\u0022)[^\r\n])*\\u0022|\\""(?:[^""\\\r\n]|\\[^""\r\n])*\\""|""(?:[^""\\\r\n]|\\.)*"")";

    // A plain token never ends in the "\" of an escaped quote, so a placeholder can never split a JSON string.
    private const string Bare = @"(?:[^\s""',;\\]|\\(?!""))+";

    private const string Placeholder = "<(?:private|secret|email|url|host|path|user|ip|mac)>";

    // A JSON \uXXXX escape left in the text (from double-escaped JSON) is a boundary, like the character it stands for.
    private const string AfterEscape = @"(?<=\\u[0-9A-Fa-f]{4})";

    private const string Colon = @"(?::|%3[Aa])";
    private const string AnySeparator = @"(?:\\\\|\\|/|%2[Ff]|%5[Cc])";
    private const string BackSeparator = @"(?:\\\\|\\|%5[Cc])(?![\\/])";

    // A drive starts after a non-alphanumeric character, a JSON \t, \n or \r escape, or a short switch (-oD:\x,
    // /ID:\x); never inside a word, so "https://" is not a drive "s:".
    // A short switch glued to a path (-oD:\x, /ID:\x, -o\\fs01\x, -o/home/x).
    private const string ShortSwitch = @"(?<=(?<![\w\\/.:-])[-/][A-Za-z]{1,3})";

    private const string DriveBehind = @"(?:(?<![A-Za-z0-9])|(?<=\\[tnr])|" + AfterEscape + "|" + ShortSwitch + ")";

    private const string DriveHead = DriveBehind + "[A-Za-z]" + Colon + AnySeparator;

    private const string UncHead = @"(?:(?<![A-Za-z0-9\\/]|(?<![A-Za-z0-9])[A-Za-z]:)|" + AfterEscape + "|" + ShortSwitch + @")(?:\\\\\\\\|\\\\)";

    // A path runs on through spaces (so "D:\Acme Projects\x" never leaks its tail) and through apostrophes inside
    // names (O'Brien, Customers' Data; only a path opened by an apostrophe ends at one). It stops at a double quote,
    // <>|, a tab, a line end, a wildcard, or a colon, which no Windows name can
    // hold (file.cs:line 42, https://), before a switch (" /c"; no Windows name starts with "/" or ends with a space),
    // and before the next drive or UNC path, so a kept path never carries another one along. A colon before a
    // backslash is kept when its letter cannot start a drive (abcD:\x), so no tail leaks.
    private const string PathTail = @"(?:(?!" + DriveHead + "|" + UncHead + @"| /|(?<!\\)\\"")(?:[^""'<>|:\r\n\t*?]|(?(open)" + QuotedApostrophe + "|')|:(?=" + BackSeparator + @")))*";

    private const string PosixTail = @"(?:[^""'<>|:\r\n\t*?]|(?(open)" + QuotedApostrophe + "|'))*";

    // Inside a path opened by an apostrophe, an apostrophe stays part of a name when doubled (PowerShell's '' escape),
    // before a letter or digit (O'Brien), when more path (a "\" or "/") follows before the next apostrophe
    // ('D:\Customers' Data\x'), or when the next apostrophe is the one that closes the quote, before a lower-case word
    // or the line end ('D:\Jones' Bakery.docx' is denied); otherwise it closes the path.
    private const string QuotedApostrophe = @"(?:''|'(?=[A-Za-z0-9])|'(?=[^'\r\n]*[\\/][^'\r\n]*')|'(?=[^'\r\n]*'(?:[ \t]+[a-z]|[ \t]*[.,;:)]*[ \t]*(?:\r|\n|\z))))";

    // Set when a path follows an opening apostrophe ('D:\x'), so only then does an apostrophe end it.
    private const string Open = "(?<open>(?<='))?";

    // Two-letter country domains that are also common file extensions or identifiers (main.md, x.py, user.id, arr.at)
    // count only after two or more labels (www.contoso.it); "vi" never counts, so LabVIEW files (main.vi) stay.
    private const string SafeDomains = "com|net|org|edu|gov|mil|int|info|biz|aero|coop|museum|mobi|asia|tel|travel|jobs|app|dev|io|ai|co|cloud|tech|online|xyz"
        + "|local|lan|corp|internal|intranet|home|arpa|localdomain|onion"
        + "|ad|ae|af|ag|al|ao|aq|ar|au|aw|ax|az|ba|bb|bd|be|bf|bg|bh|bi|bj|bm|bn|bo|br|bs|bt|bw|bz|ca|cd|cf|cg|ch|ci|ck|cl|cm|cn|cr|cu|cv|cw|cx|cy|cz"
        + "|de|dj|dk|dm|dz|ec|ee|eg|er|es|et|eu|fi|fj|fk|fm|fo|fr|ga|gd|ge|gf|gg|gh|gi|gl|gm|gn|gp|gq|gr|gs|gt|gu|gw|gy|hk|hm|hn|hr|ht|hu"
        + "|ie|il|im|iq|ir|je|jm|jo|jp|ke|kg|kh|ki|km|kn|kp|kr|kw|ky|kz|lb|lc|li|lk|lr|ls|lt|lu|lv|ly|ma|mc|mg|mh|mn|mp|mq|mr|ms|mt|mu|mv|mw|mx|my|mz"
        + "|na|nc|ne|nf|ng|ni|nl|np|nr|nu|nz|om|pa|pe|pf|pg|ph|pk|pn|pr|pt|pw|qa|re|ro|ru|rw|sa|sb|sc|sd|se|sg|si|sk|sl|sm|sn|sr|ss|st|su|sx|sy|sz"
        + "|tc|td|tg|th|tj|tk|tl|tm|tn|tr|tt|tv|tw|tz|ua|ug|uk|us|uy|uz|va|vc|ve|vg|vn|vu|wf|ws|ye|yt|za|zm|zw";

    private const string RiskyDomains = "ac|am|as|at|by|cc|do|id|in|is|it|la|md|me|mk|ml|mm|mo|no|pl|pm|ps|py|rs|sh|so|sv|tf|to";

    private const string Label = @"_?[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.";

    [GeneratedRegex(@"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(-----END [A-Z ]*PRIVATE KEY-----|\z)")]
    private static partial Regex PrivateKey();

    [GeneratedRegex(@"\b(gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|glpat-[A-Za-z0-9_-]{20,}|glrt-[A-Za-z0-9_-]{20,}|GR1348941[A-Za-z0-9_-]{20,}|(AKIA|ASIA)[0-9A-Z]{16}|xox[abprs]-[A-Za-z0-9-]{10,}|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}|npm_[A-Za-z0-9]{36}|AIza[0-9A-Za-z_-]{35})")]
    private static partial Regex Token();

    // Any scheme, and the whole value: to the closing quote (plain or escaped) when quoted, else to the end of the
    // line, so multi-part values (AWS4-HMAC-SHA256 Credential=..., Signature=...) never leak a part. The separator may
    // be ":", "=" or a table gap of two or more spaces. A well-known scheme name stays.
    [GeneratedRegex(@"(?<key>\b(?:Proxy-)?Authorization(?:\\?"")?(?:[ \t]*[:=][ \t]*|[ \t]{2,}(?=\S)))(?:(?<quote>\\?"")(?<scheme>(?:" + AuthSchemes + @")[ \t]+)?(?:[^""\\\r\n]|\\(?!""))+|(?<scheme>(?:" + AuthSchemes + @")[ \t]+)?[^""\r\n][^\r\n]*)", RegexOptions.IgnoreCase)]
    private static partial Regex Authorization();

    // A standalone credential only when it looks like one (16+ characters with a digit), so "Basic authentication" stays.
    [GeneratedRegex(@"(?<scheme>\b(?:Bearer|Basic)\s+)(?=[A-Za-z0-9._~+/=-]*[0-9])[A-Za-z0-9._~+/=-]{16,}", RegexOptions.IgnoreCase)]
    private static partial Regex Bearer();

    // Plain or escaped inside a JSON string (key=\"Password\" value=\"x\"); the quoting is kept.
    [GeneratedRegex(@"(?<key>\bkey\s*=\s*\\?""[^""\r\n]*(?:password|passwd|pwd|secret|token|key)[^""\r\n]*""\s+value\s*=\s*)(?<value>\\""(?:[^""\\\r\n]|\\[^""\r\n])*\\""|""[^""\r\n]*"")", RegexOptions.IgnoreCase | RegexOptions.NonBacktracking)]
    private static partial Regex XmlSecret();

    [GeneratedRegex(@"(?<command>\b(?:net(?:\.exe)?[ \t]+(?:use|user)|docker(?:\.exe)?[ \t]+login|(?:schtasks|sqlcmd|bcp|osql|psexec(?:64)?|certutil|7z[ar]?)(?:\.exe)?)\b)(?<args>[^\r\n]*)", RegexOptions.IgnoreCase)]
    private static partial Regex Command();

    // An argument is a run of plain characters and quoted parts, so -p"two words" and "\\fs01\a b" are one argument;
    // an unclosed quote runs to the end of the line.
    [GeneratedRegex(@"(?:[^\s""]|""[^""\r\n]*(?:""|$))+")]
    private static partial Regex Argument();

    [GeneratedRegex(@"^(?:[A-Za-z]:|\*|(?:LPT|COM)[0-9]:?)$", RegexOptions.IgnoreCase)]
    private static partial Regex NetDevice();

    // ConvertTo-SecureString "x" -AsPlainText, ... -String x, and "x" | ConvertTo-SecureString. The piped form has no key
    // to anchor it, so its quoted value never spans an escaped quote and each start scans only to the next quote.
    [GeneratedRegex(@"(?<key>\bConvertTo-SecureString\b(?:[ \t]+-(?:AsPlainText|Force|String)\b)*[ \t]+)(?:" + Quoted + @"|'[^'\r\n]*'|(?!-)(?:[^\s|;)""\\]|\\(?!""))+)|(?:""[^""\r\n]*""|\\""[^""\r\n]*\\""|'[^'\r\n]*')(?=[ \t]*\|[ \t]*ConvertTo-SecureString\b)", RegexOptions.IgnoreCase)]
    private static partial Regex SecureString();

    // new NetworkCredential("jane", "x"), New-Object System.Net.NetworkCredential -ArgumentList 'jane', 'x', and
    // [NetworkCredential]::new($env:USERNAME, "x"); a literal account becomes <user>, a variable stays.
    [GeneratedRegex(@"(?<head>\bNetworkCredential\]?(?:::new)?(?:[ \t]*\(|[ \t]+-ArgumentList)[ \t]*)(?<user>" + Quoted + @"|'[^'\r\n]*'|\$[\w:.]+)(?<separator>[ \t]*,[ \t]*)(?!" + Placeholder + ")(?:" + Quoted + @"|'[^'\r\n]*')")]
    private static partial Regex Credential();

    // Command-line flags: "--token x", "-pw x", "--password=x", "-passout pass:x", and Windows switches "/pass:x".
    [GeneratedRegex(@"(?<key>(?<![\w/-])(?:--?[A-Za-z0-9_-]*(?:token|password|passwd|pwd|secret|pat|api-?key|pw|pass(?:in|out)?)\b(?:\s+|[=:])|/[A-Za-z0-9_-]*(?:token|password|passwd|pwd|secret|pass)\b:))(?!-|" + Placeholder + @")(?:" + Quoted + @"|'[^'\r\n]*'|" + Bare + ")", RegexOptions.IgnoreCase)]
    private static partial Regex FlagSecret();

    // Name with any prefix (ACTIONS_RUNNER_INPUT_TOKEN, DB_PASSWORD, "api_key", \"password\"), then ":"/"=" or a
    // table gap of 2+ spaces. A value after a ";" (a connection-string item) runs to the next ";", the line end or a
    // quote, so a multi-word password never leaks a word; elsewhere a ";" ends it. Both are capped at 256 characters.
    // A value that starts with a PowerShell credential cmdlet ($pw = Read-Host ...) is code, not a secret, and a number
    // after the short keys pw and pass is a count (Pass: 9). A reg query type column (REG_SZ) is never the value.
    [GeneratedRegex(@"(?<ctx>(?<=;[ \t]{0,8}))?(?<key>(?<![A-Za-z0-9_.-])(?:[A-Za-z0-9_.-]*(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|account[_-]?key|shared[_-]?access[_-]?key|private[_-]?key|client[_-]?secret|credentials?)|(?<short>(?:[A-Za-z0-9_.-]*[_.-])?(?:pw|pass)))(?:\\{0,3}""|\\{1,2}u0022)?)(?<separator>\s*[:=]\s*|[ \t]{2,}(?=\S)(?!REG_[A-Z0-9_]+[ \t]))(?!" + Placeholder + @")(?(short)(?![0-9]+(?![\w.])))(?:" + Quoted + @"|'[^'\r\n]*'|(?!(?:Get|Read|ConvertTo|ConvertFrom|New|Import|Unprotect)-[A-Z])(?(ctx)(?:[^;\r\n""\\]|\\(?!"")){1,256}|(?:(?:[^;\r\n\\]|\\(?!"")){1,256}?(?=;)|(?:[^\s,;\\]|\\(?!""))+)))", RegexOptions.IgnoreCase)]
    private static partial Regex Assignment();

    // reg query rows (name, type, data) and netsh "Key Content : x": the data runs to the end of the line. Numeric types
    // (REG_DWORD, REG_QWORD) are flags such as DisablePasswordChange and stay.
    [GeneratedRegex(@"(?<key>\b(?:[\w.-]*(?:password|passwd|pwd|secret|token|credential|apikey|api_key|accesskey|accountkey|productkey|privatekey)[\w.-]*|[\w.-]*(?:pass|pw))[ \t]{2,}REG_(?:SZ|EXPAND_SZ|MULTI_SZ|BINARY|NONE)[ \t]+|\bKey Content[ \t]*:[ \t]*)[^\s][^\r\n]*", RegexOptions.IgnoreCase | RegexOptions.NonBacktracking)]
    private static partial Regex Registry();

    [GeneratedRegex(@"(?<=[?&;]|\\u00(?:26|3[Ff]|3[Bb]))sig=(?:[^&\s""'<>;\\]|\\(?!""))+", RegexOptions.IgnoreCase)]
    private static partial Regex SasSignature();

    // Account names given as switches or connection-string keys: /user:CORP\jane, --username jane, User Id=sa.
    [GeneratedRegex(@"(?<key>(?<![\w/-])/u(?:ser)?:|(?<![\w-])--user(?:name)?=|(?<![\w-])--username[ \t]+|\bUser[ \t_]?(?:Id|Name)[ \t]*=[ \t]*)(?!" + Placeholder + @")(?:" + Quoted + @"|(?:[^\s;,""\\]|\\(?!""))+)", RegexOptions.IgnoreCase)]
    private static partial Regex UserFlag();

    // Plain and JSON-escaped (https:\/\/) URLs.
    [GeneratedRegex(@"\b(?<scheme>[A-Za-z][A-Za-z0-9+.-]*)(?<separator>://|:\\/\\/)(?:[^\s/\\@""'<>]+@)?(?<host>[^\s/\\:""'<>?#]+)(?<port>:[0-9]+)?(?<rest>(?:[^\s""'<>\\]|\\[^\s""'<>])*)", RegexOptions.NonBacktracking)]
    private static partial Regex Url();

    // Any user@host:path remote, including SSH host aliases with no TLD (git@github.com-customer:org/repo.git), and a
    // user-less host:path remote when it names a repository (build01:acme/repo.git).
    [GeneratedRegex(@"\b[A-Za-z0-9._-]+@[A-Za-z0-9._-]+:[^\s""'<>]+|\b[A-Za-z0-9][A-Za-z0-9._-]+:[A-Za-z0-9_~.-][^\s""'<>\\]*\.git\b", RegexOptions.NonBacktracking)]
    private static partial Regex ScpUrl();

    // An image digest (alpine@sha256:...) is a hash, not a remote.
    [GeneratedRegex(@"^[A-Za-z0-9._-]+@sha(?:256|384|512):[0-9A-Fa-f]{32,}$")]
    private static partial Regex DigestReference();

    [GeneratedRegex(@"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", RegexOptions.NonBacktracking)]
    private static partial Regex Email();

    // One rule for every drive-path spelling: X:\, X:/, JSON-escaped X:\\, file:///X:/, X%3A/, \\?\X:\ and \\.\X:\.
    [GeneratedRegex(Open + DriveBehind + @"(?<long>file:/{2,3}|\\\\\\\\[?.]\\\\|\\\\[?.]\\)?[A-Za-z]" + Colon + AnySeparator + PathTail)]
    private static partial Regex DrivePath();

    [GeneratedRegex(Open + UncHead + @"(?![?.](?:\\\\|\\)[A-Za-z]" + Colon + @")(?:[A-Za-z0-9._$-]+|\?)" + PathTail)]
    private static partial Regex UncPath();

    // //server/share and file:////server/share spellings of a UNC path.
    [GeneratedRegex(Open + @"(?:\bfile:/{4,5}|(?:(?<![\w:/.-])|" + AfterEscape + "|" + ShortSwitch + @")//)[A-Za-z0-9._$-]+/(?=[^\s/])" + PathTail)]
    private static partial Regex UncForward();

    // Git Bash (/c/...), WSL (/mnt/c/...) and Cygwin (/cygdrive/c/...) spellings of a drive path.
    [GeneratedRegex(Open + @"(?:(?<![\w.~-])|" + AfterEscape + "|" + ShortSwitch + @")(?:/mnt|/cygdrive)?/[A-Za-z]/" + PosixTail)]
    private static partial Regex PosixDrive();

    [GeneratedRegex(Open + @"(?<root>(?:(?<![\w.~-])|" + AfterEscape + "|" + ShortSwitch + @")/(?:home|Users)/)(?!<)(?:[^/""'<>|:\r\n\t*?]|(?(open)" + QuotedApostrophe + "|'))+(?<rest>/" + PosixTail + ")?", RegexOptions.IgnoreCase)]
    private static partial Regex PosixHome();

    [GeneratedRegex(@"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b")]
    private static partial Regex Mac();

    // An address at the end of a sentence still counts; a version (Version=13.0.0.0, "ProductVersion": "1.0.0.0",
    // v1.2.3.4) does not.
    [GeneratedRegex(@"(?<![0-9]|[0-9]\.|[Vv]ersion""?[ \t]{0,32}[=:]?[ \t]{0,32}""?|\bv)(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?!\.?[0-9])")]
    private static partial Regex IPv4();

    [GeneratedRegex(@"(?<![0-9A-Za-z:])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![0-9A-Za-z:])")]
    private static partial Regex IPv6();

    private const string DirectoryKey = @"\b(?:CN|OU|DC|ST|O|C|L|S|E)[ \t]{0,2}=[ \t]{0,2}";

    // Any attribute may follow a known one (SERIALNUMBER=, OID.2.5.4.5=, SN=, G=), so an unknown one never ends a name.
    private const string AnyKey = @"\b[A-Za-z][\w.]{0,40}[ \t]{0,2}=[ \t]{0,2}";

    // Attributes are separated by "," ";" or "/", or by a line break: certutil -dump prints one per indented line, and
    // long subjects wrap after a ",".
    private const string DirectorySeparator = @"(?:[ \t]*[,;/][ \t]*(?:\r?\n[ \t]*)?|[ \t]*\r?\n[ \t]+)";

    // A value is quoted when it holds a comma (O="Contoso, Inc.", Windows and OpenSSL), or starts with a visible
    // character; a "/" ends it only before the next attribute (/C=US/O=x), so OU=Sales/Marketing keeps its slash.
    private const string DirectoryValue = @"(?:""(?:[^""\\\r\n]|\\.|"""")*""|(?:[^,;+""\r\n<>=\\/ \t]|\\.)(?:[^,;+""\r\n<>=\\/]|\\.|/(?![A-Za-z]{1,2}[ \t]{0,2}=))*)";

    // Directory names and certificate subjects as a whole: CN=Jane Doe,OU=Lab,DC=contoso,DC=local; curl's
    // "C=US; O=x; CN=y"; OpenSSL's "C = US, O = x" and "/C=US/O=x"; after an optional Subject:/Issuer: label.
    [GeneratedRegex(@"(?:(?<label>\b(?:Subject|Issuer)[ \t]{0,32}[:=](?:[ \t]*\r?\n[ \t]+|[ \t]{0,4}))/?" + AnyKey + "|/?" + DirectoryKey + ")" + DirectoryValue + "(?:" + DirectorySeparator + AnyKey + DirectoryValue + ")*", RegexOptions.IgnoreCase)]
    private static partial Regex DirectoryName();

    [GeneratedRegex("(?<key>" + AnyKey + ")(?<value>" + DirectoryValue + ")", RegexOptions.IgnoreCase)]
    private static partial Regex DirectoryPart();

    // Public certificate authorities and vendors, so TLS and code-signing issuers stay readable. The whole value must be
    // the issuer's name followed only by certificate-naming words (Microsoft Code Signing PCA 2011, DigiCert Global
    // Root G2), so "Contoso Microsoft Teams Room" and "Microsoft Teams Room - Contoso" are still private.
    [GeneratedRegex(@"^\s*(?:DigiCert|GlobalSign|Sectigo|COMODO|USERTrust|ISRG|Let's Encrypt|Entrust|GoDaddy|Starfield|VeriSign|Thawte|GeoTrust|Baltimore|IdenTrust|QuoVadis|Certum|Symantec|Microsoft|Amazon|Google Trust Services|GitHub|Internet Security Research Group)(?:[\s,.-]+(?:Corporation|Corp|Inc|LLC|Limited|Ltd|Global|Root|CA|CAs|Certificate|Certification|Authority|Code|Signing|Server|Secure|TLS|SSL|RSA|ECC|EV|PCA|Intermediate|Issuing|Public|Primary|Universal|Trust|Trusted|Services?|Assured|ID|High|Assurance|Extended|Validation|Domain|Organization|Time-?Stamp(?:ing)?|Timestamping|Production|Windows|Azure|Update|Hybrid|SHA[0-9]*|[A-Z][0-9]+|[0-9]+|\(c\)|nv-sa))*\.?\s*$", RegexOptions.IgnoreCase | RegexOptions.NonBacktracking)]
    private static partial Regex PublicIssuer();

    // Bare host names with a known top-level domain (fs01.corp.example.com, _ldap._tcp.example.com, x.co.jp/path).
    // File names (main.vi, ollama.exe), .NET namespaces (System.IO, Microsoft.NETCore.App, ASP.NET) and Windows .com
    // programs (System32\chcp.com) are not hosts.
    [GeneratedRegex(@"(?:(?<![\w.@/-])|" + AfterEscape + @")(?<!(?:System32|SysWOW64)[\\/]{1,4})(?!(?-i:System|Microsoft|ASP|Windows|Newtonsoft|NuGet)\.)(?<host>(?:" + Label + ")+(?:" + SafeDomains + ")|(?:" + Label + "){2,}(?:" + RiskyDomains + @")|(?<=\b(?:Suffix|Domain|ComputerName|Resolve-DnsName|nslookup|ping|tracert|Test-NetConnection|Test-Connection)[ \t.:=]{1,40})(?:" + Label + ")(?:" + RiskyDomains + @"))(?<rest>/[^\s""'<>]*)?(?![\w-]|\.[\w-])", RegexOptions.IgnoreCase)]
    private static partial Regex Host();

    // The account in user@address once the address is redacted (ssh jane@10.0.0.5).
    [GeneratedRegex(@"(?<![\w.-])[A-Za-z0-9._-]+@(?=<(?:ip|host)>)")]
    private static partial Regex AccountAt();

    [GeneratedRegex(@"(?<!\\)(?<pairs>(?:\\\\)*)(?:\\u(?<hex>[Dd][89ABab][0-9A-Fa-f]{2})\\u(?<low>[Dd][C-Fc-f][0-9A-Fa-f]{2})|\\u(?<hex>[0-9A-Fa-f]{4}))")]
    private static partial Regex JsonEscape();

    // A JSON string literal whose every escape is valid; raw Windows paths ("C:\Temp") are not literals.
    [GeneratedRegex(@"""(?:[^""\\\r\n]|\\[""\\/bfnrt]|\\u[0-9A-Fa-f]{4})*""", RegexOptions.NonBacktracking)]
    private static partial Regex JsonString();

    [GeneratedRegex(@"(?<!\\)\\+u(?:00[Aa][Dd]|200[B-Fb-f]|202[A-Ea-e]|206[0-4]|[Ff][Ee][Ff][Ff])")]
    private static partial Regex EscapedInvisible();

    [GeneratedRegex("(" + Placeholder + ")")]
    private static partial Regex PlaceholderSplit();

    [GeneratedRegex(@"(?:\\|/|%2[Ff]|%5[Cc])+")]
    private static partial Regex Separator();

    [GeneratedRegex(@"^[A-Za-z]:\\")]
    private static partial Regex DriveRoot();
}
