using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;
using ProjectBuddy.PeerOllama;
using ProjectBuddy.PeerProbes;
using ProjectBuddy.PeerRedact;
using Xunit;

namespace ProjectBuddy.PeerDiagnose.Tests;

public sealed class DiagnosisTests
{
    private static readonly JsonObject None = [];

    private static async Task<JsonObject> Run(FakeOllama ollama, Policy policy, ISources? sources = null, Func<string, Redaction>? redact = null)
    {
        using var client = new OllamaClient(ollama, new Uri("http://127.0.0.1:11434/"), TimeSpan.FromSeconds(30));
        return await Diagnosis.RunAsync(policy, policy.Questions[0], client, Fixtures.Registry(sources ?? new FakeSources()),
            redact ?? (text => new Redaction(text, new Dictionary<string, int>())));
    }

    private static JsonArray Messages(JsonObject request) => request["messages"]!.AsArray();

    private static string Content(JsonObject request, int index) => Messages(request)[index]!["content"]!.GetValue<string>();

    [Fact]
    public async Task AnswersFromAToolResultItWasGiven()
    {
        var ollama = new FakeOllama();
        ollama.Replies.Enqueue(FakeOllama.Calls(("network-link", None)));
        ollama.Replies.Enqueue(FakeOllama.Reply("I have what I need."));
        ollama.Replies.Enqueue(FakeOllama.Answer("link-up", "Wi-Fi is up.", ("network-link", "/data/adapters/0/status", "up")));
        var sources = new FakeSources();
        var report = await Run(ollama, Fixtures.Parse(), sources);

        var expected = await Fixtures.Registry(new FakeSources()).InvokeAsync("network-link", null);
        Assert.Equal(Versions.ReportSchema, (string?)report["schema"]);
        Assert.Equal("projectbuddy-diagnose 0.1.0", (string?)report["tool"]);
        Assert.True((bool)report["advisory"]!);
        Assert.Equal("github-unreachable", (string?)report["question"]);
        Assert.Equal("""["citations","class"]""", report["verified"]!.ToJsonString());
        Assert.Equal(
            """{"class":"link-up","basis":[{"tool":"network-link","pointer":"/data/adapters/0/status","value":"up"},{"tool":"network-link","pointer":"/data/adapters/0/gateways/0","value":"192.168.1.1"}]}""",
            report["rule"]!.ToJsonString());
        Assert.Equal("""{"class":"link-up","finding":"Wi-Fi is up.","citations":[{"tool":"network-link","pointer":"/data/adapters/0/status","value":"up"}]}""", report["answer"]!.ToJsonString());
        Assert.Equal(
            $$"""[{"tool":"network-link","arguments":{},"sha256":"{{Convert.ToHexStringLower(SHA256.HashData(Encoding.UTF8.GetBytes(expected)))}}"}]""",
            report["evidence"]!.ToJsonString());
        Assert.Equal("""{"name":"qwen2.5:7b","digest":"845dbda0ea48ed749caafd9e6037047aa19acfcfd82e704d7ca97d631a0b697e"}""", report["model"]!.ToJsonString());
        Assert.Equal("0.40.16", (string?)report["ollamaVersion"]);
        Assert.Equal(Fixtures.Parse().Sha256, (string?)report["policySha256"]);
        Assert.Equal(
            """{"numCtx":16384,"probePromptTokens":8194,"requests":3,"rounds":2,"toolCalls":1,"refusedCalls":0,"answerAttempts":1,"evidenceBytes":""" + Encoding.UTF8.GetByteCount(expected)
                + ""","maxPromptTokens":500,"outputTokens":120,"seconds":6}""",
            report["receipt"]!.ToJsonString());
        Assert.Equal(1, sources.AdapterReads);

        // The loop offers the tools; the answer is asked for without them, in the answer's shape, after the tool result.
        var chats = ollama.Chats.ToList();
        Assert.Equal(3, chats.Count);
        Assert.True(JsonNode.DeepEquals(Diagnosis.ToolDefinitions(), chats[0]["tools"]));
        Assert.Null(chats[0]["format"]);
        Assert.Null(chats[2]["tools"]);
        Assert.True(JsonNode.DeepEquals(Diagnosis.AnswerFormat(Fixtures.Parse(), Fixtures.Parse().Questions[0], "link-up"), chats[2]["format"]));
        Assert.Equal("""["link-up"]""", chats[2]["format"]!["properties"]!["class"]!["enum"]!.ToJsonString());
        Assert.Equal(["system", "user", "assistant", "tool", "user"], Messages(chats[2]).Select(message => (string?)message!["role"]));
        Assert.Equal("""[{"function":{"name":"network-link","arguments":{}}}]""", Messages(chats[2])[2]!["tool_calls"]!.ToJsonString());
        Assert.Equal("network-link", (string?)Messages(chats[2])[3]!["tool_name"]);
        Assert.Equal(expected, Content(chats[2], 3));
        Assert.Equal("answer now", Content(chats[2], 4));
        Assert.Equal(Diagnosis.Prompt(Fixtures.Parse().Questions[0]), Content(chats[0], 1));
        Assert.Equal(
            "Is the link the cause?\n\nClasses, decided by the first rule that holds:\n- no-active-link: no network-link /data/adapters/* with /status = \"up\"\n"
                + "- link-up: some network-link /data/adapters/* with /status = \"up\" and /gateways/0 present\n"
                + "When the results cannot decide these rules, the class is your judgment; use inconclusive if they do not answer the question.",
            Content(chats[0], 1));
    }

    [Fact]
    public async Task HoldsTheModelToTheClassItsRulesDecide()
    {
        // The shape allows only the decided class; a server that ignored it still cannot get a contradiction through.
        var ollama = new FakeOllama();
        ollama.Replies.Enqueue(FakeOllama.Calls(("network-link", None)));
        ollama.Replies.Enqueue(FakeOllama.Reply(""));
        ollama.Replies.Enqueue(FakeOllama.Answer("no-active-link", "No link.", ("network-link", "/data/adapters/0/status", "up")));
        ollama.Replies.Enqueue(FakeOllama.Answer("link-up", "Wi-Fi is up.", ("network-link", "/data/adapters/0/status", "up")));
        var policy = Fixtures.Parse(change => change["maxAnswerAttempts"] = 2);
        var report = await Run(ollama, policy);

        Assert.Equal("link-up", (string?)report["answer"]!["class"]);
        Assert.Equal(2, (int)report["receipt"]!["answerAttempts"]!);
        Assert.Null(report["receipt"]!["classCorrected"]);
        var retry = ollama.Chats.Last();
        Assert.Equal(
            "That answer was rejected: class \"no-active-link\" contradicts the policy's rules, which decide link-up from the results. "
                + "Answer again with JSON only; cite exact single values at pointers that exist in the tool results above.",
            Content(retry, 6));
        Assert.All(ollama.Chats.Skip(2), request => Assert.Equal("""["link-up"]""", request["format"]!["properties"]!["class"]!["enum"]!.ToJsonString()));

        ollama.Replies.Enqueue(FakeOllama.Calls(("network-link", None)));
        ollama.Replies.Enqueue(FakeOllama.Reply(""));
        ollama.Replies.Enqueue(FakeOllama.Answer("no-active-link", "No link.", ("network-link", "/data/adapters/0/status", "up")));
        var refused = await Assert.ThrowsAsync<PeerException>(() => Run(ollama, Fixtures.Parse()));
        Assert.Equal(DiagnoseExitCodes.NoAdvice, refused.ExitCode);
        Assert.Contains("contradicts the policy's rules", refused.Message);
    }

    [Fact]
    public async Task RefusesCallsOutsideTheRegistry()
    {
        var ollama = new FakeOllama();
        ollama.Replies.Enqueue(FakeOllama.Calls(("shell", None), ("event-errors", new JsonObject { ["hours"] = 0 })));
        ollama.Replies.Enqueue(FakeOllama.Calls(("disk", null)));
        ollama.Replies.Enqueue(FakeOllama.Reply(""));
        ollama.Replies.Enqueue(FakeOllama.Answer("link-up", "Disk read.", ("disk", "/data/drives/0/freePercent", 5)));
        var report = await Run(ollama, Fixtures.Parse());

        var answer = ollama.Chats.Last();
        Assert.Equal("""{"refused":"unknown tool 'shell'"}""", Content(answer, 3));
        Assert.Equal("""{"refused":"event-errors argument 'hours' must be 1-72"}""", Content(answer, 4));
        Assert.Equal("""[{"function":{"name":"disk","arguments":{}}}]""", Messages(answer)[5]!["tool_calls"]!.ToJsonString());
        Assert.Equal((3, 2), ((int)report["receipt"]!["toolCalls"]!, (int)report["receipt"]!["refusedCalls"]!));

        // Without a network-link result the rules cannot decide, so the class stays the model's, advisory and free.
        Assert.Equal("""["citations"]""", report["verified"]!.ToJsonString());
        Assert.Null(report["rule"]);
        Assert.Equal("""["link-up","no-active-link","inconclusive"]""", answer["format"]!["properties"]!["class"]!["enum"]!.ToJsonString());
        Assert.Equal("""[{"tool":"disk","arguments":{}}]""", new JsonArray([.. report["evidence"]!.AsArray().Select(item => (JsonNode)new JsonObject
        {
            ["tool"] = item!["tool"]!.DeepClone(),
            ["arguments"] = item["arguments"]!.DeepClone(),
        })]).ToJsonString());
    }

    [Fact]
    public async Task KeepsToTheCallBudgets()
    {
        var ollama = new FakeOllama();
        ollama.Replies.Enqueue(FakeOllama.Calls(("disk", None), ("network-link", None), ("ollama", None)));
        ollama.Replies.Enqueue(FakeOllama.Calls(("ollama", None), ("vm-states", None)));
        ollama.Replies.Enqueue(FakeOllama.Reply(""));
        ollama.Replies.Enqueue(FakeOllama.Answer("link-up", "Wi-Fi is up.", ("network-link", "/data/adapters/0/status", "up")));
        var report = await Run(ollama, Fixtures.Parse(policy => policy["maxCalls"] = 3));

        var answer = ollama.Chats.Last();
        Assert.Equal("""{"refused":"the call budget is spent; answer with the results you have"}""", Content(answer, 5));
        Assert.Equal("""{"reachable":false}""", JsonNode.Parse(Content(answer, 7))!["data"]!.ToJsonString());
        Assert.Equal("""{"refused":"the call budget is spent; answer with the results you have"}""", Content(answer, 8));
        Assert.Equal((3, 2), ((int)report["receipt"]!["toolCalls"]!, (int)report["receipt"]!["refusedCalls"]!));
    }

    [Fact]
    public async Task AnswersARepeatedCallWithItsFirstResult()
    {
        var ollama = new FakeOllama();
        ollama.Replies.Enqueue(FakeOllama.Calls(("disk", None)));
        ollama.Replies.Enqueue(FakeOllama.Calls(("disk", None)));
        ollama.Replies.Enqueue(FakeOllama.Reply(""));
        ollama.Replies.Enqueue(FakeOllama.Answer("link-up", "Disk read.", ("disk", "/data/drives/0/freePercent", 5)));
        var sources = new FakeSources();
        var report = await Run(ollama, Fixtures.Parse(), sources);

        Assert.Equal(1, sources.DriveReads);
        Assert.Single(report["evidence"]!.AsArray());
        var answer = ollama.Chats.Last();
        Assert.Equal(Content(answer, 3), Content(answer, 5));
        Assert.Equal(2 * Encoding.UTF8.GetByteCount(Content(answer, 3)), (int)report["receipt"]!["evidenceBytes"]!);
    }

    [Fact]
    public async Task KeepsToTheEvidenceBudget()
    {
        var sources = new FakeSources();
        sources.AdapterList.AddRange(Enumerable.Range(0, 15).Select(index => new Adapter($"vEthernet {index:D2}", "ethernet", "up", 10000, true, true, [])));
        var ollama = new FakeOllama();
        ollama.Replies.Enqueue(FakeOllama.Calls(("disk", None), ("network-link", None)));
        ollama.Replies.Enqueue(FakeOllama.Reply(""));
        ollama.Replies.Enqueue(FakeOllama.Answer("link-up", "Disk read.", ("disk", "/data/drives/0/freePercent", 5)));
        var report = await Run(ollama, Fixtures.Parse(policy => policy["maxEvidenceBytes"] = 1024), sources);

        Assert.Equal("""{"refused":"the evidence budget is spent; answer with the results you have"}""", Content(ollama.Chats.Last(), 4));
        Assert.Equal("disk", (string?)Assert.Single(report["evidence"]!.AsArray())!["tool"]);
        Assert.Equal(1, (int)report["receipt"]!["refusedCalls"]!);
    }

    [Fact]
    public async Task StopsCallingToolsAfterTheLastRound()
    {
        var ollama = new FakeOllama();
        ollama.Replies.Enqueue(FakeOllama.Calls(("disk", None)));
        ollama.Replies.Enqueue(FakeOllama.Calls(("network-link", None)));
        ollama.Replies.Enqueue(FakeOllama.Answer("link-up", "Wi-Fi is up.", ("network-link", "/data/adapters/0/status", "up")));
        var report = await Run(ollama, Fixtures.Parse(policy => policy["maxRounds"] = 2));

        Assert.Equal((2, 3), ((int)report["receipt"]!["rounds"]!, (int)report["receipt"]!["requests"]!));
        Assert.Empty(ollama.Replies);
    }

    [Theory]
    [InlineData(0)]
    [InlineData(1)]
    public async Task TrustsNothingFromARequestThatMayHaveBeenCut(int cutRequest)
    {
        var ollama = new FakeOllama();
        ollama.Replies.Enqueue(cutRequest == 0 ? FakeOllama.Reply("", 8192, ("disk", None)) : FakeOllama.Reply(""));
        ollama.Replies.Enqueue(FakeOllama.Reply("{}", 9000));
        var refused = await Assert.ThrowsAsync<PeerException>(() => Run(ollama, Fixtures.Parse()));
        Assert.Equal(DiagnoseExitCodes.NoAdvice, refused.ExitCode);
        Assert.Equal($"request {cutRequest + 1} did not provably fit half the context ({(cutRequest == 0 ? 8192 : 9000)} prompt tokens)", refused.Message);
    }

    [Fact]
    public async Task ShowsARejectedAnswerItsReasonOnce()
    {
        var ollama = new FakeOllama();
        ollama.Replies.Enqueue(FakeOllama.Calls(("network-link", None)));
        ollama.Replies.Enqueue(FakeOllama.Reply(""));
        ollama.Replies.Enqueue(FakeOllama.Answer("link-up", "Wi-Fi is up.", ("network-link", "/data/adapters/0/status", "down")));
        ollama.Replies.Enqueue(FakeOllama.Answer("link-up", "Wi-Fi is up.", ("network-link", "/data/adapters/0/status", "up")));
        var report = await Run(ollama, Fixtures.Parse(policy => policy["maxAnswerAttempts"] = 2));

        Assert.Equal(2, (int)report["receipt"]!["answerAttempts"]!);
        var retry = ollama.Chats.Last();
        Assert.Equal("assistant", (string?)Messages(retry)[5]!["role"]);
        Assert.Equal(
            "That answer was rejected: the citation \"network-link\" \"/data/adapters/0/status\" = \"down\" matches no result of that tool the model was given. "
                + "Answer again with JSON only; cite exact single values at pointers that exist in the tool results above.",
            Content(retry, 6));

        ollama.Replies.Enqueue(FakeOllama.Calls(("network-link", None)));
        ollama.Replies.Enqueue(FakeOllama.Reply(""));
        ollama.Replies.Enqueue(FakeOllama.Reply("not json"));
        ollama.Replies.Enqueue(FakeOllama.Reply("still not json"));
        var rejected = await Assert.ThrowsAsync<PeerException>(() => Run(ollama, Fixtures.Parse(policy => policy["maxAnswerAttempts"] = 2)));
        Assert.Equal(DiagnoseExitCodes.NoAdvice, rejected.ExitCode);
    }

    [Fact]
    public async Task RedactsTheAnswerBeforeItLeaves()
    {
        var ollama = new FakeOllama();
        ollama.Replies.Enqueue(FakeOllama.Calls(("network-link", None)));
        ollama.Replies.Enqueue(FakeOllama.Reply(""));
        ollama.Replies.Enqueue(FakeOllama.Answer("link-up", "BUILDHOST-01 is on Wi-Fi.", ("network-link", "/data/adapters/0/status", "up")));
        var redactor = HostRedactor.Load(null, "BUILDHOST-01", "builder");
        var report = await Run(ollama, Fixtures.Parse(), redact: redactor.Redact);
        Assert.Equal("<private> is on Wi-Fi.", (string?)report["answer"]!["finding"]);
    }

    [Fact]
    public void DescribesTheRegistryAsOllamaTools()
    {
        var tools = Diagnosis.ToolDefinitions();
        Assert.Equal(Registry.Tools.Select(tool => tool.Name), tools.Select(tool => (string?)tool!["function"]!["name"]));
        Assert.All(tools, tool => Assert.Equal("function", (string?)tool!["type"]));
        Assert.Equal(
            """{"type":"object","properties":{"hours":{"type":"integer","minimum":1,"maximum":72,"description":"How many hours back to count. Default 24."}},"required":[]}""",
            tools[^1]!["function"]!["parameters"]!.ToJsonString());
        Assert.Equal("""{"type":"object","properties":{},"required":[]}""", tools[0]!["function"]!["parameters"]!.ToJsonString());

        var format = Diagnosis.AnswerFormat(Fixtures.Parse(), Fixtures.Parse().Questions[0]);
        Assert.Equal("""["link-up","no-active-link","inconclusive"]""", format["properties"]!["class"]!["enum"]!.ToJsonString());
        Assert.Equal(2, (int)format["properties"]!["citations"]!["maxItems"]!);
        Assert.Equal(Registry.Tools.Select(tool => tool.Name), format["properties"]!["citations"]!["items"]!["properties"]!["tool"]!["enum"]!.AsArray().Select(name => (string?)name));
    }
}
