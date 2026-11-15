using System.Text;
using System.Text.Json.Nodes;

namespace ProjectBuddy.PeerBench;

internal sealed class Tally
{
    public int TriageItems;
    public int Extraction;
    public int Diagnosis;
    public int SecurityExpected;
    public int SecurityHits;
    public int FalsePositives;
    public int ToolItems;
    public int ToolHits;
    public int Malformed;
    public int Rejected;
}

public static class Bench
{
    public const string ToolName = "ask_peer";

    public static async Task<JsonObject> RunAsync(Corpus corpus, IReadOnlyList<Candidate> candidates, string machine, OllamaClient ollama, TextWriter log)
    {
        var version = await ollama.VersionAsync();
        var installed = await ollama.DigestsAsync();
        foreach (var candidate in candidates)
        {
            if (!installed.TryGetValue(candidate.Name, out var digest) || digest != candidate.Digest)
            {
                throw new BenchException(ExitCodes.Refused, $"{candidate.Name} is not installed with the pinned digest");
            }
        }

        var models = new JsonArray();
        foreach (var candidate in candidates)
        {
            models.Add((JsonNode)await RunModelAsync(corpus, candidate, ollama, log));
        }

        return new JsonObject
        {
            ["schema"] = Versions.ResultSchema,
            ["tool"] = Versions.Tool,
            ["machine"] = machine,
            ["ollamaVersion"] = version,
            ["corpusSha256"] = corpus.Sha256,
            ["numCtx"] = corpus.NumCtx,
            ["numPredict"] = corpus.NumPredict,
            ["seed"] = corpus.Seed,
            ["models"] = models,
        };
    }

    private static async Task<JsonObject> RunModelAsync(Corpus corpus, Candidate candidate, OllamaClient ollama, TextWriter log)
    {
        // Ollama keeps only the last num_ctx/2 tokens of an oversized prompt, silently. Prove the server
        // still behaves that way before trusting the per-item guard below.
        var probe = await ollama.ChatAsync(Request(corpus, candidate.Name, "", ProbeText(corpus.NumCtx), 1, null, null));
        if (probe.PromptTokens < corpus.NumCtx / 2)
        {
            throw new BenchException(ExitCodes.GuardBroken, $"{candidate.Name}: truncation probe reported {probe.PromptTokens} prompt tokens; the guard cannot be trusted");
        }

        var tally = new Tally();
        var items = new JsonArray();
        var seconds = new List<double>();
        var rates = new List<double>();
        Residency? residency = null;
        var loaded = false;
        foreach (var item in corpus.Items)
        {
            var (format, tools) = Shape(corpus, item.Kind);
            var result = await ollama.ChatAsync(Request(corpus, candidate.Name, corpus.Prompts[item.Kind], item.Input, corpus.NumPredict, format, tools));
            if (!loaded)
            {
                residency = await ollama.ResidencyAsync(candidate.Name);
                loaded = true;
            }

            var outcome = Fits(corpus, result) ? Score(corpus, item, result, tally) : Reject(tally);
            seconds.Add(result.Seconds);
            if (result.TokensPerSecond > 0)
            {
                rates.Add(result.TokensPerSecond);
            }

            log.WriteLine($"{candidate.Name} {item.Id}: {outcome} ({result.Seconds:F1} s)");
            items.Add((JsonNode)new JsonObject
            {
                ["id"] = item.Id,
                ["outcome"] = outcome,
                ["seconds"] = Math.Round(result.Seconds, 2),
                ["promptTokens"] = result.PromptTokens,
            });
        }

        await ollama.UnloadAsync(candidate.Name);
        return new JsonObject
        {
            ["name"] = candidate.Name,
            ["digest"] = candidate.Digest,
            ["probePromptTokens"] = probe.PromptTokens,
            ["vram"] = new JsonObject
            {
                ["loaded"] = residency is not null,
                ["sizeBytes"] = residency?.SizeBytes ?? 0,
                ["sizeVramBytes"] = residency?.SizeVramBytes ?? 0,
                ["fullGpu"] = residency is { SizeBytes: > 0 } fit && fit.SizeVramBytes == fit.SizeBytes,
            },
            ["triage"] = new JsonObject { ["items"] = tally.TriageItems, ["extraction"] = tally.Extraction, ["diagnosis"] = tally.Diagnosis },
            ["security"] = new JsonObject { ["expected"] = tally.SecurityExpected, ["hits"] = tally.SecurityHits, ["falsePositives"] = tally.FalsePositives },
            ["tool"] = new JsonObject { ["items"] = tally.ToolItems, ["hits"] = tally.ToolHits },
            ["malformed"] = tally.Malformed,
            ["rejected"] = tally.Rejected,
            ["latency"] = new JsonObject
            {
                ["medianSeconds"] = Math.Round(Median(seconds), 2),
                ["meanTokensPerSecond"] = Math.Round(rates.Count > 0 ? rates.Average() : 0, 1),
            },
            ["items"] = items,
        };
    }

    // Accept a reply only when the whole prompt provably fit: below half the context, where truncated prompts land.
    internal static bool Fits(Corpus corpus, ChatResult result) =>
        result.PromptTokens > 0 && result.PromptTokens < corpus.NumCtx / 2 && result.PromptTokens + result.OutputTokens <= corpus.NumCtx;

    internal static string ProbeText(int numCtx)
    {
        var text = new StringBuilder();
        for (var index = 0; index < numCtx * 2; index++)
        {
            text.Append("w").Append(index).Append(' ');
        }

        return text.ToString();
    }

    internal static JsonObject Request(Corpus corpus, string model, string system, string input, int numPredict, JsonObject? format, JsonArray? tools)
    {
        var request = new JsonObject
        {
            ["model"] = model,
            ["stream"] = false,
            ["keep_alive"] = "5m",
            ["options"] = new JsonObject
            {
                ["num_ctx"] = corpus.NumCtx,
                ["num_predict"] = numPredict,
                ["temperature"] = 0,
                ["seed"] = corpus.Seed,
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

    internal static (JsonObject? Format, JsonArray? Tools) Shape(Corpus corpus, ItemKind kind) => kind switch
    {
        ItemKind.Triage => (Schema(
            ("step", new JsonObject { ["type"] = "string" }),
            ("errorLine", new JsonObject { ["type"] = "string" }),
            ("class", Enum(corpus.TriageClasses))), null),
        ItemKind.Security => (Schema(("findings", new JsonObject
        {
            ["type"] = "array",
            ["items"] = Schema(("class", Enum(corpus.SecurityClasses)), ("line", new JsonObject { ["type"] = "integer" })),
        })), null),
        _ => (null, new JsonArray(new JsonObject
        {
            ["type"] = "function",
            ["function"] = new JsonObject
            {
                ["name"] = ToolName,
                ["description"] = "Ask the peer machine one read-only diagnostic question.",
                ["parameters"] = Schema(("verb", Enum(corpus.Verbs))),
            },
        })),
    };

    internal static string Score(Corpus corpus, CorpusItem item, ChatResult result, Tally tally) => item.Kind switch
    {
        ItemKind.Triage => ScoreTriage(item, result, tally),
        ItemKind.Security => ScoreSecurity(corpus, item, result, tally),
        _ => ScoreTool(item, result, tally),
    };

    private static string ScoreTriage(CorpusItem item, ChatResult result, Tally tally)
    {
        tally.TriageItems++;
        var answer = Json.TryObject(result.Content);
        var step = Json.TryString(answer?["step"]);
        var line = Json.TryString(answer?["errorLine"]);
        var triageClass = Json.TryString(answer?["class"]);
        if (step is null || line is null || triageClass is null)
        {
            return Malformed(tally);
        }

        // Extraction: the right step, and an error line quoted verbatim from the evidence.
        var extraction = step.Contains(item.Step, StringComparison.OrdinalIgnoreCase)
            && line.Contains(item.ErrorLine, StringComparison.Ordinal)
            && item.Input.Contains(line.Trim(), StringComparison.Ordinal);
        var diagnosis = triageClass == item.Class;
        tally.Extraction += extraction ? 1 : 0;
        tally.Diagnosis += diagnosis ? 1 : 0;
        return extraction && diagnosis ? "pass" : "fail";
    }

    private static string ScoreSecurity(Corpus corpus, CorpusItem item, ChatResult result, Tally tally)
    {
        tally.SecurityExpected += item.Classes.Count;
        if (Json.TryObject(result.Content)?["findings"] is not JsonArray findings)
        {
            return Malformed(tally);
        }

        var reported = new HashSet<string>(StringComparer.Ordinal);
        foreach (var finding in findings)
        {
            var findingClass = Json.TryString((finding as JsonObject)?["class"]);
            if (findingClass is null || !corpus.SecurityClasses.Contains(findingClass))
            {
                return Malformed(tally);
            }

            reported.Add(findingClass);
        }

        var hits = item.Classes.Count(reported.Contains);
        var falsePositives = reported.Count(found => !item.Classes.Contains(found));
        tally.SecurityHits += hits;
        tally.FalsePositives += falsePositives;
        return hits == item.Classes.Count && falsePositives == 0 ? "pass" : "fail";
    }

    private static string ScoreTool(CorpusItem item, ChatResult result, Tally tally)
    {
        tally.ToolItems++;
        if (result.ToolCalls.Count != 1)
        {
            return "fail";
        }

        var call = result.ToolCalls[0];
        var verb = Json.TryString(call.Arguments?["verb"]);
        if (verb is null)
        {
            return Malformed(tally);
        }

        var hit = call.Name == ToolName && verb == item.Verb;
        tally.ToolHits += hit ? 1 : 0;
        return hit ? "pass" : "fail";
    }

    private static string Malformed(Tally tally)
    {
        tally.Malformed++;
        return "malformed";
    }

    private static string Reject(Tally tally)
    {
        tally.Rejected++;
        return "rejected";
    }

    private static double Median(List<double> values)
    {
        var sorted = values.Order().ToList();
        var middle = sorted.Count / 2;
        return sorted.Count % 2 == 1 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
    }

    private static JsonObject Enum(IEnumerable<string> values) =>
        new() { ["type"] = "string", ["enum"] = new JsonArray(values.Select(value => (JsonNode?)JsonValue.Create(value)).ToArray()) };

    private static JsonObject Schema(params (string Name, JsonObject Type)[] properties)
    {
        var shape = new JsonObject();
        foreach (var (name, type) in properties)
        {
            shape[name] = type;
        }

        return new JsonObject
        {
            ["type"] = "object",
            ["properties"] = shape,
            ["required"] = new JsonArray(properties.Select(property => (JsonNode?)JsonValue.Create(property.Name)).ToArray()),
        };
    }
}
