using System.Text.Json.Nodes;
using Npgsql;
using Xunit;

namespace ProjectBuddy.Wiki.Tests;

public sealed class ParseTests
{
    private const string Password = "/run/pw";

    [Theory]
    [InlineData]
    [InlineData("publish")]
    [InlineData("seed", "--out", "/x", "--wiki", "/w", "--password-file", Password)]
    [InlineData("seed", "--wiki", "/w", "--wiki", "/w", "--password-file", Password)]
    [InlineData("seed", "--wiki")]
    [InlineData("seed", "--wiki", "relative", "--password-file", Password)]
    [InlineData("seed", "--wiki", "/w")]
    [InlineData("seed", "--password-file", Password)]
    [InlineData("check", "--password-file", Password)]
    [InlineData("render", "--password-file", Password)]
    [InlineData("render", "--out", "/o", "--commit", "abc", "--password-file", Password)]
    [InlineData("project", "--wiki", "/w", "--password-file", Password)]
    [InlineData("export-registry", "--importer-password-file", "/p", "--password-file", Password)]
    [InlineData("export-registry", "--approval", "/a", "--password-file", Password)]
    [InlineData("export-registry", "--approval", "relative", "--importer-password-file", "/p", "--password-file", Password)]
    [InlineData("export-registry", "--approval", "/a", "--importer-password-file", "relative", "--password-file", Password)]
    [InlineData("export-registry", "--approval", "/a", "--importer-password-file", "/p", "--importer", "Importer", "--password-file", Password)]
    [InlineData("export-registry", "--approval", "/a", "--importer-password-file", "/p", "--registry-database", "registry-local", "--password-file", Password)]
    [InlineData("migrate", "--approval", "/a", "--password-file", Password)]
    [InlineData("migrate", "--port", "0", "--password-file", Password)]
    [InlineData("migrate", "--port", "x", "--password-file", Password)]
    [InlineData("migrate", "--host", "bad host!", "--password-file", Password)]
    [InlineData("migrate", "--user", "Admin", "--password-file", Password)]
    [InlineData("migrate", "--database", "wiki-local", "--password-file", Password)]
    [InlineData("migrate", "--git", "git", "--password-file", Password)]
    [InlineData("migrate", "--create-database", "--create-database", "--password-file", Password)]
    public void RejectsMalformedCommandLines(params string[] args)
    {
        var (exit, output, error) = Cli.Run(args);
        Assert.Equal(ExitCodes.Usage, exit);
        Assert.Empty(output);
        Assert.EndsWith(Program.Usage + Environment.NewLine, error);
    }

    [Fact]
    public void FillsInTheDefaults()
    {
        var options = Program.Parse(["render", "--out", "/tmp/vault/", "--commit", new string('a', 40), "--password-file", Password, "--host", "10.0.0.5"]);
        Assert.Equal(new Options("render", "10.0.0.5", 5432, "postgres", Password, "wiki_local", null, "/tmp/vault", new string('a', 40), Program.DefaultGit, false), options);
        Assert.True(Program.Parse(["migrate", "--create-database", "--password-file", Password, "--host", "db.local"]).CreateDatabase);
    }

    [Fact]
    public void RefusesAnUnreadableOrEmptyPassword()
    {
        var (exit, _, error) = Cli.Run("migrate", "--password-file", "/nonexistent/password");
        Assert.Equal(ExitCodes.Refused, exit);
        Assert.Contains("cannot read the password file: DirectoryNotFoundException", error);

        var empty = Path.GetTempFileName();
        File.WriteAllText(empty, "\n");
        try
        {
            Assert.Contains("the password file is empty", Cli.Run("migrate", "--password-file", empty).Error);
        }
        finally
        {
            File.Delete(empty);
        }
    }

    [Fact]
    public void ReportsAnUnreachableDatabase()
    {
        var password = Path.GetTempFileName();
        File.WriteAllText(password, "x");
        try
        {
            var (exit, _, error) = Cli.Run("migrate", "--port", "1", "--password-file", password);
            Assert.Equal(ExitCodes.Database, exit);
            Assert.StartsWith("projectbuddy-wiki: database: ", error);
        }
        finally
        {
            File.Delete(password);
        }
    }
}

[System.Runtime.Versioning.UnsupportedOSPlatform("windows")]
public sealed class GitTests : IDisposable
{
    private readonly Repo repo = new();

    public void Dispose() => repo.Dispose();

    [Fact]
    public void NamesHeadOnlyInAWorkTreeWithACommit()
    {
        var git = new Git(Program.DefaultGit);
        Assert.Null(git.Head(repo.Path));
        Assert.Null(git.Head("/nonexistent/wiki"));
        var commit = repo.Write("Home.md", "home\n").Commit();
        Assert.Equal(commit, git.Head(repo.Path));
        Assert.True(Git.IsCommit(commit));
        Assert.False(Git.IsCommit(commit.ToUpperInvariant()));
        Assert.False(Git.IsCommit(commit[..39]));
        Assert.True(Git.IsCommit(new string('0', 64)));
    }

    [Fact]
    public void NamesTheTopOfTheWorkTree()
    {
        var git = new Git(Program.DefaultGit);
        Directory.CreateDirectory(Path.Combine(repo.Path, "sub"));
        Assert.True(git.IsTopLevel(repo.Path));
        Assert.False(git.IsTopLevel(Path.Combine(repo.Path, "sub")));
        Assert.False(git.IsTopLevel(Path.Combine(repo.Path, ".git")));
        Assert.False(git.IsTopLevel("/nonexistent/wiki"));
    }

    [Fact]
    public void RefusesAPathThatIsNotUtf8()
    {
        var script = Path.Combine(repo.Path, "fake-git");
        File.WriteAllText(script, "#!/bin/sh\nprintf '100644 blob 0123456789012345678901234567890123456789\\t\\377.md\\0'\n");
        File.SetUnixFileMode(script, UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute);
        var commit = new string('a', 40);
        Assert.Equal($"a path in {commit} is not valid UTF-8", Assert.Throws<WikiException>(() => new Git(script).Tree(repo.Path, commit)).Message);
    }

    [Fact]
    public void AcceptsOnlyRegularFiles()
    {
        var git = new Git(Program.DefaultGit);
        repo.Write("Home.md", "home\n").Commit();
        File.CreateSymbolicLink(Path.Combine(repo.Path, "link.md"), "Home.md");
        var linked = repo.Commit();
        var refused = Assert.Throws<WikiException>(() => git.Tree(repo.Path, linked));
        Assert.Equal("only regular files are supported, not 120000 blob at link.md", refused.Message);

        repo.Remove("link.md");
        repo.Write("run.sh", "#!/bin/sh\n");
        File.SetUnixFileMode(Path.Combine(repo.Path, "run.sh"), UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute);
        Assert.Contains("not 100755 blob at run.sh", Assert.Throws<WikiException>(() => git.Tree(repo.Path, repo.Commit())).Message);

        using var bare = new Repo();
        var empty = bare.Commit("empty");
        Assert.Equal($"commit {empty} has no files", Assert.Throws<WikiException>(() => git.Tree(bare.Path, empty)).Message);
        Assert.Equal($"git ls-tree failed on {new string('1', 40)}", Assert.Throws<WikiException>(() => git.Tree(repo.Path, new string('1', 40))).Message);
    }

    [Fact]
    public void RefusesAGitThatFailsOrHangs()
    {
        var script = Path.Combine(repo.Path, "fake-git");
        // ls-tree lists 2000 files, so the cat-file request (about 82 KB) is larger than a pipe buffer: the write always
        // blocks until the fake git exits without reading, and always breaks.
        File.WriteAllText(script, "#!/bin/sh\ncase \"$3\" in\n  ls-tree) i=0; while [ $i -lt 2000 ]; do printf '100644 blob 0123456789012345678901234567890123456789\\tf%s.md\\0' $i; i=$((i+1)); done ;;\n  cat-file) exit 1 ;;\n  *) sleep 30 ;;\nesac\n");
        File.SetUnixFileMode(script, UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute);
        Assert.Equal("git cat-file failed", Assert.Throws<WikiException>(() => new Git(script).Tree(repo.Path, new string('a', 40))).Message);
        Assert.Equal("git rev-parse timed out", Assert.Throws<WikiException>(() => new Git(script, TimeSpan.FromMilliseconds(200)).Head(repo.Path)).Message);
        Assert.Equal("cannot start /nonexistent/git", Assert.Throws<WikiException>(() => new Git("/nonexistent/git").Head(repo.Path)).Message);
    }
}

[CollectionDefinition(nameof(ProcessEnvironment), DisableParallelization = true)]
public sealed class ProcessEnvironment;

// Changes this process's environment, so it runs alone.
[Collection(nameof(ProcessEnvironment))]
public sealed class GitEnvironmentTests
{
    [Fact]
    public void IgnoresAnInheritedGitDirectory()
    {
        using var repo = new Repo();
        var commit = repo.Write("Home.md", "home\n").Commit();
        var plain = Directory.CreateTempSubdirectory("pbw-plain-").FullName;
        Environment.SetEnvironmentVariable("GIT_DIR", Path.Combine(repo.Path, ".git"));
        try
        {
            // The variable makes a plain git answer for the repository, from any directory.
            Assert.Equal(commit, Repo.Exec(plain, "rev-parse", "HEAD"));
            var git = new Git(Program.DefaultGit);
            Assert.Null(git.Head(plain));
            Assert.False(git.IsTopLevel(plain));
        }
        finally
        {
            Environment.SetEnvironmentVariable("GIT_DIR", null);
            Directory.Delete(plain, recursive: true);
        }
    }
}

public sealed class DatabaseTests : IDisposable
{
    private readonly Server server = new();

    public void Dispose() => server.Dispose();

    [Fact]
    public void MigratesOnceAndRefusesAChangedMigration()
    {
        var connection = server.NewDatabase();
        var names = string.Join(",", Database.Migrations().Select(migration => $"\"{migration.Name}\""));
        var (exit, output, _) = Cli.Run(["migrate", "--create-database", .. connection]);
        Assert.Equal(0, exit);
        Assert.Equal($$"""{"database":"{{connection[^1]}}","created":true,"applied":[{{names}}],"present":[]}""", output.Trim());

        var again = Cli.Run(["migrate", "--create-database", .. connection]);
        Assert.Equal($$"""{"database":"{{connection[^1]}}","created":false,"applied":[],"present":[{{names}}]}""", again.Output.Trim());

        server.Execute(connection, "UPDATE public.wiki_schema_migrations SET sha256 = repeat('0', 64)");
        var tampered = Cli.Run(["migrate", .. connection]);
        Assert.Equal(ExitCodes.Refused, tampered.Exit);
        Assert.Contains("migration 0001_wiki_v1.sql changed after it was applied", tampered.Error);
    }

    [Fact]
    public void NeedsTheDatabaseUnlessAskedToCreateIt()
    {
        var (exit, _, error) = Cli.Run(["migrate", .. server.NewDatabase()]);
        Assert.Equal(ExitCodes.Database, exit);
        Assert.Contains("does not exist", error);
        Assert.Equal([(1, "0001_wiki_v1.sql"), (2, "0002_ingest_parent_check.sql"), (3, "0003_wiki_projection.sql")], Database.Migrations().Select(migration => (migration.Version, migration.Name)));
    }

    [Fact]
    public async Task SerializesConcurrentFirstRuns()
    {
        for (var round = 0; round < 3; round++)
        {
            var connection = server.NewDatabase();
            var runs = await Task.WhenAll(Enumerable.Range(0, 4).Select(_ => Task.Run(() => Cli.Run(["migrate", "--create-database", .. connection]))));
            Assert.All(runs, run => Assert.Equal(0, run.Exit));
            var results = runs.Select(run => JsonNode.Parse(run.Output)!.AsObject()).ToList();
            Assert.Single(results, result => (bool)result["created"]!);
            Assert.Single(results, result => result["applied"]!.AsArray().Count == Database.Migrations().Count);
        }
    }

    [Theory]
    [InlineData("ARRAY['x', 'x/y.md']", "a snapshot cannot hold a file where it holds a directory")]
    [InlineData("ARRAY['a/b/c.md', 'a/b']", "a snapshot cannot hold a file where it holds a directory")]
    [InlineData("ARRAY['x.md', 'x.md']", "a snapshot cannot repeat a path")]
    [InlineData("ARRAY['.git/config', 'Home.md']", "violates check constraint")]
    [InlineData("ARRAY['docs/.GIT/hooks/post-checkout', 'Home.md']", "violates check constraint")]
    [InlineData("ARRAY['../escape.md', 'Home.md']", "violates check constraint")]
    public void RefusesAStateThatCannotBeWrittenAsFiles(string paths, string reason)
    {
        var connection = server.Migrated();
        var refused = Assert.Throws<PostgresException>(() => server.Execute(
            connection, $"SELECT * FROM wiki_v1.ingest_snapshot(repeat('a', 40), {paths}, ARRAY['1'::bytea, '2'::bytea])"));
        Assert.Contains(reason, refused.MessageText);
        server.Execute(connection, "SELECT * FROM wiki_v1.ingest_snapshot(repeat('a', 40), ARRAY['x.git', '.github/x.md', 'a/.gitkeep'], ARRAY['1'::bytea, '2'::bytea, '3'::bytea])");
    }
}

public sealed class SeedTests : IDisposable
{
    private readonly Server server = new();
    private readonly Repo repo = new();

    public void Dispose()
    {
        repo.Dispose();
        server.Dispose();
    }

    private (int Exit, JsonObject Result, string Error) Seed(string[] connection, string? wiki = null)
    {
        var (exit, output, error) = Cli.Run(["seed", "--wiki", wiki ?? repo.Path, .. connection]);
        return (exit, exit == 0 ? JsonNode.Parse(output)!.AsObject() : [], error);
    }

    [Fact]
    public void RecordsEachCommitOnceAndOnlyNewBytes()
    {
        var connection = server.Migrated();
        var first = repo.Write("Home.md", "home\n").Write("tests/TEST-001.md", "no final newline").Write("a/b.md", "b\n").Commit();
        var seeded = Seed(connection);
        Assert.Equal(0, seeded.Exit);
        Assert.Equal(first, (string?)seeded.Result["commit"]);
        Assert.Equal("ingested", (string?)seeded.Result["status"]);
        Assert.Equal((3, 3), ((int)seeded.Result["files"]!, (int)seeded.Result["newBlobs"]!));
        Assert.Equal("""{"added":3,"modified":0,"deleted":0}""", seeded.Result["changes"]!.ToJsonString());

        Assert.Equal("up-to-date", (string?)Seed(connection).Result["status"]);

        repo.Write("Home.md", "home, changed\n").Remove("a/b.md").Write("new.md", "new\n").Commit();
        var changed = Seed(connection);
        Assert.Equal(("ingested", 2), ((string?)changed.Result["status"], (int)changed.Result["newBlobs"]!));
        Assert.Equal("""{"added":1,"modified":1,"deleted":1}""", changed.Result["changes"]!.ToJsonString());

        // A commit that returns to an earlier state reuses it.
        repo.Write("Home.md", "home\n").Remove("new.md").Write("a/b.md", "b\n").Commit("revert");
        var reverted = Seed(connection);
        Assert.Equal(("reused", 0), ((string?)reverted.Result["status"], (int)reverted.Result["newBlobs"]!));
        Assert.Equal(seeded.Result["snapshot"]!.ToJsonString(), reverted.Result["snapshot"]!.ToJsonString());
    }

    [Fact]
    public void RefusesWhatIsNotAWikiCommit()
    {
        var connection = server.Migrated();
        var (exit, _, error) = Seed(connection);
        Assert.Equal(ExitCodes.Refused, exit);
        Assert.Contains("is not a git work tree with a commit", error);
        Assert.Equal(ExitCodes.Refused, Seed(connection, "/nonexistent/wiki").Exit);
        Assert.Equal(ExitCodes.Refused, Cli.Run(["seed", "--wiki", repo.Path, "--git", "/nonexistent/git", .. connection]).Exit);
    }

    [Fact]
    public void KeepsEveryByte()
    {
        var connection = server.Migrated();
        byte[] binary = [0, 1, 2, 255, 13, 10, 13];
        repo.Write("crlf.md", "a\r\nb\r\n").Write("bin/blob.bin", binary).Write("ünïcode/页面.md", "x").Write(".obsidian/app.json", "{}").Commit();
        Assert.Equal(0, Seed(connection).Exit);
        var target = Path.Combine(Path.GetTempPath(), $"pbw-out-{Guid.NewGuid():N}");
        try
        {
            Assert.Equal(0, Cli.Run(["render", "--out", target, .. connection]).Exit);
            Assert.Equal(binary, File.ReadAllBytes(Path.Combine(target, "bin", "blob.bin")));
            Assert.Equal("a\r\nb\r\n"u8.ToArray(), File.ReadAllBytes(Path.Combine(target, "crlf.md")));
            Assert.Equal("x"u8.ToArray(), File.ReadAllBytes(Path.Combine(target, "ünïcode", "页面.md")));
            Assert.True(File.Exists(Path.Combine(target, ".obsidian", "app.json")));
        }
        finally
        {
            Directory.Delete(target, recursive: true);
        }
    }
}

public sealed class RenderTests : IDisposable
{
    private readonly Server server = new();
    private readonly Repo repo = new();
    private readonly List<string> targets = [];

    public void Dispose()
    {
        foreach (var target in targets.Where(Directory.Exists))
        {
            Directory.Delete(target, recursive: true);
        }

        repo.Dispose();
        server.Dispose();
    }

    private string Target()
    {
        var target = Path.Combine(Path.GetTempPath(), $"pbw-out-{Guid.NewGuid():N}");
        targets.Add(target);
        return target;
    }

    private static (int Exit, JsonObject Result, string Error) Render(string[] connection, string target, params string[] extra)
    {
        var (exit, output, error) = Cli.Run(["render", "--out", target, .. extra, .. connection]);
        return (exit, exit == 0 ? JsonNode.Parse(output)!.AsObject() : [], error);
    }

    [Fact]
    public void WritesTheLatestOrANamedStateIntoNewOrEmptyDirectories()
    {
        var connection = server.Migrated();
        Assert.Contains("nothing is recorded yet; run seed first", Render(connection, Target()).Error);

        var first = repo.Write("Home.md", "v1\n").Commit();
        Cli.Run(["seed", "--wiki", repo.Path, .. connection]);
        repo.Write("Home.md", "v2\n").Commit();
        Cli.Run(["seed", "--wiki", repo.Path, .. connection]);

        var fresh = Target();
        var latest = Render(connection, fresh);
        Assert.Equal(("new", 1), ((string?)latest.Result["target"], (int)latest.Result["written"]!));
        Assert.Equal("v2\n", File.ReadAllText(Path.Combine(fresh, "Home.md")));

        var empty = Target();
        Directory.CreateDirectory(empty);
        var named = Render(connection, empty, "--commit", first);
        Assert.Equal("empty", (string?)named.Result["target"]);
        Assert.Equal("v1\n", File.ReadAllText(Path.Combine(empty, "Home.md")));

        Assert.Contains($"commit {new string('b', 40)} is not recorded", Render(connection, Target(), "--commit", new string('b', 40)).Error);
    }

    [Fact]
    public void UpdatesARecordedCloneAndPrunesWhatTheStateDoesNotHold()
    {
        var connection = server.Migrated();
        repo.Write("Home.md", "home\n").Write("a/b.md", "b\n").Commit();
        Cli.Run(["seed", "--wiki", repo.Path, .. connection]);
        repo.Write("Home.md", "edited\n").Write("extra/stray.md", "stray\n");
        Directory.CreateDirectory(Path.Combine(repo.Path, "empty", "nested"));

        var result = Render(connection, repo.Path);
        Assert.Equal(0, result.Exit);
        Assert.Equal("""{"target":"clone","written":1,"unchanged":1,"deleted":1}""",
            new JsonObject { ["target"] = result.Result["target"]!.DeepClone(), ["written"] = result.Result["written"]!.DeepClone(), ["unchanged"] = result.Result["unchanged"]!.DeepClone(), ["deleted"] = result.Result["deleted"]!.DeepClone() }.ToJsonString());
        Assert.Equal("home\n", File.ReadAllText(Path.Combine(repo.Path, "Home.md")));
        Assert.False(Directory.Exists(Path.Combine(repo.Path, "extra")));
        Assert.False(Directory.Exists(Path.Combine(repo.Path, "empty")));
        Assert.True(Directory.Exists(Path.Combine(repo.Path, ".git")));
        Assert.Equal("", repo.Git("status", "--porcelain"));
    }

    [Fact]
    public void RefusesAnyOtherTarget()
    {
        var connection = server.Migrated();
        repo.Write("Home.md", "home\n").Commit();
        Cli.Run(["seed", "--wiki", repo.Path, .. connection]);

        var file = Target();
        File.WriteAllText(file, "x");
        Assert.Contains("is a file", Render(connection, file).Error);
        File.Delete(file);

        var busy = Target();
        Directory.CreateDirectory(busy);
        File.WriteAllText(Path.Combine(busy, "keep.txt"), "x");
        Assert.Contains("must be missing, empty, or the top of a clone of this wiki at a recorded commit", Render(connection, busy).Error);
        Assert.True(File.Exists(Path.Combine(busy, "keep.txt")));

        using var other = new Repo();
        other.Write("README.md", "not the wiki\n").Commit();
        var refused = Render(connection, other.Path);
        Assert.Equal(ExitCodes.Refused, refused.Exit);
        Assert.True(File.Exists(Path.Combine(other.Path, "README.md")));

        var linked = Target();
        Directory.CreateDirectory(Path.Combine(linked, "sub"));
        File.CreateSymbolicLink(Path.Combine(linked, "sub", "escape"), "/");
        Assert.Contains("is a symbolic link", Render(connection, linked).Error);

        var linkedTarget = Target();
        Directory.CreateDirectory(linkedTarget);
        var link = Path.Combine(Path.GetTempPath(), $"pbw-link-{Guid.NewGuid():N}");
        Directory.CreateSymbolicLink(link, linkedTarget);
        try
        {
            Assert.Equal($"projectbuddy-wiki: {link} is a symbolic link", Render(connection, link).Error.Trim());
            Assert.Empty(Directory.EnumerateFileSystemEntries(linkedTarget));
        }
        finally
        {
            File.Delete(link);
        }
    }

    [Fact]
    public void RefusesAnythingButTheTopOfARecordedClone()
    {
        var connection = server.Migrated();
        repo.Write("Home.md", "home\n").Write("docs/page.md", "page\n").Commit();
        Cli.Run(["seed", "--wiki", repo.Path, .. connection]);
        var gitFiles = Directory.GetFiles(Path.Combine(repo.Path, ".git"), "*", SearchOption.AllDirectories).Order().ToList();

        // Git finds the enclosing repository from its .git, a subdirectory, or a subdirectory with a .git that is not one.
        File.WriteAllText(Path.Combine(repo.Path, "docs", "draft.md"), "untracked\n");
        Assert.Equal(ExitCodes.Refused, Render(connection, Path.Combine(repo.Path, ".git")).Exit);
        Assert.Equal(ExitCodes.Refused, Render(connection, Path.Combine(repo.Path, "docs")).Exit);
        Directory.CreateDirectory(Path.Combine(repo.Path, "docs", ".git"));
        Assert.Equal(ExitCodes.Refused, Render(connection, Path.Combine(repo.Path, "docs")).Exit);
        Directory.Delete(Path.Combine(repo.Path, "docs", ".git"));

        Assert.Equal(gitFiles, Directory.GetFiles(Path.Combine(repo.Path, ".git"), "*", SearchOption.AllDirectories).Order().ToList());
        Assert.Equal("untracked\n", File.ReadAllText(Path.Combine(repo.Path, "docs", "draft.md")));

        // A clone reached through a symbolic link to a parent directory is still the top of its work tree.
        var parent = Path.Combine(Path.GetTempPath(), $"pbw-parent-{Guid.NewGuid():N}");
        Directory.CreateSymbolicLink(parent, Path.GetDirectoryName(repo.Path)!);
        try
        {
            var throughLink = Render(connection, Path.Combine(parent, Path.GetFileName(repo.Path)));
            Assert.Equal((0, "clone", 2), (throughLink.Exit, (string?)throughLink.Result["target"], (int)throughLink.Result["unchanged"]!));
        }
        finally
        {
            File.Delete(parent);
        }

        using var unborn = new Repo();
        unborn.Write("Home.md", "home\n");
        Assert.Equal(ExitCodes.Refused, Render(connection, unborn.Path).Exit);
        Assert.True(File.Exists(Path.Combine(unborn.Path, "Home.md")));
    }

    [Fact]
    public void ReplacesAFileWithADirectoryAndBack()
    {
        var connection = server.Migrated();
        var asFile = repo.Write("Home.md", "home\n").Write("x", "file\n").Commit();
        Cli.Run(["seed", "--wiki", repo.Path, .. connection]);
        repo.Remove("x").Write("x/y.md", "nested\n").Commit();
        Cli.Run(["seed", "--wiki", repo.Path, .. connection]);

        var back = Render(connection, repo.Path, "--commit", asFile);
        Assert.Equal((0, 1, 1, 1), (back.Exit, (int)back.Result["written"]!, (int)back.Result["unchanged"]!, (int)back.Result["deleted"]!));
        Assert.Equal("file\n", File.ReadAllText(Path.Combine(repo.Path, "x")));

        var forward = Render(connection, repo.Path);
        Assert.Equal((0, 1, 1, 1), (forward.Exit, (int)forward.Result["written"]!, (int)forward.Result["unchanged"]!, (int)forward.Result["deleted"]!));
        Assert.Equal("nested\n", File.ReadAllText(Path.Combine(repo.Path, "x", "y.md")));
        Assert.Equal("", repo.Git("status", "--porcelain"));
    }

    [Fact]
    [System.Runtime.Versioning.UnsupportedOSPlatform("windows")]
    public void ReportsATargetThatCannotBeWrittenAsRefused()
    {
        var connection = server.Migrated();
        repo.Write("Home.md", "home\n").Commit();
        Cli.Run(["seed", "--wiki", repo.Path, .. connection]);

        var file = Target();
        File.WriteAllText(file, "x");
        var underFile = Render(connection, Path.Combine(file, "sub"));
        File.Delete(file);
        Assert.Equal(ExitCodes.Refused, underFile.Exit);
        Assert.StartsWith("projectbuddy-wiki: ", underFile.Error);

        var readOnly = Target();
        Directory.CreateDirectory(readOnly);
        File.SetUnixFileMode(readOnly, UnixFileMode.UserRead | UnixFileMode.UserExecute);
        var denied = Render(connection, readOnly);
        Assert.Equal(ExitCodes.Refused, denied.Exit);
        Assert.Contains("denied", denied.Error);
    }
}

public sealed class CheckTests : IDisposable
{
    private readonly Server server = new();
    private readonly Repo repo = new();

    public void Dispose()
    {
        repo.Dispose();
        server.Dispose();
    }

    private (int Exit, JsonObject Result) Check(string[] connection)
    {
        var (exit, output, _) = Cli.Run(["check", "--wiki", repo.Path, .. connection]);
        return (exit, JsonNode.Parse(output)!.AsObject());
    }

    [Fact]
    public void ProvesTheDatabaseRendersThePublishedWiki()
    {
        var connection = server.Migrated();
        var commit = repo.Write("Home.md", "home\n").Write("gone.md", "gone\n").Commit();
        var nothing = Check(connection);
        Assert.Equal(ExitCodes.Findings, nothing.Exit);
        Assert.Equal("nothing is recorded yet; run seed first", (string?)nothing.Result["reason"]);

        Cli.Run(["seed", "--wiki", repo.Path, .. connection]);
        var clean = Check(connection);
        Assert.Equal(0, clean.Exit);
        Assert.Equal((false, commit, commit), ((bool)clean.Result["drift"]!, (string?)clean.Result["commit"], (string?)clean.Result["recordedCommit"]));

        // The wiki moved on without a seed: every kind of difference is listed.
        repo.Write("Home.md", "edited\n").Remove("gone.md").Write("new.md", "new\n").Commit();
        var drift = Check(connection);
        Assert.Equal(ExitCodes.Findings, drift.Exit);
        Assert.Equal(
            """[{"path":"Home.md","change":"different"},{"path":"gone.md","change":"only-in-database"},{"path":"new.md","change":"only-in-wiki"}]""",
            drift.Result["differences"]!.ToJsonString());
        Assert.Equal(ExitCodes.Refused, Cli.Run(["check", "--wiki", "/nonexistent/wiki", .. connection]).Exit);
    }

    [Fact]
    public void ListsAtMostAHundredDifferences()
    {
        var connection = server.Migrated();
        repo.Write("Home.md", "home\n").Commit();
        Cli.Run(["seed", "--wiki", repo.Path, .. connection]);
        for (var index = 0; index < Commands.MaxListed + 5; index++)
        {
            repo.Write($"p/{index:D3}.md", "x\n");
        }

        repo.Commit();
        var drift = Check(connection);
        Assert.Equal(Commands.MaxListed + 5, (int)drift.Result["differenceCount"]!);
        Assert.Equal(Commands.MaxListed, drift.Result["differences"]!.AsArray().Count);
    }
}
