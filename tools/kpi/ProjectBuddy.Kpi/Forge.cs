using System.ComponentModel;
using System.Diagnostics;
using System.Globalization;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace ProjectBuddy.Kpi;

// Lists the requests each tracked repository merged since the first session: `gh api` for GitHub pull requests
// (owner/repo#n) and `glab api` for GitLab merge requests (group/project!n), newest update first, page by page until the
// updates are older than the first session. The forge's record is the only evidence of a merge: commands do not say
// (gh confirms only to a terminal, a blocked merge's advice says "merged"), and governed tools, helper scripts and
// auto-merge merge without a parseable command. A repository whose listing fails is reported and kept for the next run.
internal static class Forge
{
    public const int PageSize = 100;

    public static JsonObject Resolve(Database database, string gh, string glab)
    {
        var repositories = new JsonArray();
        var unresolved = new JsonArray();
        if (database.FirstSession() is not { } since)
        {
            return new JsonObject { ["since"] = null, ["repositories"] = repositories, ["unresolved"] = unresolved };
        }

        foreach (var (repository, forge) in database.Repositories())
        {
            var github = forge == "github";
            var tool = github ? gh : glab;
            var after = since.UtcDateTime.ToString("yyyy-MM-dd'T'HH:mm:ss'Z'", CultureInfo.InvariantCulture);
            var merged = 0;
            try
            {
                for (var page = 1; ; page++)
                {
                    var path = github
                        ? $"repos/{repository}/pulls?state=closed&sort=updated&direction=desc&per_page={PageSize}&page={page}"
                        : $"projects/{Uri.EscapeDataString(repository)}/merge_requests?state=merged&order_by=updated_at&sort=desc&updated_after={after}&per_page={PageSize}&page={page}";
                    var (exit, output, error) = Execute(tool, "api", path);
                    if (exit != 0)
                    {
                        throw new InvalidDataException($"{Path.GetFileName(tool)} exited {exit}: {error.Split('\n')[0].Trim()}");
                    }

                    using var answer = JsonDocument.Parse(output);
                    var requests = answer.RootElement.EnumerateArray().ToList();
                    var changes = new List<(string, DateTimeOffset)>();
                    foreach (var request in requests)
                    {
                        if (request.GetProperty("merged_at") is { ValueKind: JsonValueKind.String } at && at.GetDateTimeOffset() >= since)
                        {
                            var number = request.GetProperty(github ? "number" : "iid").GetInt64();
                            changes.Add(($"{repository}{(github ? '#' : '!')}{number}", at.GetDateTimeOffset().ToUniversalTime()));
                        }
                    }

                    database.RecordMerges(changes);
                    merged += changes.Count;
                    if (requests.Count < PageSize || requests.Any(request => request.GetProperty("updated_at").GetDateTimeOffset() < since))
                    {
                        break;
                    }
                }
            }
            catch (Win32Exception error)
            {
                unresolved.Add(Unresolved(repository, $"cannot run {tool}: {error.Message}"));
                continue;
            }
            catch (InvalidDataException error)
            {
                unresolved.Add(Unresolved(repository, error.Message));
                continue;
            }
            catch (Exception error) when (error is JsonException or KeyNotFoundException or InvalidOperationException or FormatException)
            {
                unresolved.Add(Unresolved(repository, $"unexpected answer: {error.Message}"));
                continue;
            }

            repositories.Add(new JsonObject { ["repository"] = repository, ["merged"] = merged });
        }

        return new JsonObject { ["since"] = since, ["repositories"] = repositories, ["unresolved"] = unresolved };
    }

    private static JsonObject Unresolved(string repository, string reason) => new() { ["repository"] = repository, ["reason"] = reason };

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
