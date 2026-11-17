using System.Globalization;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace ProjectBuddy.Kpi;

// Reads the agent's session event logs (<sessions>/<session id>/events.jsonl): model calls with the AI Units (AIU) the
// quota is billed in, tool calls, operator messages, and the session's active spans. Accepted changes come from the forge
// (see Forge). A forked session's log begins with its own session.start and then repeats its parent's history; events
// dated before that start belong to the parent and are skipped.
//
// The AIU are measured, not estimated. An agent (SDK) session's usage record carries its call's exact AIU
// (copilotUsage.totalNanoAiu). The session's totals come from a cumulative session.usage_checkpoint counter, or in a
// CLI session from each process's session.shutdown total. In each measured interval, what its calls' exact AIU do not
// account for is shared among its calls without an exact value (every CLI call, and older records) by their
// price-weighted tokens; the rest of it, such as helper-model calls the log records no usage for, stays in the
// session's measured total.
internal static partial class Ingest
{
    public sealed record ModelCall(
        Guid EventId, DateTimeOffset At, string Initiator, string Model, decimal Cost, long InputTokens, long OutputTokens, long CacheReadTokens,
        string Category, decimal Weight, decimal? Aiu = null);

    public sealed record ToolCall(string Id, string Tool, DateTimeOffset Started, DateTimeOffset? Completed, bool? Success);

    public sealed record Session(
        string Id, DateTimeOffset First, DateTimeOffset Last, long Events, List<ModelCall> Calls, List<ToolCall> Tools,
        List<(Guid Id, DateTimeOffset At)> Messages, List<(DateTimeOffset Start, DateTimeOffset End)> Spans, decimal? Aiu);

    // Events of these types are parsed in full; for all others only the event time is read.
    private static readonly HashSet<string> Parsed = new(StringComparer.Ordinal)
    {
        "session.start", "session.usage_record", "session.usage_checkpoint", "session.shutdown", "assistant.message",
        "tool.execution_start", "tool.execution_complete", "user.message",
    };

    public static JsonObject Run(Database database, string root)
    {
        if (!Directory.Exists(root))
        {
            throw new KpiException(ExitCodes.Refused, $"{root} is not a directory");
        }

        var idle = TimeSpan.FromMinutes((double)database.Setting("idle_gap_minutes"));
        var sessions = new JsonArray();
        foreach (var directory in Directory.EnumerateDirectories(root).Order(StringComparer.Ordinal))
        {
            var file = Path.Combine(directory, "events.jsonl");
            if (!SessionId().IsMatch(Path.GetFileName(directory)) || !File.Exists(file))
            {
                continue;
            }

            using var reader = new StreamReader(file);
            if (Read(Path.GetFileName(directory), reader, idle, file) is { } session)
            {
                sessions.Add(database.Store(session));
            }
        }

        return new JsonObject { ["sessions"] = sessions };
    }

    public static Session? Read(string id, TextReader reader, TimeSpan idle, string source)
    {
        var calls = new List<ModelCall>();
        var cliCalls = new List<ModelCall>();
        var checkpoints = new List<(DateTimeOffset, decimal)>();
        var shutdowns = new List<(DateTimeOffset, decimal)>();
        var baseline = 0m;
        var started = new Dictionary<string, (string Tool, DateTimeOffset At)>(StringComparer.Ordinal);
        var tools = new List<ToolCall>();
        var messages = new List<(Guid, DateTimeOffset)>();
        var times = new List<DateTimeOffset>();
        DateTimeOffset? floor = null;
        string? line;
        var number = 0;
        while ((line = reader.ReadLine()) is not null)
        {
            number++;
            if (line.Length == 0)
            {
                continue;
            }

            try
            {
                Line(line);
            }
            catch (Exception error) when (error is JsonException or FormatException or KeyNotFoundException or InvalidOperationException)
            {
                throw new KpiException(ExitCodes.Refused, $"{source}: line {number} is not a readable event: {error.Message}");
            }
        }

        void Line(string line)
        {
            // The logs write each event's type first. Agent sessions bill each call in a usage record, so once one is read
            // their many assistant messages need only their time. A line written another way is parsed in full.
            var kind = Kind().Match(line);
            var parse = !kind.Success
                || (Parsed.Contains(kind.Groups[1].Value) && !(calls.Count > 0 && kind.Groups[1].Value == "assistant.message"));
            if (!parse && FastTime(line) is { } fast)
            {
                if (!(fast < floor))
                {
                    times.Add(fast);
                }

                return;
            }

            using var document = JsonDocument.Parse(line);
            var root = document.RootElement;
            var at = root.GetProperty("timestamp").GetDateTimeOffset();
            var type = root.GetProperty("type").GetString();
            var data = root.GetProperty("data");
            if (type == "session.start" && floor is null)
            {
                floor = at;
            }

            if (at < floor)
            {
                // The fork's own checkpoints continue from the parent's running AIU at the fork: its last copied
                // checkpoint plus the copied calls after it.
                if (type == "session.usage_checkpoint" && Nano(data) is { } copied)
                {
                    baseline = copied;
                }
                else if (type == "session.usage_record" && Exact(data.GetProperty("usage")) is { } spent)
                {
                    baseline += spent;
                }

                return;
            }

            times.Add(at);
            switch (type)
            {
                case "session.usage_record":
                    calls.Add(Call(root.GetProperty("id").GetGuid(), at, data.GetProperty("usage")));
                    break;
                case "assistant.message":
                    cliCalls.Add(CliCall(root.GetProperty("id").GetGuid(), at, data));
                    break;
                case "session.usage_checkpoint" when Nano(data) is { } total:
                    checkpoints.Add((at, total));
                    break;
                case "session.shutdown" when Nano(data) is { } total:
                    shutdowns.Add((at, total));
                    break;
                case "tool.execution_start":
                    started[data.GetProperty("toolCallId").GetString()!] = (data.GetProperty("toolName").GetString()!, at);
                    break;
                case "tool.execution_complete":
                    var callId = data.GetProperty("toolCallId").GetString()!;
                    if (started.Remove(callId, out var start))
                    {
                        tools.Add(new(callId, start.Tool, start.At, at, data.GetProperty("success").GetBoolean()));
                    }

                    break;
                case "user.message":
                    messages.Add((root.GetProperty("id").GetGuid(), at));
                    break;
            }
        }

        tools.AddRange(started.Select(pair => new ToolCall(pair.Key, pair.Value.Tool, pair.Value.At, null, null)));
        if (times.Count == 0)
        {
            return null;
        }

        times.Sort();
        var spans = new List<(DateTimeOffset, DateTimeOffset)>();
        var (first, last) = (times[0], times[0]);
        foreach (var time in times.Skip(1))
        {
            if (time - last > idle)
            {
                spans.Add((first, last));
                first = time;
            }

            last = time;
        }

        spans.Add((first, last));
        var (measured, aiu) = Allocate(calls.Count > 0 ? calls : cliCalls, checkpoints, shutdowns, baseline);
        return new Session(id, times[0], times[^1], times.Count, measured, tools, messages, spans, aiu);
    }

    // Gives each measured interval's AIU to the calls in it: a call with an exact value keeps it, and the rest of the
    // interval is shared among the calls without one by weight (equally when they have none). The intervals are the
    // deltas of the cumulative checkpoints from the baseline (a decrease starts a new counter), or else each process's
    // shutdown total. The measured total adds the exact values of the calls after the last interval.
    public static (List<ModelCall> Calls, decimal? Aiu) Allocate(
        List<ModelCall> calls, List<(DateTimeOffset At, decimal Total)> checkpoints, List<(DateTimeOffset At, decimal Total)> shutdowns, decimal baseline)
    {
        var intervals = new List<(DateTimeOffset End, decimal Aiu)>();
        var previous = baseline;
        foreach (var (at, total) in checkpoints.OrderBy(checkpoint => checkpoint.At))
        {
            intervals.Add((at, total >= previous ? total - previous : total));
            previous = total;
        }

        if (intervals.Count == 0)
        {
            intervals.AddRange(shutdowns.OrderBy(shutdown => shutdown.At));
        }

        var ordered = calls.OrderBy(call => call.At).ToList();
        var shared = new List<ModelCall>(ordered.Count);
        var next = 0;
        foreach (var (end, aiu) in intervals)
        {
            var members = new List<ModelCall>();
            while (next < ordered.Count && ordered[next].At <= end)
            {
                members.Add(ordered[next++]);
            }

            var open = members.Where(call => call.Aiu is null).ToList();
            var rest = Math.Max(0, aiu - members.Sum(call => call.Aiu ?? 0));
            var weight = open.Sum(call => call.Weight);
            shared.AddRange(members.Select(call => call.Aiu is not null ? call : call with
            {
                Aiu = Math.Round(weight > 0 ? rest * call.Weight / weight : rest / open.Count, 6),
            }));
        }

        var tail = ordered.Skip(next).ToList();
        shared.AddRange(tail);
        var exact = calls.Any(call => call.Aiu is not null);
        return intervals.Count == 0 && !exact ? (shared, null) : (shared, intervals.Sum(interval => interval.Aiu) + tail.Sum(call => call.Aiu ?? 0));
    }

    // What a model call asked for, by the tools it requested.
    public static string Category(IReadOnlySet<string> tools) =>
        tools switch
        {
            { Count: 0 } => "respond",
            _ when tools.Contains("ask_user") => "ask",
            _ when tools.Overlaps(["edit", "create"]) => "edit",
            _ when tools.Overlaps(["task", "write_agent"]) => "delegate",
            _ when tools.Contains("bash") => "run",
            _ when tools.Overlaps(["store_memory", "vote_memory"]) => "memory",
            _ => "explore",
        };

    // An agent session's call, from its usage record, with its exact AIU when the record carries them. Its weight, which
    // only shares an interval's AIU among calls without an exact value, prices its tokens in the ratios Copilot's models share:
    // uncached input 1, cache writes 1.25, cache reads 0.1, output 5. The input count includes the cached tokens.
    private static ModelCall Call(Guid id, DateTimeOffset at, JsonElement usage)
    {
        var (input, output, read, write) = (Long(usage, "inputTokens"), Long(usage, "outputTokens"), Long(usage, "cacheReadTokens"), Long(usage, "cacheWriteTokens"));
        var tools = usage.TryGetProperty("toolCounts", out var counts) && counts.ValueKind == JsonValueKind.Object
            ? counts.EnumerateObject().Select(property => property.Name).ToHashSet(StringComparer.Ordinal)
            : [];
        return new(
            id, at, Text(usage, "initiator") ?? "unknown", Text(usage, "model") ?? "unknown",
            usage.TryGetProperty("cost", out var cost) && cost.ValueKind == JsonValueKind.Number ? cost.GetDecimal() : 0,
            input, output, read, Category(tools), Math.Max(0, input - read - write) + (1.25m * write) + (0.1m * read) + (5m * output), Exact(usage));
    }

    // A usage record's exact billed AIU.
    private static decimal? Exact(JsonElement usage) =>
        usage.TryGetProperty("copilotUsage", out var billed) && billed.ValueKind == JsonValueKind.Object ? Nano(billed) : null;

    // A CLI session's call, from its assistant message, which gives only the output tokens: they are its weight. A
    // sub-agent's messages name the tool call that started it.
    private static ModelCall CliCall(Guid id, DateTimeOffset at, JsonElement message)
    {
        var tools = message.TryGetProperty("toolRequests", out var requests) && requests.ValueKind == JsonValueKind.Array
            ? requests.EnumerateArray().Select(request => Text(request, "name")).OfType<string>().ToHashSet(StringComparer.Ordinal)
            : [];
        var output = Long(message, "outputTokens");
        return new(
            id, at, message.TryGetProperty("parentToolCallId", out _) ? "sub-agent" : "agent", Text(message, "model") ?? "unknown",
            0, 0, output, 0, Category(tools), output);
    }

    private static decimal? Nano(JsonElement data) =>
        data.TryGetProperty("totalNanoAiu", out var total) && total.ValueKind == JsonValueKind.Number ? total.GetDecimal() / 1_000_000_000m : null;

    private static long Long(JsonElement element, string name) =>
        element.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.Number ? value.GetInt64() : 0;

    private static string? Text(JsonElement element, string name) =>
        element.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.String ? value.GetString() : null;

    // The event time of a line not parsed in full. The root's timestamp follows its data, near the end of the line; a line
    // written another way is parsed in full.
    private static DateTimeOffset? FastTime(string line)
    {
        var match = Timestamp().Match(line, Math.Max(0, line.Length - 200));
        return match.Success ? DateTimeOffset.Parse(match.Groups[1].Value, CultureInfo.InvariantCulture, DateTimeStyles.RoundtripKind) : null;
    }

    [GeneratedRegex("\"timestamp\":\"([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+Z)\"")]
    private static partial Regex Timestamp();

    [GeneratedRegex("^\\{\"type\":\"([A-Za-z0-9_.]+)\"")]
    private static partial Regex Kind();

    [GeneratedRegex("^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")]
    private static partial Regex SessionId();
}
