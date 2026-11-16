using System.Diagnostics;
using System.Text;

namespace ProjectBuddy.Wiki;

public sealed record WikiFile(string Path, byte[] Content);

// Reads a wiki clone's committed bytes through git's object store (never the working tree, which line-ending settings
// or local edits can change). Only regular, non-executable files are accepted: a symbolic link, an executable or a
// submodule has no byte-for-byte meaning as a wiki page.
internal sealed class Git(string executable, TimeSpan? timeout = null)
{
    public static readonly TimeSpan DefaultTimeout = TimeSpan.FromSeconds(120);

    private static readonly UTF8Encoding StrictUtf8 = new(encoderShouldEmitUTF8Identifier: false, throwOnInvalidBytes: true);

    public static bool IsCommit(string text) => text.Length is 40 or 64 && text.All(char.IsAsciiHexDigitLower);

    // The commit HEAD names, or null when the directory is not a git work tree with a commit.
    public string? Head(string repository)
    {
        var (exit, output) = Run(repository, ["rev-parse", "--verify", "--quiet", "HEAD^{commit}"], null);
        var commit = Encoding.UTF8.GetString(output).Trim();
        return exit == 0 && IsCommit(commit) ? commit : null;
    }

    // Whether the directory is the top of a work tree: not a subdirectory, not inside a git directory. Git searches
    // upward, so Head alone does not prove this. Git answers directly, so a path through a symbolic link still matches.
    public bool IsTopLevel(string directory)
    {
        var (exit, output) = Run(directory, ["rev-parse", "--is-inside-work-tree", "--show-cdup"], null);
        return exit == 0 && output.AsSpan().SequenceEqual("true\n\n"u8);
    }

    public IReadOnlyList<WikiFile> Tree(string repository, string commit)
    {
        var (exit, listing) = Run(repository, ["ls-tree", "-r", "-z", "--full-tree", commit], null);
        if (exit != 0)
        {
            throw new WikiException(ExitCodes.Refused, $"git ls-tree failed on {commit}");
        }

        string text;
        try
        {
            text = StrictUtf8.GetString(listing);
        }
        catch (DecoderFallbackException)
        {
            // A lossy decode would store the file under another name, and check would not notice.
            throw new WikiException(ExitCodes.Refused, $"a path in {commit} is not valid UTF-8");
        }

        var entries = new List<(string Path, string Hash)>();
        foreach (var entry in text.Split('\0', StringSplitOptions.RemoveEmptyEntries))
        {
            // "<mode> <type> <hash>\t<path>"
            var tab = entry.IndexOf('\t');
            var fields = entry[..tab].Split(' ');
            if (fields is not ["100644", "blob", var hash])
            {
                throw new WikiException(ExitCodes.Refused, $"only regular files are supported, not {fields[0]} {fields[1]} at {entry[(tab + 1)..]}");
            }

            entries.Add((entry[(tab + 1)..], hash));
        }

        if (entries.Count == 0)
        {
            throw new WikiException(ExitCodes.Refused, $"commit {commit} has no files");
        }

        var request = Encoding.ASCII.GetBytes(string.Concat(entries.Select(entry => entry.Hash + "\n")));
        var (batchExit, batch) = Run(repository, ["cat-file", "--batch"], request);
        if (batchExit != 0)
        {
            throw new WikiException(ExitCodes.Refused, "git cat-file failed");
        }

        // "<hash> blob <size>\n<content>\n" per object, in request order.
        var files = new List<WikiFile>(entries.Count);
        var position = 0;
        foreach (var (path, hash) in entries)
        {
            var end = Array.IndexOf(batch, (byte)'\n', position);
            var header = Encoding.ASCII.GetString(batch, position, end - position).Split(' ');
            var size = int.Parse(header[2]);
            files.Add(new WikiFile(path, batch.AsSpan(end + 1, size).ToArray()));
            position = end + 1 + size + 1;
        }

        return files;
    }

    private (int Exit, byte[] Output) Run(string repository, IReadOnlyList<string> arguments, byte[]? input)
    {
        var start = new ProcessStartInfo(executable)
        {
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardInput = input is not null,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
        };
        start.ArgumentList.Add("-C");
        start.ArgumentList.Add(repository);
        foreach (var argument in arguments)
        {
            start.ArgumentList.Add(argument);
        }

        // An inherited GIT_DIR, GIT_WORK_TREE or similar would make git answer for another repository than the directory.
        foreach (var name in start.Environment.Keys.Where(name => name.StartsWith("GIT_", StringComparison.Ordinal)).ToList())
        {
            start.Environment.Remove(name);
        }

        start.Environment["GIT_CONFIG_NOSYSTEM"] = "1";
        start.Environment["GIT_TERMINAL_PROMPT"] = "0";
        Process process;
        try
        {
            process = Process.Start(start)!;
        }
        catch (System.ComponentModel.Win32Exception)
        {
            throw new WikiException(ExitCodes.Refused, $"cannot start {executable}");
        }

        using var started = process;
        _ = process.StandardError.ReadToEndAsync();

        // Input is written while output is read, so neither pipe can fill and stall the other.
        var writer = input is null ? Task.CompletedTask : Task.Run(() =>
        {
            try
            {
                process.StandardInput.BaseStream.Write(input);
                process.StandardInput.Close();
            }
            catch (IOException)
            {
                // git stopped reading (it exited early); its exit code reports why.
            }
        });
        using var output = new MemoryStream();
        var reader = process.StandardOutput.BaseStream.CopyToAsync(output);
        if (!process.WaitForExit(timeout ?? DefaultTimeout))
        {
            process.Kill(entireProcessTree: true);
            throw new WikiException(ExitCodes.Refused, $"git {arguments[0]} timed out");
        }

        Task.WaitAll(writer, reader);
        return (process.ExitCode, output.ToArray());
    }
}
