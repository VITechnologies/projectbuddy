using Npgsql;
using NpgsqlTypes;

namespace ProjectBuddy.Wiki;

// The RSR requirement registry (registry_v1). Snapshots are ingested as the importer login. The lookup that reuses a
// snapshot whose content key exists (the registry refuses a changed payload, such as a new commit, under that key)
// reads as the administrator login, as RSR's own bootstrap does, until the importer can look snapshots up itself
// (svelderrainruiz/repo-standards-review work item 2).
internal sealed class Registry(Options options)
{
    private readonly string administratorPassword = Database.ReadPassword(options.PasswordFile);
    private readonly string importerPassword = Database.ReadPassword(options.ImporterPasswordFile!);

    public Guid? Find(string source, string scope, string treeDigest, string parserVersion, string manifestDigest)
    {
        using var connection = Database.Connect(options, options.RegistryDatabase, options.User, administratorPassword);
        using var command = new NpgsqlCommand(
            "SELECT snapshot_uuid FROM registry_v1.snapshots WHERE source_uuid = $1::uuid AND scope = $2 AND tree_digest = $3"
            + " AND parser_version = $4 AND authority_manifest_digest = $5",
            connection)
        {
            Parameters = { new() { Value = source }, new() { Value = scope }, new() { Value = treeDigest }, new() { Value = parserVersion }, new() { Value = manifestDigest } },
        };
        return command.ExecuteScalar() as Guid?;
    }

    public (Guid Snapshot, bool Reused) Ingest(string envelope)
    {
        using var connection = Database.Connect(options, options.RegistryDatabase, options.Importer, importerPassword);
        using var command = new NpgsqlCommand("CALL registry_v1.ingest_snapshot($1, NULL, NULL, NULL)", connection)
        {
            Parameters = { new() { Value = envelope, NpgsqlDbType = NpgsqlDbType.Jsonb } },
        };
        using var reader = command.ExecuteReader();
        reader.Read();
        return (reader.GetGuid(0), reader.GetBoolean(2));
    }
}
