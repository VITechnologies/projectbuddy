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
        Assert.Equal("{\"asked\":0,\"merged\":0,\"unresolved\":[]}", Cli.Json(["resolve", .. connection]).ToJsonString());
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
        // A forked session repeats its parent's history after its own start: those events are skipped.
        var log = new Log()
            .Event("session.start", 0, new JsonObject { ["context"] = new JsonObject { ["cwd"] = "/w/r", ["gitRoot"] = "/w/r", ["repository"] = "o/r" } })
            .Call(-30, 15, Counts("view"))
            .Tool("copied", "bash", -29, "gh pr merge 1 --repo o/r").Done("copied", -28)
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
            .Event("session.usage_record", 9.5, new JsonObject { ["usage"] = new JsonObject { ["cost"] = "x", ["inputTokens"] = "x" } })
            .Event("tool.execution_start", 9.6, new JsonObject { ["toolCallId"] = "text", ["toolName"] = "bash", ["arguments"] = "gh pr merge 3 --repo o/r" })
            .Done("text", 9.7)
            .Tool("merge", "bash", 10, "gh pr merge 5 --repo o/r --merge && glab mr merge 7 --yes -R g/s/p").Done("merge", 11)
            .Tool("short", "bash", 12, "gh pr merge 6 -R 'o/r' --admin").Done("short", 12)
            .Tool("failed", "bash", 13, "cd ~/other && gh pr merge 8 --squash 2>&1").Done("failed", 13, success: false)
            .Tool("cwd", "bash", 14, "gh pr merge 9").Done("cwd", 14)
            .Tool("unknown", "bash", 14.5, "gh pr merge 10 --repo $R; cd /elsewhere && gh pr merge 11; echo gh pr merge --admin; glab mr merge 12 -R onlyone")
            .Done("unknown", 14.5)
            .Event("tool.execution_start", 15, new JsonObject { ["toolCallId"] = "args", ["toolName"] = "bash", ["arguments"] = new JsonObject { ["command"] = 7 } })
            .Done("args", 15)
            .Tool("view", "view", 16).Done("view", 16)
            .Done("never-started", 17)
            .Tool("open", "ask_user", 18)
            .Event("user.message", 19, [])
            .Raw("")
            .Raw($$"""{"timestamp": "{{Log.Time(20)}}", "type": "assistant.turn_start", "data": {}, "id": "{{Guid.NewGuid()}}"}""")
            .Event("assistant.message", 90, []);
        var workspaces = Workspaces.From([new Ingest.Context("/h/other", "/h/other", "o/other")], "/h");
        var session = Ingest.Read(A, log.Reader(), TimeSpan.FromMinutes(30), "log", workspaces)!;

        Assert.Equal((Log.Start, Log.Start.AddMinutes(90), 32L), (session.First, session.Last, session.Events));
        Assert.Equal(["explore", "ask", "edit", "delegate", "run", "memory", "respond", "respond", "respond", "respond"], session.Calls.Select(call => call.Category));
        Assert.Equal((0m, 0L), (session.Calls[9].Cost, session.Calls[9].InputTokens));
        Assert.Equal(("sub-agent", "claude-haiku-5.5", 1.5m), (session.Calls[7].Initiator, session.Calls[7].Model, session.Calls[7].Cost));
        Assert.Equal(("unknown", "unknown", 0m, 0L), (session.Calls[8].Initiator, session.Calls[8].Model, session.Calls[8].Cost, session.Calls[8].InputTokens));
        // Every merge command is an attempt, whatever it printed and however it exited; the forge decides (ForgeTests).
        Assert.Equal(["o/r#5", "g/s/p!7", "o/r#6", "o/other#8", "o/r#9"], session.Merges.Select(merge => merge.ChangeRef));
        Assert.Equal((Log.Start.AddMinutes(10), Log.Start.AddMinutes(11)), (session.Merges[1].Started, session.Merges[1].Completed));
        Assert.Equal(3, session.UnattributedMerges);
        Assert.Equal(9, session.Tools.Count);
        Assert.Equal(("ask_user", (DateTimeOffset?)null, (bool?)null), (session.Tools[^1].Tool, session.Tools[^1].Completed, session.Tools[^1].Success));
        Assert.Single(session.Messages);
        Assert.Equal([(Log.Start, Log.Start.AddMinutes(20)), (Log.Start.AddMinutes(90), Log.Start.AddMinutes(90))], session.Spans);
        Assert.Null(Ingest.Read(A, new StringReader(""), TimeSpan.FromMinutes(30), "log", workspaces));
    }

    [Fact]
    public void RefusesALineThatIsNotAnEvent()
    {
        var refused = Assert.Throws<KpiException>(() => Ingest.Read(A, new Log().Event("x", 0, []).Raw("{not json").Reader(), TimeSpan.FromMinutes(30), "events.jsonl", Workspaces.From([], "/h")));
        Assert.StartsWith("events.jsonl: line 2 is not a readable event", refused.Message);
    }

    [Fact]
    public void StoresEachFactOnceAndCompletesOpenToolCalls()
    {
        var connection = server.Migrated();
        Assert.Equal(ExitCodes.Refused, Cli.Run(["ingest", "--sessions", Path.Combine(root, "missing"), .. connection]).Exit);
        new Log().Call(0, 15, Counts("view")).Tool("open", "ask_user", 1).Tool("merge", "bash", 2, "gh pr merge 5 --repo o/r").Done("merge", 3).Write(root, A);
        new Log().Event("x", 0, []).Write(root, "not-a-session");
        Directory.CreateDirectory(Path.Combine(root, B));
        File.WriteAllText(Path.Combine(root, "cccccccc-0000-4000-8000-000000000003", "events.jsonl") is var empty && Directory.CreateDirectory(Path.GetDirectoryName(empty)!) is not null ? empty : empty, "");

        var first = Cli.Json(["ingest", "--sessions", root, .. connection])["sessions"]!.AsArray();
        Assert.Equal((A, 1, 1), ((string?)first.Single()!["session"], (int)first.Single()!["newModelCalls"]!, (int)first.Single()!["newMergeAttempts"]!));
        var again = Cli.Json(["ingest", "--sessions", root, .. connection])["sessions"]!.AsArray().Single()!;
        Assert.Equal((0, 0), ((int)again["newModelCalls"]!, (int)again["newMergeAttempts"]!));

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
            .Tool("m1", "bash", 2.5, "gh pr merge 1 --repo o/r").Done("m1", 3)
            .Call(5, 3, new JsonObject { ["bash"] = 1 }, initiator: "sub-agent")
            .Tool("m2", "bash", 5.5, "gh pr merge 2 --repo o/r").Done("m2", 6)
            .Tool("ask", "ask_user", 6).Done("ask", 26)
            .Write(root, A);
        // Session B overlaps A in time and tries o/r#1 again after it merged: neither counts twice. Its o/r#4 is not
        // resolved yet, and its bare `gh pr merge 3` has no repository (B has no start context).
        new Log().Event("x", 1, []).Tool("again", "bash", 7, "gh pr merge 1 --repo o/r").Done("again", 8)
            .Tool("other", "bash", 7.5, "gh pr merge 4 --repo o/r; gh pr merge 3").Done("other", 7.5).Write(root, B);
        Cli.Json(["ingest", "--sessions", root, .. connection]);
        server.Execute(connection, $"INSERT INTO kpi_v1.forge_changes (change_ref, merged_at) VALUES ('o/r#1', '{Log.Time(2.9)}'), ('o/r#2', '{Log.Time(5.9)}')");

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
        Assert.Equal((3, 1, 1), ((int)report["merges"]!["attempted"]!, (int)report["merges"]!["unresolved"]!, (int)report["merges"]!["unattributed"]!));

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

public sealed class WorkspacesTests
{
    private static readonly Workspaces Known = Workspaces.From(
        [null, new Ingest.Context("/x", null, "o/x"), new Ingest.Context("/x", "/x", null), new("/h/other", "/h/other", "o/other"),
         new("/w/r", "/w/r", "o/r"), new("/w/r/nested", "/w/r/nested", "o/nested"),
         new("/a", "/a", "o/a"), new("/a", "/a", "o/b"), new("/a", "/a", "o/a")],
        "/h");

    [Theory]
    [InlineData(null, "o/r")]
    [InlineData("~/other", "o/other")]
    [InlineData("~/other/sub", "o/other")]
    [InlineData("$HOME/other", "o/other")]
    [InlineData("${HOME}/other", "o/other")]
    [InlineData("\"/w/r/nested/x\"", "o/nested")]
    [InlineData("'/w/r/'", "o/r")]
    [InlineData("sub", "o/r")]
    [InlineData("../r/nested", "o/nested")]
    [InlineData("~", null)]
    [InlineData("/w/rx", null)]
    [InlineData("$OTHER/x", null)]
    [InlineData("/a", null)]
    [InlineData("/x", null)]
    public void MapsTheDirectoryACommandRanInToItsRepository(string? directory, string? repository) =>
        Assert.Equal(repository, Known.Repository(directory, new Ingest.Context("/w/r", "/w/r", "o/r")));

    [Fact]
    public void NeedsTheSessionForARelativeOrOwnDirectory()
    {
        Assert.Null(Known.Repository(null, null));
        Assert.Null(Known.Repository("sub", null));
        Assert.Null(Known.Repository("sub", new Ingest.Context(null, null, "o/r")));
    }

    [Theory]
    [InlineData(null)]
    [InlineData("""{"type":"assistant.message","data":{},"timestamp":"2026-11-16T10:00:00.000Z"}""")]
    [InlineData("{broken \"session.start\"")]
    [InlineData("""{"type":"user.message","data":{"content":"session.start"}}""")]
    [InlineData("""{"type":"session.start"}""")]
    public void ReadsOnlyAStartContext(string? line) => Assert.Null(Ingest.StartContext(line));

    [Fact]
    public void ReadsTheStartContext() => Assert.Equal(new Ingest.Context("/w/r/sub", "/w/r", "o/r"),
        Ingest.StartContext("""{"type":"session.start","data":{"context":{"cwd":"/w/r/sub","gitRoot":"/w/r","repository":"o/r"}}}"""));
}

public sealed class ForgeTests : IDisposable
{
    private const string A = "aaaaaaaa-0000-4000-8000-000000000001";

    private readonly Server server = new();
    private readonly string root = Directory.CreateTempSubdirectory("pbk-forge-").FullName;

    public void Dispose()
    {
        Directory.Delete(root, recursive: true);
        server.Dispose();
    }

    // A stand-in for gh or glab: `<tool> api <path>` prints the answer for a known path and fails for any other.
    private string Fake(string name, string cases)
    {
        var path = Path.Combine(root, name);
        File.WriteAllText(path, $"#!/bin/sh\ncase \"$2\" in\n{cases}\n  *) echo 'HTTP 404: Not Found' >&2; echo 'more' >&2; exit 1 ;;\nesac\n");
        if (!OperatingSystem.IsWindows())
        {
            File.SetUnixFileMode(path, UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute);
        }
        return path;
    }

    [Fact]
    public void AcceptsAChangeOnlyIfTheForgeMergedItDuringAnAttempt()
    {
        var connection = server.Migrated();
        var sessions = Directory.CreateDirectory(Path.Combine(root, "sessions")).FullName;
        new Log()
            .Event("session.start", 0, new JsonObject { ["context"] = new JsonObject { ["cwd"] = "/w/r", ["gitRoot"] = "/w/r", ["repository"] = "o/r" } })
            .Tool("m1", "bash", 2, "gh pr merge 1").Done("m1", 3)
            .Tool("m7", "bash", 2, "glab mr merge 7 -R g/s/p --yes").Done("m7", 3)
            .Tool("m2", "bash", 4, "gh pr merge 2 --repo o/r").Done("m2", 4)
            .Tool("m5", "bash", 5, "gh pr merge 5 --repo o/r --auto").Done("m5", 5)
            .Tool("bad", "bash", 6, "gh pr merge 3 -R o/r; gh pr merge 4 -R o/r; gh pr merge 6 -R o/r").Done("bad", 6)
            .Write(sessions, A);
        Cli.Json(["ingest", "--sessions", sessions, .. connection]);

        // o/r#1 merged during its attempt; g/s/p!7 1.5 minutes after (within the 2-minute skew); o/r#2 is open; o/r#5
        // merged an hour later by auto-merge; the answers for o/r#3, #4 and #6 cannot be read.
        var gh = Fake("gh", $$"""
              repos/o/r/pulls/1) echo '{"merged_at":"{{Log.Time(2.9)}}"}' ;;
              repos/o/r/pulls/2) echo '{"merged_at":null}' ;;
              repos/o/r/pulls/3) echo 'not json' ;;
              repos/o/r/pulls/5) echo '{"merged_at":"{{Log.Time(60)}}"}' ;;
              repos/o/r/pulls/6) echo '{"state":"open"}' ;;
            """);
        var glab = Fake("glab", $$"""  projects/g%2Fs%2Fp/merge_requests/7) echo '{"merged_at":"{{Log.Time(4.5)}}"}' ;;""");
        var first = Cli.Json(["resolve", "--gh", gh, "--glab", glab, .. connection]);
        Assert.Equal((4, 3), ((int)first["asked"]!, (int)first["merged"]!));
        var unresolved = first["unresolved"]!.AsArray();
        Assert.Equal(["o/r#3", "o/r#4", "o/r#6"], unresolved.Select(entry => (string)entry!["change"]!));
        Assert.StartsWith("unexpected answer: ", (string)unresolved[0]!["reason"]!);
        Assert.Equal("gh exited 1: HTTP 404: Not Found", (string)unresolved[1]!["reason"]!);
        Assert.StartsWith("unexpected answer: ", (string)unresolved[2]!["reason"]!);
        Assert.Equal("g/s/p!7 o/r#1", server.Scalar(connection, "SELECT string_agg(change_ref, ' ' ORDER BY change_ref) FROM kpi_v1.accepted_changes"));
        var merges = Cli.Json(["report", .. connection])["merges"]!;
        Assert.Equal((7, 3, 0), ((int)merges["attempted"]!, (int)merges["unresolved"]!, (int)merges["unattributed"]!));

        // Merged changes are not asked again; a forge tool that cannot run leaves the rest unresolved.
        var second = Cli.Json(["resolve", "--gh", "/nonexistent/gh", "--glab", glab, .. connection]);
        Assert.Equal(0, (int)second["asked"]!);
        Assert.Equal(["o/r#2", "o/r#3", "o/r#4", "o/r#6"], second["unresolved"]!.AsArray().Select(entry => (string)entry!["change"]!));
        Assert.All(second["unresolved"]!.AsArray(), entry => Assert.StartsWith("cannot run /nonexistent/gh: ", (string)entry!["reason"]!));
    }
}
