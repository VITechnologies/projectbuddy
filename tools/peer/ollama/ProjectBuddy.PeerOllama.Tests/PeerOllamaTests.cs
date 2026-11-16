using System.Net;
using System.Text.Json.Nodes;
using Xunit;

namespace ProjectBuddy.PeerOllama.Tests;

public class JsonTests
{
    [Fact]
    public void ParseReadsObjectsAndRefusesNullAndInvalidText()
    {
        Assert.Equal(1, Json.Parse("""{"a":1}"""u8.ToArray(), "doc", ExitCodes.Refused)["a"]!.GetValue<int>());
        Assert.Equal(ExitCodes.Unreachable, Assert.Throws<PeerException>(() => Json.Parse("null"u8.ToArray(), "doc", ExitCodes.Unreachable)).ExitCode);
        Assert.Equal(ExitCodes.Refused, Assert.Throws<PeerException>(() => Json.Parse("{"u8.ToArray(), "doc", ExitCodes.Refused)).ExitCode);
    }

    [Fact]
    public void AccessorsReturnWellShapedValues()
    {
        var node = JsonNode.Parse("""{"s":"x","n":5,"list":["a","b"],"o":{}}""")!;
        Assert.Equal("x", Json.String(node["s"], "s"));
        Assert.Equal(5, Json.Integer(node["n"], "n", 0, 10));
        Assert.Equal(["a", "b"], Json.Strings(node["list"], "list"));
        Assert.Empty(Json.Object(node["o"], "o"));
        Json.Keys(node.AsObject(), "doc", "s", "n", "list", "o");
    }

    [Fact]
    public void MissingNodesAreRefused()
    {
        Assert.Throws<PeerException>(() => Json.String(null, "s"));
        Assert.Throws<PeerException>(() => Json.Integer(null, "n", 0, 1));
    }

    [Fact]
    public void KeysRefusesSameCountWithDifferentNames() =>
        Assert.Throws<PeerException>(() => Json.Keys(new JsonObject { ["a"] = 1, ["c"] = 2 }, "x", "a", "b"));

    [Fact]
    public void KeysRefusesADifferentCount() =>
        Assert.Throws<PeerException>(() => Json.Keys(new JsonObject { ["a"] = 1 }, "x", "a", "b"));

    [Fact]
    public void StringsRefusesNonStringItems() =>
        Assert.Throws<PeerException>(() => Json.Strings(JsonNode.Parse("""["a",1]"""), "list"));

    [Theory]
    [InlineData("{\"a\":{}}")]
    [InlineData("{\"a\":1}")]
    [InlineData("{\"a\":\"\"}")]
    public void StringRefusesNonStringsAndEmpty(string text) =>
        Assert.Throws<PeerException>(() => Json.String(JsonNode.Parse(text)!["a"], "a"));

    [Theory]
    [InlineData("{\"a\":{}}")]
    [InlineData("{\"a\":1.5}")]
    [InlineData("{\"a\":-1}")]
    [InlineData("{\"a\":11}")]
    public void IntegerRefusesOutOfRangeAndNonIntegers(string text) =>
        Assert.Throws<PeerException>(() => Json.Integer(JsonNode.Parse(text)!["a"], "a", 0, 10));

    [Fact]
    public void ArrayAndKeysRefuseWrongShapes()
    {
        Assert.Throws<PeerException>(() => Json.Array(new JsonObject(), "a"));
        Assert.Throws<PeerException>(() => Json.Keys(new JsonObject { ["a"] = 1, ["c"] = 2 }, "x", "a", "b"));
    }

    [Theory]
    [InlineData("not json", false)]
    [InlineData("[1]", false)]
    [InlineData("""{"step":"a","step":"b"}""", false)]
    [InlineData("{}", true)]
    public void TryObjectNeverThrows(string text, bool isObject) => Assert.Equal(isObject, Json.TryObject(text) is not null);

    [Fact]
    public void TryStringReadsOnlyStrings()
    {
        Assert.Null(Json.TryString(new JsonObject()));
        Assert.Null(Json.TryString(JsonValue.Create(3)));
        Assert.Equal("x", Json.TryString(JsonValue.Create("x")));
    }
}

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
        Assert.Equal(ExitCodes.Refused, Assert.Throws<PeerException>(() => OllamaClient.RequireLoopback(url)).ExitCode);

    [Fact]
    public async Task ReadsVersionDigestsAndResidency()
    {
        var fake = new FakeOllama();
        using var client = Client(fake);
        Assert.Equal("0.32.3", await client.VersionAsync());
        Assert.Equal(FakeOllama.Digest, (await client.DigestsAsync())[FakeOllama.Model]);
        Assert.Equal(new Residency(100, 100), await client.ResidencyAsync(FakeOllama.Model));
        Assert.Null(await client.ResidencyAsync("other:1"));
        Assert.Equal([new LoadedModel(FakeOllama.Model, "", 100, 100, 0)], await client.LoadedAsync());
        fake.Ps = new JsonObject { ["models"] = new JsonArray(new JsonObject { ["name"] = FakeOllama.Model, ["digest"] = FakeOllama.Digest, ["size"] = 5, ["size_vram"] = 4, ["context_length"] = 8192 }) };
        Assert.Equal([new LoadedModel(FakeOllama.Model, FakeOllama.Digest, 5, 4, 8192)], await client.LoadedAsync());
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
        { "redirect", (_, _) => new HttpResponseMessage(HttpStatusCode.TemporaryRedirect) { Headers = { Location = new Uri("http://10.0.0.9:11434/api/chat") } } },
        { "duplicate keys", (_, _) => new HttpResponseMessage(HttpStatusCode.OK) { Content = new StringContent("""{"message":{},"message":{}}""") } },
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
        var error = await Assert.ThrowsAsync<PeerException>(() => client.ChatAsync(new JsonObject()));
        Assert.Equal(ExitCodes.Unreachable, error.ExitCode);
        Assert.False(string.IsNullOrEmpty(label));
    }

    [Fact]
    public void LoopbackHandlerIgnoresProxiesAndRedirects()
    {
        using var handler = OllamaClient.LoopbackHandler();
        Assert.False(handler.UseProxy);
        Assert.False(handler.AllowAutoRedirect);
    }

    [Fact]
    public void ChatRequestPinsDecodingAndAddsOnlyWhatIsGiven()
    {
        var plain = OllamaClient.ChatRequest("m:1", "sys", "in", 8192, 64, 7);
        Assert.Equal("""{"num_ctx":8192,"num_predict":64,"temperature":0,"seed":7}""", plain["options"]!.ToJsonString());
        Assert.Equal("sys", plain["messages"]![0]!["content"]!.GetValue<string>());
        Assert.Null(plain["format"]);
        Assert.Null(plain["tools"]);
        var shaped = OllamaClient.ChatRequest("m:1", "", "in", 8192, 64, 7, new JsonObject(), new JsonArray());
        Assert.NotNull(shaped["format"]);
        Assert.NotNull(shaped["tools"]);
    }
}

public class GuardTests
{
    private static ChatResult Result(int prompt, int output) => new("", [], prompt, output, 1, 1);

    [Theory]
    [InlineData(1, 10, true)]
    [InlineData(4095, 10, true)]
    [InlineData(0, 10, false)]
    [InlineData(4096, 10, false)]
    [InlineData(4098, 0, false)]
    [InlineData(4000, 4500, false)]
    public void FitsOnlyBelowHalfTheContextWithRoomForTheReply(int prompt, int output, bool fits) =>
        Assert.Equal(fits, TruncationGuard.Fits(8192, Result(prompt, output)));

    [Fact]
    public void ProbeTextOutgrowsTheContext()
    {
        var text = TruncationGuard.ProbeText(64);
        Assert.Equal(128, text.Split(' ', StringSplitOptions.RemoveEmptyEntries).Length);
        Assert.StartsWith("w0 w1 ", text);
    }

    [Fact]
    public async Task ProbePassesWhenTheServerTruncatesToHalf()
    {
        var fake = new FakeOllama();
        using var client = new OllamaClient(fake, new Uri("http://127.0.0.1:11435/"), TimeSpan.FromSeconds(5));
        Assert.Equal(4098, await TruncationGuard.ProbeAsync(client, FakeOllama.Model, 8192, 1));
        var request = fake.Requests.Single().Body!;
        Assert.Equal(1, request["options"]!["num_predict"]!.GetValue<int>());
        Assert.Equal("", request["messages"]![0]!["content"]!.GetValue<string>());
    }

    [Fact]
    public async Task ProbeFailsWhenTheServerBehavesDifferently()
    {
        using var client = new OllamaClient(new FakeOllama { ProbeTokens = 4095 }, new Uri("http://127.0.0.1:11435/"), TimeSpan.FromSeconds(5));
        var error = await Assert.ThrowsAsync<PeerException>(() => TruncationGuard.ProbeAsync(client, FakeOllama.Model, 8192, 1));
        Assert.Equal(ExitCodes.GuardBroken, error.ExitCode);
    }
}

public class ModelPinTests
{
    [Fact]
    public void CreateAcceptsANameTagAndFullDigest() =>
        Assert.Equal(FakeOllama.Model, ModelPin.Create(FakeOllama.Model, FakeOllama.Digest).Name);

    [Theory]
    [InlineData("Bad Name", FakeOllama.Digest)]
    [InlineData("untagged", FakeOllama.Digest)]
    [InlineData("a:1", "845dbda0ea48")]
    public void CreateRefusesAnythingElse(string name, string digest) =>
        Assert.Equal(ExitCodes.Refused, Assert.Throws<PeerException>(() => ModelPin.Create(name, digest)).ExitCode);

    [Fact]
    public async Task RequireInstalledPassesOnlyForExactDigests()
    {
        using var client = new OllamaClient(new FakeOllama(), new Uri("http://127.0.0.1:11435/"), TimeSpan.FromSeconds(5));
        await ModelPin.RequireInstalledAsync(client, [ModelPin.Create(FakeOllama.Model, FakeOllama.Digest)]);
        await Assert.ThrowsAsync<PeerException>(() => ModelPin.RequireInstalledAsync(client, [ModelPin.Create("other:1", FakeOllama.Digest)]));
        var error = await Assert.ThrowsAsync<PeerException>(() => ModelPin.RequireInstalledAsync(client, [ModelPin.Create(FakeOllama.Model, new string('0', 64))]));
        Assert.Equal(ExitCodes.Refused, error.ExitCode);
    }
}
