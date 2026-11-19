using System.Diagnostics.CodeAnalysis;
using System.Globalization;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using Npgsql;

namespace ProjectBuddy.Kpi;

public static class ExitCodes
{
    public const int Ok = 0;
    public const int Usage = 2;
    public const int Refused = 3;
    public const int Alarm = 4;
    public const int Database = 5;
}

public sealed class KpiException(int exitCode, string message) : Exception(message)
{
    public int ExitCode { get; } = exitCode;
}

public sealed record Options(string Command, string Host, int Port, string User, string PasswordFile, string Database, IReadOnlyDictionary<string, string> Values)
{
    public string? Get(string name) => Values.GetValueOrDefault(name);
}

// projectbuddy-kpi (#328, #330): the cost of AI-assisted work, from the agent's session event logs, and the KPIs that
// steer spec-driven design.
public static partial class Program
{
    public const string Usage =
        "usage: projectbuddy-kpi migrate [--create-database] | ingest [--sessions <dir>] | ingest-farm --export <file>"
        + " | farm-reference --workload <id> --aiu-per-job <n> | track --repository <owner/repo|group/project> --forge <github|gitlab>"
        + " | resolve [--gh <path>] [--glab <path>] | report"
        + " | mark --session <id> --item <ref> | gap --session <id> --item <ref> --kind <code-read|operator-question|failed-run> --fact <text> --belongs-in <document>"
        + " | review --session <id> --item <ref> --round <n> --findings <n> | requirements --item <ref> --count <n>"
        + " | operator --tier <T1-T9> --minutes <n> --activity <prompts|approvals|technician|review|watching|restarts> [--item <ref>] [--note <text>]"
        + " --password-file <path> [--host <host>] [--port <n>] [--user <name>] [--database <name>]";

    private static readonly Dictionary<string, (string[] Required, string[] Optional)> Commands = new(StringComparer.Ordinal)
    {
        ["migrate"] = ([], ["--create-database"]),
        ["ingest"] = ([], ["--sessions"]),
        ["ingest-farm"] = (["--export"], []),
        ["farm-reference"] = (["--workload", "--aiu-per-job"], []),
        ["budget"] = (["--usd"], ["--reserve-usd"]),
        ["quota"] = ([], ["--window-hours", "--items"]),
        ["track"] = (["--repository", "--forge"], []),
        ["resolve"] = ([], ["--gh", "--glab"]),
        ["report"] = ([], []),
        ["mark"] = (["--session", "--item"], []),
        ["gap"] = (["--session", "--item", "--kind", "--fact", "--belongs-in"], []),
        ["review"] = (["--session", "--item", "--round", "--findings"], []),
        ["requirements"] = (["--item", "--count"], []),
        ["operator"] = (["--tier", "--minutes", "--activity"], ["--item", "--note"]),
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
            if (options.Command != "migrate")
            {
                database.EnsureMigrated();
            }

            var exit = ExitCodes.Ok;
            JsonNode result = options.Command switch
            {
                "migrate" => database.Migrate(options.Values.ContainsKey("--create-database")),
                "ingest" => Ingest.Run(database, options.Get("--sessions")!),
                "ingest-farm" => FarmIngest.Run(database, options.Get("--export")!),
                "resolve" => Forge.Resolve(database, options.Get("--gh") ?? "gh", options.Get("--glab") ?? "glab"),
                "report" => database.Report(),
                "quota" => Quota.Run(database, options, out exit),
                _ => database.Record(options),
            };
            stdout.WriteLine(result.ToJsonString());
            return exit;
        }
        catch (KpiException error)
        {
            stderr.WriteLine($"projectbuddy-kpi: {error.Message}");
            if (error.ExitCode == ExitCodes.Usage)
            {
                stderr.WriteLine(Usage);
            }

            return error.ExitCode;
        }
        catch (NpgsqlException error)
        {
            stderr.WriteLine($"projectbuddy-kpi: database: {error.Message}");
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

            if (name == "--create-database")
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

        if (args[0] == "ingest-farm")
        {
            values["--export"] = Path.GetFullPath(values["--export"]);
        }

        if (args[0] == "ingest")
        {
            values["--sessions"] = Path.GetFullPath(values.GetValueOrDefault("--sessions")
                ?? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".copilot", "session-state"));
        }

        var parsed = new Options(args[0], values.GetValueOrDefault("--host", "127.0.0.1"), number, user, password, database, values);
        foreach (var name in new[] { "--usd", "--reserve-usd", "--window-hours" }.Where(values.ContainsKey))
        {
            Number(parsed, name);
        }

        if (parsed.Get("--items") is { } items)
        {
            Quota.Items(items);
        }

        return parsed;
    }

    internal static int Integer(Options options, string name) =>
        int.TryParse(options.Get(name), NumberStyles.None, CultureInfo.InvariantCulture, out var value) ? value : throw UsageError($"{name} must be a whole number");

    internal static decimal Number(Options options, string name) =>
        decimal.TryParse(options.Get(name), NumberStyles.AllowDecimalPoint, CultureInfo.InvariantCulture, out var value) ? value : throw UsageError($"{name} must be a number");

    private static KpiException UsageError(string message) => new(ExitCodes.Usage, message);

    [GeneratedRegex("^[a-z_][a-z0-9_]{0,62}$")]
    private static partial Regex Identifier();
}
