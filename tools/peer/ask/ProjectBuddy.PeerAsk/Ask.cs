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

    // The peer allows diagnose its own timeout; the client waits a little longer, so the peer's answer arrives first.
    public static readonly TimeSpan DiagnoseAllowance = Serve.DiagnoseTimeout + TimeSpan.FromSeconds(20);

    // Likewise for infer, whose whole-call timeout already covers reading the request.
    public static readonly TimeSpan InferAllowance = Infer.Timeout + TimeSpan.FromSeconds(20);

    // An infer request travels as ssh's stdin, byte for byte as read from the file.
    public sealed record Options(string Verb, string Host, string User, string Identity, string KnownHosts, int ConnectTimeout, string Ssh, string? Question = null,
        InferRequest? Request = null, byte[]? RequestBytes = null);

    public static int Execute(string[] args, IProcessRunner runner, string defaultSsh, Func<string, bool> fileExists, TextWriter stdout, TextWriter stderr)
    {
        var options = Parse(args, defaultSsh, fileExists, out var problem);
        if (options is null)
        {
            stderr.WriteLine($"projectbuddy-ask: {problem}");
            stderr.WriteLine(Program.Usage);
            return ExitCodes.Usage;
        }

        var allowance = options.Request is not null ? InferAllowance : options.Question is null ? CommandAllowance : DiagnoseAllowance;
        var result = runner.Run(options.Ssh, SshArguments(options), TimeSpan.FromSeconds(options.ConnectTimeout) + allowance, options.RequestBytes);
        var (code, message) = Classify(result, options.Verb, options.Question, options.Request);
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
        string? question = null;
        if (verb == Serve.DiagnoseVerb)
        {
            question = args.Length > 1 ? Serve.Question($"{verb} {args[1]}") : null;
            if (question is null)
            {
                problem = "diagnose needs a question id: a letter or digit, then [a-z0-9-], at most 64 characters";
                return null;
            }
        }
        else if (verb != Infer.Verb && !Serve.Verbs.Contains(verb))
        {
            problem = $"unknown verb '{verb}'";
            return null;
        }

        var infer = verb == Infer.Verb;
        var values = new Dictionary<string, string>(StringComparer.Ordinal);
        string[] required = infer ? ["--host", "--user", "--identity", "--known-hosts", "--request"] : ["--host", "--user", "--identity", "--known-hosts"];
        string[] known = [.. required, "--connect-timeout", "--ssh"];
        for (var i = question is null ? 1 : 2; i < args.Length; i += 2)
        {
            if (!known.Contains(args[i]) || i + 1 >= args.Length || values.ContainsKey(args[i]))
            {
                problem = $"bad option '{args[i]}'";
                return null;
            }

            values[args[i]] = args[i + 1];
        }

        foreach (var option in required)
        {
            if (!values.ContainsKey(option))
            {
                problem = $"missing {option}";
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

        InferRequest? request = null;
        byte[]? bytes = null;
        if (infer)
        {
            bytes = ReadRequest(values["--request"]);
            if (bytes is null)
            {
                problem = $"--request must be a readable file of at most {Infer.MaxRequestBytes} bytes";
                return null;
            }

            try
            {
                request = Infer.ParseRequest(bytes);
            }
            catch (RefusedException error)
            {
                problem = $"--request is not a valid {Infer.RequestSchema} document: {error.Message}";
                return null;
            }
        }

        return new Options(verb, host, values["--user"], values["--identity"], values["--known-hosts"], connectTimeout, ssh, question, request, bytes);
    }

    // The request file stays on the workstation; only its bytes travel, so it needs no path rules beyond being readable.
    private static byte[]? ReadRequest(string path)
    {
        try
        {
            return BoundedFile.Read(path, Infer.MaxRequestBytes);
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or ArgumentException)
        {
            return null;
        }
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
        .. options.Question is null ? Array.Empty<string>() : [options.Question],
    ];

    public static readonly IReadOnlyList<string> HostKeyPhrases =
        ["Host key verification failed", "REMOTE HOST IDENTIFICATION", "host key is known for"];

    public static readonly IReadOnlyList<string> AuthPhrases = ["Permission denied"];

    public static readonly IReadOnlyList<string> UnreachablePhrases =
    [
        "timed out", "No route to host", "Network is unreachable", "Connection refused", "Connection closed",
        "Connection reset", "Could not resolve hostname",
    ];

    public static (int Code, string Message) Classify(ProcessResult result, string verb, string? question = null, InferRequest? request = null)
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
                return Validate(result.Stdout, verb, question, request);
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

    private static (int Code, string Message) Validate(string stdout, string verb, string? question, InferRequest? request)
    {
        if (Encoding.UTF8.GetByteCount(stdout) > (verb == Infer.Verb ? Infer.MaxOutputBytes : Serve.MaxOutputBytes) + 2)
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
                && root.TryGetProperty("data", out var data) && data.ValueKind == JsonValueKind.Object
                && (question is null || IsDiagnosis(data, question))
                && (verb != Infer.Verb || (request is not null && IsInference(data, request))))
            {
                return (0, "");
            }
        }
        catch (Exception error) when (error is JsonException or InvalidOperationException)
        {
            // InvalidOperationException: a string with an escaped lone surrogate cannot be read.
        }

        return (ExitCodes.Protocol, "protocol: answer is not a projectbuddy/peer-ask@1 document for this verb");
    }

    // A diagnose answer names its question and either carries a grounded report or says why there is no advice.
    private static bool IsDiagnosis(JsonElement data, string question) =>
        data.TryGetProperty("question", out var answered) && answered.ValueKind == JsonValueKind.String && answered.GetString() == question
        && data.TryGetProperty("advice", out var advice) && advice.ValueKind is JsonValueKind.True or JsonValueKind.False
        && (advice.ValueKind == JsonValueKind.True
            ? data.TryGetProperty("report", out var report) && report.ValueKind == JsonValueKind.Object
                && report.TryGetProperty("schema", out var schema) && schema.ValueKind == JsonValueKind.String && schema.GetString() == Serve.DiagnosisSchema
                && report.TryGetProperty("verified", out var verified) && verified.ValueKind == JsonValueKind.Array
                && verified.EnumerateArray().Any(item => item.ValueKind == JsonValueKind.String && item.GetString() == "citations")
            : data.TryGetProperty("reason", out var reason) && reason.ValueKind == JsonValueKind.String);

    // An infer answer names the requested model and its pinned digest, and answers every requested id exactly once, from an
    // allowed port, and nothing else.
    private static bool IsInference(JsonElement data, InferRequest request)
    {
        if (!(data.TryGetProperty("model", out var model) && model.ValueKind == JsonValueKind.String && model.GetString() == request.Model
              && data.TryGetProperty("digest", out var digest) && digest.ValueKind == JsonValueKind.String && Digest().IsMatch(digest.GetString()!)
              && data.TryGetProperty("items", out var items) && items.ValueKind == JsonValueKind.Array))
        {
            return false;
        }

        var expected = request.Items.Select(item => item.Id).ToHashSet(StringComparer.Ordinal);
        var answered = new HashSet<string>(StringComparer.Ordinal);
        foreach (var item in items.EnumerateArray())
        {
            if (!(item.ValueKind == JsonValueKind.Object
                  && item.TryGetProperty("id", out var id) && id.ValueKind == JsonValueKind.String
                  && expected.Contains(id.GetString()!) && answered.Add(id.GetString()!)
                  && item.TryGetProperty("answer", out var answer) && answer.ValueKind == JsonValueKind.String
                  && Count(item, "evalCount") && Count(item, "promptEvalCount")
                  && item.TryGetProperty("seconds", out var seconds) && seconds.ValueKind == JsonValueKind.Number && seconds.GetDouble() >= 0
                  && item.TryGetProperty("port", out var port) && port.ValueKind == JsonValueKind.Number && port.TryGetInt32(out var number)
                  && Infer.AllowedPorts.Contains(number)))
            {
                return false;
            }
        }

        return answered.Count == expected.Count;
    }

    private static bool Count(JsonElement item, string name) =>
        item.TryGetProperty(name, out var count) && count.ValueKind == JsonValueKind.Number && count.TryGetInt64(out var value) && value >= 0;

    [GeneratedRegex("^[0-9a-f]{64}\\z")]
    private static partial Regex Digest();

    [GeneratedRegex("^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")]
    private static partial Regex HostName();

    [GeneratedRegex("^[A-Za-z0-9_][A-Za-z0-9._-]{0,63}$")]
    private static partial Regex UserName();

    // OpenSSH expands %tokens and ${VAR} in these paths, so neither may appear.
    [GeneratedRegex("^[^\\s\"'%$\\p{Cc}]+$")]
    private static partial Regex SafePath();
}
