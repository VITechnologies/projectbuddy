using System.Text.Json.Nodes;
using Npgsql;

namespace ProjectBuddy.Kpi.Tests;

// A real PostgreSQL server for the tests, from PBK_TEST_* variables (CI starts one as a service). Each test gets its own
// database, dropped afterwards.
internal sealed class Server : IDisposable
{
    private readonly List<string> created = [];

    public string Host { get; } = Environment.GetEnvironmentVariable("PBK_TEST_HOST") ?? "127.0.0.1";

    public string Port { get; } = Environment.GetEnvironmentVariable("PBK_TEST_PORT") ?? "5432";

    public string User { get; } = Environment.GetEnvironmentVariable("PBK_TEST_USER") ?? "postgres";

    public string PasswordFile { get; } = Environment.GetEnvironmentVariable("PBK_TEST_PASSWORD_FILE")
        ?? throw new InvalidOperationException("set PBK_TEST_PASSWORD_FILE (and PBK_TEST_HOST/PORT/USER) to a PostgreSQL server for the tests");

    public string[] NewDatabase()
    {
        var name = $"kpi_test_{Guid.NewGuid():N}"[..30];
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

    public static JsonObject Json(params string[] args)
    {
        var (exit, output, error) = Run(args);
        return exit == 0 ? JsonNode.Parse(output)!.AsObject() : throw new InvalidOperationException($"exit {exit}: {error}");
    }
}

// Builds events.jsonl lines in the agent's format: {"type", "data", "id", "timestamp", "parentId"}.
internal sealed class Log
{
    public static readonly DateTimeOffset Start = new(2026, 11, 16, 10, 0, 0, TimeSpan.Zero);

    private readonly List<string> lines = [];

    public static string Time(double minutes) => Start.AddMinutes(minutes).ToString("yyyy-MM-dd'T'HH:mm:ss.fff'Z'");

    public Log Event(string type, double minutes, JsonObject data, Guid? id = null)
    {
        lines.Add(new JsonObject
        {
            ["type"] = type,
            ["data"] = data,
            ["id"] = (id ?? Guid.NewGuid()).ToString(),
            ["timestamp"] = Time(minutes),
            ["parentId"] = Guid.NewGuid().ToString(),
        }.ToJsonString());
        return this;
    }

    // A usage record; aiu writes the exact billed AIU that agent sessions record per call.
    public Log Call(double minutes, decimal cost, JsonNode? toolCounts, string initiator = "agent", string model = "claude-opus-5.5", decimal? aiu = null) =>
        Event("session.usage_record", minutes, new JsonObject
        {
            ["usage"] = new JsonObject
            {
                ["initiator"] = initiator, ["model"] = model, ["cost"] = cost, ["inputTokens"] = 100, ["outputTokens"] = 10,
                ["cacheReadTokens"] = 90, ["toolCounts"] = toolCounts, ["copilotUsage"] = aiu is null ? null : Total(aiu),
            },
        });

    // A model call's success event, which carries the premium-interactions quota snapshot.
    public Log Quota(double minutes, decimal overage, string reset = "2026-12-01T00:00:00Z") =>
        Event("model.model_call_success", minutes, new JsonObject
        {
            ["model"] = "claude-opus-5.5",
            ["quotaSnapshots"] = new JsonObject
            {
                ["premium_interactions"] = new JsonObject
                {
                    ["entitlementRequests"] = 200, ["usedRequests"] = 200, ["overage"] = overage, ["resetDate"] = reset, ["remainingPercentage"] = 0,
                },
            },
        });

    public Log Tool(string id, string tool, double minutes, string? command = null) =>
        Event("tool.execution_start", minutes, new JsonObject
        {
            ["toolCallId"] = id, ["toolName"] = tool, ["arguments"] = command is null ? new JsonObject() : new JsonObject { ["command"] = command },
        });

    public Log Done(string id, double minutes, bool success = true, string content = "ok") =>
        Event("tool.execution_complete", minutes, new JsonObject { ["toolCallId"] = id, ["success"] = success, ["result"] = new JsonObject { ["content"] = content } });

    // A cumulative agent-session checkpoint, or a CLI process's shutdown total; null writes one without a total.
    public Log Checkpoint(double minutes, decimal? aiu) => Event("session.usage_checkpoint", minutes, Total(aiu));

    public Log Shutdown(double minutes, decimal? aiu) => Event("session.shutdown", minutes, Total(aiu));

    public Log Raw(string line)
    {
        lines.Add(line);
        return this;
    }

    public string Write(string root, string session)
    {
        var directory = Directory.CreateDirectory(Path.Combine(root, session)).FullName;
        File.AppendAllLines(Path.Combine(directory, "events.jsonl"), lines);
        lines.Clear();
        return directory;
    }

    public TextReader Reader() => new StringReader(string.Join('\n', lines));

    private static JsonObject Total(decimal? aiu) => aiu is { } value ? new JsonObject { ["totalNanoAiu"] = value * 1_000_000_000m } : [];
}
