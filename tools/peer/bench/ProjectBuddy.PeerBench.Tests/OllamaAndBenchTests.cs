using System.Net;
using System.Text.Json.Nodes;
using Xunit;

namespace ProjectBuddy.PeerBench.Tests;

public class OllamaTests
{
    private static OllamaClient Client(FakeOllama fake) => new(fake, new Uri("http://127.0.0.1:11435/"), TimeSpan.FromSeconds(5));

    [Theory]
    [InlineData("http://127.0.0.1:11434")]
    [InlineData("http://127.0.0.1:11434/")]
    [InlineData("http://[::1]:11434")]
    public void LoopbackIpLiteralsAreAccepted(string url) => Assert.True(OllamaClient.RequireLoopback(url).IsLoopback);

    [Theory]
    [InlineData("not a url")]
    [InlineData("https://127.0.0.1:11434")]
    [InlineData("http://localhost:11434")]
    [InlineData("http://10.211.0.2:11434")]
    [InlineData("http://127.0.0.1:11434/api")]
    [InlineData("http://127.0.0.1:11434/?x=1")]
    [InlineData("http://user@127.0.0.1:11434")]
    public void EverythingElseIsRefused(string url) =>
        Assert.Equal(ExitCodes.Refused, Assert.Throws<BenchException>(() => OllamaClient.RequireLoopback(url)).ExitCode);

    [Fact]
    public async Task ReadsVersionDigestsAndResidency()
    {
        var fake = new FakeOllama();
        using var client = Client(fake);
        Assert.Equal("0.32.3", await client.VersionAsync());
        Assert.Equal(FakeOllama.Digest, (await client.DigestsAsync())[FakeOllama.Model]);
        Assert.Equal(new Residency(100, 100), await client.ResidencyAsync(FakeOllama.Model));
        Assert.Null(await client.ResidencyAsync("other:1"));
        await client.UnloadAsync(FakeOllama.Model);
        Assert.Equal(0, fake.Requests[^1].Body!["keep_alive"]!.GetValue<int>());
    }

    [Fact]
    public async Task ParsesChatCountersAndToolCalls()
    {
        var fake = new FakeOllama { Respond = (_, _) => FakeOllama.Ok(FakeOllama.Reply("text", 120, 40, FakeOllama.Call("ask_peer", "vbox"))) };
        using var client = Client(fake);
        var result = await client.ChatAsync(new JsonObject { ["model"] = "m" });
        Assert.Equal(("text", 120, 40, 2.0, 40.0), (result.Content, result.PromptTokens, result.OutputTokens, result.Seconds, result.TokensPerSecond));
        Assert.Equal("vbox", result.ToolCalls.Single().Arguments!["verb"]!.GetValue<string>());
    }

    [Fact]
    public async Task MissingOrInvalidCountersReadAsZero()
    {
        var reply = new JsonObject { ["message"] = new JsonObject(), ["prompt_eval_count"] = -5, ["eval_count"] = "many" };
        var fake = new FakeOllama { Respond = (_, _) => FakeOllama.Ok(reply) };
        using var client = Client(fake);
        var result = await client.ChatAsync(new JsonObject());
        Assert.Equal(("", 0, 0, 0.0, 0.0), (result.Content, result.PromptTokens, result.OutputTokens, result.Seconds, result.TokensPerSecond));
        Assert.Empty(result.ToolCalls);
    }

    public static TheoryData<string, Func<string, JsonObject?, HttpResponseMessage>> Faults => new()
    {
        { "http 500", (_, _) => new HttpResponseMessage(HttpStatusCode.InternalServerError) },
        { "not json", (_, _) => new HttpResponseMessage(HttpStatusCode.OK) { Content = new StringContent("<html>") } },
        { "not object", (_, _) => FakeOllama.Ok(new JsonArray()) },
        { "transport", (_, _) => throw new HttpRequestException("refused") },
        { "timeout", (_, _) => throw new TaskCanceledException("slow") },
        { "no message", (_, _) => FakeOllama.Ok(new JsonObject()) },
        { "bad tool call", (_, _) => FakeOllama.Ok(new JsonObject { ["message"] = new JsonObject { ["tool_calls"] = new JsonArray(1) } }) },
    };

    [Theory]
    [MemberData(nameof(Faults))]
    public async Task ServerFaultsAreUnreachable(string label, Func<string, JsonObject?, HttpResponseMessage> respond)
    {
        using var client = Client(new FakeOllama { Respond = respond });
        var error = await Assert.ThrowsAsync<BenchException>(() => client.ChatAsync(new JsonObject()));
        Assert.Equal(ExitCodes.Unreachable, error.ExitCode);
        Assert.False(string.IsNullOrEmpty(label));
    }
}

public class BenchTests
{
    private static Corpus Corpus() => Fixtures.Parse(Fixtures.Corpus());

    private static IReadOnlyList<Candidate> Candidates() => Candidate.ParseAll(System.Text.Encoding.UTF8.GetBytes(Fixtures.Candidates));

    private static async Task<JsonObject> Run(FakeOllama fake, Corpus? corpus = null)
    {
        using var client = new OllamaClient(fake, new Uri("http://127.0.0.1:11435/"), TimeSpan.FromSeconds(5));
        return await Bench.RunAsync(corpus ?? Corpus(), Candidates(), "peer", client, TextWriter.Null);
    }

    private static FakeOllama AllCorrect()
    {
        var fake = new FakeOllama();
        fake.Chats["step one\nFAIL gate-x -- broken thing\nexit 1"] = FakeOllama.Reply("""{"step":"gate-x","errorLine":"FAIL gate-x -- broken thing","class":"gate-failure"}""");
        fake.Chats["echo $TOKEN"] = FakeOllama.Reply("""{"findings":[{"class":"secret-in-log","line":1}]}""");
        fake.Chats["safe code"] = FakeOllama.Reply("""{"findings":[]}""", output: 3);
        fake.Chats["is the vm running?"] = FakeOllama.Reply("", toolCalls: FakeOllama.Call(Bench.ToolName, "vbox"));
        return fake;
    }

    [Fact]
    public async Task PerfectRunScoresEverythingAndUnloads()
    {
        var fake = AllCorrect();
        var result = await Run(fake);
        var model = result["models"]![0]!;
        Assert.Equal("peer", result["machine"]!.GetValue<string>());
        Assert.Equal("""{"items":1,"extraction":1,"diagnosis":1}""", model["triage"]!.ToJsonString());
        Assert.Equal("""{"expected":1,"hits":1,"falsePositives":0}""", model["security"]!.ToJsonString());
        Assert.Equal("""{"items":1,"hits":1}""", model["tool"]!.ToJsonString());
        Assert.True(model["vram"]!["fullGpu"]!.GetValue<bool>());
        Assert.Equal(4098, model["probePromptTokens"]!.GetValue<int>());
        Assert.Equal(2.0, model["latency"]!["medianSeconds"]!.GetValue<double>());
        Assert.All(model["items"]!.AsArray(), item => Assert.Equal("pass", item!["outcome"]!.GetValue<string>()));
        Assert.Equal("/api/generate", fake.Requests[^1].Path);

        var triageRequest = fake.Requests.Single(request => request.Body?["format"]?["properties"]?["step"] is not null).Body!;
        Assert.Equal(8192, triageRequest["options"]!["num_ctx"]!.GetValue<int>());
        Assert.Null(triageRequest["tools"]);
        Assert.NotNull(fake.Requests.Single(request => request.Body?["tools"] is not null).Body);
    }

    [Fact]
    public async Task WrongAnswersFailWithoutCrashing()
    {
        var fake = new FakeOllama();
        fake.Chats["step one\nFAIL gate-x -- broken thing\nexit 1"] = FakeOllama.Reply("""{"step":"gate-x","errorLine":"made up line","class":"segfault"}""");
        fake.Chats["echo $TOKEN"] = FakeOllama.Reply("""{"findings":[{"class":"command-injection","line":1}]}""");
        fake.Chats["safe code"] = FakeOllama.Reply("""{"findings":[{"class":"secret-in-log","line":1}]}""");
        fake.Chats["is the vm running?"] = FakeOllama.Reply("", toolCalls: FakeOllama.Call("ask_other", "vbox"));
        var model = (await Run(fake))["models"]![0]!;
        Assert.Equal("""{"items":1,"extraction":0,"diagnosis":0}""", model["triage"]!.ToJsonString());
        Assert.Equal("""{"expected":1,"hits":0,"falsePositives":2}""", model["security"]!.ToJsonString());
        Assert.Equal(0, model["tool"]!["hits"]!.GetValue<int>());
        Assert.All(model["items"]!.AsArray(), item => Assert.Equal("fail", item!["outcome"]!.GetValue<string>()));
    }

    [Fact]
    public async Task MalformedAnswersFailClosed()
    {
        var fake = new FakeOllama();
        fake.Chats["step one\nFAIL gate-x -- broken thing\nexit 1"] = FakeOllama.Reply("""{"step":"gate-x"}""");
        fake.Chats["echo $TOKEN"] = FakeOllama.Reply("""{"findings":[{"class":"weather","line":1}]}""");
        fake.Chats["safe code"] = FakeOllama.Reply("""{"findings":[7]}""");
        fake.Chats["is the vm running?"] = FakeOllama.Reply("", toolCalls: FakeOllama.Call(Bench.ToolName, null));
        var model = (await Run(fake))["models"]![0]!;
        Assert.Equal(4, model["malformed"]!.GetValue<int>());
        Assert.Equal(1, model["security"]!["expected"]!.GetValue<int>());
    }

    [Fact]
    public async Task NonJsonAndMissingToolCallsAreScored()
    {
        var fake = new FakeOllama();
        fake.Chats["step one\nFAIL gate-x -- broken thing\nexit 1"] = FakeOllama.Reply("The gate failed.");
        fake.Chats["echo $TOKEN"] = FakeOllama.Reply("I think it is fine.");
        var model = (await Run(fake))["models"]![0]!;
        Assert.Equal(3, model["malformed"]!.GetValue<int>());
        Assert.Equal("fail", model["items"]![3]!["outcome"]!.GetValue<string>());
    }

    [Theory]
    [InlineData(0, 10)]
    [InlineData(4096, 10)]
    [InlineData(4000, 4500)]
    public async Task PromptsThatMayHaveBeenTruncatedAreRejected(int prompt, int output)
    {
        var fake = AllCorrect();
        foreach (var key in fake.Chats.Keys.ToList())
        {
            fake.Chats[key] = FakeOllama.Reply("{}", prompt, output);
        }

        var model = (await Run(fake))["models"]![0]!;
        Assert.Equal(4, model["rejected"]!.GetValue<int>());
        Assert.Equal(0, model["triage"]!["items"]!.GetValue<int>());
    }

    [Fact]
    public async Task BrokenTruncationProbeStopsTheRun()
    {
        var fake = new FakeOllama { ProbeTokens = 4095 };
        Assert.Equal(ExitCodes.GuardBroken, (await Assert.ThrowsAsync<BenchException>(() => Run(fake))).ExitCode);
    }

    [Theory]
    [InlineData("other:1", FakeOllama.Digest)]
    [InlineData(FakeOllama.Model, "0000000000000000000000000000000000000000000000000000000000000000")]
    public async Task UnpinnedModelsAreRefused(string name, string digest)
    {
        var fake = new FakeOllama();
        var tags = new JsonObject { ["models"] = new JsonArray(new JsonObject { ["name"] = name, ["digest"] = digest }) };
        var original = fake.Respond;
        fake.Respond = (path, body) => path == "/api/tags" ? FakeOllama.Ok(tags) : original(path, body);
        Assert.Equal(ExitCodes.Refused, (await Assert.ThrowsAsync<BenchException>(() => Run(fake))).ExitCode);
    }

    [Theory]
    [InlineData(null, null, false, false)]
    [InlineData(0L, 0L, true, false)]
    [InlineData(100L, 60L, true, false)]
    [InlineData(100L, 100L, true, true)]
    public async Task VramFitNeedsTheWholeModelOnTheGpu(long? size, long? vram, bool loaded, bool fullGpu)
    {
        var fake = AllCorrect();
        fake.Ps = new JsonObject
        {
            ["models"] = size is null ? new JsonArray() : new JsonArray(new JsonObject { ["name"] = FakeOllama.Model, ["size"] = size, ["size_vram"] = vram }),
        };
        var residency = (await Run(fake))["models"]![0]!["vram"]!;
        Assert.Equal(loaded, residency["loaded"]!.GetValue<bool>());
        Assert.Equal(fullGpu, residency["fullGpu"]!.GetValue<bool>());
        Assert.Equal(4, fake.Requests.Count(request => request.Path == "/api/chat" && request.Body!["messages"]![0]!["content"]!.GetValue<string>().Length > 0));
        Assert.Single(fake.Requests, request => request.Path == "/api/ps");
    }

    [Fact]
    public async Task OddItemCountsUseTheMiddleLatencyAndZeroRatesAreSkipped()
    {
        var json = Fixtures.Corpus();
        json["items"]!.AsArray().RemoveAt(3);
        var fake = new FakeOllama();
        var slow = FakeOllama.Reply("{}");
        slow["eval_duration"] = 0;
        fake.Respond = (path, body) => path == "/api/chat" && body!["messages"]![0]!["content"]!.GetValue<string>().Length > 0
            ? FakeOllama.Ok(slow)
            : new FakeOllama().Respond(path, body);
        var model = (await Run(fake, Fixtures.Parse(json)))["models"]![0]!;
        Assert.Equal(2.0, model["latency"]!["medianSeconds"]!.GetValue<double>());
        Assert.Equal(0.0, model["latency"]!["meanTokensPerSecond"]!.GetValue<double>());
    }

    [Fact]
    public void ProbeTextOutgrowsTheContext()
    {
        var text = Bench.ProbeText(64);
        Assert.Equal(128, text.Split(' ', StringSplitOptions.RemoveEmptyEntries).Length);
        Assert.StartsWith("w0 w1 ", text);
    }
}
