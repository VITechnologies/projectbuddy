using System.Text.Json.Nodes;
using ProjectBuddy.PeerOllama;
using Xunit;

namespace ProjectBuddy.PeerBench.Tests;

public class ProgramTests
{
    private static readonly string[] Valid = ["run", "--corpus", "c.json", "--candidates", "m.json", "--machine", "workstation", "--ollama", "http://127.0.0.1:11435"];

    private static (int Code, string Out, string Err) Run(string[] args, FakeOllama? ollama = null, Func<string, byte[]>? read = null)
    {
        var files = new Dictionary<string, byte[]>
        {
            ["c.json"] = Fixtures.Bytes(Fixtures.Corpus()),
            ["m.json"] = System.Text.Encoding.UTF8.GetBytes(Fixtures.Candidates),
        };
        using var stdout = new StringWriter();
        using var stderr = new StringWriter();
        var code = Program.Run(args, read ?? (path => files[path]), ollama ?? new FakeOllama(), stdout, stderr);
        return (code, stdout.ToString(), stderr.ToString());
    }

    [Fact]
    public void VersionPrintsToolAndSchema()
    {
        var (code, output, _) = Run(["--version"]);
        Assert.Equal(0, code);
        Assert.Equal($"{Versions.Tool} {Versions.ResultSchema}", output.Trim());
    }

    [Fact]
    public void MainAnswersVersion() => Assert.Equal(0, Program.Main(["--version"]));

    [Theory]
    [InlineData]
    [InlineData("serve")]
    [InlineData("run", "--corpus")]
    [InlineData("run", "--bogus", "x")]
    [InlineData("run", "--corpus", "a", "--corpus", "b", "--candidates", "m", "--machine", "peer")]
    [InlineData("run", "--corpus", "a", "--candidates", "m")]
    [InlineData("run", "--corpus", "a", "--candidates", "m", "--machine", "laptop")]
    [InlineData("run", "--corpus", "a", "--candidates", "m", "--machine", "peer", "--timeout-seconds", "soon")]
    [InlineData("run", "--corpus", "a", "--candidates", "m", "--machine", "peer", "--timeout-seconds", "0")]
    [InlineData("run", "--corpus", "a", "--candidates", "m", "--machine", "peer", "--timeout-seconds", "3601")]
    public void BadArgumentsPrintUsage(params string[] args)
    {
        var (code, _, error) = Run(args);
        Assert.Equal(ExitCodes.Usage, code);
        Assert.Contains("usage:", error);
    }

    [Fact]
    public void DefaultsApplyWhenOptionalFlagsAreAbsent()
    {
        Assert.True(Program.TryParse(["--corpus", "a", "--candidates", "m", "--machine", "peer"], out var options));
        Assert.Equal(Program.DefaultOllama, options["--ollama"]);
        Assert.Equal("600", options["--timeout-seconds"]);
    }

    [Fact]
    public void RunWritesResultJson()
    {
        var (code, output, error) = Run(Valid);
        Assert.Equal(0, code);
        Assert.Equal(Versions.ResultSchema, JsonNode.Parse(output)!["schema"]!.GetValue<string>());
        Assert.Contains("qwen2.5:7b t1:", error);
    }

    [Fact]
    public void UnreadableFileIsRefusedWithoutItsPath()
    {
        var (code, _, error) = Run(Valid, read: path => throw new IOException($"/secret/dir/{path}"));
        Assert.Equal(ExitCodes.Refused, code);
        Assert.Contains("cannot read c.json", error);
        Assert.DoesNotContain("/secret/dir", error);
    }

    [Fact]
    public void NonLoopbackOllamaIsRefused()
    {
        var args = Valid.ToArray();
        args[^1] = "http://10.211.0.2:11434";
        var (code, _, error) = Run(args);
        Assert.Equal(ExitCodes.Refused, code);
        Assert.Contains("loopback", error);
    }
}

public class CorpusTests
{
    [Fact]
    public void CommittedCorpusAndCandidatesAreValid()
    {
        var corpus = Corpus.Parse(File.ReadAllBytes(Fixtures.RepositoryFile("corpus.json")));
        var candidates = Candidate.ParseAll(File.ReadAllBytes(Fixtures.RepositoryFile("candidates.json")));
        Assert.Equal(8192, corpus.NumCtx);
        Assert.Contains(corpus.Items, item => item.Kind == ItemKind.Security && item.Classes.Count == 0);
        Assert.Equal(corpus.Verbs.Count, corpus.Items.Count(item => item.Kind == ItemKind.Tool));
        Assert.Equal(3, candidates.Count);
    }

    [Fact]
    public void ParsesEveryKindAndHashesTheBytes()
    {
        var bytes = Fixtures.Bytes(Fixtures.Corpus());
        var corpus = Corpus.Parse(bytes);
        Assert.Equal(Convert.ToHexStringLower(System.Security.Cryptography.SHA256.HashData(bytes)), corpus.Sha256);
        Assert.Equal(["t1", "s1", "s2", "v1"], corpus.Items.Select(item => item.Id));
        Assert.Equal("gate-x", corpus.Items[0].Step);
        Assert.Equal(["secret-in-log"], corpus.Items[1].Classes);
        Assert.Equal("vbox", corpus.Items[3].Verb);
    }

    public static TheoryData<string, Action<JsonObject>> Refusals => new()
    {
        { "schema", corpus => corpus["schema"] = "other@1" },
        { "keys", corpus => corpus.Remove("seed") },
        { "numCtx", corpus => corpus["numCtx"] = 1024 },
        { "numPredict", corpus => corpus["numPredict"] = 4096 },
        { "prompts keys", corpus => corpus["prompts"]!.AsObject().Remove("tool") },
        { "triageClasses", corpus => corpus["triageClasses"] = new JsonArray("a", "a") },
        { "no items", corpus => corpus["items"] = new JsonArray() },
        { "item object", corpus => Items(corpus).Add("text") },
        { "duplicate id", corpus => Items(corpus).Add(Fixtures.Item("t1", "tool", "x", new JsonObject { ["verb"] = "vbox" })) },
        { "id pattern", corpus => Items(corpus)[0]!["id"] = "T 1" },
        { "item keys", corpus => Items(corpus)[0]!.AsObject().Remove("source") },
        { "input size", corpus => Items(corpus)[0]!["input"] = new string('x', Corpus.MaxInputBytes + 1) },
        { "kind", corpus => Items(corpus)[0]!["kind"] = "poem" },
        { "triage expect", corpus => Items(corpus)[0]!["expect"]!.AsObject().Remove("class") },
        { "errorLine absent", corpus => Items(corpus)[0]!["expect"]!["errorLine"] = "not in the input" },
        { "triage class", corpus => Items(corpus)[0]!["expect"]!["class"] = "weather" },
        { "security class", corpus => Items(corpus)[1]!["expect"]!["classes"] = new JsonArray("weather") },
        { "security repeat", corpus => Items(corpus)[1]!["expect"]!["classes"] = new JsonArray("secret-in-log", "secret-in-log") },
        { "verb", corpus => Items(corpus)[3]!["expect"]!["verb"] = "reboot" },
    };

    [Theory]
    [MemberData(nameof(Refusals))]
    public void InvalidCorpusIsRefused(string label, Action<JsonObject> mutate)
    {
        var corpus = Fixtures.Corpus();
        mutate(corpus);
        var error = Assert.Throws<PeerException>(() => Fixtures.Parse(corpus));
        Assert.Equal(ExitCodes.Refused, error.ExitCode);
        Assert.False(string.IsNullOrEmpty(label));
    }

    [Theory]
    [InlineData("not json")]
    [InlineData("null")]
    [InlineData("[]")]
    [InlineData("""{"schema":"a","schema":"b"}""")]
    public void NonObjectCorpusIsRefused(string text) =>
        Assert.Equal(ExitCodes.Refused, Assert.Throws<PeerException>(() => Corpus.Parse(System.Text.Encoding.UTF8.GetBytes(text))).ExitCode);

    [Theory]
    [InlineData("""{"schema":"x@1","models":[]}""")]
    [InlineData("""{"schema":"projectbuddy/peer-bench-candidates@1","models":[]}""")]
    [InlineData("""{"schema":"projectbuddy/peer-bench-candidates@1","models":[{"name":"Bad Name","digest":"845dbda0ea48ed749caafd9e6037047aa19acfcfd82e704d7ca97d631a0b697e"}]}""")]
    [InlineData("""{"schema":"projectbuddy/peer-bench-candidates@1","models":[{"name":"a:1","digest":"short"}]}""")]
    [InlineData("""{"schema":"projectbuddy/peer-bench-candidates@1","models":[{"name":"a:1","digest":"845dbda0ea48ed749caafd9e6037047aa19acfcfd82e704d7ca97d631a0b697e"},{"name":"a:1","digest":"845dbda0ea48ed749caafd9e6037047aa19acfcfd82e704d7ca97d631a0b697e"}]}""")]
    [InlineData("""{"schema":"projectbuddy/peer-bench-candidates@1","models":[{"name":"a:1"}]}""")]
    public void InvalidCandidatesAreRefused(string text) =>
        Assert.Equal(ExitCodes.Refused, Assert.Throws<PeerException>(() => Candidate.ParseAll(System.Text.Encoding.UTF8.GetBytes(text))).ExitCode);

    private static JsonArray Items(JsonObject corpus) => corpus["items"]!.AsArray();
}
