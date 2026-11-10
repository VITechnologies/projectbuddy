using System.Net;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace ProjectBuddy.PeerAsk;

// The workstation client: one fixed ssh invocation, then classification of the result.
public static partial class Ask
{
    public const int DefaultConnectTimeout = 5;
    public static readonly TimeSpan CommandAllowance = TimeSpan.FromSeconds(30);

    public sealed record Options(string Verb, string Host, string User, string Identity, string KnownHosts, int ConnectTimeout, string Ssh);

    public static int Execute(string[] args, IProcessRunner runner, string defaultSsh, Func<string, bool> fileExists, TextWriter stdout, TextWriter stderr)
    {
        var options = Parse(args, defaultSsh, fileExists, out var problem);
        if (options is null)
        {
            stderr.WriteLine($"projectbuddy-ask: {problem}");
            stderr.WriteLine(Program.Usage);
            return ExitCodes.Usage;
        }

        var timeout = TimeSpan.FromSeconds(options.ConnectTimeout) + CommandAllowance;
        var result = runner.Run(options.Ssh, SshArguments(options), timeout);
        var (code, message) = Classify(result, options.Verb);
        if (code == 0)
        {
            stdout.WriteLine(result.Stdout.Trim());
            return 0;
        }

        stderr.WriteLine($"projectbuddy-ask: {message}");
        return code;
    }

    public static Options? Parse(string[] args, string defaultSsh, Func<string, bool> fileExists, out string problem)
    {
        problem = "";
        if (args.Length == 0 || args[0].StartsWith('-'))
        {
            problem = "missing verb";
            return null;
        }

        var verb = args[0];
        if (!Serve.Verbs.Contains(verb))
        {
            problem = $"unknown verb '{verb}'";
            return null;
        }

        var values = new Dictionary<string, string>(StringComparer.Ordinal);
        string[] known = ["--host", "--user", "--identity", "--known-hosts", "--connect-timeout", "--ssh"];
        for (var i = 1; i < args.Length; i += 2)
        {
            if (!known.Contains(args[i]) || i + 1 >= args.Length || values.ContainsKey(args[i]))
            {
                problem = $"bad option '{args[i]}'";
                return null;
            }

            values[args[i]] = args[i + 1];
        }

        foreach (var required in known[..4])
        {
            if (!values.ContainsKey(required))
            {
                problem = $"missing {required}";
                return null;
            }
        }

        var host = values["--host"];
        if (!(IPAddress.TryParse(host, out var address) && address.ToString() == host) && !HostName().IsMatch(host))
        {
            problem = "--host must be an IP address or a host name";
            return null;
        }

        if (!UserName().IsMatch(values["--user"]))
        {
            problem = "--user must be a plain account name";
            return null;
        }

        if (!values.ContainsKey("--ssh") && !fileExists(defaultSsh))
        {
            problem = $"no ssh client at {defaultSsh}; pass --ssh";
            return null;
        }

        var ssh = values.GetValueOrDefault("--ssh", defaultSsh);
        foreach (var (option, path) in new[] { ("--identity", values["--identity"]), ("--known-hosts", values["--known-hosts"]), ("--ssh", ssh) })
        {
            if (!SafePath().IsMatch(path) || !Path.IsPathFullyQualified(path) || !fileExists(path))
            {
                problem = $"{option} must be an existing absolute path without spaces, quotes or %";
                return null;
            }
        }

        var connectTimeout = DefaultConnectTimeout;
        if (values.TryGetValue("--connect-timeout", out var text)
            && (!int.TryParse(text, out connectTimeout) || connectTimeout is < 1 or > 60))
        {
            problem = "--connect-timeout must be 1..60";
            return null;
        }

        return new Options(verb, host, values["--user"], values["--identity"], values["--known-hosts"], connectTimeout, ssh);
    }

    public static IReadOnlyList<string> SshArguments(Options options) =>
    [
        "-F", "none",
        "-o", $"IdentityFile={options.Identity}",
        "-o", "IdentitiesOnly=yes",
        "-o", "IdentityAgent=none",
        "-o", $"UserKnownHostsFile={options.KnownHosts}",
        "-o", "GlobalKnownHostsFile=none",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "UpdateHostKeys=no",
        "-o", "CheckHostIP=no",
        "-o", "BatchMode=yes",
        "-o", "PasswordAuthentication=no",
        "-o", "KbdInteractiveAuthentication=no",
        "-o", $"ConnectTimeout={options.ConnectTimeout}",
        "-o", "ConnectionAttempts=1",
        "-o", "ServerAliveInterval=5",
        "-o", "ServerAliveCountMax=2",
        "-o", "ClearAllForwardings=yes",
        "-o", "ForwardAgent=no",
        "-o", "ForwardX11=no",
        "-o", "RequestTTY=no",
        "-o", "PermitLocalCommand=no",
        "-o", "LogLevel=ERROR",
        "-l", options.User,
        "--", options.Host, options.Verb,
    ];

    public static readonly IReadOnlyList<string> HostKeyPhrases =
        ["Host key verification failed", "REMOTE HOST IDENTIFICATION", "host key is known for"];

    public static readonly IReadOnlyList<string> AuthPhrases = ["Permission denied"];

    public static readonly IReadOnlyList<string> UnreachablePhrases =
    [
        "timed out", "No route to host", "Network is unreachable", "Connection refused", "Connection closed",
        "Connection reset", "Could not resolve hostname",
    ];

    public static (int Code, string Message) Classify(ProcessResult result, string verb)
    {
        if (!result.Started)
        {
            return (ExitCodes.SshError, "ssh-error: ssh could not be started");
        }

        if (result.TimedOut)
        {
            return (ExitCodes.Unreachable, "unreachable: timed out");
        }

        switch (result.ExitCode)
        {
            case 0:
                return Validate(result.Stdout, verb);
            case ExitCodes.Refused:
                return (ExitCodes.PeerRefused, "refused: the peer refused the request");
            case ExitCodes.OutputLimit:
                return (ExitCodes.PeerError, "peer-error: the answer exceeded the output limit");
            case ExitCodes.Internal:
                return (ExitCodes.PeerError, "peer-error: internal error on the peer");
        }

        // OpenSSH prints advisory "** WARNING" banners before the real error, so match on every line.
        var lines = result.Stderr.Split('\n').Select(line => line.Trim()).Where(line => line.Length > 0 && !line.StartsWith("**", StringComparison.Ordinal)).ToList();
        var detail = Serve.Field(lines.FirstOrDefault() ?? "");
        if (result.ExitCode != 255)
        {
            return (ExitCodes.PeerError, $"peer-error: exit {result.ExitCode}: {detail}");
        }

        foreach (var (code, label, phrases) in new[]
                 {
                     (ExitCodes.HostKey, "host-key", HostKeyPhrases),
                     (ExitCodes.Auth, "auth", AuthPhrases),
                     (ExitCodes.Unreachable, "unreachable", UnreachablePhrases),
                 })
        {
            var match = lines.FirstOrDefault(line => phrases.Any(phrase => line.Contains(phrase, StringComparison.OrdinalIgnoreCase)));
            if (match is not null)
            {
                return (code, $"{label}: {Serve.Field(match)}");
            }
        }

        return (ExitCodes.SshError, $"ssh-error: {detail}");
    }

    private static (int Code, string Message) Validate(string stdout, string verb)
    {
        if (Encoding.UTF8.GetByteCount(stdout) > Serve.MaxOutputBytes + 2)
        {
            return (ExitCodes.Protocol, "protocol: answer exceeds the output limit");
        }

        try
        {
            using var document = JsonDocument.Parse(stdout);
            var root = document.RootElement;
            if (root.ValueKind == JsonValueKind.Object
                && root.TryGetProperty("schema", out var schema) && schema.ValueKind == JsonValueKind.String && schema.GetString() == Versions.Schema
                && root.TryGetProperty("verb", out var answered) && answered.ValueKind == JsonValueKind.String && answered.GetString() == verb
                && root.TryGetProperty("data", out var data) && data.ValueKind == JsonValueKind.Object)
            {
                return (0, "");
            }
        }
        catch (JsonException)
        {
        }

        return (ExitCodes.Protocol, "protocol: answer is not a projectbuddy/peer-ask@1 document for this verb");
    }

    [GeneratedRegex("^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")]
    private static partial Regex HostName();

    [GeneratedRegex("^[A-Za-z0-9_][A-Za-z0-9._-]{0,63}$")]
    private static partial Regex UserName();

    // OpenSSH expands %tokens and ${VAR} in these paths, so neither may appear.
    [GeneratedRegex("^[^\\s\"'%$\\p{Cc}]+$")]
    private static partial Regex SafePath();
}
