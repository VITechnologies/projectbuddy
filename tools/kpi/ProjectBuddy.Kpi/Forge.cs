using System.ComponentModel;
using System.Diagnostics;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace ProjectBuddy.Kpi;

// Asks the forge whether each change the sessions tried to merge was merged, and when: `gh api` for GitHub pull requests
// (owner/repo#n) and `glab api` for GitLab merge requests (group/project!n). A command's output does not say whether it
// merged (gh prints its confirmation only to a terminal, and a blocked merge mentions "merged"), so the forge decides.
// Merged changes are not asked again; an answer that cannot be read is reported and leaves the change unresolved.
internal static class Forge
{
    public static JsonObject Resolve(Database database, string gh, string glab)
    {
        var (asked, merged) = (0, 0);
        var unresolved = new JsonArray();
        foreach (var change in database.UnmergedChanges())
        {
            var github = change.Contains('#', StringComparison.Ordinal);
            var split = change.LastIndexOf(github ? '#' : '!');
            var (repository, number) = (change[..split], change[(split + 1)..]);
            var (tool, path) = github
                ? (gh, $"repos/{repository}/pulls/{number}")
                : (glab, $"projects/{Uri.EscapeDataString(repository)}/merge_requests/{number}");
            DateTimeOffset? mergedAt;
            try
            {
                var (exit, output, error) = Execute(tool, "api", path);
                if (exit != 0)
                {
                    unresolved.Add(Unresolved(change, $"{Path.GetFileName(tool)} exited {exit}: {error.Split('\n')[0].Trim()}"));
                    continue;
                }

                using var answer = JsonDocument.Parse(output);
                mergedAt = answer.RootElement.GetProperty("merged_at") is { ValueKind: JsonValueKind.Null } ? null
                    : answer.RootElement.GetProperty("merged_at").GetDateTimeOffset().ToUniversalTime();
            }
            catch (Win32Exception error)
            {
                unresolved.Add(Unresolved(change, $"cannot run {tool}: {error.Message}"));
                continue;
            }
            catch (Exception error) when (error is JsonException or KeyNotFoundException or InvalidOperationException or FormatException)
            {
                unresolved.Add(Unresolved(change, $"unexpected answer: {error.Message}"));
                continue;
            }

            database.RecordForge(change, mergedAt);
            asked++;
            merged += mergedAt is null ? 0 : 1;
        }

        return new JsonObject { ["asked"] = asked, ["merged"] = merged, ["unresolved"] = unresolved };
    }

    private static JsonObject Unresolved(string change, string reason) => new() { ["change"] = change, ["reason"] = reason };

    private static (int Exit, string Output, string Error) Execute(string file, params string[] arguments)
    {
        var start = new ProcessStartInfo(file) { RedirectStandardOutput = true, RedirectStandardError = true, UseShellExecute = false };
        foreach (var argument in arguments)
        {
            start.ArgumentList.Add(argument);
        }

        using var process = Process.Start(start)!;
        var output = process.StandardOutput.ReadToEndAsync();
        var error = process.StandardError.ReadToEndAsync();
        process.WaitForExit();
        return (process.ExitCode, output.Result, error.Result);
    }
}
