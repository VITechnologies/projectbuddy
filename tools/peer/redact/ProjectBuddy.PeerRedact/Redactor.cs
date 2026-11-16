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
        return new Redaction(Settle(text, counts, settledOnly), counts);
    }

    private string Settle(string text, Dictionary<string, int> counts, bool settledOnly)
    {
        var result = text;
        for (var pass = 0; pass < Passes; pass++)
        {
            var next = Pass(Clean(result), counts);
            if (next == result)
            {
                return result;
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
        string Apply(string source, string rule, Regex pattern, MatchEvaluator evaluator) => pattern.Replace(source, match =>
        {
            var replaced = evaluator(match);
            if (replaced != match.Value)
            {
                counts[rule] = counts.GetValueOrDefault(rule) + 1;
            }

            return replaced;
        });

        // Name/Value JSON (an object's Key/Value members, an argument array's flag and value) is read while its strings are
        // still plain JSON, before the escaped ones are redacted and masked below.
        text = RedactJsonLayouts(text, counts);

        // A JSON string literal with escapes, between JSON punctuation ({"k":"v"}), is redacted as the text it stands for:
        // decoded (so "\n" is a line break and "\"" a quote), settled with every rule, nested JSON included, and
        // encoded again, so the output stays well formed. While the rules below run over the rest of the text, it is
        // held behind a quoted mask of noncharacters: a value rule ("password":"...") still takes it whole, and nothing
        // reads its escapes as path separators. A quoted raw path ("C:\temp\new") is not between JSON punctuation and
        // stays with the rules. A literal holding what may be a raw drive or UNC path ("D:\repos\fabrikam",
        // "\\fs01\temp"; an encoder always doubles the backslash there) is read both ways: decoded and redacted, then
        // left unmasked so the path rules read it raw as well.
        var literals = new List<string>();
        var input = text;
        text = JsonString().Replace(text, literal =>
        {
            if (!literal.Value.Contains('\\') || !InJson(input, literal))
            {
                return literal.Value;
            }

            // A literal that may be a raw path is read both ways: decoded here, then left unmasked for the path rules.
            var redacted = Quote(Settle(Unescape(literal.Value), counts, false));
            if (RawPath().IsMatch(literal.Value))
            {
                return redacted;
            }

            literals.Add(redacted);
            return "\"\uFDD0" + string.Concat((literals.Count - 1).ToString(CultureInfo.InvariantCulture).Select(digit => (char)('\uFDD2' + digit - '0'))) + "\uFDD1\"";
        });

        var result = Apply(text, "secret", PrivateKey(), _ => "<secret>");
        result = Apply(result, "secret", Token(), _ => "<secret>");
        result = Apply(result, "secret", Authorization(), match => match.Groups["key"].Value + match.Groups["quote"].Value + match.Groups["scheme"].Value + "<secret>");
        result = Apply(result, "secret", Bearer(), match => match.Groups["scheme"].Value + "<secret>");
        result = Apply(result, "secret", XmlSecret(), match => SecretWord().IsMatch(match.Groups["name"].Value) ? match.Groups["key"].Value + Requote(match.Groups["value"].Value) : match.Value);
        result = Apply(result, "secret", Command(), RedactCommand);
        result = Apply(result, "secret", SecureString(), match => match.Groups["key"].Value + "<secret>");
        result = Apply(result, "secret", Credential(), match => match.Groups["head"].Value
            + (match.Groups["user"].Value.StartsWith('$') ? match.Groups["user"].Value : "<user>") + match.Groups["separator"].Value + "<secret>");
        result = Apply(result, "secret", FlagSecret(), match => match.Groups["key"].Value + "<secret>");
        result = Apply(result, "secret", Registry(), match => SecretToClose(match.Groups["key"].Value, match.Groups["value"].Value));
        result = Apply(result, "secret", XmlElement(), RedactXmlElement);
        result = Apply(result, "secret", XmlValueChild(), match => SecretName().IsMatch(LocalName(match.Groups["tag"].Value)) ? match.Groups["head"].Value + "<secret>" : match.Value);
        result = Apply(result, "secret", XmlNameValue(), match => SecretName().IsMatch(match.Groups["name"].Value.Trim()) ? match.Groups["head"].Value + "<secret>" : match.Value);
        result = Apply(result, "secret", PsObject(), RedactPsObject);
        result = Apply(result, "secret", ProcessRecord(), RedactProcessRecord);
        result = Apply(result, "secret", ProcessRecordArgumentsFirst(), RedactProcessRecord);
        result = Apply(result, "secret", Record(), RedactRecord);
        result = Apply(result, "secret", RecordValueFirst(), RedactRecord);
        result = Apply(result, "secret", CsvBlock(), RedactCsv);
        result = Apply(result, "secret", LineAssignment(), RedactAssignment);
        result = Apply(result, "secret", QuotedAssignment(), match => match.Groups["key"].Value + "<secret>");
        result = Apply(result, "secret", Assignment(), RedactAssignment);
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

        return literals.Count == 0 ? result : Mask().Replace(result, mask =>
            literals[int.Parse(string.Concat(mask.Groups["index"].Value.Select(digit => (char)(digit - '\uFDD2' + '0'))), CultureInfo.InvariantCulture)]);
    }

    // Between JSON punctuation: after "{", "[", ":" or "," (or the start) and before ":", ",", "]" or "}" (or the end).
    private static bool InJson(string text, Match literal)
    {
        var before = literal.Index - 1;
        while (before >= 0 && char.IsWhiteSpace(text[before]))
        {
            before--;
        }

        var after = literal.Index + literal.Length;
        while (after < text.Length && char.IsWhiteSpace(text[after]))
        {
            after++;
        }

        return (before < 0 || text[before] is '{' or '[' or ':' or ',') && (after == text.Length || text[after] is ':' or ',' or ']' or '}');
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
            else if (CharUnicodeInfo.GetUnicodeCategory(character) != UnicodeCategory.Format && character is < '\uFDD0' or > '\uFDEF')
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

    // Name/Value layouts keep a secret's name and its value in sibling fields, not key=value: JSON objects
    // ({"Key":"DB_PASSWORD","Value":"x"}, Win32_Environment's VariableValue), Format-List or YAML records
    // (Name : DB_PASSWORD / Value : x), CSV tables with Name and Value columns, and JSON argument arrays
    // (["sqlcmd","-P","x"]). When the name holds a secret word, the value is secret.
    private static readonly string[] NameFields = ["Name", "Key"];

    private static readonly string[] ValueFields = ["Value", "VariableValue", "Data"];

    private static readonly string[] ProgramFields = ["FileName", "Path", "Executable", "Program", "Command"];

    private static readonly string[] ArgumentFields = ["Arguments", "ArgumentList", "Args", "Argv"];

    private const int MaxJsonDepth = 256;

    private sealed record JsonValue(string? Name, int Start, int End, bool IsString, JsonFrame? Child, bool Cut = false);

    private sealed class JsonFrame(bool isObject, int start, string? member, bool inParent)
    {
        public bool IsObject { get; } = isObject;

        public int Start { get; } = start;

        public string? Member { get; } = member;

        public bool InParent { get; } = inParent;

        public List<JsonValue> Values { get; } = [];

        public string? Name { get; set; }

        public bool ExpectValue { get; set; } = !isObject;
    }

    // One linear scan pairs sibling members at every object depth, so an environment item that also holds nested
    // objects (PSDrive, PSProvider in Get-ChildItem env: | ConvertTo-Json) is still read as Key/Value, and an argument
    // array is read with its command, named in the array or in a sibling FileName. Text that is not JSON is left as
    // it is: a closing bracket that does not match starts the scan over.
    private static string RedactJsonLayouts(string text, Dictionary<string, int> counts)
    {
        if (text.IndexOfAny(['{', '[']) < 0)
        {
            return text;
        }

        var stack = new List<JsonFrame>();
        var edits = new SortedDictionary<int, (int End, string Text)>();
        int stringStart = -1, stringEnd = -1, previous = -1, unterminatedUntil = -1;

        // Frames are always finished before they are dropped, so a stray closer or the depth cap never skips a pair.
        void Reset()
        {
            for (var depth = stack.Count - 1; depth >= 0; depth--)
            {
                Finish(text, stack[depth], edits);
            }

            stack.Clear();
        }

        for (var index = 0; index < text.Length; index++)
        {
            var character = text[index];
            if (char.IsWhiteSpace(character))
            {
                continue;
            }

            var top = stack.Count > 0 ? stack[^1] : null;
            if (character == '"')
            {
                // Once a quote finds no closing quote before the line end, no later quote on that line can: the escape
                // pairing from here on is the same, so the line is not scanned again.
                var end = index < unterminatedUntil ? -1 : StringEnd(text, index);
                if (end < 0 && index >= unterminatedUntil)
                {
                    var lineEnd = text.IndexOfAny(['\r', '\n'], index);
                    unterminatedUntil = lineEnd < 0 ? text.Length : lineEnd;
                }

                if (end < 0 && top is { ExpectValue: true })
                {
                    // A string cut off at the line end (truncated JSON) is taken to the line end.
                    top.Values.Add(new(top.Name, index, unterminatedUntil, true, null, Cut: true));
                    top.ExpectValue = false;
                    index = unterminatedUntil - 1;
                }
                else if (end > 0 && (top is { ExpectValue: true } || ClosesJsonString(text, end)))
                {
                    if (top is { ExpectValue: true })
                    {
                        top.Values.Add(new(top.Name, index, end, true, null));
                        top.ExpectValue = false;
                    }

                    (stringStart, stringEnd, index) = (index, end, end - 1);
                }
            }
            else if (character is '{' or '[')
            {
                // Past the depth cap the scan starts over at this bracket, so memory stays bounded and nothing is skipped.
                var inParent = top is { ExpectValue: true } && stack.Count < MaxJsonDepth;
                if (stack.Count >= MaxJsonDepth)
                {
                    Reset();
                }

                stack.Add(new(character == '{', index, inParent ? top!.Name : null, inParent));
                if (inParent)
                {
                    top!.ExpectValue = false;
                }
            }
            else if (character is '}' or ']')
            {
                if (top is null || top.IsObject != (character == '}'))
                {
                    Reset();
                }
                else
                {
                    stack.RemoveAt(stack.Count - 1);
                    Finish(text, top, edits);
                    if (top.InParent)
                    {
                        stack[^1].Values.Add(new(top.Member, top.Start, index + 1, false, top));
                    }
                }
            }
            else if (character == ':')
            {
                if (top is { IsObject: true } && previous == stringEnd - 1)
                {
                    top.Name = text[(stringStart + 1)..(stringEnd - 1)];
                    top.ExpectValue = true;
                }
            }
            else if (character == ',')
            {
                if (top is not null)
                {
                    top.Name = null;
                    top.ExpectValue = !top.IsObject;
                }
            }
            else if (top is { ExpectValue: true })
            {
                var stop = index;
                while (stop < text.Length && text[stop] is not (',' or '}' or ']') && !char.IsWhiteSpace(text[stop]))
                {
                    stop++;
                }

                top.Values.Add(new(top.Name, index, stop, false, null));
                top.ExpectValue = false;
                index = stop - 1;
            }

            previous = index;
        }

        // Truncated JSON (a cut log line) is read as far as it goes.
        Reset();

        if (edits.Count == 0)
        {
            return text;
        }

        var result = new StringBuilder(text.Length);
        var position = 0;
        foreach (var (start, (end, replacement)) in edits)
        {
            if (start < position)
            {
                continue;
            }

            result.Append(text, position, start - position).Append(replacement);
            var rule = replacement.Contains("<user>", StringComparison.Ordinal) ? "user" : "secret";
            counts[rule] = counts.GetValueOrDefault(rule) + 1;
            position = end;
        }

        return result.Append(text, position, text.Length - position).ToString();
    }

    // The index after a string's closing quote, or -1 when the line ends first.
    private static int StringEnd(string text, int start)
    {
        for (var index = start + 1; index < text.Length; index++)
        {
            switch (text[index])
            {
                case '"':
                    return index + 1;
                case '\\':
                    index++;
                    break;
                case '\r' or '\n':
                    return -1;
            }
        }

        return -1;
    }

    // Outside a value position a string counts only when JSON punctuation (or the line end) follows its closing quote,
    // so a stray quote in prose or a command line ("C:\scripts\" deploy.cmd {...}) cannot shift the pairing of the
    // quotes after it.
    private static bool ClosesJsonString(string text, int end)
    {
        while (end < text.Length && text[end] is ' ' or '\t')
        {
            end++;
        }

        return end == text.Length || text[end] is ':' or ',' or '}' or ']' or '\r' or '\n';
    }

    private static string StringText(string text, JsonValue value)
    {
        var literal = text[value.Start..value.End];
        return value.Cut ? literal[1..]
            : JsonString().Match(literal) is { Success: true } match && match.Length == literal.Length ? Unescape(literal) : literal[1..^1];
    }

    private static void Finish(string text, JsonFrame frame, SortedDictionary<int, (int End, string Text)> edits)
    {
        if (!frame.IsObject)
        {
            RedactArguments(text, frame.Values, null, edits);
            return;
        }

        bool Is(JsonValue value, string[] fields) => fields.Contains(value.Name!, StringComparer.OrdinalIgnoreCase);
        if (frame.Values.Any(value => value.IsString && Is(value, NameFields) && SecretName().IsMatch(StringText(text, value))))
        {
            foreach (var value in frame.Values.Where(value => Is(value, ValueFields) && !(value.IsString && Redacted(StringText(text, value)))))
            {
                edits[value.Start] = (value.End, "\"<secret>\"");
            }
        }

        var program = frame.Values.FirstOrDefault(value => value.IsString && Is(value, ProgramFields));
        foreach (var arguments in frame.Values.Where(value => value.Child is { IsObject: false } && Is(value, ArgumentFields)))
        {
            RedactArguments(text, arguments.Child!.Values, program is null ? null : StringText(text, program), edits);
        }

        // ProcessStartInfo.Arguments is one string: it is read as the FileName's command line.
        foreach (var arguments in frame.Values.Where(value => program is not null && value.IsString && Is(value, ArgumentFields)))
        {
            var original = StringText(text, arguments);
            var redacted = RedactArgumentText(StringText(text, program!), original);
            if (redacted != original)
            {
                edits[arguments.Start] = (arguments.End, Quote(redacted));
            }
        }
    }

    // In an argument array the element after a secret flag, or after the command's own password switch, is secret;
    // after an account switch it is <user>. The command is the first element, or the sibling FileName.
    private static void RedactArguments(string text, List<JsonValue> elements, string? program, SortedDictionary<int, (int End, string Text)> edits)
    {
        var words = elements.Select(element => element.IsString ? StringText(text, element) : null).ToList();
        var switches = CommandSwitches(program ?? words.FirstOrDefault() ?? "");

        // docker's -p and -u are ports and users of a container, except after the "login" subcommand (which may follow
        // global options: docker --context prod login -p x).
        var from = switches == Docker ? words.IndexOf("login") : -1;
        if (switches == Docker && from < 0)
        {
            switches = null;
        }

        for (var index = 0; index + 1 < words.Count; index++)
        {
            var (flag, next) = (words[index], words[index + 1]);
            if (flag is null || next is null || next.StartsWith('-') || next.StartsWith('/') || Redacted(next))
            {
                continue;
            }

            var family = index > from ? switches : null;
            var secret = SecretFlag().IsMatch(flag) || (family is not null && family.Secret.Contains(flag, StringComparer.FromComparison(family.Case)));
            var user = !secret && (UserSwitch().IsMatch(flag) || (family is not null && family.User.Contains(flag, StringComparer.FromComparison(family.Case))));
            if (secret || user)
            {
                edits[elements[index + 1].Start] = (elements[index + 1].End, secret ? "\"<secret>\"" : "\"<user>\"");
            }
        }
    }

    // Format-List and YAML records, with the name line first or the value line first.
    private static string RedactRecord(Match match) =>
        SecretName().IsMatch(match.Groups["name"].Value.Trim()) && !Redacted(match.Groups["value"].Value)
            ? match.Groups["head"].Value + "<secret>" + match.Groups["tail"].Value
            : match.Value;

    private static bool Redacted(string value) => Placeholders.Contains(value.Trim().Trim('"'));

    private static string LocalName(string tag) => tag[(tag.LastIndexOf(':') + 1)..];

    // <Password>x</Password>, <AdministratorPassword> (by the element name), and ConvertTo-Xml's
    // <Property Name="Password">x</Property> or CLIXML's <S N="Password">x</S> (by the name attribute). A true or false
    // flag (<RequirePassword>true</RequirePassword>) is a setting, not a secret.
    private static string RedactXmlElement(Match match)
    {
        var named = match.Groups["named"].Success && SecretName().IsMatch(match.Groups["named"].Value);
        return (SecretName().IsMatch(LocalName(match.Groups["tag"].Value)) || named) && !Flag().IsMatch(match.Groups["value"].Value)
            ? match.Groups["head"].Value + "<secret>" + match.Groups["close"].Value
            : match.Value;
    }

    // @{Name=DB_PASSWORD; Value=x} (a PowerShell object written as a string) and @{Password=a b}: a value whose key is
    // a secret name, or a Value member beside a Name or Key holding one, becomes <secret>.
    private static string RedactPsObject(Match match)
    {
        var members = PsMember().Matches(match.Value);
        var secretItem = members.Any(member => NameFields.Contains(member.Groups["key"].Value.Trim(), StringComparer.OrdinalIgnoreCase) && SecretName().IsMatch(member.Groups["value"].Value.Trim()));
        return PsMember().Replace(match.Value, member =>
        {
            var key = member.Groups["key"].Value.Trim();
            var secret = (secretItem && ValueFields.Contains(key, StringComparer.OrdinalIgnoreCase)) || (!NameFields.Contains(key, StringComparer.OrdinalIgnoreCase) && SecretName().IsMatch(key));
            return secret && member.Groups["value"].Value.Trim().Length > 0 && !Redacted(member.Groups["value"].Value)
                ? member.Value[..(member.Groups["value"].Index - member.Index)] + "<secret>"
                : member.Value;
        });
    }

    // A ProcessStartInfo shown with Format-List: its Arguments read with the FileName's switch table.
    private static string RedactProcessRecord(Match match) =>
        match.Groups["head"].Value + RedactArgumentText(match.Groups["program"].Value.Trim(), match.Groups["args"].Value) + match.Groups["tail"].Value;

    // An argument string for a program (ProcessStartInfo.Arguments) is read as that program's command line.
    private static string RedactArgumentText(string program, string arguments)
    {
        var name = Path.GetFileName(program.Trim('"').Replace('\\', '/'));
        if (name.Length == 0)
        {
            return arguments;
        }

        var redacted = Command().Replace(name + " " + arguments, RedactCommand);
        return redacted[(name.Length + 1)..];
    }

    // A CSV row is read against the last header naming a name column and a value column. A row with more fields than
    // the header holds a comma in an unquoted value, so everything from the value column on is secret.
    private static string RedactCsv(Match match)
    {
        int nameColumn = -1, valueColumn = -1, columns = 0;
        return string.Concat(CsvLine().Split(match.Value).Select(line =>
        {
            var fields = CsvField().Matches(line.TrimEnd('\r')).ToList();
            var names = fields.Select(field => field.Value.Trim().Trim('"')).ToList();
            var name = names.FindIndex(field => NameFields.Contains(field, StringComparer.OrdinalIgnoreCase));
            var value = names.FindIndex(field => ValueFields.Contains(field, StringComparer.OrdinalIgnoreCase));
            if (name >= 0 && value >= 0)
            {
                (nameColumn, valueColumn, columns) = (name, value, fields.Count);
                return line;
            }

            if (columns == 0 || fields.Count < columns || !SecretName().IsMatch(names[nameColumn]) || Redacted(names[valueColumn]))
            {
                return line;
            }

            var target = fields[valueColumn];
            var end = fields.Count > columns ? fields[^1].Index + fields[^1].Length : target.Index + target.Length;
            return line[..target.Index] + (target.Value.StartsWith('"') ? "\"<secret>\"" : "<secret>") + line[end..];
        }));
    }

    // The switch table for a command, given its name or path (sqlcmd, C:\Tools\sqlcmd.exe, docker login).
    private static Switches? CommandSwitches(string command) =>
        Path.GetFileName(command.Replace('\\', '/')).ToLowerInvariant().Replace(".exe", "", StringComparison.Ordinal) switch
        {
            "schtasks" => Schtasks,
            "sqlcmd" or "bcp" or "osql" => Sql,
            "psexec" or "psexec64" => PsExec,
            "docker" => Docker,
            "certutil" => CertUtil,
            "7z" or "7za" or "7zr" => SevenZip,
            _ => null,
        };

    // net use takes its password as the first plain argument and net user as the second (after the account name).
    private static string RedactCommand(Match match)
    {
        var command = match.Groups["command"].Value;
        var switches = CommandSwitches(command.Split([' ', '\t'])[0]);
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
                return glued is null ? value : glued + Unclosed(value[glued.Length..]) + (switches.Secret.Contains(glued) ? "<secret>" : "<user>");
            }

            if (next is not null)
            {
                var replacement = Unclosed(value) + next;
                next = null;
                return replacement;
            }

            var bare = value.TrimStart('"');
            if (switches is not null || UncArgument().IsMatch(bare) || bare.StartsWith("<path>", StringComparison.Ordinal) || NetDevice().IsMatch(value))
            {
                return value;
            }

            // An argument already redacted (<user>, <secret>) keeps its place in the count.
            positional++;
            return bare.StartsWith('<') ? value : user && positional == 1 ? Unclosed(value) + "<user>" : positional == (user ? 2 : 1) ? Unclosed(value) + "<secret>" : value;
        });
    }

    // A quoted argument whose closing quote lies past the arguments (at a line or JSON string end) keeps its opening
    // quote, so the quote that follows stays paired: "Qzx secret9" -> "<secret>".
    private static string Unclosed(string argument) =>
        argument.StartsWith('"') && (argument.Length == 1 || !argument.EndsWith('"')) ? "\"" : "";

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

    private static readonly string[] QuoteSpellings = [@"\\\""", @"\\u0022", @"\u0022", "\\\"", "\""];

    // A quoted value keeps its quote spelling ({"password":"<secret>"}), so JSON stays well formed; a .NET type name
    // ("Credential":"System.Management.Automation.PSCredential") names a type, not a secret.
    private static string RedactAssignment(Match match)
    {
        var value = match.Groups["value"].Value;
        var quote = AssignmentQuotes.FirstOrDefault(spelling => value.Length >= 2 * spelling.Length && value.StartsWith(spelling, StringComparison.Ordinal) && value.EndsWith(spelling, StringComparison.Ordinal));
        if (TypeName().IsMatch(quote is null ? value : value[quote.Length..^quote.Length]))
        {
            return match.Value;
        }

        // A bare JSON value after a quoted key ("token": null) is written as a string, so the JSON still parses.
        var key = match.Groups["key"].Value;
        quote ??= QuoteSpellings.FirstOrDefault(spelling => key.EndsWith(spelling, StringComparison.Ordinal));
        return key + match.Groups["separator"].Value + quote + "<secret>" + quote;
    }

    private static readonly string[] AssignmentQuotes = [.. QuoteSpellings, "'"];

    // A quoted secret keeps the quote spelling it was written with, so JSON at any depth stays well formed.
    private static string Requote(string quoted)
    {
        var quote = QuoteSpellings.First(spelling => quoted.StartsWith(spelling, StringComparison.Ordinal));
        return quote + "<secret>" + quote;
    }

    // A value that runs to the line end stops at the close of a JSON string, which stays.
    private static string SecretToClose(string key, string value)
    {
        var close = JsonClose().Match(value);
        return key + "<secret>" + (close.Success ? value[close.Index..] : "");
    }

    // The value of a JSON string literal whose escapes are all valid (JsonString).
    private static string Unescape(string literal)
    {
        var value = new StringBuilder(literal.Length);
        for (var index = 1; index < literal.Length - 1; index++)
        {
            var character = literal[index];
            if (character != '\\')
            {
                value.Append(character);
                continue;
            }

            var escape = literal[++index];
            if (escape == 'u')
            {
                value.Append((char)Convert.ToInt32(literal.Substring(index + 1, 4), 16));
                index += 4;
                continue;
            }

            value.Append(escape switch { 'b' => '\b', 'f' => '\f', 'n' => '\n', 'r' => '\r', 't' => '\t', _ => escape });
        }

        return value.ToString();
    }

    // The shortest JSON string literal for a value: a quote and a backslash escaped, control characters as their short
    // escape or \u00XX, everything else literal.
    private static string Quote(string value)
    {
        var literal = new StringBuilder(value.Length + 2).Append('"');
        foreach (var character in value)
        {
            literal.Append(character switch
            {
                '"' => "\\\"",
                '\\' => "\\\\",
                '\b' => "\\b",
                '\f' => "\\f",
                '\n' => "\\n",
                '\r' => "\\r",
                '\t' => "\\t",
                < ' ' => $"\\u{(int)character:x4}",
                _ => character.ToString(),
            });
        }

        return literal.Append('"').ToString();
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

    // A name that holds a secret: any prefix before a secret word (DB_PASSWORD, ACTIONS_RUNNER_INPUT_TOKEN), a
    // *_KEY qualified as a secret key (AZURE_STORAGE_KEY, SIGNING_KEY; not cache_key or Public-Key), or pw, pass or pat
    // as a whole final part ($pw, db_pass, AZURE_DEVOPS_EXT_PAT; not bypass). An assembly's PublicKeyToken is public.
    private const string SecretKey = @"(?:[A-Za-z0-9_.-]*(?:password|passwd|pwd|passphrase|secret|(?<!PublicKey)token|api[_-]?key|access[_-]?key|account[_-]?key|shared[_-]?access[_-]?key|private[_-]?key|client[_-]?secret|credentials?)|[A-Za-z0-9_.-]*(?:storage|signing|encryption|decryption|master|license|licence|product|subscription|deploy|deployment|ssh|gpg|pgp|hmac|jwt|session|crypto|cipher|aes|admin|service|app|client|function|webhook|sas)[_.-]?key|(?<short>(?:[A-Za-z0-9_.-]*[_.-])?(?:pw|pass|pat)))";

    private const string AuthSchemes = "Bearer|Basic|Digest|token|Negotiate|NTLM|SharedKey|SharedKeyLite|ApiKey|SSWS|AWS4-HMAC-SHA256";

    // A quoted value at any JSON nesting: "x" (with \" escapes inside), \"x\" (JSON), \\\"x\\\" and \\u0022x\\u0022
    // (JSON inside JSON), and a raw \u0022x\u0022. Each closes only with its own spelling.
    private const string Quoted = @"(?:\\\\\\""(?:(?!\\\\\\"")[^\r\n])*\\\\\\""|\\\\u0022(?:(?!\\\\u0022)[^\r\n])*\\\\u0022|\\u0022(?:(?!\\u0022)[^\r\n])*\\u0022|\\""(?:[^""\\\r\n]|\\[^""\r\n])*\\""|""(?:[^""\\\r\n]|\\.)*"")";

    // Not at the close of a JSON string ("..."} or \"...\"}) or of a JSON object or array (null}), so a value never takes
    // the quote or bracket that closes what it sits in, and a placeholder never splits one.
    private const string NotClosing = @"(?!\\?""[ \t]*(?:[,}\]:]|\r|\n|\z)|[}\]][ \t]*(?:[,}\]]|\r|\n|\z))";

    // The rest of a line, up to the close of a JSON string.
    private const string LineRest = "(?:" + NotClosing + @"[^\r\n])*";

    // A word start, also right after a JSON \uXXXX escape left at a deeper level (\\u0022net use ...).
    private const string WordStart = @"(?:\b|" + AfterEscape + ")";

    // A plain token: a backslash always travels with the next character (\" stays whole), and it stops at the close of
    // a JSON string.
    private const string Bare = "(?:" + NotClosing + @"(?:\\[^\s]|[^\s""',;\\]))+";

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

    private const string UncHead = @"(?:(?<![A-Za-z0-9\\/]|" + DriveBehind + "[A-Za-z]" + Colon + @")|" + AfterEscape + "|" + ShortSwitch + @")(?:\\\\){1,8}";

    // A path runs on through spaces (so "D:\Acme Projects\x" never leaks its tail) and through apostrophes inside
    // names (O'Brien, Customers' Data; only a path opened by an apostrophe ends at one). It stops at a double quote,
    // <>|, a tab, a line end, a wildcard, or a colon, which no Windows name can
    // hold (file.cs:line 42, https://), before a switch (" /c"; no Windows name starts with "/" or ends with a space),
    // and before the next drive or UNC path, so a kept path never carries another one along. A colon before a
    // backslash is kept when its letter cannot start a drive (abcD:\x), so no tail leaks.
    private const string PathTail = @"(?:(?!" + DriveHead + "|" + UncHead + @"| /|(?<!\\)(?:\\\\)*\\"")(?:[^""'<>|:\r\n\t*?]|(?(open)" + QuotedApostrophe + "|')|:(?=" + BackSeparator + @")))*";

    private const string PosixTail = @"(?:[^""'<>|:\r\n\t*?\\]|\\(?!"")|(?(open)" + QuotedApostrophe + "|'))*";

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
    [GeneratedRegex(@"(?<key>" + WordStart + @"(?:Proxy-)?Authorization(?:\\{0,3}""|\\{1,2}u0022)?(?:[ \t]*[:=][ \t]*|[ \t]{2,}(?=\S)))(?:(?<quote>\\\\\\""|\\\\u0022|\\u0022|\\?"")(?<scheme>(?:" + AuthSchemes + @")[ \t]+)?(?:(?!\k<quote>)[^\r\n])+|(?<scheme>(?:" + AuthSchemes + @")[ \t]+)?" + NotClosing + @"[^""\r\n]" + LineRest + ")", RegexOptions.IgnoreCase)]
    private static partial Regex Authorization();

    // A standalone credential only when it looks like one (16+ characters with a digit), so "Basic authentication" stays.
    [GeneratedRegex(@"(?<scheme>\b(?:Bearer|Basic)\s+)(?=[A-Za-z0-9._~+/=-]*[0-9])[A-Za-z0-9._~+/=-]{16,}", RegexOptions.IgnoreCase)]
    private static partial Regex Bearer();

    // At any JSON depth (key=\"Password\" value=\"x\"); the setting name must hold a secret word, and the quoting is kept.
    [GeneratedRegex(@"(?<key>" + WordStart + @"key\s*=\s*(?<name>" + Quoted + @")\s+value\s*=\s*)(?<value>" + Quoted + ")", RegexOptions.IgnoreCase)]
    private static partial Regex XmlSecret();

    [GeneratedRegex(@"(?<command>" + WordStart + @"(?:net(?:\.exe)?[ \t]+(?:use|user)|docker(?:\.exe)?(?:[ \t]+--?[A-Za-z][\w-]{0,40}(?:[ \t=]+[^\s-]\S{0,200})?){0,4}?[ \t]+login|(?:schtasks|sqlcmd|bcp|osql|psexec(?:64)?|certutil|7z[ar]?)(?:\.exe)?)\b)(?<args>" + LineRest + ")", RegexOptions.IgnoreCase)]
    private static partial Regex Command();

    // An argument is a run of plain characters and quoted parts, so -p"two words" and "\\fs01\a b" are one argument;
    // an unclosed quote runs to the end of the line.
    [GeneratedRegex(@"(?:[^\s""]|""[^""\r\n]*(?:""|$))+")]
    private static partial Regex Argument();

    // A UNC root at any JSON depth (\\fs01, \\\\fs01), not an escaped quote (\\\"x).
    [GeneratedRegex(@"^\\\\+[A-Za-z0-9._$?-]")]
    private static partial Regex UncArgument();

    [GeneratedRegex(@"^(?:[A-Za-z]:|\*|(?:LPT|COM)[0-9]:?)$", RegexOptions.IgnoreCase)]
    private static partial Regex NetDevice();

    // ConvertTo-SecureString "x" -AsPlainText, ... -String x, and "x" | ConvertTo-SecureString. The piped form has no key
    // to anchor it, so its quoted value never spans an escaped quote and each start scans only to the next quote.
    [GeneratedRegex(@"(?<key>" + WordStart + @"ConvertTo-SecureString\b(?:[ \t]+-(?:AsPlainText|Force|String)\b)*[ \t]+)(?:" + Quoted + @"|'[^'\r\n]*'|(?!-)(?:[^\s|;)""\\]|\\(?!""))+)|(?:""[^""\r\n]*""|\\""[^""\r\n]*\\""|'[^'\r\n]*')(?=[ \t]*\|[ \t]*ConvertTo-SecureString\b)", RegexOptions.IgnoreCase)]
    private static partial Regex SecureString();

    // new NetworkCredential("jane", "x"), New-Object System.Net.NetworkCredential -ArgumentList 'jane', 'x', and
    // [NetworkCredential]::new($env:USERNAME, "x"); a literal account becomes <user>, a variable stays.
    [GeneratedRegex(@"(?<head>" + WordStart + @"NetworkCredential\]?(?:::new)?(?:[ \t]*\(|[ \t]+-ArgumentList)[ \t]*)(?<user>" + Quoted + @"|'[^'\r\n]*'|\$[\w:.]+)(?<separator>[ \t]*,[ \t]*)(?!" + Placeholder + ")(?:" + Quoted + @"|'[^'\r\n]*')")]
    private static partial Regex Credential();

    // Command-line flags: "--token x", "-pw x", "--password=x", "-passout pass:x", and Windows switches "/pass:x".
    [GeneratedRegex(@"(?<key>(?:(?<![\w/-])|" + AfterEscape + @")(?:--?[A-Za-z0-9_-]*(?:token|password|passwd|pwd|passphrase|secret|pat|api-?key|pw|pass(?:in|out)?)\b(?:\s+|[=:])|/[A-Za-z0-9_-]*(?:token|password|passwd|pwd|secret|pass)\b:))(?!-|" + Placeholder + @")(?:" + Quoted + @"|'[^'\r\n]*'|" + Bare + ")", RegexOptions.IgnoreCase)]
    private static partial Regex FlagSecret();

    // Name with any prefix (ACTIONS_RUNNER_INPUT_TOKEN, DB_PASSWORD, "api_key", \"password\"), then ":"/"=" or a
    // table gap of 2+ spaces. A value after a ";" (a connection-string item) runs to the next ";", the line end or a
    // quote, so a multi-word password never leaks a word; elsewhere a ";" ends it. Both are capped at 256 characters.
    // A value that starts with a PowerShell credential cmdlet ($pw = Read-Host ...) is code, not a secret, and a number
    // after the short keys pw and pass is a count (Pass: 9). A reg query type column (REG_SZ) is never the value.
    [GeneratedRegex(@"(?<ctx>(?<=;[ \t]{0,8}))?(?<key>(?:(?<![A-Za-z0-9_.-])|" + AfterEscape + ")" + SecretKey + @"(?:\\{0,3}""|\\{1,2}u0022)?)(?<separator>\s*[:=]\s*|[ \t]{2,}(?=\S)(?![:=]|REG_[A-Z0-9_]+[ \t]))(?!" + Placeholder + @"|[{\[])(?(short)(?![0-9]+(?![\w.])))(?<value>" + Quoted + @"|'[^'\r\n]*'|(?!(?:Get|Read|ConvertTo|ConvertFrom|New|Import|Unprotect)-[A-Z])(?(ctx)(?:" + NotClosing + @"(?:\\[^\r\n]|[^;\r\n\\])){1,256}|(?:(?:" + NotClosing + @"(?:\\[^\r\n]|[^;\r\n\\])){1,256}?(?=;)|(?:" + NotClosing + @"(?:\\[^\s]|[^\s,;\\]))+)))", RegexOptions.IgnoreCase)]
    private static partial Regex Assignment();

    // reg query rows (name, type, data) and netsh "Key Content : x": the data runs to the end of the line. Numeric types
    // (REG_DWORD, REG_QWORD) are flags such as DisablePasswordChange and stay.
    [GeneratedRegex(@"(?<key>\b(?:[\w.-]*(?:password|passwd|pwd|passphrase|secret|token|credential|apikey|api_key|accesskey|accountkey|productkey|privatekey)[\w.-]*|[\w.-]*(?:pass|pw))[ \t]{2,}REG_(?:SZ|EXPAND_SZ|MULTI_SZ|BINARY|NONE)[ \t]+|\bKey Content[ \t]*:[ \t]*)(?<value>[^\s][^\r\n]*)", RegexOptions.IgnoreCase | RegexOptions.NonBacktracking)]
    private static partial Regex Registry();

    // At the start of a line (after an optional log timestamp, as in GitHub Actions logs), an unquoted secret key
    // (GPG_PASSPHRASE=correct horse, export X=..., YAML "password: a b", an env table row "X    a b") takes the rest of
    // the line, quotes and brackets included, so a value with spaces or an unclosed quote never leaks a word.
    [GeneratedRegex(@"(?<key>^[ \t]*(?:[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9:.]+Z?[ \t]+)?(?:(?:export|set|ENV)[ \t]+|-[ \t]+)?" + SecretKey + @")(?<separator>[ \t]*[=:][ \t]*|[ \t]{2,}(?![=:]|REG_))(?!" + Placeholder + @")(?(short)(?![0-9]+(?![\w.])))(?!(?:Get|Read|ConvertTo|ConvertFrom|New|Import|Unprotect)-[A-Z])(?<value>[^\s][^\r\n]*)", RegexOptions.Multiline | RegexOptions.IgnoreCase)]
    private static partial Regex LineAssignment();

    // A secret key that opens a quoted string ("GPG_PASSPHRASE=a b c" in docker inspect Env, docker run -e "X=a b",
    // \"X=a b\" in escaped JSON, 'X=a b') takes the value to the closing quote of the same spelling.
    [GeneratedRegex(@"(?<key>(?<![\w\\""])""" + SecretKey + @"[ \t]*=[ \t]*)(?!" + Placeholder + @")(?>(?:[^""\\\r\n]|\\[^\r\n])+)(?="")|(?<key>(?<!\\)\\""" + SecretKey + @"[ \t]*=[ \t]*)(?!" + Placeholder + @")(?>(?:[^""\\\r\n]|\\(?!"")[^\r\n])+)(?=\\"")|(?<key>(?<![\w'])'" + SecretKey + @"[ \t]*=[ \t]*)(?!" + Placeholder + @")(?>[^'\r\n]+)(?=')", RegexOptions.IgnoreCase)]
    private static partial Regex QuotedAssignment();

    [GeneratedRegex(@"(?<=[?&;]|\\u00(?:26|3[Ff]|3[Bb]))sig=(?:[^&\s""'<>;\\]|\\(?!""))+", RegexOptions.IgnoreCase)]
    private static partial Regex SasSignature();

    // Account names given as switches or connection-string keys: /user:CORP\jane, --username jane, User Id=sa.
    [GeneratedRegex(@"(?<key>(?:(?<![\w/-])|" + AfterEscape + @")(?:/u(?:ser)?:|--user(?:name)?=|--username[ \t]+)|" + WordStart + @"User[ \t_]?(?:Id|Name)[ \t]*=[ \t]*)(?!" + Placeholder + @")(?:" + Quoted + @"|(?:" + NotClosing + @"(?:\\[^\s]|[^\s;,""\\]))+)", RegexOptions.IgnoreCase)]
    private static partial Regex UserFlag();

    // Plain and JSON-escaped (https:\/\/) URLs.
    [GeneratedRegex(@"\b(?<scheme>[A-Za-z][A-Za-z0-9+.-]*)(?<separator>://|:\\/\\/)(?:[^\s/\\@""'<>]+@)?(?<host>[^\s/\\:""'<>?#]+)(?<port>:[0-9]+)?(?<rest>(?:[^\s""'<>\\]|\\[^\s""'<>])*)", RegexOptions.NonBacktracking)]
    private static partial Regex Url();

    // Any user@host:path remote, including SSH host aliases with no TLD (git@github.com-customer:org/repo.git), and a
    // user-less host:path remote when it names a repository (build01:acme/repo.git).
    [GeneratedRegex(@"\b[A-Za-z0-9._-]+@[A-Za-z0-9._-]+:(?:[^\s""'<>\\]|\\[^\s""'<>])+|\b[A-Za-z0-9][A-Za-z0-9._-]+:[A-Za-z0-9_~.-][^\s""'<>\\]*\.git\b", RegexOptions.NonBacktracking)]
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

    [GeneratedRegex(Open + @"(?<root>(?:(?<![\w.~-])|" + AfterEscape + "|" + ShortSwitch + @")/(?:home|Users)/)(?!<)(?:[^/""'<>|:\r\n\t*?\\]|\\(?!"")|(?(open)" + QuotedApostrophe + "|'))+(?<rest>/" + PosixTail + ")?", RegexOptions.IgnoreCase)]
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
    [GeneratedRegex(@"(?:(?<![\w.@/-])|" + AfterEscape + @")(?<!(?:System32|SysWOW64)[\\/]{1,4})(?!(?-i:System|Microsoft|ASP|Windows|Newtonsoft|NuGet)\.)(?<host>(?:" + Label + ")+(?:" + SafeDomains + ")|(?:" + Label + "){2,}(?:" + RiskyDomains + @")|(?<=\b(?:Suffix|Domain|ComputerName|Resolve-DnsName|nslookup|ping|tracert|Test-NetConnection|Test-Connection)[ \t.:=]{1,40})(?:" + Label + ")(?:" + RiskyDomains + @"))(?<rest>/(?:[^\s""'<>\\]|\\[^\s""'<>])*)?(?![\w-]|\.[\w-])", RegexOptions.IgnoreCase)]
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

    [GeneratedRegex("password|passwd|pwd|secret|token|key", RegexOptions.IgnoreCase)]
    private static partial Regex SecretWord();

    [GeneratedRegex(@"\\?""[ \t]*(?:[,}\]:]|\r|\n|\z)", RegexOptions.NonBacktracking)]
    private static partial Regex JsonClose();

    [GeneratedRegex("\"\uFDD0(?<index>[\uFDD2-\uFDDB]+)\uFDD1\"")]
    private static partial Regex Mask();

    // A standalone drive colon, or a UNC server name at the start of a path, followed by a single backslash escape
    // (\r, \f, \n, \t, ...): a raw path. "Issuer:\n" and an encoded "...\\LabVIEW.exe\n" are not.
    [GeneratedRegex(@"(?<![A-Za-z0-9])[A-Za-z]:\\(?![\\""])|(?<![\\\w:])\\\\[A-Za-z0-9._$-]+\\(?![\\""])")]
    private static partial Regex RawPath();

    [GeneratedRegex(@"password|passwd|pwd|passphrase|secret|(?<!publickey)token|api[_-]?key|access[_-]?key|account[_-]?key|private[_-]?key|client[_-]?secret|credential|(?:^|[_.-])(?:pw|pass|pat)$|(?:storage|signing|encryption|decryption|master|license|licence|product|subscription|deploy|deployment|ssh|gpg|pgp|hmac|jwt|session|crypto|cipher|aes|admin|service|app|client|function|webhook|sas)[_.-]?key(?:$|[_.-])", RegexOptions.IgnoreCase)]
    private static partial Regex SecretName();

    [GeneratedRegex(@"^(?:--?[A-Za-z0-9_-]*(?:token|password|passwd|pwd|passphrase|secret|pat|api-?key|pw|pass(?:in|out)?)|/[A-Za-z0-9_-]*(?:token|password|passwd|pwd|secret|pass))$", RegexOptions.IgnoreCase)]
    private static partial Regex SecretFlag();

    [GeneratedRegex(@"^(?:--user(?:name)?|/u(?:ser)?)$", RegexOptions.IgnoreCase)]
    private static partial Regex UserSwitch();

    [GeneratedRegex(@"^(?:System|Microsoft)(?:\.[A-Za-z_][\w`]*)+(?:\[[^\]]*\])?$")]
    private static partial Regex TypeName();

    [GeneratedRegex(@"^\s*(?:true|false)\s*$", RegexOptions.IgnoreCase)]
    private static partial Regex Flag();

    // An element with text content: the element name or a Name/N/Key attribute names the secret.
    [GeneratedRegex(@"(?<head><(?<tag>[A-Za-z_][\w.:-]{0,100})(?:[ \t\r\n][^<>]*?\b(?:Name|N|Key)[ \t]*=[ \t]*[""'](?<named>[^""'<>]*)[""'][^<>]*|[ \t\r\n][^<>]*)?>)(?<value>(?=\s*[^\s<])(?>[^<]+))(?<close></\k<tag>>)", RegexOptions.IgnoreCase)]
    private static partial Regex XmlElement();

    // An unattend.xml secret element holding a <Value> child: <AdministratorPassword><Value>x</Value>.
    [GeneratedRegex(@"(?<head><(?<tag>[A-Za-z_][\w.:-]{0,100})(?:[ \t\r\n][^<>]*)?>\s*<Value(?:[ \t\r\n][^<>]*)?>)(?=\s*[^\s<])(?>[^<]+)(?=</Value>)", RegexOptions.IgnoreCase)]
    private static partial Regex XmlValueChild();

    // ConvertTo-Xml and CLIXML Name/Value siblings: <S N="Name">DB_PASSWORD</S> ... <S N="Value">x</S>.
    [GeneratedRegex(@"(?<head><(?<t1>[A-Za-z][\w.:-]{0,100})[ \t\r\n][^<>]*?\b(?:Name|N)[ \t]*=[ \t]*[""'](?:Name|Key)[""'][^<>]*>(?<name>[^<]*)</\k<t1>>(?:\s*<[^<>/]*>[^<]*</[^<>]*>){0,8}?\s*<(?<t2>[A-Za-z][\w.:-]{0,100})[ \t\r\n][^<>]*?\b(?:Name|N)[ \t]*=[ \t]*[""'](?:Value|VariableValue|Data)[""'][^<>]*>)(?=\s*[^\s<])(?>[^<]+)(?=</\k<t2>>)", RegexOptions.IgnoreCase)]
    private static partial Regex XmlNameValue();

    [GeneratedRegex(@"@\{[^{}\r\n]*\}", RegexOptions.NonBacktracking)]
    private static partial Regex PsObject();

    [GeneratedRegex(@"(?<=@\{|;)(?<key>[^=;{}]+)=(?<value>[^;}]*)")]
    private static partial Regex PsMember();

    [GeneratedRegex(@"(?<head>^[ \t]*FileName[ \t]*:[ \t]*(?<program>[^\r\n]*)\r*\n(?:[ \t]*[A-Za-z]\w*[ \t]*:[^\r\n]*\r*\n){0,16}?[ \t]*Arguments[ \t]*:[ \t]*)(?<args>[^\r\n]*)", RegexOptions.Multiline | RegexOptions.IgnoreCase)]
    private static partial Regex ProcessRecord();

    [GeneratedRegex(@"(?<head>^[ \t]*Arguments[ \t]*:[ \t]*)(?<args>[^\r\n]*)(?<tail>\r*\n(?:[ \t]*[A-Za-z]\w*[ \t]*:[^\r\n]*\r*\n){0,16}?[ \t]*FileName[ \t]*:[ \t]*(?<program>[^\r\n]*))", RegexOptions.Multiline | RegexOptions.IgnoreCase)]
    private static partial Regex ProcessRecordArgumentsFirst();

    // Format-List and YAML records: "Name : DB_PASSWORD" (or "- name: x"), then within a few lines "Value : x", or the
    // value line first.
    [GeneratedRegex(@"(?<head>^[ \t]*(?:-[ \t]+)?(?:Name|Key)[ \t]*:[ \t]*(?<name>[^\r\n]*)\r*\n(?:[ \t]*[A-Za-z]\w*[ \t]*:[^\r\n]*\r*\n){0,8}?[ \t]*(?:Value|VariableValue|Data)[ \t]*:[ \t]*)(?<value>[^\r\n]*)", RegexOptions.Multiline | RegexOptions.IgnoreCase)]
    private static partial Regex Record();

    [GeneratedRegex(@"(?<head>^[ \t]*(?:-[ \t]+)?(?:Value|VariableValue|Data)[ \t]*:[ \t]*)(?<value>[^\r\n]*)(?<tail>\r*\n(?:[ \t]*[A-Za-z]\w*[ \t]*:[^\r\n]*\r*\n){0,8}?[ \t]*(?:Name|Key)[ \t]*:[ \t]*(?<name>[^\r\n]*))", RegexOptions.Multiline | RegexOptions.IgnoreCase)]
    private static partial Regex RecordValueFirst();

    // Consecutive lines that hold commas; RedactCsv finds the header among them.
    [GeneratedRegex(@"^[^\r\n]*,[^\r\n]*(?:\r*\n[^\r\n]*,[^\r\n]*)+", RegexOptions.Multiline | RegexOptions.NonBacktracking)]
    private static partial Regex CsvBlock();

    [GeneratedRegex(@"(\r*\n)")]
    private static partial Regex CsvLine();

    [GeneratedRegex(@"(?<=^|,)(?:""(?:[^""]|"""")*""|[^,]*)")]
    private static partial Regex CsvField();

    [GeneratedRegex("(" + Placeholder + ")")]
    private static partial Regex PlaceholderSplit();

    [GeneratedRegex(@"(?:\\|/|%2[Ff]|%5[Cc])+")]
    private static partial Regex Separator();

    [GeneratedRegex(@"^[A-Za-z]:\\")]
    private static partial Regex DriveRoot();
}
