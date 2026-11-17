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

    // A member whose tests are TEST-UNIT-NNN and TEST-DOC-NNN (mprr's shape), whose software requirements are MPRR-REQ-NNN
    // and MPRR-NFR-NNN, and whose system scope keeps the single idPrefix, which @2 still allows.
    private const string MultiPrefixProfile = """
        {"schema":"rsr/wiki-profile@2","scopes":[
          {"scope":"system","directory":"system-requirements","idPrefix":"MPRR-SYS","sections":["Requirement","TestID","CodeRef","Status"]},
          {"scope":"software","directory":"software-requirements","idPrefixes":["MPRR-REQ","MPRR-NFR"],"sections":["Requirement","TestID","CodeRef","Status"]},
          {"scope":"test","directory":"tests","idPrefixes":["TEST-UNIT","TEST-DOC"],"sections":["Stimulus","Required observation"]}]}
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

    private static string MemberTest(string id) => $"# {id}\n\n## Stimulus\n\nRun it.\n\n## Required observation\n\nIt works.\n";

    private static string Listed(JsonNode problems) =>
        string.Join(";", problems.AsArray().Select(problem => $"{problem!["path"]}|{problem["link"]}|{problem["problem"]}"));

    [Fact]
    public void GovernsEveryPrefixOfAScope()
    {
        repo.Write(".wiki-profile.json", MultiPrefixProfile)
            .Write("system-requirements/MPRR-SYS-001.md", MemberRequirement("MPRR-SYS-001", "[[TEST-DOC-001]]", "[[MPRR-REQ-001]], [[MPRR-NFR-001]]"))
            .Write("system-requirements/MPRR-SYS-002.md", MemberRequirement("MPRR-SYS-002", "None", "[[TEST-UNIT-001]]"))
            .Write("software-requirements/MPRR-REQ-001.md", MemberRequirement("MPRR-REQ-001", "[[TEST-UNIT-001]], [[TEST-DOC-001|the manual]], [[TEST-001]]"))
            .Write("software-requirements/MPRR-REQ-002.md", MemberRequirement("MPRR-REQ-002", "[[MPRR-NFR-001]]"))
            .Write("software-requirements/MPRR-NFR-001.md", MemberRequirement("MPRR-NFR-001", "[[TEST-UNIT-002#case]]"))
            .Write("software-requirements/TEST-DOC-002.md", Pages.Marker("TEST-DOC-002", "retired", "x"))
            .Write("tests/TEST-UNIT-001.md", MemberTest("TEST-UNIT-001"))
            .Write("tests/TEST-UNIT-002.md", Pages.Marker("TEST-UNIT-002", "stub", "Not defined yet."))
            .Write("tests/TEST-DOC-001.md", MemberTest("TEST-DOC-001"))
            .Write("tests/MPRR-REQ-003.md", Pages.Marker("MPRR-REQ-003", "stub", "x"));
        var (_, connection, report, exit) = Project();

        Assert.Equal(ExitCodes.Findings, exit);
        Assert.True(JsonNode.DeepEquals(JsonNode.Parse("""{"source":"wiki","scopes":["software","system","test"]}"""), report["profile"]));
        Assert.True(JsonNode.DeepEquals(
            JsonNode.Parse("""{"system":{"sections":2},"software":{"sections":3,"retired":1},"test":{"sections":2,"stub":2}}"""), report["pages"]));
        Assert.True(JsonNode.DeepEquals(JsonNode.Parse("""{"verifies":4,"allocates":2}"""), report["edges"]));

        // An ID with another scope's prefix is outside its directory. A TestID link must have one of the test prefixes and
        // a CodeRef link one of the software prefixes; [[TEST-001]] has no prefix of this profile and is not a link.
        Assert.Equal(
            "software-requirements/MPRR-REQ-002.md|MPRR-NFR-001 verifies MPRR-REQ-002|link-outside-field;"
            + "software-requirements/TEST-DOC-002.md||id-outside-scope;"
            + "system-requirements/MPRR-SYS-002.md|MPRR-SYS-002 allocates TEST-UNIT-001|link-outside-field;"
            + "tests/MPRR-REQ-003.md||id-outside-scope",
            Listed(report["problems"]!));

        repo.Remove("system-requirements/MPRR-SYS-002.md").Remove("software-requirements/MPRR-REQ-002.md")
            .Remove("software-requirements/TEST-DOC-002.md").Remove("tests/MPRR-REQ-003.md");
        var commit = repo.Commit();
        Cli.Run(["seed", "--wiki", repo.Path, .. connection]);
        var test = JsonNode.Parse((string)server.Scalar(connection,
            $$"""SELECT wiki_v1.registry_envelope('{{commit}}', 'test', '{{Identity}}', '{"software":"aaaaaaaa-0000-4000-8000-000000000001","system":"aaaaaaaa-0000-4000-8000-000000000002"}')::text""")!)!;
        Assert.Equal(["TEST-DOC-001", "TEST-UNIT-001", "TEST-UNIT-002"], test["nodes"]!.AsArray().Select(node => (string)node!["id"]!));
        Assert.Equal(
            [
                "TEST-DOC-001 verifies MPRR-REQ-001 in aaaaaaaa-0000-4000-8000-000000000001",
                "TEST-DOC-001 verifies MPRR-SYS-001 in aaaaaaaa-0000-4000-8000-000000000002",
                "TEST-UNIT-001 verifies MPRR-REQ-001 in aaaaaaaa-0000-4000-8000-000000000001",
                "TEST-UNIT-002 verifies MPRR-NFR-001 in aaaaaaaa-0000-4000-8000-000000000001",
            ],
            test["edges"]!.AsArray().Select(edge => $"{edge!["from_id"]} {edge["type"]} {edge["to_id"]} in {edge["to_snapshot_uuid"]}"));
    }

    [Fact]
    public void KeepsAPrefixAndItsExtensionApart()
    {
        // T and T-X are different prefixes of one scope: T-X-001 has only the prefix T-X, and T-Y-001 has neither.
        repo.Write(".wiki-profile.json", """
                {"schema":"rsr/wiki-profile@2","scopes":[
                  {"scope":"software","directory":"software-requirements","idPrefix":"R","sections":["Requirement","TestID","CodeRef","Status"]},
                  {"scope":"test","directory":"tests","idPrefixes":["T","T-X"],"sections":["Stimulus","Required observation"]}]}
                """)
            .Write("software-requirements/R-001.md", MemberRequirement("R-001", "[[T-001]], [[T-X-001]], [[T-Y-001]]"))
            .Write("tests/T-001.md", MemberTest("T-001"))
            .Write("tests/T-X-001.md", MemberTest("T-X-001"))
            .Write("tests/T-Y-001.md", MemberTest("T-Y-001"));
        var (_, _, report, _) = Project();
        Assert.True(JsonNode.DeepEquals(JsonNode.Parse("""{"verifies":2}"""), report["edges"]));
        Assert.Equal("tests/T-Y-001.md||id-outside-scope", Listed(report["problems"]!));
    }

    [Theory]
    [InlineData("""{"schema":"rsr/wiki-profile@1","scopes":[{"scope":"test","directory":"d","idPrefix":"R","sections":["A"]}]}""")]
    [InlineData("""{"schema":"rsr/wiki-profile@2","scopes":[{"scope":"test","directory":"d","idPrefix":"R","sections":["A"]}]}""")]
    [InlineData("""{"schema":"rsr/wiki-profile@2","scopes":[{"scope":"test","directory":"d","idPrefixes":["R"],"sections":["A"]}]}""")]
    [InlineData("""{"schema":"rsr/wiki-profile@2","scopes":[{"scope":"test","directory":"d","idPrefixes":["R","R-UNIT","R-DOC"],"sections":["A"]}]}""")]
    [InlineData("""{"schema":"rsr/wiki-profile@2","scopes":[{"scope":"software","directory":"d","idPrefixes":["R","S"],"sections":["A"]},{"scope":"test","directory":"e","idPrefix":"R-T","sections":["A"]}]}""")]
    public void AcceptsEachFormOfAScopesPrefixes(string profile)
    {
        repo.Write(".wiki-profile.json", profile).Write("d/R-001.md", "# R-001\n\n## A\n\nx\n");
        var (_, _, report, exit) = Project();
        Assert.Equal((0, 0), (exit, (int)report["problemCount"]!));
        Assert.Equal("wiki", (string?)report["profile"]!["source"]);
        Assert.Equal(1, (int)report["pages"]!.AsObject().Single().Value!["sections"]!);
    }

    [Fact]
    public void RecognizesIdPagesByEveryPrefix()
    {
        // Outside the governed directories, a page named after an ID with any prefix of the profile is an ID page, which
        // needs no hub; TEST-007 and TEST-UNIT-07 are not IDs of this profile.
        repo.Write(".wiki-profile.json", MultiPrefixProfile)
            .Write("Hub.md", "# Hub\n\n<!-- table: | ID | Title | -->\n<!-- /table -->\n")
            .Write("tests/TEST-UNIT-001.md", MemberTest("TEST-UNIT-001"))
            .Write("tests/TEST-DOC-001.md", MemberTest("TEST-DOC-001"))
            .Write("archive/TEST-UNIT-007.md", "# TEST-UNIT-007\n\nx\n")
            .Write("archive/TEST-DOC-007.md", "# TEST-DOC-007\n\nx\n")
            .Write("archive/MPRR-NFR-007.md", "# MPRR-NFR-007\n\nx\n")
            .Write("archive/MPRR-SYS-007.md", "# MPRR-SYS-007\n\nx\n")
            .Write("archive/TEST-007.md", "# TEST-007\n\nx\n")
            .Write("archive/TEST-UNIT-07.md", "# TEST-UNIT-07\n\nx\n");
        var (_, _, report, exit) = Project();
        Assert.Equal((ExitCodes.Findings, 0), (exit, (int)report["problemCount"]!));
        Assert.Equal(["archive/TEST-007.md:page-not-indexed", "archive/TEST-UNIT-07.md:page-not-indexed"],
            report["wikiProblems"]!.AsArray().Select(problem => $"{problem!["path"]}:{problem["problem"]}"));
    }

    [Fact]
    public void KeepsRealisticRowEstimatesAfterEveryMigration()
    {
        // CREATE OR REPLACE resets a function's estimate to 1,000 rows, and PostgreSQL then JIT-compiles whole-wiki queries
        // (0005). ingest_snapshot and latest_snapshot never had an estimate.
        var connection = server.Migrated();
        Assert.Equal(
            "architecture_pages=50;changed_paths=10;export_snapshot=400;governed_edges=400;governed_pages=300;governed_scopes=3;"
            + "page_links=3;parse_architecture_page=1;parse_page=1;snapshot_profile=1;wiki_pages=400",
            server.Scalar(connection, """
                SELECT string_agg(p.proname::text || '=' || p.prorows::text, ';' ORDER BY p.proname)
                FROM pg_proc AS p
                WHERE p.pronamespace = 'wiki_v1'::regnamespace AND p.proretset AND p.proname NOT IN ('ingest_snapshot', 'latest_snapshot')
                """));
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
    [InlineData("""{"schema":"rsr/wiki-profile@1","scopes":[{"scope":"software","directory":"d","idPrefix":"R","sections":["A","ID"]}]}""")]
    [InlineData("""{"schema":"rsr/wiki-profile@1","scopes":[{"scope":"software","directory":"d","idPrefix":"R","sections":["Page kind"]}]}""")]
    [InlineData("""{"schema":"rsr/wiki-profile@1","scopes":[{"scope":"software","directory":"d","idPrefix":"R","sections":["A\u0000"]}]}""")]
    [InlineData("""{"schema":"rsr/wiki-profile@1","scopes":1e1000000}""")]
    [InlineData("""{"schema":"rsr/wiki-profile@1","scopes":[{"scope":"software","directory":"d","idPrefix":"R","sections":["A"]},{"scope":"software","directory":"e","idPrefix":"S","sections":["A"]}]}""")]
    [InlineData("""{"schema":"rsr/wiki-profile@1","scopes":[{"scope":"software","directory":"d","idPrefix":"R","sections":["A"]},{"scope":"test","directory":"d","idPrefix":"S","sections":["A"]}]}""")]
    [InlineData("""{"schema":"rsr/wiki-profile@1","scopes":[{"scope":"software","directory":"d","idPrefix":"R","sections":["A"]},{"scope":"test","directory":"e","idPrefix":"R","sections":["A"]}]}""")]
    [InlineData("""{"schema":"rsr/wiki-profile@3","scopes":[{"scope":"test","directory":"d","idPrefix":"R","sections":["A"]}]}""")]
    [InlineData("""{"schema":null,"scopes":[{"scope":"test","directory":"d","idPrefix":"R","sections":["A"]}]}""")]
    [InlineData("""{"schema":"rsr/wiki-profile@1","scopes":[{"scope":"test","directory":"d","idPrefixes":["R"],"sections":["A"]}]}""")]
    [InlineData("""{"schema":"rsr/wiki-profile@2","scopes":[{"scope":"test","directory":"d","idPrefix":"R","idPrefixes":["S"],"sections":["A"]}]}""")]
    [InlineData("""{"schema":"rsr/wiki-profile@2","scopes":[{"scope":"test","directory":"d","sections":["A"]}]}""")]
    [InlineData("""{"schema":"rsr/wiki-profile@2","scopes":[{"scope":"test","directory":"d","idPrefixes":["R"],"sections":["A"],"extra":1}]}""")]
    [InlineData("""{"schema":"rsr/wiki-profile@2","scopes":[{"scope":"test","directory":"d","idPrefixes":[],"sections":["A"]}]}""")]
    [InlineData("""{"schema":"rsr/wiki-profile@2","scopes":[{"scope":"test","directory":"d","idPrefixes":["R","R"],"sections":["A"]}]}""")]
    [InlineData("""{"schema":"rsr/wiki-profile@2","scopes":[{"scope":"test","directory":"d","idPrefixes":"R","sections":["A"]}]}""")]
    [InlineData("""{"schema":"rsr/wiki-profile@2","scopes":[{"scope":"test","directory":"d","idPrefixes":null,"sections":["A"]}]}""")]
    [InlineData("""{"schema":"rsr/wiki-profile@2","scopes":[{"scope":"test","directory":"d","idPrefixes":["R",1],"sections":["A"]}]}""")]
    [InlineData("""{"schema":"rsr/wiki-profile@2","scopes":[{"scope":"test","directory":"d","idPrefixes":["R","r"],"sections":["A"]}]}""")]
    [InlineData("""{"schema":"rsr/wiki-profile@2","scopes":[{"scope":"test","directory":"d","idPrefix":["R"],"sections":["A"]}]}""")]
    [InlineData("""{"schema":"rsr/wiki-profile@2","scopes":[{"scope":"test","directory":"d","idPrefix":"R-","sections":["A"]}]}""")]
    [InlineData("""{"schema":"rsr/wiki-profile@2","scopes":[{"scope":"test","directory":"d","idPrefixes":["R"],"sections":[]}]}""")]
    [InlineData("""{"schema":"rsr/wiki-profile@2","scopes":[{"scope":"test","directory":"d","idPrefixes":["TEST-UNIT","TEST-DOC","TEST-UNIT"],"sections":["A"]}]}""")]
    [InlineData("""{"schema":"rsr/wiki-profile@2","scopes":[{"scope":"software","directory":"d","idPrefixes":["R","S"],"sections":["A"]},{"scope":"test","directory":"e","idPrefix":"S","sections":["A"]}]}""")]
    [InlineData("""{"schema":"rsr/wiki-profile@2","scopes":[{"scope":"software","directory":"d","idPrefixes":["R","S"],"sections":["A"]},{"scope":"test","directory":"e","idPrefixes":["T","R"],"sections":["A"]}]}""")]
    [InlineData("""{"schema":"rsr/wiki-profile@2","scopes":[{"scope":"software","directory":"d","idPrefixes":["R"],"sections":["A"]},{"scope":"test","directory":"d","idPrefixes":["S"],"sections":["A"]}]}""")]
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
    public void ChecksALinkAgainstTheWholeIdOfItsField()
    {
        // REQ-X-001 starts with the test prefix REQ followed by '-', but it is a software ID, not a test ID.
        repo.Write(".wiki-profile.json", """
                {"schema":"rsr/wiki-profile@1","scopes":[
                  {"scope":"software","directory":"software-requirements","idPrefix":"REQ-X","sections":["Requirement","TestID"]},
                  {"scope":"test","directory":"tests","idPrefix":"REQ","sections":["Stimulus"]}]}
                """)
            .Write("software-requirements/REQ-X-001.md", "# REQ-X-001\n\n## Requirement\n\nx\n\n## TestID\n\n[[REQ-X-001]]\n");
        Assert.Equal("software-requirements/REQ-X-001.md|REQ-X-001 verifies REQ-X-001|link-outside-field",
            string.Join(";", Project().Report["problems"]!.AsArray().Select(problem => $"{problem!["path"]}|{problem["link"]}|{problem["problem"]}")));
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

    // Exports the latest recorded state into a new registry and returns each scope's result.
    private JsonObject ExportRegistry(string[] connection)
    {
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
            return JsonNode.Parse(output)!["scopes"]!.AsObject();
        }
        finally
        {
            File.Delete(approval);
        }
    }

    [Fact]
    public void ExportsOnlyTheProfileScopes()
    {
        repo.Write(".wiki-profile.json", MemberProfile)
            .Write("software-requirements/LBA-REQ-001.md", MemberRequirement("LBA-REQ-001", "[[T-001]]"))
            .Write("tests/T-001.md", MemberTest("T-001"));
        var scopes = ExportRegistry(Project().Connection);
        Assert.Equal(["software", "test"], scopes.Select(scope => scope.Key));
        Assert.Equal((1, 1), ((int)scopes["test"]!["nodes"]!, (int)scopes["test"]!["edges"]!));
    }

    [Fact]
    public void ExportsAScopeWithSeveralPrefixes()
    {
        repo.Write(".wiki-profile.json", MultiPrefixProfile)
            .Write("system-requirements/MPRR-SYS-001.md", MemberRequirement("MPRR-SYS-001", "[[TEST-DOC-001]]", "[[MPRR-REQ-001]], [[MPRR-NFR-001]]"))
            .Write("software-requirements/MPRR-REQ-001.md", MemberRequirement("MPRR-REQ-001", "[[TEST-UNIT-001]], [[TEST-DOC-001]]"))
            .Write("software-requirements/MPRR-NFR-001.md", MemberRequirement("MPRR-NFR-001", "[[TEST-UNIT-001]]"))
            .Write("tests/TEST-UNIT-001.md", MemberTest("TEST-UNIT-001"))
            .Write("tests/TEST-DOC-001.md", MemberTest("TEST-DOC-001"));
        var scopes = ExportRegistry(Project().Connection);
        Assert.Equal(["software", "system", "test"], scopes.Select(scope => scope.Key));
        Assert.Equal("software:2/0;system:1/2;test:2/4",
            string.Join(";", scopes.Select(scope => $"{scope.Key}:{scope.Value!["nodes"]}/{scope.Value["edges"]}")));
    }
}
