namespace ProjectBuddy.PeerAsk;

public static class Program
{
    public const string Usage =
        "usage: projectbuddy-ask serve | ask <verb> --host <address> --user <name> --identity <file> --known-hosts <file> [--connect-timeout <seconds>] [--ssh <path>] | --version";

    public static int Main(string[] args) =>
        Run(args, Environment.GetEnvironmentVariable, new SystemProcessRunner(), new SystemFacts(), File.Exists, Console.Out, Console.Error);

    public static int Run(
        string[] args,
        Func<string, string?> environment,
        IProcessRunner runner,
        IHostFacts facts,
        Func<string, bool> fileExists,
        TextWriter stdout,
        TextWriter stderr)
    {
        if (args is ["serve"])
        {
            return Serve.Execute(environment("SSH_ORIGINAL_COMMAND"), runner, facts, stdout, stderr);
        }

        if (args is ["ask", .. var rest])
        {
            return Ask.Execute(rest, runner, facts.SshPath, fileExists, stdout, stderr);
        }

        if (args is ["--version"])
        {
            stdout.WriteLine($"{Versions.Tool} {Versions.Schema}");
            return 0;
        }

        stderr.WriteLine(Usage);
        return ExitCodes.Usage;
    }
}

public static class Versions
{
    public const string Tool = "0.2.0";
    public const string Schema = "projectbuddy/peer-ask@1";
}

public static class ExitCodes
{
    public const int Usage = 2;
    public const int Refused = 64;
    public const int OutputLimit = 65;
    public const int Internal = 66;

    // Client-side classification of a failed round trip.
    public const int PeerRefused = 3;
    public const int HostKey = 4;
    public const int Auth = 5;
    public const int Unreachable = 6;
    public const int SshError = 7;
    public const int PeerError = 8;
    public const int Protocol = 9;
}
