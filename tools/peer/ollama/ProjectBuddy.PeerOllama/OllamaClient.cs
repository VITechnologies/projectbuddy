using System.Net;
using System.Text;
using System.Text.Json.Nodes;

namespace ProjectBuddy.PeerOllama;

public sealed record ToolCall(string Name, JsonObject? Arguments);

public sealed record ChatResult(string Content, IReadOnlyList<ToolCall> ToolCalls, int PromptTokens, int OutputTokens, double Seconds, double TokensPerSecond);

public sealed record Residency(long SizeBytes, long SizeVramBytes);

// A model held in memory (/api/ps); a missing digest reads as empty and a missing counter as zero.
public sealed record LoadedModel(string Name, string Digest, long SizeBytes, long SizeVramBytes, long ContextLength);

// Talks only to an Ollama bound to a loopback IP literal (#210 D2); any server or transport fault is "unreachable".
public sealed class OllamaClient(HttpMessageHandler handler, Uri baseUri, TimeSpan timeout) : IDisposable
{
    private readonly HttpClient http = new(handler, disposeHandler: false) { BaseAddress = baseUri, Timeout = timeout };

    public static Uri RequireLoopback(string url)
    {
        if (!Uri.TryCreate(url, UriKind.Absolute, out var uri)
            || uri.Scheme != Uri.UriSchemeHttp
            || !IPAddress.TryParse(uri.Host.Trim('[', ']'), out var address)
            || !IPAddress.IsLoopback(address)
            || uri.AbsolutePath != "/"
            || uri.Query.Length > 0
            || uri.UserInfo.Length > 0)
        {
            throw new PeerException(ExitCodes.Refused, "--ollama must be http://<loopback IP>:<port> with no path");
        }

        return uri;
    }

    // No proxy and no redirects: either could carry a loopback request to another host (D2).
    public static SocketsHttpHandler LoopbackHandler() => new() { UseProxy = false, AllowAutoRedirect = false };

    // Temperature is always 0; the caller fixes the context, output budget and seed.
    public static JsonObject ChatRequest(string model, string system, string input, int numCtx, int numPredict, int seed, JsonObject? format = null, JsonArray? tools = null)
    {
        var request = new JsonObject
        {
            ["model"] = model,
            ["stream"] = false,
            ["keep_alive"] = "5m",
            ["options"] = new JsonObject
            {
                ["num_ctx"] = numCtx,
                ["num_predict"] = numPredict,
                ["temperature"] = 0,
                ["seed"] = seed,
            },
            ["messages"] = new JsonArray(
                new JsonObject { ["role"] = "system", ["content"] = system },
                new JsonObject { ["role"] = "user", ["content"] = input }),
        };
        if (format is not null)
        {
            request["format"] = format;
        }

        if (tools is not null)
        {
            request["tools"] = tools;
        }

        return request;
    }

    public async Task<string> VersionAsync() =>
        Json.String((await SendAsync(HttpMethod.Get, "api/version", null))["version"], "Ollama version", ExitCodes.Unreachable);

    public async Task<IReadOnlyDictionary<string, string>> DigestsAsync()
    {
        var digests = new Dictionary<string, string>(StringComparer.Ordinal);
        foreach (var node in Json.Array((await SendAsync(HttpMethod.Get, "api/tags", null))["models"], "Ollama tags", ExitCodes.Unreachable))
        {
            var model = Json.Object(node, "Ollama tag", ExitCodes.Unreachable);
            digests[Json.String(model["name"], "tag name", ExitCodes.Unreachable)] = Json.String(model["digest"], "tag digest", ExitCodes.Unreachable);
        }

        return digests;
    }

    public async Task<ChatResult> ChatAsync(JsonObject request)
    {
        var response = await SendAsync(HttpMethod.Post, "api/chat", request);
        var message = Json.Object(response["message"], "chat message", ExitCodes.Unreachable);
        var calls = new List<ToolCall>();
        if (message["tool_calls"] is JsonArray toolCalls)
        {
            foreach (var call in toolCalls)
            {
                var function = Json.Object(Json.Object(call, "tool call", ExitCodes.Unreachable)["function"], "tool function", ExitCodes.Unreachable);
                calls.Add(new ToolCall(Json.String(function["name"], "tool name", ExitCodes.Unreachable), function["arguments"] as JsonObject));
            }
        }

        var prompt = Count(response, "prompt_eval_count");
        var output = Count(response, "eval_count");
        var evalSeconds = Count(response, "eval_duration") / 1e9;
        return new ChatResult(
            Json.TryString(message["content"]) ?? "",
            calls,
            (int)prompt,
            (int)output,
            Count(response, "total_duration") / 1e9,
            evalSeconds > 0 ? output / evalSeconds : 0);
    }

    public async Task<Residency?> ResidencyAsync(string model)
    {
        foreach (var node in Json.Array((await SendAsync(HttpMethod.Get, "api/ps", null))["models"], "Ollama ps", ExitCodes.Unreachable))
        {
            var entry = Json.Object(node, "Ollama ps entry", ExitCodes.Unreachable);
            if (Json.TryString(entry["name"]) == model)
            {
                return new Residency(Count(entry, "size"), Count(entry, "size_vram"));
            }
        }

        return null;
    }

    public async Task<IReadOnlyList<LoadedModel>> LoadedAsync()
    {
        var loaded = new List<LoadedModel>();
        foreach (var node in Json.Array((await SendAsync(HttpMethod.Get, "api/ps", null))["models"], "Ollama ps", ExitCodes.Unreachable))
        {
            var entry = Json.Object(node, "Ollama ps entry", ExitCodes.Unreachable);
            loaded.Add(new LoadedModel(Json.String(entry["name"], "ps name", ExitCodes.Unreachable), Json.TryString(entry["digest"]) ?? "",
                Count(entry, "size"), Count(entry, "size_vram"), Count(entry, "context_length")));
        }

        return loaded;
    }

    public Task UnloadAsync(string model) =>
        SendAsync(HttpMethod.Post, "api/generate", new JsonObject { ["model"] = model, ["keep_alive"] = 0 });

    public void Dispose() => http.Dispose();

    // A missing counter reads as zero, which the truncation guard rejects.
    private static long Count(JsonObject response, string key) =>
        response[key] is JsonValue value && value.TryGetValue<long>(out var count) && count >= 0 ? count : 0;

    private async Task<JsonObject> SendAsync(HttpMethod method, string path, JsonObject? body)
    {
        try
        {
            using var request = new HttpRequestMessage(method, path);
            if (body is not null)
            {
                request.Content = new StringContent(body.ToJsonString(), Encoding.UTF8, "application/json");
            }

            using var response = await http.SendAsync(request);
            if (!response.IsSuccessStatusCode)
            {
                throw new PeerException(ExitCodes.Unreachable, $"Ollama {path} returned HTTP {(int)response.StatusCode}");
            }

            return Json.Object(Json.Parse(await response.Content.ReadAsByteArrayAsync(), $"Ollama {path} response", ExitCodes.Unreachable), $"Ollama {path} response", ExitCodes.Unreachable);
        }
        catch (Exception error) when (error is HttpRequestException or TaskCanceledException)
        {
            throw new PeerException(ExitCodes.Unreachable, $"Ollama {path} is unreachable");
        }
    }
}
