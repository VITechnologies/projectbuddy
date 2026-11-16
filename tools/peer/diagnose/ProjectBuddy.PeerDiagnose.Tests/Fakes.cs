using System.Net;
using System.Text;
using System.Text.Json.Nodes;
using ProjectBuddy.PeerOllama;
using ProjectBuddy.PeerProbes;

namespace ProjectBuddy.PeerDiagnose.Tests;

// A scripted Ollama: the truncation probe (empty system prompt) gets ProbeTokens; every other chat takes Replies in order.
internal sealed class FakeOllama : HttpMessageHandler
{
    public const string Model = "qwen2.5:7b";
    public const string Digest = "845dbda0ea48ed749caafd9e6037047aa19acfcfd82e704d7ca97d631a0b697e";

    public List<(string Path, JsonObject? Body)> Requests { get; } = [];

    public Queue<JsonObject> Replies { get; } = new();

    public int ProbeTokens { get; set; } = 8194;

    public IEnumerable<JsonObject> Chats =>
        Requests.Where(request => request.Path == "/api/chat" && request.Body!["messages"]![0]!["content"]!.GetValue<string>().Length > 0).Select(request => request.Body!);

    public static HttpResponseMessage Ok(JsonNode body) => new(HttpStatusCode.OK) { Content = new StringContent(body.ToJsonString(), Encoding.UTF8, "application/json") };

    public static JsonObject Reply(string content, int prompt = 500, params (string Name, JsonObject? Arguments)[] calls)
    {
        var message = new JsonObject { ["role"] = "assistant", ["content"] = content };
        if (calls.Length > 0)
        {
            message["tool_calls"] = new JsonArray([.. calls.Select(call =>
            {
                var function = new JsonObject { ["name"] = call.Name };
                if (call.Arguments is not null)
                {
                    function["arguments"] = call.Arguments.DeepClone();
                }

                return (JsonNode)new JsonObject { ["function"] = function };
            })]);
        }

        return new JsonObject
        {
            ["message"] = message,
            ["prompt_eval_count"] = prompt,
            ["eval_count"] = 40,
            ["total_duration"] = 2_000_000_000L,
            ["eval_duration"] = 1_000_000_000L,
        };
    }

    public static JsonObject Calls(params (string Name, JsonObject? Arguments)[] calls) => Reply("", 500, calls);

    public static JsonObject Answer(string answerClass, string finding, params (string Tool, string Pointer, JsonNode? Value)[] citations) =>
        Reply(new JsonObject
        {
            ["class"] = answerClass,
            ["finding"] = finding,
            ["citations"] = new JsonArray([.. citations.Select(citation => (JsonNode)new JsonObject
            {
                ["tool"] = citation.Tool,
                ["pointer"] = citation.Pointer,
                ["value"] = citation.Value,
            })]),
        }.ToJsonString());

    protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken)
    {
        var body = request.Content is null ? null : JsonNode.Parse(await request.Content.ReadAsStringAsync(cancellationToken)) as JsonObject;
        var path = request.RequestUri!.AbsolutePath;
        Requests.Add((path, body));
        return path switch
        {
            "/api/version" => Ok(new JsonObject { ["version"] = "0.40.16" }),
            "/api/tags" => Ok(new JsonObject { ["models"] = new JsonArray(new JsonObject { ["name"] = Model, ["digest"] = Digest }) }),
            _ => Ok(body!["messages"]![0]!["content"]!.GetValue<string>().Length == 0 ? Reply("", ProbeTokens) : Replies.Dequeue()),
        };
    }
}

// A fixed machine: one live Wi-Fi link, one drive, no VirtualBox, no Windows services; counts how often each is read.
internal sealed class FakeSources : ISources
{
    public bool IsWindows => false;

    public List<Adapter> AdapterList { get; } = [new("Wi-Fi", "wifi", "up", 866, true, true, ["192.168.1.1"])];

    public int AdapterReads { get; private set; }

    public int DriveReads { get; private set; }

    public IReadOnlyList<Adapter> Adapters()
    {
        AdapterReads++;
        return AdapterList;
    }

    public IReadOnlyList<Drive> Drives()
    {
        DriveReads++;
        return [new Drive("C:\\", "NTFS", 1000, 50)];
    }

    public Task<OllamaState?> OllamaAsync() => Task.FromResult<OllamaState?>(null);

    public CommandResult? Run(string program, IReadOnlyList<string> arguments) => null;
}

internal static class Fixtures
{
    public const string Rules = """
        [
          {"class":"no-active-link","all":[{"tool":"network-link","pointer":"/data/adapters/*","quantifier":"none","test":[{"pointer":"/status","op":"eq","value":"up"}]}]},
          {"class":"link-up","all":[{"tool":"network-link","pointer":"/data/adapters/*","quantifier":"some","test":[{"pointer":"/status","op":"eq","value":"up"},{"pointer":"/gateways/0","op":"present"}]}]}
        ]
        """;

    public static JsonObject Policy(Action<JsonObject>? change = null)
    {
        var policy = new JsonObject
        {
            ["schema"] = "projectbuddy/diagnose-policy@2",
            ["model"] = new JsonObject { ["name"] = FakeOllama.Model, ["digest"] = FakeOllama.Digest },
            ["numCtx"] = 16384,
            ["numPredict"] = 384,
            ["seed"] = 42,
            ["maxRounds"] = 4,
            ["maxCallsPerRound"] = 2,
            ["maxCalls"] = 6,
            ["maxEvidenceBytes"] = 12000,
            ["maxFindingChars"] = 60,
            ["maxCitations"] = 2,
            ["maxValueChars"] = 16,
            ["maxAnswerAttempts"] = 1,
            ["system"] = "diagnose",
            ["answerPrompt"] = "answer now",
            ["questions"] = new JsonArray(new JsonObject
            {
                ["id"] = "github-unreachable",
                ["text"] = "Is the link the cause?",
                ["classes"] = new JsonArray("link-up", "no-active-link", "inconclusive"),
                ["rules"] = JsonNode.Parse(Rules)!.AsArray(),
            }),
        };
        change?.Invoke(policy);
        return policy;
    }

    public static Policy Parse(Action<JsonObject>? change = null) => PeerDiagnose.Policy.Parse(Encoding.UTF8.GetBytes(Policy(change).ToJsonString()));

    public static Registry Registry(ISources sources) => new(sources, text => new ProjectBuddy.PeerRedact.Redaction(text, new Dictionary<string, int>()), new ProbeOptions());
}
