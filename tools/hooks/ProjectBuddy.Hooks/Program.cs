using System.Diagnostics.CodeAnalysis;
using System.Globalization;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using Npgsql;

namespace ProjectBuddy.Hooks;

public static class ExitCodes
{
    public const int Ok = 0;
    public const int Usage = 2;
    public const int Refused = 3;
    public const int Database = 5;
    public const int Forge = 6;
}

public sealed class HooksException(int exitCode, string message) : Exception(message)
{
    public int ExitCode { get; } = exitCode;
}

public sealed record Options(string Command, string Host, int Port, string User, string PasswordFile, string Database, IReadOnlyDictionary<string, string> Values)
{
    public string? Get(string name) => Values.GetValueOrDefault(name);
}

// projectbuddy-hooks (#381): what the orchestrator reads and writes next to the user-level Copilot hooks: the session registry,
// the decision queue and the compare-and-swap writes of a handoff issue such as #23.
public static partial class Program
{
    public const string Usage =
        "usage: projectbuddy-hooks sessions [--status <running|waiting|done>]"
        + " | sessions-set --session <id> [--work-ref <owner/repo#N>] [--parent <id>] [--title <text>]"
        + " | decisions [--all] | answer --id <n> --text <text>"
        + " | handoff-read --work-ref <owner/repo#N> [--gh <path>]"
        + " | handoff-write --work-ref <owner/repo#N> --expected <updatedAt> --body-file <path> [--session <id>] [--gh <path>] [--lock-seconds <n>]"
        + " --password-file <path> [--host <host>] [--port <n>] [--user <name>] [--database <name>]";

    private static readonly Dictionary<string, (string[] Required, string[] Optional)> Commands = new(StringComparer.Ordinal)
    {
        ["sessions"] = ([], ["--status"]),
        ["sessions-set"] = (["--session"], ["--work-ref", "--parent", "--title"]),
        ["decisions"] = ([], ["--all"]),
        ["answer"] = (["--id", "--text"], []),
        ["handoff-read"] = (["--work-ref"], ["--gh"]),
        ["handoff-write"] = (["--work-ref", "--expected", "--body-file"], ["--session", "--gh", "--lock-seconds"]),
    };

    private static readonly string[] Common = ["--password-file", "--host", "--port", "--user", "--database"];

    [ExcludeFromCodeCoverage(Justification = "The process entry point; Run takes the writers as parameters.")]
    public static int Main(string[] args) => Run(args, Console.Out, Console.Error);

    public static int Run(string[] args, TextWriter stdout, TextWriter stderr)
    {
        try
        {
            var options = Parse(args);
            var database = new Database(options);
            JsonNode result = options.Command switch
            {
                "sessions" => database.Sessions(options.Get("--status")),
                "sessions-set" => database.SetSession(options),
                "decisions" => database.Decisions(options.Values.ContainsKey("--all")),
                "answer" => database.Answer(Integer(options, "--id"), options.Get("--text")!),
                "handoff-read" => Handoff.Read(options),
                _ => Handoff.Write(database, options),
            };
            stdout.WriteLine(result.ToJsonString());
            return ExitCodes.Ok;
        }
        catch (HooksException error)
        {
            stderr.WriteLine($"projectbuddy-hooks: {error.Message}");
            if (error.ExitCode == ExitCodes.Usage)
            {
                stderr.WriteLine(Usage);
            }

            return error.ExitCode;
        }
        catch (IOException error)
        {
            stderr.WriteLine($"projectbuddy-hooks: {error.Message}");
            return ExitCodes.Usage;
        }
        catch (NpgsqlException error)
        {
            stderr.WriteLine($"projectbuddy-hooks: database: {error.Message}");
            return ExitCodes.Database;
        }
    }

    internal static Options Parse(string[] args)
    {
        if (args.Length == 0 || !Commands.TryGetValue(args[0], out var spec))
        {
            throw UsageError("expected a command: " + string.Join(", ", Commands.Keys));
        }

        var values = new Dictionary<string, string>(StringComparer.Ordinal);
        for (var index = 1; index < args.Length; index++)
        {
            var name = args[index];
            if (!(spec.Required.Contains(name) || spec.Optional.Contains(name) || Common.Contains(name)) || values.ContainsKey(name))
            {
                throw UsageError($"unexpected argument '{name}'");
            }

            if (name == "--all")
            {
                values[name] = "";
                continue;
            }

            if (++index >= args.Length)
            {
                throw UsageError($"{name} needs a value");
            }

            values[name] = args[index];
        }

        foreach (var name in spec.Required.Where(name => !values.ContainsKey(name)))
        {
            throw UsageError($"{name} is required");
        }

        var port = values.GetValueOrDefault("--port", "5432");
        if (!int.TryParse(port, NumberStyles.None, CultureInfo.InvariantCulture, out var number) || number is < 1 or > 65535)
        {
            throw UsageError("--port must be 1-65535");
        }

        var user = values.GetValueOrDefault("--user", "postgres");
        var database = values.GetValueOrDefault("--database", "kpi_local");
        if (!Identifier().IsMatch(user) || !Identifier().IsMatch(database))
        {
            throw UsageError("--user and --database must be plain identifiers ([a-z_][a-z0-9_]*)");
        }

        if (!values.TryGetValue("--password-file", out var password) || !Path.IsPathFullyQualified(password))
        {
            throw UsageError("--password-file needs a fully qualified path");
        }

        values.TryAdd("--gh", "gh");
        values.TryAdd("--lock-seconds", "30");
        var parsed = new Options(args[0], values.GetValueOrDefault("--host", "127.0.0.1"), number, user, password, database, values);
        if (parsed.Get("--status") is { } status && status is not ("running" or "waiting" or "done"))
        {
            throw UsageError("--status must be running, waiting or done");
        }

        foreach (var name in new[] { "--session", "--parent" }.Where(values.ContainsKey))
        {
            if (!SessionId().IsMatch(values[name]))
            {
                throw UsageError($"{name} must be a session id ([A-Za-z0-9._-], 8-80 characters)");
            }
        }

        if (parsed.Get("--work-ref") is { } workRef && !WorkRef().IsMatch(workRef))
        {
            throw UsageError("--work-ref must look like owner/repo#N");
        }

        return parsed;
    }

    internal static int Integer(Options options, string name) =>
        int.TryParse(options.Get(name), NumberStyles.None, CultureInfo.InvariantCulture, out var value) ? value : throw UsageError($"{name} must be a whole number");

    internal static HooksException UsageError(string message) => new(ExitCodes.Usage, message);

    [GeneratedRegex("^[a-z_][a-z0-9_]{0,62}$")]
    private static partial Regex Identifier();

    [GeneratedRegex("^[A-Za-z0-9._-]{8,80}$")]
    private static partial Regex SessionId();

    [GeneratedRegex(@"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[0-9]+$")]
    internal static partial Regex WorkRef();
}
