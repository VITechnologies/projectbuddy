using System.Text;
using System.Text.Json.Nodes;
using ProjectBuddy.PeerOllama;
using ProjectBuddy.PeerProbes;
using Xunit;

namespace ProjectBuddy.PeerDiagnose.Tests;

public sealed class ProgramTests
{
    private static readonly byte[] PolicyBytes = Encoding.UTF8.GetBytes(Fixtures.Policy().ToJsonString());

    private static (int Exit, string Output, string Error) Run(FakeOllama ollama, Func<string, byte[]> readFile, params string[] args)
    {
        var (output, error) = (new StringWriter(), new StringWriter());
        var exit = Program.Run(args, readFile, ollama, (_, _) => new FakeSources(), ("BUILDHOST-01", "builder"), output, error);
        return (exit, output.ToString(), error.ToString());
    }

    private static byte[] Policy(string path) => path == "policy.json" ? PolicyBytes : throw new FileNotFoundException(path);

    [Fact]
    public void PrintsItsVersion()
    {
        var (exit, output, _) = Run(new FakeOllama(), Policy, "--version");
        Assert.Equal(0, exit);
        Assert.Equal("projectbuddy-diagnose 0.1.0 projectbuddy/peer-diagnosis@1" + Environment.NewLine, output);
    }

    [Theory]
    [InlineData]
    [InlineData("--policy", "policy.json")]
    [InlineData("--question", "github-unreachable")]
    [InlineData("--policy", "policy.json", "--question")]
    [InlineData("--policy", "policy.json", "--question", "q", "--shell", "x")]
    [InlineData("--policy", "policy.json", "--policy", "policy.json", "--question", "q")]
    [InlineData("--policy", "policy.json", "--question", "q", "--timeout-seconds", "0")]
    [InlineData("--policy", "policy.json", "--question", "q", "--timeout-seconds", "x")]
    [InlineData("--policy", "policy.json", "--question", "q", "--vboxmanage", "VBoxManage")]
    public void RejectsMalformedCommandLines(params string[] args)
    {
        var ollama = new FakeOllama();
        var (exit, output, error) = Run(ollama, Policy, args);
        Assert.Equal(ExitCodes.Usage, exit);
        Assert.Empty(output);
        Assert.Equal(Program.Usage + Environment.NewLine, error);
        Assert.Empty(ollama.Requests);
    }

    [Theory]
    [InlineData("missing.json", "github-unreachable", null, null, "cannot read the policy: FileNotFoundException")]
    [InlineData("policy.json", "shell", null, null, "unknown question 'shell'")]
    [InlineData("policy.json", "github-unreachable", "http://example.com:11434", null, "--ollama must be")]
    [InlineData("policy.json", "github-unreachable", null, "/nonexistent/deny.json", "the deny list cannot be read")]
    public void RefusesBeforeAskingTheModel(string policy, string question, string? ollamaUrl, string? denyList, string message)
    {
        List<string> args = ["--policy", policy, "--question", question];
        if (ollamaUrl is not null)
        {
            args.AddRange(["--ollama", ollamaUrl]);
        }

        if (denyList is not null)
        {
            args.AddRange(["--deny-list", denyList]);
        }

        var ollama = new FakeOllama();
        var (exit, output, error) = Run(ollama, Policy, [.. args]);
        Assert.Equal(ExitCodes.Refused, exit);
        Assert.Empty(output);
        Assert.StartsWith("projectbuddy-diagnose: ", error);
        Assert.Contains(message, error);
        Assert.Empty(ollama.Requests);
    }

    [Fact]
    public void WritesTheReportOrNothing()
    {
        var ollama = new FakeOllama();
        ollama.Replies.Enqueue(FakeOllama.Calls(("network-link", [])));
        ollama.Replies.Enqueue(FakeOllama.Reply(""));
        ollama.Replies.Enqueue(FakeOllama.Answer("link-up", "Wi-Fi is up on BUILDHOST-01.", ("network-link", "/data/adapters/0/status", "up")));
        (string? VBoxManage, Uri? Ollama) seen = default;
        var (output, error) = (new StringWriter(), new StringWriter());
        var exit = Program.Run(
            ["--policy", "policy.json", "--question", "github-unreachable", "--ollama", "http://127.0.0.1:11500", "--vboxmanage", "/opt/vbox/VBoxManage", "--timeout-seconds", "60"],
            Policy, ollama, (vboxManage, uri) =>
            {
                seen = (vboxManage, uri);
                return new FakeSources();
            }, ("BUILDHOST-01", "builder"), output, error);

        Assert.Equal(0, exit);
        Assert.Empty(error.ToString());
        Assert.Equal(("/opt/vbox/VBoxManage", new Uri("http://127.0.0.1:11500/")), seen);
        var report = JsonNode.Parse(output.ToString())!;
        Assert.Equal("Wi-Fi is up on <private>.", (string?)report["answer"]!["finding"]);

        ollama.Replies.Enqueue(FakeOllama.Reply("", 9000));
        var (noAdvice, noOutput, reason) = Run(ollama, Policy, "--policy", "policy.json", "--question", "github-unreachable");
        Assert.Equal(DiagnoseExitCodes.NoAdvice, noAdvice);
        Assert.Empty(noOutput);
        Assert.Contains("did not provably fit", reason);
    }
}
