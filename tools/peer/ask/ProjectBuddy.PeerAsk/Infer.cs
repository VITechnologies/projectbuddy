using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using ProjectBuddy.PeerOllama;

namespace ProjectBuddy.PeerAsk;

public sealed record InferItem(string Id, string Prompt, bool Json);

// Only the options a caller may set; each one reaches Ollama only when the request gives it.
public sealed record InferOptions(double? Temperature, long? Seed, long? NumCtx);

public sealed record InferRequest(string Model, InferOptions Options, IReadOnlyList<InferItem> Items);

public sealed record InferLimits(TimeSpan Item, TimeSpan Total)
{
    public static readonly InferLimits Default = new(Infer.ItemTimeout, Infer.Timeout);
}

internal sealed record InferAnswer(string Id, string Text, int EvalCount, int PromptEvalCount, double Seconds);

// "infer" (#358): the workstation sends a batch of prompts on stdin and the peer runs them on its local Ollama. The peer
// returns only the model's output for those prompts, from an allowlisted model whose installed digest matches its pin.
public static partial class Infer
{
    public const string Verb = "infer";
    public const string RequestSchema = "projectbuddy/peer-infer-request@1";
    public const string AllowlistSchema = "projectbuddy/peer-infer-allowlist@1";
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
    public static readonly TimeSpan ItemTimeout = TimeSpan.FromSeconds(120);
    public static readonly TimeSpan Timeout = TimeSpan.FromSeconds(600);

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

        var options = root.ContainsKey("options") ? Options(root["options"]) : new InferOptions(null, null, null);
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

    // The allowlist deployment writes on the peer: {"schema", "models": [{"name", "digest"}]}. Anything unusable is a refusal.
    public static IReadOnlyDictionary<string, ModelPin> ParseAllowlist(byte[]? utf8)
    {
        if (utf8 is null)
        {
            throw new RefusedException($"the allowlist is over {MaxAllowlistBytes} bytes");
        }

        var root = ParseObject(utf8, "the allowlist");
        Keys(root, "the allowlist", ["schema", "models"], []);
        if (Text(root["schema"]) != AllowlistSchema || root["models"] is not JsonArray models || models.Count is < 1 or > MaxAllowlistModels)
        {
            throw new RefusedException($"the allowlist must be {AllowlistSchema} with 1 to {MaxAllowlistModels} models");
        }

        var pins = new Dictionary<string, ModelPin>(StringComparer.Ordinal);
        foreach (var node in models)
        {
            if (node is not JsonObject entry)
            {
                throw new RefusedException("each allowlisted model must be an object");
            }

            Keys(entry, "an allowlisted model", ["name", "digest"], []);
            ModelPin pin;
            try
            {
                pin = ModelPin.Create(Text(entry["name"]) ?? "", Text(entry["digest"]) ?? "");
            }
            catch (PeerException error)
            {
                throw new RefusedException(error.Message);
            }

            if (!pins.TryAdd(pin.Name, pin))
            {
                throw new RefusedException($"{pin.Name} is allowlisted twice");
            }
        }

        return pins;
    }

    // The peer side. The whole-call timeout covers reading the request, the allowlist and digest checks, and every
    // prompt; Ollama has a per-prompt timeout. Any failure fails the whole call, so no partial answer is ever sent.
    public static byte[] Render(Stream stdin, HttpMessageHandler handler, IHostFacts facts, InferLimits limits)
    {
        // Disposing the client on the way out cancels any prompt still running after a failure or the timeout.
        using var ollama = new OllamaClient(handler, OllamaClient.RequireLoopback(facts.OllamaUrl), limits.Item);
        using var stop = new CancellationTokenSource();
        var work = Task.Run(() => RunAsync(stdin, ollama, facts, stop.Token));
        var finished = Task.WhenAny(work, Task.Delay(limits.Total, stop.Token)).GetAwaiter().GetResult();
        stop.Cancel();
        return finished == work ? work.GetAwaiter().GetResult() : throw new TimeoutException("infer exceeded its whole-call timeout");
    }

    internal static async Task<byte[]> RunAsync(Stream stdin, OllamaClient ollama, IHostFacts facts, CancellationToken stop)
    {
        var request = ParseRequest(await BoundedFile.ReadAsync(stdin, MaxRequestBytes, stop) ?? throw new RefusedException("the request is over 1 MiB"));
        var allowlist = ParseAllowlist(ReadAllowlist(facts.InferAllowlistPath));
        var pin = allowlist.GetValueOrDefault(request.Model) ?? throw new RefusedException($"{request.Model} is not allowlisted");
        try
        {
            // Never pulls: the model must already be installed with exactly the pinned digest.
            await ModelPin.RequireInstalledAsync(ollama, [pin]);
        }
        catch (PeerException error) when (error.ExitCode == PeerOllama.ExitCodes.Refused)
        {
            throw new RefusedException(error.Message);
        }

        var gate = new SemaphoreSlim(Math.Clamp(facts.InferConcurrency, 1, MaxItems));
        using var abandon = CancellationTokenSource.CreateLinkedTokenSource(stop);
        var answers = request.Items.Select(item => AnswerAsync(ollama, request, item, gate, abandon.Token)).ToList();
        var pending = answers.Cast<Task>().ToList();
        while (pending.Count > 0)
        {
            var done = await Task.WhenAny(pending);
            pending.Remove(done);
            if (!done.IsCompletedSuccessfully)
            {
                // Fail fast: no prompt waiting for a slot starts after the first failure, which the await rethrows.
                abandon.Cancel();
            }

            await done;
        }

        return Serve.Envelope(Verb, writer =>
        {
            writer.WriteString("model", request.Model);
            writer.WriteString("digest", pin.Digest);
            writer.WriteStartArray("items");
            foreach (var answer in answers.Select(task => task.Result))
            {
                writer.WriteStartObject();
                writer.WriteString("id", answer.Id);
                writer.WriteString("answer", answer.Text);
                writer.WriteNumber("evalCount", answer.EvalCount);
                writer.WriteNumber("promptEvalCount", answer.PromptEvalCount);
                writer.WriteNumber("seconds", Math.Round(answer.Seconds, 3));
                writer.WriteEndObject();
            }

            writer.WriteEndArray();
        });
    }

    // One user message and nothing else: no system prompt, no tools, no images, so the model sees only the prompt.
    internal static JsonObject ChatRequest(InferRequest request, InferItem item)
    {
        var chat = new JsonObject
        {
            ["model"] = request.Model,
            ["stream"] = false,
            ["messages"] = new JsonArray(new JsonObject { ["role"] = "user", ["content"] = item.Prompt }),
        };
        var options = new JsonObject();
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

        if (options.Count > 0)
        {
            chat["options"] = options;
        }

        if (item.Json)
        {
            chat["format"] = "json";
        }

        return chat;
    }

    private static async Task<InferAnswer> AnswerAsync(OllamaClient ollama, InferRequest request, InferItem item, SemaphoreSlim gate, CancellationToken abandon)
    {
        await gate.WaitAsync(abandon);
        try
        {
            var result = await ollama.ChatAsync(ChatRequest(request, item));
            return new InferAnswer(item.Id, result.Content, result.OutputTokens, result.PromptTokens, result.Seconds);
        }
        finally
        {
            gate.Release();
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

    private static InferOptions Options(JsonNode? node)
    {
        if (node is not JsonObject options)
        {
            throw new RefusedException("options must be an object");
        }

        Keys(options, "options", [], ["temperature", "seed", "num_ctx"]);
        return new InferOptions(
            options.ContainsKey("temperature") ? Number(options["temperature"], "options.temperature", 0, MaxTemperature) : null,
            options.ContainsKey("seed") ? Integer(options["seed"], "options.seed", 0, int.MaxValue) : null,
            options.ContainsKey("num_ctx") ? Integer(options["num_ctx"], "options.num_ctx", MinNumCtx, MaxNumCtx) : null);
    }

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
