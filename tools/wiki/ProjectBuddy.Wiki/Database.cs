using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;
using Npgsql;
using NpgsqlTypes;

namespace ProjectBuddy.Wiki;

public sealed record Snapshot(Guid Id, string Digest, string Commit, int FileCount, long ByteCount);

public sealed record Ingested(Guid Snapshot, string Digest, bool Reused, int NewBlobs);

// The local PostgreSQL database: embedded migrations and the wiki_v1 stored procedures. All logic about states, digests
// and history lives in the procedures; this class only passes parameters and reads rows.
internal sealed class Database(Options options)
{
    public const string MigrationTable = "public.wiki_schema_migrations";

    private readonly string password = ReadPassword(options.PasswordFile);

    public static IReadOnlyList<(int Version, string Name, string Sql)> Migrations()
    {
        var assembly = typeof(Database).Assembly;
        const string prefix = "ProjectBuddy.Wiki.Sql.";
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

            // Session locks (connections are not pooled, so closing releases them) serialize concurrent first runs.
            using (var lockCommand = new NpgsqlCommand("SELECT pg_advisory_lock(hashtext('wiki_create_database'))", server))
            {
                lockCommand.ExecuteNonQuery();
            }

            using var exists = new NpgsqlCommand("SELECT 1 FROM pg_database WHERE datname = $1", server) { Parameters = { new() { Value = options.Database } } };
            if (exists.ExecuteScalar() is null)
            {
                // The name is a validated identifier ([a-z_][a-z0-9_]*), so quoting it is safe.
                using var create = new NpgsqlCommand($"CREATE DATABASE \"{options.Database}\"", server);
                create.ExecuteNonQuery();
                created = true;
            }
        }

        using var connection = Open(options.Database);
        using (var lockCommand = new NpgsqlCommand("SELECT pg_advisory_lock(hashtext('wiki_schema_migrations'))", connection))
        {
            lockCommand.ExecuteNonQuery();
        }

        using (var table = new NpgsqlCommand(
            $"CREATE TABLE IF NOT EXISTS {MigrationTable} (version integer PRIMARY KEY, name text NOT NULL, sha256 text NOT NULL, applied_at timestamptz NOT NULL DEFAULT clock_timestamp())",
            connection))
        {
            table.ExecuteNonQuery();
        }

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
                    throw new WikiException(ExitCodes.Refused, $"migration {name} changed after it was applied");
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
            throw new WikiException(ExitCodes.Refused, $"the database has {applied} of {embedded} migrations; run migrate");
        }
    }

    public Ingested Ingest(string commit, IReadOnlyList<WikiFile> files)
    {
        using var connection = Open(options.Database);
        using var command = new NpgsqlCommand("SELECT snapshot_uuid, digest, reused, new_blobs FROM wiki_v1.ingest_snapshot($1, $2, $3)", connection)
        {
            Parameters =
            {
                new() { Value = commit },
                new() { Value = files.Select(file => file.Path).ToArray() },
                new() { Value = files.Select(file => file.Content).ToArray(), NpgsqlDbType = NpgsqlDbType.Array | NpgsqlDbType.Bytea },
            },
        };
        using var reader = command.ExecuteReader();
        reader.Read();
        return new Ingested(reader.GetGuid(0), reader.GetString(1), reader.GetBoolean(2), reader.GetInt32(3));
    }

    public Snapshot? Latest()
    {
        using var connection = Open(options.Database);
        using var command = new NpgsqlCommand("SELECT snapshot_uuid, digest, wiki_commit, file_count, byte_count FROM wiki_v1.latest_snapshot()", connection);
        using var reader = command.ExecuteReader();
        return reader.Read() ? new Snapshot(reader.GetGuid(0), reader.GetString(1), reader.GetString(2), reader.GetInt32(3), reader.GetInt64(4)) : null;
    }

    public Guid? CommitSnapshot(string commit)
    {
        using var connection = Open(options.Database);
        using var command = new NpgsqlCommand("SELECT wiki_v1.commit_snapshot($1)", connection) { Parameters = { new() { Value = commit } } };
        return command.ExecuteScalar() as Guid?;
    }

    public JsonObject ProjectionReport(Guid snapshot)
    {
        using var connection = Open(options.Database);
        using var command = new NpgsqlCommand("SELECT wiki_v1.projection_report($1)::text", connection) { Parameters = { new() { Value = snapshot } } };
        return JsonNode.Parse((string)command.ExecuteScalar()!)!.AsObject();
    }

    public string Envelope(string commit, string scope, string identity, string targets)
    {
        using var connection = Open(options.Database);
        using var command = new NpgsqlCommand("SELECT wiki_v1.registry_envelope($1, $2, $3::jsonb, $4::jsonb)::text", connection)
        {
            Parameters = { new() { Value = commit }, new() { Value = scope }, new() { Value = identity }, new() { Value = targets } },
        };
        return (string)command.ExecuteScalar()!;
    }

    public IReadOnlyList<WikiFile> Export(Guid snapshot)
    {
        using var connection = Open(options.Database);
        using var command = new NpgsqlCommand("SELECT path, content FROM wiki_v1.export_snapshot($1)", connection) { Parameters = { new() { Value = snapshot } } };
        using var reader = command.ExecuteReader();
        var files = new List<WikiFile>();
        while (reader.Read())
        {
            files.Add(new WikiFile(reader.GetString(0), (byte[])reader.GetValue(1)));
        }

        return files;
    }

    public IReadOnlyList<(string Path, string Change)> Changed(Guid from, Guid to)
    {
        using var connection = Open(options.Database);
        using var command = new NpgsqlCommand("SELECT path, change FROM wiki_v1.changed_paths($1, $2)", connection)
        {
            Parameters = { new() { Value = from }, new() { Value = to } },
        };
        using var reader = command.ExecuteReader();
        var changes = new List<(string, string)>();
        while (reader.Read())
        {
            changes.Add((reader.GetString(0), reader.GetString(1)));
        }

        return changes;
    }

    internal static NpgsqlConnection Connect(Options options, string database, string user, string password)
    {
        var connection = new NpgsqlConnection(new NpgsqlConnectionStringBuilder
        {
            Host = options.Host,
            Port = options.Port,
            Username = user,
            Password = password,
            Database = database,
            Timeout = 10,
            CommandTimeout = 300,
            Pooling = false,
        }.ConnectionString);
        connection.Open();
        return connection;
    }

    private NpgsqlConnection Open(string database) => Connect(options, database, options.User, password);

    internal static string ReadPassword(string path)
    {
        string text;
        try
        {
            text = File.ReadAllText(path).TrimEnd('\r', '\n');
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException)
        {
            throw new WikiException(ExitCodes.Refused, $"cannot read the password file: {error.GetType().Name}");
        }

        return text.Length > 0 ? text : throw new WikiException(ExitCodes.Refused, "the password file is empty");
    }
}
