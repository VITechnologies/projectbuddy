using System.Globalization;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace ProjectBuddy.Kpi;

// Reads the agent's session event logs (<sessions>/<session id>/events.jsonl): billed model calls, tool calls, operator
// messages, and the session's active spans. Accepted changes come from the forge (see Forge). A forked session's log begins with its own session.start
// and then repeats its parent's history; events dated before that start belong to the parent and are skipped.
internal static partial class Ingest
{
    public sealed record ModelCall(Guid EventId, DateTimeOffset At, string Initiator, string Model, decimal Cost, long InputTokens, long OutputTokens, long CacheReadTokens, string Category);

    public sealed record ToolCall(string Id, string Tool, DateTimeOffset Started, DateTimeOffset? Completed, bool? Success);

    public sealed record Session(
        string Id, DateTimeOffset First, DateTimeOffset Last, long Events, List<ModelCall> Calls, List<ToolCall> Tools,
        List<(Guid Id, DateTimeOffset At)> Messages, List<(DateTimeOffset Start, DateTimeOffset End)> Spans);

    // Lines of these types are parsed in full; for all others only the event time is read.
    private static readonly string[] Parsed = ["\"session.start\"", "\"session.usage_record\"", "\"tool.execution_start\"", "\"tool.execution_complete\"", "\"user.message\""];

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
            if (!Parsed.Any(type => line.Contains(type, StringComparison.Ordinal)) && FastTime(line) is { } fast)
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
                return;
            }

            times.Add(at);
            switch (type)
            {
                case "session.usage_record":
                    calls.Add(Call(root.GetProperty("id").GetGuid(), at, data.GetProperty("usage")));
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
        return new Session(id, times[0], times[^1], times.Count, calls, tools, messages, spans);
    }

    // What a model call asked for, by the tools it requested.
    public static string Category(JsonElement usage)
    {
        var tools = usage.TryGetProperty("toolCounts", out var counts) && counts.ValueKind == JsonValueKind.Object
            ? counts.EnumerateObject().Select(property => property.Name).ToHashSet(StringComparer.Ordinal)
            : [];
        return tools switch
        {
            { Count: 0 } => "respond",
            _ when tools.Contains("ask_user") => "ask",
            _ when tools.Overlaps(["edit", "create"]) => "edit",
            _ when tools.Overlaps(["task", "write_agent"]) => "delegate",
            _ when tools.Contains("bash") => "run",
            _ when tools.Overlaps(["store_memory", "vote_memory"]) => "memory",
            _ => "explore",
        };
    }

    private static ModelCall Call(Guid id, DateTimeOffset at, JsonElement usage) => new(
        id, at, Text(usage, "initiator") ?? "unknown", Text(usage, "model") ?? "unknown",
        usage.TryGetProperty("cost", out var cost) && cost.ValueKind == JsonValueKind.Number ? cost.GetDecimal() : 0,
        Long(usage, "inputTokens"), Long(usage, "outputTokens"), Long(usage, "cacheReadTokens"), Category(usage));

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

    [GeneratedRegex("^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")]
    private static partial Regex SessionId();
}
