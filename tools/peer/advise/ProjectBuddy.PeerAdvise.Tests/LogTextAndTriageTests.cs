using System.Text;
using System.Text.Json.Nodes;
using ProjectBuddy.PeerOllama;
using Xunit;

namespace ProjectBuddy.PeerAdvise.Tests;

public class LogTextTests
{
    [Fact]
    public void NormalizeTurnsGitHubRowsIntoSectionHeaders()
    {
        Assert.Equal(
            "== build / compile ==\ncompiling\n== build / verify ==\nFAIL  gate-x  -- broken thing\n##[error]Process completed with exit code 1.",
            LogText.Normalize(Fixtures.Log));
    }

    [Fact]
    public void NormalizeStripsStampsAnsiAndBlankRuns()
    {
        var raw = "\n\n\uFEFF2026-11-15T09:26:25Z \u001b[31mred\u001b[0m   \r\n\r\n\r\nplain line\r2026-11-15T09:26:25.5Z \n";
        Assert.Equal("red\n\nplain line", LogText.Normalize(raw));
    }

    [Fact]
    public void NormalizeDropsSectionsWithoutContent()
    {
        var raw = "build\tverify\t2026-11-15T09:26:25Z FAIL thing\nbuild\tempty\t2026-11-15T09:26:26Z \nbuild\tlast\t2026-11-15T09:26:27Z done\nbuild\tPost Run\t2026-11-15T09:26:28Z \n";
        Assert.Equal("== build / verify ==\nFAIL thing\n== build / last ==\ndone", LogText.Normalize(raw));
    }

    [Fact]
    public void NormalizeClipsVeryLongLines()
    {
        var line = LogText.Normalize(new string('x', LogText.MaxLineChars + 50));
        Assert.Equal(LogText.MaxLineChars + " [...]".Length, line.Length);
        Assert.EndsWith(" [...]", line);
    }

    [Fact]
    public void TailKeepsTheNewestWholeLinesAndTheirSectionHeader()
    {
        var tail = LogText.Tail("== job / a ==\none\ntwo\nthree", 20);
        Assert.Equal("== job / a ==\nthree", tail.Text);
        Assert.Equal(Encoding.UTF8.GetByteCount(tail.Text), tail.Bytes);
        Assert.Equal(3, tail.DroppedLines);
    }

    [Fact]
    public void TailDoesNotRepeatAHeaderAlreadyKept()
    {
        Assert.Equal("== job / a ==\none", LogText.Tail("== job / a ==\none", 100).Text);
        Assert.Equal("one\ntwo", LogText.Tail("one\ntwo", 100).Text);
    }

    [Fact]
    public void TailNeverReturnsAHeaderWithoutContent()
    {
        var tail = LogText.Tail("== job / a ==\n" + new string('x', 50), 20);
        Assert.Equal(("", 0, 2), (tail.Text, tail.Bytes, tail.DroppedLines));
        Assert.Equal("", LogText.Tail("== job / a ==\nthree", 10).Text);
        Assert.Equal("", LogText.Tail(new string('x', 50), 20).Text);
        Assert.Empty(LogText.Tail("== job / a ==\n== job / b ==", 100).Lines);
    }

    [Fact]
    public void TailStartsAtAHeaderWhenThatIsCheaper()
    {
        Assert.Equal("== job / b ==\nlast", LogText.Tail("== job / a ==\nlong line here\n== job / b ==\nlast", 20).Text);
    }

    [Fact]
    public void BuildPutsDistinctErrorLinesFromTheWholeLogFirst()
    {
        var normalized = "== job / a ==\nerror: early\nok\nerror: early\nFAIL  late gate\n" + string.Join('\n', Enumerable.Repeat("PASS fine", 200));
        var evidence = LogText.Build(normalized, 400);
        Assert.StartsWith($"{LogText.SignalHeading}\nerror: early\nFAIL  late gate\n{LogText.TailHeading}\n== job / a ==\nPASS fine", evidence.Text);
        Assert.Equal(2, evidence.SignalLines);
        Assert.Equal(["error: early", "FAIL  late gate", "== job / a =="], evidence.Lines.Take(3));
        Assert.DoesNotContain(LogText.SignalHeading, evidence.Lines);
        Assert.True(evidence.Bytes <= 400);
        Assert.Equal(Encoding.UTF8.GetByteCount(evidence.Text), evidence.Bytes);
    }

    [Fact]
    public void BuildCapsSignalsByCountAndBytes()
    {
        var many = string.Join('\n', Enumerable.Range(0, 60).Select(index => $"error: number {index}"));
        Assert.Equal(LogText.MaxSignalLines, LogText.Build(many, 100000).SignalLines);
        Assert.True(LogText.Build(many, 300).SignalLines < 10);
    }

    [Fact]
    public void BuildWithoutSignalsIsJustTheTailAndEmptyWhenNothingFits()
    {
        Assert.Equal("all good", LogText.Build("all good", 100).Text);
        var nothing = LogText.Build("error: " + new string('x', 500), 200);
        Assert.Equal("", nothing.Text);
        Assert.Equal((0, 0), (nothing.Bytes, nothing.SignalLines));
        Assert.Empty(nothing.Lines);
    }
}

public class TriageTests
{
    private static OllamaClient Client(FakeOllama fake) => new(fake, new Uri("http://127.0.0.1:11435/"), TimeSpan.FromSeconds(5));

    private static async Task<JsonObject> Run(FakeOllama fake, Policy? policy = null, string log = Fixtures.Log)
    {
        using var client = Client(fake);
        return await Triage.RunAsync(policy ?? Fixtures.ParsePolicy(), Encoding.UTF8.GetBytes(log), client);
    }

    [Fact]
    public async Task GroundedAnswerProducesTheReport()
    {
        var fake = new FakeOllama();
        fake.Replies.Enqueue(FakeOllama.Answer(" gate-x ", "FAIL  gate-x  -- broken thing", "gate-failure"));
        var report = await Run(fake);
        Assert.Equal("""{"step":"gate-x","errorLine":"FAIL  gate-x  -- broken thing","class":"gate-failure"}""", report["answer"]!.ToJsonString());
        Assert.Equal("""["step","errorLine"]""", report["verified"]!.ToJsonString());
        Assert.True(report["advisory"]!.GetValue<bool>());
        Assert.Equal("0.40.16", report["ollamaVersion"]!.GetValue<string>());
        Assert.Equal(1, report["input"]!["attempts"]!.GetValue<int>());
        Assert.Equal(2, report["input"]!["signalLines"]!.GetValue<int>());
        Assert.Equal(8194, report["receipt"]!["probePromptTokens"]!.GetValue<int>());
        Assert.Equal(3.0, report["receipt"]!["seconds"]!.GetValue<double>());
        var request = fake.TriageRequests.Single();
        Assert.Equal(16384, request["options"]!["num_ctx"]!.GetValue<int>());
        Assert.Equal("""["gate-failure","segfault","vm-state"]""", request["format"]!["properties"]!["class"]!["enum"]!.ToJsonString());
    }

    [Fact]
    public async Task APromptThatMayHaveBeenCutIsRetriedWithHalfTheBudget()
    {
        var fake = new FakeOllama();
        fake.Replies.Enqueue(FakeOllama.Reply("{}", 8194));
        fake.Replies.Enqueue(FakeOllama.Answer("gate-x", "FAIL  gate-x  -- broken thing", "gate-failure"));
        var report = await Run(fake);
        Assert.Equal(2, report["input"]!["attempts"]!.GetValue<int>());
        Assert.Equal(2, fake.TriageRequests.Count());
    }

    [Fact]
    public async Task NoAttemptFittingGivesNoAdvice()
    {
        var fake = new FakeOllama();
        fake.Replies.Enqueue(FakeOllama.Reply("{}", 8194));
        fake.Replies.Enqueue(FakeOllama.Reply("{}", 9000));
        var error = await Assert.ThrowsAsync<PeerException>(() => Run(fake));
        Assert.Equal(AdviseExitCodes.NoAdvice, error.ExitCode);
        Assert.Contains("2 attempt(s)", error.Message);
    }

    [Fact]
    public async Task EvidenceThatCannotFitTheBudgetGivesNoAdviceWithoutAsking()
    {
        var policy = Fixtures.Policy();
        policy["maxInputBytes"] = 1024;
        var fake = new FakeOllama();
        var error = await Assert.ThrowsAsync<PeerException>(() => Run(fake, Fixtures.ParsePolicy(policy), new string('x', LogText.MaxLineChars)));
        Assert.Equal(AdviseExitCodes.NoAdvice, error.ExitCode);
        Assert.Empty(fake.TriageRequests);
    }

    [Fact]
    public async Task EmptyLogIsRefusedBeforeContactingOllama()
    {
        var fake = new FakeOllama();
        Assert.Equal(ExitCodes.Refused, (await Assert.ThrowsAsync<PeerException>(() => Run(fake, log: "\n \n"))).ExitCode);
        Assert.Empty(fake.Requests);
    }

    [Fact]
    public async Task UnpinnedModelAndBrokenProbeStopTheRun()
    {
        Assert.Equal(ExitCodes.Refused, (await Assert.ThrowsAsync<PeerException>(() => Run(new FakeOllama { TagDigest = new string('0', 64) }))).ExitCode);
        Assert.Equal(ExitCodes.GuardBroken, (await Assert.ThrowsAsync<PeerException>(() => Run(new FakeOllama { ProbeTokens = 8191 }))).ExitCode);
    }

    public static TheoryData<string, string> Ungrounded => new()
    {
        { "not json", "The gate failed." },
        { "array", "[1]" },
        { "missing class", """{"step":"gate-x","errorLine":"FAIL  gate-x  -- broken thing"}""" },
        { "extra key", """{"step":"gate-x","errorLine":"FAIL  gate-x  -- broken thing","class":"gate-failure","why":"x"}""" },
        { "non-string step", """{"step":1,"errorLine":"FAIL  gate-x  -- broken thing","class":"gate-failure"}""" },
        { "empty step", """{"step":"  ","errorLine":"FAIL  gate-x  -- broken thing","class":"gate-failure"}""" },
        { "long step", $$"""{"step":"{{new string('a', Triage.MaxStepChars + 1)}}","errorLine":"FAIL  gate-x  -- broken thing","class":"gate-failure"}""" },
        { "invented step", """{"step":"deploy","errorLine":"FAIL  gate-x  -- broken thing","class":"gate-failure"}""" },
        { "empty line", """{"step":"gate-x","errorLine":" ","class":"gate-failure"}""" },
        { "two lines", """{"step":"gate-x","errorLine":"compiling\nFAIL  gate-x  -- broken thing","class":"gate-failure"}""" },
        { "carriage return", """{"step":"gate-x","errorLine":"FAIL\r gate-x","class":"gate-failure"}""" },
        { "paraphrase", """{"step":"gate-x","errorLine":"FAIL gate-x -- broken thing","class":"gate-failure"}""" },
        { "tool heading as line", """{"step":"verify","errorLine":"# End of the log:","class":"gate-failure"}""" },
        { "tool heading as step", """{"step":"Lines that look like errors","errorLine":"FAIL  gate-x  -- broken thing","class":"gate-failure"}""" },
        { "tiny fragment", """{"step":"gate-x","errorLine":"1","class":"gate-failure"}""" },
        { "tiny step", """{"step":"e","errorLine":"FAIL  gate-x  -- broken thing","class":"gate-failure"}""" },
        { "section header as line", """{"step":"verify","errorLine":"== build / verify ==","class":"gate-failure"}""" },
        { "unknown class", """{"step":"gate-x","errorLine":"FAIL  gate-x  -- broken thing","class":"weather"}""" },
    };

    [Theory]
    [MemberData(nameof(Ungrounded))]
    public void UngroundedAnswersGiveNoAdvice(string label, string content)
    {
        var evidence = LogText.Build(LogText.Normalize(Fixtures.Log), 12000);
        var error = Assert.Throws<PeerException>(() => Triage.Validate(Fixtures.ParsePolicy(), evidence, content));
        Assert.Equal(AdviseExitCodes.NoAdvice, error.ExitCode);
        Assert.False(string.IsNullOrEmpty(label));
    }

    [Fact]
    public void AGroundedFragmentReportsTheWholeLogLine()
    {
        var evidence = LogText.Build(LogText.Normalize(Fixtures.Log), 12000);
        var answer = Triage.Validate(Fixtures.ParsePolicy(), evidence, """{"step":"verify","errorLine":"-- broken thing","class":"gate-failure"}""");
        Assert.Equal("""{"step":"verify","errorLine":"FAIL  gate-x  -- broken thing","class":"gate-failure"}""", answer.ToJsonString());
    }

    [Fact]
    public void AnExactQuoteBeatsANewerLineThatOnlyMentionsIt()
    {
        var log = "ci\tpush image\t2026-11-15T09:26:25Z error: unauthorized: authentication required\n"
            + "ci\tpush image\t2026-11-15T09:26:26Z retry 1/3 ignored transient 'error: unauthorized: authentication required' from mirror cache\n"
            + "ci\tpush image\t2026-11-15T09:26:27Z ##[error]Process completed with exit code 1.";
        var evidence = LogText.Build(LogText.Normalize(log), 12000);
        var exact = Triage.Validate(Fixtures.ParsePolicy(), evidence, """{"step":"push image","errorLine":"error: unauthorized: authentication required","class":"gate-failure"}""");
        Assert.Equal("error: unauthorized: authentication required", exact["errorLine"]!.GetValue<string>());
        var fragment = Triage.Validate(Fixtures.ParsePolicy(), evidence, """{"step":"push image","errorLine":"ignored transient","class":"gate-failure"}""");
        Assert.StartsWith("retry 1/3", fragment["errorLine"]!.GetValue<string>());
    }

    [Fact]
    public void QuotesOfClippedLinesMatchTheirTextButNotTheClipMarker()
    {
        var evidence = LogText.Build(LogText.Normalize("error: " + new string('x', LogText.MaxLineChars)), 12000);
        var answer = Triage.Validate(Fixtures.ParsePolicy(), evidence, """{"step":"error","errorLine":"error: xxxxxxxx","class":"segfault"}""");
        Assert.EndsWith(LogText.ClipMarker, answer["errorLine"]!.GetValue<string>());
        Assert.Throws<PeerException>(() => Triage.Validate(Fixtures.ParsePolicy(), evidence, """{"step":"error","errorLine":"xxxxx [...]","class":"segfault"}"""));
    }

    [Fact]
    public void FormatConstrainsTheClassToThePolicy()
    {
        var format = Triage.Format(Fixtures.ParsePolicy());
        Assert.Equal("""["step","errorLine","class"]""", format["required"]!.ToJsonString());
        Assert.Equal(3, format["properties"]!["class"]!["enum"]!.AsArray().Count);
    }
}
