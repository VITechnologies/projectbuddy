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
    public const string InternalMessage = "projectbuddy-ask: internal error";
    public const string VmPrefix = "projectbuddy-";

    // At most three tool calls per verb, so a verb finishes well inside the client's 30 s allowance.
    public static readonly TimeSpan ToolTimeout = TimeSpan.FromSeconds(5);
    public static readonly IReadOnlyList<string> Verbs = ["identity", "vbox", "disk", "ollama", "lbabus"];

    public static int Execute(string? command, IProcessRunner runner, IHostFacts facts, TextWriter stdout, TextWriter stderr)
    {
        if (command is null || !Verbs.Contains(command))
        {
            stderr.WriteLine(RefusedMessage);
            return ExitCodes.Refused;
        }

        byte[] json;
        try
        {
            json = Render(command, runner, facts);
        }
        catch (Exception)
        {
            // The client sees stderr, so never let a stack trace or exception message reach it.
            stderr.WriteLine(InternalMessage);
            return ExitCodes.Internal;
        }

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
                    WriteOllama(writer, runner, facts.OllamaPath);
                    break;
                default:
                    WriteTool(writer, runner, facts.LbabusPath);
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

    private static bool Succeeded(ProcessResult result) => result is { Started: true, TimedOut: false, ExitCode: 0 };

    private static void WriteTool(Utf8JsonWriter writer, IProcessRunner runner, string tool)
    {
        var result = runner.Run(tool, ["--version"], ToolTimeout);
        writer.WriteBoolean("installed", Succeeded(result));
        if (Succeeded(result))
        {
            writer.WriteString("version", Field(result.Stdout));
        }
    }

    private static void WriteOllama(Utf8JsonWriter writer, IProcessRunner runner, string ollama)
    {
        var version = runner.Run(ollama, ["--version"], ToolTimeout);
        writer.WriteBoolean("installed", Succeeded(version));
        if (!Succeeded(version))
        {
            return;
        }

        writer.WriteString("version", Field(version.Stdout));
        var ps = runner.Run(ollama, ["ps"], ToolTimeout);
        writer.WriteStartArray("loadedModels");
        if (Succeeded(ps))
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
        writer.WriteBoolean("installed", Succeeded(version));
        if (!Succeeded(version))
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

        var runningIds = running.Select(vm => vm.Id).ToHashSet(StringComparer.OrdinalIgnoreCase);
        var owned = all.Where(vm => vm.Name is not null && vm.Name.StartsWith(VmPrefix, StringComparison.Ordinal)).ToList();
        writer.WriteStartArray("vms");
        foreach (var vm in owned)
        {
            writer.WriteStartObject();
            writer.WriteString("name", Field(vm.Name!));
            writer.WriteBoolean("running", runningIds.Contains(vm.Id!));
            writer.WriteEndObject();
        }

        writer.WriteEndArray();
        writer.WriteNumber("otherVmCount", all.Count - owned.Count);
    }

    // Every non-empty line is a VM; a line that does not parse still counts, without a name.
    private static List<(string? Name, string? Id)>? ListVms(IProcessRunner runner, string vboxManage, string what)
    {
        var result = runner.Run(vboxManage, ["list", what], ToolTimeout);
        if (!Succeeded(result))
        {
            return null;
        }

        return result.Stdout.Split('\n')
            .Select(line => line.TrimEnd('\r'))
            .Where(line => line.Trim().Length > 0)
            .Select(line => VmLine().Match(line) is { Success: true } match
                ? (match.Groups[1].Value, match.Groups[2].Value)
                : ((string?)null, (string?)null))
            .ToList();
    }

    [GeneratedRegex("^\"(.*)\" \\{([0-9a-fA-F-]+)\\}$")]
    private static partial Regex VmLine();
}
