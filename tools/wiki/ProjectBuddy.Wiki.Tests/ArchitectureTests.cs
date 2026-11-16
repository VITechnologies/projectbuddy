using System.Text.Json.Nodes;
using Xunit;

namespace ProjectBuddy.Wiki.Tests;

public sealed class ArchitectureTests : IDisposable
{
    private readonly Server server = new();
    private readonly Repo repo = new();

    public void Dispose()
    {
        repo.Dispose();
        server.Dispose();
    }

    private (int Exit, JsonObject Report, string[] Connection) Project()
    {
        var connection = server.Migrated();
        repo.Write("tests/TEST-001.md", Pages.Marker("TEST-001", "stub", "x")).Commit();
        Assert.Equal(0, Cli.Run(["seed", "--wiki", repo.Path, .. connection]).Exit);
        var (exit, output, _) = Cli.Run(["project", .. connection]);
        return (exit, JsonNode.Parse(output)!.AsObject(), connection);
    }

    [Fact]
    public void ProjectsHubsAndPagesInOneLayout()
    {
        repo.Write("Architecture-Packet.md", "# Architecture Packet\n\nThe packet.\n\n<!-- sections -->\n- [[Context-View]]\n- [[Boundaries]]\n<!-- /sections -->\n")
            .Write("architecture/Context-View.md", "# Context View\n\n## Actors\n\n```text\n## not a section\n```\n\n")
            .Write("architecture/Boundaries.md", "# Boundaries\n\n<!-- sections -->\n- [[Context-View]]\n<!-- /sections -->\n")
            .Write("architecture/Tables.md", "# Tables\n\n<!-- table: | ID | Requirement | -->\n- [[REQ-001]]\n<!-- /table -->\n");
        var (exit, report, connection) = Project();
        Assert.Equal(0, exit);
        Assert.True(JsonNode.DeepEquals(JsonNode.Parse("""{"pages":4,"hubs":2}"""), report["architecture"]));
        Assert.Equal((0, true), ((int)report["architectureProblemCount"]!, (bool)report["exportable"]!));

        var snapshot = $"wiki_v1.commit_snapshot('{report["commit"]}')";
        Assert.Equal(true, server.Scalar(connection, $"""
            SELECT bool_and(wiki_v1.render_architecture_page(a.title, a.body, a.children) = convert_from(b.content, 'UTF8'))
            FROM wiki_v1.architecture_pages({snapshot}) AS a
            JOIN wiki_v1.snapshot_files AS f ON f.path = a.path AND f.snapshot_uuid = {snapshot} JOIN wiki_v1.blobs AS b ON b.sha256 = f.sha256
            """));
        Assert.Equal(
            "Architecture-Packet.md|The packet.|Context-View,Boundaries;architecture/Boundaries.md||Context-View;"
            + "architecture/Context-View.md|## Actors\n\n```text\n## not a section\n```\n|;architecture/Tables.md|<!-- table: | ID | Requirement | -->\n- [[REQ-001]]\n<!-- /table -->|",
            server.Scalar(connection, $"""
                SELECT string_agg(a.path || '|' || coalesce(a.body, '') || '|' || coalesce(array_to_string(a.children, ','), ''), ';' ORDER BY a.path COLLATE "C")
                FROM wiki_v1.architecture_pages({snapshot}) AS a
                """));
        Assert.Equal(true, server.Scalar(connection, $"SELECT a.body IS NULL FROM wiki_v1.architecture_pages({snapshot}) AS a WHERE a.path = 'architecture/Boundaries.md'"));
    }

    [Fact]
    public void ImplementsExactlyThePinnedStandard()
    {
        // Every finding-like literal the projection SQL can report, less the literals that are not findings, must be the
        // pinned standard's catalogue: a finding added, renamed or dropped without moving the pin fails here.
        string[] notFindings = ["projectbuddy-wiki-pages-v1", "software-requirements", "system-requirements"];
        var sql = string.Concat(Database.Migrations().Where(migration => migration.Version >= 3).Select(migration => migration.Sql));
        var literals = System.Text.RegularExpressions.Regex.Matches(sql, "'([a-z]+(?:-[a-z0-9]+)+)'").Select(match => match.Groups[1].Value);
        Assert.Equal(
            Standard.PageFindings.Concat(Standard.ArchitectureFindings).Concat(Standard.EdgeFindings).Distinct().Order(StringComparer.Ordinal),
            literals.Except(notFindings).Distinct().Order(StringComparer.Ordinal));

        var (_, report, _) = Project();
        Assert.Equal("""{"id":"rsr/ecosystem-wiki-standard@1","rsrRelease":"v0.5.0","rsrCommit":"0cab3803284aafc66f786d7a1114d58fc04fc5e6"}""",
            report["standard"]!.ToJsonString());
    }

    [Fact]
    public void RefusesADatabaseThatIsNotMigrated()
    {
        var unmigrated = server.NewDatabase();
        server.Execute([.. unmigrated[..^1], "postgres"], $"CREATE DATABASE \"{unmigrated[^1]}\"");
        var embedded = Database.Migrations().Count;
        var fresh = Cli.Run(["project", .. unmigrated]);
        Assert.Equal((ExitCodes.Refused, $"projectbuddy-wiki: the database has 0 of {embedded} migrations; run migrate"), (fresh.Exit, fresh.Error.Trim()));

        var behind = server.Migrated();
        server.Execute(behind, $"DELETE FROM public.wiki_schema_migrations WHERE version = {embedded}");
        Assert.Contains($"the database has {embedded - 1} of {embedded} migrations; run migrate", Cli.Run(["seed", "--wiki", repo.Path, .. behind]).Error);
    }

    [Fact]
    public void ListsArchitectureProblemsWithoutKeepingTheWikiFromTheRegistry()
    {
        repo.Write("architecture/notes.txt", "notes\n")
            .Write("architecture/sub/Nested.md", "# Nested\n\nx\n")
            .Write("architecture/No-Title.md", "Text\n")
            .Write("architecture/No-Newline.md", "# No Newline\n\nx")
            .Write("architecture/Binary.md", [0xff])
            .Write("architecture/Hub.md", "# Hub\n\nBody.\n\n<!-- sections -->\n- [[Missing-Page]]\n<!-- /sections -->\n")
            .Write("architecture/Trash-Hub.md", "# Trash Hub\n\n<!-- sections -->\n- [[Trashed]]\n<!-- /sections -->\n")
            .Write(".trash/Trashed.md", "# Trashed\n\nx\n")
            .Write("architecture/Tight-Hub.md", "# Tight Hub\n\nIntro\n<!-- sections -->\n- [[Hub]]\n<!-- /sections -->\n")
            .Write("architecture/Title-Only.md", "# Title Only\n\n");
        var (exit, report, _) = Project();
        Assert.Equal(ExitCodes.Findings, exit);
        Assert.Equal((true, 0, 9), ((bool)report["exportable"]!, (int)report["problemCount"]!, (int)report["architectureProblemCount"]!));
        Assert.Equal(
            [
                "architecture/Binary.md:not-utf8",
                "architecture/Hub.md:dangling-child",
                "architecture/No-Newline.md:no-final-newline",
                "architecture/No-Title.md:no-title",
                "architecture/Tight-Hub.md:misplaced-index-marker",
                "architecture/Title-Only.md:not-canonical",
                "architecture/Trash-Hub.md:dangling-child",
                "architecture/notes.txt:unexpected-file",
                "architecture/sub/Nested.md:unexpected-file",
            ],
            report["architectureProblems"]!.AsArray().Select(problem => $"{problem!["path"]}:{problem["problem"]}"));
    }
}
