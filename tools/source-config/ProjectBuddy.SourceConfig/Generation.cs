using System.Diagnostics;
using System.Security.Cryptography;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text;
using System.Text.Json;
using Tomlyn;

namespace ProjectBuddy.SourceConfig;

public sealed record Settings(string Root, string Manifest, string Build, string Repository,
    string Revision, string Schema, string Output, string OllamaUrl, string Model, string ModelDigest,
    ReviewBudget Budget, ReviewPrivacy Privacy, ReviewLifecyclePolicy Lifecycle);
public sealed record Review(string Decision, string[] Blockers, string Model, string Digest)
{
    public ReviewRequestBudget? RequestBudget { get; init; }
    public ReviewModelBudget? ModelBudget { get; init; }
    public string? ModelOutputSha256 { get; init; }
    public ReviewLifecycleEvidence? Lifecycle { get; init; }
    public bool Accepted => Decision == "accept" && Blockers.Length == 0;
}
public sealed record InputFile(string Text, string Sha256);

public static class Generation
{
    public static async Task<bool> RunAsync(Settings settings, Func<Settings, Draft, Task<Review>> review)
    {
        OllamaReview.ValidateModelIdentity(settings);
        var root = Path.GetFullPath(settings.Root);
        var output = Path.GetFullPath(settings.Output);
        Configuration.Require(!Directory.Exists(output) && !File.Exists(output), "Output already exists; choose a new directory.");
        Configuration.Require(!Inside(root, output), "Output must be outside the inspected checkout.");
        var parent = Path.GetDirectoryName(output)!;
        Configuration.Require(Directory.Exists(parent), "Output parent must already exist.");
        CheckLinks(parent);
        OllamaReview.Endpoint(settings.OllamaUrl);
        Configuration.Require(await Git(root, "rev-parse", "HEAD") == settings.Revision, "Checkout HEAD differs from the requested revision.");
        var manifest = await ReadPinned(root, settings.Manifest);
        var model = Toml.ToModel(manifest.Text);
        var build = Configuration.Table(Configuration.Table(model, "build"), settings.Build);
        var buildPath = Configuration.Text(build, "path");
        var project = await ReadPinned(root, buildPath);
        var schema = await File.ReadAllTextAsync(settings.Schema);
        var draft = Configuration.Create(manifest.Text, project.Text, settings.Build, settings.Repository, settings.Revision, schema);
        var verdict = await review(settings, draft);
        Configuration.Require(verdict.Model == settings.Model && verdict.Digest == settings.ModelDigest,
            "Review must identify the caller-approved model tag and digest.");
        Configuration.Require(await ReadPinned(root, settings.Manifest) == manifest
            && await ReadPinned(root, buildPath) == project
            && await Git(root, "rev-parse", "HEAD") == settings.Revision, "Source changed during review.");
        var valid = verdict.Accepted;
        var report = JsonSerializer.Serialize(new
        {
            schema = "projectbuddy/source-config@1",
            configurationValid = valid,
            nativeBuildProven = false,
            deterministicChecksPassed = true,
            repository = settings.Repository,
            revision = settings.Revision,
            selectedBuild = settings.Build,
            sourceFiles = new Dictionary<string, string> { [settings.Manifest] = manifest.Sha256, [buildPath] = project.Sha256 },
            schemaSha256 = Configuration.Hash(schema),
            outputs = new Dictionary<string, string> { ["pipeline.json"] = Configuration.Hash(draft.PipelineJson), ["vipm.toml"] = Configuration.Hash(draft.VipmToml) },
            provenance = new
            {
                projectIdentityAndVersions = settings.Manifest,
                buildSpecificationPath = buildPath,
                sbomPath = draft.SbomPath,
                artifactPath = draft.ArtifactPath,
                runtimeAndCleanupPolicy = "native-windows; cleanup disabled"
            },
            approvedOllama = new { model = settings.Model, digest = settings.ModelDigest },
            reviewBudget = settings.Budget,
            reviewPrivacy = settings.Privacy,
            reviewLifecycle = settings.Lifecycle,
            ollama = verdict
        }, new JsonSerializerOptions { WriteIndented = true }) + "\n";
        Configuration.Require(!Directory.Exists(output) && !File.Exists(output), "Output appeared during review; refusing to write.");
        settings.Privacy.Validate();
        CreatePrivateDirectory(output);
        CheckLinks(output);
        foreach (var file in new Dictionary<string, string>
        {
            ["pipeline.json"] = draft.PipelineJson,
            ["vipm.toml"] = draft.VipmToml,
            ["evidence.json"] = report
        })
        {
            await using var stream = new FileStream(Path.Combine(output, file.Key), FileMode.CreateNew, FileAccess.Write, FileShare.None);
            ProtectPrivateFile(Path.Combine(output, file.Key));
            await stream.WriteAsync(Encoding.UTF8.GetBytes(file.Value));
        }
        return valid;
    }

    private static void CreatePrivateDirectory(string path)
    {
        if (OperatingSystem.IsWindows())
        {
            using var identity = WindowsIdentity.GetCurrent();
            var user = identity.User ?? throw new InvalidDataException("Current-user SID is unavailable.");
            var security = new DirectorySecurity();
            security.SetOwner(user);
            security.SetAccessRuleProtection(true, false);
            foreach (var sid in new[] { user, new SecurityIdentifier(WellKnownSidType.LocalSystemSid, null) })
                security.AddAccessRule(new FileSystemAccessRule(sid, FileSystemRights.FullControl,
                    InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit, PropagationFlags.None, AccessControlType.Allow));
            new DirectoryInfo(path).Create(security);
        }
        else
            Directory.CreateDirectory(path, UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute);
        CheckLinks(path);
        VerifyPrivate(path, true);
    }

    private static void ProtectPrivateFile(string path)
    {
        CheckLinks(path);
        if (OperatingSystem.IsWindows())
        {
            using var identity = WindowsIdentity.GetCurrent();
            var security = new FileInfo(path).GetAccessControl(AccessControlSections.Access | AccessControlSections.Owner);
            security.SetOwner(identity.User ?? throw new InvalidDataException("Current-user SID is unavailable."));
            new FileInfo(path).SetAccessControl(security);
        }
        else
            File.SetUnixFileMode(path, UnixFileMode.UserRead | UnixFileMode.UserWrite);
        VerifyPrivate(path, false);
    }

    internal static void VerifyPrivate(string path, bool directory)
    {
        if (OperatingSystem.IsWindows())
        {
            using var identity = WindowsIdentity.GetCurrent();
            var user = identity.User ?? throw new InvalidDataException("Current-user SID is unavailable.");
            FileSystemSecurity security = directory
                ? new DirectoryInfo(path).GetAccessControl(AccessControlSections.Access | AccessControlSections.Owner)
                : new FileInfo(path).GetAccessControl(AccessControlSections.Access | AccessControlSections.Owner);
            var rules = security.GetAccessRules(true, true, typeof(SecurityIdentifier)).Cast<FileSystemAccessRule>().ToArray();
            var allowed = new[] { user.Value, new SecurityIdentifier(WellKnownSidType.LocalSystemSid, null).Value }.Distinct().ToArray();
            var identities = new HashSet<string>();
            var valid = security.GetOwner(typeof(SecurityIdentifier))!.Equals(user) && rules.Length == allowed.Length;
            foreach (var rule in rules)
            {
                identities.Add(rule.IdentityReference.Value);
                valid &= allowed.Contains(rule.IdentityReference.Value) && rule.AccessControlType == AccessControlType.Allow
                    && (rule.FileSystemRights & FileSystemRights.FullControl) == FileSystemRights.FullControl;
            }
            Configuration.Require(valid && identities.Count == allowed.Length, "Generated evidence requires exact current-user-plus-SYSTEM access.");
        }
        else
        {
            var expected = UnixFileMode.UserRead | UnixFileMode.UserWrite | (directory ? UnixFileMode.UserExecute : 0);
            Configuration.Require(File.GetUnixFileMode(path) == expected, "Generated evidence requires private custody.");
        }
    }

    public static async Task<InputFile> ReadPinned(string root, string relative)
    {
        Configuration.Require(!Path.IsPathRooted(relative) && !relative.Contains('\\') && !relative.Contains(':')
            && !relative.Split('/').Any(part => part is "" or "." or ".."), "Source path must be checkout-relative.");
        var path = Path.GetFullPath(Path.Combine(root, relative));
        Configuration.Require(Inside(root, path) && File.Exists(path), "Source file is missing or escapes the checkout.");
        CheckLinks(path);
        Configuration.Require(new FileInfo(path).Length <= 8 * 1024 * 1024, "Source metadata exceeds the size limit.");
        var expected = await Git(root, "rev-parse", "--verify", $"HEAD:{relative}");
        var bytes = await File.ReadAllBytesAsync(path);
        Configuration.Require(bytes.Length <= 8 * 1024 * 1024, "Source metadata exceeds the size limit.");
        var text = new UTF8Encoding(false, true).GetString(bytes);
        var canonicalBytes = bytes;
        var actual = GitBlobHash(bytes, expected.Length == 64);
        if (actual != expected)
        {
            canonicalBytes = Encoding.UTF8.GetBytes(text.Replace("\r\n", "\n", StringComparison.Ordinal));
            actual = GitBlobHash(canonicalBytes, expected.Length == 64);
        }
        Configuration.Require(expected == actual, $"Source metadata differs from Git: {relative}.");
        var hash = Convert.ToHexStringLower(SHA256.HashData(canonicalBytes));
        return new InputFile(text.TrimStart('\uFEFF'), hash);
    }

    private static string GitBlobHash(byte[] bytes, bool sha256)
    {
        var blob = Encoding.ASCII.GetBytes($"blob {bytes.Length}\0").Concat(bytes).ToArray();
        return Convert.ToHexStringLower(sha256 ? SHA256.HashData(blob) : SHA1.HashData(blob));
    }

    public static bool Inside(string root, string path)
    {
        var relative = Path.GetRelativePath(Path.GetFullPath(root), Path.GetFullPath(path));
        return relative != ".." && !relative.StartsWith(".." + Path.DirectorySeparatorChar, StringComparison.Ordinal)
            && !Path.IsPathRooted(relative);
    }

    public static void CheckLinks(string path)
    {
        for (var current = Path.GetFullPath(path); current is not null; current = Path.GetDirectoryName(current))
            Configuration.Require((File.GetAttributes(current) & FileAttributes.ReparsePoint) == 0, "Symlinks and reparse points are not supported.");
    }

    public static async Task<string> Git(string root, params string[] arguments)
    {
        var start = new ProcessStartInfo("git") { RedirectStandardOutput = true, RedirectStandardError = true, UseShellExecute = false };
        foreach (var argument in new[] { "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", root }.Concat(arguments))
            start.ArgumentList.Add(argument);
        using var process = Process.Start(start) ?? throw new IOException("Could not start Git.");
        using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(30));
        var stdout = process.StandardOutput.ReadToEndAsync(timeout.Token);
        var stderr = process.StandardError.ReadToEndAsync(timeout.Token);
        try
        {
            await process.WaitForExitAsync(timeout.Token);
            await Task.WhenAll(stdout, stderr).WaitAsync(timeout.Token);
        }
        catch (OperationCanceledException)
        {
            if (!process.HasExited) process.Kill(entireProcessTree: true);
            throw new IOException("Git exceeded its time limit.");
        }
        Configuration.Require(process.ExitCode == 0, "Git could not verify the requested source metadata.");
        return (await stdout).Trim();
    }
}