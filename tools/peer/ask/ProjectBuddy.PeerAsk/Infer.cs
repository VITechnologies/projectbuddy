using System.Collections.Concurrent;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using ProjectBuddy.PeerOllama;

namespace ProjectBuddy.PeerAsk;

public sealed record InferItem(string Id, string Prompt, bool Json);

// Only the options a caller may set; each one reaches Ollama only when the request gives it. Think is Ollama's top-level
// "think" switch, not a model option.
public sealed record InferOptions(double? Temperature, long? Seed, long? NumCtx, bool? Think = null);

public sealed record InferRequest(string Model, InferOptions Options, IReadOnlyList<InferItem> Items);

// An allowlisted model: its digest pin, the loopback Ollama ports that serve it (each checked against the pin), and how
// long each of its Ollama calls may take.
public sealed record InferModel(ModelPin Pin, IReadOnlyList<int> Ports, int ItemTimeoutSeconds = Infer.DefaultItemTimeoutSeconds);

// Second is how long one allowlist second lasts: a real second, except in tests. Total is the whole-call limit.
public sealed record InferLimits(TimeSpan Second, TimeSpan Total)
{
    public static readonly InferLimits Default = new(TimeSpan.FromSeconds(1), Infer.Timeout);
}

internal sealed record InferAnswer(string Id, string Text, int EvalCount, int PromptEvalCount, double Seconds, int Port);

// "infer" (#358): the workstation sends a batch of prompts on stdin and the peer runs them on its local Ollama. The peer
// returns only the model's output for those prompts, from an allowlisted model whose installed digest matches its pin.
public static partial class Infer
{
    public const string Verb = "infer";
    public const string RequestSchema = "projectbuddy/peer-infer-request@1";
    public const string AllowlistSchema = "projectbuddy/peer-infer-allowlist@3";

    // Older allowlists stay valid: @2 entries have no item timeout, and @1 entries have no ports either.
    public const string AllowlistSchemaV2 = "projectbuddy/peer-infer-allowlist@2";
    public const string AllowlistSchemaV1 = "projectbuddy/peer-infer-allowlist@1";

    // The peer's two loopback Ollama servers: the NVIDIA GPU, and the Intel Arc iGPU through Vulkan. Fixed, not configurable.
    public const string OllamaHost = "127.0.0.1";
    public const int DefaultPort = 11434;
    public static readonly IReadOnlyList<int> AllowedPorts = [11434, 11435];

    public const int MaxRequestBytes = 1024 * 1024;
    public const int MaxOutputBytes = 1024 * 1024;
    public const int MaxItems = 64;
    public const int MaxPromptBytes = 64 * 1024;
    public const int MaxModelChars = 200;
    public const int MaxAllowlistBytes = 64 * 1024;
    public const int MaxAllowlistModels = 64;
    public const double MaxTemperature = 2;
    public const int MinNumCtx = 512;
    public const int MaxNumCtx = 32768;

    // Fixed on the peer, never set by the caller: at most 4096 generated tokens per item, thinking included, and an answer
    // of at most 16 KiB.
    public const int MaxPredictTokens = 4096;
    public const int MaxAnswerBytes = 16 * 1024;

    // Each Ollama call of a batch (the digest check and every item) has its model's itemTimeoutSeconds. The whole call,
    // from reading the request to the last answer, has a fixed limit, which the client's wait is derived from.
    public const int DefaultItemTimeoutSeconds = 120;
    public const int MinItemTimeoutSeconds = 30;
    public const int MaxItemTimeoutSeconds = 900;
    public static readonly TimeSpan Timeout = TimeSpan.FromSeconds(1800);

    private static readonly JsonDocumentOptions Strict = new() { AllowDuplicateProperties = false, MaxDepth = 8 };
    private static readonly UTF8Encoding StrictUtf8 = new(encoderShouldEmitUTF8Identifier: false, throwOnInvalidBytes: true);

    // Shared by the client, which checks its own request file, and the peer, which never trusts the client's check.
    public static InferRequest ParseRequest(byte[] utf8)
    {
        if (utf8.Length > MaxRequestBytes)
        {
            throw new RefusedException($"the request is over {MaxRequestBytes} bytes");
        }

        var root = ParseObject(utf8, "the request");
        Keys(root, "the request", ["schema", "model", "items"], ["options"]);
        if (Text(root["schema"]) != RequestSchema)
        {
            throw new RefusedException($"schema must be {RequestSchema}");
        }

        var model = Text(root["model"]);
        if (model is not { Length: > 0 and <= MaxModelChars })
        {
            throw new RefusedException($"model must be a string of 1 to {MaxModelChars} characters");
        }

        var options = root.ContainsKey("options") ? Options(root["options"]) : new InferOptions(null, null, null, null);
        if (root["items"] is not JsonArray items || items.Count is < 1 or > MaxItems)
        {
            throw new RefusedException($"items must be an array of 1 to {MaxItems} items");
        }

        var ids = new HashSet<string>(StringComparer.Ordinal);
        var parsed = new List<InferItem>();
        foreach (var node in items)
        {
            if (node is not JsonObject item)
            {
                throw new RefusedException("each item must be an object");
            }

            Keys(item, "an item", ["id", "prompt"], ["format"]);
            var id = Text(item["id"]);
            if (id is null || !IdPattern().IsMatch(id))
            {
                throw new RefusedException("an item id must be 1 to 64 of [A-Za-z0-9._:-]");
            }

            if (!ids.Add(id))
            {
                throw new RefusedException($"item id {id} is not unique");
            }

            var prompt = Text(item["prompt"]);
            if (prompt is not { Length: > 0 } || Encoding.UTF8.GetByteCount(prompt) > MaxPromptBytes)
            {
                throw new RefusedException($"the prompt of {id} must be 1 to {MaxPromptBytes} UTF-8 bytes");
            }

            // An absent format and a JSON null both mean free text.
            var json = item["format"] is { } format && (Text(format) == "json" ? true : throw new RefusedException($"the format of {id} must be \"json\" or null"));
            parsed.Add(new InferItem(id, prompt, json));
        }

        return new InferRequest(model, options, parsed);
    }

    // The allowlist deployment writes on the peer: {"schema", "models": [{"name", "digest", "ports", "itemTimeoutSeconds"}]}.
    // @3 entries may set both optional keys, @2 entries only ports, and @1 entries neither. Anything unusable is a refusal.
    public static IReadOnlyDictionary<string, InferModel> ParseAllowlist(byte[]? utf8)
    {
        if (utf8 is null)
        {
            throw new RefusedException($"the allowlist is over {MaxAllowlistBytes} bytes");
        }

        var root = ParseObject(utf8, "the allowlist");
        Keys(root, "the allowlist", ["schema", "models"], []);
        string[] optional = Text(root["schema"]) switch
        {
            AllowlistSchema => ["ports", "itemTimeoutSeconds"],
            AllowlistSchemaV2 => ["ports"],
            AllowlistSchemaV1 => [],
            _ => throw new RefusedException($"the allowlist schema must be {AllowlistSchema}, {AllowlistSchemaV2} or {AllowlistSchemaV1}"),
        };
        if (root["models"] is not JsonArray models || models.Count is < 1 or > MaxAllowlistModels)
        {
            throw new RefusedException($"the allowlist must have 1 to {MaxAllowlistModels} models");
        }

        var allowed = new Dictionary<string, InferModel>(StringComparer.Ordinal);
        foreach (var node in models)
        {
            if (node is not JsonObject entry)
            {
                throw new RefusedException("each allowlisted model must be an object");
            }

            Keys(entry, "an allowlisted model", ["name", "digest"], optional);
            ModelPin pin;
            try
            {
                pin = ModelPin.Create(Text(entry["name"]) ?? "", Text(entry["digest"]) ?? "");
            }
            catch (PeerException error)
            {
                throw new RefusedException(error.Message);
            }

            var ports = entry.ContainsKey("ports") ? Ports(entry["ports"]) : [DefaultPort];
            var itemTimeout = entry.ContainsKey("itemTimeoutSeconds")
                ? (int)Integer(entry["itemTimeoutSeconds"], "itemTimeoutSeconds", MinItemTimeoutSeconds, MaxItemTimeoutSeconds)
                : DefaultItemTimeoutSeconds;
            if (!allowed.TryAdd(pin.Name, new InferModel(pin, ports, itemTimeout)))
            {
                throw new RefusedException($"{pin.Name} is allowlisted twice");
            }
        }

        return allowed;
    }

    // The peer side. The whole-call timeout covers reading the request, the allowlist and digest checks, and every
    // prompt; each Ollama call has its model's item timeout. Any failure fails the whole call, so no partial answer is ever
    // sent.
    public static byte[] Render(Stream stdin, HttpMessageHandler handler, IHostFacts facts, InferLimits limits)
    {
        // One client per allowed port, with the longest item timeout as a backstop; the model's own timeout is applied per
        // call. Disposing the clients on the way out cancels any prompt still running after a failure or the timeout.
        var clients = AllowedPorts.ToDictionary(port => port,
            port => new OllamaClient(handler, OllamaClient.RequireLoopback($"http://{OllamaHost}:{port}/"), limits.Second * MaxItemTimeoutSeconds));
        using var stop = new CancellationTokenSource();
        try
        {
            var work = Task.Run(() => RunAsync(stdin, clients, facts, limits.Second, stop.Token));
            var finished = Task.WhenAny(work, Task.Delay(limits.Total, stop.Token)).GetAwaiter().GetResult();
            stop.Cancel();
            return finished == work ? work.GetAwaiter().GetResult() : throw new TimeoutException("infer exceeded its whole-call timeout");
        }
        finally
        {
            foreach (var client in clients.Values)
            {
                client.Dispose();
            }
        }
    }

    internal static async Task<byte[]> RunAsync(Stream stdin, IReadOnlyDictionary<int, OllamaClient> clients, IHostFacts facts, TimeSpan second, CancellationToken stop)
    {
        var request = ParseRequest(await BoundedFile.ReadAsync(stdin, MaxRequestBytes, stop) ?? throw new RefusedException("the request is over 1 MiB"));
        var allowlist = ParseAllowlist(ReadAllowlist(facts.InferAllowlistPath));
        var model = allowlist.GetValueOrDefault(request.Model) ?? throw new RefusedException($"{request.Model} is not allowlisted");
        var itemTimeout = second * model.ItemTimeoutSeconds;
        try
        {
            // Never pulls: every listed port's server must already have the model with exactly the pinned digest.
            foreach (var port in model.Ports)
            {
                await ModelPin.RequireInstalledAsync(clients[port], [model.Pin]).WaitAsync(itemTimeout);
            }
        }
        catch (PeerException error) when (error.ExitCode == PeerOllama.ExitCodes.Refused)
        {
            throw new RefusedException(error.Message);
        }

        // One shared queue; each port runs as many workers as its server has slots, and a free worker on either port takes
        // the next item, so a faster server answers more of the batch and neither sits idle while items remain.
        var queue = new ConcurrentQueue<int>(Enumerable.Range(0, request.Items.Count));
        var answers = new InferAnswer[request.Items.Count];
        using var abandon = CancellationTokenSource.CreateLinkedTokenSource(stop);
        var pending = model.Ports
            .SelectMany(port => Enumerable.Range(0, Math.Clamp(facts.InferWorkers(port), 1, MaxItems))
                .Select(_ => WorkAsync(port, clients[port], itemTimeout, request, queue, answers, abandon.Token)))
            .ToList();
        while (pending.Count > 0)
        {
            var done = await Task.WhenAny(pending);
            pending.Remove(done);
            if (!done.IsCompletedSuccessfully)
            {
                // Fail fast: no worker takes another item after the first failure, which the await rethrows.
                abandon.Cancel();
            }

            await done;
        }

        return Serve.Envelope(Verb, writer =>
        {
            writer.WriteString("model", request.Model);
            writer.WriteString("digest", model.Pin.Digest);
            writer.WriteStartArray("items");
            foreach (var answer in answers)
            {
                writer.WriteStartObject();
                writer.WriteString("id", answer.Id);
                writer.WriteString("answer", answer.Text);
                writer.WriteNumber("evalCount", answer.EvalCount);
                writer.WriteNumber("promptEvalCount", answer.PromptEvalCount);
                writer.WriteNumber("seconds", Math.Round(answer.Seconds, 3));
                writer.WriteNumber("port", answer.Port);
                writer.WriteEndObject();
            }

            writer.WriteEndArray();
        });
    }

    // One user message and nothing else: no system prompt, no tools, no images, so the model sees only the prompt. The
    // generation cap is always set.
    internal static JsonObject ChatRequest(InferRequest request, InferItem item)
    {
        var options = new JsonObject();
        var chat = new JsonObject
        {
            ["model"] = request.Model,
            ["stream"] = false,
            ["messages"] = new JsonArray(new JsonObject { ["role"] = "user", ["content"] = item.Prompt }),
            ["options"] = options,
        };
        if (request.Options.Temperature is { } temperature)
        {
            options["temperature"] = temperature;
        }

        if (request.Options.Seed is { } seed)
        {
            options["seed"] = seed;
        }

        if (request.Options.NumCtx is { } numCtx)
        {
            options["num_ctx"] = numCtx;
        }

        options["num_predict"] = MaxPredictTokens;
        if (request.Options.Think is { } think)
        {
            chat["think"] = think;
        }

        if (item.Json)
        {
            chat["format"] = "json";
        }

        return chat;
    }

    // One worker on one port: takes items off the shared queue until it is empty or the batch is abandoned. An item over its
    // timeout fails the batch, and the call it leaves running is cancelled when the clients are disposed.
    internal static async Task WorkAsync(int port, OllamaClient ollama, TimeSpan itemTimeout, InferRequest request, ConcurrentQueue<int> queue, InferAnswer[] answers, CancellationToken abandon)
    {
        while (!abandon.IsCancellationRequested && queue.TryDequeue(out var index))
        {
            var item = request.Items[index];

            // Only the final message content is kept: Ollama returns thinking text in a separate field, which is dropped.
            var result = await ollama.ChatAsync(ChatRequest(request, item)).WaitAsync(itemTimeout);
            if (Encoding.UTF8.GetByteCount(result.Content) > MaxAnswerBytes)
            {
                throw new AnswerLimitException();
            }

            answers[index] = new InferAnswer(item.Id, result.Content, result.OutputTokens, result.PromptTokens, result.Seconds, port);
        }
    }

    private static byte[]? ReadAllowlist(string path)
    {
        try
        {
            return BoundedFile.Read(path, MaxAllowlistBytes);
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException)
        {
            throw new RefusedException("the allowlist cannot be read");
        }
    }

    private static List<int> Ports(JsonNode? node)
    {
        if (node is not JsonArray array || array.Count == 0)
        {
            throw new RefusedException("ports must be a non-empty array");
        }

        var ports = new List<int>();
        foreach (var item in array)
        {
            if (!(item is JsonValue value && value.TryGetValue<int>(out var port) && AllowedPorts.Contains(port) && !ports.Contains(port)))
            {
                throw new RefusedException($"ports must be distinct values from [{string.Join(", ", AllowedPorts)}]");
            }

            ports.Add(port);
        }

        return ports;
    }

    private static InferOptions Options(JsonNode? node)
    {
        if (node is not JsonObject options)
        {
            throw new RefusedException("options must be an object");
        }

        Keys(options, "options", [], ["temperature", "seed", "num_ctx", "think"]);
        return new InferOptions(
            options.ContainsKey("temperature") ? Number(options["temperature"], "options.temperature", 0, MaxTemperature) : null,
            options.ContainsKey("seed") ? Integer(options["seed"], "options.seed", 0, int.MaxValue) : null,
            options.ContainsKey("num_ctx") ? Integer(options["num_ctx"], "options.num_ctx", MinNumCtx, MaxNumCtx) : null,
            options.ContainsKey("think") ? Boolean(options["think"], "options.think") : null);
    }

    private static bool Boolean(JsonNode? node, string what) =>
        node is JsonValue value && value.TryGetValue<bool>(out var flag) ? flag : throw new RefusedException($"{what} must be true or false");

    private static JsonObject ParseObject(byte[] utf8, string what)
    {
        try
        {
            StrictUtf8.GetCharCount(utf8);
            return JsonNode.Parse(utf8, documentOptions: Strict) as JsonObject ?? throw new RefusedException($"{what} must be a JSON object");
        }
        catch (Exception error) when (error is JsonException or DecoderFallbackException)
        {
            throw new RefusedException($"{what} is not UTF-8 JSON");
        }
    }

    private static void Keys(JsonObject value, string what, string[] required, string[] optional)
    {
        if (value.Any(pair => !required.Contains(pair.Key) && !optional.Contains(pair.Key)) || !required.All(value.ContainsKey))
        {
            throw new RefusedException($"{what} needs [{string.Join(", ", required)}] and may have [{string.Join(", ", optional)}], nothing else");
        }
    }

    // A JSON string, or null for anything else. A string with an escaped lone surrogate cannot be read, so it is not text.
    private static string? Text(JsonNode? node)
    {
        try
        {
            return node is JsonValue value && value.GetValueKind() == JsonValueKind.String ? value.GetValue<string>() : null;
        }
        catch (InvalidOperationException)
        {
            return null;
        }
    }

    private static double Number(JsonNode? node, string what, double min, double max) =>
        node is JsonValue value && value.TryGetValue<double>(out var number) && number >= min && number <= max
            ? number
            : throw new RefusedException($"{what} must be a number from {min} to {max}");

    private static long Integer(JsonNode? node, string what, long min, long max) =>
        node is JsonValue value && value.TryGetValue<long>(out var number) && number >= min && number <= max
            ? number
            : throw new RefusedException($"{what} must be an integer from {min} to {max}");

    // \z, unlike $, admits no trailing newline.
    [GeneratedRegex("^[A-Za-z0-9._:-]{1,64}\\z")]
    private static partial Regex IdPattern();
}

// A refusal: the peer answers exit 64 with its fixed message and nothing else; the client shows the reason for its own
// request file.
public sealed class RefusedException(string reason) : Exception(reason);

// One answer over Infer.MaxAnswerBytes: the peer fails the call with its output-limit exit code and fixed message.
public sealed class AnswerLimitException : Exception;
