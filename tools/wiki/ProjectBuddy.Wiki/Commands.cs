using System.Text.Json.Nodes;

namespace ProjectBuddy.Wiki;

internal static class Commands
{
    public const int MaxListed = 100;

    // Records the wiki clone's HEAD. A commit already recorded changes nothing; otherwise the procedure stores only the
    // bytes it does not hold yet, and the result says what changed since the previously recorded state.
    public static JsonObject Seed(Database database, Git git, string wiki)
    {
        var commit = git.Head(wiki) ?? throw new WikiException(ExitCodes.Refused, $"{wiki} is not a git work tree with a commit");
        if (database.CommitSnapshot(commit) is { } known)
        {
            return new JsonObject { ["commit"] = commit, ["snapshot"] = known.ToString(), ["status"] = "up-to-date" };
        }

        var previous = database.Latest();
        var files = git.Tree(wiki, commit);
        var ingested = database.Ingest(commit, files);
        var changes = previous is null ? files.Select(file => (file.Path, Change: "added")).ToList() : [.. database.Changed(previous.Id, ingested.Snapshot)];
        return new JsonObject
        {
            ["commit"] = commit,
            ["snapshot"] = ingested.Snapshot.ToString(),
            ["digest"] = ingested.Digest,
            ["status"] = ingested.Reused ? "reused" : "ingested",
            ["files"] = files.Count,
            ["newBlobs"] = ingested.NewBlobs,
            ["changes"] = Count(changes),
        };
    }

    // Writes a recorded state as files. The target must be missing, an empty directory, or the top of a git clone whose
    // HEAD is a commit this database recorded (so it is this wiki, never another repository, its .git or a subdirectory);
    // a symbolic link as or anywhere in the target refuses the render. Files the state does not hold are removed first,
    // so a file can replace a directory and a directory a file.
    public static JsonObject Render(Database database, Git git, string target, string? commit)
    {
        var (snapshot, _) = Recorded(database, commit);
        var mode = Target(database, git, target);
        var files = database.Export(snapshot).Select(file => (Path: Path.GetFullPath(Path.Combine(target, file.Path.Replace('/', Path.DirectorySeparatorChar))), file.Content)).ToList();
        var expected = files.Select(file => file.Path).ToHashSet(StringComparer.Ordinal);
        int written = 0, unchanged = 0, deleted = 0;

        // The database refuses a state that holds a file where it holds a directory, so after this prune no directory is
        // left where a file goes, and no file where a directory goes.
        foreach (var path in Walk(target).Where(path => !expected.Contains(path)).ToList())
        {
            File.Delete(path);
            deleted++;
        }

        RemoveEmptyDirectories(target, root: true);
        foreach (var (path, content) in files)
        {
            if (File.Exists(path) && File.ReadAllBytes(path).AsSpan().SequenceEqual(content))
            {
                unchanged++;
                continue;
            }

            Directory.CreateDirectory(Path.GetDirectoryName(path)!);
            File.WriteAllBytes(path, content);
            written++;
        }

        return new JsonObject
        {
            ["snapshot"] = snapshot.ToString(),
            ["target"] = mode,
            ["written"] = written,
            ["unchanged"] = unchanged,
            ["deleted"] = deleted,
        };
    }

    // Compares the wiki clone's HEAD with the latest recorded state, byte for byte: no drift means the database renders
    // exactly the published wiki. Drift is a finding.
    public static (JsonObject Result, bool Drift) Check(Database database, Git git, string wiki)
    {
        var commit = git.Head(wiki) ?? throw new WikiException(ExitCodes.Refused, $"{wiki} is not a git work tree with a commit");
        if (database.Latest() is not { } latest)
        {
            return (new JsonObject { ["commit"] = commit, ["drift"] = true, ["reason"] = "nothing is recorded yet; run seed first" }, true);
        }

        var published = git.Tree(wiki, commit).ToDictionary(file => file.Path, file => file.Content, StringComparer.Ordinal);
        var recorded = database.Export(latest.Id).ToDictionary(file => file.Path, file => file.Content, StringComparer.Ordinal);
        var differences = published.Keys.Union(recorded.Keys).Order(StringComparer.Ordinal)
            .Select(path => (Path: path, Change: !recorded.ContainsKey(path) ? "only-in-wiki" : !published.ContainsKey(path) ? "only-in-database"
                : published[path].AsSpan().SequenceEqual(recorded[path]) ? null : "different"))
            .Where(item => item.Change is not null)
            .ToList();
        return (new JsonObject
        {
            ["commit"] = commit,
            ["snapshot"] = latest.Id.ToString(),
            ["recordedCommit"] = latest.Commit,
            ["drift"] = differences.Count > 0,
            ["differenceCount"] = differences.Count,
            ["differences"] = new JsonArray([.. differences.Take(MaxListed).Select(item => (JsonNode)new JsonObject { ["path"] = item.Path, ["change"] = item.Change })]),
        }, differences.Count > 0);
    }

    // Reports the governed projection of a recorded state (wiki_v1.projection_report): its pages per scope and kind, its
    // trace edges per type, and every page or link that keeps it out of the registry, which is a finding.
    public static (JsonObject Result, bool NotExportable) Project(Database database, string? commit)
    {
        var (snapshot, recorded) = Recorded(database, commit);
        var result = new JsonObject { ["commit"] = recorded, ["snapshot"] = snapshot.ToString() };
        foreach (var (name, value) in database.ProjectionReport(snapshot))
        {
            result[name] = value!.DeepClone();
        }

        var exportable = (int)result["problemCount"]! == 0;
        result["exportable"] = exportable;
        return (result, !exportable);
    }

    // Exports a recorded state to the RSR registry, one snapshot per scope in the order software, system, test: each
    // scope's edges point into the snapshots before it. A scope whose content key the registry holds already is reused,
    // so exporting unchanged content again, even from a later commit, changes nothing.
    public static JsonObject ExportRegistry(Database database, Registry registry, Approval approval, string? commit)
    {
        var (snapshot, recorded) = Recorded(database, commit);
        var problems = (int)database.ProjectionReport(snapshot)["problemCount"]!;
        if (problems > 0)
        {
            throw new WikiException(ExitCodes.Refused, $"{problems} pages or links cannot be exported; run project");
        }

        var identity = new JsonObject
        {
            ["source_uuid"] = approval.SourceUuid,
            ["constellation_uuid"] = approval.ConstellationUuid,
            ["manifest_digest"] = approval.ManifestDigest,
            ["source_ref"] = approval.SourceRef,
        }.ToJsonString();
        var targets = new JsonObject();
        var scopes = new JsonObject();
        foreach (var scope in (string[])["software", "system", "test"])
        {
            var text = database.Envelope(recorded, scope, identity, targets.ToJsonString());
            var envelope = JsonNode.Parse(text)!;
            var (id, reused) = registry.Find(approval.SourceUuid, scope, (string)envelope["tree_digest"]!, (string)envelope["parser_version"]!, approval.ManifestDigest) is { } found
                ? (found, true)
                : registry.Ingest(text);
            targets[scope] = id.ToString();
            scopes[scope] = new JsonObject
            {
                ["snapshot"] = id.ToString(),
                ["reused"] = reused,
                ["nodes"] = envelope["nodes"]!.AsArray().Count,
                ["edges"] = envelope["edges"]!.AsArray().Count,
            };
        }

        return new JsonObject { ["commit"] = recorded, ["snapshot"] = snapshot.ToString(), ["source"] = approval.SourceUuid, ["scopes"] = scopes };
    }

    // The latest recorded state, or the state of a named recorded commit.
    private static (Guid Snapshot, string Commit) Recorded(Database database, string? commit)
    {
        if (commit is null)
        {
            var latest = database.Latest() ?? throw new WikiException(ExitCodes.Refused, "nothing is recorded yet; run seed first");
            return (latest.Id, latest.Commit);
        }

        return (database.CommitSnapshot(commit) ?? throw new WikiException(ExitCodes.Refused, $"commit {commit} is not recorded"), commit);
    }

    private static string Target(Database database, Git git, string target)
    {
        if (new FileInfo(target).LinkTarget is not null)
        {
            throw new WikiException(ExitCodes.Refused, $"{target} is a symbolic link");
        }

        if (File.Exists(target))
        {
            throw new WikiException(ExitCodes.Refused, $"{target} is a file");
        }

        if (!Directory.Exists(target))
        {
            Directory.CreateDirectory(target);
            return "new";
        }

        Walk(target).ToList();
        if (!Directory.EnumerateFileSystemEntries(target).Any())
        {
            return "empty";
        }

        if (Directory.Exists(Path.Combine(target, ".git"))
            && git.IsTopLevel(target)
            && git.Head(target) is { } head
            && database.CommitSnapshot(head) is not null)
        {
            return "clone";
        }

        throw new WikiException(ExitCodes.Refused, $"{target} must be missing, empty, or the top of a clone of this wiki at a recorded commit");
    }

    // Every file under the target outside its top-level .git, as full paths; a symbolic link refuses the render.
    private static IEnumerable<string> Walk(string directory, bool root = true)
    {
        foreach (var entry in new DirectoryInfo(directory).EnumerateFileSystemInfos())
        {
            if (entry.Attributes.HasFlag(FileAttributes.ReparsePoint))
            {
                throw new WikiException(ExitCodes.Refused, $"{entry.FullName} is a symbolic link");
            }

            if (root && entry.Name == ".git")
            {
                continue;
            }

            if (entry is DirectoryInfo child)
            {
                foreach (var file in Walk(child.FullName, root: false))
                {
                    yield return file;
                }
            }
            else
            {
                yield return Path.GetFullPath(entry.FullName);
            }
        }
    }

    private static void RemoveEmptyDirectories(string directory, bool root)
    {
        foreach (var child in Directory.EnumerateDirectories(directory).Where(path => !(root && Path.GetFileName(path) == ".git")))
        {
            RemoveEmptyDirectories(child, root: false);
        }

        if (!root && !Directory.EnumerateFileSystemEntries(directory).Any())
        {
            Directory.Delete(directory);
        }
    }

    private static JsonObject Count(IReadOnlyList<(string Path, string Change)> changes) => new()
    {
        ["added"] = changes.Count(change => change.Change == "added"),
        ["modified"] = changes.Count(change => change.Change == "modified"),
        ["deleted"] = changes.Count(change => change.Change == "deleted"),
    };
}
