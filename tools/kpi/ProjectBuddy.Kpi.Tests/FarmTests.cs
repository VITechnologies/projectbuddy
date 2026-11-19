using System.Text.Json.Nodes;
using Xunit;

namespace ProjectBuddy.Kpi.Tests;

// Synthetic farm facts (ALW#10) in the export format of assurance-learning-workbench's export-farm-telemetry: run A
// with a 100 s busy span, and run B with a 50 s span that ran decompose on a peer instance and a local one.
public sealed class FarmTests : IDisposable
{
    private const string At = "2026-11-19T07:22:51+00:00";
    private static readonly string A = new('a', 64);
    private static readonly string B = new('b', 64);
    private static readonly string C = new('c', 64);

    private readonly Server server = new();
    private readonly string root = Directory.CreateTempSubdirectory("pbk-farm-").FullName;

    public void Dispose()
    {
        Directory.Delete(root, recursive: true);
        server.Dispose();
    }

    private static JsonObject Job(string source, string workload, string instance, string backend, int jobs, int cacheHits, int answered, int failed, int tokens, decimal window) => new()
    {
        ["source_sha256"] = source, ["recorded_at"] = At, ["workload"] = workload, ["instance_id"] = instance, ["backend"] = backend,
        ["jobs"] = jobs, ["cache_hits"] = cacheHits, ["answered"] = answered, ["failed"] = failed, ["eval_tokens"] = tokens, ["window_seconds"] = window,
    };

    private static JsonObject Sample(string source, string instance, decimal window, decimal busy, string backend = "local-ollama") => new()
    {
        ["source_sha256"] = source, ["recorded_at"] = At, ["instance_id"] = instance, ["backend"] = backend, ["window_seconds"] = window, ["busy_seconds"] = busy,
    };

    private static JsonObject Export(JsonArray? jobs = null, JsonArray? samples = null) => new()
    {
        ["schema_version"] = "1.0.0",
        ["export_kind"] = "alw-farm-telemetry",
        ["jobs"] = jobs ?? new JsonArray(
            Job(A, "decompose", "workstation-gpu", "local-ollama", 8, 2, 5, 1, 400, 100m),
            Job(A, "draft-rewrite", "workstation-cpu", "local-ollama", 4, 0, 4, 0, 120, 100m),
            Job(B, "decompose", "peer-rtx", "peer-infer", 6, 3, 3, 0, 90, 50m),
            Job(B, "decompose", "workstation-gpu", "local-ollama", 2, 0, 2, 0, 50, 50m)),
        ["device_samples"] = samples ?? new JsonArray(
            Sample(A, "workstation-gpu", 100m, 60m),
            Sample(A, "workstation-cpu", 100m, 25.5m),
            Sample(B, "workstation-gpu", 50m, 40m)),
    };

    private string Write(string text, string name = "farm.json")
    {
        var path = Path.Combine(root, name);
        File.WriteAllText(path, text);
        return path;
    }

    private static (long, long, long, long) Counts(JsonObject stored) =>
        ((long)stored["jobs"]!["new"]!, (long)stored["jobs"]!["present"]!, (long)stored["deviceSamples"]!["new"]!, (long)stored["deviceSamples"]!["present"]!);

    [Fact]
    public void ReportsUtilizationJobsPerSecondCacheHitRateAndTheAiuAvoidedEstimate()
    {
        var connection = server.Migrated();
        var empty = Cli.Json(["report", .. connection])["farm"]!;
        Assert.Equal((0, 0, (JsonNode?)null, (JsonNode?)null, (JsonNode?)null), ((int)empty["runs"]!, (int)empty["jobs"]!, empty["cacheHitRate"], empty["jobsPerSecond"], empty["aiuAvoidedEstimate"]));
        Assert.Equal(("[]", "[]", "[]"), (empty["byWorkload"]!.ToJsonString(), empty["devices"]!.ToJsonString(), empty["workloadsWithoutReference"]!.ToJsonString()));

        var path = Write(Export().ToJsonString());
        var first = Cli.Json(["ingest-farm", "--export", path, .. connection]);
        Assert.Equal(((4L, 0L, 3L, 0L), path), (Counts(first), (string)first["export"]!));
        Assert.Equal((0L, 4L, 0L, 3L), Counts(Cli.Json(["ingest-farm", "--export", path, .. connection])));

        // decompose's reference cost, from a paid run kpi_v1 measured, is 2.5 AIU per job; setting it again replaces it.
        Assert.Equal("farm-reference", (string?)Cli.Json(["farm-reference", "--workload", "decompose", "--aiu-per-job", "3", .. connection])["recorded"]);
        Cli.Json(["farm-reference", "--workload", "decompose", "--aiu-per-job", "2.5", .. connection]);

        var report = Cli.Json(["report", .. connection]);
        var farm = report["farm"]!;
        // 20 jobs, 19 completed (5 cache hits and 14 answers) in A's 100 s and B's 50 s.
        Assert.Equal((2, 20, 19, 5, 0.25m, 0.1267m), ((int)farm["runs"]!, (int)farm["jobs"]!, (int)farm["completed"]!, (int)farm["cacheHits"]!,
            (decimal)farm["cacheHitRate"]!, (decimal)farm["jobsPerSecond"]!));
        // The estimate: decompose's 15 completed jobs at 2.5 AIU; draft-rewrite has no reference, so no estimate.
        Assert.Equal((37.5m, "[\"draft-rewrite\"]"), ((decimal)farm["aiuAvoidedEstimate"]!, farm["workloadsWithoutReference"]!.ToJsonString()));

        // decompose: 16 jobs (8 + 6 + 2), 15 completed and 5 cache hits, over 150 s: B's span counts once though B ran
        // decompose on two instances.
        var workloads = farm["byWorkload"]!.AsArray();
        Assert.Equal(("decompose", 16, 15, 5, 0.3125m, 0.1m, 2.5m, 37.5m), ((string)workloads[0]!["workload"]!, (int)workloads[0]!["jobs"]!,
            (int)workloads[0]!["completed"]!, (int)workloads[0]!["cacheHits"]!, (decimal)workloads[0]!["cacheHitRate"]!, (decimal)workloads[0]!["jobsPerSecond"]!,
            (decimal)workloads[0]!["referenceAiuPerJob"]!, (decimal)workloads[0]!["aiuAvoidedEstimate"]!));
        Assert.Equal(("draft-rewrite", 4, 4, 0, 0m, 0.04m, (JsonNode?)null, (JsonNode?)null), ((string)workloads[1]!["workload"]!, (int)workloads[1]!["jobs"]!,
            (int)workloads[1]!["completed"]!, (int)workloads[1]!["cacheHits"]!, (decimal)workloads[1]!["cacheHitRate"]!, (decimal)workloads[1]!["jobsPerSecond"]!,
            workloads[1]!["referenceAiuPerJob"], workloads[1]!["aiuAvoidedEstimate"]));

        // Utilization per device: the GPU was busy 100 of 150 s, the CPU 25.5 of 100 s. The peer has jobs but no device
        // samples, since no device telemetry leaves it.
        var devices = farm["devices"]!.AsArray();
        Assert.Equal(2, devices.Count);
        Assert.Equal(("workstation-cpu", 1, 25.5m, 100m, 0.255m), ((string)devices[0]!["instanceId"]!, (int)devices[0]!["samples"]!,
            (decimal)devices[0]!["busySeconds"]!, (decimal)devices[0]!["windowSeconds"]!, (decimal)devices[0]!["utilization"]!));
        Assert.Equal(("workstation-gpu", 2, 100m, 150m, 0.6667m), ((string)devices[1]!["instanceId"]!, (int)devices[1]!["samples"]!,
            (decimal)devices[1]!["busySeconds"]!, (decimal)devices[1]!["windowSeconds"]!, (decimal)devices[1]!["utilization"]!));

        // Every other figure is the report's as before, and measured AIU stays apart from the estimate.
        report.Remove("farm");
        Assert.True(JsonNode.DeepEquals(JsonNode.Parse((string)server.Scalar(connection, "SELECT kpi_v1.report_core()::text")!), report));
        Assert.Equal(0m, (decimal)report["ai"]!["aiu"]!);
    }

    [Fact]
    public void RefusesAnExportThatChangesAnIngestedRowAndLeavesTheDatabaseUnchanged()
    {
        var connection = server.Migrated();
        Cli.Json(["ingest-farm", "--export", Write(Export().ToJsonString()), .. connection]);
        var job = Export();
        job["jobs"]![0]!["jobs"] = 9;
        job["jobs"]!.AsArray().Add(Job(C, "decompose", "workstation-gpu", "local-ollama", 1, 0, 1, 0, 10, 5m));
        Assert.Equal((ExitCodes.Refused, "projectbuddy-kpi: the export changes a farm_jobs row already ingested"),
            (Cli.Run(["ingest-farm", "--export", Write(job.ToJsonString(), "job.json"), .. connection]).Exit,
             Cli.Run(["ingest-farm", "--export", Write(job.ToJsonString(), "job.json"), .. connection]).Error.Trim()));
        var sample = Export();
        sample["device_samples"]![1]!["busy_seconds"] = 26;
        Assert.Contains("the export changes a farm_device_samples row already ingested",
            Cli.Run(["ingest-farm", "--export", Write(sample.ToJsonString(), "sample.json"), .. connection]).Error);
        Assert.Equal((4L, 20L, 3L), ((long)server.Scalar(connection, "SELECT count(*) FROM kpi_v1.farm_jobs")!,
            (long)server.Scalar(connection, "SELECT sum(jobs) FROM kpi_v1.farm_jobs")!, (long)server.Scalar(connection, "SELECT count(*) FROM kpi_v1.farm_device_samples")!));
    }

    [Fact]
    public void LeavesRangeChecksToTheDatabase()
    {
        var connection = server.Migrated();
        foreach (var export in new[]
        {
            Export(samples: [Sample(C, "peer-rtx", 10m, 1m, backend: "peer-infer")]),
            Export(samples: [Sample(C, "workstation-gpu", 10m, 10.5m)]),
            Export(jobs: [Job(C, "decompose", "workstation-gpu", "local-ollama", 2, 2, 1, 0, 10, 5m)]),
            Export(jobs: [Job("not-a-digest", "decompose", "workstation-gpu", "local-ollama", 1, 0, 1, 0, 10, 5m)]),
        })
        {
            var (exit, _, error) = Cli.Run(["ingest-farm", "--export", Write(export.ToJsonString()), .. connection]);
            Assert.Equal(ExitCodes.Database, exit);
            Assert.Contains("violates check constraint", error);
        }

        Assert.Contains("violates check constraint", Cli.Run(["farm-reference", "--workload", "Decompose", "--aiu-per-job", "1", .. connection]).Error);
        Assert.Contains("violates check constraint", Cli.Run(["farm-reference", "--workload", "decompose", "--aiu-per-job", "0", .. connection]).Error);
        Assert.Equal(ExitCodes.Usage, Cli.Run(["farm-reference", "--workload", "decompose", "--aiu-per-job", "x", .. connection]).Exit);
        Assert.Equal(0L, (long)server.Scalar(connection, "SELECT count(*) FROM kpi_v1.farm_jobs")!);
    }

    public static TheoryData<string, string> Malformed()
    {
        static string Changed(Action<JsonObject> change)
        {
            var export = Export();
            change(export);
            return export.ToJsonString();
        }

        return new()
        {
            { "{", "cannot read the farm export: " },
            { "[]", "the export must be an object with exactly schema_version, export_kind, jobs, device_samples" },
            { Changed(e => e.Remove("device_samples")), "the export must be an object with exactly" },
            { Changed(e => e["prompt"] = "text"), "the export must be an object with exactly" },
            { Changed(e => e["schema_version"] = 1), "the export.schema_version must be a JSON string" },
            { Changed(e => e["schema_version"] = "2.0.0"), "the file is not an alw-farm-telemetry 1.0.0 export" },
            { Changed(e => e["export_kind"] = "other"), "the file is not an alw-farm-telemetry 1.0.0 export" },
            { Changed(e => e["jobs"] = new JsonObject()), "the export.jobs must be a JSON array" },
            { Changed(e => e["jobs"]![0] = 1), "jobs[0] must be an object with exactly source_sha256" },
            { Changed(e => e["jobs"]![1]!["answer"] = "text"), "jobs[1] must be an object with exactly" },
            { Changed(e => e["jobs"]![0]!["workload"] = 7), "jobs[0].workload must be a JSON string" },
            { Changed(e => e["jobs"]![0]!["jobs"] = 1.5), "jobs[0].jobs must be a whole number" },
            { Changed(e => e["jobs"]![0]!["recorded_at"] = "2026-11-19T07:22:51"), "jobs[0].recorded_at must be an ISO 8601 time with an offset" },
            { Changed(e => e["device_samples"]![2]!["busy_seconds"] = JsonNode.Parse("1e999")), "device_samples[2].busy_seconds is out of range" },
        };
    }

    [Theory]
    [MemberData(nameof(Malformed))]
    public void RefusesAMalformedExport(string text, string message)
    {
        var connection = server.Migrated();
        var (exit, output, error) = Cli.Run(["ingest-farm", "--export", Write(text), .. connection]);
        Assert.Equal(ExitCodes.Refused, exit);
        Assert.Empty(output);
        Assert.StartsWith($"projectbuddy-kpi: {message}", error);
        Assert.Contains("FileNotFoundException", Cli.Run(["ingest-farm", "--export", Path.Combine(root, "absent.json"), .. connection]).Error);
    }
}
