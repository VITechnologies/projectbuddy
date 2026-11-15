using ProjectBuddy.PeerOllama;

namespace ProjectBuddy.PeerAdvise;

public static class Program
{
    public const string Usage =
        "usage: projectbuddy-advise triage --policy <file> [--input <file>] [--ollama <http://127.0.0.1:port>] [--timeout-seconds <n>] | --version";

    public const string DefaultOllama = "http://127.0.0.1:11434";

    public static int Main(string[] args) =>
        Run(args, File.ReadAllBytes, ReadStdin, OllamaClient.LoopbackHandler(), Console.Out, Console.Error);

    public static int Run(
        string[] args,
        Func<string, byte[]> readFile,
        Func<byte[]> readStdin,
        HttpMessageHandler handler,
        TextWriter stdout,
        TextWriter stderr)
    {
        if (args is ["--version"])
        {
            stdout.WriteLine($"{Versions.Tool} {Versions.TriageSchema}");
            return 0;
        }

        if (args is not ["triage", .. var rest] || !TryParse(rest, out var options))
        {
            stderr.WriteLine(Usage);
            return ExitCodes.Usage;
        }

        try
        {
            var policyBytes = Read(readFile, options["--policy"]);
            var policy = Policy.Parse(policyBytes);
            var input = options.TryGetValue("--input", out var path) ? Read(readFile, path) : readStdin();
            var baseUri = OllamaClient.RequireLoopback(options["--ollama"]);
            using var ollama = new OllamaClient(handler, baseUri, TimeSpan.FromSeconds(int.Parse(options["--timeout-seconds"])));
            var report = Triage.RunAsync(policy, input, ollama).GetAwaiter().GetResult();
            stdout.WriteLine(report.ToJsonString());
            return 0;
        }
        catch (PeerException error)
        {
            // Never print a partial answer: a failed run yields no advice at all.
            stderr.WriteLine($"projectbuddy-advise: {error.Message}");
            return error.ExitCode;
        }
    }

    internal static bool TryParse(string[] args, out Dictionary<string, string> options)
    {
        options = new Dictionary<string, string>(StringComparer.Ordinal)
        {
            ["--ollama"] = DefaultOllama,
            ["--timeout-seconds"] = "300",
        };
        string[] known = ["--policy", "--input", "--ollama", "--timeout-seconds"];
        var seen = new HashSet<string>(StringComparer.Ordinal);
        for (var index = 0; index < args.Length; index += 2)
        {
            if (!known.Contains(args[index]) || index + 1 >= args.Length || !seen.Add(args[index]))
            {
                return false;
            }

            options[args[index]] = args[index + 1];
        }

        return seen.Contains("--policy")
            && int.TryParse(options["--timeout-seconds"], out var seconds) && seconds is >= 1 and <= 3600;
    }

    [System.Diagnostics.CodeAnalysis.ExcludeFromCodeCoverage(Justification = "The process's real standard input; Run takes it as an injected reader.")]
    private static byte[] ReadStdin()
    {
        using var buffer = new MemoryStream();
        Console.OpenStandardInput().CopyTo(buffer);
        return buffer.ToArray();
    }

    private static byte[] Read(Func<string, byte[]> readFile, string path)
    {
        try
        {
            return readFile(path);
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException)
        {
            throw new PeerException(ExitCodes.Refused, $"cannot read {Path.GetFileName(path)}");
        }
    }
}

public static class Versions
{
    public const string Tool = "0.1.0";
    public const string TriageSchema = "projectbuddy/advise-triage@1";
}

public static class AdviseExitCodes
{
    // The model answered, but nothing it said could be trusted (prompt may have been cut, or the answer was ungrounded).
    public const int NoAdvice = 6;
}
