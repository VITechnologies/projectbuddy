using System.Text;
using System.Text.RegularExpressions;
using ProjectBuddy.PeerOllama;

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
    public const int MaxLineNumber = 100_000_000;

    // git's default new-side prefix (b/; a/ under -R) and its mnemonic prefixes (w/ work tree, i/ index, c/ commit, o/ object).
    private static readonly string[] Prefixes = ["b/", "a/", "w/", "i/", "c/", "o/"];

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

        // A byte-order mark (Windows PowerShell's UTF-8 files) would otherwise hide the first "diff --git" line.
        foreach (var raw in text.TrimStart('\uFEFF').Replace("\r\n", "\n").TrimEnd('\n').Split('\n'))
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
                var target = Unquote(raw[4..].Trim());
                var prefix = Prefixes.FirstOrDefault(candidate => target.StartsWith(candidate, StringComparison.Ordinal));
                path = target == "/dev/null" ? null : prefix is null ? target : target[prefix.Length..];
                continue;
            }

            var hunk = HunkHeader().Match(raw);
            if (hunk.Success)
            {
                CloseHunk();
                header = raw;
                if (!int.TryParse(hunk.Groups["start"].Value, out newLine) || newLine > MaxLineNumber)
                {
                    throw new PeerException(ExitCodes.Refused, $"hunk header is not usable: {raw}");
                }

                lines = [];
                continue;
            }

            // Any other "@@" line is a damaged hunk header; skipping it would number the next lines as part of
            // the previous hunk and anchor findings to the wrong code.
            if (raw.StartsWith("@@", StringComparison.Ordinal))
            {
                throw new PeerException(ExitCodes.Refused, $"hunk header is not usable: {raw}");
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

    // git C-quotes paths with special or non-ASCII characters: "b/t\303\253st.cs" is b/tëst.cs.
    internal static string Unquote(string target)
    {
        if (target.Length < 2 || target[0] != '"' || target[^1] != '"')
        {
            return target;
        }

        var bytes = new List<byte>();
        var plain = new StringBuilder();
        for (var index = 1; index < target.Length - 1; index++)
        {
            var character = target[index];
            if (character != '\\')
            {
                plain.Append(character);
                continue;
            }

            // Plain runs are encoded whole, so a surrogate pair (an emoji left unescaped) stays one character.
            bytes.AddRange(Encoding.UTF8.GetBytes(plain.ToString()));
            plain.Clear();

            var next = index + 1 < target.Length - 1 ? target[++index] : '\0';
            if (next is >= '0' and <= '3' && index + 2 < target.Length - 1 && IsOctal(target[index + 1]) && IsOctal(target[index + 2]))
            {
                bytes.Add((byte)(((next - '0') << 6) | ((target[index + 1] - '0') << 3) | (target[index + 2] - '0')));
                index += 2;
                continue;
            }

            bytes.Add(next switch
            {
                'n' => (byte)'\n',
                't' => (byte)'\t',
                'r' => (byte)'\r',
                'a' => 7,
                'b' => 8,
                'f' => 12,
                'v' => 11,
                '"' => (byte)'"',
                '\\' => (byte)'\\',
                _ => throw new PeerException(ExitCodes.Refused, $"path is not valid C-quoted text: {target}"),
            });
        }

        bytes.AddRange(Encoding.UTF8.GetBytes(plain.ToString()));
        try
        {
            return new UTF8Encoding(false, true).GetString(bytes.ToArray());
        }
        catch (DecoderFallbackException)
        {
            // A legacy (non-UTF-8) file name keeps git's quoted spelling; it labels findings but never anchors them.
            return target;
        }
    }

    private static bool IsOctal(char character) => character is >= '0' and <= '7';

    [GeneratedRegex(@"^@@ -[0-9]+(,[0-9]+)? \+(?<start>[0-9]+)(,[0-9]+)? @@")]
    private static partial Regex HunkHeader();
}
