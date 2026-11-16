using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;
using Npgsql;
using Xunit;

namespace ProjectBuddy.Wiki.Tests;

public sealed class ProjectionTests : IDisposable
{
    private const string Source = "11111111-2222-4333-8444-555555555555";
    private const string Constellation = "66666666-7777-4888-8999-aaaaaaaaaaaa";
    private static readonly string Manifest = new('d', 64);
    private static readonly string Identity =
        $$"""{"source_uuid":"{{Source}}","constellation_uuid":"{{Constellation}}","manifest_digest":"{{Manifest}}","source_ref":"master"}""";

    // 129 characters: one more than the registry allows for a node ID.
    private static readonly string LongId = "REQ-" + new string('1', 125);

    private const string SoftwareTarget = "aaaaaaaa-0000-4000-8000-000000000001";
    private const string SystemTarget = "aaaaaaaa-0000-4000-8000-000000000002";

    private readonly Server server = new();
    private readonly Repo repo = new();

    public void Dispose()
    {
        repo.Dispose();
        server.Dispose();
    }

    private static string Requirement(string id, string title, string testIds, string codeRef = "None", bool withStatus = true) =>
        $"# {id}: {title}\n\n## Requirement\n\nThe system shall work.\n\n## Rationale\n\nBecause.\n\n## Fit Criterion\n\nIt works.\n\n"
        + $"## Verification\n\nPlanned.\n\n## Source\n\n[[Specification]]\n\n## Priority\n\nHigh\n\n## TestID\n\n{testIds}\n\n"
        + $"## TestArtifact\n\nReview\n\n## CodeRef\n\n{codeRef}" + (withStatus ? "\n\n## Status\n\nPlanned\n" : "\n");

    private static string Marker(string id, string kind, string body) => $"# {id}\n\n<!-- page-kind: {kind} -->\n{body}\n";

    private (string Commit, string[] Connection) SeedValidWiki()
    {
        var connection = server.Migrated();
        var commit = repo
            .Write("Home.md", "# Home\n")
            .Write("system-requirements/SYS-REQ-001.md", Requirement("SYS-REQ-001", "Reusable engine", "[[TEST-001]]", "[[REQ-001]], [[REQ-002|the old one]]"))
            .Write("software-requirements/REQ-001.md", Requirement("REQ-001", "No pinned config: none", "[[TEST-001]], [[TEST-002#case]] and again [[TEST-001]]"))
            .Write("software-requirements/REQ-002.md", Marker("REQ-002", "retired", "Merged into [[REQ-001]]; see [[Software-Requirements-Specification]]."))
            .Write("tests/TEST-001.md", "# TEST-001\n\n## Stimulus\n\nRun it.\n## not a heading\n\n## Required observation\n\n\n")
            .Write("tests/TEST-002.md", Marker("TEST-002", "stub", "No definition yet."))
            .Commit();
        Assert.Equal(0, Cli.Run(["seed", "--wiki", repo.Path, .. connection]).Exit);
        return (commit, connection);
    }

    private JsonObject Envelope(string[] connection, string commit, string scope, string targets) =>
        JsonNode.Parse((string)server.Scalar(connection, $"SELECT wiki_v1.registry_envelope('{commit}', '{scope}', '{Identity}', '{targets}')::text")!)!.AsObject();

    private string Refusal(string[] connection, string commit, string scope, string identity, string targets) =>
        Assert.Throws<PostgresException>(() => server.Scalar(connection, $"SELECT wiki_v1.registry_envelope('{commit}', '{scope}', {identity}, {targets})")).MessageText;

    private static string Sha256(string text) => Convert.ToHexStringLower(SHA256.HashData(Encoding.UTF8.GetBytes(text)));

    private static string Literal(string text) =>
        "convert_to(E'" + text.Replace("\\", "\\\\").Replace("'", "''").Replace("\n", "\\n").Replace("\r", "\\r") + "', 'UTF8')";

    [Theory]
    [InlineData("# REQ-001\n\n<!-- page-kind: sections -->\nhello\n", "not-canonical")]
    [InlineData("# REQ-001\n\n<!-- page-kind: stub -->\n\n", "round-trips")]
    [InlineData("# REQ-001\n\n<!-- page-kind: stub -->\na\n\n", "round-trips")]
    [InlineData("# REQ-001: t\n\n## A\n\n\n\n## B\n\nb\n", "round-trips")]
    [InlineData("# REQ-001\n\n## A\n\nx\n\n## \n\ny\n", "round-trips")]
    [InlineData("# REQ-001\n\n## A\n\nx\n\n\n## B\n\ny\n", "round-trips")]
    [InlineData("# REQ-001\n\n## A\n\nx\n\n## B\n\n## C\n\n\n", "round-trips")]
    [InlineData("# REQ-001\n\n## A\n\nx", "not-canonical")]
    [InlineData("# REQ-001\n\n<!-- page-kind: stub -->", "not-canonical")]
    [InlineData("# REQ-001\r\n\r\n## A\r\n\r\nx\r\n", "no-id-heading")]
    [InlineData("# REQ-001: \n\n## A\n\nx\n", "no-id-heading")]
    public void ParsesOnlyWhatRendersBackByteForByte(string page, string outcome)
    {
        var connection = server.Migrated();
        Assert.Equal(outcome, server.Scalar(connection, $"""
            SELECT coalesce(p.reason, CASE WHEN wiki_v1.render_page(p.page_id, p.title, p.page_kind, p.sections) = convert_from(i.x, 'UTF8')
                THEN 'round-trips' ELSE 'BROKEN' END)
            FROM (SELECT {Literal(page)} AS x) AS i CROSS JOIN LATERAL wiki_v1.parse_page(i.x) AS p
            """));
    }

    [Fact]
    public void RefusesAnEnvelopeOverTheRegistryLimits()
    {
        var connection = server.Migrated();
        for (var index = 0; index < 10001; index++)
        {
            repo.Write($"tests/TEST-{index:D5}.md", Marker($"TEST-{index:D5}", "stub", "x"));
        }

        var commit = repo.Commit();
        Assert.Equal(0, Cli.Run(["seed", "--wiki", repo.Path, .. connection]).Exit);
        var refused = Assert.Throws<PostgresException>(() => server.Scalar(connection,
            $$"""SELECT wiki_v1.registry_envelope('{{commit}}', 'test', '{{Identity}}', '{"software":"{{SoftwareTarget}}","system":"{{SystemTarget}}"}')"""));
        Assert.Equal(("54000", "the test envelope exceeds the registry limits of 10000 nodes, 50000 edges and 16 MiB"), (refused.SqlState, refused.MessageText));
    }

    [Fact]
    public void ReportsEveryKindOfPageAndEdge()
    {
        var (commit, connection) = SeedValidWiki();
        var (exit, output, _) = Cli.Run(["project", .. connection]);
        Assert.Equal(0, exit);
        var report = JsonNode.Parse(output)!.AsObject();
        Assert.Equal(commit, (string?)report["commit"]);
        Assert.True((bool)report["exportable"]!);
        Assert.Equal("projectbuddy-wiki-pages-v1", (string?)report["parserVersion"]);
        Assert.True(JsonNode.DeepEquals(
            JsonNode.Parse("""{"system":{"sections":1},"software":{"sections":1,"retired":1},"test":{"sections":1,"stub":1}}"""), report["pages"]));
        Assert.True(JsonNode.DeepEquals(JsonNode.Parse("""{"verifies":3,"allocates":2,"supersedes":1}"""), report["edges"]));
        Assert.Equal((0, "[]"), ((int)report["problemCount"]!, report["problems"]!.ToJsonString()));

        // The canonical layout gives every governed page its bytes back.
        Assert.Equal(true, server.Scalar(connection, """
            SELECT bool_and(wiki_v1.render_page(g.page_id, g.title, g.page_kind, g.sections) = convert_from(b.content, 'UTF8'))
            FROM wiki_v1.governed_pages(wiki_v1.commit_snapshot('COMMIT')) AS g JOIN wiki_v1.blobs AS b USING (sha256)
            """.Replace("COMMIT", commit)));
    }

    [Fact]
    public void BuildsTheRegistryEnvelopeOfEachScope()
    {
        var (commit, connection) = SeedValidWiki();

        var software = Envelope(connection, commit, "software", "{}");
        Assert.Equal(11, software.Count);
        Assert.Equal(("rsr/registry-snapshot@1", "software", "master", commit), ((string?)software["schema"], (string?)software["scope"], (string?)software["source_ref"], (string?)software["source_revision"]));
        Assert.Equal((Source, Constellation, Manifest), ((string?)software["source_uuid"], (string?)software["constellation_uuid"], (string?)software["manifest_digest"]));
        var nodes = software["nodes"]!.AsArray();
        Assert.Equal(["REQ-001:active", "REQ-002:superseded"], nodes.Select(node => $"{node!["id"]}:{node["status"]}"));
        var retired = nodes[1]!;
        Assert.Equal("""{"Body":"Merged into [[REQ-001]]; see [[Software-Requirements-Specification]].","ID":"REQ-002","Page kind":"retired"}""", (string?)retired["text"]);
        Assert.Equal(("software", "software-requirements/REQ-002.md"), ((string?)retired["kind"], (string?)retired["location"]));
        Assert.All(nodes, node => Assert.Equal(Sha256((string)node!["text"]!), (string?)node["content_digest"]));
        Assert.StartsWith("""{"CodeRef":"None","Fit Criterion":"It works.","ID":"REQ-001","Priority":"High",""", (string?)nodes[0]!["text"]);
        Assert.Equal(
            """[{"type":"supersedes","to_id":"REQ-002","from_id":"REQ-001","metadata":{"page":"software-requirements/REQ-002.md","field":"Body"},"to_snapshot_uuid":null}]""",
            software["edges"]!.ToJsonString());

        var system = Envelope(connection, commit, "system", $$"""{"software":"{{SoftwareTarget}}"}""");
        Assert.Equal(["SYS-REQ-001 allocates REQ-001", "SYS-REQ-001 allocates REQ-002"],
            system["edges"]!.AsArray().Select(edge => $"{edge!["from_id"]} {edge["type"]} {edge["to_id"]}"));
        Assert.All(system["edges"]!.AsArray(), edge => Assert.Equal(SoftwareTarget, (string?)edge!["to_snapshot_uuid"]));

        var targets = $$"""{"software":"{{SoftwareTarget}}","system":"{{SystemTarget}}"}""";
        var test = Envelope(connection, commit, "test", targets);
        Assert.Equal(
            [$"TEST-001 verifies REQ-001 {SoftwareTarget}", $"TEST-001 verifies SYS-REQ-001 {SystemTarget}", $"TEST-002 verifies REQ-001 {SoftwareTarget}"],
            test["edges"]!.AsArray().Select(edge => $"{edge!["from_id"]} {edge["type"]} {edge["to_id"]} {edge["to_snapshot_uuid"]}"));
        Assert.Equal("""{"ID":"TEST-001","Required observation":"","Stimulus":"Run it.\n## not a heading"}""", (string?)test["nodes"]![0]!["text"]);

        // The tree digest is stable for the same inputs and follows the registry snapshots the edges point into.
        Assert.Equal((string?)test["tree_digest"], (string?)Envelope(connection, commit, "test", targets)["tree_digest"]);
        Assert.NotEqual((string?)test["tree_digest"], (string?)Envelope(connection, commit, "test", targets.Replace("0002", "0003"))["tree_digest"]);
        Assert.Matches("^[0-9a-f]{64}$", (string?)software["tree_digest"]);
    }

    [Fact]
    public void KeysAScopeByItsContentNotItsCommit()
    {
        // The registry refuses a changed payload under an existing key, so an exporter must look this key up and reuse
        // the snapshot when only the commit changed (README: "Reuse before ingesting").
        var (first, connection) = SeedValidWiki();
        var second = repo.Write("Home.md", "# Home, edited\n").Commit();
        Assert.Equal(0, Cli.Run(["seed", "--wiki", repo.Path, .. connection]).Exit);

        var before = Envelope(connection, first, "software", "{}");
        var after = Envelope(connection, second, "software", "{}");
        Assert.Equal((string?)before["tree_digest"], (string?)after["tree_digest"]);
        Assert.Equal((first, second), ((string?)before["source_revision"], (string?)after["source_revision"]));
        Assert.True(JsonNode.DeepEquals(before["nodes"], after["nodes"]));
    }

    [Fact]
    public void RefusesAnEnvelopeItCannotBind()
    {
        var (commit, connection) = SeedValidWiki();
        var identity = $"'{Identity}'";
        Assert.Contains($"commit {new string('e', 40)} is not recorded", Refusal(connection, new string('e', 40), "software", identity, "'{}'"));
        Assert.Equal("scope must be system, software or test", Refusal(connection, commit, "dependency", identity, "'{}'"));

        Assert.Equal("the source identity must be a JSON object", Refusal(connection, commit, "software", "NULL", "'{}'"));
        Assert.Equal("the source identity must be a JSON object", Refusal(connection, commit, "software", "'[]'", "'{}'"));
        foreach (var bad in new[]
        {
            Identity.Replace(",\"source_ref\":\"master\"", ""),
            Identity.Replace("\"master\"", "null"),
            Identity.Replace("\"master\"", "\"  \""),
            Identity.Replace(Source, "not-a-uuid"),
            Identity.Replace(Constellation, Constellation.ToUpperInvariant()),
            Identity.Replace(Manifest, "abc"),
            Identity.Replace("}", ""","extra":1}"""),
        })
        {
            Assert.Equal("the source identity needs exactly source_uuid, constellation_uuid, manifest_digest and source_ref",
                Refusal(connection, commit, "software", $"'{bad}'", "'{}'"));
        }

        Assert.Equal("the registry targets must be a JSON object", Refusal(connection, commit, "software", identity, "NULL"));
        Assert.Equal("the registry targets must be a JSON object", Refusal(connection, commit, "software", identity, "'[]'"));
        Assert.Equal("the software scope needs registry snapshots for exactly: none",
            Refusal(connection, commit, "software", identity, $$"""'{"software":"{{SoftwareTarget}}"}'"""));
        Assert.Equal("the system scope needs registry snapshots for exactly: software", Refusal(connection, commit, "system", identity, "'{}'"));
        Assert.Equal("the test scope needs registry snapshots for exactly: software, system",
            Refusal(connection, commit, "test", identity, $$"""'{"software":"{{SoftwareTarget}}","system":1}'"""));
        Assert.Equal("the test scope needs registry snapshots for exactly: software, system",
            Refusal(connection, commit, "test", identity, $$"""'{"software":"{{SoftwareTarget}}","system":"x"}'"""));
    }

    [Fact]
    public void ListsEveryPageAndLinkThatCannotBeExported()
    {
        var connection = server.Migrated();
        var commit = repo
            .Write("tests/notes.txt", "notes\n")
            .Write("tests/sub/TEST-010.md", Marker("TEST-010", "stub", "x"))
            .Write("tests/TEST-011.md", [0xff, 0xfe])
            .Write("tests/TEST-012.md", "Just text\n")
            .Write("tests/TEST-013.md", "# TEST-013\n## Stimulus\n")
            .Write("tests/TEST-014.md", Marker("TEST-015", "stub", "x"))
            .Write("tests/TEST-016.md", Marker("TEST-016", "retired", "x"))
            .Write("tests/TEST-017.md", Marker("TEST-017", "stub", new string('x', 17000)))
            .Write("software-requirements/SYS-REQ-020.md", Requirement("SYS-REQ-020", "Misplaced", "None"))
            .Write("software-requirements/REQ-030.md", Requirement("REQ-030", "No status", "None", withStatus: false))
            .Write("software-requirements/REQ-033.md", Marker("REQ-033", "retired", "Merged into [[SYS-REQ-031]] and [[REQ-033]]."))
            .Write($"software-requirements/{LongId}.md", Marker(LongId, "retired", "x"))
            .Write("system-requirements/SYS-REQ-031.md", Requirement("SYS-REQ-031", "Wrong links", "[[REQ-040]]", "[[TEST-016]]"))
            .Write("system-requirements/SYS-REQ-032.md", Requirement("SYS-REQ-032", "Dangling links", "[[TEST-999]]", "[[REQ-999]]"))
            .Commit();
        Assert.Equal(0, Cli.Run(["seed", "--wiki", repo.Path, .. connection]).Exit);

        var (exit, output, _) = Cli.Run(["project", "--commit", commit, .. connection]);
        Assert.Equal(ExitCodes.Findings, exit);
        var report = JsonNode.Parse(output)!.AsObject();
        Assert.False((bool)report["exportable"]!);
        Assert.Equal(
            [
                "software-requirements/REQ-030.md||unexpected-sections",
                "software-requirements/REQ-033.md|REQ-033 supersedes REQ-033|link-outside-scope",
                "software-requirements/REQ-033.md|SYS-REQ-031 supersedes REQ-033|link-outside-scope",
                $"software-requirements/{LongId}.md||id-too-long",
                "software-requirements/SYS-REQ-020.md||id-outside-scope",
                "system-requirements/SYS-REQ-031.md|REQ-040 verifies SYS-REQ-031|link-outside-field",
                "system-requirements/SYS-REQ-031.md|SYS-REQ-031 allocates TEST-016|link-outside-field",
                "system-requirements/SYS-REQ-032.md|SYS-REQ-032 allocates REQ-999|dangling-link",
                "system-requirements/SYS-REQ-032.md|TEST-999 verifies SYS-REQ-032|dangling-link",
                "tests/TEST-011.md||not-utf8",
                "tests/TEST-012.md||no-id-heading",
                "tests/TEST-013.md||not-canonical",
                "tests/TEST-014.md||id-does-not-match-path",
                "tests/TEST-016.md||unexpected-page-kind",
                "tests/TEST-017.md||text-too-long",
                "tests/notes.txt||unexpected-file",
                "tests/sub/TEST-010.md||unexpected-file",
            ],
            report["problems"]!.AsArray().Select(problem => $"{problem!["path"]}|{problem["link"]}|{problem["problem"]}"));
        Assert.Equal(17, (int)report["problemCount"]!);

        // The registry never receives part of a wiki.
        var refused = Assert.Throws<PostgresException>(() => server.Scalar(connection, $"SELECT wiki_v1.registry_envelope('{commit}', 'software', '{Identity}', '{{}}')"));
        Assert.Equal(("22000", "the wiki has 17 pages or links that cannot be exported"), (refused.SqlState, refused.MessageText));
        Assert.Contains("tests/notes.txt", refused.Detail);
    }

    [Fact]
    public void ProjectsTheLatestOrANamedRecordedState()
    {
        var connection = server.Migrated();
        Assert.Contains("nothing is recorded yet; run seed first", Cli.Run(["project", .. connection]).Error);

        var first = repo.Write("tests/TEST-001.md", Marker("TEST-001", "stub", "x")).Commit();
        Cli.Run(["seed", "--wiki", repo.Path, .. connection]);
        repo.Write("tests/TEST-002.md", Marker("TEST-002", "stub", "y")).Commit();
        Cli.Run(["seed", "--wiki", repo.Path, .. connection]);

        var latest = JsonNode.Parse(Cli.Run(["project", .. connection]).Output)!;
        Assert.Equal(2, (int)latest["pages"]!["test"]!["stub"]!);
        var named = JsonNode.Parse(Cli.Run(["project", "--commit", first, .. connection]).Output)!;
        Assert.Equal((first, 1), ((string?)named["commit"], (int)named["pages"]!["test"]!["stub"]!));

        var unknown = Cli.Run(["project", "--commit", new string('f', 40), .. connection]);
        Assert.Equal(ExitCodes.Refused, unknown.Exit);
        Assert.Contains($"commit {new string('f', 40)} is not recorded", unknown.Error);
    }
}
