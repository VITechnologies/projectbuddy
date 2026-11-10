using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace ProjectBuddy.PeerAsk;

// The forced command on the peer: answers one allow-listed, read-only verb as bounded JSON.
public static partial class Serve
{
    public const int MaxOutputBytes = 16 * 1024;
    public const int MaxFieldChars = 200;
    public const string RefusedMessage = "projectbuddy-ask: request refused";
    public const string OutputLimitMessage = "projectbuddy-ask: output limit exceeded";
    public const string VmPrefix = "projectbuddy-";
    public static readonly TimeSpan ToolTimeout = TimeSpan.FromSeconds(10);
    public static readonly IReadOnlyList<string> Verbs = ["identity", "vbox", "disk", "ollama", "lbabus"];

    public static int Execute(string? command, IProcessRunner runner, IHostFacts facts, TextWriter stdout, TextWriter stderr)
    {
        if (command is null || !Verbs.Contains(command))
        {
            stderr.WriteLine(RefusedMessage);
            return ExitCodes.Refused;
        }

        var json = Render(command, runner, facts);
        if (json.Length > MaxOutputBytes)
        {
            stderr.WriteLine(OutputLimitMessage);
            return ExitCodes.OutputLimit;
        }

        stdout.WriteLine(Encoding.UTF8.GetString(json));
        return 0;
    }

    public static byte[] Render(string verb, IProcessRunner runner, IHostFacts facts)
    {
        using var buffer = new MemoryStream();
        using (var writer = new Utf8JsonWriter(buffer))
        {
            writer.WriteStartObject();
            writer.WriteString("schema", Versions.Schema);
            writer.WriteString("tool", Versions.Tool);
            writer.WriteString("verb", verb);
            writer.WriteStartObject("data");
            switch (verb)
            {
                case "identity":
                    writer.WriteString("platform", facts.Platform);
                    writer.WriteNumber("osBuild", facts.OsBuild);
                    writer.WriteNumber("uptimeSeconds", facts.UptimeSeconds);
                    break;
                case "vbox":
                    WriteVbox(writer, runner, facts.VBoxManagePath);
                    break;
                case "disk":
                    WriteDisk(writer, facts);
                    break;
                case "ollama":
                    WriteOllama(writer, runner);
                    break;
                default:
                    WriteTool(writer, runner, "lbabus");
                    break;
            }

            writer.WriteEndObject();
            writer.WriteEndObject();
        }

        return buffer.ToArray();
    }

    internal static string Field(string text)
    {
        var line = text.Split('\n', 2)[0].Trim();
        return line.Length <= MaxFieldChars ? line : line[..MaxFieldChars];
    }

    private static void WriteTool(Utf8JsonWriter writer, IProcessRunner runner, string tool)
    {
        var result = runner.Run(tool, ["--version"], ToolTimeout);
        var installed = result is { Started: true, TimedOut: false, ExitCode: 0 };
        writer.WriteBoolean("installed", installed);
        if (installed)
        {
            writer.WriteString("version", Field(result.Stdout));
        }
    }

    private static void WriteOllama(Utf8JsonWriter writer, IProcessRunner runner)
    {
        var version = runner.Run("ollama", ["--version"], ToolTimeout);
        var installed = version is { Started: true, TimedOut: false, ExitCode: 0 };
        writer.WriteBoolean("installed", installed);
        if (!installed)
        {
            return;
        }

        writer.WriteString("version", Field(version.Stdout));
        var ps = runner.Run("ollama", ["ps"], ToolTimeout);
        writer.WriteStartArray("loadedModels");
        if (ps is { Started: true, TimedOut: false, ExitCode: 0 })
        {
            foreach (var line in ps.Stdout.Split('\n').Skip(1))
            {
                var name = line.Split(' ', 2, StringSplitOptions.RemoveEmptyEntries).FirstOrDefault();
                if (name is not null)
                {
                    writer.WriteStringValue(Field(name));
                }
            }
        }

        writer.WriteEndArray();
    }

    private static void WriteDisk(Utf8JsonWriter writer, IHostFacts facts)
    {
        writer.WriteStartArray("volumes");
        foreach (var volume in facts.FixedVolumes())
        {
            writer.WriteStartObject();
            writer.WriteString("name", Field(volume.Name));
            writer.WriteNumber("freeBytes", volume.FreeBytes);
            writer.WriteNumber("totalBytes", volume.TotalBytes);
            writer.WriteEndObject();
        }

        writer.WriteEndArray();
    }

    private static void WriteVbox(Utf8JsonWriter writer, IProcessRunner runner, string vboxManage)
    {
        var version = runner.Run(vboxManage, ["--version"], ToolTimeout);
        var installed = version is { Started: true, TimedOut: false, ExitCode: 0 };
        writer.WriteBoolean("installed", installed);
        if (!installed)
        {
            return;
        }

        writer.WriteString("version", Field(version.Stdout));
        var all = ListVms(runner, vboxManage, "vms");
        var running = ListVms(runner, vboxManage, "runningvms");
        if (all is null || running is null)
        {
            writer.WriteString("error", "list-failed");
            return;
        }

        var owned = all.Where(name => name.StartsWith(VmPrefix, StringComparison.Ordinal)).ToList();
        writer.WriteStartArray("vms");
        foreach (var name in owned)
        {
            writer.WriteStartObject();
            writer.WriteString("name", Field(name));
            writer.WriteBoolean("running", running.Contains(name));
            writer.WriteEndObject();
        }

        writer.WriteEndArray();
        writer.WriteNumber("otherVmCount", all.Count - owned.Count);
    }

    private static List<string>? ListVms(IProcessRunner runner, string vboxManage, string what)
    {
        var result = runner.Run(vboxManage, ["list", what], ToolTimeout);
        if (result is not { Started: true, TimedOut: false, ExitCode: 0 })
        {
            return null;
        }

        return VmLine().Matches(result.Stdout).Select(match => match.Groups[1].Value).ToList();
    }

    [GeneratedRegex("^\"(.*)\" \\{[0-9a-fA-F-]+\\}\\r?$", RegexOptions.Multiline)]
    private static partial Regex VmLine();
}
