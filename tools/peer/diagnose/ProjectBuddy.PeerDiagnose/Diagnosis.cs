using System.Security.Cryptography;
using System.Text;
using System.Text.Encodings.Web;
using System.Text.Json;
using System.Text.Json.Nodes;
using ProjectBuddy.PeerOllama;
using ProjectBuddy.PeerProbes;
using ProjectBuddy.PeerRedact;

namespace ProjectBuddy.PeerDiagnose;

public static class DiagnoseExitCodes
{
    // The model answered, but nothing it said could be trusted: a prompt may have been cut, or a citation did not match
    // the tool results it was given.
    public const int NoAdvice = 6;
}

public static class Versions
{
    public const string Tool = "0.1.0";
    public const string ReportSchema = "projectbuddy/peer-diagnosis@1";
}

// One tool result the model was given: the accepted call and the redacted envelope it returned.
internal sealed record Evidence(string Tool, JsonObject Arguments, string Envelope);

// Counters for the receipt; every request and call adds to them.
internal sealed class Receipt
{
    public int Requests { get; set; }

    public int Rounds { get; set; }

    public int ToolCalls { get; set; }

    public int Refused { get; set; }

    public int AnswerAttempts { get; set; }

    public int EvidenceBytes { get; set; }

    public int MaxPromptTokens { get; set; }

    public int OutputTokens { get; set; }

    public double Seconds { get; set; }
}

// The P4 agent loop (#295): the model may call only the read-only tools of tools/peer/probes, within fixed budgets, and
// its answer is trusted only when every fact it cites is found, exactly, in a tool result it was given.
public static class Diagnosis
{
    // Refusals are read by the model; they are never embedded in HTML.
    private static readonly JsonSerializerOptions Readable = new() { Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping };

    private const int MaxShownChars = 120;

    public static async Task<JsonObject> RunAsync(Policy policy, Question question, OllamaClient ollama, Registry registry, Func<string, Redaction> redact)
    {
        var version = await ollama.VersionAsync();
        await ModelPin.RequireInstalledAsync(ollama, [policy.Model]);
        var probeTokens = await TruncationGuard.ProbeAsync(ollama, policy.Model.Name, policy.NumCtx, policy.Seed);

        var request = OllamaClient.ChatRequest(policy.Model.Name, policy.System, Prompt(question), policy.NumCtx, policy.NumPredict, policy.Seed, tools: ToolDefinitions());
        var messages = request["messages"]!.AsArray();
        var evidence = new List<Evidence>();
        var receipt = new Receipt();
        while (receipt.Rounds < policy.MaxRounds)
        {
            var reply = await ChatAsync(ollama, policy, request, receipt);
            receipt.Rounds++;
            if (reply.ToolCalls.Count == 0)
            {
                break;
            }

            messages.Add((JsonNode)new JsonObject
            {
                ["role"] = "assistant",
                ["content"] = reply.Content,
                ["tool_calls"] = new JsonArray([.. reply.ToolCalls.Select(call => (JsonNode)new JsonObject
                {
                    ["function"] = new JsonObject { ["name"] = call.Name, ["arguments"] = call.Arguments?.DeepClone() ?? new JsonObject() },
                })]),
            });
            for (var index = 0; index < reply.ToolCalls.Count; index++)
            {
                var content = await AnswerCallAsync(policy, registry, reply.ToolCalls[index], index, evidence, receipt);
                messages.Add((JsonNode)new JsonObject { ["role"] = "tool", ["tool_name"] = reply.ToolCalls[index].Name, ["content"] = content });
            }
        }

        // The answer is asked for on its own, without tools and constrained to the answer's shape. When the rules decide
        // the class from the evidence, the shape allows only that class from the first attempt: the class is verified by
        // construction and the model's attempts go to the finding and the citations (#302). What leaves passes the
        // redactor again and is grounded as redacted: tool results were already redacted, so true citations still match.
        // A rejected answer is shown its reason and may be given again, up to maxAnswerAttempts.
        var decision = Rules.Decide(question.Rules, evidence);
        request.Remove("tools");
        request["format"] = AnswerFormat(policy, question, decision.Class);
        messages.Add((JsonNode)new JsonObject { ["role"] = "user", ["content"] = decision.Class is null ? policy.AnswerPrompt : $"{policy.AnswerPrompt}\n\n{Hint(decision)}" });
        JsonObject answer;
        while (true)
        {
            var content = redact((await ChatAsync(ollama, policy, request, receipt)).Content).Text;
            receipt.AnswerAttempts++;
            try
            {
                answer = Ground(policy, question, evidence, content);

                // The shape already allows only the decided class; this check holds even if a server ignored it.
                if (decision.Class is { } decided && (string?)answer["class"] != decided)
                {
                    throw NoAdvice($"class {Show((string)answer["class"]!)} contradicts the policy's rules, which decide {decided} from the results");
                }

                break;
            }
            catch (PeerException rejected) when (receipt.AnswerAttempts < policy.MaxAnswerAttempts)
            {
                messages.Add((JsonNode)new JsonObject { ["role"] = "assistant", ["content"] = content });
                messages.Add((JsonNode)new JsonObject
                {
                    ["role"] = "user",
                    ["content"] = $"That answer was rejected: {rejected.Message}. Answer again with JSON only; cite exact single values at pointers that exist in the tool results above.",
                });
            }
        }

        return new JsonObject
        {
            ["schema"] = Versions.ReportSchema,
            ["tool"] = $"projectbuddy-diagnose {Versions.Tool}",
            ["advisory"] = true,
            ["question"] = question.Id,
            ["verified"] = decision.Class is null ? new JsonArray("citations") : new JsonArray("citations", "class"),
            ["answer"] = answer,
            ["rule"] = decision.Class is null ? null : new JsonObject
            {
                ["class"] = decision.Class,
                ["basis"] = new JsonArray([.. decision.Basis.Select(fact => (JsonNode)new JsonObject { ["tool"] = fact.Tool, ["pointer"] = fact.Pointer, ["value"] = fact.Value })]),
            },
            ["evidence"] = new JsonArray([.. evidence.Select(item => (JsonNode)new JsonObject
            {
                ["tool"] = item.Tool,
                ["arguments"] = item.Arguments.DeepClone(),
                ["sha256"] = Convert.ToHexStringLower(SHA256.HashData(Encoding.UTF8.GetBytes(item.Envelope))),
            })]),
            ["model"] = new JsonObject { ["name"] = policy.Model.Name, ["digest"] = policy.Model.Digest },
            ["ollamaVersion"] = version,
            ["policySha256"] = policy.Sha256,
            ["receipt"] = new JsonObject
            {
                ["numCtx"] = policy.NumCtx,
                ["probePromptTokens"] = probeTokens,
                ["requests"] = receipt.Requests,
                ["rounds"] = receipt.Rounds,
                ["toolCalls"] = receipt.ToolCalls,
                ["refusedCalls"] = receipt.Refused,
                ["answerAttempts"] = receipt.AnswerAttempts,
                ["evidenceBytes"] = receipt.EvidenceBytes,
                ["maxPromptTokens"] = receipt.MaxPromptTokens,
                ["outputTokens"] = receipt.OutputTokens,
                ["seconds"] = Math.Round(receipt.Seconds, 2),
            },
        };
    }

    // The question as the model reads it: its text, then its class rules as generated prose.
    public static string Prompt(Question question) =>
        $"{question.Text}\n\n{Rules.Describe(question.Rules)}\nWhen the results cannot decide these rules, the class is your judgment; use inconclusive if they do not answer the question.";

    // The registry's specs as Ollama tool definitions: the closed list of names, and each argument's type and range.
    public static JsonArray ToolDefinitions() => new([.. Registry.Tools.Select(spec => (JsonNode)new JsonObject
    {
        ["type"] = "function",
        ["function"] = new JsonObject
        {
            ["name"] = spec.Name,
            ["description"] = spec.Description,
            ["parameters"] = new JsonObject
            {
                ["type"] = "object",
                ["properties"] = new JsonObject(spec.Arguments.Select(argument => KeyValuePair.Create<string, JsonNode?>(argument.Name, new JsonObject
                {
                    ["type"] = "integer",
                    ["minimum"] = argument.Min,
                    ["maximum"] = argument.Max,
                    ["description"] = $"{argument.Description} Default {argument.Default}.",
                }))),
                ["required"] = new JsonArray(),
            },
        },
    })]);

    // The answer's shape: a class from the question's list (or only onlyClass), a bounded finding, and 1 to
    // maxCitations citations of short scalar values from named tools.
    public static JsonObject AnswerFormat(Policy policy, Question question, string? onlyClass = null) => new()
    {
        ["type"] = "object",
        ["properties"] = new JsonObject
        {
            ["class"] = new JsonObject { ["type"] = "string", ["enum"] = new JsonArray([.. (onlyClass is null ? question.Classes : [onlyClass]).Select(name => (JsonNode)name)]) },
            ["finding"] = new JsonObject { ["type"] = "string", ["maxLength"] = policy.MaxFindingChars },
            ["citations"] = new JsonObject
            {
                ["type"] = "array",
                ["minItems"] = 1,
                ["maxItems"] = policy.MaxCitations,
                ["items"] = new JsonObject
                {
                    ["type"] = "object",
                    ["properties"] = new JsonObject
                    {
                        ["tool"] = new JsonObject { ["type"] = "string", ["enum"] = new JsonArray([.. Registry.Tools.Select(spec => (JsonNode)spec.Name)]) },
                        ["pointer"] = new JsonObject { ["type"] = "string" },
                        ["value"] = new JsonObject { ["type"] = new JsonArray("string", "number", "boolean", "null") },
                    },
                    ["required"] = new JsonArray("tool", "pointer", "value"),
                },
            },
        },
        ["required"] = new JsonArray("class", "finding", "citations"),
    };

    // Model output is evidence to check, never to trust. The answer must be exactly {class, finding, citations}; every
    // citation's pointer (RFC 6901) must resolve, in a result of that tool the model was given, to exactly the cited
    // value.
    internal static JsonObject Ground(Policy policy, Question question, IReadOnlyList<Evidence> evidence, string content)
    {
        var answer = Json.TryObject(content);
        if (answer is null || answer.Count != 3 || Json.TryString(answer["class"]) is not { } answerClass
            || Json.TryString(answer["finding"]) is not { } finding || answer["citations"] is not JsonArray citations)
        {
            throw NoAdvice("the answer is not exactly {class, finding, citations}");
        }

        if (!question.Classes.Contains(answerClass))
        {
            throw NoAdvice($"class {Show(answerClass)} is not one of {question.Id}'s classes");
        }

        finding = finding.Trim();
        if (finding.Length == 0 || finding.Length > policy.MaxFindingChars)
        {
            throw NoAdvice($"the finding must be 1 to {policy.MaxFindingChars} characters");
        }

        if (citations.Count == 0 || citations.Count > policy.MaxCitations)
        {
            throw NoAdvice($"the answer needs 1 to {policy.MaxCitations} citations");
        }

        var grounded = new JsonArray();
        foreach (var node in citations)
        {
            if (node is not JsonObject citation || citation.Count != 3 || Json.TryString(citation["tool"]) is not { } tool
                || Json.TryString(citation["pointer"]) is not { } pointer || !citation.TryGetPropertyValue("value", out var value) || !IsScalar(value, policy.MaxValueChars))
            {
                throw NoAdvice($"each citation must be exactly {{tool, pointer, value}} with a scalar value of at most {policy.MaxValueChars} characters");
            }

            if (!evidence.Any(item => item.Tool == tool && Matches(JsonNode.Parse(item.Envelope), pointer, value)))
            {
                throw NoAdvice($"the citation {Show(tool)} {Show(pointer)} = {Show(value)} matches no result of that tool the model was given");
            }

            grounded.Add((JsonNode)new JsonObject { ["tool"] = tool, ["pointer"] = pointer, ["value"] = value?.DeepClone() });
        }

        return new JsonObject { ["class"] = answerClass, ["finding"] = finding, ["citations"] = grounded };
    }

    // A pointer is resolved against the tool result as the model received it and, failing that, against the result's
    // data object, which is where models usually start counting. Either way the value must match exactly.
    private static bool Matches(JsonNode? result, string pointer, JsonNode? value) =>
        (Resolve(result, pointer, out var found) && Same(found, value)) || (Resolve(result!["data"], pointer, out var inData) && Same(inData, value));

    // When the rules decide, the model is shown the facts they rest on (or, for a rule with no facts to show, such as
    // "no element exists", the rule itself), so it can cite exactly what was found (#304). Grounding is unchanged.
    internal static string Hint(Decision decision) => decision.Basis.Count > 0
        ? $"The policy's rules decide the class {decision.Class} from these facts; cite them exactly as written (tool, pointer, value):"
            + string.Concat(decision.Basis.Select(fact => $"\n- {fact.Tool} {fact.Pointer} = {Show(fact.Value)}"))
        : $"The policy's rules decide the class {decision.Class} because {Rules.Describe(decision.Rule!)}.";

    // Scalars are compared by their JSON text, never converted: a string by its content, a number or boolean by its exact
    // token, which a cited string may also give ("5" cites 5, models often quote numbers), and an empty list or object by
    // "[]" or "{}". Comparing text cannot throw on an exponent no number type holds, and only the evidence's own
    // spelling of a number is accepted.
    internal static bool Same(JsonNode? found, JsonNode? cited) => found switch
    {
        null => cited is null,
        JsonValue value when value.GetValueKind() == JsonValueKind.String => Json.TryString(cited) == value.GetValue<string>(),
        JsonValue value => cited is not null && (cited.ToJsonString() == value.ToJsonString() || Json.TryString(cited) == value.ToJsonString()),

        // An empty list or object is a fact too; its JSON text is cited as a string (#304).
        JsonArray { Count: 0 } => Json.TryString(cited) == "[]",
        JsonObject { Count: 0 } => Json.TryString(cited) == "{}",
        _ => false,
    };

    // RFC 6901: "/a/0/b", with ~1 for "/" and ~0 for "~"; array indexes are decimal without leading zeros.
    internal static bool Resolve(JsonNode? root, string pointer, out JsonNode? found)
    {
        found = null;
        if (pointer.Length == 0 || pointer[0] != '/')
        {
            return false;
        }

        var node = root;
        foreach (var raw in pointer[1..].Split('/'))
        {
            var token = raw.Replace("~1", "/", StringComparison.Ordinal).Replace("~0", "~", StringComparison.Ordinal);
            if (node is JsonObject value && value.TryGetPropertyValue(token, out var child))
            {
                node = child;
            }
            else if (node is JsonArray array && Index(token) is { } index && index < array.Count)
            {
                node = array[index];
            }
            else
            {
                return false;
            }
        }

        found = node;
        return true;
    }

    private static int? Index(string token) =>
        token.Length is > 0 and <= 9 && token.All(char.IsAsciiDigit) && (token == "0" || token[0] != '0') ? int.Parse(token) : null;

    private static bool IsScalar(JsonNode? node, int maxChars) => node switch
    {
        null => true,
        JsonValue value when value.TryGetValue<string>(out var text) => text.Length <= maxChars,
        // Parsed JSON has no other kinds of value: anything else that is a JsonValue is a number or a boolean.
        JsonValue => true,
        _ => false,
    };

    // Every request must provably fit half the context, or nothing from this run is trusted.
    private static async Task<ChatResult> ChatAsync(OllamaClient ollama, Policy policy, JsonObject request, Receipt receipt)
    {
        var result = await ollama.ChatAsync(request);
        receipt.Requests++;
        receipt.MaxPromptTokens = Math.Max(receipt.MaxPromptTokens, result.PromptTokens);
        receipt.OutputTokens += result.OutputTokens;
        receipt.Seconds += result.Seconds;
        return TruncationGuard.Fits(policy.NumCtx, result)
            ? result
            : throw NoAdvice($"request {receipt.Requests} did not provably fit half the context ({result.PromptTokens} prompt tokens)");
    }

    // A call outside the budgets or the registry's spec is answered with a refusal the model can read; an accepted call
    // returns the redacted envelope. A repeated call returns its first result, so one question sees one machine state.
    private static async Task<string> AnswerCallAsync(Policy policy, Registry registry, ToolCall call, int index, List<Evidence> evidence, Receipt receipt)
    {
        if (index >= policy.MaxCallsPerRound || receipt.ToolCalls >= policy.MaxCalls)
        {
            receipt.Refused++;
            return Refusal("the call budget is spent; answer with the results you have");
        }

        receipt.ToolCalls++;
        var arguments = call.Arguments ?? [];
        var earlier = evidence.FirstOrDefault(item => item.Tool == call.Name && JsonNode.DeepEquals(item.Arguments, arguments));
        string envelope;
        try
        {
            envelope = earlier?.Envelope ?? await registry.InvokeAsync(call.Name, arguments);
        }
        catch (ToolRefusedException refused)
        {
            receipt.Refused++;
            return Refusal(refused.Message);
        }

        var bytes = Encoding.UTF8.GetByteCount(envelope);
        if (receipt.EvidenceBytes + bytes > policy.MaxEvidenceBytes)
        {
            receipt.Refused++;
            return Refusal("the evidence budget is spent; answer with the results you have");
        }

        receipt.EvidenceBytes += bytes;
        if (earlier is null)
        {
            evidence.Add(new Evidence(call.Name, arguments, envelope));
        }

        return envelope;
    }

    // Model text in a message is shown as JSON, so control characters stay escaped, and cut short.
    private static string Show(JsonNode? node)
    {
        var text = node?.ToJsonString(Readable) ?? "null";
        return text.Length <= MaxShownChars ? text : text[..MaxShownChars] + "…";
    }

    private static string Show(string text) => Show(JsonValue.Create(text));

    private static string Refusal(string reason) => new JsonObject { ["refused"] = reason }.ToJsonString(Readable);

    private static PeerException NoAdvice(string reason) => new(DiagnoseExitCodes.NoAdvice, reason);
}
