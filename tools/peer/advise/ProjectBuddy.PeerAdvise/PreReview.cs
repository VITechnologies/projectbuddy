using System.Text;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using ProjectBuddy.PeerOllama;

namespace ProjectBuddy.PeerAdvise;

// One request's worth of diff: the rendered text and, per file, the added lines a finding may cite.
public sealed record ReviewBatch(string Text, IReadOnlyDictionary<string, IReadOnlyDictionary<int, string>> Added);

public sealed record Batching(IReadOnlyList<ReviewBatch> Batches, IReadOnlyList<JsonObject> Unreviewed);

public static partial class PreReview
{
    public const int MaxMessageChars = 300;

    public static async Task<JsonObject> RunAsync(Policy policy, byte[] input, OllamaClient ollama)
    {
        var files = UnifiedDiff.Parse(new UTF8Encoding(false, false).GetString(input));
        if (files.Count == 0)
        {
            throw new PeerException(ExitCodes.Refused, "the diff adds no reviewable lines");
        }

        // Concatenated diffs (staged plus unstaged, git log -p) number one path against different file versions,
        // so anchors could not be told apart; review them one diff at a time.
        var repeated = files.GroupBy(file => file.Path, StringComparer.Ordinal).FirstOrDefault(group => group.Count() > 1);
        if (repeated is not null)
        {
            throw new PeerException(ExitCodes.Refused, $"{repeated.Key} appears more than once; review one diff at a time");
        }

        var version = await ollama.VersionAsync();
        await ModelPin.RequireInstalledAsync(ollama, [policy.Model]);
        var probeTokens = await TruncationGuard.ProbeAsync(ollama, policy.Model.Name, policy.NumCtx, policy.Seed);

        var plan = Batch(files, policy.MaxInputBytes);
        var unreviewed = plan.Unreviewed.ToList();
        var findings = new Dictionary<(string File, int Line, string Rule), JsonObject>();
        var dropped = new Dictionary<string, int> { ["unanchored"] = 0, ["malformed"] = 0, ["overLimit"] = 0 };
        var receipts = new JsonArray();
        var reviewedLines = 0;
        for (var index = 0; index < plan.Batches.Count; index++)
        {
            var batch = plan.Batches[index];
            if (index >= policy.PreReview.MaxBatches)
            {
                unreviewed.AddRange(Unreviewed(batch, "batch limit reached"));
                continue;
            }

            var result = await ollama.ChatAsync(OllamaClient.ChatRequest(
                policy.Model.Name, Prompt(policy), batch.Text, policy.NumCtx, policy.PreReview.NumPredict, policy.Seed, Format(policy)));
            var fits = TruncationGuard.Fits(policy.NumCtx, result);
            var raw = fits ? Json.TryObject(result.Content)?["findings"] as JsonArray : null;
            receipts.Add((JsonNode)new JsonObject
            {
                ["promptTokens"] = result.PromptTokens,
                ["outputTokens"] = result.OutputTokens,
                ["seconds"] = Math.Round(result.Seconds, 2),
                ["outcome"] = !fits ? "rejected" : raw is null ? "malformed" : "reviewed",
            });
            if (raw is null)
            {
                unreviewed.AddRange(Unreviewed(batch, fits ? "the reply was not a findings list" : "the prompt may have been cut"));
                continue;
            }

            reviewedLines += batch.Added.Values.Sum(lines => lines.Count);
            dropped["overLimit"] += Math.Max(0, raw.Count - policy.PreReview.MaxFindings);
            foreach (var node in raw.Take(policy.PreReview.MaxFindings))
            {
                var (finding, reason) = Validate(policy, batch, node);
                if (finding is null)
                {
                    dropped[reason!]++;
                    continue;
                }

                findings.TryAdd((finding["file"]!.GetValue<string>(), finding["line"]!.GetValue<int>(), finding["ruleId"]!.GetValue<string>()), finding);
            }
        }

        if (reviewedLines == 0)
        {
            throw new PeerException(AdviseExitCodes.NoAdvice, "no part of the diff could be reviewed");
        }

        var sorted = findings.Values
            .OrderBy(finding => Array.IndexOf(Policy.Severities, finding["severity"]!.GetValue<string>()))
            .ThenBy(finding => finding["generic"]!.GetValue<bool>())
            .ThenBy(finding => finding["file"]!.GetValue<string>(), StringComparer.Ordinal)
            .ThenBy(finding => finding["line"]!.GetValue<int>())
            .ThenBy(finding => finding["ruleId"]!.GetValue<string>(), StringComparer.Ordinal)
            .Select(finding => (JsonNode)finding)
            .ToArray();
        var addedLines = files.Sum(file => file.Added.Count());
        return new JsonObject
        {
            ["schema"] = Versions.PreReviewSchema,
            ["tool"] = Versions.Tool,
            ["advisory"] = true,
            ["findings"] = new JsonArray(sorted),
            ["summary"] = new JsonObject
            {
                ["total"] = sorted.Length,
                ["specific"] = sorted.Count(finding => !finding!["generic"]!.GetValue<bool>()),
                ["blockers"] = sorted.Count(finding => finding!["severity"]!.GetValue<string>() == "blocker"),
                ["warnings"] = sorted.Count(finding => finding!["severity"]!.GetValue<string>() == "warning"),
                ["nits"] = sorted.Count(finding => finding!["severity"]!.GetValue<string>() == "nit"),
            },
            ["coverage"] = new JsonObject
            {
                ["files"] = files.Count,
                ["addedLines"] = addedLines,
                ["reviewedAddedLines"] = reviewedLines,
                ["complete"] = reviewedLines == addedLines,
                ["unreviewed"] = new JsonArray(unreviewed.Select(item => (JsonNode)item).ToArray()),
            },
            ["dropped"] = new JsonObject
            {
                ["unanchored"] = dropped["unanchored"],
                ["malformed"] = dropped["malformed"],
                ["overLimit"] = dropped["overLimit"],
            },
            ["model"] = new JsonObject { ["name"] = policy.Model.Name, ["digest"] = policy.Model.Digest },
            ["ollamaVersion"] = version,
            ["policySha256"] = policy.Sha256,
            ["receipt"] = new JsonObject { ["numCtx"] = policy.NumCtx, ["probePromptTokens"] = probeTokens, ["batches"] = receipts },
        };
    }

    // A finding counts only when it names a rule and cites an added line in this batch; the report carries
    // that line's code so a reader can check the claim without opening the file.
    internal static (JsonObject? Finding, string? Reason) Validate(Policy policy, ReviewBatch batch, JsonNode? node)
    {
        var finding = node as JsonObject;
        var file = Json.TryString(finding?["file"]);
        var ruleId = Json.TryString(finding?["ruleId"]);
        var message = Json.TryString(finding?["message"])?.Trim();
        var rule = policy.PreReview.Rules.FirstOrDefault(candidate => candidate.Id == ruleId);
        if (finding is null || finding.Count != 4 || file is null || rule is null
            || finding["line"] is not JsonValue lineValue || !lineValue.TryGetValue<int>(out var line)
            || message is null || message.Length == 0 || message.Length > MaxMessageChars || message.IndexOfAny(['\n', '\r']) >= 0)
        {
            return (null, "malformed");
        }

        // A blank line shows nothing, so a finding pinned to one cannot be checked against the code.
        if (!batch.Added.TryGetValue(file, out var lines) || !lines.TryGetValue(line, out var code) || code.Trim().Length == 0)
        {
            return (null, "unanchored");
        }

        return (new JsonObject
        {
            ["file"] = file,
            ["line"] = line,
            ["ruleId"] = rule.Id,
            ["severity"] = rule.Severity,
            ["message"] = message,
            // A message that only restates the rule's guidance names no specific reason: on real diffs these were
            // the false positives, but on the benchmark corpus one was a true hit, so it is labelled, not dropped.
            ["generic"] = Generic(message, rule.Guidance),
            ["code"] = code.Trim(),
        }, null);
    }

    internal static bool Generic(string message, string guidance)
    {
        var said = NonWord().Replace(message.ToLowerInvariant(), " ").Trim();
        return said.Length == 0 || NonWord().Replace(guidance.ToLowerInvariant(), " ").Trim().Contains(said, StringComparison.Ordinal);
    }

    [GeneratedRegex("[^a-z0-9]+")]
    private static partial Regex NonWord();

    // Files in path order; a file too big for one request is split by hunk, and a hunk too big by line window.
    public static Batching Batch(IReadOnlyList<FileDiff> files, int maxBytes)
    {
        var chunks = new List<(string Path, List<DiffHunk> Hunks, string Text)>();
        var unreviewed = new List<JsonObject>();
        foreach (var file in files.OrderBy(file => file.Path, StringComparer.Ordinal))
        {
            var current = new List<DiffHunk>();
            // Windows holding only context or removed lines leave nothing to anchor a finding to.
            foreach (var hunk in file.Hunks.SelectMany(hunk => Split(file.Path, hunk, maxBytes)).Where(hunk => hunk.Lines.Any(line => line.Kind == DiffLineKind.Added)))
            {
                if (Bytes(UnifiedDiff.Render(file.Path, [hunk])) > maxBytes)
                {
                    unreviewed.Add(new JsonObject { ["file"] = file.Path, ["hunk"] = hunk.Header, ["reason"] = "a single line exceeds the budget" });
                    continue;
                }

                if (current.Count > 0 && Bytes(UnifiedDiff.Render(file.Path, [.. current, hunk])) > maxBytes)
                {
                    chunks.Add((file.Path, current, UnifiedDiff.Render(file.Path, current)));
                    current = [];
                }

                current.Add(hunk);
            }

            if (current.Count > 0)
            {
                chunks.Add((file.Path, current, UnifiedDiff.Render(file.Path, current)));
            }
        }

        var batches = new List<ReviewBatch>();
        var text = new StringBuilder();
        var added = new Dictionary<string, Dictionary<int, string>>(StringComparer.Ordinal);
        foreach (var chunk in chunks)
        {
            if (text.Length > 0 && Bytes(text.ToString()) + Bytes(chunk.Text) > maxBytes)
            {
                batches.Add(Close(text, added));
                text.Clear();
                added = new Dictionary<string, Dictionary<int, string>>(StringComparer.Ordinal);
            }

            // Two chunks of one file never share a batch (they were split because they did not fit together),
            // so Add, which throws on a repeat, states that invariant instead of silently merging anchors.
            text.Append(chunk.Text);
            var lines = new Dictionary<int, string>();
            added.Add(chunk.Path, lines);
            foreach (var line in chunk.Hunks.SelectMany(hunk => hunk.Lines).Where(line => line.Kind == DiffLineKind.Added))
            {
                lines[line.NewLine!.Value] = line.Text;
            }
        }

        if (text.Length > 0)
        {
            batches.Add(Close(text, added));
        }

        return new Batching(batches, unreviewed);
    }

    internal static string Prompt(Policy policy) =>
        policy.PreReview.Prompt + $" Report at most {policy.PreReview.MaxFindings} findings.\nRules:\n"
        + string.Join('\n', policy.PreReview.Rules.Select(rule => $"- {rule.Id}: {rule.Guidance}"));

    internal static JsonObject Format(Policy policy) => new()
    {
        ["type"] = "object",
        ["properties"] = new JsonObject
        {
            ["findings"] = new JsonObject
            {
                ["type"] = "array",
                ["items"] = new JsonObject
                {
                    ["type"] = "object",
                    ["properties"] = new JsonObject
                    {
                        ["file"] = new JsonObject { ["type"] = "string" },
                        ["line"] = new JsonObject { ["type"] = "integer" },
                        ["ruleId"] = new JsonObject
                        {
                            ["type"] = "string",
                            ["enum"] = new JsonArray(policy.PreReview.Rules.Select(rule => (JsonNode?)JsonValue.Create(rule.Id)).ToArray()),
                        },
                        ["message"] = new JsonObject { ["type"] = "string" },
                    },
                    ["required"] = new JsonArray("file", "line", "ruleId", "message"),
                },
            },
        },
        ["required"] = new JsonArray("findings"),
    };

    // A hunk larger than the budget is cut into windows of whole lines; the line numbers stay those of the new file.
    private static IEnumerable<DiffHunk> Split(string path, DiffHunk hunk, int maxBytes)
    {
        if (Bytes(UnifiedDiff.Render(path, [hunk])) <= maxBytes)
        {
            yield return hunk;
            yield break;
        }

        var window = new List<DiffLine>();
        var part = 1;
        foreach (var line in hunk.Lines)
        {
            // Measure with the header the window will carry: "(part N)" adds bytes.
            if (window.Count > 0 && Bytes(UnifiedDiff.Render(path, [new DiffHunk($"{hunk.Header} (part {part})", [.. window, line])])) > maxBytes)
            {
                yield return new DiffHunk($"{hunk.Header} (part {part++})", window);
                window = [];
            }

            window.Add(line);
        }

        yield return new DiffHunk($"{hunk.Header} (part {part})", window);
    }

    private static IEnumerable<JsonObject> Unreviewed(ReviewBatch batch, string reason) =>
        batch.Added.Keys.Order(StringComparer.Ordinal).Select(file => new JsonObject { ["file"] = file, ["reason"] = reason });

    private static ReviewBatch Close(StringBuilder text, Dictionary<string, Dictionary<int, string>> added) =>
        new(text.ToString(), added.ToDictionary(pair => pair.Key, pair => (IReadOnlyDictionary<int, string>)pair.Value, StringComparer.Ordinal));

    private static int Bytes(string text) => Encoding.UTF8.GetByteCount(text);
}
