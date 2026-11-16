using System.Diagnostics.CodeAnalysis;
using System.Text.Encodings.Web;
using System.Text.Json;
using ProjectBuddy.PeerOllama;
using ProjectBuddy.PeerProbes;
using ProjectBuddy.PeerRedact;

namespace ProjectBuddy.PeerDiagnose;

public static class Program
{
    public const string Usage =
        "usage: projectbuddy-diagnose --policy <file> --question <id> [--deny-list <file>] [--ollama <http://127.0.0.1:port>] [--vboxmanage <path>] [--timeout-seconds <n>] | --version";

    public const string DefaultOllama = "http://127.0.0.1:11434";

    private static readonly JsonSerializerOptions Output = new() { Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping };

    [ExcludeFromCodeCoverage(Justification = "The process entry point; Run takes the file reader, HTTP handler, sources and host as parameters.")]
    public static int Main(string[] args) =>
        Run(args, File.ReadAllBytes, OllamaClient.LoopbackHandler(), (vboxManage, ollama) => new SystemSources(ollama, vboxManage),
            (Environment.MachineName, Environment.UserName), Console.Out, Console.Error);

    public static int Run(
        string[] args,
        Func<string, byte[]> readFile,
        HttpMessageHandler handler,
        Func<string?, Uri, ISources> sources,
        (string Machine, string User) host,
        TextWriter stdout,
        TextWriter stderr)
    {
        if (args is ["--version"])
        {
            stdout.WriteLine($"projectbuddy-diagnose {Versions.Tool} {Versions.ReportSchema}");
            return 0;
        }

        if (!TryParse(args, out var options))
        {
            stderr.WriteLine(Usage);
            return ExitCodes.Usage;
        }

        try
        {
            var policy = Policy.Parse(Read(readFile, options["--policy"]));
            var question = policy.Find(options["--question"]);
            var baseUri = OllamaClient.RequireLoopback(options["--ollama"]);
            var redactor = HostRedactor.Load(options.GetValueOrDefault("--deny-list"), host.Machine, host.User);

            // Each tool result may use at most half the evidence budget, so one large result cannot crowd out the rest.
            var probeOptions = new ProbeOptions(MaxResultBytes: Math.Clamp(policy.MaxEvidenceBytes / 2, Registry.MinResultBytes, Registry.MaxResultBytes));
            var registry = new Registry(sources(options.GetValueOrDefault("--vboxmanage"), baseUri), redactor.Redact, probeOptions);
            using var ollama = new OllamaClient(handler, baseUri, TimeSpan.FromSeconds(int.Parse(options["--timeout-seconds"])));
            var report = Diagnosis.RunAsync(policy, question, ollama, registry, redactor.Redact).GetAwaiter().GetResult();
            stdout.WriteLine(report.ToJsonString(Output));
            return 0;
        }
        catch (PeerException error)
        {
            // Never print a partial answer: a failed run yields no advice at all.
            stderr.WriteLine($"projectbuddy-diagnose: {error.Message}");
            return error.ExitCode;
        }
        catch (RedactionException error)
        {
            stderr.WriteLine($"projectbuddy-diagnose: {error.Message}");
            return ExitCodes.Refused;
        }
    }

    internal static bool TryParse(string[] args, out Dictionary<string, string> options)
    {
        options = new Dictionary<string, string>(StringComparer.Ordinal)
        {
            ["--ollama"] = DefaultOllama,
            ["--timeout-seconds"] = "300",
        };
        string[] known = ["--policy", "--question", "--deny-list", "--ollama", "--vboxmanage", "--timeout-seconds"];
        var seen = new HashSet<string>(StringComparer.Ordinal);
        for (var index = 0; index < args.Length; index += 2)
        {
            if (!known.Contains(args[index]) || index + 1 >= args.Length || !seen.Add(args[index]))
            {
                return false;
            }

            options[args[index]] = args[index + 1];
        }

        return seen.Contains("--policy") && seen.Contains("--question")
            && int.TryParse(options["--timeout-seconds"], out var seconds) && seconds is >= 1 and <= 3600
            && (!options.TryGetValue("--vboxmanage", out var vboxManage) || Path.IsPathFullyQualified(vboxManage));
    }

    private static byte[] Read(Func<string, byte[]> readFile, string path)
    {
        try
        {
            return readFile(path);
        }
        catch (Exception error)
        {
            throw new PeerException(ExitCodes.Refused, $"cannot read the policy: {error.GetType().Name}");
        }
    }
}
