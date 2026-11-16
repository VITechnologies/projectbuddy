using System.Globalization;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace ProjectBuddy.Kpi;

// Reads the agent's session event logs (<sessions>/<session id>/events.jsonl): billed model calls, tool calls, merge
// attempts, operator messages, and the session's active spans. A forked session's log begins with its own session.start
// and then repeats its parent's history; events dated before that start belong to the parent and are skipped.
internal static partial class Ingest
{
    public sealed record ModelCall(Guid EventId, DateTimeOffset At, string Initiator, string Model, decimal Cost, long InputTokens, long OutputTokens, long CacheReadTokens, string Category);

    public sealed record ToolCall(string Id, string Tool, DateTimeOffset Started, DateTimeOffset? Completed, bool? Success);

    // A command that tried to merge a change. Whether, and when, the change merged is the forge's answer (see Forge).
    public sealed record MergeAttempt(string ToolCallId, string ChangeRef, DateTimeOffset Started, DateTimeOffset Completed);

    public sealed record Session(
        string Id, DateTimeOffset First, DateTimeOffset Last, long Events, List<ModelCall> Calls, List<ToolCall> Tools, List<MergeAttempt> Merges,
        int UnattributedMerges, List<(Guid Id, DateTimeOffset At)> Messages, List<(DateTimeOffset Start, DateTimeOffset End)> Spans);

    // A session's working directory and the repository checked out there, from its session.start.
    public sealed record Context(string? Cwd, string? GitRoot, string? Repository);

    // Lines of these types are parsed in full; for all others only the event time is read.
    private static readonly string[] Parsed = ["\"session.start\"", "\"session.usage_record\"", "\"tool.execution_start\"", "\"tool.execution_complete\"", "\"user.message\""];

    public static JsonObject Run(Database database, string root)
    {
        if (!Directory.Exists(root))
        {
            throw new KpiException(ExitCodes.Refused, $"{root} is not a directory");
        }

        var idle = TimeSpan.FromMinutes((double)database.Setting("idle_gap_minutes"));
        var files = Directory.EnumerateDirectories(root).Order(StringComparer.Ordinal)
            .Where(directory => SessionId().IsMatch(Path.GetFileName(directory)))
            .Select(directory => Path.Combine(directory, "events.jsonl")).Where(File.Exists).ToList();
        var workspaces = Workspaces.From(files.Select(file => StartContext(File.ReadLines(file).FirstOrDefault())),
            Environment.GetFolderPath(Environment.SpecialFolder.UserProfile));
        var sessions = new JsonArray();
        foreach (var file in files)
        {
            using var reader = new StreamReader(file);
            if (Read(Path.GetFileName(Path.GetDirectoryName(file))!, reader, idle, file, workspaces) is { } session)
            {
                sessions.Add(database.Store(session));
            }
        }

        return new JsonObject { ["sessions"] = sessions };
    }

    public static Session? Read(string id, TextReader reader, TimeSpan idle, string source, Workspaces workspaces)
    {
        var calls = new List<ModelCall>();
        var started = new Dictionary<string, (string Tool, DateTimeOffset At, string? Command)>(StringComparer.Ordinal);
        var tools = new List<ToolCall>();
        var merges = new List<MergeAttempt>();
        var unattributed = 0;
        var messages = new List<(Guid, DateTimeOffset)>();
        var times = new List<DateTimeOffset>();
        DateTimeOffset? floor = null;
        Context? context = null;
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
                (floor, context) = (at, ContextOf(data));
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
                    var tool = data.GetProperty("toolName").GetString()!;
                    started[data.GetProperty("toolCallId").GetString()!] = (tool, at, tool == "bash" ? Text(data, "arguments", "command") : null);
                    break;
                case "tool.execution_complete":
                    var callId = data.GetProperty("toolCallId").GetString()!;
                    if (started.Remove(callId, out var start))
                    {
                        tools.Add(new(callId, start.Tool, start.At, at, data.GetProperty("success").GetBoolean()));
                        if (start.Command is { } command)
                        {
                            var (changes, unknown) = Changes(command, directory => workspaces.Repository(directory, context));
                            merges.AddRange(changes.Select(change => new MergeAttempt(callId, change, start.At, at)));
                            unattributed += unknown;
                        }
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
        return new Session(id, times[0], times[^1], times.Count, calls, tools, merges, unattributed, messages, spans);
    }

    // The changes a command tried to merge: `gh pr merge <n>` and `glab mr merge <n>`, in the repository named by -R or
    // --repo, else in the one checked out where the command ran (the last `cd` before it, or the session's own directory).
    // A merge whose repository cannot be told is counted, not attributed; text without a number (prose) is not a merge.
    public static (List<string> Changes, int Unattributed) Changes(string command, Func<string?, string?> repositoryIn)
    {
        var changes = new List<string>();
        var unattributed = 0;
        foreach (Match merge in Merge().Matches(command))
        {
            var arguments = merge.Groups["args"].Value;
            var number = Number().Match(arguments);
            if (!number.Success)
            {
                continue;
            }

            var github = merge.Groups["tool"].Value == "gh pr";
            var option = RepositoryOption().Match(arguments);
            var repository = option.Success
                ? option.Groups["repo"].Value.Trim('"', '\'')
                : repositoryIn(Cd().Matches(command[..merge.Index]).LastOrDefault()?.Groups["dir"].Value);
            if (repository is not null && (github ? GitHubRepository() : GitLabRepository()).IsMatch(repository))
            {
                changes.Add($"{repository}{(github ? '#' : '!')}{number.Groups[1].Value}");
            }
            else
            {
                unattributed++;
            }
        }

        return (changes, unattributed);
    }

    public static Context? StartContext(string? line)
    {
        if (line is null || !line.Contains("\"session.start\"", StringComparison.Ordinal))
        {
            return null;
        }

        try
        {
            using var document = JsonDocument.Parse(line);
            var root = document.RootElement;
            return Text(root, "type") == "session.start" && root.TryGetProperty("data", out var data) ? ContextOf(data) : null;
        }
        catch (JsonException)
        {
            return null;
        }
    }

    private static Context ContextOf(JsonElement data) =>
        new(Text(data, "context", "cwd"), Text(data, "context", "gitRoot"), Text(data, "context", "repository"));

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

    private static string? Text(JsonElement element, params string[] path)
    {
        foreach (var name in path)
        {
            if (element.ValueKind != JsonValueKind.Object || !element.TryGetProperty(name, out element))
            {
                return null;
            }
        }

        return element.ValueKind == JsonValueKind.String ? element.GetString() : null;
    }

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

    [GeneratedRegex(@"\b(?<tool>gh pr|glab mr) merge\b(?<args>[^\n;&|]*)")]
    private static partial Regex Merge();

    [GeneratedRegex(@"(?:^|\s)([0-9]+)(?=\s|$)")]
    private static partial Regex Number();

    [GeneratedRegex(@"(?:^|\s)(?:-R|--repo)[ =](?<repo>\S+)")]
    private static partial Regex RepositoryOption();

    [GeneratedRegex(@"(?:^|[\s;&|(])cd\s+(?<dir>""[^""]*""|'[^']*'|[^\s;&|)]+)")]
    private static partial Regex Cd();

    [GeneratedRegex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")]
    private static partial Regex GitHubRepository();

    [GeneratedRegex("^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+$")]
    private static partial Regex GitLabRepository();
}
