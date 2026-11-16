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
