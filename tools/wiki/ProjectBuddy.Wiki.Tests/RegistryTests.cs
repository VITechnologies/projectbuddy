using System.Text.Json.Nodes;
using Xunit;

namespace ProjectBuddy.Wiki.Tests;

public sealed class RegistryTests : IDisposable
{
    private const string Source = "11111111-2222-4333-8444-555555555555";
    private const string Constellation = "66666666-7777-4888-8999-aaaaaaaaaaaa";
    private static readonly string Manifest = new('d', 64);

    private readonly Server server = new();
    private readonly Repo repo = new();
    private readonly string approval = Path.GetTempFileName();

    public RegistryTests() => WriteApproval(Manifest);

    public void Dispose()
    {
        File.Delete(approval);
        repo.Dispose();
        server.Dispose();
    }

    private void WriteApproval(string manifest) => File.WriteAllText(approval, new JsonObject
    {
        ["approval_status"] = "APPROVED",
        ["approved_by"] = "test",
        ["authority_manifest"] = new JsonObject { ["source_uuid"] = Source, ["constellation_uuid"] = Constellation },
        ["authority_manifest_digest"] = manifest,
        ["source_lock"] = new JsonObject { ["accepted_ref"] = "master" },
    }.ToJsonString());

    private (int Exit, JsonObject Result, string Error) Export(string[] connection, string registry)
    {
        var (exit, output, error) = Cli.Run([
            "export-registry", "--approval", approval, "--importer-password-file", server.PasswordFile, "--importer", server.User,
            "--registry-database", registry, .. connection]);
        return (exit, exit == 0 ? JsonNode.Parse(output)!.AsObject() : [], error);
    }

    private static string Scopes(JsonObject result) => string.Join(" ", ((string[])["software", "system", "test"]).Select(scope =>
        $"{scope}:{((bool)result["scopes"]![scope]!["reused"]! ? "reused" : "ingested")}:{result["scopes"]![scope]!["nodes"]}/{result["scopes"]![scope]!["edges"]}"));

    private static string Ids(JsonObject result) => string.Join(" ", ((string[])["software", "system", "test"]).Select(scope => (string?)result["scopes"]![scope]!["snapshot"]));

    [Fact]
    public void ExportsEachScopeOnceAndReusesUnchangedContent()
    {
        var connection = server.Migrated();
        var registry = server.NewRegistry(Source, Constellation, Manifest);
        var first = Pages.WriteValidWiki(repo).Commit();
        Cli.Run(["seed", "--wiki", repo.Path, .. connection]);

        var exported = Export(connection, registry);
        Assert.Equal(0, exported.Exit);
        Assert.Equal((first, Source), ((string?)exported.Result["commit"], (string?)exported.Result["source"]));
        Assert.Equal("software:ingested:2/1 system:ingested:1/2 test:ingested:2/3", Scopes(exported.Result));

        // Again, and from a later commit that changed no governed page: the registry would refuse the new revision under
        // the same content key, so the export reuses every snapshot.
        Assert.Equal("software:reused:2/1 system:reused:1/2 test:reused:2/3", Scopes(Export(connection, registry).Result));
        repo.Write("Home.md", "# Home, edited\n").Commit();
        Cli.Run(["seed", "--wiki", repo.Path, .. connection]);
        var later = Export(connection, registry);
        Assert.Equal("software:reused:2/1 system:reused:1/2 test:reused:2/3", Scopes(later.Result));
        Assert.Equal(Ids(exported.Result), Ids(later.Result));

        // A changed requirement is a new software snapshot, and so new system and test snapshots, whose edges point into it.
        repo.Write("software-requirements/REQ-001.md", Pages.Requirement("REQ-001", "Changed", "[[TEST-001]]")).Commit();
        Cli.Run(["seed", "--wiki", repo.Path, .. connection]);
        var changed = Export(connection, registry);
        Assert.Equal("software:ingested:2/1 system:ingested:1/2 test:ingested:2/2", Scopes(changed.Result));

        string[] registryConnection = [.. connection[..^1], registry];
        Assert.Equal(6L, server.Scalar(registryConnection, "SELECT count(*) FROM registry_v1.snapshots"));
        Assert.Equal(2L, server.Scalar(registryConnection, "SELECT count(*) FROM registry_v1.requirement_nodes WHERE local_id = 'REQ-001'"));
        var software = (string?)changed.Result["scopes"]!["software"]!["snapshot"];
        var system = (string?)changed.Result["scopes"]!["system"]!["snapshot"];
        Assert.Equal("REQ-001 REQ-002", server.Scalar(registryConnection, $"""
            SELECT string_agg(to_local_id, ' ' ORDER BY to_local_id) FROM registry_v1.edges
            WHERE snapshot_uuid = '{system}' AND from_local_id = 'SYS-REQ-001' AND edge_type = 'allocates' AND to_snapshot_uuid = '{software}'
            """));
    }

    [Fact]
    public void RefusesWhatItCannotExport()
    {
        var connection = server.Migrated();
        var registry = server.NewRegistry(Source, Constellation, Manifest);
        Pages.WriteValidWiki(repo).Commit();
        Cli.Run(["seed", "--wiki", repo.Path, .. connection]);

        foreach (var (record, reason) in new[]
        {
            ("{", "the approval record is not JSON"),
            ("[]", "the approval record has no approval_status"),
            ("""{"approval_status":"PROPOSED"}""", "the approval record is not APPROVED"),
            ("""{"approval_status":"APPROVED","authority_manifest":{"source_uuid":1}}""", "the approval record has no authority_manifest.source_uuid"),
            ($$"""{"approval_status":"APPROVED","authority_manifest":{"source_uuid":"{{Source}}","constellation_uuid":"{{Constellation}}"},"authority_manifest_digest":"{{Manifest}}"}""",
                "the approval record has no source_lock.accepted_ref"),
        })
        {
            File.WriteAllText(approval, record);
            var refused = Export(connection, registry);
            Assert.Equal(ExitCodes.Refused, refused.Exit);
            Assert.Equal($"projectbuddy-wiki: {reason}", refused.Error.Trim());
        }

        File.Delete(approval);
        Assert.Equal(ExitCodes.Refused, Export(connection, registry).Exit);

        // A manifest the registry has not admitted.
        WriteApproval(new string('e', 64));
        var unadmitted = Export(connection, registry);
        Assert.Equal(ExitCodes.Database, unadmitted.Exit);
        Assert.Contains("source_authority_not_admitted", unadmitted.Error);

        // A wiki with a page that cannot be exported reaches no registry call.
        WriteApproval(Manifest);
        repo.Write("tests/notes.txt", "notes\n").Commit();
        Cli.Run(["seed", "--wiki", repo.Path, .. connection]);
        var problems = Export(connection, registry);
        Assert.Equal(ExitCodes.Refused, problems.Exit);
        Assert.Contains("1 pages or links cannot be exported; run project", problems.Error);
        Assert.Equal(0L, server.Scalar([.. connection[..^1], registry], "SELECT count(*) FROM registry_v1.snapshots"));
    }
}
