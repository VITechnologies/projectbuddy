using System.Net;
using System.Diagnostics;
using System.Text;
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
        var pinnedManifest = await Generation.ReadPinned(settings.Root, settings.Manifest);
        var canonicalManifest = ConfigurationTests.Manifest.Replace("\r\n", "\n", StringComparison.Ordinal);
        Assert.Equal(Configuration.Hash(canonicalManifest), pinnedManifest.Sha256);
        var valid = await Generation.RunAsync(settings, (_, _) => Task.FromResult(Verdict(accepted)));
        Assert.Equal(accepted, valid);
        using var report = JsonDocument.Parse(await File.ReadAllTextAsync(Path.Combine(settings.Output, "evidence.json")));
        Assert.Equal(accepted, report.RootElement.GetProperty("configurationValid").GetBoolean());
        Assert.False(report.RootElement.GetProperty("nativeBuildProven").GetBoolean());
        Assert.Equal(settings.ModelDigest, report.RootElement.GetProperty("approvedOllama").GetProperty("digest").GetString());
        Assert.Equal(settings.ModelDigest, report.RootElement.GetProperty("ollama").GetProperty("Digest").GetString());
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
    public async Task UnapprovedReviewIdentityLeavesNoConfiguration()
    {
        var settings = await Fixture();
        await Assert.ThrowsAsync<InvalidDataException>(() => Generation.RunAsync(settings,
            (_, _) => Task.FromResult(Verdict(true) with { Digest = new string('b', 64) })));
        Assert.False(Directory.Exists(settings.Output));
    }

    [Theory]
    [InlineData("")]
    [InlineData("abc")]
    [InlineData("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")]
    [InlineData("gggggggggggggggggggggggggggggggggggggggggggggggggggggggggggggggg")]
    public async Task InvalidModelApprovalNeverReachesReview(string digest)
    {
        var settings = await Fixture();
        settings = settings with { ModelDigest = digest };
        await Assert.ThrowsAsync<InvalidDataException>(() => Generation.RunAsync(settings,
            (_, _) => throw new Exception("must not reach model")));
        Assert.False(Directory.Exists(settings.Output));
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
    [InlineData("missing-models")]
    [InlineData("not-object")]
    [InlineData("not-array")]
    [InlineData("null-entry")]
    [InlineData("missing-name")]
    [InlineData("missing-digest")]
    [InlineData("non-string-digest")]
    [InlineData("short-digest")]
    [InlineData("non-hex-digest")]
    [InlineData("duplicate-model")]
    [InlineData("duplicate-name")]
    [InlineData("duplicate-digest")]
    [InlineData("duplicate-inventory")]
    [InlineData("not-installed")]
    [InlineData("mismatched-digest")]
    [InlineData("alias-mismatch")]
    public async Task InventoryErrorsSendNoEvidence(string problem)
    {
        var settings = await Fixture();
        using var handler = new ReviewHttpHandler(InvalidInventory(problem));
        await Assert.ThrowsAsync<InvalidDataException>(() => Generation.RunAsync(settings,
            (approved, draft) => OllamaReview.RunAsync(approved, draft, handler)));
        Assert.Equal(0, handler.InferenceRequests);
        Assert.Equal(1, handler.InventoryRequests);
        Assert.False(Directory.Exists(settings.Output));
        Assert.Equal(new string('a', 64), settings.ModelDigest);
    }

    [Fact]
    public async Task MalformedInventoryJsonSendsNoEvidence()
    {
        var settings = await Fixture();
        using var handler = new ReviewHttpHandler("{invalid-json");
        await Assert.ThrowsAnyAsync<JsonException>(() => Generation.RunAsync(settings,
            (approved, draft) => OllamaReview.RunAsync(approved, draft, handler)));
        Assert.Equal(0, handler.InferenceRequests);
        Assert.False(Directory.Exists(settings.Output));
    }

    [Fact]
    public async Task UnavailablePreflightSendsNoEvidence()
    {
        var settings = await Fixture();
        using var handler = new ReviewHttpHandler(Inventory(), unavailablePreflight: true);
        await Assert.ThrowsAsync<HttpRequestException>(() => Generation.RunAsync(settings,
            (approved, draft) => OllamaReview.RunAsync(approved, draft, handler)));
        Assert.Equal(0, handler.InferenceRequests);
        Assert.False(Directory.Exists(settings.Output));
    }

    [Fact]
    public async Task ApprovedCpuOptionsAreHashedBeforeActualSubmission()
    {
        var settings = await Fixture();
        settings = settings with { Execution = settings.Execution with { Threads = 2 } };
        using var handler = new ReviewHttpHandler(Inventory());
        Assert.True(await Generation.RunAsync(settings, (approved, draft) => OllamaReview.RunAsync(approved, draft, handler)));
        var body = Assert.Single(handler.InferenceBodies);
        using var request = JsonDocument.Parse(body);
        Assert.Equal(0, request.RootElement.GetProperty("options").GetProperty("num_gpu").GetInt32());
        Assert.Equal(2, request.RootElement.GetProperty("options").GetProperty("num_thread").GetInt32());
        using var evidence = JsonDocument.Parse(await File.ReadAllTextAsync(Path.Combine(settings.Output, "evidence.json")));
        Assert.Equal(2, evidence.RootElement.GetProperty("reviewExecution").GetProperty("Threads").GetInt32());
        Assert.Equal(Configuration.Hash(body), evidence.RootElement.GetProperty("ollama").GetProperty("RequestBudget").GetProperty("RequestSha256").GetString());
    }

    [Theory]
    [InlineData("missing")]
    [InlineData("zero")]
    [InlineData("negative")]
    [InlineData("excessive")]
    [InlineData("mode")]
    [InlineData("digest")]
    public async Task UnsupportedExecutionPolicyNeverContactsTheModel(string problem)
    {
        var settings = await Fixture();
        settings = settings with { Execution = problem switch
        {
            "missing" => null!,
            "zero" => settings.Execution with { Threads = 0 },
            "negative" => settings.Execution with { Threads = -1 },
            "excessive" => settings.Execution with { Threads = 9 },
            "mode" => settings.Execution with { Mode = "automatic" },
            _ => settings.Execution with { ModelDigest = new string('b', 64) }
        }};
        using var handler = new ReviewHttpHandler(Inventory());
        await Assert.ThrowsAsync<InvalidDataException>(() => Generation.RunAsync(settings,
            (approved, draft) => OllamaReview.RunAsync(approved, draft, handler)));
        Assert.Empty(handler.RequestPaths);
        Assert.False(Directory.Exists(settings.Output));
    }

    [Theory]
    [InlineData("{\"schema\":\"projectbuddy/ollama-execution@1\",\"modelDigest\":\"fixture\",\"mode\":\"cpu-only\",\"threads\":true}")]
    [InlineData("{\"schema\":\"projectbuddy/ollama-execution@1\",\"modelDigest\":\"fixture\",\"mode\":\"cpu-only\",\"threads\":1,\"num_gpu\":1}")]
    public async Task UnknownOrBooleanExecutionControlsAreRefused(string policy)
    {
        var settings = await Fixture();
        var path = Path.Combine(Path.GetDirectoryName(settings.Root)!, "execution.json");
        await File.WriteAllTextAsync(path, policy);
        var error = Record.Exception(() => ReviewExecutionPolicy.Read(path));
        Assert.True(error is JsonException or InvalidDataException);
        Assert.False(Directory.Exists(settings.Output));
    }

    [Fact]
    public async Task CancelledReviewStartsNoHttpRequest()
    {
        var settings = await Fixture();
        using var cancellation = new CancellationTokenSource();
        cancellation.Cancel();
        using var handler = new ReviewHttpHandler(Inventory());
        await Assert.ThrowsAsync<ReviewCancelledException>(() => Generation.RunAsync(settings,
            (approved, draft) => OllamaReview.RunAsync(approved, draft, handler, cancellation.Token)));
        Assert.Empty(handler.RequestPaths);
        Assert.False(Directory.Exists(settings.Output));
    }

    [Theory]
    [InlineData(true)]
    [InlineData(false)]
    public async Task MonotonicReviewDeadlineStopsSlowPreflight(bool overall)
    {
        var settings = await Fixture();
        settings = settings with { Lifecycle = settings.Lifecycle with { TotalSeconds = overall ? 1 : 10, RequestSeconds = 1 } };
        var elapsed = 0.0;
        using var handler = new ReviewHttpHandler(Inventory(), onRequest: _ => elapsed += 2);
        await Assert.ThrowsAsync<ReviewDeadlineException>(() => Generation.RunAsync(settings,
            (approved, draft) => OllamaReview.RunAsync(approved, draft, handler, elapsed: () => elapsed)));
        Assert.Single(handler.RequestPaths);
        Assert.Equal(0, handler.InferenceRequests);
        Assert.False(Directory.Exists(settings.Output));
    }

    [Fact]
    public async Task CancelledRequestCannotProceedToInference()
    {
        var settings = await Fixture();
        using var cancellation = new CancellationTokenSource();
        using var handler = new ReviewHttpHandler(Inventory(), onRequest: _ => cancellation.Cancel());
        await Assert.ThrowsAsync<ReviewCancelledException>(() => Generation.RunAsync(settings,
            (approved, draft) => OllamaReview.RunAsync(approved, draft, handler, cancellation.Token)));
        Assert.Single(handler.RequestPaths);
        Assert.False(Directory.Exists(settings.Output));
    }

    [Fact]
    public async Task TransientRetryIsBoundedAndResidencyIsNotAnUnload()
    {
        var settings = await Fixture();
        settings = settings with { Lifecycle = settings.Lifecycle with { MaxRetries = 1, BackoffSeconds = 1 } };
        var failed = false;
        var elapsed = 0.0;
        using var handler = new ReviewHttpHandler(Inventory(), responseStatus: (path, _) =>
        {
            if (path == "/api/chat" && !failed) { failed = true; return HttpStatusCode.ServiceUnavailable; }
            return null;
        });
        Assert.True(await Generation.RunAsync(settings, (approved, draft) => OllamaReview.RunAsync(approved, draft, handler,
            elapsed: () => elapsed, delay: (duration, _) => { elapsed += duration.TotalSeconds; return Task.CompletedTask; })));
        Assert.Equal(2, handler.RequestPaths.Count(path => path == "/api/chat"));
        Assert.All(handler.Residencies, residency => Assert.Equal(30, residency));
        using var report = JsonDocument.Parse(await File.ReadAllTextAsync(Path.Combine(settings.Output, "evidence.json")));
        var lifecycle = report.RootElement.GetProperty("ollama").GetProperty("Lifecycle");
        Assert.Equal(5, lifecycle.GetProperty("RequestCount").GetInt32());
        Assert.Equal(1, lifecycle.GetProperty("RetryCount").GetInt32());
    }

    [Theory]
    [InlineData(503, 2)]
    [InlineData(403, 1)]
    public async Task RetriesDoNotWeakenRequestOrStatusPolicy(int status, int expected)
    {
        var settings = await Fixture();
        settings = settings with { Lifecycle = settings.Lifecycle with { MaxRetries = 1 } };
        using var handler = new ReviewHttpHandler(Inventory(), responseStatus: (path, _) => path == "/api/chat" ? (HttpStatusCode)status : null);
        await Assert.ThrowsAsync<HttpRequestException>(() => Generation.RunAsync(settings,
            (approved, draft) => OllamaReview.RunAsync(approved, draft, handler)));
        Assert.Equal(expected, handler.RequestPaths.Count(path => path == "/api/chat"));
        Assert.False(Directory.Exists(settings.Output));
    }

    [Fact]
    public async Task RequestBudgetCannotBeUsedToSkipPostflight()
    {
        var settings = await Fixture();
        settings = settings with { Lifecycle = settings.Lifecycle with { MaxRequests = 3 } };
        using var handler = new ReviewHttpHandler(Inventory());
        await Assert.ThrowsAsync<InvalidDataException>(() => Generation.RunAsync(settings,
            (approved, draft) => OllamaReview.RunAsync(approved, draft, handler)));
        Assert.Equal(3, handler.RequestPaths.Count);
        Assert.False(Directory.Exists(settings.Output));
    }

    [Fact]
    public async Task CancelledBackoffStartsNoRetry()
    {
        var settings = await Fixture();
        settings = settings with { Lifecycle = settings.Lifecycle with { MaxRetries = 2, BackoffSeconds = 1 } };
        using var cancellation = new CancellationTokenSource();
        using var handler = new ReviewHttpHandler(Inventory(), responseStatus: (path, _) => path == "/api/chat" ? HttpStatusCode.ServiceUnavailable : null);
        await Assert.ThrowsAsync<ReviewCancelledException>(() => Generation.RunAsync(settings,
            (approved, draft) => OllamaReview.RunAsync(approved, draft, handler, cancellation.Token,
                delay: (_, token) => { cancellation.Cancel(); return Task.FromCanceled(token); })));
        Assert.Single(handler.RequestPaths, path => path == "/api/chat");
        Assert.False(Directory.Exists(settings.Output));
    }

    [Fact]
    public async Task MalformedModelOutputDoesNotTriggerFormatRetry()
    {
        var settings = await Fixture();
        settings = settings with { Lifecycle = settings.Lifecycle with { MaxRetries = 3 } };
        using var handler = new ReviewHttpHandler(Inventory(), reviewContent: "{\"decision\":\"accept\"}");
        await Assert.ThrowsAsync<InvalidDataException>(() => Generation.RunAsync(settings,
            (approved, draft) => OllamaReview.RunAsync(approved, draft, handler)));
        Assert.Single(handler.RequestPaths, path => path == "/api/chat");
        Assert.False(Directory.Exists(settings.Output));
    }

    [Theory]
    [InlineData("time")]
    [InlineData("requests")]
    [InlineData("residency")]
    public async Task InvalidLifecycleNeverStartsAReview(string problem)
    {
        var settings = await Fixture();
        settings = settings with { Lifecycle = problem switch
        {
            "time" => settings.Lifecycle with { TotalSeconds = double.PositiveInfinity },
            "requests" => settings.Lifecycle with { MaxRequests = 0 },
            _ => settings.Lifecycle with { ResidencySeconds = 0 }
        }};
        await Assert.ThrowsAsync<InvalidDataException>(() => Generation.RunAsync(settings,
            (_, _) => throw new Exception("must not reach model")));
        Assert.False(Directory.Exists(settings.Output));
    }

    [Fact]
    public async Task GeneratedOutputsRequirePrivateCustody()
    {
        var settings = await Fixture();
        Assert.True(await Generation.RunAsync(settings, (_, _) => Task.FromResult(Verdict(true))));
        Generation.VerifyPrivate(settings.Output, true);
        foreach (var path in Directory.GetFiles(settings.Output)) Generation.VerifyPrivate(path, false);
        if (!OperatingSystem.IsWindows())
        {
            var evidence = Path.Combine(settings.Output, "evidence.json");
            File.SetUnixFileMode(evidence, UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.GroupRead);
            Assert.Throws<InvalidDataException>(() => Generation.VerifyPrivate(evidence, false));
        }
    }

    [Theory]
    [InlineData("expired")]
    [InlineData("images")]
    [InlineData("text-retention")]
    public async Task InvalidPrivacyNeverReachesReview(string problem)
    {
        var settings = await Fixture();
        settings = settings with { Privacy = problem switch
        {
            "expired" => settings.Privacy with { ExpiresAt = DateTimeOffset.Parse("2000-01-01T00:00:00Z") },
            "images" => settings.Privacy with { ImageMode = "full-frame" },
            _ => settings.Privacy with { RetainModelText = true }
        }};
        await Assert.ThrowsAsync<InvalidDataException>(() => Generation.RunAsync(settings,
            (_, _) => throw new Exception("must not reach model")));
        Assert.False(Directory.Exists(settings.Output));
    }

    [Fact]
    public void ModelProseIsNotRetainedInStructuredReview()
    {
        const string canary = "PRIVATE_CANARY_8471";
        var content = JsonSerializer.Serialize(new { decision = "reject", blockers = new[] { canary } });
        using var response = JsonDocument.Parse(JsonSerializer.Serialize(new
        {
            model = "fixture:1", done = true, done_reason = "stop", message = new { content }
        }));
        var review = OllamaReview.Parse(response.RootElement, "fixture:1", new string('a', 64));
        Assert.False(review.Accepted);
        Assert.Single(review.Blockers);
        Assert.DoesNotContain(canary, JsonSerializer.Serialize(review));
        Assert.Equal(64, review.ModelOutputSha256!.Length);
    }

    [Fact]
    public async Task ModelProseNeverReachesGeneratedEvidence()
    {
        var settings = await Fixture();
        const string canary = "PRIVATE_CANARY_8471";
        var content = JsonSerializer.Serialize(new { decision = "reject", blockers = new[] { canary } });
        using var handler = new ReviewHttpHandler(Inventory(), reviewContent: content);
        Assert.False(await Generation.RunAsync(settings, (approved, draft) => OllamaReview.RunAsync(approved, draft, handler)));
        Assert.Equal(1, handler.InferenceRequests);
        foreach (var path in Directory.GetFiles(settings.Output))
        {
            Assert.DoesNotContain(canary, await File.ReadAllTextAsync(path));
            Generation.VerifyPrivate(path, false);
        }
    }

    [Fact]
    public async Task CliDiagnosticsDoNotEchoPrivateInputPaths()
    {
        var settings = await Fixture();
        const string canary = "PRIVATE_CANARY_8471";
        var options = new Dictionary<string, string>
        {
            ["root"] = settings.Root, ["manifest"] = settings.Manifest, ["build"] = settings.Build,
            ["repository"] = settings.Repository, ["revision"] = settings.Revision, ["schema"] = settings.Schema,
            ["output"] = settings.Output, ["ollama-url"] = settings.OllamaUrl, ["model"] = settings.Model,
            ["model-digest"] = settings.ModelDigest, ["review-budget"] = Path.Combine(settings.Root, canary),
            ["review-privacy"] = "unused-fixture-path", ["review-lifecycle"] = "unused-fixture-path", ["review-execution"] = "unused-fixture-path"
        };
        var start = new ProcessStartInfo("dotnet") { RedirectStandardOutput = true, RedirectStandardError = true, UseShellExecute = false };
        start.ArgumentList.Add(typeof(Generation).Assembly.Location);
        foreach (var option in options)
        {
            start.ArgumentList.Add("--" + option.Key);
            start.ArgumentList.Add(option.Value);
        }
        using var process = Process.Start(start)!;
        var output = process.StandardOutput.ReadToEndAsync();
        var errors = process.StandardError.ReadToEndAsync();
        await process.WaitForExitAsync();
        Assert.Equal(1, process.ExitCode);
        Assert.Contains("source-config: refused (FileNotFoundException)", await errors);
        Assert.DoesNotContain(canary, await output + await errors);
    }

    [Fact]
    public async Task OverBudgetEvidenceSendsNoInferenceAndWritesNoConfiguration()
    {
        var settings = await Fixture();
        settings = settings with { Budget = settings.Budget with { MaxTextBytes = 10 } };
        using var handler = new ReviewHttpHandler(Inventory());
        await Assert.ThrowsAsync<InvalidDataException>(() => Generation.RunAsync(settings,
            (approved, draft) => OllamaReview.RunAsync(approved, draft, handler)));
        Assert.Equal(0, handler.InferenceRequests);
        Assert.False(Directory.Exists(settings.Output));
    }

    [Theory]
    [InlineData("tokenizer")]
    [InlineData("context")]
    [InlineData("template")]
    [InlineData("missing")]
    public async Task UnsupportedModelBudgetSendsNoInference(string problem)
    {
        var settings = await Fixture();
        var metadata = problem switch
        {
            "tokenizer" => "{\"template\":\"\",\"model_info\":{\"general.architecture\":\"fixture\",\"fixture.context_length\":32768,\"tokenizer.ggml.model\":\"unknown\"}}",
            "context" => "{\"template\":\"\",\"model_info\":{\"general.architecture\":\"fixture\",\"fixture.context_length\":1024,\"tokenizer.ggml.model\":\"gpt2\"}}",
            "template" => "{\"template\":\"" + new string('x', 2000) + "\",\"model_info\":{\"general.architecture\":\"fixture\",\"fixture.context_length\":32768,\"tokenizer.ggml.model\":\"gpt2\"}}",
            _ => "{}"
        };
        using var handler = new ReviewHttpHandler(Inventory(), metadataOverride: metadata);
        await Assert.ThrowsAsync<InvalidDataException>(() => Generation.RunAsync(settings,
            (approved, draft) => OllamaReview.RunAsync(approved, draft, handler)));
        Assert.Equal(0, handler.InferenceRequests);
        Assert.False(Directory.Exists(settings.Output));
    }

    [Theory]
    [InlineData(null, 16, "stop")]
    [InlineData(0, 16, "stop")]
    [InlineData(999999, 16, "stop")]
    [InlineData(64, 0, "stop")]
    [InlineData(64, 999999, "stop")]
    [InlineData(64, 16, "length")]
    public async Task IncompleteCompletionLeavesNoConfiguration(int? inputCount, int? outputCount, string reason)
    {
        var settings = await Fixture();
        using var handler = new ReviewHttpHandler(Inventory(), inputCount: inputCount, outputCount: outputCount, reason: reason);
        await Assert.ThrowsAsync<InvalidDataException>(() => Generation.RunAsync(settings,
            (approved, draft) => OllamaReview.RunAsync(approved, draft, handler)));
        Assert.Equal(1, handler.InferenceRequests);
        Assert.False(Directory.Exists(settings.Output));
    }

    [Fact]
    public async Task ReviewBudgetBoundaryAndEvidenceAreExplicit()
    {
        var settings = await Fixture();
        using var payload = JsonDocument.Parse("{\"format\":{},\"messages\":[{\"role\":\"user\",\"content\":\"fixture\"}]}");
        var measured = settings.Budget.Check(payload.RootElement);
        var exact = settings.Budget with { ContextTokens = measured.InputTokenUpperBound + settings.Budget.OutputTokens };
        exact.Validate(settings.ModelDigest);
        Assert.Equal(measured.InputTokenUpperBound, exact.Check(payload.RootElement).InputTokenUpperBound);
        Assert.Throws<InvalidDataException>(() => (exact with { ContextTokens = exact.ContextTokens - 1 }).Check(payload.RootElement));
        using var handler = new ReviewHttpHandler(Inventory());
        Assert.True(await Generation.RunAsync(settings, (approved, draft) => OllamaReview.RunAsync(approved, draft, handler)));
        using var evidence = JsonDocument.Parse(await File.ReadAllTextAsync(Path.Combine(settings.Output, "evidence.json")));
        var request = evidence.RootElement.GetProperty("ollama").GetProperty("RequestBudget");
        Assert.Equal(64, request.GetProperty("PolicySha256").GetString()!.Length);
        Assert.Equal("all-submitted-evidence; no truncation", request.GetProperty("Coverage").GetString());
    }

    [Fact]
    public async Task ApprovedHttpReviewBindsExpectedAndObservedIdentity()
    {
        var settings = await Fixture();
        using var handler = new ReviewHttpHandler(Inventory());
        Assert.True(await Generation.RunAsync(settings,
            (approved, draft) => OllamaReview.RunAsync(approved, draft, handler)));
        Assert.Equal(1, handler.InferenceRequests);
        Assert.Equal(2, handler.InventoryRequests);
        using var report = JsonDocument.Parse(await File.ReadAllTextAsync(Path.Combine(settings.Output, "evidence.json")));
        Assert.Equal(settings.Model, report.RootElement.GetProperty("approvedOllama").GetProperty("model").GetString());
        Assert.Equal(settings.ModelDigest, report.RootElement.GetProperty("approvedOllama").GetProperty("digest").GetString());
        Assert.Equal(settings.ModelDigest, report.RootElement.GetProperty("ollama").GetProperty("Digest").GetString());
        Assert.Equal(settings.Revision, report.RootElement.GetProperty("revision").GetString());
        Assert.False(report.RootElement.GetProperty("nativeBuildProven").GetBoolean());
    }

    [Theory]
    [InlineData("mismatched-digest")]
    [InlineData("duplicate-model")]
    [InlineData("missing-models")]
    [InlineData("not-installed")]
    public async Task InvalidPostflightLeavesNoConfiguration(string problem)
    {
        var settings = await Fixture();
        using var handler = new ReviewHttpHandler(Inventory(), InvalidInventory(problem));
        await Assert.ThrowsAsync<InvalidDataException>(() => Generation.RunAsync(settings,
            (approved, draft) => OllamaReview.RunAsync(approved, draft, handler)));
        Assert.Equal(1, handler.InferenceRequests);
        Assert.Equal(2, handler.InventoryRequests);
        Assert.False(Directory.Exists(settings.Output));
    }

    [Fact]
    public async Task UnavailablePostflightLeavesNoConfiguration()
    {
        var settings = await Fixture();
        using var handler = new ReviewHttpHandler(Inventory(), unavailablePostflight: true);
        await Assert.ThrowsAsync<HttpRequestException>(() => Generation.RunAsync(settings,
            (approved, draft) => OllamaReview.RunAsync(approved, draft, handler)));
        Assert.Equal(1, handler.InferenceRequests);
        Assert.Equal(2, handler.InventoryRequests);
        Assert.False(Directory.Exists(settings.Output));
    }

    [Fact]
    public async Task WrongResponseModelLeavesNoConfiguration()
    {
        var settings = await Fixture();
        using var handler = new ReviewHttpHandler(Inventory(), responseModel: "other:1");
        await Assert.ThrowsAsync<InvalidDataException>(() => Generation.RunAsync(settings,
            (approved, draft) => OllamaReview.RunAsync(approved, draft, handler)));
        Assert.Equal(1, handler.InferenceRequests);
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

    private static string Inventory(string name = "fixture:1", string? digest = null) =>
        JsonSerializer.Serialize(new { models = new[] { new { name, model = name, digest = digest ?? new string('a', 64) } } });

    private static string InvalidInventory(string problem) => problem switch
    {
        "missing-models" => "{}",
        "not-object" => "[]",
        "not-array" => "{\"models\":{}}",
        "null-entry" => "{\"models\":[null]}",
        "missing-name" => "{\"models\":[{\"digest\":\"" + new string('a', 64) + "\"}]}",
        "missing-digest" => "{\"models\":[{\"name\":\"fixture:1\"}]}",
        "non-string-digest" => "{\"models\":[{\"name\":\"fixture:1\",\"digest\":3}]}",
        "short-digest" => Inventory(digest: "abc"),
        "non-hex-digest" => Inventory(digest: new string('g', 64)),
        "duplicate-model" => "{\"models\":[" + InventoryEntry() + "," + InventoryEntry() + "]}",
        "duplicate-name" => "{\"models\":[{\"name\":\"other:1\",\"name\":\"fixture:1\",\"digest\":\"" + new string('a', 64) + "\"}]}",
        "duplicate-digest" => "{\"models\":[{\"name\":\"fixture:1\",\"digest\":\"" + new string('b', 64) + "\",\"digest\":\"" + new string('a', 64) + "\"}]}",
        "duplicate-inventory" => "{\"models\":[],\"models\":[" + InventoryEntry() + "]}",
        "not-installed" => Inventory(name: "other:1"),
        "mismatched-digest" => Inventory(digest: new string('b', 64)),
        "alias-mismatch" => "{\"models\":[{\"name\":\"fixture:1\",\"model\":\"other:1\",\"digest\":\"" + new string('a', 64) + "\"}]}",
        _ => throw new ArgumentException("Unknown inventory fixture.", nameof(problem))
    };

    private static string InventoryEntry() => JsonSerializer.Serialize(new { name = "fixture:1", digest = new string('a', 64) });

    private sealed class ReviewHttpHandler(string preflight, string? postflight = null,
        bool unavailablePostflight = false, string responseModel = "fixture:1", bool unavailablePreflight = false,
        string? metadataOverride = null, int? inputCount = 64, int? outputCount = 16, string reason = "stop",
        string? reviewContent = null, Action<string>? onRequest = null,
        Func<string, int, HttpStatusCode?>? responseStatus = null) : HttpMessageHandler
    {
        public int InventoryRequests { get; private set; }
        public int InferenceRequests { get; private set; }
        public List<string> RequestPaths { get; } = [];
        public List<int> Residencies { get; } = [];
        public List<string> InferenceBodies { get; } = [];

        protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken)
        {
            Assert.Equal("127.0.0.1", request.RequestUri!.Host);
            RequestPaths.Add(request.RequestUri.AbsolutePath);
            onRequest?.Invoke(request.RequestUri.AbsolutePath);
            var failure = responseStatus?.Invoke(request.RequestUri.AbsolutePath, RequestPaths.Count);
            if (failure is not null) return new HttpResponseMessage(failure.Value);
            string content;
            if (request.Method == HttpMethod.Get && request.RequestUri.AbsolutePath == "/api/tags")
            {
                InventoryRequests++;
                if (InventoryRequests == 1 && unavailablePreflight || InventoryRequests > 1 && unavailablePostflight)
                    return new HttpResponseMessage(HttpStatusCode.ServiceUnavailable);
                content = InventoryRequests == 1 ? preflight : postflight ?? preflight;
            }
            else if (request.Method == HttpMethod.Post && request.RequestUri.AbsolutePath == "/api/show")
            {
                content = metadataOverride ?? "{\"template\":\"\",\"system\":\"\",\"model_info\":{\"general.architecture\":\"fixture\",\"fixture.context_length\":32768,\"tokenizer.ggml.model\":\"gpt2\"}}";
            }
            else
            {
                Assert.Equal(HttpMethod.Post, request.Method);
                Assert.Equal("/api/chat", request.RequestUri.AbsolutePath);
                InferenceRequests++;
                var body = await request.Content!.ReadAsStringAsync(cancellationToken);
                InferenceBodies.Add(body);
                using var posted = JsonDocument.Parse(body);
                Residencies.Add(posted.RootElement.GetProperty("keep_alive").GetInt32());
                content = JsonSerializer.Serialize(new
                {
                    model = responseModel, done = true, done_reason = reason, prompt_eval_count = inputCount, eval_count = outputCount,
                    message = new { content = reviewContent ?? "{\"decision\":\"accept\",\"blockers\":[]}" }
                });
            }
            return new HttpResponseMessage(HttpStatusCode.OK)
            {
                Content = new StringContent(content, Encoding.UTF8, "application/json")
            };
        }
    }

    public static async Task<Settings> Fixture()
    {
        var directory = Path.Combine(Path.GetTempPath(), "projectbuddy-source-config-" + Guid.NewGuid().ToString("N"));
        var root = Path.Combine(directory, "source");
        Directory.CreateDirectory(Path.Combine(root, "src"));
        var manifest = ConfigurationTests.Manifest.Replace("\r\n", "\n", StringComparison.Ordinal);
        var project = ConfigurationTests.Xml.Replace("\r\n", "\n", StringComparison.Ordinal);
        await File.WriteAllTextAsync(Path.Combine(root, "vipm.toml"), manifest.Replace("\n", "\r\n", StringComparison.Ordinal));
        await File.WriteAllTextAsync(Path.Combine(root, "src/fixture.lvproj"), project.Replace("\n", "\r\n", StringComparison.Ordinal));
        await Generation.Git(root, "init");
        await Generation.Git(root, "-c", "core.autocrlf=true", "add", "vipm.toml", "src/fixture.lvproj");
        await Generation.Git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false", "commit", "-m", "Fixture");
        var revision = await Generation.Git(root, "rev-parse", "HEAD");
        return new Settings(root, "vipm.toml", "ppl", "https://example.org/fixture.git", revision,
            Path.Combine(AppContext.BaseDirectory, "pipeline-config.schema.json"), Path.Combine(directory, "generated"),
            "http://127.0.0.1:11435", "fixture:1", new string('a', 64),
            new ReviewBudget("projectbuddy/ollama-budget@1", "utf8-byte-plus-approved-media@1", new string('a', 64),
                16384, 512, 1024, 0, 65536, 0, 0),
            new ReviewPrivacy("projectbuddy/ollama-privacy@1", "none", JsonSerializer.SerializeToElement(Array.Empty<object>()),
                false, DateTimeOffset.Parse("2099-01-01T00:00:00Z")),
            new ReviewLifecyclePolicy("projectbuddy/ollama-lifecycle@1", 300, 90, 16, 0, 0, 30, 0),
            new ReviewExecutionPolicy("projectbuddy/ollama-execution@1", new string('a', 64), "cpu-only", 1));
    }
}