using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;
using Npgsql;
using NpgsqlTypes;

namespace ProjectBuddy.Kpi;

// The local PostgreSQL database: embedded migrations, the facts ingested from event logs, the records the agent writes,
// and the KPI report. All KPI logic lives in the kpi_v1 views and functions.
internal sealed class Database(Options options)
{
    public const string MigrationTable = "public.kpi_schema_migrations";

    private readonly string password = ReadPassword(options.PasswordFile);

    public static IReadOnlyList<(int Version, string Name, string Sql)> Migrations()
    {
        var assembly = typeof(Database).Assembly;
        const string prefix = "ProjectBuddy.Kpi.Sql.";
        return [.. assembly.GetManifestResourceNames()
            .Where(name => name.StartsWith(prefix, StringComparison.Ordinal))
            .Order(StringComparer.Ordinal)
            .Select(name =>
            {
                using var stream = assembly.GetManifestResourceStream(name)!;
                using var reader = new StreamReader(stream, Encoding.UTF8);
                var file = name[prefix.Length..];
                return (int.Parse(file[..4]), file, reader.ReadToEnd());
            })];
    }

    public JsonObject Migrate(bool createDatabase)
    {
        var created = false;
        if (createDatabase)
        {
            using var server = Open("postgres");
            Execute(server, "SELECT pg_advisory_lock(hashtext('kpi_create_database'))");
            using var exists = new NpgsqlCommand("SELECT 1 FROM pg_database WHERE datname = $1", server) { Parameters = { new() { Value = options.Database } } };
            if (exists.ExecuteScalar() is null)
            {
                // The name is a validated identifier ([a-z_][a-z0-9_]*), so quoting it is safe.
                Execute(server, $"CREATE DATABASE \"{options.Database}\"");
                created = true;
            }
        }

        using var connection = Open(options.Database);
        Execute(connection, "SELECT pg_advisory_lock(hashtext('kpi_schema_migrations'))");
        Execute(connection, $"CREATE TABLE IF NOT EXISTS {MigrationTable} (version integer PRIMARY KEY, name text NOT NULL, sha256 text NOT NULL, applied_at timestamptz NOT NULL DEFAULT clock_timestamp())");
        var applied = new JsonArray();
        var present = new JsonArray();
        foreach (var (version, name, sql) in Migrations())
        {
            var hash = Convert.ToHexStringLower(SHA256.HashData(Encoding.UTF8.GetBytes(sql)));
            using var transaction = connection.BeginTransaction();
            using var recorded = new NpgsqlCommand($"SELECT sha256 FROM {MigrationTable} WHERE version = $1", connection, transaction) { Parameters = { new() { Value = version } } };
            if (recorded.ExecuteScalar() is string known)
            {
                if (known != hash)
                {
                    throw new KpiException(ExitCodes.Refused, $"migration {name} changed after it was applied");
                }

                present.Add((JsonNode)name);
                continue;
            }

            using (var migrate = new NpgsqlCommand(sql, connection, transaction))
            {
                migrate.ExecuteNonQuery();
            }

            using (var record = new NpgsqlCommand($"INSERT INTO {MigrationTable} (version, name, sha256) VALUES ($1, $2, $3)", connection, transaction)
            {
                Parameters = { new() { Value = version }, new() { Value = name }, new() { Value = hash } },
            })
            {
                record.ExecuteNonQuery();
            }

            transaction.Commit();
            applied.Add((JsonNode)name);
        }

        return new JsonObject { ["database"] = options.Database, ["created"] = created, ["applied"] = applied, ["present"] = present };
    }

    // Every command but migrate needs the schema this tool embeds; an older database is refused rather than misread.
    public void EnsureMigrated()
    {
        using var connection = Open(options.Database);
        using var exists = new NpgsqlCommand($"SELECT to_regclass('{MigrationTable}') IS NOT NULL", connection);
        var applied = 0L;
        if ((bool)exists.ExecuteScalar()!)
        {
            using var count = new NpgsqlCommand($"SELECT count(*) FROM {MigrationTable}", connection);
            applied = (long)count.ExecuteScalar()!;
        }

        var embedded = Migrations().Count;
        if (applied < embedded)
        {
            throw new KpiException(ExitCodes.Refused, $"the database has {applied} of {embedded} migrations; run migrate");
        }
    }

    public decimal Setting(string key)
    {
        using var connection = Open(options.Database);
        using var command = new NpgsqlCommand("SELECT kpi_v1.setting($1)", connection) { Parameters = { new() { Value = key } } };
        return (decimal)command.ExecuteScalar()!;
    }

    // Stores one session's facts in one transaction. Facts keyed by event or tool-call ID are inserted once; a tool call
    // completed since the last ingest is updated; the active spans are derived again.
    public JsonObject Store(Ingest.Session session)
    {
        using var connection = Open(options.Database);
        using var transaction = connection.BeginTransaction();
        int Run(string sql, params object[] values)
        {
            using var command = new NpgsqlCommand(sql, connection, transaction);
            foreach (var value in values)
            {
                command.Parameters.Add(value as NpgsqlParameter ?? new() { Value = value });
            }

            return command.ExecuteNonQuery();
        }

        Run("""
            INSERT INTO kpi_v1.sessions (session_id, first_at, last_at, events, aiu) VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (session_id) DO UPDATE SET first_at = EXCLUDED.first_at, last_at = EXCLUDED.last_at, events = EXCLUDED.events, aiu = EXCLUDED.aiu
            """, session.Id, session.First, session.Last, session.Events,
            new NpgsqlParameter { Value = (object?)session.Aiu ?? DBNull.Value, NpgsqlDbType = NpgsqlDbType.Numeric });
        var calls = Run("""
            INSERT INTO kpi_v1.model_calls (event_id, session_id, at, initiator, model, cost, input_tokens, output_tokens, cache_read_tokens, category)
            SELECT c.id, $1, c.at, c.initiator, c.model, c.cost, c.input, c.output, c.cache_read, c.category
            FROM unnest($2::uuid[], $3::timestamptz[], $4::text[], $5::text[], $6::numeric[], $7::bigint[], $8::bigint[], $9::bigint[], $10::text[])
                AS c(id, at, initiator, model, cost, input, output, cache_read, category)
            ON CONFLICT (event_id) DO NOTHING
            """, session.Id, session.Calls.Select(c => c.EventId).ToArray(), session.Calls.Select(c => c.At).ToArray(),
            session.Calls.Select(c => c.Initiator).ToArray(), session.Calls.Select(c => c.Model).ToArray(), session.Calls.Select(c => c.Cost).ToArray(),
            session.Calls.Select(c => c.InputTokens).ToArray(), session.Calls.Select(c => c.OutputTokens).ToArray(),
            session.Calls.Select(c => c.CacheReadTokens).ToArray(), session.Calls.Select(c => c.Category).ToArray());

        // A call's AIU share is set once a later checkpoint measures its interval.
        Run("""
            UPDATE kpi_v1.model_calls AS c SET aiu = m.aiu FROM unnest($1::uuid[], $2::numeric[]) AS m(id, aiu)
            WHERE c.event_id = m.id AND c.aiu IS DISTINCT FROM m.aiu
            """, session.Calls.Select(c => c.EventId).ToArray(), session.Calls.Select(c => c.Aiu).ToArray());
        Run("""
            INSERT INTO kpi_v1.tool_calls (session_id, tool_call_id, tool, started_at, completed_at, success)
            SELECT $1, t.id, t.tool, t.started, t.completed, t.success
            FROM unnest($2::text[], $3::text[], $4::timestamptz[], $5::timestamptz[], $6::boolean[]) AS t(id, tool, started, completed, success)
            ON CONFLICT (session_id, tool_call_id) DO UPDATE SET completed_at = EXCLUDED.completed_at, success = EXCLUDED.success
            WHERE kpi_v1.tool_calls.completed_at IS NULL
            """, session.Id, session.Tools.Select(t => t.Id).ToArray(), session.Tools.Select(t => t.Tool).ToArray(),
            session.Tools.Select(t => t.Started).ToArray(), session.Tools.Select(t => t.Completed).ToArray(), session.Tools.Select(t => t.Success).ToArray());
        Run("""
            INSERT INTO kpi_v1.operator_messages (event_id, session_id, at)
            SELECT m.id, $1, m.at FROM unnest($2::uuid[], $3::timestamptz[]) AS m(id, at) ON CONFLICT DO NOTHING
            """, session.Id, session.Messages.Select(m => m.Id).ToArray(), session.Messages.Select(m => m.At).ToArray());
        Run("DELETE FROM kpi_v1.active_spans WHERE session_id = $1", session.Id);
        Run("""
            INSERT INTO kpi_v1.active_spans (session_id, span)
            SELECT $1, tstzrange(s.started, s.ended, '[]') FROM unnest($2::timestamptz[], $3::timestamptz[]) AS s(started, ended)
            """, session.Id, session.Spans.Select(s => s.Start).ToArray(), session.Spans.Select(s => s.End).ToArray());
        transaction.Commit();
        return new JsonObject
        {
            ["session"] = session.Id,
            ["events"] = session.Events,
            ["newModelCalls"] = calls,
            ["aiu"] = session.Aiu,
            ["activeSpans"] = session.Spans.Count,
        };
    }

    // The earliest session's start: merges before it are no session's.
    public DateTimeOffset? FirstSession()
    {
        using var connection = Open(options.Database);
        using var command = new NpgsqlCommand("SELECT min(first_at) FROM kpi_v1.sessions", connection);
        return command.ExecuteScalar() is DateTime first ? new DateTimeOffset(first, TimeSpan.Zero) : null;
    }

    public List<(string Repository, string Forge)> Repositories()
    {
        using var connection = Open(options.Database);
        using var command = new NpgsqlCommand("SELECT repository, forge FROM kpi_v1.repositories ORDER BY repository", connection);
        using var reader = command.ExecuteReader();
        var repositories = new List<(string, string)>();
        while (reader.Read())
        {
            repositories.Add((reader.GetString(0), reader.GetString(1)));
        }

        return repositories;
    }

    public void RecordMerges(IReadOnlyList<(string Change, DateTimeOffset MergedAt)> merges)
    {
        using var connection = Open(options.Database);
        using var command = new NpgsqlCommand("""
            INSERT INTO kpi_v1.forge_changes (change_ref, merged_at) SELECT m.ref, m.at FROM unnest($1::text[], $2::timestamptz[]) AS m(ref, at)
            ON CONFLICT (change_ref) DO UPDATE SET merged_at = EXCLUDED.merged_at, checked_at = clock_timestamp()
            """, connection)
        {
            Parameters = { new() { Value = merges.Select(m => m.Change).ToArray() }, new() { Value = merges.Select(m => m.MergedAt).ToArray() } },
        };
        command.ExecuteNonQuery();
    }

    public JsonObject Record(Options record)
    {
        var item = (object?)record.Get("--item") ?? DBNull.Value;
        var (sql, values) = record.Command switch
        {
            "mark" => ("INSERT INTO kpi_v1.work_markers (session_id, item) VALUES ($1, $2) RETURNING id, at", new object[] { record.Get("--session")!, item }),
            "gap" => ("INSERT INTO kpi_v1.spec_gaps (session_id, item, kind, fact, belongs_in) VALUES ($1, $2, $3, $4, $5) RETURNING id, at",
                [record.Get("--session")!, item, record.Get("--kind")!, record.Get("--fact")!, record.Get("--belongs-in")!]),
            "review" => ("INSERT INTO kpi_v1.reviews (session_id, item, round, findings) VALUES ($1, $2, $3, $4) RETURNING id, at",
                [record.Get("--session")!, item, Program.Integer(record, "--round"), Program.Integer(record, "--findings")]),
            "track" => ("""
                INSERT INTO kpi_v1.repositories (repository, forge) VALUES ($1, $2)
                ON CONFLICT (repository) DO UPDATE SET forge = EXCLUDED.forge RETURNING 0::bigint AS id, clock_timestamp() AS at
                """, [record.Get("--repository")!, record.Get("--forge")!]),
            "requirements" => ("""
                INSERT INTO kpi_v1.requirement_counts (item, requirements) VALUES ($1, $2)
                ON CONFLICT (item) DO UPDATE SET requirements = EXCLUDED.requirements RETURNING 0::bigint AS id, clock_timestamp() AS at
                """, [item, Program.Integer(record, "--count")]),
            _ => ("INSERT INTO kpi_v1.operator_entries (tier, minutes, activity, item, note) VALUES ($1, $2, $3, $4, $5) RETURNING id, at",
                [record.Get("--tier")!, Program.Number(record, "--minutes"), record.Get("--activity")!, item, (object?)record.Get("--note") ?? DBNull.Value]),
        };
        using var connection = Open(options.Database);
        using var command = new NpgsqlCommand(sql, connection);
        foreach (var value in values)
        {
            command.Parameters.Add(value is DBNull ? new NpgsqlParameter { Value = value, NpgsqlDbType = NpgsqlDbType.Text } : new NpgsqlParameter { Value = value });
        }

        using var reader = command.ExecuteReader();
        reader.Read();
        return new JsonObject { ["recorded"] = record.Command, ["id"] = reader.GetInt64(0), ["at"] = reader.GetFieldValue<DateTimeOffset>(1) };
    }

    public JsonObject Report()
    {
        using var connection = Open(options.Database);
        using var command = new NpgsqlCommand("SELECT kpi_v1.report()::text", connection);
        return JsonNode.Parse((string)command.ExecuteScalar()!)!.AsObject();
    }

    internal static string ReadPassword(string path)
    {
        string text;
        try
        {
            text = File.ReadAllText(path).TrimEnd('\r', '\n');
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException)
        {
            throw new KpiException(ExitCodes.Refused, $"cannot read the password file: {error.GetType().Name}");
        }

        return text.Length > 0 ? text : throw new KpiException(ExitCodes.Refused, "the password file is empty");
    }

    private static void Execute(NpgsqlConnection connection, string sql)
    {
        using var command = new NpgsqlCommand(sql, connection);
        command.ExecuteNonQuery();
    }

    private NpgsqlConnection Open(string database)
    {
        var connection = new NpgsqlConnection(new NpgsqlConnectionStringBuilder
        {
            Host = options.Host,
            Port = options.Port,
            Username = options.User,
            Password = password,
            Database = database,
            Timeout = 10,
            CommandTimeout = 600,
            Pooling = false,
        }.ConnectionString);
        connection.Open();
        return connection;
    }
}
