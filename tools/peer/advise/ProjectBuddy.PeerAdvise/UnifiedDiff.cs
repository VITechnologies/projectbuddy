using System.Text;
using System.Text.RegularExpressions;

namespace ProjectBuddy.PeerAdvise;

public enum DiffLineKind
{
    Context,
    Added,
    Removed,
}

// NewLine is the line number in the new file; removed lines have none.
public sealed record DiffLine(DiffLineKind Kind, int? NewLine, string Text);

public sealed record DiffHunk(string Header, IReadOnlyList<DiffLine> Lines);

public sealed record FileDiff(string Path, IReadOnlyList<DiffHunk> Hunks)
{
    public IEnumerable<DiffLine> Added => Hunks.SelectMany(hunk => hunk.Lines).Where(line => line.Kind == DiffLineKind.Added);
}

// Reads `git diff` output. Only text changes to files that still exist are reviewable: deletions and binary
// changes carry no added lines a finding could be anchored to.
public static partial class UnifiedDiff
{
    public const string FileHeading = "=== file: ";

    public static IReadOnlyList<FileDiff> Parse(string text)
    {
        var files = new List<FileDiff>();
        string? path = null;
        List<DiffHunk>? hunks = null;
        List<DiffLine>? lines = null;
        var header = "";
        var newLine = 0;

        void CloseHunk()
        {
            if (lines is { Count: > 0 })
            {
                hunks!.Add(new DiffHunk(header, lines));
            }

            lines = null;
        }

        void CloseFile()
        {
            CloseHunk();
            if (path is not null && hunks!.Any(hunk => hunk.Lines.Any(line => line.Kind == DiffLineKind.Added)))
            {
                files.Add(new FileDiff(path, hunks!));
            }

            path = null;
            hunks = null;
        }

        foreach (var raw in text.Replace("\r\n", "\n").TrimEnd('\n').Split('\n'))
        {
            if (raw.StartsWith("diff --git ", StringComparison.Ordinal))
            {
                CloseFile();
                hunks = [];
                continue;
            }

            if (hunks is null)
            {
                continue;
            }

            if (lines is null && raw.StartsWith("+++ ", StringComparison.Ordinal))
            {
                var target = raw[4..].Trim();
                path = target == "/dev/null" ? null : target.StartsWith("b/", StringComparison.Ordinal) ? target[2..] : target;
                continue;
            }

            var hunk = HunkHeader().Match(raw);
            if (hunk.Success)
            {
                CloseHunk();
                header = raw;
                newLine = int.Parse(hunk.Groups["start"].Value);
                lines = [];
                continue;
            }

            if (lines is null)
            {
                continue;
            }

            // Editors and mail clients strip the single space of an empty context line; it still takes a line number.
            switch (raw.Length == 0 ? ' ' : raw[0])
            {
                case '+':
                    lines.Add(new DiffLine(DiffLineKind.Added, newLine++, raw[1..]));
                    break;
                case ' ':
                    lines.Add(new DiffLine(DiffLineKind.Context, newLine++, raw.Length == 0 ? "" : raw[1..]));
                    break;
                case '-':
                    lines.Add(new DiffLine(DiffLineKind.Removed, null, raw[1..]));
                    break;
            }
        }

        CloseFile();
        return files;
    }

    // What the model sees for one file: every line numbered in the new file so a finding can cite it exactly.
    public static string Render(string path, IEnumerable<DiffHunk> hunks)
    {
        var text = new StringBuilder().Append(FileHeading).Append(path).Append('\n');
        foreach (var hunk in hunks)
        {
            text.Append(hunk.Header).Append('\n');
            foreach (var line in hunk.Lines)
            {
                var marker = line.Kind switch
                {
                    DiffLineKind.Added => '+',
                    DiffLineKind.Removed => '-',
                    _ => ' ',
                };
                text.Append(marker).Append(line.NewLine?.ToString().PadLeft(5) ?? "     ").Append("| ").Append(line.Text).Append('\n');
            }
        }

        return text.ToString();
    }

    [GeneratedRegex(@"^@@ -\d+(,\d+)? \+(?<start>\d+)(,\d+)? @@")]
    private static partial Regex HunkHeader();
}
