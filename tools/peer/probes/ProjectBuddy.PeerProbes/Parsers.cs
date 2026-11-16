using System.Text;
using System.Text.RegularExpressions;
using System.Xml;

namespace ProjectBuddy.PeerProbes;

public sealed record VirtualMachine(string Name, string Uuid);

public sealed record ServiceState(string Name, string State);

public sealed record TaskState(string State, string LastResult);

public sealed record EventRecord(string Provider, int Id, int Level);

// Pure readers of the fixed commands' output. They keep only the fields the probes report and never the rest of a
// line, so a value outside the allow-list cannot leak through a parser.
public static partial class Parsers
{
    // VBoxManage list vms / list runningvms: "name" {uuid}.
    public static IReadOnlyList<VirtualMachine> VirtualMachines(string output) =>
        [.. VmLine().Matches(output).Select(match => new VirtualMachine(match.Groups["name"].Value, match.Groups["uuid"].Value.ToLowerInvariant()))];

    // VBoxManage showvminfo --machinereadable: key=value per line, either side possibly quoted with \" and \\ escapes.
    // Only the keys asked for are returned; every other line (including vrdeproperty[...]) is skipped unread.
    public static IReadOnlyDictionary<string, string> MachineReadable(string output, IReadOnlySet<string> keys)
    {
        var values = new Dictionary<string, string>(StringComparer.Ordinal);
        foreach (Match match in MachineLine().Matches(output))
        {
            var key = Unquote(match.Groups["key"].Value);
            if (keys.Contains(key))
            {
                values.TryAdd(key, Unquote(match.Groups["value"].Value.TrimEnd('\r')));
            }
        }

        return values;
    }

    // sc.exe query: SERVICE_NAME: x, then STATE : 4  RUNNING.
    public static IReadOnlyList<ServiceState> Services(string output)
    {
        var services = new List<ServiceState>();
        string? name = null;
        foreach (var line in output.Split('\n'))
        {
            var service = ServiceName().Match(line);
            if (service.Success)
            {
                name = service.Groups["name"].Value.Trim();
                continue;
            }

            var field = ScField().Match(line);
            if (name is not null && field.Success && field.Groups["field"].Value == "STATE")
            {
                services.Add(new ServiceState(name, field.Groups["word"].Value));
                name = null;
            }
        }

        return services;
    }

    // sc.exe qc: START_TYPE : 2   AUTO_START  (DELAYED).
    public static string StartType(string output) =>
        output.Split('\n').Select(line => ScField().Match(line)).FirstOrDefault(field => field.Success && field.Groups["field"].Value == "START_TYPE") is { } start
            ? start.Groups["word"].Value + (start.Groups["rest"].Value.Contains("DELAYED", StringComparison.Ordinal) ? "_DELAYED" : "")
            : "unknown";

    // schtasks /query /fo csv /v: a header row and one row per trigger; only Status and Last Result are read.
    public static TaskState Task(string output)
    {
        var rows = output.Split('\n').Select(line => line.TrimEnd('\r')).Where(line => line.Length > 0).Select(CsvFields).ToList();
        if (rows.Count < 2)
        {
            return new TaskState("unknown", "unknown");
        }

        string Column(string header) => rows[0].IndexOf(header) is var index and >= 0 && index < rows[1].Count ? rows[1][index] : "unknown";
        return new TaskState(Column("Status"), Column("Last Result"));
    }

    // wevtutil qe /f:xml: <Event> fragments. Only Provider@Name, EventID and Level directly under each event's own
    // <System> header (depth 1) are read; event data and messages are never touched. Malformed or cut XML stops the
    // read, and the events read so far are kept.
    public static (IReadOnlyList<EventRecord> Events, bool Complete) Events(string output)
    {
        var events = new List<EventRecord>();
        var settings = new XmlReaderSettings
        {
            ConformanceLevel = ConformanceLevel.Fragment,
            DtdProcessing = DtdProcessing.Prohibit,
            XmlResolver = null,
            IgnoreComments = true,

            // Event data may hold characters XML does not allow; it is never read, so they must not stop the count.
            // Raw ones become spaces before parsing, and character references are not checked.
            CheckCharacters = false,
        };
        var (inSystem, provider, id, level) = (false, "", -1, -1);
        try
        {
            using var reader = XmlReader.Create(new StringReader(XmlInvalid().Replace(output, " ")), settings);

            // Reading an element's content already moves past its end tag, so the next node is used without Read().
            var positioned = false;
            while (positioned || reader.Read())
            {
                positioned = false;
                if (reader.NodeType == XmlNodeType.Element && reader.Depth == 1 && reader.LocalName == "System" && !reader.IsEmptyElement)
                {
                    (inSystem, provider, id, level) = (true, "", -1, -1);
                }
                else if (inSystem && reader.NodeType == XmlNodeType.Element && reader.Depth == 2 && reader.LocalName == "Provider")
                {
                    provider = reader.GetAttribute("Name") ?? "";
                }
                else if (inSystem && reader.NodeType == XmlNodeType.Element && reader.Depth == 2 && reader.LocalName is "EventID" or "Level")
                {
                    var isId = reader.LocalName == "EventID";
                    var value = int.TryParse(reader.ReadElementContentAsString(), out var number) ? number : -1;
                    (id, level) = isId ? (value, level) : (id, value);
                    positioned = true;
                }
                else if (inSystem && reader.NodeType == XmlNodeType.EndElement && reader.Depth == 1)
                {
                    events.Add(new EventRecord(provider, id, level));
                    inSystem = false;
                }
            }
        }
        catch (XmlException)
        {
            return (events, false);
        }

        return (events, true);
    }

    private static string Unquote(string text)
    {
        if (text.Length < 2 || text[0] != '"' || text[^1] != '"')
        {
            return text;
        }

        var value = new StringBuilder(text.Length);
        for (var index = 1; index < text.Length - 1; index++)
        {
            if (text[index] == '\\' && index + 1 < text.Length - 1)
            {
                index++;
            }

            value.Append(text[index]);
        }

        return value.ToString();
    }

    // A CSV row: "a","b ""c""",d.
    private static List<string> CsvFields(string line) =>
        [.. CsvField().Matches(line).Select(field => field.Value is ['"', .., '"'] ? field.Value[1..^1].Replace("\"\"", "\"", StringComparison.Ordinal) : field.Value)];

    [GeneratedRegex(@"^""(?<name>.*)"" \{(?<uuid>[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})\}\r?$", RegexOptions.Multiline)]
    private static partial Regex VmLine();

    [GeneratedRegex(@"^(?<key>""(?:[^""\\\r\n]|\\.)*""|[^=""\r\n]+)=(?<value>[^\n]*)$", RegexOptions.Multiline)]
    private static partial Regex MachineLine();

    [GeneratedRegex(@"^SERVICE_NAME:\s*(?<name>.+?)\s*$")]
    private static partial Regex ServiceName();

    [GeneratedRegex(@"^\s*(?<field>[A-Z_]+)\s*:\s*[0-9]+\s+(?<word>[A-Z_]+)(?<rest>.*)$")]
    private static partial Regex ScField();

    [GeneratedRegex(@"(?<=^|,)(?:""(?:[^""]|"""")*""|[^,]*)")]
    private static partial Regex CsvField();

    // Characters XML 1.0 does not allow raw: C0 controls other than tab, LF and CR; U+FFFE and U+FFFF; lone surrogates.
    [GeneratedRegex(@"[\x00-\x08\x0B\x0C\x0E-\x1F\uFFFE\uFFFF]|[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]")]
    private static partial Regex XmlInvalid();
}
