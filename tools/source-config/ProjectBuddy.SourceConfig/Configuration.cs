using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using System.Xml;
using System.Xml.Linq;
using Json.Schema;
using Tomlyn;
using Tomlyn.Model;

namespace ProjectBuddy.SourceConfig;

public sealed record Draft(string PipelineJson, string VipmToml, string BuildPath, string SbomPath,
    string ArtifactPath, string ProjectName, string BuildSpec, string LabviewVersion, string LabviewBitness);

public static class Configuration
{
    public static Draft Create(string manifestText, string xmlText, string buildKey,
        string repository, string revision, string schemaText)
    {
        Require(Regex.IsMatch(revision, "\\A[0-9a-f]{40}(?:[0-9a-f]{24})?\\z"), "An immutable Git revision is required.");
        Require(Uri.TryCreate(repository, UriKind.Absolute, out var remote)
            && remote.Scheme == "https" && remote.UserInfo.Length == 0
            && remote.Query.Length == 0 && remote.Fragment.Length == 0, "A credential-free HTTPS repository URL is required.");
        var manifest = Toml.ToModel(manifestText);
        var project = Table(manifest, "project");
        var name = Text(project, "name");
        var version = Text(project, "labview-version");
        var bitness = Text(project, "labview-bitness");
        Require(Regex.IsMatch(version, "\\A[0-9]{4}\\z") && bitness is "32" or "64", "Explicit LabVIEW year and bitness are required.");
        foreach (var dependency in Table(manifest, "dependencies"))
            Require(dependency.Value is string value && Regex.IsMatch(value, "\\A[0-9]+(?:\\.[0-9]+){1,3}(?:[-+][A-Za-z0-9.-]+)?\\z"),
                $"Dependency {dependency.Key} must declare an exact version.");
        var build = Table(Table(manifest, "build"), buildKey);
        Require(Text(build, "type") == "lvproj", "The selected build must be an lvproj target.");
        Require(!build.ContainsKey("depends-on"), "Build prerequisites need an explicit multi-build profile; this slice supports a standalone PPL.");
        var buildPath = Text(build, "path");
        Require(!Path.IsPathRooted(buildPath) && !buildPath.Contains('\\')
            && !buildPath.Contains(':') && !buildPath.Split('/').Any(part => part is "" or "." or ".."), "Build path must be checkout-relative.");
        var target = Text(build, "target");
        var spec = Text(build, "spec");
        using var reader = XmlReader.Create(new StringReader(xmlText), new XmlReaderSettings
        {
            DtdProcessing = DtdProcessing.Prohibit,
            XmlResolver = null,
            MaxCharactersInDocument = 8 * 1024 * 1024
        });
        var document = XDocument.Load(reader);
        var matches = document.Root?.Elements("Item").Where(item => (string?)item.Attribute("Name") == target)
            .SelectMany(item => item.Elements("Item").Where(group => (string?)group.Attribute("Type") == "Build"))
            .SelectMany(group => group.Elements("Item").Where(item => (string?)item.Attribute("Name") == spec)).ToArray() ?? [];
        Require(matches.Length == 1 && (string?)matches[0].Attribute("Type") == "Packed Library", "Select exactly one existing packed-library build specification.");
        string TextProperty(string propertyName)
        {
            var values = matches[0].Elements("Property").Where(item => (string?)item.Attribute("Name") == propertyName).ToArray();
            Require(values.Length == 1 && !string.IsNullOrWhiteSpace(values[0].Value), $"Missing or ambiguous build metadata: {propertyName}.");
            return values[0].Value;
        }
        var sbom = WindowsPath(TextProperty("Bld_sbomFile"));
        var artifactDirectory = WindowsPath(TextProperty("Bld_localDestDir"));
        var destinationName = TextProperty("Destination[0].destName");
        var targetFileName = TextProperty("TgtF_targetfileName");
        Require(destinationName == targetFileName, "PPL destination and target filenames must agree.");
        Require(Path.GetFileName(destinationName) == destinationName
            && Regex.IsMatch(destinationName, "\\A[A-Za-z0-9][A-Za-z0-9._+-]*\\.lvlibp\\z", RegexOptions.IgnoreCase),
            "PPL target filename must be a plain .lvlibp filename.");
        var artifactPath = artifactDirectory.TrimEnd('\\') + "\\" + destinationName;
        Require(sbom.EndsWith(".json", StringComparison.OrdinalIgnoreCase), "Build SBOM path must identify a JSON file.");
        var pipeline = JsonSerializer.Serialize(new
        {
            workingDirectoryBase = "workspace",
            cleanOutputsBeforeRun = false,
            supportedRuntimes = new[] { "native-windows" },
            projects = new[] { new { name, enabled = true, vcs = "git", revision, repoUrl = repository,
                labviewVersion = version, labviewBitness = bitness,
                build = new { sbomPath = sbom, artifactPath } } }
        }, new JsonSerializerOptions { WriteIndented = true }) + "\n";
        var result = JsonSchema.FromText(schemaText).Evaluate(JsonNode.Parse(pipeline));
        Require(result.IsValid, "Generated pipeline JSON does not satisfy the supplied schema.");
        manifest["build"] = new TomlTable { [buildKey] = build };
        return new Draft(pipeline, Toml.FromModel(manifest).Replace("\r\n", "\n"), buildPath, sbom,
            artifactPath, name, spec, version, bitness);
    }

    public static string WindowsPath(string value)
    {
        Require(Regex.IsMatch(value, "\\A/[A-Za-z]/") && !value.Contains("NI_AB_", StringComparison.Ordinal)
            && !value.Contains('$') && !value.Contains('%')
            && !value.Split('/').Any(part => part is "." or ".."), "Build output path must be an explicit absolute Windows path.");
        return char.ToUpperInvariant(value[1]) + ":\\" + value[3..].Replace('/', '\\');
    }

    public static string Hash(string text) => Convert.ToHexStringLower(SHA256.HashData(Encoding.UTF8.GetBytes(text)));

    public static TomlTable Table(TomlTable parent, string key)
    {
        Require(parent.TryGetValue(key, out var value) && value is TomlTable, $"Missing table: {key}.");
        return (TomlTable)value!;
    }

    public static string Text(TomlTable parent, string key)
    {
        Require(parent.TryGetValue(key, out var value) && value is string text && !string.IsNullOrWhiteSpace(text), $"Missing string: {key}.");
        return (string)value!;
    }

    public static void Require(bool condition, string message)
    {
        if (!condition) throw new InvalidDataException(message);
    }
}