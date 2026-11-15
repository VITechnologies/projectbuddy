using System.Net;
using System.Text;
using System.Text.Json.Nodes;

namespace ProjectBuddy.PeerBench.Tests;

// A scripted Ollama: routes by path, records every request, and never touches the network.
internal sealed class FakeOllama : HttpMessageHandler
{
    public const string Model = "qwen2.5:7b";
    public const string Digest = "845dbda0ea48ed749caafd9e6037047aa19acfcfd82e704d7ca97d631a0b697e";

    public List<(string Path, JsonObject? Body)> Requests { get; } = [];

    public Func<string, JsonObject?, HttpResponseMessage> Respond { get; set; }

    // Chat replies keyed by the user message; the probe (empty system prompt) gets ProbeTokens.
    public Dictionary<string, JsonObject> Chats { get; } = new(StringComparer.Ordinal);

    public int ProbeTokens { get; set; } = 4098;

    public JsonObject? Ps { get; set; } = new() { ["models"] = new JsonArray(new JsonObject { ["name"] = Model, ["size"] = 100, ["size_vram"] = 100 }) };

    public FakeOllama() => Respond = Default;

    public static HttpResponseMessage Ok(JsonNode body) => new(HttpStatusCode.OK) { Content = new StringContent(body.ToJsonString(), Encoding.UTF8, "application/json") };

    public static JsonObject Reply(string content, int prompt = 100, int output = 20, JsonArray? toolCalls = null)
    {
        var message = new JsonObject { ["role"] = "assistant", ["content"] = content };
        if (toolCalls is not null)
        {
            message["tool_calls"] = toolCalls;
        }

        return new JsonObject
        {
            ["message"] = message,
            ["prompt_eval_count"] = prompt,
            ["eval_count"] = output,
            ["total_duration"] = 2_000_000_000L,
            ["eval_duration"] = 1_000_000_000L,
        };
    }

    public static JsonArray Call(string name, string? verb) =>
        new(new JsonObject { ["function"] = new JsonObject { ["name"] = name, ["arguments"] = verb is null ? null : new JsonObject { ["verb"] = verb } } });

    protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken)
    {
        var body = request.Content is null ? null : JsonNode.Parse(await request.Content.ReadAsStringAsync(cancellationToken)) as JsonObject;
        var path = request.RequestUri!.AbsolutePath;
        Requests.Add((path, body));
        return Respond(path, body);
    }

    private HttpResponseMessage Default(string path, JsonObject? body) => path switch
    {
        "/api/version" => Ok(new JsonObject { ["version"] = "0.32.3" }),
        "/api/tags" => Ok(new JsonObject { ["models"] = new JsonArray(new JsonObject { ["name"] = Model, ["digest"] = Digest }) }),
        "/api/ps" => Ok(Ps!),
        "/api/generate" => Ok(new JsonObject { ["done"] = true }),
        _ => Ok(ChatReply(body!)),
    };

    private JsonObject ChatReply(JsonObject body)
    {
        var messages = body["messages"]!.AsArray();
        if (messages[0]!["content"]!.GetValue<string>().Length == 0)
        {
            return Reply("", ProbeTokens, 1);
        }

        return Chats.TryGetValue(messages[1]!["content"]!.GetValue<string>(), out var reply) ? reply : Reply("{}");
    }
}

internal static class Fixtures
{
    public const string Candidates = """
        {"schema":"projectbuddy/peer-bench-candidates@1","models":[{"name":"qwen2.5:7b","digest":"845dbda0ea48ed749caafd9e6037047aa19acfcfd82e704d7ca97d631a0b697e"}]}
        """;

    public static JsonObject Corpus() => new()
    {
        ["schema"] = "projectbuddy/peer-bench-corpus@1",
        ["numCtx"] = 8192,
        ["numPredict"] = 256,
        ["seed"] = 42,
        ["prompts"] = new JsonObject { ["triage"] = "triage it", ["security"] = "review it", ["tool"] = "dispatch it" },
        ["triageClasses"] = new JsonArray("gate-failure", "segfault"),
        ["securityClasses"] = new JsonArray("secret-in-log", "command-injection"),
        ["verbs"] = new JsonArray("vbox", "disk"),
        ["items"] = new JsonArray(
            Item("t1", "triage", "step one\nFAIL gate-x -- broken thing\nexit 1", new JsonObject { ["step"] = "gate-x", ["errorLine"] = "broken thing", ["class"] = "gate-failure" }),
            Item("s1", "security", "echo $TOKEN", new JsonObject { ["classes"] = new JsonArray("secret-in-log") }),
            Item("s2", "security", "safe code", new JsonObject { ["classes"] = new JsonArray() }),
            Item("v1", "tool", "is the vm running?", new JsonObject { ["verb"] = "vbox" })),
    };

    public static JsonObject Item(string id, string kind, string input, JsonObject expect) => new()
    {
        ["id"] = id,
        ["kind"] = kind,
        ["source"] = "synthetic",
        ["input"] = input,
        ["expect"] = expect,
    };

    public static byte[] Bytes(JsonNode node) => Encoding.UTF8.GetBytes(node.ToJsonString());

    public static Corpus Parse(JsonObject corpus) => ProjectBuddy.PeerBench.Corpus.Parse(Bytes(corpus));

    public static string RepositoryFile(string name)
    {
        for (var directory = new DirectoryInfo(AppContext.BaseDirectory); directory is not null; directory = directory.Parent)
        {
            var candidate = Path.Combine(directory.FullName, name);
            if (File.Exists(candidate) && File.Exists(Path.Combine(directory.FullName, "global.json")))
            {
                return candidate;
            }
        }

        throw new FileNotFoundException(name);
    }
}
