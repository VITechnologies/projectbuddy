using System.Text.Json;
using ProjectBuddy.SourceConfig;
using Tomlyn;
using Tomlyn.Model;
using Xunit;

namespace ProjectBuddy.SourceConfig.Tests;

public class ConfigurationTests
{
    public const string Manifest = """
        [project]
        name = "fixture-project"
        version = "0.1.0"
        labview-version = "2026"
        labview-bitness = "64"
        [dependencies]
        fixture_package = "1.2.3.4"
        [build.ppl]
        type = "lvproj"
        path = "src/fixture.lvproj"
        target = "My Computer"
        spec = "Fixture PPL"
        [build.unrelated]
        type = "vipb"
        path = "unrelated.vipb"
        """;
    public const string Xml = """
        <Project><Item Name="My Computer"><Item Type="Build">
        <Item Name="Fixture PPL" Type="Packed Library">
        <Property Name="Bld_sbomFile">/C/Public/SBOMs/fixture.json</Property>
        <Property Name="Bld_localDestDir">/C/Public/builds</Property>
        <Property Name="Destination[0].destName">fixture.lvlibp</Property>
        <Property Name="TgtF_targetfileName">fixture.lvlibp</Property>
        </Item></Item></Item></Project>
        """;
    public static readonly string Revision = new('a', 40);
    public static readonly string Schema = File.ReadAllText(Path.Combine(AppContext.BaseDirectory, "pipeline-config.schema.json"));

    [Fact]
    public void DerivesSchemaValidConfigurationAndPreservesSelectedIntent()
    {
        var draft = Create();
        using var pipeline = JsonDocument.Parse(draft.PipelineJson);
        Assert.False(pipeline.RootElement.GetProperty("cleanOutputsBeforeRun").GetBoolean());
        var project = pipeline.RootElement.GetProperty("projects")[0];
        Assert.Equal(Revision, project.GetProperty("revision").GetString());
        Assert.Equal("C:\\Public\\builds\\fixture.lvlibp", project.GetProperty("build").GetProperty("artifactPath").GetString());
        Assert.Equal("C:\\Public\\SBOMs\\fixture.json", project.GetProperty("build").GetProperty("sbomPath").GetString());
        Assert.Equal("C:\\Public\\builds\\fixture.lvlibp", draft.ArtifactPath);
        var manifest = Toml.ToModel(draft.VipmToml);
        Assert.Single((TomlTable)manifest["build"]);
        Assert.Equal("1.2.3.4", ((TomlTable)manifest["dependencies"])["fixture_package"]);
        Assert.Equal(draft, Create());
    }

    [Theory]
    [InlineData("labview-bitness = \"64\"", "", "bitness")]
    [InlineData("1.2.3.4", ">=1.2", "exact version")]
    [InlineData("src/fixture.lvproj", "../escape.lvproj", "relative")]
    public void MissingOrAmbiguousFactsAreNotInvented(string before, string after, string error)
    {
        Assert.Contains(error, Assert.Throws<InvalidDataException>(() => Create(Manifest.Replace(before, after))).Message);
    }

    [Fact]
    public void RejectsWrongBuildKind()
    {
        Assert.Throws<InvalidDataException>(() => Create(xml: Xml.Replace("Packed Library", "Source Distribution")));
    }

    [Fact]
    public void RejectsMissingOutputMetadata()
    {
        Assert.Throws<InvalidDataException>(() => Create(xml: Xml.Replace("Bld_sbomFile", "missing")));
    }

    [Theory]
    [InlineData("fixture.lvlibp", "other.lvlibp")]
    [InlineData("../outside.lvlibp", "../outside.lvlibp")]
    [InlineData("fixture.txt", "fixture.txt")]
    public void RejectsAmbiguousOrUnsafePplArtifactNames(string destinationName, string targetFileName)
    {
        var xml = Xml.Replace("<Property Name=\"Destination[0].destName\">fixture.lvlibp</Property>",
                $"<Property Name=\"Destination[0].destName\">{destinationName}</Property>")
            .Replace("<Property Name=\"TgtF_targetfileName\">fixture.lvlibp</Property>",
                $"<Property Name=\"TgtF_targetfileName\">{targetFileName}</Property>");
        Assert.Throws<InvalidDataException>(() => Create(xml: xml));
    }

    [Theory]
    [InlineData("https://user:password@example.org/repo.git", "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")]
    [InlineData("https://example.org/repo.git", "main")]
    public void RequiresSafeRepositoryAndImmutableRevision(string repository, string revision)
    {
        Assert.Throws<InvalidDataException>(() => Configuration.Create(Manifest, Xml, "ppl", repository, revision, Schema));
    }

    public static Draft Create(string manifest = Manifest, string xml = Xml) =>
        Configuration.Create(manifest, xml, "ppl", "https://example.org/fixture.git", Revision, Schema);
}