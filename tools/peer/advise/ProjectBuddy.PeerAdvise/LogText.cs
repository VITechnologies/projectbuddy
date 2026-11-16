using System.Text;
using System.Text.RegularExpressions;

namespace ProjectBuddy.PeerAdvise;

// Lines are the log-derived lines in Text: never the tool's own headings.
public sealed record LogTail(string Text, int Bytes, int DroppedLines, IReadOnlyList<string> Lines);

public sealed record Evidence(string Text, int Bytes, int SignalLines, int DroppedLines, IReadOnlyList<string> Lines);

// Shrinks a CI log to the evidence a model needs: no ANSI codes, no per-line timestamps, one header per
// GitHub job/step instead of a prefix on every line, and only the newest lines that fit the byte budget.
public static partial class LogText
{
    public const int MaxLineChars = 2000;
    public const string ClipMarker = " [...]";
    public const int MaxSignalLines = 40;
    public const string SignalHeading = "# Lines that look like errors, from the whole log:";
    public const string TailHeading = "# End of the log:";

    public static string Normalize(string raw)
    {
        var output = new List<string>();
        string? section = null;
        foreach (var source in raw.Replace("\r\n", "\n").Replace('\r', '\n').Split('\n'))
        {
            var line = Ansi().Replace(source, "");
            var row = GitHubRow().Match(line);
            if (row.Success)
            {
                var header = $"== {row.Groups["job"].Value} / {row.Groups["step"].Value} ==";
                if (header != section)
                {
                    output.Add(header);
                    section = header;
                }

                line = row.Groups["text"].Value;
            }
            else
            {
                line = Stamp().Replace(line, "");
            }

            line = line.TrimEnd();
            if (line.Length > MaxLineChars)
            {
                line = line[..MaxLineChars] + ClipMarker;
            }

            if (line.Length == 0 && (output.Count == 0 || output[^1].Length == 0))
            {
                continue;
            }

            output.Add(line);
        }

        // A section with no content (an empty last step, say) would give the model a header and nothing to read;
        // then drop blank lines left in front of a header or another blank line.
        var kept = output.Where((line, index) => !IsHeader(line) || output.Skip(index + 1).TakeWhile(next => !IsHeader(next)).Any(next => next.Length > 0)).ToList();
        var tidy = kept.Where((line, index) => line.Length > 0 || (index + 1 < kept.Count && kept[index + 1].Length > 0 && !IsHeader(kept[index + 1])));
        return string.Join('\n', tidy).Trim('\n');
    }

    // Errors sit at the end of a log, so keep the newest whole lines that fit, counting the header of the
    // section the kept text starts in, so the model still sees which job and step it is reading. A header
    // is never returned without at least one line of its content.
    public static LogTail Tail(string normalized, int maxBytes)
    {
        var lines = normalized.Split('\n');
        var headerOf = new string?[lines.Length];
        string? current = null;
        for (var index = 0; index < lines.Length; index++)
        {
            current = IsHeader(lines[index]) ? lines[index] : current;
            headerOf[index] = current;
        }

        var start = lines.Length;
        var bytes = -1;
        for (var index = lines.Length - 1; index >= 0; index--)
        {
            bytes += Encoding.UTF8.GetByteCount(lines[index]) + 1;
            if (bytes > maxBytes)
            {
                break;
            }

            var header = IsHeader(lines[index]) ? null : headerOf[index];
            if (bytes + (header is null ? 0 : Encoding.UTF8.GetByteCount(header) + 1) <= maxBytes)
            {
                start = index;
            }
        }

        if (start == lines.Length || lines[start..].All(IsHeader))
        {
            return new LogTail("", 0, lines.Length, []);
        }

        var kept = IsHeader(lines[start]) || headerOf[start] is null ? lines[start..] : [headerOf[start]!, .. lines[start..]];
        var text = string.Join('\n', kept);
        return new LogTail(text, Encoding.UTF8.GetByteCount(text), start, kept);
    }

    // A failure buried among hundreds of passing lines, or before the tail, still reaches the model: the
    // newest distinct error-looking lines from the whole log go first, within a third of the budget.
    public static Evidence Build(string normalized, int maxBytes)
    {
        var signals = new List<string>();
        var signalBytes = Encoding.UTF8.GetByteCount(SignalHeading) + 1;
        foreach (var line in normalized.Split('\n').Reverse().Where(line => !IsHeader(line) && Signal().IsMatch(line)).Distinct(StringComparer.Ordinal))
        {
            var size = Encoding.UTF8.GetByteCount(line) + 1;
            if (signals.Count == MaxSignalLines || signalBytes + size > maxBytes / 3)
            {
                break;
            }

            signals.Insert(0, line);
            signalBytes += size;
        }

        var prefix = signals.Count == 0 ? "" : SignalHeading + "\n" + string.Join('\n', signals) + "\n" + TailHeading + "\n";
        var tail = Tail(normalized, maxBytes - Encoding.UTF8.GetByteCount(prefix));
        if (tail.Text.Length == 0)
        {
            return new Evidence("", 0, 0, tail.DroppedLines, []);
        }

        var text = prefix + tail.Text;
        return new Evidence(text, Encoding.UTF8.GetByteCount(text), signals.Count, tail.DroppedLines, [.. signals, .. tail.Lines]);
    }

    public static bool IsHeader(string line) => line.StartsWith("== ", StringComparison.Ordinal) && line.EndsWith(" ==", StringComparison.Ordinal);

    [GeneratedRegex(@"\b(FAIL(ED|URE)?|[Ee]rror|ERROR|[Ff]atal|FATAL|[Ee]xception|[Pp]anic|[Ss]egmentation fault|Killed|[Tt]imed out|[Dd]enied|[Rr]efused|[Mm]ismatch|exit(ed)?( with)?( code)?:? ?[1-9][0-9]*)\b")]
    private static partial Regex Signal();

    [GeneratedRegex(@"\x1B\[[0-9;?]*[ -/]*[@-~]")]
    private static partial Regex Ansi();

    // `gh run view --log` rows: job, step and a timestamp before the text.
    [GeneratedRegex(@"^(?<job>[^\t]+)\t(?<step>[^\t]+)\t\uFEFF?\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z ?(?<text>.*)$")]
    private static partial Regex GitHubRow();

    [GeneratedRegex(@"^\uFEFF?\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z ?")]
    private static partial Regex Stamp();
}
