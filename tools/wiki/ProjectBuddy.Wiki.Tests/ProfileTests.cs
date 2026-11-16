using System.Text.Json.Nodes;
using Npgsql;
using Xunit;

namespace ProjectBuddy.Wiki.Tests;

public sealed class ProfileTests : IDisposable
{
    // A member whose requirements are LBA-REQ-NNN and whose tests are T-NNN, with no system scope (lbabus's shape).
    private const string MemberProfile = """
        {"schema":"rsr/wiki-profile@1","scopes":[
          {"scope":"software","directory":"software-requirements","idPrefix":"LBA-REQ","sections":["Requirement","TestID","CodeRef","Status"]},
          {"scope":"test","directory":"tests","idPrefix":"T","sections":["Stimulus","Required observation"]}]}
        """;

    private const string Source = "11111111-2222-4333-8444-555555555555";
    private const string Constellation = "66666666-7777-4888-8999-aaaaaaaaaaaa";
    private static readonly string Manifest = new('d', 64);
    private static readonly string Identity =
        $$"""{"source_uuid":"{{Source}}","constellation_uuid":"{{Constellation}}","manifest_digest":"{{Manifest}}","source_ref":"master"}""";

    private readonly Server server = new();
    private readonly Repo repo = new();

    public void Dispose()
    {
        repo.Dispose();
        server.Dispose();
    }

    private static string MemberRequirement(string id, string testIds, string codeRef = "None") =>
        $"# {id}: A member requirement\n\n## Requirement\n\nIt shall work.\n\n## TestID\n\n{testIds}\n\n## CodeRef\n\n{codeRef}\n\n## Status\n\nPlanned\n";

    private (string Commit, string[] Connection, JsonObject Report, int Exit) Project()
    {
        var connection = server.Migrated();
        var commit = repo.Commit();
        Assert.Equal(0, Cli.Run(["seed", "--wiki", repo.Path, .. connection]).Exit);
        var (exit, output, _) = Cli.Run(["project", .. connection]);
        return (commit, connection, JsonNode.Parse(output)!.AsObject(), exit);
    }

    [Fact]
    public void GovernsTheScopesAWikiProfileDeclares()
    {
        repo.Write(".wiki-profile.json", MemberProfile)
            .Write("software-requirements/LBA-REQ-001.md", MemberRequirement("LBA-REQ-001", "[[T-001]], [[TEST-001]]", "[[LBA-REQ-002]]"))
            .Write("software-requirements/LBA-REQ-002.md", Pages.Marker("LBA-REQ-002", "retired", "Merged into [[LBA-REQ-001]]."))
            .Write("software-requirements/LBA-REQ-003.md", MemberRequirement("LBA-REQ-003", "[[LBA-REQ-001]]"))
            .Write("tests/T-001.md", "# T-001\n\n## Stimulus\n\nRun it.\n\n## Required observation\n\nIt works.\n")
            .Write("system-requirements/SYS-REQ-001.md", Pages.Requirement("SYS-REQ-001", "Not governed here", "[[TEST-001]]"));
        var (commit, connection, report, exit) = Project();

        Assert.Equal(ExitCodes.Findings, exit);
        Assert.True(JsonNode.DeepEquals(JsonNode.Parse("""{"source":"wiki","scopes":["software","test"]}"""), report["profile"]));
        Assert.True(JsonNode.DeepEquals(JsonNode.Parse("""{"software":{"sections":2,"retired":1},"test":{"sections":1}}"""), report["pages"]));
        Assert.True(JsonNode.DeepEquals(JsonNode.Parse("""{"verifies":1,"supersedes":1}"""), report["edges"]));

        // A TestID link is checked against the test prefix; [[TEST-001]] has no prefix of this profile and is not a link.
        Assert.Equal(
            "software-requirements/LBA-REQ-003.md|LBA-REQ-001 verifies LBA-REQ-003|link-outside-field",
            string.Join(";", report["problems"]!.AsArray().Select(problem => $"{problem!["path"]}|{problem["link"]}|{problem["problem"]}")));

        repo.Write("software-requirements/LBA-REQ-003.md", MemberRequirement("LBA-REQ-003", "[[T-001]]"));
        var fixedCommit = repo.Commit();
        Cli.Run(["seed", "--wiki", repo.Path, .. connection]);
        string Envelope(string scope, string targets) => (string)server.Scalar(connection,
            $"SELECT wiki_v1.registry_envelope('{fixedCommit}', '{scope}', '{Identity}', '{targets}')::text")!;
        Assert.Equal(["LBA-REQ-001", "LBA-REQ-002", "LBA-REQ-003"],
            JsonNode.Parse(Envelope("software", "{}"))!["nodes"]!.AsArray().Select(node => (string)node!["id"]!));

        // Without a system scope, the test scope's edges point into the software snapshot only.
        var test = JsonNode.Parse(Envelope("test", """{"software":"aaaaaaaa-0000-4000-8000-000000000001"}"""))!;
        Assert.Equal(["T-001 verifies LBA-REQ-001", "T-001 verifies LBA-REQ-003"],
            test["edges"]!.AsArray().Select(edge => $"{edge!["from_id"]} {edge["type"]} {edge["to_id"]}"));
        var refused = Assert.Throws<PostgresException>(() => Envelope("system", """{"software":"aaaaaaaa-0000-4000-8000-000000000001"}"""));
        Assert.Equal("the wiki profile has no system scope", refused.MessageText);
        Assert.Contains("needs registry snapshots for exactly: software",
            Assert.Throws<PostgresException>(() => Envelope("test", "{}")).MessageText);
        Assert.NotEqual(commit, fixedCommit);
    }

    [Theory]
    [InlineData("{")]
    [InlineData("[]")]
    [InlineData("""{"schema":"rsr/wiki-profile@2","scopes":[]}""")]
    [InlineData("""{"schema":"rsr/wiki-profile@1","scopes":[],"extra":1}""")]
    [InlineData("""{"schema":"rsr/wiki-profile@1","scopes":{}}""")]
    [InlineData("""{"schema":"rsr/wiki-profile@1","scopes":[]}""")]
    [InlineData("""{"schema":"rsr/wiki-profile@1","scopes":[1,2,3,4]}""")]
    [InlineData("""{"schema":"rsr/wiki-profile@1","scopes":["software"]}""")]
    [InlineData("""{"schema":"rsr/wiki-profile@1","scopes":[{"scope":"software","directory":"d","idPrefix":"R"}]}""")]
    [InlineData("""{"schema":"rsr/wiki-profile@1","scopes":[{"scope":"software","directory":7,"idPrefix":"R","sections":["A"]}]}""")]
    [InlineData("""{"schema":"rsr/wiki-profile@1","scopes":[{"scope":"dependency","directory":"d","idPrefix":"R","sections":["A"]}]}""")]
    [InlineData("""{"schema":"rsr/wiki-profile@1","scopes":[{"scope":"software","directory":"a/b","idPrefix":"R","sections":["A"]}]}""")]
    [InlineData("""{"schema":"rsr/wiki-profile@1","scopes":[{"scope":"software","directory":"d","idPrefix":"req","sections":["A"]}]}""")]
    [InlineData("""{"schema":"rsr/wiki-profile@1","scopes":[{"scope":"software","directory":"d","idPrefix":"R","sections":[]}]}""")]
    [InlineData("""{"schema":"rsr/wiki-profile@1","scopes":[{"scope":"software","directory":"d","idPrefix":"R","sections":[1]}]}""")]
    [InlineData("""{"schema":"rsr/wiki-profile@1","scopes":[{"scope":"software","directory":"d","idPrefix":"R","sections":[" "]}]}""")]
    [InlineData("""{"schema":"rsr/wiki-profile@1","scopes":[{"scope":"software","directory":"d","idPrefix":"R","sections":["A\nB"]}]}""")]
    [InlineData("""{"schema":"rsr/wiki-profile@1","scopes":[{"scope":"software","directory":"d","idPrefix":"R","sections":["A","A"]}]}""")]
    [InlineData("""{"schema":"rsr/wiki-profile@1","scopes":[{"scope":"software","directory":"d","idPrefix":"R","sections":["A"]},{"scope":"software","directory":"e","idPrefix":"S","sections":["A"]}]}""")]
    [InlineData("""{"schema":"rsr/wiki-profile@1","scopes":[{"scope":"software","directory":"d","idPrefix":"R","sections":["A"]},{"scope":"test","directory":"d","idPrefix":"S","sections":["A"]}]}""")]
    [InlineData("""{"schema":"rsr/wiki-profile@1","scopes":[{"scope":"software","directory":"d","idPrefix":"R","sections":["A"]},{"scope":"test","directory":"e","idPrefix":"R","sections":["A"]}]}""")]
    public void AnInvalidProfileGovernsNothingAndBlocksTheExport(string profile)
    {
        repo.Write(".wiki-profile.json", profile).Write("tests/TEST-001.md", Pages.Marker("TEST-001", "stub", "x"));
        var (commit, connection, report, exit) = Project();
        Assert.Equal(ExitCodes.Findings, exit);
        Assert.False((bool)report["exportable"]!);
        Assert.True(JsonNode.DeepEquals(JsonNode.Parse("""{"source":"wiki","scopes":[]}"""), report["profile"]));
        Assert.Equal(".wiki-profile.json|invalid-profile",
            string.Join(";", report["problems"]!.AsArray().Select(problem => $"{problem!["path"]}|{problem["problem"]}")));
        Assert.Equal("{}", report["pages"]!.ToJsonString());
        Assert.Contains("cannot be exported", Assert.Throws<PostgresException>(() => server.Scalar(connection,
            $"SELECT wiki_v1.registry_envelope('{commit}', 'test', '{Identity}', '{{}}')")).MessageText);
    }

    [Fact]
    public void AProfileThatIsNotUtf8IsInvalid()
    {
        repo.Write(".wiki-profile.json", [0xff, 0xfe]);
        Assert.Equal(".wiki-profile.json", (string?)Project().Report["problems"]![0]!["path"]);
    }

    [Fact]
    public void ListsPagesOutsideEveryHubIndexAndOverTheBudget()
    {
        repo.Write("Hub.md", "# Hub\n\n<!-- table: | ID | Title | -->\n<!-- /table -->\n\n<!-- sections -->\n- [[Child]]\n<!-- /sections -->\n")
            .Write("Child.md", "# Child\n\nx\n")
            .Write("Note.md", "# Note\n\n<!-- page-kind: note -->\nx\n")
            .Write("Orphan.md", "# Orphan\n\nx\n")
            .Write("Late-Note.md", "# Late Note\n\nx\n<!-- page-kind: note -->\n")
            .Write("Big.md", "# Big\n\n<!-- page-kind: note -->\n" + new string('x', 16400) + "\n")
            .Write("Bad.md", [0xff])
            .Write("architecture/Bad-Architecture.md", [0xff])
            .Write(".trash/Ignored.md", "# Ignored\n\nx\n")
            .Write("notes.txt", "not a page\n")
            .Write("tests/TEST-001.md", Pages.Marker("TEST-001", "stub", "x"));
        var (_, _, report, exit) = Project();
        Assert.Equal(ExitCodes.Findings, exit);
        Assert.True((bool)report["exportable"]!);
        Assert.Equal(["Bad.md:not-utf8", "Big.md:page-over-budget", "Late-Note.md:page-not-indexed", "Orphan.md:page-not-indexed"],
            report["wikiProblems"]!.AsArray().Select(problem => $"{problem!["path"]}:{problem["problem"]}"));
        Assert.Equal(4, (int)report["wikiProblemCount"]!);
    }

    [Fact]
    public void NeedsAHubBeforeIndexingIsRequired()
    {
        repo.Write("Home.md", "# Home\n\nx\n").Write("tests/TEST-001.md", Pages.Marker("TEST-001", "stub", "x"));
        var (_, _, report, exit) = Project();
        Assert.Equal((0, 0), (exit, (int)report["wikiProblemCount"]!));
        Assert.True(JsonNode.DeepEquals(JsonNode.Parse("""{"source":"default","scopes":["software","system","test"]}"""), report["profile"]));
    }

    [Fact]
    public void ExportsOnlyTheProfileScopes()
    {
        repo.Write(".wiki-profile.json", MemberProfile)
            .Write("software-requirements/LBA-REQ-001.md", MemberRequirement("LBA-REQ-001", "[[T-001]]"))
            .Write("tests/T-001.md", "# T-001\n\n## Stimulus\n\nRun it.\n\n## Required observation\n\nIt works.\n");
        var (_, connection, _, _) = Project();
        var registry = server.NewRegistry(Source, Constellation, Manifest);
        var approval = Path.GetTempFileName();
        try
        {
            File.WriteAllText(approval, new JsonObject
            {
                ["approval_status"] = "APPROVED",
                ["authority_manifest"] = new JsonObject { ["source_uuid"] = Source, ["constellation_uuid"] = Constellation },
                ["authority_manifest_digest"] = Manifest,
                ["source_lock"] = new JsonObject { ["accepted_ref"] = "master" },
            }.ToJsonString());
            var (exit, output, error) = Cli.Run([
                "export-registry", "--approval", approval, "--importer-password-file", server.PasswordFile, "--importer", server.User,
                "--registry-database", registry, .. connection]);
            Assert.True(exit == 0, error);
            var scopes = JsonNode.Parse(output)!["scopes"]!.AsObject();
            Assert.Equal(["software", "test"], scopes.Select(scope => scope.Key));
            Assert.Equal((1, 1), ((int)scopes["test"]!["nodes"]!, (int)scopes["test"]!["edges"]!));
        }
        finally
        {
            File.Delete(approval);
        }
    }
}
