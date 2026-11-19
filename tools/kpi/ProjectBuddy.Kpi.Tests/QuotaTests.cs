using System.Text.Json.Nodes;
using Xunit;

namespace ProjectBuddy.Kpi.Tests;

public sealed class QuotaIngestTests : IDisposable
{
    private const string A = "aaaaaaaa-0000-4000-8000-000000000001";

    private readonly Server server = new();
    private readonly string root = Directory.CreateTempSubdirectory("pbk-quota-").FullName;

    public void Dispose()
    {
        Directory.Delete(root, recursive: true);
        server.Dispose();
    }

    private static Ingest.Session Read(Log log) => Ingest.Read(A, log.Reader(), TimeSpan.FromMinutes(30), "log")!;

    private static string Line(string premium, string time = "2026-11-16T10:00:00.000Z") =>
        """{"type":"model.model_call_success","data":{"quotaSnapshots":{"premium_interactions":""" + premium
        + $$$$""","chat":{"x":1}}},"id":"{{{{Guid.NewGuid()}}}}","timestamp":"{{{{time}}}}"}""";

    [Fact]
    public void KeepsASnapshotOnlyWhenTheOverageChanged()
    {
        var log = new Log()
            .Event("session.start", 0, [])
            .Quota(-5, 7)
            .Quota(1, 100.5m)
            .Quota(2, 100.5m)
            .Quota(3, 150, "2026-12-02T00:00:00Z")
            .Event("model.model_call_success", 4, [])
            .Event("assistant.message", 5, []);
        var snapshots = Read(log).Quota;
        Assert.Equal([(Log.Start.AddMinutes(1), 100.5m), (Log.Start.AddMinutes(3), 150m)], snapshots.Select(s => (s.At, s.Overage)));
        Assert.Equal((200m, 200m, DateTimeOffset.Parse("2026-12-02T00:00:00Z")), (snapshots[1].UsedRequests, snapshots[1].EntitlementRequests, snapshots[1].ResetDate));
    }

    [Theory]
    [InlineData("""{"usedRequests":1,"entitlementRequests":2,"overage":3}""")]
    [InlineData("""{"usedRequests":1,"overage":3,"resetDate":"2026-12-01T00:00:00Z"}""")]
    [InlineData("""{"entitlementRequests":2,"overage":3,"resetDate":"2026-12-01T00:00:00Z"}""")]
    [InlineData("""{"usedRequests":1,"entitlementRequests":2,"resetDate":"2026-12-01T00:00:00Z"}""")]
    [InlineData("""{"usedRequests":1,"entitlementRequests":2,"overage":"3","resetDate":"2026-12-01T00:00:00Z"}""")]
    [InlineData("""{"usedRequests":1,"entitlementRequests":2,"overage":1e400,"resetDate":"2026-12-01T00:00:00Z"}""")]
    [InlineData("""{"usedRequests":1,"entitlementRequests":2,"overage":3,"resetDate":5}""")]
    [InlineData("""{"usedRequests":1,"entitlementRequests":2,"overage":3,"resetDate":"later"}""")]
    [InlineData("""{"usedRequests":1,"entitlementRequests":2,"overage":}""")]
    public void SkipsASnapshotThatIsMalformed(string premium)
    {
        var session = Read(new Log().Raw(Line(premium)).Event("assistant.message", 5, []));
        Assert.Empty(session.Quota);
        Assert.Equal(2, session.Events);
    }

    [Fact]
    public void SkipsAMissingOrUnclosedObjectButStillRefusesAnUnreadableLine()
    {
        var unclosed = """{"type":"model.model_call_success","data":{"premium_interactions":{"timestamp":"2026-11-16T10:00:00.000Z"}""".TrimEnd('}');
        Assert.Empty(Read(new Log().Raw(unclosed)).Quota);
        Assert.Empty(Read(new Log().Raw(Line("5").Replace("\"premium_interactions\":5", "\"other\":5"))).Quota);
        Assert.Throws<KpiException>(() => Read(new Log().Raw("{not json")));
    }

    [Fact]
    public void ReadsASnapshotFromALineTheFastPathCannotTime()
    {
        var premium = """{"usedRequests":1,"entitlementRequests":2,"overage":3,"resetDate":"2026-12-01T00:00:00Z"}""";
        var snapshots = Read(new Log().Raw(Line(premium, "2026-11-16T10:00:00+00:00"))).Quota;
        Assert.Equal([(Log.Start, 3m)], snapshots.Select(s => (s.At, s.Overage)));
    }

    [Fact]
    public void StoresSnapshotsOnceWhateverTheNumberOfIngests()
    {
        var connection = server.Migrated();
        new Log().Event("session.start", 0, []).Quota(1, 10).Quota(2, 20).Quota(3, 20).Write(root, A);
        Cli.Json(["ingest", "--sessions", root, .. connection]);
        Cli.Json(["ingest", "--sessions", root, .. connection]);
        Assert.Equal(2L, server.Scalar(connection, "SELECT count(*) FROM kpi_v1.quota_snapshots"));
        Assert.Equal((20m, 200m), ((decimal)server.Scalar(connection, "SELECT max(overage) FROM kpi_v1.quota_snapshots")!, (decimal)server.Scalar(connection, "SELECT max(used_requests) FROM kpi_v1.quota_snapshots")!));
    }
}

public sealed class QuotaTests : IDisposable
{
    private const string A = "aaaaaaaa-0000-4000-8000-000000000001";
    private const string B = "bbbbbbbb-0000-4000-8000-000000000002";

    private readonly Server server = new();
    private readonly string root = Directory.CreateTempSubdirectory("pbk-quota-").FullName;

    public void Dispose()
    {
        Directory.Delete(root, recursive: true);
        server.Dispose();
    }

    // The usd_per_unit setting is 0.01 here, so an overage of 1000 is 10 USD.
    private string[] Ingested(Log log, string session = A)
    {
        var connection = server.Migrated();
        log.Write(root, session);
        Cli.Json(["ingest", "--sessions", root, .. connection]);
        return connection;
    }

    private static (int Exit, JsonObject Result) Quota(string[] connection, params string[] extra)
    {
        var (exit, output, _) = Cli.Run(["quota", .. extra, .. connection]);
        return (exit, JsonNode.Parse(output)!.AsObject());
    }

    private static Log Steady() => new Log().Event("session.start", 0, []).Quota(0, 1000).Quota(60, 1100).Quota(120, 1300);

    [Fact]
    public void SetsTheBudgetAndTheReserve()
    {
        var connection = server.Migrated();
        Assert.Equal(DBNull.Value, server.Scalar(connection, "SELECT kpi_v1.setting('budget_usd')"));
        Assert.Equal(24m, server.Scalar(connection, "SELECT kpi_v1.setting('burn_window_hours')"));
        Assert.Equal("budget", (string?)Cli.Json(["budget", "--usd", "50", .. connection])["recorded"]);
        Assert.Equal((50m, 0m), Settings(connection));
        Cli.Json(["budget", "--usd", "60.5", "--reserve-usd", "5", .. connection]);
        Assert.Equal((60.5m, 5m), Settings(connection));
        Cli.Json(["budget", "--usd", "0", .. connection]);
        Assert.Equal((0m, 5m), Settings(connection));
        Assert.Equal(ExitCodes.Usage, Cli.Run(["budget", "--usd", "-1", .. connection]).Exit);
        Assert.Equal((0m, 5m), Settings(connection));
    }

    private (decimal, decimal) Settings(string[] connection) =>
        ((decimal)server.Scalar(connection, "SELECT kpi_v1.setting('budget_usd')")!, (decimal)server.Scalar(connection, "SELECT kpi_v1.setting('reserve_usd')")!);

    [Fact]
    public void LimitsTheSettingsKeysAndKeepsTheOthersPositive()
    {
        var connection = server.Migrated();
        Assert.Contains("violates check constraint", Assert.Throws<Npgsql.PostgresException>(() => server.Execute(connection, "INSERT INTO kpi_v1.settings VALUES ('bogus', 1)")).Message);
        Assert.Contains("violates check constraint", Assert.Throws<Npgsql.PostgresException>(() => server.Execute(connection, "UPDATE kpi_v1.settings SET value = 0 WHERE key = 'burn_window_hours'")).Message);
        Assert.Contains("violates check constraint", Assert.Throws<Npgsql.PostgresException>(() => server.Execute(connection, "INSERT INTO kpi_v1.settings VALUES ('budget_usd', -1)")).Message);
        server.Execute(connection, "INSERT INTO kpi_v1.settings VALUES ('farm_reference_aiu_per_job:decompose', 2)");
    }

    [Fact]
    public void RefusesWithoutASnapshotOrABudget()
    {
        var connection = server.Migrated();
        var (exit, output, error) = Cli.Run(["quota", .. connection]);
        Assert.Equal((ExitCodes.Refused, ""), (exit, output));
        Assert.Contains("no quota snapshot", error);

        var withSnapshot = Ingested(Steady());
        (exit, output, error) = Cli.Run(["quota", .. withSnapshot]);
        Assert.Equal((ExitCodes.Refused, ""), (exit, output));
        Assert.Contains("budget_usd is not set", error);
    }

    [Fact]
    public void ReportsTheHeadroomAndTheAllocableUsd()
    {
        var connection = Ingested(new Log().Event("session.start", 0, []).Quota(0, 1000));
        Cli.Json(["budget", "--usd", "50", "--reserve-usd", "5", .. connection]);
        var (exit, result) = Quota(connection);
        Assert.Equal(ExitCodes.Ok, exit);
        Assert.Equal((10m, 50m, 40m, 5m, 35m, 24d), ((decimal)result["spendUsd"]!, (decimal)result["budgetUsd"]!, (decimal)result["headroomUsd"]!,
            (decimal)result["reserveUsd"]!, (decimal)result["allocableUsd"]!, (double)result["burnWindowHours"]!));
        Assert.Equal((Log.Start, 200m, 200m, DateTimeOffset.Parse("2026-12-01T00:00:00Z")),
            ((DateTimeOffset)result["snapshotAt"]!, (decimal)result["usedRequests"]!, (decimal)result["entitlementRequests"]!, (DateTimeOffset)result["resetDate"]!));
        Assert.Equal((null, null, false, false), (result["burnUsdPerHour"], result["projectedExhaustionAt"], (bool)result["exhaustsBeforeReset"]!, result.ContainsKey("scope")));
    }

    [Fact]
    public void BurnsOverTheWindowAndProjectsTheExhaustion()
    {
        var connection = Ingested(Steady());
        Cli.Json(["budget", "--usd", "1000", .. connection]);
        var (exit, result) = Quota(connection);
        Assert.Equal(ExitCodes.Ok, exit);
        Assert.Equal(1.5m, (decimal)result["burnUsdPerHour"]!);
        var left = (DateTimeOffset)result["projectedExhaustionAt"]! - Log.Start.AddMinutes(120);
        Assert.Equal(987 / 1.5, left.TotalHours, 3);

        Assert.Equal(2m, (decimal)Quota(connection, "--window-hours", "1.5").Result["burnUsdPerHour"]!);
        Assert.Null(Quota(connection, "--window-hours", "0.5").Result["burnUsdPerHour"]);
        server.Execute(connection, "UPDATE kpi_v1.settings SET value = 1.5 WHERE key = 'burn_window_hours'");
        var narrowed = Quota(connection).Result;
        Assert.Equal((2m, 1.5d), ((decimal)narrowed["burnUsdPerHour"]!, (double)narrowed["burnWindowHours"]!));
    }

    [Fact]
    public void BurnsOnlyFromTheLastBillingCycle()
    {
        var log = new Log().Event("session.start", 0, []).Quota(0, 5000).Quota(60, 6000).Quota(120, 100).Quota(180, 300);
        var connection = Ingested(log);
        Cli.Json(["budget", "--usd", "50", .. connection]);
        Assert.Equal(2m, (decimal)Quota(connection).Result["burnUsdPerHour"]!);
    }

    [Fact]
    public void HasNoBurnWithoutElapsedTimeAndNoProjectionWithoutBurn()
    {
        var connection = Ingested(new Log().Event("session.start", 0, []).Quota(10, 1000), A);
        new Log().Event("session.start", 0, []).Quota(10, 1100).Write(root, B);
        Cli.Json(["ingest", "--sessions", root, .. connection]);
        Cli.Json(["budget", "--usd", "50", .. connection]);
        var same = Quota(connection).Result;
        Assert.Equal((null, null), (same["burnUsdPerHour"], same["projectedExhaustionAt"]));

        server.Execute(connection, "DELETE FROM kpi_v1.quota_snapshots WHERE session_id = 'bbbbbbbb-0000-4000-8000-000000000002'");
        Directory.Delete(Path.Combine(root, B), recursive: true);
        new Log().Event("session.start", 0, []).Quota(70, 1000).Write(root, B);
        Cli.Json(["ingest", "--sessions", root, .. connection]);
        var flat = Quota(connection).Result;
        Assert.Equal((0m, null), ((decimal)flat["burnUsdPerHour"]!, flat["projectedExhaustionAt"]));

        server.Execute(connection, "UPDATE kpi_v1.quota_snapshots SET overage = 1.00001 WHERE session_id = 'bbbbbbbb-0000-4000-8000-000000000002'");
        server.Execute(connection, "UPDATE kpi_v1.quota_snapshots SET overage = 1 WHERE session_id = 'aaaaaaaa-0000-4000-8000-000000000001'");
        var slow = Quota(connection).Result;
        Assert.NotNull(slow["burnUsdPerHour"]);
        Assert.Null(slow["projectedExhaustionAt"]);
    }

    [Fact]
    public void AlarmsWhenTheQuotaExhaustsBeforeTheReset()
    {
        var log = new Log().Event("session.start", 0, []).Quota(0, 1000, "2026-11-17T00:00:00Z").Quota(60, 1100, "2026-11-17T00:00:00Z");
        var connection = Ingested(log);
        // Burning 1 USD an hour from 11 USD spent: 19 USD of headroom lasts past the reset, 9 USD does not.
        Cli.Json(["budget", "--usd", "30", .. connection]);
        var (exit, result) = Quota(connection);
        Assert.Equal((ExitCodes.Ok, false), (exit, (bool)result["exhaustsBeforeReset"]!));
        Cli.Json(["budget", "--usd", "20", .. connection]);
        (exit, result) = Quota(connection);
        Assert.Equal((ExitCodes.Alarm, true), (exit, (bool)result["exhaustsBeforeReset"]!));
        Assert.Equal(9d, ((DateTimeOffset)result["projectedExhaustionAt"]! - Log.Start.AddMinutes(60)).TotalHours, 3);
    }

    [Fact]
    public void ProjectsTheExhaustionAtOnceWithoutHeadroom()
    {
        var connection = Ingested(Steady());
        Cli.Json(["budget", "--usd", "5", .. connection]);
        var (exit, result) = Quota(connection);
        Assert.Equal((ExitCodes.Alarm, true, Log.Start.AddMinutes(120)), (exit, (bool)result["exhaustsBeforeReset"]!, (DateTimeOffset)result["projectedExhaustionAt"]!));
        Assert.Equal((-8m, 0m), ((decimal)result["headroomUsd"]!, (decimal)result["allocableUsd"]!));
    }

    [Fact]
    public void AlarmsOnlyWhenTheHeadroomIsBelowTheReserve()
    {
        var connection = Ingested(new Log().Event("session.start", 0, []).Quota(0, 1000));
        Cli.Json(["budget", "--usd", "15", "--reserve-usd", "5", .. connection]);
        Assert.Equal((ExitCodes.Ok, 0m), (Quota(connection).Exit, (decimal)Quota(connection).Result["allocableUsd"]!));
        Cli.Json(["budget", "--usd", "12", "--reserve-usd", "5", .. connection]);
        var (exit, result) = Quota(connection);
        Assert.Equal((ExitCodes.Alarm, 0m, false), (exit, (decimal)result["allocableUsd"]!, (bool)result["exhaustsBeforeReset"]!));
    }

    [Fact]
    public void CoversTheItemsInPriorityOrder()
    {
        var connection = Ingested(new Log().Event("session.start", 0, []).Quota(0, 1000));
        Cli.Json(["budget", "--usd", "50", "--reserve-usd", "5", .. connection]);
        // 35 USD is allocable: b does not fit, and c, though it would alone, is behind it.
        var scope = Quota(connection, "--items", "a=10,b=30,c=1").Result["scope"]!;
        Assert.Equal("""[{"name":"a","usd":10,"covered":true},{"name":"b","usd":30,"covered":false},{"name":"c","usd":1,"covered":false}]""", scope["items"]!.ToJsonString());
        Assert.Equal((1, 10m, 31m), ((int)scope["coveredCount"]!, (decimal)scope["coveredUsd"]!, (decimal)scope["uncoveredUsd"]!));

        scope = Quota(connection, "--items", "a=10,b=25.0,c=0.01").Result["scope"]!;
        Assert.Equal((2, 35m, 0.01m), ((int)scope["coveredCount"]!, (decimal)scope["coveredUsd"]!, (decimal)scope["uncoveredUsd"]!));
        Assert.False((bool)scope["items"]![2]!["covered"]!);
    }
}
