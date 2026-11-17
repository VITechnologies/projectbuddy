using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace ProjectBuddy.PeerAsk;

// The forced command on the peer: answers one allow-listed, read-only verb, one diagnose question, or one infer batch,
// as bounded JSON.
public static partial class Serve
{
    // Every verb but infer, which has its own limit (Infer.MaxOutputBytes).
    public const int MaxOutputBytes = 16 * 1024;
    public const int MaxFieldChars = 200;
    public const string RefusedMessage = "projectbuddy-ask: request refused";
    public const string OutputLimitMessage = "projectbuddy-ask: output limit exceeded";
    public const string InternalMessage = "projectbuddy-ask: internal error";
    public const string VmPrefix = "projectbuddy-";

    // At most three tool calls per verb, so a verb finishes well inside the client's 30 s allowance.
    public static readonly TimeSpan ToolTimeout = TimeSpan.FromSeconds(5);
    public static readonly IReadOnlyList<string> Verbs = ["identity", "vbox", "disk", "ollama", "lbabus"];

    // "diagnose <question-id>" (#298): the peer's diagnostics agent answers one question from its pinned policy's closed
    // list, which projectbuddy-diagnose itself checks. A model answer takes seconds, and its first load longer.
    public const string DiagnoseVerb = "diagnose";
    public const string DiagnosisSchema = "projectbuddy/peer-diagnosis@1";
    public const int MaxReasonChars = 400;
    public static readonly TimeSpan DiagnoseTimeout = TimeSpan.FromSeconds(180);

    // Only infer reads stdin: its request. The Ollama handler is used only by infer.
    public static int Execute(string? command, IProcessRunner runner, IHostFacts facts, Stream stdin, HttpMessageHandler ollama, TextWriter stdout, TextWriter stderr) =>
        Execute(command, runner, facts, stdin, ollama, stdout, stderr, InferLimits.Default);

    internal static int Execute(string? command, IProcessRunner runner, IHostFacts facts, Stream stdin, HttpMessageHandler ollama, TextWriter stdout, TextWriter stderr, InferLimits limits)
    {
        var question = Question(command);
        var infer = command == Infer.Verb;
        if (command is null || (question is null && !infer && !Verbs.Contains(command)))
        {
            stderr.WriteLine(RefusedMessage);
            return ExitCodes.Refused;
        }

        byte[] json;
        try
        {
            json = infer ? Infer.Render(stdin, ollama, facts, limits)
                : question is null ? Render(command, runner, facts) : RenderDiagnosis(question, runner, facts);
        }
        catch (RefusedException)
        {
            stderr.WriteLine(RefusedMessage);
            return ExitCodes.Refused;
        }
        catch (AnswerLimitException)
        {
            stderr.WriteLine(OutputLimitMessage);
            return ExitCodes.OutputLimit;
        }
        catch (Exception)
        {
            // The client sees stderr, so never let a stack trace or exception message reach it.
            stderr.WriteLine(InternalMessage);
            return ExitCodes.Internal;
        }

        if (json.Length > (infer ? Infer.MaxOutputBytes : MaxOutputBytes))
        {
            stderr.WriteLine(OutputLimitMessage);
            return ExitCodes.OutputLimit;
        }

        stdout.WriteLine(Encoding.UTF8.GetString(json));
        return 0;
    }

    public static byte[] Render(string verb, IProcessRunner runner, IHostFacts facts) => Envelope(verb, writer =>
    {
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
    });

    // The question named by "diagnose <question-id>", or null for any other command.
    public static string? Question(string? command) =>
        command is not null && DiagnoseCommand().Match(command) is { Success: true } match ? match.Groups[1].Value : null;

    // Runs the deployed projectbuddy-diagnose with its pinned policy and the deny list: no shell, fixed paths. Its report
    // is already redacted and grounded; "no advice" (exit 6) is a normal answer with its reason, and an unknown question
    // or an unusable deny list (exit 3) is refused.
    public static byte[] RenderDiagnosis(string question, IProcessRunner runner, IHostFacts facts)
    {
        var result = runner.Run(facts.DiagnosePath, ["--policy", facts.DiagnosePolicyPath, "--question", question, "--deny-list", facts.DenyListPath], DiagnoseTimeout);
        if (result is { Started: true, TimedOut: false, ExitCode: 3 })
        {
            throw new RefusedException("projectbuddy-diagnose refused the question");
        }

        if (result is not { Started: true, TimedOut: false, ExitCode: 0 or 6 })
        {
            throw new InvalidOperationException("projectbuddy-diagnose did not answer");
        }

        using var report = result.ExitCode == 0 ? JsonDocument.Parse(result.Stdout) : null;
        return Envelope(DiagnoseVerb, writer =>
        {
            writer.WriteString("question", question);
            writer.WriteBoolean("advice", report is not null);
            if (report is not null)
            {
                writer.WritePropertyName("report");
                report.RootElement.WriteTo(writer);
            }
            else
            {
                writer.WriteString("reason", Reason(result.Stderr));
            }
        });
    }

    internal static string Reason(string stderr)
    {
        const string prefix = "projectbuddy-diagnose: ";
        var line = stderr.Split('\n', 2)[0].Trim();
        line = line.StartsWith(prefix, StringComparison.Ordinal) ? line[prefix.Length..] : line;
        return line.Length <= MaxReasonChars ? line : line[..MaxReasonChars];
    }

    internal static byte[] Envelope(string verb, Action<Utf8JsonWriter> data)
    {
        using var buffer = new MemoryStream();
        using (var writer = new Utf8JsonWriter(buffer))
        {
            writer.WriteStartObject();
            writer.WriteString("schema", Versions.Schema);
            writer.WriteString("tool", Versions.Tool);
            writer.WriteString("verb", verb);
            writer.WriteStartObject("data");
            data(writer);
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

    // The id starts with a letter or digit, so it can never read as an option; \z, unlike $, admits no trailing newline.
    [GeneratedRegex("^diagnose ([a-z0-9][a-z0-9-]{0,63})\\z")]
    private static partial Regex DiagnoseCommand();
}
