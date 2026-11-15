namespace ProjectBuddy.PeerBench;

public static class Program
{
    public const string Usage =
        "usage: projectbuddy-bench run --corpus <file> --candidates <file> --machine workstation|peer [--ollama <http://127.0.0.1:port>] [--timeout-seconds <n>] | --version";

    public const string DefaultOllama = "http://127.0.0.1:11434";

    public static int Main(string[] args) =>
        Run(args, File.ReadAllBytes, new SocketsHttpHandler(), Console.Out, Console.Error);

    public static int Run(string[] args, Func<string, byte[]> readFile, HttpMessageHandler handler, TextWriter stdout, TextWriter stderr)
    {
        if (args is ["--version"])
        {
            stdout.WriteLine($"{Versions.Tool} {Versions.ResultSchema}");
            return 0;
        }

        if (args is not ["run", .. var rest] || !TryParse(rest, out var options))
        {
            stderr.WriteLine(Usage);
            return ExitCodes.Usage;
        }

        try
        {
            var corpus = Corpus.Parse(Read(readFile, options["--corpus"]));
            var candidates = Candidate.ParseAll(Read(readFile, options["--candidates"]));
            var baseUri = OllamaClient.RequireLoopback(options["--ollama"]);
            using var ollama = new OllamaClient(handler, baseUri, TimeSpan.FromSeconds(int.Parse(options["--timeout-seconds"])));
            var result = Bench.RunAsync(corpus, candidates, options["--machine"], ollama, stderr).GetAwaiter().GetResult();
            stdout.WriteLine(result.ToJsonString());
            return 0;
        }
        catch (BenchException error)
        {
            stderr.WriteLine($"projectbuddy-bench: {error.Message}");
            return error.ExitCode;
        }
    }

    internal static bool TryParse(string[] args, out Dictionary<string, string> options)
    {
        options = new Dictionary<string, string>(StringComparer.Ordinal)
        {
            ["--ollama"] = DefaultOllama,
            ["--timeout-seconds"] = "600",
        };
        string[] known = ["--corpus", "--candidates", "--machine", "--ollama", "--timeout-seconds"];
        var seen = new HashSet<string>(StringComparer.Ordinal);
        for (var index = 0; index < args.Length; index += 2)
        {
            if (!known.Contains(args[index]) || index + 1 >= args.Length || !seen.Add(args[index]))
            {
                return false;
            }

            options[args[index]] = args[index + 1];
        }

        return seen.IsSupersetOf(["--corpus", "--candidates", "--machine"])
            && options["--machine"] is "workstation" or "peer"
            && int.TryParse(options["--timeout-seconds"], out var seconds) && seconds is >= 1 and <= 3600;
    }

    private static byte[] Read(Func<string, byte[]> readFile, string path)
    {
        try
        {
            return readFile(path);
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException)
        {
            throw new BenchException(ExitCodes.Refused, $"cannot read {Path.GetFileName(path)}");
        }
    }
}

public static class Versions
{
    public const string Tool = "0.1.0";
    public const string ResultSchema = "projectbuddy/peer-bench-result@1";
}

public static class ExitCodes
{
    public const int Usage = 2;
    public const int Refused = 3;
    public const int GuardBroken = 4;
    public const int Unreachable = 5;
}

public sealed class BenchException(int exitCode, string message) : Exception(message)
{
    public int ExitCode { get; } = exitCode;
}
