using System.Text.Json;
using System.Text.Json.Nodes;
using Xunit;

namespace ProjectBuddy.Kpi.Tests;

public sealed class ParseTests
{
    private const string Password = "/run/pw";

    [Theory]
    [InlineData]
    [InlineData("publish")]
    [InlineData("report", "--bogus", "x", "--password-file", Password)]
    [InlineData("report", "--host", "h", "--host", "h", "--password-file", Password)]
    [InlineData("report", "--port")]
    [InlineData("report", "--port", "0", "--password-file", Password)]
    [InlineData("report", "--port", "x", "--password-file", Password)]
    [InlineData("report", "--user", "Admin", "--password-file", Password)]
    [InlineData("report", "--database", "kpi-local", "--password-file", Password)]
    [InlineData("report")]
    [InlineData("report", "--password-file", "relative")]
    [InlineData("mark", "--item", "o/r#1", "--password-file", Password)]
    [InlineData("migrate", "--create-database", "--create-database", "--password-file", Password)]
    [InlineData("report", "--session", "s", "--password-file", Password)]
    public void RejectsMalformedCommandLines(params string[] args)
    {
        var (exit, output, error) = Cli.Run(args);
        Assert.Equal(ExitCodes.Usage, exit);
        Assert.Empty(output);
        Assert.EndsWith(Program.Usage + Environment.NewLine, error);
    }

    [Fact]
    public void FillsInTheDefaults()
    {
        var options = Program.Parse(["ingest", "--password-file", Password]);
        Assert.Equal(("127.0.0.1", 5432, "postgres", "kpi_local"), (options.Host, options.Port, options.User, options.Database));
        Assert.Equal(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".copilot", "session-state"), options.Get("--sessions"));
        Assert.Equal(Path.GetFullPath("relative"), Program.Parse(["ingest", "--sessions", "relative", "--password-file", Password]).Get("--sessions"));
        Assert.Null(Program.Parse(["report", "--password-file", Password]).Get("--sessions"));
        Assert.Throws<KpiException>(() => Program.Integer(Program.Parse(["review", "--session", "s", "--item", "i", "--round", "x", "--findings", "0", "--password-file", Password]), "--round"));
        Assert.Throws<KpiException>(() => Program.Number(Program.Parse(["operator", "--tier", "T1", "--minutes", "x", "--activity", "review", "--password-file", Password]), "--minutes"));
    }

    [Fact]
    public void RefusesAnUnreadableOrEmptyPassword()
    {
        Assert.Contains("cannot read the password file", Cli.Run("report", "--password-file", "/nonexistent/password").Error);
        var empty = Path.GetTempFileName();
        File.WriteAllText(empty, "\n");
        try
        {
            Assert.Equal((ExitCodes.Refused, "projectbuddy-kpi: the password file is empty"), (Cli.Run("report", "--password-file", empty).Exit, Cli.Run("report", "--password-file", empty).Error.Trim()));
            File.WriteAllText(empty, "x");
            var (exit, _, error) = Cli.Run("report", "--port", "1", "--password-file", empty);
            Assert.Equal(ExitCodes.Database, exit);
            Assert.StartsWith("projectbuddy-kpi: database: ", error);
        }
        finally
        {
            File.Delete(empty);
        }
    }
}

public sealed class DatabaseTests : IDisposable
{
    private readonly Server server = new();

    public void Dispose() => server.Dispose();

    [Fact]
    public void MigratesOnceAndRefusesAChangedMigration()
    {
        var connection = server.NewDatabase();
        Assert.Contains("does not exist", Cli.Run(["migrate", .. connection]).Error);
        var first = Cli.Json(["migrate", "--create-database", .. connection]);
        Assert.Equal((true, "[\"0001_kpi_v1.sql\"]"), ((bool)first["created"]!, first["applied"]!.ToJsonString()));
        var again = Cli.Json(["migrate", "--create-database", .. connection]);
        Assert.Equal((false, "[\"0001_kpi_v1.sql\"]"), ((bool)again["created"]!, again["present"]!.ToJsonString()));
        server.Execute(connection, "UPDATE public.kpi_schema_migrations SET sha256 = repeat('0', 64)");
        Assert.Contains("migration 0001_kpi_v1.sql changed after it was applied", Cli.Run(["migrate", .. connection]).Error);
    }

    [Fact]
    public void RefusesADatabaseThatIsNotMigrated()
    {
        var connection = server.NewDatabase();
        server.Execute([.. connection[..^1], "postgres"], $"CREATE DATABASE \"{connection[^1]}\"");
        Assert.Equal((ExitCodes.Refused, "projectbuddy-kpi: the database has 0 of 1 migrations; run migrate"), (Cli.Run(["report", .. connection]).Exit, Cli.Run(["report", .. connection]).Error.Trim()));
        var behind = server.Migrated();
        server.Execute(behind, "DELETE FROM public.kpi_schema_migrations");
        Assert.Contains("has 0 of 1 migrations", Cli.Run(["report", .. behind]).Error);
    }

    [Fact]
    public void RecordsWhatTheAgentReports()
    {
        var connection = server.Migrated();
        const string Session = "11111111-2222-4333-8444-555555555555";
        Assert.Equal("mark", (string?)Cli.Json(["mark", "--session", Session, "--item", "o/r#1", .. connection])["recorded"]);
        Assert.Equal("gap", (string?)Cli.Json(["gap", "--session", Session, "--item", "o/r#1", "--kind", "code-read", "--fact", "The ID prefix", "--belongs-in", "README", .. connection])["recorded"]);
        Assert.Equal("review", (string?)Cli.Json(["review", "--session", Session, "--item", "o/r#1", "--round", "1", "--findings", "2", .. connection])["recorded"]);
        Cli.Json(["requirements", "--item", "o/r#1", "--count", "3", .. connection]);
        Assert.Equal(0L, (long)Cli.Json(["requirements", "--item", "o/r#1", "--count", "4", .. connection])["id"]!);
        Cli.Json(["operator", "--tier", "T4", "--minutes", "12.5", "--activity", "technician", .. connection]);
        Cli.Json(["operator", "--tier", "T1", "--minutes", "5", "--activity", "approvals", "--item", "o/r!2", "--note", "release", .. connection]);
        Assert.Equal(4, server.Scalar(connection, "SELECT requirements FROM kpi_v1.requirement_counts"));
        Assert.Equal(2L, server.Scalar(connection, "SELECT count(*) FROM kpi_v1.operator_entries"));
        var (exit, _, error) = Cli.Run(["mark", "--session", Session, "--item", "not a ref", .. connection]);
        Assert.Equal(ExitCodes.Database, exit);
        Assert.Contains("violates check constraint", error);
    }
}

public sealed class IngestTests : IDisposable
{
    private const string A = "aaaaaaaa-0000-4000-8000-000000000001";
    private const string B = "bbbbbbbb-0000-4000-8000-000000000002";

    private readonly Server server = new();
    private readonly string root = Directory.CreateTempSubdirectory("pbk-sessions-").FullName;

    public void Dispose()
    {
        Directory.Delete(root, recursive: true);
        server.Dispose();
    }

    private static JsonObject Counts(params string[] tools) => new(tools.Select(tool => new KeyValuePair<string, JsonNode?>(tool, 1)));

    [Fact]
    public void ReadsCallsToolsChangesMessagesAndSpans()
    {
        var log = new Log()
            .Event("session.start", 0, [])
            .Call(1, 15, Counts("view", "grep"))
            .Call(2, 15, Counts("ask_user", "bash"))
            .Call(3, 15, Counts("edit"))
            .Call(4, 15, Counts("task"))
            .Call(5, 15, Counts("bash"))
            .Call(6, 15, Counts("store_memory"))
            .Call(7, 15, Counts())
            .Call(8, 1.5m, new JsonArray(), initiator: "sub-agent", model: "claude-haiku-5.5")
            .Event("session.usage_record", 9, new JsonObject { ["usage"] = new JsonObject() })
            .Event("session.usage_record", 9.5, new JsonObject { ["usage"] = new JsonObject { ["cost"] = "x", ["inputTokens"] = "x" } })
            .Event("tool.execution_start", 9.6, new JsonObject { ["toolCallId"] = "text", ["toolName"] = "bash", ["arguments"] = "gh pr merge 3 --repo o/r" })
            .Event("tool.execution_complete", 9.7, new JsonObject { ["toolCallId"] = "text", ["success"] = true, ["result"] = new JsonObject { ["content"] = 7 } })
            .Tool("number", "bash", 9.8, "gh pr merge 4 --repo o/r")
            .Event("tool.execution_complete", 9.9, new JsonObject { ["toolCallId"] = "number", ["success"] = true, ["result"] = new JsonObject { ["content"] = 7 } })
            .Tool("merge", "bash", 10, "gh pr merge 5 --repo o/r --merge && glab mr merge 7 --yes -R g/p")
            .Done("merge", 11, content: "✓ Merged")
            .Tool("quiet", "bash", 12, "gh pr merge 6 --repo o/r").Done("quiet", 12, content: "nothing")
            .Tool("failed", "bash", 13, "gh pr merge 8 --repo o/r").Done("failed", 13, success: false, content: "MERGED")
            .Tool("norepo", "bash", 14, "gh pr merge 9").Done("norepo", 14, content: "MERGED")
            .Tool("args", "bash", 15).Done("args", 15, content: "MERGED")
            .Tool("view", "view", 16).Done("view", 16)
            .Done("unknown", 17)
            .Tool("open", "ask_user", 18)
            .Event("user.message", 19, [])
            .Raw("")
            .Raw($$"""{"timestamp": "{{Log.Time(20)}}", "type": "assistant.turn_start", "data": {}, "id": "{{Guid.NewGuid()}}"}""")
            .Event("assistant.message", 90, []);
        var session = Ingest.Read(A, log.Reader(), TimeSpan.FromMinutes(30), "log")!;

        Assert.Equal((Log.Start, Log.Start.AddMinutes(90), 32L), (session.First, session.Last, session.Events));
        Assert.Equal(["explore", "ask", "edit", "delegate", "run", "memory", "respond", "respond", "respond", "respond"], session.Calls.Select(call => call.Category));
        Assert.Equal((0m, 0L), (session.Calls[9].Cost, session.Calls[9].InputTokens));
        Assert.Equal(("sub-agent", "claude-haiku-5.5", 1.5m), (session.Calls[7].Initiator, session.Calls[7].Model, session.Calls[7].Cost));
        Assert.Equal(("unknown", "unknown", 0m, 0L), (session.Calls[8].Initiator, session.Calls[8].Model, session.Calls[8].Cost, session.Calls[8].InputTokens));
        Assert.Equal(["o/r#5", "g/p!7"], session.Acceptances.Select(acceptance => acceptance.ChangeRef));
        Assert.All(session.Acceptances, acceptance => Assert.Equal(Log.Start.AddMinutes(11), acceptance.At));
        Assert.Equal(9, session.Tools.Count);
        Assert.Equal(("ask_user", (DateTimeOffset?)null, (bool?)null), (session.Tools[^1].Tool, session.Tools[^1].Completed, session.Tools[^1].Success));
        Assert.Single(session.Messages);
        Assert.Equal([(Log.Start, Log.Start.AddMinutes(20)), (Log.Start.AddMinutes(90), Log.Start.AddMinutes(90))], session.Spans);
        Assert.Null(Ingest.Read(A, new StringReader(""), TimeSpan.FromMinutes(30), "log"));
    }

    [Fact]
    public void RefusesALineThatIsNotAnEvent()
    {
        var refused = Assert.Throws<KpiException>(() => Ingest.Read(A, new Log().Event("x", 0, []).Raw("{not json").Reader(), TimeSpan.FromMinutes(30), "events.jsonl"));
        Assert.StartsWith("events.jsonl: line 2 is not a readable event", refused.Message);
    }

    [Fact]
    public void StoresEachFactOnceAndCompletesOpenToolCalls()
    {
        var connection = server.Migrated();
        Assert.Equal(ExitCodes.Refused, Cli.Run(["ingest", "--sessions", Path.Combine(root, "missing"), .. connection]).Exit);
        new Log().Call(0, 15, Counts("view")).Tool("open", "ask_user", 1).Tool("merge", "bash", 2, "gh pr merge 5 --repo o/r").Done("merge", 3, content: "MERGED").Write(root, A);
        new Log().Event("x", 0, []).Write(root, "not-a-session");
        Directory.CreateDirectory(Path.Combine(root, B));
        File.WriteAllText(Path.Combine(root, "cccccccc-0000-4000-8000-000000000003", "events.jsonl") is var empty && Directory.CreateDirectory(Path.GetDirectoryName(empty)!) is not null ? empty : empty, "");

        var first = Cli.Json(["ingest", "--sessions", root, .. connection])["sessions"]!.AsArray();
        Assert.Equal((A, 1, 1), ((string?)first.Single()!["session"], (int)first.Single()!["newModelCalls"]!, (int)first.Single()!["newAcceptances"]!));
        var again = Cli.Json(["ingest", "--sessions", root, .. connection])["sessions"]!.AsArray().Single()!;
        Assert.Equal((0, 0), ((int)again["newModelCalls"]!, (int)again["newAcceptances"]!));

        new Log().Done("open", 25).Write(root, A);
        Cli.Json(["ingest", "--sessions", root, .. connection]);
        Assert.Equal(24.0, Convert.ToDouble(server.Scalar(connection, "SELECT extract(epoch FROM completed_at - started_at) / 60 FROM kpi_v1.tool_calls WHERE tool_call_id = 'open'")));
        Assert.Equal(1L, server.Scalar(connection, "SELECT count(*) FROM kpi_v1.active_spans"));

        new Log().Raw("{broken").Write(root, A);
        Assert.Contains("is not a readable event", Cli.Run(["ingest", "--sessions", root, .. connection]).Error);
    }
}

public sealed class ReportTests : IDisposable
{
    private const string A = "aaaaaaaa-0000-4000-8000-000000000001";
    private const string B = "bbbbbbbb-0000-4000-8000-000000000002";

    private readonly Server server = new();
    private readonly string root = Directory.CreateTempSubdirectory("pbk-sessions-").FullName;

    public void Dispose()
    {
        Directory.Delete(root, recursive: true);
        server.Dispose();
    }

    [Fact]
    public void ReportsEveryKpi()
    {
        var connection = server.Migrated();
        var empty = Cli.Json(["report", .. connection]);
        Assert.Equal((0m, (JsonNode?)null, 0), ((decimal)empty["totalUsd"]!, empty["explorationShare"], (int)empty["acceptedChanges"]!["count"]!));

        // Session A: two changes. Calls at 1 and 2 minutes, accepted at 3; a call at 5, accepted at 6. An ask_user
        // prompt answered after 20 minutes counts 10.
        new Log()
            .Call(1, 15, new JsonObject { ["view"] = 1 })
            .Call(2, 15, new JsonObject { ["edit"] = 1 })
            .Tool("m1", "bash", 2.5, "gh pr merge 1 --repo o/r").Done("m1", 3, content: "MERGED")
            .Call(5, 3, new JsonObject { ["bash"] = 1 }, initiator: "sub-agent")
            .Tool("m2", "bash", 5.5, "gh pr merge 2 --repo o/r").Done("m2", 6, content: "MERGED")
            .Tool("ask", "ask_user", 6).Done("ask", 26)
            .Write(root, A);
        // Session B overlaps A in time, and prints o/r#1 merged again later: neither counts twice.
        new Log().Event("x", 1, []).Tool("again", "bash", 7, "gh pr merge 1 --repo o/r").Done("again", 8, content: "MERGED").Write(root, B);
        Cli.Json(["ingest", "--sessions", root, .. connection]);

        // Markers attribute calls to items; o/r#1's first review (findings 2) is after its first call.
        server.Execute(connection, $"""
            INSERT INTO kpi_v1.work_markers (session_id, at, item) VALUES ('{A}', '{Log.Time(0.5)}', 'o/r#1'), ('{A}', '{Log.Time(4)}', 'o/r#2');
            INSERT INTO kpi_v1.reviews (session_id, at, item, round, findings) VALUES ('{A}', '{Log.Time(1.5)}', 'o/r#1', 1, 2), ('{A}', '{Log.Time(5.5)}', 'o/r#2', 1, 0);
            INSERT INTO kpi_v1.requirement_counts VALUES ('o/r#1', 2);
            INSERT INTO kpi_v1.spec_gaps (session_id, item, kind, fact, belongs_in) VALUES ('{A}', 'o/r#1', 'operator-question', 'Which tier', 'README');
            """);
        Cli.Json(["operator", "--tier", "T4", "--minutes", "30", "--activity", "technician", .. connection]);

        var report = Cli.Json(["report", .. connection]);
        Assert.Equal((2, 3, 33m, 1.32m), ((int)report["sessions"]!, (int)report["ai"]!["calls"]!, (decimal)report["ai"]!["units"]!, (decimal)report["ai"]!["usd"]!));
        // Active time is the union of A (1 to 26 minutes) and B (1 to 8 minutes): 25 minutes at T1 (62.50 USD), plus 30
        // minutes at T4 (22.50 USD).
        Assert.Equal((0.42m, 0.5m, 85m, 1, 10.0m), ((decimal)report["operator"]!["activeHours"]!, (decimal)report["operator"]!["entryHours"]!,
            (decimal)report["operator"]!["usd"]!, (int)report["operator"]!["prompts"]!, (decimal)report["operator"]!["promptMinutesCapped"]!));
        Assert.Equal(86.32m, (decimal)report["totalUsd"]!);
        Assert.Equal((30m, 3m), ((decimal)report["unitsByInitiator"]!["agent"]!, (decimal)report["unitsByInitiator"]!["sub-agent"]!));
        Assert.Equal(33m, (decimal)report["unitsByModel"]!["claude-opus-5.5"]!);
        Assert.Equal((15m, 15m, 3m), ((decimal)report["unitsByCategory"]!["explore"]!, (decimal)report["unitsByCategory"]!["edit"]!, (decimal)report["unitsByCategory"]!["run"]!));
        Assert.Equal(0.4545m, (decimal)report["explorationShare"]!);

        // o/r#1: calls at 1 and 2 (30 units), 3 minutes of A; o/r#2: the call at 5 (3 units), 3 minutes.
        Assert.Equal(2, (int)report["acceptedChanges"]!["count"]!);
        var recent = report["recentChanges"]!.AsArray();
        Assert.Equal(["o/r#2", "o/r#1"], recent.Select(change => (string)change!["change"]!));
        Assert.Equal((1, 3m, 0.12m, 0.05m, 7.62m), ((int)recent[0]!["calls"]!, (decimal)recent[0]!["units"]!, (decimal)recent[0]!["aiUsd"]!,
            (decimal)recent[0]!["operatorHours"]!, (decimal)recent[0]!["totalUsd"]!));
        Assert.Equal((2, 30m, 1.2m), ((int)recent[1]!["calls"]!, (decimal)recent[1]!["units"]!, (decimal)recent[1]!["aiUsd"]!));

        // Items: o/r#1 costs 1.20 USD for 2 requirements; its rework is the call after its first review (15 units).
        Assert.Equal((0.6m, 0.5m, 15m), ((decimal)report["costPerRequirementUsd"]!, (decimal)report["firstPassYield"]!, (decimal)report["reworkUnits"]!));
        Assert.Equal((1, 0.5m), ((int)report["specGaps"]!["count"]!, (decimal)report["specGaps"]!["perAcceptedChange"]!));
    }
}
