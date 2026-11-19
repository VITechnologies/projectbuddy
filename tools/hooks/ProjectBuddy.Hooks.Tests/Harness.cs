using System.Reflection;
using System.Text.Json.Nodes;
using Npgsql;

namespace ProjectBuddy.Hooks.Tests;

// A real PostgreSQL server for the tests, from PBK_TEST_* variables (CI starts one as a service). Each test gets its own
// database with the hooks migration applied, dropped afterwards.
internal sealed class Server : IDisposable
{
    private readonly List<string> created = [];

    public string Host { get; } = Environment.GetEnvironmentVariable("PBK_TEST_HOST") ?? "127.0.0.1";

    public string Port { get; } = Environment.GetEnvironmentVariable("PBK_TEST_PORT") ?? "5432";

    public string User { get; } = Environment.GetEnvironmentVariable("PBK_TEST_USER") ?? "postgres";

    public string PasswordFile { get; } = Environment.GetEnvironmentVariable("PBK_TEST_PASSWORD_FILE")
        ?? throw new InvalidOperationException("set PBK_TEST_PASSWORD_FILE (and PBK_TEST_HOST/PORT/USER) to a PostgreSQL server for the tests");

    public string[] Migrated()
    {
        var name = $"hooks_test_{Guid.NewGuid():N}"[..30];
        created.Add(name);
        using (var server = Open("postgres"))
        {
            using var create = new NpgsqlCommand($"CREATE DATABASE \"{name}\"", server);
            create.ExecuteNonQuery();
        }

        using var stream = Assembly.GetExecutingAssembly().GetManifestResourceStream("0006_hooks.sql")!;
        using var data = Open(name);
        using var migrate = new NpgsqlCommand(new StreamReader(stream).ReadToEnd(), data);
        migrate.ExecuteNonQuery();
        return ["--host", Host, "--port", Port, "--user", User, "--password-file", PasswordFile, "--database", name];
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

    public NpgsqlConnection OpenTo(string[] connection) => Open(connection[^1]);

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

    public static JsonObject Json(params string[] args)
    {
        var (exit, output, error) = Run(args);
        return exit == 0 ? JsonNode.Parse(output)!.AsObject() : throw new InvalidOperationException($"exit {exit}: {error}");
    }
}

// A stand-in for `gh issue view|edit` on one issue: updatedAt moves on by a second at every edit, as GitHub's does.
internal sealed class FakeGh : IDisposable
{
    private readonly string directory = Directory.CreateTempSubdirectory("pbh-gh-").FullName;

    public FakeGh(string body = "first body", int editMilliseconds = 0, int exitCode = 0)
    {
        Path = System.IO.Path.Combine(directory, "gh");
        File.WriteAllText(System.IO.Path.Combine(directory, "body"), body);
        File.WriteAllText(System.IO.Path.Combine(directory, "n"), "0");
        File.WriteAllText(Path, $$"""
            #!/usr/bin/env bash
            d='{{directory}}'
            [ {{exitCode}} -eq 0 ] || { echo "boom" >&2; exit {{exitCode}}; }
            case "$2" in
              view)
                n=$(cat "$d/n")
                printf '{"updatedAt":"%s","body":%s}\n' "$(date -u -d "2026-01-01T00:00:00Z + $n seconds" +%Y-%m-%dT%H:%M:%SZ)" "$(python3 -c 'import json,sys;print(json.dumps(open(sys.argv[1]).read()))' "$d/body")"
                ;;
              edit)
                while [ $# -gt 0 ]; do [ "$1" = --body-file ] && f="$2"; shift; done
                sleep {{editMilliseconds / 1000.0:0.0}}
                cp "$f" "$d/body"
                echo $(( $(cat "$d/n") + 1 )) > "$d/n"
                ;;
            esac
            """.Replace("\r", ""));
        if (OperatingSystem.IsWindows())
        {
            throw new PlatformNotSupportedException("the fake gh is a bash script");
        }

        File.SetUnixFileMode(Path, UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute);
    }

    public string Path { get; }

    public string Body => File.ReadAllText(System.IO.Path.Combine(directory, "body"));

    public string UpdatedAt(int n) => new DateTimeOffset(2026, 1, 1, 0, 0, 0, TimeSpan.Zero).AddSeconds(n).ToString("O");

    public void Dispose() => Directory.Delete(directory, true);
}
