using System.Text;
using System.Text.Json.Nodes;
using ProjectBuddy.PeerOllama;
using Xunit;

namespace ProjectBuddy.PeerAdvise.Tests;

public class ProgramTests
{
    private static readonly string[] Valid = ["triage", "--policy", "p.json", "--input", "log.txt", "--ollama", "http://127.0.0.1:11435"];

    private static (int Code, string Out, string Err) Run(string[] args, FakeOllama? ollama = null, Func<string, byte[]>? read = null, string stdin = "")
    {
        var files = new Dictionary<string, byte[]>
        {
            ["p.json"] = Fixtures.Bytes(Fixtures.Policy()),
            ["log.txt"] = Encoding.UTF8.GetBytes(Fixtures.Log),
        };
        if (ollama is null)
        {
            ollama = new FakeOllama();
            ollama.Replies.Enqueue(FakeOllama.Answer("gate-x", "FAIL  gate-x  -- broken thing", "gate-failure"));
        }

        using var stdout = new StringWriter();
        using var stderr = new StringWriter();
        var code = Program.Run(args, read ?? (path => files[path]), () => Encoding.UTF8.GetBytes(stdin), ollama, stdout, stderr);
        return (code, stdout.ToString(), stderr.ToString());
    }

    [Fact]
    public void VersionPrintsToolAndSchema()
    {
        var (code, output, _) = Run(["--version"]);
        Assert.Equal(0, code);
        Assert.Equal($"{Versions.Tool} {Versions.TriageSchema} {Versions.PreReviewSchema}", output.Trim());
    }

    [Fact]
    public void MainAnswersVersion() => Assert.Equal(0, Program.Main(["--version"]));

    [Theory]
    [InlineData]
    [InlineData("review")]
    [InlineData("triage")]
    [InlineData("triage", "--policy")]
    [InlineData("triage", "--bogus", "x")]
    [InlineData("triage", "--policy", "a", "--policy", "b")]
    [InlineData("triage", "--input", "log.txt")]
    [InlineData("triage", "--policy", "a", "--timeout-seconds", "soon")]
    [InlineData("triage", "--policy", "a", "--timeout-seconds", "0")]
    [InlineData("triage", "--policy", "a", "--timeout-seconds", "3601")]
    public void BadArgumentsPrintUsage(params string[] args)
    {
        var (code, _, error) = Run(args);
        Assert.Equal(ExitCodes.Usage, code);
        Assert.Contains("usage:", error);
    }

    [Fact]
    public void DefaultsApplyWhenOptionalFlagsAreAbsent()
    {
        Assert.True(Program.TryParse(["--policy", "a"], out var options));
        Assert.Equal(Program.DefaultOllama, options["--ollama"]);
        Assert.Equal("300", options["--timeout-seconds"]);
        Assert.False(options.ContainsKey("--input"));
    }

    [Fact]
    public void RunWritesTheReportFromAFile()
    {
        var (code, output, error) = Run(Valid);
        Assert.Equal(0, code);
        Assert.Equal("", error);
        Assert.Equal(Versions.TriageSchema, JsonNode.Parse(output)!["schema"]!.GetValue<string>());
    }

    [Fact]
    public void PreReviewVerbWritesItsReport()
    {
        var ollama = new FakeOllama();
        ollama.Replies.Enqueue(FakeOllama.Reply("""{"findings":[]}"""));
        var (code, output, _) = Run(["pre-review", "--policy", "p.json", "--ollama", "http://127.0.0.1:11435"], ollama, stdin: Fixtures.Diff);
        Assert.Equal(0, code);
        Assert.Equal(Versions.PreReviewSchema, JsonNode.Parse(output)!["schema"]!.GetValue<string>());
    }

    [Fact]
    public void ReportsKeepQuotesReadable()
    {
        var ollama = new FakeOllama();
        ollama.Replies.Enqueue(FakeOllama.Answer("verify", "FAIL  gate-x  -- broken thing", "gate-failure"));
        var (_, output, _) = Run(Valid, ollama, read: path => path == "p.json" ? Fixtures.Bytes(Fixtures.Policy()) : Encoding.UTF8.GetBytes("build\tverify\t2026-11-15T09:26:26Z FAIL  gate-x  -- broken thing \"quoted\""));
        Assert.Contains("\"errorLine\":\"FAIL  gate-x  -- broken thing \\\"quoted\\\"\"", output);
        Assert.DoesNotContain("\\u0022", output);
    }

    [Fact]
    public void RunReadsStandardInputWithoutInputFlag()
    {
        var (code, output, _) = Run(["triage", "--policy", "p.json", "--ollama", "http://127.0.0.1:11435"], stdin: Fixtures.Log);
        Assert.Equal(0, code);
        Assert.Equal("gate-failure", JsonNode.Parse(output)!["answer"]!["class"]!.GetValue<string>());
    }

    [Fact]
    public void FailuresPrintNoAdviceAtAll()
    {
        var ollama = new FakeOllama();
        ollama.Replies.Enqueue(FakeOllama.Answer("gate-x", "invented line", "gate-failure"));
        var (code, output, error) = Run(Valid, ollama);
        Assert.Equal(AdviseExitCodes.NoAdvice, code);
        Assert.Equal("", output);
        Assert.StartsWith("projectbuddy-advise: no advice:", error);
    }

    [Fact]
    public void UnreadableFileIsRefusedWithoutItsPath()
    {
        var (code, _, error) = Run(Valid, read: path => throw new UnauthorizedAccessException($"/secret/dir/{path}"));
        Assert.Equal(ExitCodes.Refused, code);
        Assert.Contains("cannot read p.json", error);
        Assert.DoesNotContain("/secret/dir", error);
    }

    [Fact]
    public void NonLoopbackOllamaIsRefused()
    {
        var args = Valid.ToArray();
        args[^1] = "http://10.211.0.2:11434";
        var (code, _, error) = Run(args);
        Assert.Equal(ExitCodes.Refused, code);
        Assert.Contains("loopback", error);
    }
}

public class PolicyTests
{
    [Fact]
    public void CommittedPolicyIsValid()
    {
        var policy = Policy.Parse(File.ReadAllBytes(Fixtures.RepositoryFile("policy.json")));
        Assert.Equal("qwen2.5:7b", policy.Model.Name);
        Assert.True(policy.MaxInputBytes * 2 <= policy.NumCtx * 4);
        Assert.Contains("segfault", policy.TriageClasses);
        Assert.Contains(policy.PreReview.Rules, rule => rule.Id == "fail-open-check" && rule.Severity == "blocker");
        Assert.True(policy.PreReview.MaxFindings > 0 && policy.PreReview.MaxBatches > 0);
    }

    [Fact]
    public void ParsesFieldsAndHashesTheBytes()
    {
        var bytes = Fixtures.Bytes(Fixtures.Policy());
        var policy = Policy.Parse(bytes);
        Assert.Equal(Convert.ToHexStringLower(System.Security.Cryptography.SHA256.HashData(bytes)), policy.Sha256);
        Assert.Equal((16384, 256, 42, 12000, "triage it"), (policy.NumCtx, policy.NumPredict, policy.Seed, policy.MaxInputBytes, policy.TriagePrompt));
        Assert.Equal(Fixtures.Classes, policy.TriageClasses);
    }

    public static TheoryData<string, Action<JsonObject>> Refusals => new()
    {
        { "schema", policy => policy["schema"] = "other@1" },
        { "keys", policy => policy.Remove("seed") },
        { "model keys", policy => policy["model"]!.AsObject().Remove("digest") },
        { "model name", policy => policy["model"]!["name"] = "Bad Name" },
        { "numCtx", policy => policy["numCtx"] = 2048 },
        { "numPredict", policy => policy["numPredict"] = 4096 },
        { "maxInputBytes small", policy => policy["maxInputBytes"] = 100 },
        { "maxInputBytes large", policy => policy["maxInputBytes"] = 16384 * 4 + 1 },
        { "triage keys", policy => policy["triage"]!.AsObject().Remove("prompt") },
        { "no classes", policy => policy["triage"]!["classes"] = new JsonArray() },
        { "repeated classes", policy => policy["triage"]!["classes"] = new JsonArray("a", "a") },
        { "no preReview", policy => policy.Remove("preReview") },
        { "preReview keys", policy => policy["preReview"]!.AsObject().Remove("maxBatches") },
        { "preReview numPredict", policy => policy["preReview"]!["numPredict"] = 32 },
        { "maxFindings", policy => policy["preReview"]!["maxFindings"] = 0 },
        { "maxBatches", policy => policy["preReview"]!["maxBatches"] = 65 },
        { "no rules", policy => policy["preReview"]!["rules"] = new JsonArray() },
        { "rule keys", policy => policy["preReview"]!["rules"]![0]!.AsObject().Remove("guidance") },
        { "rule id", policy => policy["preReview"]!["rules"]![0]!["id"] = "Bad Id" },
        { "rule severity", policy => policy["preReview"]!["rules"]![0]!["severity"] = "critical" },
        { "repeated rule", policy => policy["preReview"]!["rules"]!.AsArray().Add(Fixtures.Rule("naming", "nit")) },
    };

    [Theory]
    [MemberData(nameof(Refusals))]
    public void InvalidPolicyIsRefused(string label, Action<JsonObject> mutate)
    {
        var policy = Fixtures.Policy();
        mutate(policy);
        Assert.Equal(ExitCodes.Refused, Assert.Throws<PeerException>(() => Fixtures.ParsePolicy(policy)).ExitCode);
        Assert.False(string.IsNullOrEmpty(label));
    }
}
