using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;
using ProjectBuddy.PeerOllama;
using Xunit;

namespace ProjectBuddy.PeerProbes.Tests;

public sealed class ProgramTests
{
    private static readonly (string, string) Host = ("BUILDHOST-01", "builder");

    private static async Task<(int Exit, string Output, string Error, Program.Cli? Cli, Uri? Ollama)> Run(FakeSources sources, params string[] args)
    {
        var (output, error) = (new StringWriter(), new StringWriter());
        Program.Cli? seen = null;
        Uri? ollama = null;
        var exit = await Program.RunAsync(args, output, error, Host, (cli, url) =>
        {
            (seen, ollama) = (cli, url);
            return sources;
        });
        return (exit, output.ToString(), error.ToString(), seen, ollama);
    }

    [Fact]
    public async Task ListsTheToolsForAModel()
    {
        var (exit, output, error, cli, _) = await Run(new FakeSources(), "list");
        Assert.Equal(0, exit);
        Assert.Empty(error);
        Assert.Null(cli);
        var list = JsonNode.Parse(output)!;
        Assert.Equal(Program.ListSchema, (string?)list["schema"]);
        Assert.Equal(Registry.Tools.Select(tool => tool.Name), list["tools"]!.AsArray().Select(tool => (string?)tool!["name"]));
        Assert.Equal(
            """[{"name":"hours","type":"integer","min":1,"max":72,"default":24,"description":"How many hours back to count."}]""",
            list["tools"]![5]!["arguments"]!.ToJsonString());
        Assert.Equal("[]", list["tools"]![0]!["arguments"]!.ToJsonString());
        Assert.Equal(Program.List() + Environment.NewLine, output);
    }

    [Fact]
    public async Task RunsOneToolWithItsArguments()
    {
        var sources = new FakeSources();
        sources.DriveList.Add(new Drive("C:\\", "NTFS", 10, 5));
        var (exit, output, error, cli, ollama) = await Run(sources, "run", "disk");
        Assert.Equal(0, exit);
        Assert.Empty(error);
        Assert.Equal("disk", (string?)JsonNode.Parse(output)!["tool"]);
        Assert.Equal(new Uri(Program.DefaultOllama), ollama);
        Assert.Equal(new Program.Cli("run", "disk", null, null, Program.DefaultOllama, null, "projectbuddy:0.3-qwen2.5-7b", 24), cli);

        (exit, _, _, _, _) = await Run(sources, "run", "event-errors", "--args", """{"hours":2}""");
        Assert.Equal(0, exit);
        Assert.Contains(sources.Calls, call => call.Contains("<= 7200000]", StringComparison.Ordinal));
    }

    [Fact]
    public async Task PassesTheOptionsThrough()
    {
        var sources = new FakeSources { Ollama = FakeSources.State(models: 2) };
        var (exit, output, _, cli, ollama) = await Run(sources, "run", "ollama", "--ollama", "http://127.0.0.2:11500/", "--vboxmanage", "/opt/vbox/VBoxManage", "--identity", "model-001:latest");
        Assert.Equal(0, exit);
        Assert.Equal(new Uri("http://127.0.0.2:11500/"), ollama);
        Assert.Equal("/opt/vbox/VBoxManage", cli!.VBoxManage);
        var models = JsonNode.Parse(output)!["data"]!["models"]!.AsArray();
        Assert.Equal([true, false], models.Select(model => (bool)model!["identity"]!));
    }

    [Fact]
    public async Task RunsEveryToolAndHashesEachResult()
    {
        var sources = new FakeSources();
        var (exit, output, error, _, _) = await Run(sources, "run-all", "--hours", "3");
        Assert.Equal(0, exit);
        Assert.Empty(error);
        var lines = output.Split(Environment.NewLine, StringSplitOptions.RemoveEmptyEntries);
        Assert.Equal(Registry.Tools.Count + 1, lines.Length);
        var summary = JsonNode.Parse(lines[^1])!;
        Assert.Equal(Program.RunSchema, (string?)summary["schema"]);
        for (var index = 0; index < Registry.Tools.Count; index++)
        {
            Assert.Equal(Registry.Tools[index].Name, (string?)JsonNode.Parse(lines[index])!["tool"]);
            Assert.Equal(Convert.ToHexStringLower(SHA256.HashData(Encoding.UTF8.GetBytes(lines[index]))), (string?)summary["sha256"]![Registry.Tools[index].Name]);
        }

        Assert.Contains(sources.Calls, call => call.Contains("<= 10800000]", StringComparison.Ordinal));
    }

    [Fact]
    public async Task ChecksRunAllArgumentsBeforeRunningAnything()
    {
        var sources = new FakeSources();
        var (exit, output, error, _, _) = await Run(sources, "run-all", "--hours", "73");
        Assert.Equal(ExitCodes.Refused, exit);
        Assert.Empty(output);
        Assert.Contains("must be 1-72", error);
        Assert.Empty(sources.Calls);
    }

    [Theory]
    [InlineData]
    [InlineData("nope")]
    [InlineData("run")]
    [InlineData("run", "--args", "{}")]
    [InlineData("run", "disk", "--hours", "1")]
    [InlineData("run-all", "--args", "{}")]
    [InlineData("run-all", "--identity", "a", "--identity", "b")]
    [InlineData("run-all", "--ollama")]
    [InlineData("run-all", "--hours", "x")]
    [InlineData("list", "--hours", "1")]
    [InlineData("run", "vm-states", "--vboxmanage", "VBoxManage")]
    [InlineData("run-all", "--vboxmanage", "Oracle/VirtualBox/VBoxManage.exe")]
    public async Task RejectsMalformedCommandLines(params string[] args)
    {
        var (exit, output, error, cli, _) = await Run(new FakeSources(), args);
        Assert.Equal(ExitCodes.Usage, exit);
        Assert.Empty(output);
        Assert.Contains("usage: projectbuddy-probes", error);
        Assert.Null(cli);
    }

    [Theory]
    [InlineData("not json", "--args is not valid JSON")]
    [InlineData("[1]", "--args must be a JSON object")]
    [InlineData("null", "--args must be a JSON object")]
    [InlineData("""{"hours":1,"hours":2}""", "--args is not valid JSON")]
    [InlineData("""{"hours":0}""", "must be 1-72")]
    public async Task RefusesBadToolArguments(string arguments, string message)
    {
        var (exit, output, error, _, _) = await Run(new FakeSources(), "run", "event-errors", "--args", arguments);
        Assert.Equal(ExitCodes.Refused, exit);
        Assert.Empty(output);
        Assert.Contains(message, error);
    }

    [Fact]
    public async Task RefusesUnknownToolsAndRemoteOllama()
    {
        var unknown = await Run(new FakeSources(), "run", "shell");
        Assert.Equal(ExitCodes.Refused, unknown.Exit);
        Assert.Equal("refused: unknown tool 'shell'" + Environment.NewLine, unknown.Error);

        var remote = await Run(new FakeSources(), "run", "disk", "--ollama", "http://example.com:11434/");
        Assert.Equal(ExitCodes.Refused, remote.Exit);
        Assert.Contains("--ollama must be", remote.Error);
        Assert.Null(remote.Cli);
    }

    [Fact]
    public async Task RedactsWithTheDenyListAndTheHostNames()
    {
        var path = Path.GetTempFileName();
        try
        {
            File.WriteAllText(path, """{"schema":"projectbuddy/redaction-denylist@1","terms":["acme-corp"]}""");
            var sources = new FakeSources();
            sources.AdapterList.Add(new Adapter("acme-corp uplink", "ethernet", "up", 1000, true, true, []));
            sources.AdapterList.Add(new Adapter("BUILDHOST-01 bridge", "ethernet", "up", 1000, true, true, []));
            var (exit, output, _, _, _) = await Run(sources, "run", "network-link", "--deny-list", path);
            Assert.Equal(0, exit);
            Assert.DoesNotContain("acme-corp", output, StringComparison.OrdinalIgnoreCase);
            Assert.DoesNotContain("BUILDHOST-01", output, StringComparison.OrdinalIgnoreCase);
            Assert.Equal(2, (int)JsonNode.Parse(output)!["redaction"]!["private"]!);

            File.WriteAllText(path, """{"terms":[]}""");
            var invalid = await Run(sources, "run", "disk", "--deny-list", path);
            Assert.Equal(ExitCodes.Refused, invalid.Exit);
            Assert.Contains("refused: the deny list must be", invalid.Error);
            Assert.Empty(invalid.Output);
        }
        finally
        {
            File.Delete(path);
        }

        var missing = await Run(new FakeSources(), "run", "disk", "--deny-list", path);
        Assert.Equal(ExitCodes.Refused, missing.Exit);
        Assert.Equal("refused: the deny list cannot be read: FileNotFoundException" + Environment.NewLine, missing.Error);
        Assert.Contains("cannot be read: ArgumentException", (await Run(new FakeSources(), "run", "disk", "--deny-list", "")).Error);
    }

    [Fact]
    public void LoadsTheHostNamesWithoutADenyList()
    {
        var redaction = HostRedactor.Load(null, "BUILDHOST-01", "builder").Redact("""{"name":"builder on BUILDHOST-01"}""");
        Assert.DoesNotContain("builder", redaction.Text, StringComparison.Ordinal);
        Assert.DoesNotContain("BUILDHOST-01", redaction.Text, StringComparison.Ordinal);
    }
}
