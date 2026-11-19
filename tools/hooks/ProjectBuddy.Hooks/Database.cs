using System.Text.Json.Nodes;
using Npgsql;

namespace ProjectBuddy.Hooks;

internal sealed class Database(Options options)
{
    public NpgsqlConnection Open()
    {
        var password = File.ReadAllText(options.PasswordFile).TrimEnd('\r', '\n');
        var connection = new NpgsqlConnection(new NpgsqlConnectionStringBuilder
        {
            Host = options.Host,
            Port = options.Port,
            Username = options.User,
            Password = password,
            Database = options.Database,
            Pooling = false,
        }.ConnectionString);
        connection.Open();
        return connection;
    }

    public JsonNode Sessions(string? status)
    {
        using var connection = Open();
        using var command = new NpgsqlCommand(
            "SELECT s.session_id, s.title, s.workspace, s.work_ref, s.parent_session_id, s.status, s.started_at, s.last_seen_at, "
            + "(SELECT count(*) FROM hooks.decisions d WHERE d.session_id = s.session_id AND d.status = 'open') "
            + "FROM hooks.sessions s WHERE $1::text IS NULL OR s.status = $1 ORDER BY s.last_seen_at DESC, s.session_id", connection)
        { Parameters = { new() { Value = (object?)status ?? DBNull.Value, NpgsqlDbType = NpgsqlTypes.NpgsqlDbType.Text } } };
        using var reader = command.ExecuteReader();
        var rows = new JsonArray();
        while (reader.Read())
        {
            rows.Add(new JsonObject
            {
                ["session_id"] = reader.GetString(0),
                ["title"] = Text(reader, 1),
                ["workspace"] = Text(reader, 2),
                ["work_ref"] = Text(reader, 3),
                ["parent_session_id"] = Text(reader, 4),
                ["status"] = reader.GetString(5),
                ["started_at"] = reader.GetDateTime(6).ToString("O"),
                ["last_seen_at"] = reader.GetDateTime(7).ToString("O"),
                ["open_decisions"] = reader.GetInt64(8),
            });
        }

        return new JsonObject { ["sessions"] = rows };
    }

    // A work_ref that is set or changed marks the session's calls from then on (#391). The first one is backdated to the
    // session's start: a worker session is one item by design, and its calls before the orchestrator set the ref belong to it.
    public JsonNode SetSession(Options set)
    {
        using var connection = Open();
        using var transaction = connection.BeginTransaction();
        using var read = new NpgsqlCommand("SELECT work_ref FROM hooks.sessions WHERE session_id = $1 FOR UPDATE", connection, transaction)
        { Parameters = { new() { Value = set.Get("--session")! } } };
        var found = read.ExecuteScalar();
        var previous = found as string;
        using var command = new NpgsqlCommand("""
            WITH upserted AS (
                INSERT INTO hooks.sessions AS s (session_id, title, work_ref, parent_session_id) VALUES ($1, $2, $3, $4)
                ON CONFLICT (session_id) DO UPDATE SET title = coalesce(EXCLUDED.title, s.title), work_ref = coalesce(EXCLUDED.work_ref, s.work_ref),
                    parent_session_id = coalesce(EXCLUDED.parent_session_id, s.parent_session_id)
                RETURNING s.work_ref, s.started_at)
            INSERT INTO kpi_v1.work_markers (session_id, at, item)
            SELECT $1, CASE WHEN $5 THEN clock_timestamp() ELSE u.started_at END, u.work_ref
            FROM upserted AS u
            WHERE u.work_ref IS NOT NULL AND u.work_ref IS DISTINCT FROM $6
            RETURNING id
            """, connection, transaction)
        {
            Parameters =
            {
                new() { Value = set.Get("--session")! },
                new() { Value = (object?)set.Get("--title") ?? DBNull.Value, NpgsqlDbType = NpgsqlTypes.NpgsqlDbType.Text },
                new() { Value = (object?)set.Get("--work-ref") ?? DBNull.Value, NpgsqlDbType = NpgsqlTypes.NpgsqlDbType.Text },
                new() { Value = (object?)set.Get("--parent") ?? DBNull.Value, NpgsqlDbType = NpgsqlTypes.NpgsqlDbType.Text },
                new() { Value = previous is not null },
                new() { Value = (object?)previous ?? DBNull.Value, NpgsqlDbType = NpgsqlTypes.NpgsqlDbType.Text },
            },
        };
        var marked = command.ExecuteScalar() is not null;
        transaction.Commit();
        return new JsonObject { ["session_id"] = set.Get("--session"), ["set"] = true, ["marked"] = marked };
    }

    public JsonNode Decisions(bool all)
    {
        using var connection = Open();
        using var command = new NpgsqlCommand(
            "SELECT id, session_id, work_ref, message, status, answer, answered_by, created_at FROM hooks.decisions "
            + "WHERE $1 OR status = 'open' ORDER BY id", connection) { Parameters = { new() { Value = all } } };
        using var reader = command.ExecuteReader();
        var rows = new JsonArray();
        while (reader.Read())
        {
            rows.Add(new JsonObject
            {
                ["id"] = reader.GetInt64(0),
                ["session_id"] = reader.GetString(1),
                ["work_ref"] = Text(reader, 2),
                ["message"] = reader.GetString(3),
                ["status"] = reader.GetString(4),
                ["answer"] = Text(reader, 5),
                ["answered_by"] = Text(reader, 6),
                ["created_at"] = reader.GetDateTime(7).ToString("O"),
            });
        }

        return new JsonObject { ["decisions"] = rows };
    }

    public JsonNode Answer(int id, string text)
    {
        using var connection = Open();
        using var command = new NpgsqlCommand(
            "UPDATE hooks.decisions SET status = 'answered', answer = $2, answered_by = 'cli', answered_at = clock_timestamp() WHERE id = $1 AND status = 'open'", connection)
        { Parameters = { new() { Value = (long)id }, new() { Value = text } } };
        return command.ExecuteNonQuery() == 1
            ? new JsonObject { ["id"] = id, ["answered"] = true }
            : throw new HooksException(ExitCodes.Refused, $"decision {id} does not exist or is already answered");
    }

    private static string? Text(NpgsqlDataReader reader, int column) => reader.IsDBNull(column) ? null : reader.GetString(column);
}
