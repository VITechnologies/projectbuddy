namespace ProjectBuddy.Kpi;

// The repositories checked out in the operator's directories, from every session's start context (its gitRoot and
// repository). A directory that two sessions saw with different repositories is ambiguous and maps to none.
internal sealed class Workspaces
{
    private readonly Dictionary<string, string?> roots = new(StringComparer.Ordinal);
    private readonly string home;

    private Workspaces(string home) => this.home = home;

    public static Workspaces From(IEnumerable<Ingest.Context?> contexts, string home)
    {
        var workspaces = new Workspaces(home);
        foreach (var context in contexts)
        {
            if (context is { GitRoot: { } root, Repository: { } repository })
            {
                workspaces.roots[root] = workspaces.roots.TryGetValue(root, out var known) && known != repository ? null : repository;
            }
        }

        return workspaces;
    }

    // The repository a command ran in: the directory of its last `cd` (absolute, relative to the session's directory,
    // or under ~ or $HOME), else the session's own; null when that directory is not a known checkout.
    public string? Repository(string? directory, Ingest.Context? session)
    {
        if (directory is null)
        {
            return session?.Repository;
        }

        var path = directory.Trim('"', '\'').Replace("${HOME}", "$HOME", StringComparison.Ordinal);
        path = path == "~" || path.StartsWith("~/", StringComparison.Ordinal) ? home + path[1..]
            : path.StartsWith("$HOME", StringComparison.Ordinal) ? home + path[5..]
            : path;
        if (path.Contains('$', StringComparison.Ordinal))
        {
            return null;
        }

        if (!Path.IsPathRooted(path))
        {
            if (session?.Cwd is not { } cwd)
            {
                return null;
            }

            path = Path.Combine(cwd, path);
        }

        path = Path.TrimEndingDirectorySeparator(Path.GetFullPath(path));
        return roots.Where(pair => path == pair.Key || path.StartsWith(pair.Key + "/", StringComparison.Ordinal))
            .OrderByDescending(pair => pair.Key.Length).Select(pair => pair.Value).FirstOrDefault();
    }
}
