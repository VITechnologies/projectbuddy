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
        Assert.Equal((true, "[\"0001_kpi_v1.sql\",\"0002_ai_units.sql\"]"), ((bool)first["created"]!, first["applied"]!.ToJsonString()));
        var again = Cli.Json(["migrate", "--create-database", .. connection]);
        Assert.Equal((false, "[\"0001_kpi_v1.sql\",\"0002_ai_units.sql\"]"), ((bool)again["created"]!, again["present"]!.ToJsonString()));
        server.Execute(connection, "UPDATE public.kpi_schema_migrations SET sha256 = repeat('0', 64)");
        Assert.Contains("migration 0001_kpi_v1.sql changed after it was applied", Cli.Run(["migrate", .. connection]).Error);
    }

    [Fact]
    public void RefusesADatabaseThatIsNotMigrated()
    {
        var connection = server.NewDatabase();
        server.Execute([.. connection[..^1], "postgres"], $"CREATE DATABASE \"{connection[^1]}\"");
        Assert.Equal((ExitCodes.Refused, "projectbuddy-kpi: the database has 0 of 2 migrations; run migrate"), (Cli.Run(["report", .. connection]).Exit, Cli.Run(["report", .. connection]).Error.Trim()));
        var behind = server.Migrated();
        server.Execute(behind, "DELETE FROM public.kpi_schema_migrations");
        Assert.Contains("has 0 of 2 migrations", Cli.Run(["report", .. behind]).Error);
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
        Cli.Json(["track", "--repository", "o/r", "--forge", "github", .. connection]);
        Assert.Equal("track", (string?)Cli.Json(["track", "--repository", "g/s/p", "--forge", "gitlab", .. connection])["recorded"]);
        Assert.Contains("violates check constraint", Cli.Run(["track", "--repository", "g/s/p", "--forge", "github", .. connection]).Error);
        Assert.Equal("{\"since\":null,\"repositories\":[],\"unresolved\":[]}", Cli.Json(["resolve", .. connection]).ToJsonString());
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
    public void ReadsCallsToolsMessagesAndSpans()
    {
        // A forked session repeats its parent's history after its own start: those events are skipped.
        var log = new Log()
            .Event("session.start", 0, [])
            .Call(-30, 15, Counts("view"))
            .Tool("copied", "bash", -29).Done("copied", -28)
            .Event("assistant.message", -27, [])
            .Event("session.start", -26, [])
            .Call(1, 15, Counts("view", "grep"))
            .Call(2, 15, Counts("ask_user", "bash"))
            .Call(3, 15, Counts("edit"))
            .Call(4, 15, Counts("task"))
            .Call(5, 15, Counts("bash"))
            .Call(6, 15, Counts("store_memory"))
            .Call(7, 15, Counts())
            .Call(8, 1.5m, new JsonArray(), initiator: "sub-agent", model: "claude-haiku-5.5")
            .Event("session.usage_record", 9, new JsonObject { ["usage"] = new JsonObject() })
            .Event("session.usage_record", 9.5, new JsonObject { ["usage"] = new JsonObject { ["initiator"] = 5, ["cost"] = "x", ["inputTokens"] = "x" } })
            .Tool("run", "bash", 10).Done("run", 11)
            .Tool("failed", "bash", 13).Done("failed", 13, success: false)
            .Tool("view", "view", 16).Done("view", 16)
            .Done("never-started", 17)
            .Tool("open", "ask_user", 18)
            .Event("user.message", 19, [])
            .Raw("")
            .Raw($$"""{"type":"assistant.turn_start","data":{},"id":"{{Guid.NewGuid()}}","timestamp": "{{Log.Time(20)}}"}""")
            .Event("assistant.message", 90, []);
        var session = Ingest.Read(A, log.Reader(), TimeSpan.FromMinutes(30), "log")!;

        Assert.Equal((Log.Start, Log.Start.AddMinutes(90), 22L), (session.First, session.Last, session.Events));
        Assert.Equal(["explore", "ask", "edit", "delegate", "run", "memory", "respond", "respond", "respond", "respond"], session.Calls.Select(call => call.Category));
        Assert.Equal((0m, 0L), (session.Calls[9].Cost, session.Calls[9].InputTokens));
        Assert.Equal(("sub-agent", "claude-haiku-5.5", 1.5m), (session.Calls[7].Initiator, session.Calls[7].Model, session.Calls[7].Cost));
        Assert.Equal(("unknown", "unknown", 0m, 0L), (session.Calls[8].Initiator, session.Calls[8].Model, session.Calls[8].Cost, session.Calls[8].InputTokens));
        Assert.Equal("unknown", session.Calls[9].Initiator);
        Assert.Equal(["run", "failed", "view", "open"], session.Tools.Select(tool => tool.Id));
        Assert.Equal([true, false, true, null], session.Tools.Select(tool => tool.Success));
        Assert.Equal(("ask_user", (DateTimeOffset?)null), (session.Tools[^1].Tool, session.Tools[^1].Completed));
        Assert.Single(session.Messages);
        Assert.Equal([(Log.Start, Log.Start.AddMinutes(20)), (Log.Start.AddMinutes(90), Log.Start.AddMinutes(90))], session.Spans);
        Assert.Null(Ingest.Read(A, new StringReader(""), TimeSpan.FromMinutes(30), "log"));
        Assert.Null(session.Aiu);
        Assert.All(session.Calls, call => Assert.Null(call.Aiu));
    }

    [Fact]
    public void ApportionsMeasuredAiuAmongTheCalls()
    {
        // An agent session forked from a parent whose copied checkpoint is 100 AIU. The 30 AIU measured at 3 minutes are
        // shared by weight: 69 for the harness's tokens, 207 for 207 uncached input tokens. The counter then restarts at
        // 10 AIU for the call at 4. The call at 6 is not measured yet. Assistant messages are not its calls.
        var agent = new Log()
            .Event("session.start", 0, [])
            .Checkpoint(-10, 100)
            .Checkpoint(-9, null)
            .Event("assistant.message", 0.5, new JsonObject { ["model"] = "m", ["outputTokens"] = 5 })
            .Call(1, 15, Counts("view"))
            .Event("session.usage_record", 2, new JsonObject { ["usage"] = new JsonObject { ["model"] = "claude-opus-5.5", ["inputTokens"] = 207, ["cost"] = 15 } })
            .Event("assistant.message", 2.5, new JsonObject { ["model"] = "m" })
            .Checkpoint(3, 130)
            .Checkpoint(3.5, null)
            .Call(4, 15, Counts("bash"))
            .Checkpoint(5, 10)
            .Shutdown(5.5, 999)
            .Call(6, 15, Counts());
        var session = Ingest.Read(A, agent.Reader(), TimeSpan.FromMinutes(30), "log")!;
        Assert.Equal([7.5m, 22.5m, 10m, null], session.Calls.Select(call => call.Aiu));
        Assert.Equal(40m, session.Aiu);

        // A CLI session: calls are its assistant messages, a sub-agent's naming its tool call, and each process's
        // shutdown total is shared by output tokens, or equally when they have none.
        var cli = new Log()
            .Event("session.start", 0, [])
            .Event("assistant.message", 1, new JsonObject
            {
                ["model"] = "claude-opus-5.5", ["outputTokens"] = 30, ["parentToolCallId"] = "t1",
                ["toolRequests"] = new JsonArray(new JsonObject { ["name"] = "bash" }, new JsonObject { ["name"] = 7 }),
            })
            .Event("assistant.message", 2, new JsonObject { ["model"] = "claude-opus-5.5", ["outputTokens"] = 10, ["toolRequests"] = "none" })
            .Shutdown(3, 8)
            .Shutdown(3.5, null)
            .Event("assistant.message", 4, new JsonObject { ["outputTokens"] = 0 })
            .Event("assistant.message", 4.5, [])
            .Shutdown(5, 5);
        var run = Ingest.Read(B, cli.Reader(), TimeSpan.FromMinutes(30), "log")!;
        Assert.Equal([6m, 2m, 2.5m, 2.5m], run.Calls.Select(call => call.Aiu));
        Assert.Equal(13m, run.Aiu);
        Assert.Equal(["sub-agent", "agent", "agent", "agent"], run.Calls.Select(call => call.Initiator));
        Assert.Equal(["run", "respond", "respond", "respond"], run.Calls.Select(call => call.Category));
        Assert.Equal(("claude-opus-5.5", "unknown", 0m), (run.Calls[0].Model, run.Calls[3].Model, run.Calls.Sum(call => call.Cost)));
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
        new Log().Call(0, 15, Counts("view")).Tool("open", "ask_user", 1).Tool("run", "bash", 2).Done("run", 3).Write(root, A);
        new Log().Event("x", 0, []).Write(root, "not-a-session");
        Directory.CreateDirectory(Path.Combine(root, B));
        File.WriteAllText(Path.Combine(root, "cccccccc-0000-4000-8000-000000000003", "events.jsonl") is var empty && Directory.CreateDirectory(Path.GetDirectoryName(empty)!) is not null ? empty : empty, "");

        var first = Cli.Json(["ingest", "--sessions", root, .. connection])["sessions"]!.AsArray();
        Assert.Equal((A, 1), ((string?)first.Single()!["session"], (int)first.Single()!["newModelCalls"]!));
        var again = Cli.Json(["ingest", "--sessions", root, .. connection])["sessions"]!.AsArray().Single()!;
        Assert.Equal(0, (int)again["newModelCalls"]!);

        new Log().Done("open", 25).Checkpoint(26, 15).Write(root, A);
        Cli.Json(["ingest", "--sessions", root, .. connection]);
        Assert.Equal(24.0, Convert.ToDouble(server.Scalar(connection, "SELECT extract(epoch FROM completed_at - started_at) / 60 FROM kpi_v1.tool_calls WHERE tool_call_id = 'open'")));
        Assert.Equal(1L, server.Scalar(connection, "SELECT count(*) FROM kpi_v1.active_spans"));
        Assert.Equal("15 15", server.Scalar(connection, "SELECT c.aiu || ' ' || s.aiu FROM kpi_v1.model_calls AS c JOIN kpi_v1.sessions AS s USING (session_id)"));

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

        // Session A: two changes. Calls at 1 and 2 minutes (30 AIU measured at 2.2), accepted at 3; a call at 5 (3 AIU,
        // measured at 5.2), accepted at 6. Its nominal costs are the same, 15, 15 and 3. An ask_user
        // prompt answered after 20 minutes counts 10.
        new Log()
            .Call(1, 15, new JsonObject { ["view"] = 1 })
            .Call(2, 15, new JsonObject { ["edit"] = 1 })
            .Checkpoint(2.2, 30)
            .Tool("m1", "bash", 2.5, "gh pr merge 1 --repo o/r").Done("m1", 3)
            .Call(5, 3, new JsonObject { ["bash"] = 1 }, initiator: "sub-agent")
            .Checkpoint(5.2, 33)
            .Tool("m2", "bash", 5.5, "gh pr merge 2 --repo o/r").Done("m2", 6)
            .Tool("ask", "ask_user", 6).Done("ask", 26)
            .Write(root, A);
        // Session B overlaps A in time but ran no tool before either merge, so A accepts both. o/r#9 merged while no
        // session was active.
        new Log().Event("x", 1, []).Tool("again", "bash", 7, "gh pr merge 1 --repo o/r").Done("again", 8).Write(root, B);
        Cli.Json(["ingest", "--sessions", root, .. connection]);
        server.Execute(connection, $"""
            INSERT INTO kpi_v1.forge_changes (change_ref, merged_at)
            VALUES ('o/r#1', '{Log.Time(2.9)}'), ('o/r#2', '{Log.Time(5.9)}'), ('o/r#9', '{Log.Time(300)}')
            """);

        // Markers attribute calls to items; o/r#1's first review (findings 2) is after its first call.
        server.Execute(connection, $"""
            INSERT INTO kpi_v1.work_markers (session_id, at, item) VALUES ('{A}', '{Log.Time(0.5)}', 'o/r#1'), ('{A}', '{Log.Time(4)}', 'o/r#2');
            INSERT INTO kpi_v1.reviews (session_id, at, item, round, findings) VALUES ('{A}', '{Log.Time(1.5)}', 'o/r#1', 1, 2), ('{A}', '{Log.Time(5.5)}', 'o/r#2', 1, 0);
            INSERT INTO kpi_v1.requirement_counts VALUES ('o/r#1', 2);
            INSERT INTO kpi_v1.spec_gaps (session_id, item, kind, fact, belongs_in) VALUES ('{A}', 'o/r#1', 'operator-question', 'Which tier', 'README');
            """);
        Cli.Json(["operator", "--tier", "T4", "--minutes", "30", "--activity", "technician", .. connection]);

        var report = Cli.Json(["report", .. connection]);
        Assert.Equal((2, 3, 33m, 1.32m), ((int)report["sessions"]!, (int)report["ai"]!["calls"]!, (decimal)report["ai"]!["aiu"]!, (decimal)report["ai"]!["usd"]!));
        Assert.Equal((3, 33m, 33m), ((int)report["ai"]!["measuredCalls"]!, (decimal)report["ai"]!["nominalUnits"]!, (decimal)report["ai"]!["measuredAiu"]!));
        // Active time is the union of A (1 to 26 minutes) and B (1 to 8 minutes): 25 minutes at T1 (62.50 USD), plus 30
        // minutes at T4 (22.50 USD).
        Assert.Equal((0.42m, 0.5m, 85m, 1, 10.0m), ((decimal)report["operator"]!["activeHours"]!, (decimal)report["operator"]!["entryHours"]!,
            (decimal)report["operator"]!["usd"]!, (int)report["operator"]!["prompts"]!, (decimal)report["operator"]!["promptMinutesCapped"]!));
        Assert.Equal(86.32m, (decimal)report["totalUsd"]!);
        Assert.Equal((30m, 3m), ((decimal)report["aiuByInitiator"]!["agent"]!, (decimal)report["aiuByInitiator"]!["sub-agent"]!));
        Assert.Equal(33m, (decimal)report["aiuByModel"]!["claude-opus-5.5"]!);
        Assert.Equal((15m, 15m, 3m), ((decimal)report["aiuByCategory"]!["explore"]!, (decimal)report["aiuByCategory"]!["edit"]!, (decimal)report["aiuByCategory"]!["run"]!));
        Assert.Equal(0.4545m, (decimal)report["explorationShare"]!);
        Assert.Equal((0, 3, 1), ((int)report["merges"]!["trackedRepositories"]!, (int)report["merges"]!["merged"]!, (int)report["merges"]!["outsideSessions"]!));

        // o/r#1: calls at 1 and 2 (30 AIU), 3 minutes of A; o/r#2: the call at 5 (3 AIU), 3 minutes.
        Assert.Equal(2, (int)report["acceptedChanges"]!["count"]!);
        var recent = report["recentChanges"]!.AsArray();
        Assert.Equal(["o/r#2", "o/r#1"], recent.Select(change => (string)change!["change"]!));
        Assert.Equal((1, 3m, 0.12m, 0.05m, 7.62m), ((int)recent[0]!["calls"]!, (decimal)recent[0]!["aiu"]!, (decimal)recent[0]!["aiUsd"]!,
            (decimal)recent[0]!["operatorHours"]!, (decimal)recent[0]!["totalUsd"]!));
        Assert.Equal((2, 30m, 1.2m), ((int)recent[1]!["calls"]!, (decimal)recent[1]!["aiu"]!, (decimal)recent[1]!["aiUsd"]!));

        // Items: o/r#1 costs 1.20 USD for 2 requirements; its rework is the call after its first review (15 AIU).
        Assert.Equal((0.6m, 0.5m, 15m), ((decimal)report["costPerRequirementUsd"]!, (decimal)report["firstPassYield"]!, (decimal)report["reworkAiu"]!));
        Assert.Equal((1, 0.5m), ((int)report["specGaps"]!["count"]!, (decimal)report["specGaps"]!["perAcceptedChange"]!));
    }
}

public sealed class ForgeTests : IDisposable
{
    private const string A = "aaaaaaaa-0000-4000-8000-000000000001";
    private const string B = "bbbbbbbb-0000-4000-8000-000000000002";
    private const string C = "cccccccc-0000-4000-8000-000000000003";

    private readonly Server server = new();
    private readonly string root = Directory.CreateTempSubdirectory("pbk-forge-").FullName;

    public void Dispose()
    {
        Directory.Delete(root, recursive: true);
        server.Dispose();
    }

    private static string Name(string path) => string.Concat(path.Select(c => "/?&=%:".Contains(c) ? '_' : c));

    // A stand-in for gh and glab: `<tool> api <path>` prints the answer saved for that path, or fails as a 404.
    private string Fake()
    {
        var path = Path.Combine(root, "forge");
        File.WriteAllText(path, $$"""
            #!/bin/sh
            f="{{root}}/answers/$(printf '%s' "$2" | tr '/?&=%:' '______')"
            if [ -f "$f" ]; then cat "$f"; else echo 'HTTP 404: Not Found' >&2; echo 'more' >&2; exit 1; fi

            """);
        if (!OperatingSystem.IsWindows())
        {
            File.SetUnixFileMode(path, UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute);
        }

        return path;
    }

    private void Answer(string path, JsonNode answer) =>
        File.WriteAllText(Path.Combine(Directory.CreateDirectory(Path.Combine(root, "answers")).FullName, Name(path)), answer.ToJsonString());

    private static JsonObject Request(string key, long number, double? merged, double updated) =>
        new() { [key] = number, ["merged_at"] = merged is { } at ? Log.Time(at) : null, ["updated_at"] = Log.Time(updated) };

    private static string GitHub(string repository, int page) =>
        $"repos/{repository}/pulls?state=closed&sort=updated&direction=desc&per_page=100&page={page}";

    [Fact]
    public void AcceptsWhatTheForgeMergedWhileASessionWasActive()
    {
        var connection = server.Migrated();
        var sessions = Directory.CreateDirectory(Path.Combine(root, "sessions")).FullName;
        new Log().Event("session.start", 0, []).Tool("a1", "bash", 2).Done("a1", 3).Tool("a2", "bash", 6).Done("a2", 6.2).Event("x", 10, []).Write(sessions, A);
        new Log().Event("session.start", 5, []).Tool("b1", "bash", 7).Done("b1", 8).Event("x", 12, []).Write(sessions, B);
        new Log().Event("session.start", 200, []).Tool("c1", "bash", 201).Done("c1", 201).Write(sessions, C);
        Cli.Json(["ingest", "--sessions", sessions, .. connection]);
        foreach (var (repository, forge) in new[] { ("o/r", "github"), ("g/s/p", "gitlab"), ("o/missing", "github"), ("o/bad", "github") })
        {
            Cli.Json(["track", "--repository", repository, "--forge", forge, .. connection]);
        }

        // o/r, page 1 (full): #1 merged while only A was active; #2 while both were, after B's latest tool call; #3 closed
        // unmerged; #4 while no session was active; #5 within the skew before C's first event. Page 2 (full) reaches
        // updates older than the first session, so listing stops there (a third page would fail): #102 merged within the
        // skew of A's span but after B's tool call; #101 merged before any session.
        var first = new JsonArray(Request("number", 1, 2.9, 2.9), Request("number", 2, 7.5, 7.5), Request("number", 3, null, 8),
            Request("number", 4, 50, 50), Request("number", 5, 199, 199));
        var second = new JsonArray(Request("number", 102, 11, 11), Request("number", 101, -10, -10));
        for (var n = 6; n <= 100; n++)
        {
            first.Add(Request("number", n, null, 9));
        }

        for (var n = 103; n <= 200; n++)
        {
            second.Add(Request("number", n, null, 9));
        }

        Answer(GitHub("o/r", 1), first);
        Answer(GitHub("o/r", 2), second);
        Answer($"projects/g%2Fs%2Fp/merge_requests?state=merged&order_by=updated_at&sort=desc&updated_after={Log.Time(0)[..19]}Z&per_page=100&page=1",
            new JsonArray(Request("iid", 7, 6.5, 6.5)));
        Answer(GitHub("o/bad", 1), new JsonObject { ["message"] = "not a list" });
        var fake = Fake();

        var resolved = Cli.Json(["resolve", "--gh", fake, "--glab", fake, .. connection]);
        Assert.Equal(Log.Start, (DateTimeOffset)resolved["since"]!);
        Assert.Equal("""[{"repository":"g/s/p","merged":1},{"repository":"o/r","merged":5}]""", resolved["repositories"]!.ToJsonString());
        var unresolved = resolved["unresolved"]!.AsArray();
        Assert.Equal(["o/bad", "o/missing"], unresolved.Select(entry => (string)entry!["repository"]!));
        Assert.StartsWith("unexpected answer: ", (string)unresolved[0]!["reason"]!);
        Assert.Equal("forge exited 1: HTTP 404: Not Found", (string)unresolved[1]!["reason"]!);
        Assert.Equal("g/s/p!7=a o/r#1=a o/r#102=b o/r#2=b o/r#5=c", server.Scalar(connection,
            "SELECT string_agg(change_ref || '=' || left(session_id, 1), ' ' ORDER BY change_ref COLLATE \"C\") FROM kpi_v1.accepted_changes"));
        var merges = Cli.Json(["report", .. connection])["merges"]!;
        Assert.Equal((4, 6, 1), ((int)merges["trackedRepositories"]!, (int)merges["merged"]!, (int)merges["outsideSessions"]!));
        Assert.Equal("0 0", server.Scalar(connection, "SELECT calls || ' ' || operator_hours FROM kpi_v1.change_costs WHERE change_ref = 'o/r#5'"));

        // Listing again changes nothing; a forge tool that cannot run leaves its repositories for the next run.
        var again = Cli.Json(["resolve", "--gh", "/nonexistent/gh", "--glab", fake, .. connection]);
        Assert.Equal("""[{"repository":"g/s/p","merged":1}]""", again["repositories"]!.ToJsonString());
        Assert.Equal(["o/bad", "o/missing", "o/r"], again["unresolved"]!.AsArray().Select(entry => (string)entry!["repository"]!));
        Assert.All(again["unresolved"]!.AsArray(), entry => Assert.StartsWith("cannot run /nonexistent/gh: ", (string)entry!["reason"]!));
        Assert.Equal(6L, server.Scalar(connection, "SELECT count(*) FROM kpi_v1.forge_changes"));
    }
}
