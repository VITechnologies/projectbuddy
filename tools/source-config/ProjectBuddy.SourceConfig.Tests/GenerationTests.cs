using System.Text.Json;
using ProjectBuddy.SourceConfig;
using Xunit;

namespace ProjectBuddy.SourceConfig.Tests;

public class GenerationTests
{
    [Theory]
    [InlineData(true)]
    [InlineData(false)]
    public async Task ReviewIsRequiredAndNeverProvesANativeBuild(bool accepted)
    {
        var settings = await Fixture();
        var valid = await Generation.RunAsync(settings, (_, _) => Task.FromResult(Verdict(accepted)));
        Assert.Equal(accepted, valid);
        using var report = JsonDocument.Parse(await File.ReadAllTextAsync(Path.Combine(settings.Output, "evidence.json")));
        Assert.Equal(accepted, report.RootElement.GetProperty("configurationValid").GetBoolean());
        Assert.False(report.RootElement.GetProperty("nativeBuildProven").GetBoolean());
        var digest = Configuration.Hash(await File.ReadAllTextAsync(Path.Combine(settings.Output, "pipeline.json")));
        Assert.Equal(digest, report.RootElement.GetProperty("outputs").GetProperty("pipeline.json").GetString());
    }

    [Theory]
    [InlineData("modified")]
    [InlineData("untracked")]
    [InlineData("revision")]
    [InlineData("existing-output")]
    public async Task InvalidInputsNeverReachTheModel(string problem)
    {
        var settings = await Fixture();
        if (problem == "modified") await File.AppendAllTextAsync(Path.Combine(settings.Root, settings.Manifest), "\n# changed");
        if (problem == "untracked")
        {
            await File.WriteAllTextAsync(Path.Combine(settings.Root, "untracked.toml"), ConfigurationTests.Manifest);
            settings = settings with { Manifest = "untracked.toml" };
        }
        if (problem == "revision") settings = settings with { Revision = new string('b', 40) };
        if (problem == "existing-output") Directory.CreateDirectory(settings.Output);
        await Assert.ThrowsAsync<InvalidDataException>(() => Generation.RunAsync(settings, (_, _) => throw new Exception("must not reach model")));
        Assert.False(File.Exists(Path.Combine(settings.Output, "pipeline.json")));
    }

    [Fact]
    public async Task UnavailableModelLeavesNoConfiguration()
    {
        var settings = await Fixture();
        await Assert.ThrowsAsync<HttpRequestException>(() => Generation.RunAsync(settings, (_, _) => throw new HttpRequestException("fixture unavailable")));
        Assert.False(Directory.Exists(settings.Output));
    }

    [Fact]
    public async Task SourceMutationDuringReviewIsRejected()
    {
        var settings = await Fixture();
        await Assert.ThrowsAsync<InvalidDataException>(() => Generation.RunAsync(settings, async (_, _) =>
        {
            await File.AppendAllTextAsync(Path.Combine(settings.Root, settings.Manifest), "\n# changed during review");
            return Verdict(true);
        }));
        Assert.False(Directory.Exists(settings.Output));
    }

    [Theory]
    [InlineData("https://example.org")]
    [InlineData("http://localhost:11435")]
    [InlineData("http://127.0.0.1/path")]
    [InlineData("http://user:pass@127.0.0.1")]
    [InlineData("http://127.0.0.1?query=1")]
    public void RefusesRemoteOrAmbiguousReviewEndpoints(string value)
    {
        Assert.Throws<InvalidDataException>(() => OllamaReview.Endpoint(value));
    }

    [Theory]
    [InlineData("accept", true)]
    [InlineData("reject", false)]
    [InlineData("inconclusive", false)]
    public void OnlyAnExplicitAcceptCanPass(string decision, bool accepted)
    {
        var content = JsonSerializer.Serialize(new { decision, blockers = Array.Empty<string>() });
        using var response = JsonDocument.Parse(JsonSerializer.Serialize(new
        {
            model = "fixture:1", done = true, done_reason = "stop", message = new { content }
        }));
        Assert.Equal(accepted, OllamaReview.Parse(response.RootElement, "fixture:1", new string('a', 64)).Accepted);
    }

    [Fact]
    public void TruncatedReviewsCannotPass()
    {
        using var response = JsonDocument.Parse("""{"model":"fixture:1","done":true,"done_reason":"length"}""");
        Assert.Throws<InvalidDataException>(() => OllamaReview.Parse(response.RootElement, "fixture:1", new string('a', 64)));
    }

    [Fact]
    public void ContradictoryAcceptWithBlockersCannotPass()
    {
        var review = new Review("accept", ["unresolved fact"], "fixture:1", new string('a', 64));
        Assert.False(review.Accepted);
    }

    public static Review Verdict(bool accepted) => new(accepted ? "accept" : "reject", accepted ? [] : ["fixture blocker"], "fixture:1", new string('a', 64));

    public static async Task<Settings> Fixture()
    {
        var directory = Path.Combine(Path.GetTempPath(), "projectbuddy-source-config-" + Guid.NewGuid().ToString("N"));
        var root = Path.Combine(directory, "source");
        Directory.CreateDirectory(Path.Combine(root, "src"));
        await File.WriteAllTextAsync(Path.Combine(root, "vipm.toml"), ConfigurationTests.Manifest);
        await File.WriteAllTextAsync(Path.Combine(root, "src/fixture.lvproj"), ConfigurationTests.Xml);
        await Generation.Git(root, "init");
        await Generation.Git(root, "add", "vipm.toml", "src/fixture.lvproj");
        await Generation.Git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false", "commit", "-m", "Fixture");
        var revision = await Generation.Git(root, "rev-parse", "HEAD");
        return new Settings(root, "vipm.toml", "ppl", "https://example.org/fixture.git", revision,
            Path.Combine(AppContext.BaseDirectory, "pipeline-config.schema.json"), Path.Combine(directory, "generated"),
            "http://127.0.0.1:11435", "fixture:1");
    }
}