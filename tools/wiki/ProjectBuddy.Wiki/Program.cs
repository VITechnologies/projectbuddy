using System.Diagnostics.CodeAnalysis;
using System.Net;
using System.Text.Encodings.Web;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using Npgsql;

namespace ProjectBuddy.Wiki;

public static class ExitCodes
{
    public const int Ok = 0;
    public const int Findings = 1;
    public const int Usage = 2;
    public const int Refused = 3;
    public const int Database = 5;
}

public sealed class WikiException(int exitCode, string message) : Exception(message)
{
    public int ExitCode { get; } = exitCode;
}

public sealed record Options(
    string Command,
    string Host,
    int Port,
    string User,
    string PasswordFile,
    string Database,
    string? Wiki,
    string? Out,
    string? Commit,
    string Git,
    bool CreateDatabase,
    string? Approval = null,
    string? ImporterPasswordFile = null,
    string Importer = Program.DefaultImporter,
    string RegistryDatabase = Program.DefaultRegistryDatabase);

// projectbuddy-wiki (#307, #308): a local, append-only copy of a GitHub wiki in PostgreSQL that seeds itself from the
// wiki's committed bytes and renders the wiki back byte for byte.
public static partial class Program
{
    public const string Usage =
        "usage: projectbuddy-wiki migrate [--create-database] | seed --wiki <clone> | render --out <dir> [--commit <sha>] | check --wiki <clone>"
        + " | project [--commit <sha>]"
        + " | export-registry --approval <file> --importer-password-file <path> [--importer <name>] [--registry-database <name>] [--commit <sha>]"
        + " --password-file <path> [--host <host>] [--port <n>] [--user <name>] [--database <name>] [--git <path>]";

    public const string DefaultGit = "/usr/bin/git";

    public const string DefaultImporter = "registry_importer_local";

    public const string DefaultRegistryDatabase = "registry_local";

    internal static readonly JsonSerializerOptions Output = new() { Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping };

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

            var git = new Git(options.Git);
            // Findings are what check and project report rather than refuse: drift, or pages that cannot be exported.
            var (result, findings) = options.Command switch
            {
                "migrate" => (database.Migrate(options.CreateDatabase), false),
                "seed" => (Commands.Seed(database, git, options.Wiki!), false),
                "render" => (Commands.Render(database, git, options.Out!, options.Commit), false),
                "project" => Commands.Project(database, options.Commit),
                "export-registry" => (Commands.ExportRegistry(database, new Registry(options), Approval.Read(options.Approval!), options.Commit), false),
                _ => Commands.Check(database, git, options.Wiki!),
            };
            stdout.WriteLine(result.ToJsonString(Output));
            return findings ? ExitCodes.Findings : ExitCodes.Ok;
        }
        catch (WikiException error)
        {
            stderr.WriteLine($"projectbuddy-wiki: {error.Message}");
            if (error.ExitCode == ExitCodes.Usage)
            {
                stderr.WriteLine(Usage);
            }

            return error.ExitCode;
        }
        catch (NpgsqlException error)
        {
            stderr.WriteLine($"projectbuddy-wiki: database: {error.Message}");
            return ExitCodes.Database;
        }
        catch (IOException error)
        {
            stderr.WriteLine($"projectbuddy-wiki: {error.Message}");
            return ExitCodes.Refused;
        }
        catch (UnauthorizedAccessException error)
        {
            stderr.WriteLine($"projectbuddy-wiki: {error.Message}");
            return ExitCodes.Refused;
        }
    }

    internal static Options Parse(string[] args)
    {
        if (args.Length == 0 || args[0] is not ("migrate" or "seed" or "render" or "check" or "project" or "export-registry"))
        {
            throw UsageError("expected a command: migrate, seed, render, check, project or export-registry");
        }

        var command = args[0];
        string[] allowed = command switch
        {
            "migrate" => ["--create-database"],
            "render" => ["--out", "--commit"],
            "project" => ["--commit"],
            "export-registry" => ["--commit", "--approval", "--importer-password-file", "--importer", "--registry-database"],
            _ => ["--wiki"],
        };
        string[] common = ["--password-file", "--host", "--port", "--user", "--database", "--git"];
        var values = new Dictionary<string, string>(StringComparer.Ordinal);
        for (var index = 1; index < args.Length; index++)
        {
            var name = args[index];
            if (!(allowed.Contains(name) || common.Contains(name)) || values.ContainsKey(name))
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

        var port = values.GetValueOrDefault("--port", "5432");
        var options = new Options(
            command,
            values.GetValueOrDefault("--host", "127.0.0.1"),
            int.TryParse(port, out var number) && number is >= 1 and <= 65535 ? number : throw UsageError("--port must be 1-65535"),
            values.GetValueOrDefault("--user", "postgres"),
            values.TryGetValue("--password-file", out var password) ? Qualified(password, "--password-file") : throw UsageError("--password-file is required"),
            values.GetValueOrDefault("--database", "wiki_local"),
            values.TryGetValue("--wiki", out var wiki) ? Qualified(wiki, "--wiki") : null,
            values.TryGetValue("--out", out var output) ? Qualified(output, "--out") : null,
            values.GetValueOrDefault("--commit"),
            Qualified(values.GetValueOrDefault("--git", DefaultGit), "--git"),
            values.ContainsKey("--create-database"),
            values.TryGetValue("--approval", out var approval) ? Qualified(approval, "--approval") : null,
            values.TryGetValue("--importer-password-file", out var importer) ? Qualified(importer, "--importer-password-file") : null,
            values.GetValueOrDefault("--importer", DefaultImporter),
            values.GetValueOrDefault("--registry-database", DefaultRegistryDatabase));

        if (!HostName().IsMatch(options.Host) && !IPAddress.TryParse(options.Host, out _))
        {
            throw UsageError("--host must be an IP address or a host name");
        }

        if (!new[] { options.User, options.Database, options.Importer, options.RegistryDatabase }.All(Identifier().IsMatch))
        {
            throw UsageError("--user, --database, --importer and --registry-database must be plain identifiers ([a-z_][a-z0-9_]*)");
        }

        if (options.Commit is not null && !Git.IsCommit(options.Commit))
        {
            throw UsageError("--commit must be a full commit hash");
        }

        return command switch
        {
            "seed" or "check" when options.Wiki is null => throw UsageError("--wiki is required"),
            "render" when options.Out is null => throw UsageError("--out is required"),
            "export-registry" when options.Approval is null || options.ImporterPasswordFile is null
                => throw UsageError("--approval and --importer-password-file are required"),
            _ => options,
        };
    }

    private static string Qualified(string path, string option) =>
        Path.IsPathFullyQualified(path) ? Path.TrimEndingDirectorySeparator(path) : throw UsageError($"{option} needs a fully qualified path");

    private static WikiException UsageError(string message) => new(ExitCodes.Usage, message);

    [GeneratedRegex("^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")]
    private static partial Regex HostName();

    [GeneratedRegex("^[a-z_][a-z0-9_]{0,62}$")]
    private static partial Regex Identifier();
}
