using System.Diagnostics;
using System.Text;
using Npgsql;

namespace ProjectBuddy.Wiki.Tests;

// A real PostgreSQL server for the tests, from PBW_TEST_* variables (CI starts one as a service). Each test gets its own
// database, dropped afterwards. Missing settings fail the tests rather than skipping them.
internal sealed class Server : IDisposable
{
    private readonly List<string> created = [];

    public string Host { get; } = Environment.GetEnvironmentVariable("PBW_TEST_HOST") ?? "127.0.0.1";

    public string Port { get; } = Environment.GetEnvironmentVariable("PBW_TEST_PORT") ?? "5432";

    public string User { get; } = Environment.GetEnvironmentVariable("PBW_TEST_USER") ?? "postgres";

    public string PasswordFile { get; } = Environment.GetEnvironmentVariable("PBW_TEST_PASSWORD_FILE")
        ?? throw new InvalidOperationException("set PBW_TEST_PASSWORD_FILE (and PBW_TEST_HOST/PORT/USER) to a PostgreSQL server for the tests");

    // Connection arguments for a new, not yet created database.
    public string[] NewDatabase()
    {
        var name = $"wiki_test_{Guid.NewGuid():N}"[..30];
        created.Add(name);
        return ["--host", Host, "--port", Port, "--user", User, "--password-file", PasswordFile, "--database", name];
    }

    public string[] Migrated()
    {
        var connection = NewDatabase();
        var (exit, _, error) = Cli.Run(["migrate", "--create-database", .. connection]);
        return exit == 0 ? connection : throw new InvalidOperationException(error);
    }

    public void Execute(string[] connection, string sql)
    {
        using var data = Open(connection[^1]);
        using var command = new NpgsqlCommand(sql, data);
        command.ExecuteNonQuery();
    }

    public object? Scalar(string[] connection, string sql)
    {
        using var data = Open(connection[^1]);
        using var command = new NpgsqlCommand(sql, data);
        return command.ExecuteScalar();
    }

    // A database holding a registry_v1 schema with one admitted source that this login may import into. The schema is
    // built from the SQL files PBW_TEST_REGISTRY_MIGRATIONS names: RSR's own db/registry-v1, or, in public CI (RSR is
    // private), the contract stub in RegistryContract/.
    public string NewRegistry(string source, string constellation, string manifest)
    {
        var migrations = Environment.GetEnvironmentVariable("PBW_TEST_REGISTRY_MIGRATIONS")
            ?? throw new InvalidOperationException("set PBW_TEST_REGISTRY_MIGRATIONS to RSR's db/registry-v1 directory");
        var name = NewDatabase()[^1];
        using (var server = Open("postgres"))
        {
            using var create = new NpgsqlCommand($"CREATE DATABASE \"{name}\"", server);
            create.ExecuteNonQuery();
        }

        using var registry = Open(name);
        foreach (var file in Directory.GetFiles(migrations, "*.sql").Order(StringComparer.Ordinal))
        {
            using var transaction = registry.BeginTransaction();
            using (var migrate = new NpgsqlCommand(File.ReadAllText(file), registry, transaction))
            {
                migrate.ExecuteNonQuery();
            }

            transaction.Commit();
        }

        using var admit = new NpgsqlCommand($"""
            INSERT INTO registry_v1.sources (source_uuid, constellation_uuid, canonical_remote) VALUES ('{source}', '{constellation}', 'https://example.invalid/wiki.git');
            INSERT INTO registry_v1.source_authority_manifests (source_uuid, constellation_uuid, manifest_digest, source_role, allowed_refs, allowed_scopes, accepted_by)
            VALUES ('{source}', '{constellation}', '{manifest}', 'canonical', '["master"]', '["system","software","test"]', 'test');
            INSERT INTO registry_v1.importer_authorizations (principal, constellation_uuid, granted_by) VALUES ('{User}', '{constellation}', 'test');
            """, registry);
        admit.ExecuteNonQuery();
        return name;
    }

    public void Dispose()
    {
        using var server = Open("postgres");
        foreach (var name in created)
        {
            using var drop = new NpgsqlCommand($"DROP DATABASE IF EXISTS \"{name}\" WITH (FORCE)", server);
            drop.ExecuteNonQuery();
        }
    }

    private NpgsqlConnection Open(string database)
    {
        var connection = new NpgsqlConnection(new NpgsqlConnectionStringBuilder
        {
            Host = Host,
            Port = int.Parse(Port),
            Username = User,
            Password = File.ReadAllText(PasswordFile).TrimEnd('\r', '\n'),
            Database = database,
            Pooling = false,
            IncludeErrorDetail = true,
        }.ConnectionString);
        connection.Open();
        return connection;
    }
}

internal static class Cli
{
    public static (int Exit, string Output, string Error) Run(params string[] args)
    {
        var (output, error) = (new StringWriter(), new StringWriter());
        var exit = Program.Run(args, output, error);
        return (exit, output.ToString(), error.ToString());
    }
}

// Governed wiki pages in the canonical layout.
internal static class Pages
{
    public static string Requirement(string id, string title, string testIds, string codeRef = "None", bool withStatus = true) =>
        $"# {id}: {title}\n\n## Requirement\n\nThe system shall work.\n\n## Rationale\n\nBecause.\n\n## Fit Criterion\n\nIt works.\n\n"
        + $"## Verification\n\nPlanned.\n\n## Source\n\n[[Specification]]\n\n## Priority\n\nHigh\n\n## TestID\n\n{testIds}\n\n"
        + $"## TestArtifact\n\nReview\n\n## CodeRef\n\n{codeRef}" + (withStatus ? "\n\n## Status\n\nPlanned\n" : "\n");

    public static string Marker(string id, string kind, string body) => $"# {id}\n\n<!-- page-kind: {kind} -->\n{body}\n";

    // Two requirements, a retired one, a defined test and a stub: every page kind and edge type.
    public static Repo WriteValidWiki(Repo repo) => repo
        .Write("Home.md", "# Home\n")
        .Write("system-requirements/SYS-REQ-001.md", Requirement("SYS-REQ-001", "Reusable engine", "[[TEST-001]]", "[[REQ-001]], [[REQ-002|the old one]]"))
        .Write("software-requirements/REQ-001.md", Requirement("REQ-001", "No pinned config: none", "[[TEST-001]], [[TEST-002#case]] and again [[TEST-001]]"))
        .Write("software-requirements/REQ-002.md", Marker("REQ-002", "retired", "Merged into [[REQ-001]]; see [[Software-Requirements-Specification]]."))
        .Write("tests/TEST-001.md", "# TEST-001\n\n## Stimulus\n\nRun it.\n## not a heading\n\n## Required observation\n\n\n")
        .Write("tests/TEST-002.md", Marker("TEST-002", "stub", "No definition yet."));
}

// A throwaway git repository standing in for a wiki clone.
internal sealed class Repo : IDisposable
{
    public Repo()
    {
        Path = Directory.CreateTempSubdirectory("pbw-repo-").FullName;
        Git("init", "-q", "-b", "main");
    }

    public string Path { get; }

    public static string Exec(string directory, params string[] args)
    {
        var start = new ProcessStartInfo("/usr/bin/git") { RedirectStandardOutput = true, RedirectStandardError = true, UseShellExecute = false };
        foreach (var arg in (string[])["-C", directory, "-c", "user.name=test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false", .. args])
        {
            start.ArgumentList.Add(arg);
        }

        using var process = Process.Start(start)!;
        var output = process.StandardOutput.ReadToEnd();
        var error = process.StandardError.ReadToEnd();
        process.WaitForExit();
        return process.ExitCode == 0 ? output.Trim() : throw new InvalidOperationException($"git {string.Join(' ', args)}: {error}");
    }

    public string Git(params string[] args) => Exec(Path, args);

    public Repo Write(string path, string text) => Write(path, Encoding.UTF8.GetBytes(text));

    public Repo Write(string path, byte[] bytes)
    {
        var full = System.IO.Path.Combine(Path, path);
        Directory.CreateDirectory(System.IO.Path.GetDirectoryName(full)!);
        File.WriteAllBytes(full, bytes);
        return this;
    }

    public Repo Remove(string path)
    {
        File.Delete(System.IO.Path.Combine(Path, path));
        return this;
    }

    public string Commit(string message = "change")
    {
        Git("add", "-A");
        Git("commit", "-q", "--allow-empty", "-m", message);
        return Git("rev-parse", "HEAD");
    }

    public void Dispose() => Directory.Delete(Path, recursive: true);
}
