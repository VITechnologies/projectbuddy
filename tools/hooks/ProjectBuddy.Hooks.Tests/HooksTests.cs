using System.Text.Json.Nodes;
using Xunit;

namespace ProjectBuddy.Hooks.Tests;

public sealed class UsageTests
{
    private static readonly string Password = Path.Combine(Path.GetTempPath(), "pbh-password");

    [Theory]
    [InlineData]
    [InlineData("nope")]
    [InlineData("sessions", "--bogus", "x")]
    [InlineData("sessions", "--status")]
    [InlineData("sessions", "--status", "running", "--status", "done")]
    [InlineData("answer", "--text", "x")]
    [InlineData("sessions", "--password-file", "relative")]
    [InlineData("sessions")]
    [InlineData("sessions", "--password-file", "/tmp/p", "--port", "0")]
    [InlineData("sessions", "--password-file", "/tmp/p", "--port", "x")]
    [InlineData("sessions", "--password-file", "/tmp/p", "--user", "Bad-User")]
    [InlineData("sessions", "--password-file", "/tmp/p", "--status", "bogus")]
    [InlineData("sessions-set", "--password-file", "/tmp/p", "--session", "short")]
    [InlineData("sessions-set", "--password-file", "/tmp/p", "--session", "abcdefgh1", "--parent", "bad id")]
    [InlineData("sessions-set", "--password-file", "/tmp/p", "--session", "abcdefgh1", "--work-ref", "owner/repo/381")]
    [InlineData("answer", "--password-file", "/tmp/p", "--id", "x", "--text", "t")]
    public void Bad_command_lines_are_usage_errors(params string[] args)
    {
        var (exit, _, error) = Cli.Run(args);
        Assert.Equal(ExitCodes.Usage, exit);
        Assert.Contains("usage: projectbuddy-hooks", error);
    }

    [Fact]
    public void A_missing_password_file_is_reported_not_thrown()
    {
        var (exit, _, error) = Cli.Run("sessions", "--password-file", Path.Combine(Path.GetTempPath(), "pbh-no-such-file"));
        Assert.Equal(ExitCodes.Usage, exit);
        Assert.Contains("pbh-no-such-file", error);
    }

    [Fact]
    public void An_unreachable_server_is_a_database_error()
    {
        File.WriteAllText(Password, "x");
        var (exit, _, error) = Cli.Run("sessions", "--password-file", Password, "--port", "1");
        Assert.Equal(ExitCodes.Database, exit);
        Assert.Contains("database:", error);
    }
}

public sealed class RegistryAndQueueTests : IDisposable
{
    private readonly Server server = new();

    public void Dispose() => server.Dispose();

    private const string Session = "11111111-aaaa-bbbb-cccc-000000000001";
    private const string Worker = "22222222-aaaa-bbbb-cccc-000000000002";

    [Fact]
    public void Hooks_and_the_cli_share_one_session_row()
    {
        var connection = server.Migrated();
        server.Execute(connection, $"SELECT hooks.on_start('{{\"sessionId\":\"{Worker}\",\"cwd\":\"/work\"}}')");
        var set = Cli.Json(["sessions-set", "--session", Worker, "--work-ref", "VITechnologies/projectbuddy#381", "--parent", Session, "--title", "hooks", .. connection]);
        Assert.True((bool)set["set"]!);

        // A later hook event keeps what the orchestrator wrote.
        server.Execute(connection, $"SELECT hooks.on_prompt('{{\"session_id\":\"{Worker}\",\"prompt\":\"go\"}}')");
        var row = Cli.Json(["sessions", .. connection])["sessions"]![0]!;
        Assert.Equal(Worker, (string)row["session_id"]!);
        Assert.Equal("VITechnologies/projectbuddy#381", (string)row["work_ref"]!);
        Assert.Equal(Session, (string)row["parent_session_id"]!);
        Assert.Equal("hooks", (string)row["title"]!);
        Assert.Equal("/work", (string)row["workspace"]!);
        Assert.Equal("running", (string)row["status"]!);
        Assert.Equal("VITechnologies/projectbuddy#381", server.Scalar(connection, $"SELECT hooks.work_ref_of('{Worker}')"));

        Cli.Json(["sessions-set", "--session", Worker, .. connection]);
        Assert.Equal("hooks", (string)Cli.Json(["sessions", "--status", "running", .. connection])["sessions"]![0]!["title"]!);
        Assert.Empty(Cli.Json(["sessions", "--status", "done", .. connection])["sessions"]!.AsArray());
    }

    [Fact]
    public void A_long_single_turn_is_attributed_to_the_item_from_the_session_start()
    {
        var connection = server.Migrated();
        const string item = "VITechnologies/projectbuddy#387";
        server.Execute(connection, $"SELECT hooks.on_start('{{\"sessionId\":\"{Worker}\"}}')");
        server.Execute(connection, "UPDATE hooks.sessions SET started_at = now() - interval '3 hours'");
        server.Execute(connection, $"INSERT INTO kpi_v1.sessions VALUES ('{Worker}', now() - interval '3 hours', now(), 3)");
        for (var n = 0; n < 3; n++)
        {
            server.Execute(connection, $"INSERT INTO kpi_v1.model_calls VALUES (gen_random_uuid(), '{Worker}', now() - interval '{150 - n * 60} minutes', 'user', 'm', 0, 1, 1, 1, 'run')");
        }

        // The ref arrives after the calls, and the turn has not stopped yet.
        Assert.True((bool)Cli.Json(["sessions-set", "--session", Worker, "--work-ref", item, .. connection])["marked"]!);
        Assert.Equal(3L, server.Scalar(connection, $"SELECT count(*) FROM kpi_v1.call_items WHERE item = '{item}'"));
        Assert.Equal(1L, server.Scalar(connection, "SELECT count(*) FROM kpi_v1.work_markers"));

        // The same ref again, as at agentStop, marks nothing; a changed ref marks from now on.
        Assert.False((bool)Cli.Json(["sessions-set", "--session", Worker, "--work-ref", item, .. connection])["marked"]!);
        Assert.False((bool)Cli.Json(["sessions-set", "--session", Worker, "--title", "t", .. connection])["marked"]!);
        Assert.Equal(1L, server.Scalar(connection, "SELECT count(*) FROM kpi_v1.work_markers"));
        Assert.True((bool)Cli.Json(["sessions-set", "--session", Worker, "--work-ref", "VITechnologies/projectbuddy#388", .. connection])["marked"]!);
        Assert.Equal(2L, server.Scalar(connection, "SELECT count(*) FROM kpi_v1.work_markers"));
        Assert.Equal(3L, server.Scalar(connection, $"SELECT count(*) FROM kpi_v1.call_items WHERE item = '{item}'"));
    }

    [Fact]
    public void A_session_without_a_work_ref_has_none_to_mark()
    {
        var connection = server.Migrated();
        server.Execute(connection, $"SELECT hooks.on_start('{{\"sessionId\":\"{Worker}\"}}')");
        Assert.Equal(DBNull.Value, server.Scalar(connection, $"SELECT hooks.work_ref_of('{Worker}')"));
        Assert.Null(Cli.Json(["sessions", .. connection])["sessions"]![0]!["workspace"]);
    }

    [Fact]
    public void A_stop_turns_the_last_message_into_a_status_and_a_decision()
    {
        var connection = server.Migrated();
        Cli.Json(["sessions-set", "--session", Worker, "--work-ref", "VITechnologies/projectbuddy#381", .. connection]);
        string Stop(string content, string id = "m-1", string stamp = "") => (string)server.Scalar(connection,
            $"SELECT hooks.on_stop('{{\"sessionId\":\"{Worker}\"{stamp}}}', '{new JsonObject { ["type"] = "assistant.message", ["data"] = new JsonObject { ["messageId"] = id, ["content"] = content } }.ToJsonString().Replace("'", "''")}')")!;

        // Inline, the hook has no message yet: it only records that the session waits.
        Assert.Equal("waiting", (string)server.Scalar(connection, "SELECT hooks.on_stop('{\"sessionId\":\"" + Worker + "\"}', '')")!);
        Assert.Equal("waiting", (string)server.Scalar(connection, "SELECT hooks.on_stop('{\"sessionId\":\"" + Worker + "\"}', NULL)")!);
        Assert.Equal("waiting", Stop("working on it"));
        Assert.Equal("waiting", (string)server.Scalar(connection, "SELECT hooks.on_stop('{\"sessionId\":\"" + Worker + "\"}', 'not json')")!);
        Assert.Empty(Cli.Json(["decisions", .. connection])["decisions"]!.AsArray());

        Assert.Equal("waiting", Stop("DECISION NEEDED: A or B?", "m-2"));
        Assert.Equal("waiting", Stop("**DECISION NEEDED:** A, B or C?", "m-3"));
        var open = Cli.Json(["decisions", .. connection])["decisions"]!.AsArray();
        Assert.Single(open);
        Assert.Equal("VITechnologies/projectbuddy#381", (string)open[0]!["work_ref"]!);
        Assert.Contains("A, B or C", (string)open[0]!["message"]!);

        var id = (int)open[0]!["id"]!;
        Assert.True((bool)Cli.Json(["answer", "--id", id.ToString(), "--text", "take A", .. connection])["answered"]!);
        var (exit, _, error) = Cli.Run(["answer", "--id", id.ToString(), "--text", "again", .. connection]);
        Assert.Equal(ExitCodes.Refused, exit);
        Assert.Contains("already answered", error);
        Assert.Empty(Cli.Json(["decisions", .. connection])["decisions"]!.AsArray());
        var all = Cli.Json(["decisions", "--all", .. connection])["decisions"]![0]!;
        Assert.Equal("cli", (string)all["answered_by"]!);
        Assert.Equal("take A", (string)all["answer"]!);

        // Looking at the same message again (a retry, a late settle) must not reopen the answered decision.
        Stop("**DECISION NEEDED:** A, B or C?", "m-3");
        Assert.Empty(Cli.Json(["decisions", .. connection])["decisions"]!.AsArray());

        Assert.Equal("done", Stop("DONE: it landed", "m-4"));
        Assert.Equal("done", (string)Cli.Json(["sessions", .. connection])["sessions"]![0]!["status"]!);
    }

    [Fact]
    public void A_late_look_at_an_old_stop_is_ignored_once_the_session_moved_on()
    {
        var connection = server.Migrated();
        server.Execute(connection, $"SELECT hooks.on_prompt('{{\"sessionId\":\"{Worker}\",\"prompt\":\"go\"}}')");
        var old = DateTimeOffset.UtcNow.AddMinutes(-5).ToUnixTimeMilliseconds();
        var line = "{\"data\":{\"messageId\":\"m-1\",\"content\":\"DECISION NEEDED: late\"}}";
        Assert.Equal("stale", server.Scalar(connection, $"SELECT hooks.on_stop('{{\"sessionId\":\"{Worker}\",\"timestamp\":{old}}}', '{line}')"));
        Assert.Empty(Cli.Json(["decisions", .. connection])["decisions"]!.AsArray());
        Assert.Equal("running", (string)Cli.Json(["sessions", .. connection])["sessions"]![0]!["status"]!);

        var now = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
        Assert.Equal("waiting", server.Scalar(connection, $"SELECT hooks.on_stop('{{\"sessionId\":\"{Worker}\",\"timestamp\":{now}}}', '{line}')"));
        Assert.Single(Cli.Json(["decisions", .. connection])["decisions"]!.AsArray());
    }

    [Fact]
    public void The_next_prompt_answers_an_open_decision()
    {
        var connection = server.Migrated();
        server.Execute(connection, $"SELECT hooks.on_stop('{{\"sessionId\":\"{Worker}\"}}', '{{\"data\":{{\"content\":\"DECISION NEEDED: ok?\"}}}}')");
        Assert.Single(Cli.Json(["sessions", .. connection])["sessions"]!.AsArray());
        Assert.Equal(1, (int)Cli.Json(["sessions", .. connection])["sessions"]![0]!["open_decisions"]!);
        server.Execute(connection, $"SELECT hooks.on_prompt('{{\"sessionId\":\"{Worker}\",\"prompt\":\"yes, go\"}}')");
        var decision = Cli.Json(["decisions", "--all", .. connection])["decisions"]![0]!;
        Assert.Equal("answered", (string)decision["status"]!);
        Assert.Equal("prompt", (string)decision["answered_by"]!);
        Assert.Equal("yes, go", (string)decision["answer"]!);
        Assert.Equal(0, (int)Cli.Json(["sessions", .. connection])["sessions"]![0]!["open_decisions"]!);
    }

    [Fact]
    public void The_writer_role_can_only_call_the_hook_functions()
    {
        var connection = server.Migrated();
        server.Execute(connection, "CREATE ROLE hooks_probe LOGIN IN ROLE hooks_writer");
        try
        {
            using var probe = server.OpenTo(connection);
            using (var call = new Npgsql.NpgsqlCommand("SET ROLE hooks_probe; SELECT hooks.on_start('{\"sessionId\":\"33333333-aaaa\"}')", probe))
            {
                call.ExecuteNonQuery();
            }

            using var read = new Npgsql.NpgsqlCommand("SELECT count(*) FROM hooks.sessions", probe);
            Assert.Contains("permission denied", Assert.Throws<Npgsql.PostgresException>(() => read.ExecuteNonQuery()).Message);
        }
        finally
        {
            server.Execute(connection, "DROP OWNED BY hooks_probe; DROP ROLE hooks_probe");
        }

        Assert.Single(Cli.Json(["sessions", .. connection])["sessions"]!.AsArray());
    }
}

public sealed class HandoffTests : IDisposable
{
    private readonly Server server = new();
    private readonly string body = Path.GetTempFileName();

    public void Dispose()
    {
        server.Dispose();
        File.Delete(body);
    }

    private const string Ref = "VITechnologies/projectbuddy#23";

    [Fact]
    public void A_write_based_on_the_current_version_lands_and_is_recorded()
    {
        using var gh = new FakeGh();
        var connection = server.Migrated();
        var read = Cli.Json(["handoff-read", "--work-ref", Ref, "--gh", gh.Path, .. connection]);
        Assert.Equal("first body", (string)read["body"]!);
        File.WriteAllText(body, "second body");

        var written = Cli.Json(["handoff-write", "--work-ref", Ref, "--expected", (string)read["updated_at"]!, "--body-file", body, "--session", "11111111-aaaa", "--gh", gh.Path, .. connection]);
        Assert.Equal("second body", gh.Body);
        Assert.Equal(gh.UpdatedAt(0), (string)written["previous_updated_at"]!);
        Assert.Equal(gh.UpdatedAt(1), (string)written["updated_at"]!);
        Assert.Equal("11111111-aaaa", server.Scalar(connection, $"SELECT writer_session FROM hooks.handoffs WHERE work_ref = '{Ref}'"));

        // The same call twice, and a call with no session, update the one row.
        File.WriteAllText(body, "third body");
        Cli.Json(["handoff-write", "--work-ref", Ref, "--expected", gh.UpdatedAt(1), "--body-file", body, "--gh", gh.Path, .. connection]);
        Assert.Equal(1L, server.Scalar(connection, "SELECT count(*) FROM hooks.handoffs"));
        Assert.Equal(DBNull.Value, server.Scalar(connection, "SELECT writer_session FROM hooks.handoffs"));
    }

    [Fact]
    public void A_stale_copy_is_refused_and_changes_nothing()
    {
        using var gh = new FakeGh();
        var connection = server.Migrated();
        File.WriteAllText(body, "newer rewrite");
        Cli.Json(["handoff-write", "--work-ref", Ref, "--expected", gh.UpdatedAt(0), "--body-file", body, "--gh", gh.Path, .. connection]);

        File.WriteAllText(body, "stale copy");
        var (exit, _, error) = Cli.Run(["handoff-write", "--work-ref", Ref, "--expected", gh.UpdatedAt(0), "--body-file", body, "--gh", gh.Path, .. connection]);
        Assert.Equal(ExitCodes.Refused, exit);
        Assert.Contains("stale", error);
        Assert.Equal("newer rewrite", gh.Body);
    }

    [Fact]
    public async Task Two_writers_from_the_same_version_cannot_both_win()
    {
        using var gh = new FakeGh(editMilliseconds: 600);
        var connection = server.Migrated();
        var one = Path.GetTempFileName();
        File.WriteAllText(one, "writer one");
        File.WriteAllText(body, "writer two");
        try
        {
            var runs = new[] { one, body }.Select(file => Task.Run(() =>
                Cli.Run(["handoff-write", "--work-ref", Ref, "--expected", gh.UpdatedAt(0), "--body-file", file, "--gh", gh.Path, .. connection]).Exit)).ToArray();
            var exits = (await Task.WhenAll(runs)).Order().ToArray();
            Assert.Equal(new[] { ExitCodes.Ok, ExitCodes.Refused }, exits);
            Assert.Equal("1", File.ReadAllText(Path.Combine(Path.GetDirectoryName(gh.Path)!, "n")).Trim());
        }
        finally
        {
            File.Delete(one);
        }
    }

    [Fact]
    public void A_held_lock_refuses_the_write_after_the_wait()
    {
        using var gh = new FakeGh();
        var connection = server.Migrated();
        File.WriteAllText(body, "x");
        using var holder = server.OpenTo(connection);
        using (var hold = new Npgsql.NpgsqlCommand("SELECT pg_advisory_lock(hashtext('hooks.handoff:" + Ref + "'))", holder))
        {
            hold.ExecuteNonQuery();
        }

        foreach (var wait in new[] { "0", "1" })
        {
            var (exit, _, error) = Cli.Run(["handoff-write", "--work-ref", Ref, "--expected", gh.UpdatedAt(0), "--body-file", body, "--lock-seconds", wait, "--gh", gh.Path, .. connection]);
            Assert.Equal(ExitCodes.Refused, exit);
            Assert.Contains("lock", error);
        }

        Assert.Equal("first body", gh.Body);
    }

    [Fact]
    public void Bad_inputs_and_a_failing_gh_are_reported()
    {
        var connection = server.Migrated();
        File.WriteAllText(body, "x");
        using var gh = new FakeGh();
        (int, string) Write(string expected, string file, string ghPath, params string[] more)
        {
            var (exit, _, error) = Cli.Run(["handoff-write", "--work-ref", Ref, "--expected", expected, "--body-file", file, "--gh", ghPath, .. more, .. connection]);
            return (exit, error);
        }

        Assert.Equal(ExitCodes.Usage, Write("yesterday-ish", body, gh.Path).Item1);
        Assert.Equal(ExitCodes.Usage, Write(gh.UpdatedAt(0), body + ".missing", gh.Path).Item1);
        Assert.Equal(ExitCodes.Usage, Write(gh.UpdatedAt(0), body, gh.Path, "--lock-seconds", "soon").Item1);

        using var failing = new FakeGh(exitCode: 4);
        var (exit, error) = Write(failing.UpdatedAt(0), body, failing.Path);
        Assert.Equal(ExitCodes.Forge, exit);
        Assert.Contains("boom", error);
        Assert.Equal(ExitCodes.Forge, Cli.Run(["handoff-read", "--work-ref", Ref, "--gh", failing.Path, .. connection]).Exit);

        var (missingExit, _, missingError) = Cli.Run(["handoff-read", "--work-ref", Ref, "--gh", "/nonexistent/gh", .. connection]);
        Assert.Equal(ExitCodes.Forge, missingExit);
        Assert.Contains("cannot run", missingError);
    }
}
