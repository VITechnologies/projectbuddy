using System.Text;
using System.Text.Json.Nodes;
using ProjectBuddy.PeerOllama;

namespace ProjectBuddy.PeerAdvise;

public static class Triage
{
    public const int MinStepChars = 3;
    public const int MaxStepChars = 200;
    public const int MinLineChars = 3;
    public const int MinQuoteChars = 8;

    public static async Task<JsonObject> RunAsync(Policy policy, byte[] input, OllamaClient ollama)
    {
        var normalized = LogText.Normalize(new UTF8Encoding(false, false).GetString(input));
        if (normalized.Length == 0)
        {
            throw new PeerException(ExitCodes.Refused, "the log is empty");
        }

        var version = await ollama.VersionAsync();
        await ModelPin.RequireInstalledAsync(ollama, [policy.Model]);
        var probeTokens = await TruncationGuard.ProbeAsync(ollama, policy.Model.Name, policy.NumCtx, policy.Seed);

        // Full budget first, then half: a dense log (paths, hashes) can cost two tokens per byte.
        int[] budgets = [policy.MaxInputBytes, policy.MaxInputBytes / 2];
        var attempts = 0;
        foreach (var budget in budgets)
        {
            attempts++;
            var tail = LogText.Build(normalized, budget);
            if (tail.Text.Length == 0)
            {
                break;
            }

            var result = await ollama.ChatAsync(OllamaClient.ChatRequest(
                policy.Model.Name, policy.TriagePrompt, tail.Text, policy.NumCtx, policy.NumPredict, policy.Seed, Format(policy)));
            if (!TruncationGuard.Fits(policy.NumCtx, result))
            {
                continue;
            }

            var answer = Validate(policy, tail, result.Content);
            return new JsonObject
            {
                ["schema"] = Versions.TriageSchema,
                ["tool"] = Versions.Tool,
                ["advisory"] = true,
                // Only checked fields are listed: errorLine always, step when it was found in the log (otherwise it
                // is reported as null); class is the model's judgment.
                ["verified"] = answer["step"] is null ? new JsonArray("errorLine") : new JsonArray("step", "errorLine"),
                ["answer"] = answer,
                ["model"] = new JsonObject { ["name"] = policy.Model.Name, ["digest"] = policy.Model.Digest },
                ["ollamaVersion"] = version,
                ["policySha256"] = policy.Sha256,
                ["input"] = new JsonObject
                {
                    ["bytes"] = input.Length,
                    ["normalizedBytes"] = Encoding.UTF8.GetByteCount(normalized),
                    ["sentBytes"] = tail.Bytes,
                    ["signalLines"] = tail.SignalLines,
                    ["droppedLines"] = tail.DroppedLines,
                    ["attempts"] = attempts,
                },
                ["receipt"] = new JsonObject
                {
                    ["numCtx"] = policy.NumCtx,
                    ["probePromptTokens"] = probeTokens,
                    ["promptTokens"] = result.PromptTokens,
                    ["outputTokens"] = result.OutputTokens,
                    ["seconds"] = Math.Round(result.Seconds, 2),
                },
            };
        }

        throw new PeerException(AdviseExitCodes.NoAdvice, $"the evidence did not provably fit half the context after {attempts} attempt(s); lower maxInputBytes");
    }

    // Model output is evidence to check, never to trust: every claim must come from a line of the log itself,
    // never from the tool's own headings, and the reported error line is the whole log line the quote came from.
    // The error line is required; a step the log does not contain is dropped rather than passed on.
    internal static JsonObject Validate(Policy policy, Evidence evidence, string content)
    {
        var answer = Json.TryObject(content);
        var step = Json.TryString(answer?["step"])?.Trim();
        var quote = Json.TryString(answer?["errorLine"])?.Trim();
        var triageClass = Json.TryString(answer?["class"]);
        if (answer is null || answer.Count != 3 || step is null || quote is null || triageClass is null)
        {
            throw NoAdvice("the answer is not exactly {step, errorLine, class}");
        }

        var stepFound = step.Length >= MinStepChars && step.Length <= MaxStepChars
            && evidence.Lines.Any(line => line.Contains(step, StringComparison.OrdinalIgnoreCase));

        // Prefer a line the quote equals: a later line that merely mentions the same text (a retry, an echo)
        // may say the opposite of the failure the model quoted.
        var lines = evidence.Lines.Where(line => !LogText.IsHeader(line)).ToList();
        // A whole line counts from MinLineChars (a bare "Killed" is real evidence); a fragment needs MinQuoteChars.
        var oneLine = quote.IndexOfAny(['\n', '\r']) < 0;
        var line = (oneLine && quote.Length >= MinLineChars ? lines.LastOrDefault(line => Unclipped(line).Trim() == quote) : null)
            ?? (oneLine && quote.Length >= MinQuoteChars ? lines.LastOrDefault(line => Unclipped(line).Contains(quote, StringComparison.Ordinal)) : null);
        if (line is null)
        {
            throw NoAdvice($"the error line is neither a whole log line nor a quote of at least {MinQuoteChars} characters from one");
        }

        if (!policy.TriageClasses.Contains(triageClass))
        {
            throw NoAdvice("the class is not in the policy");
        }

        return new JsonObject { ["step"] = stepFound ? step : null, ["errorLine"] = line.Trim(), ["class"] = triageClass };
    }

    private static string Unclipped(string line) => line.EndsWith(LogText.ClipMarker, StringComparison.Ordinal) ? line[..^LogText.ClipMarker.Length] : line;

    internal static JsonObject Format(Policy policy) => new()
    {
        ["type"] = "object",
        ["properties"] = new JsonObject
        {
            ["step"] = new JsonObject { ["type"] = "string" },
            ["errorLine"] = new JsonObject { ["type"] = "string" },
            ["class"] = new JsonObject
            {
                ["type"] = "string",
                ["enum"] = new JsonArray(policy.TriageClasses.Select(value => (JsonNode?)JsonValue.Create(value)).ToArray()),
            },
        },
        ["required"] = new JsonArray("step", "errorLine", "class"),
    };

    private static PeerException NoAdvice(string reason) => new(AdviseExitCodes.NoAdvice, $"no advice: {reason}");
}
