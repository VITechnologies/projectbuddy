using Xunit;

namespace ProjectBuddy.PeerAsk.Tests;

public class AskTests
{
    private static readonly string[] Required =
        ["--host", "10.0.0.2", "--user", "peer", "--identity", "/k/id", "--known-hosts", "/k/known"];

    private const string GoodAnswer = "{\"schema\":\"projectbuddy/peer-ask@1\",\"tool\":\"0.1.0\",\"verb\":\"identity\",\"data\":{}}";

    private static (int Code, string Out, string Err, FakeRunner Runner) Ask(ProcessResult result, params string[] extra)
    {
        var runner = new FakeRunner();
        var options = PeerAsk.Ask.Parse(["identity", .. Required, .. extra], out _)!;
        runner.On($"{options.Ssh} {string.Join(' ', PeerAsk.Ask.SshArguments(options))}", result);
        var stdout = new StringWriter();
        var stderr = new StringWriter();
        var code = PeerAsk.Ask.Execute(["identity", .. Required, .. extra], runner, stdout, stderr);
        return (code, stdout.ToString(), stderr.ToString(), runner);
    }

    [Fact]
    public void Builds_one_fixed_locked_down_ssh_invocation()
    {
        var options = PeerAsk.Ask.Parse(["vbox", .. Required], out var problem)!;
        Assert.Equal("", problem);
        var argv = PeerAsk.Ask.SshArguments(options);
        Assert.Equal(["-F", "none"], argv.Take(2));
        Assert.Contains("IdentityFile=/k/id", argv);
        Assert.Contains("IdentitiesOnly=yes", argv);
        Assert.Contains("IdentityAgent=none", argv);
        Assert.Contains("UserKnownHostsFile=/k/known", argv);
        Assert.Contains("GlobalKnownHostsFile=none", argv);
        Assert.Contains("StrictHostKeyChecking=yes", argv);
        Assert.Contains("BatchMode=yes", argv);
        Assert.Contains("ConnectTimeout=5", argv);
        Assert.Contains("ClearAllForwardings=yes", argv);
        Assert.Contains("RequestTTY=no", argv);
        Assert.Equal(["-l", "peer", "--", "10.0.0.2", "vbox"], argv.TakeLast(5));
        Assert.Equal("ssh", options.Ssh);
    }

    [Fact]
    public void Returns_the_validated_answer()
    {
        var (code, output, error, runner) = Ask(FakeRunner.Ok(GoodAnswer + "\r\n"), "--connect-timeout", "3", "--ssh", "/usr/bin/ssh");
        Assert.Equal(0, code);
        Assert.Equal(GoodAnswer + Environment.NewLine, output);
        Assert.Equal("", error);
        Assert.Equal("/usr/bin/ssh", runner.Calls[0].File);
        Assert.Equal(TimeSpan.FromSeconds(33), runner.Calls[0].Timeout);
        Assert.Contains("ConnectTimeout=3", runner.Calls[0].Args);
    }

    [Theory]
    [InlineData(new string[0], "missing verb")]
    [InlineData(new[] { "--host" }, "missing verb")]
    [InlineData(new[] { "rm" }, "unknown verb 'rm'")]
    [InlineData(new[] { "identity", "--bogus", "x" }, "bad option '--bogus'")]
    [InlineData(new[] { "identity", "--host" }, "bad option '--host'")]
    [InlineData(new[] { "identity", "--host", "a", "--host", "b" }, "bad option '--host'")]
    [InlineData(new[] { "identity", "--host", "a" }, "missing --user")]
    [InlineData(new[] { "identity", "--host", "", "--user", "u", "--identity", "i", "--known-hosts", "k" }, "missing --host")]
    [InlineData(new[] { "identity", "--host", "a", "--user", "u", "--identity", "i", "--known-hosts", "k", "--connect-timeout", "0" }, "--connect-timeout must be 1..60")]
    [InlineData(new[] { "identity", "--host", "a", "--user", "u", "--identity", "i", "--known-hosts", "k", "--connect-timeout", "61" }, "--connect-timeout must be 1..60")]
    [InlineData(new[] { "identity", "--host", "a", "--user", "u", "--identity", "i", "--known-hosts", "k", "--connect-timeout", "x" }, "--connect-timeout must be 1..60")]
    public void Rejects_bad_arguments_without_running_ssh(string[] args, string problem)
    {
        var runner = new FakeRunner();
        var stderr = new StringWriter();
        var code = PeerAsk.Ask.Execute(args, runner, new StringWriter(), stderr);
        Assert.Equal(ExitCodes.Usage, code);
        Assert.StartsWith($"projectbuddy-ask: {problem}{Environment.NewLine}{Program.Usage}", stderr.ToString());
        Assert.Empty(runner.Calls);
    }

    [Theory]
    [InlineData(false, false, -1, "", ExitCodes.SshError, "ssh-error: ssh could not be started")]
    [InlineData(true, true, -1, "", ExitCodes.Unreachable, "unreachable: timed out")]
    [InlineData(true, false, 64, "projectbuddy-ask: request refused", ExitCodes.PeerRefused, "refused: the peer refused the request")]
    [InlineData(true, false, 255, "Host key verification failed.", ExitCodes.HostKey, "host-key: Host key verification failed.")]
    [InlineData(true, false, 255, "@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@", ExitCodes.HostKey, "host-key: @@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@")]
    [InlineData(true, false, 255, "No ED25519 host key is known for 10.0.0.2", ExitCodes.HostKey, "host-key: No ED25519 host key is known for 10.0.0.2")]
    [InlineData(true, false, 255, "peer@10.0.0.2: Permission denied (publickey).", ExitCodes.Auth, "auth: peer@10.0.0.2: Permission denied (publickey).")]
    [InlineData(true, false, 255, "ssh: connect to host 10.0.0.2 port 22: Connection timed out", ExitCodes.Unreachable, "unreachable: ssh: connect to host 10.0.0.2 port 22: Connection timed out")]
    [InlineData(true, false, 255, "ssh: connect to host 10.0.0.2 port 22: No route to host", ExitCodes.Unreachable, "unreachable: ssh: connect to host 10.0.0.2 port 22: No route to host")]
    [InlineData(true, false, 255, "ssh: connect to host 10.0.0.2 port 22: Network is unreachable", ExitCodes.Unreachable, "unreachable: ssh: connect to host 10.0.0.2 port 22: Network is unreachable")]
    [InlineData(true, false, 255, "ssh: connect to host 10.0.0.2 port 22: Connection refused", ExitCodes.Unreachable, "unreachable: ssh: connect to host 10.0.0.2 port 22: Connection refused")]
    [InlineData(true, false, 255, "Connection closed by 10.0.0.2 port 22", ExitCodes.Unreachable, "unreachable: Connection closed by 10.0.0.2 port 22")]
    [InlineData(true, false, 255, "kex_exchange_identification: read: Connection reset by peer", ExitCodes.Unreachable, "unreachable: kex_exchange_identification: read: Connection reset by peer")]
    [InlineData(true, false, 255, "something else", ExitCodes.SshError, "ssh-error: something else")]
    [InlineData(true, false, 1, "boom\nmore", ExitCodes.PeerError, "peer-error: exit 1: boom")]
    public void Classifies_failures(bool started, bool timedOut, int exit, string stderrText, int expected, string message)
    {
        var (code, output, error, _) = Ask(new ProcessResult(started, timedOut, exit, "", stderrText));
        Assert.Equal(expected, code);
        Assert.Equal("", output);
        Assert.Equal($"projectbuddy-ask: {message}{Environment.NewLine}", error);
    }

    [Theory]
    [InlineData("not json")]
    [InlineData("[]")]
    [InlineData("{\"schema\":\"other@1\",\"verb\":\"identity\",\"data\":{}}")]
    [InlineData("{\"schema\":1,\"verb\":\"identity\",\"data\":{}}")]
    [InlineData("{\"verb\":\"identity\",\"data\":{}}")]
    [InlineData("{\"schema\":\"projectbuddy/peer-ask@1\",\"verb\":\"vbox\",\"data\":{}}")]
    [InlineData("{\"schema\":\"projectbuddy/peer-ask@1\",\"verb\":2,\"data\":{}}")]
    [InlineData("{\"schema\":\"projectbuddy/peer-ask@1\",\"data\":{}}")]
    [InlineData("{\"schema\":\"projectbuddy/peer-ask@1\",\"verb\":\"identity\",\"data\":[]}")]
    [InlineData("{\"schema\":\"projectbuddy/peer-ask@1\",\"verb\":\"identity\"}")]
    public void Rejects_answers_that_are_not_peer_ask_documents(string answer)
    {
        var (code, _, error, _) = Ask(FakeRunner.Ok(answer));
        Assert.Equal(ExitCodes.Protocol, code);
        Assert.Contains("protocol:", error);
    }

    [Fact]
    public void Rejects_oversized_answers()
    {
        var (code, _, error, _) = Ask(FakeRunner.Ok(GoodAnswer + new string(' ', PeerAsk.Serve.MaxOutputBytes)));
        Assert.Equal(ExitCodes.Protocol, code);
        Assert.Contains("exceeds the output limit", error);
    }
}

public class ProgramTests
{
    private static (int Code, string Out, string Err) Run(string[] args, string? sshCommand = null)
    {
        var stdout = new StringWriter();
        var stderr = new StringWriter();
        var code = Program.Run(args, name => name == "SSH_ORIGINAL_COMMAND" ? sshCommand : null, new FakeRunner(), new FakeFacts(), stdout, stderr);
        return (code, stdout.ToString(), stderr.ToString());
    }

    [Fact]
    public void Serve_reads_the_original_ssh_command()
    {
        Assert.Equal(0, Run(["serve"], "identity").Code);
        Assert.Equal(ExitCodes.Refused, Run(["serve"], "cmd.exe").Code);
        Assert.Equal(ExitCodes.Refused, Run(["serve"]).Code);
    }

    [Fact]
    public void Ask_is_dispatched()
    {
        Assert.Equal(ExitCodes.Usage, Run(["ask"]).Code);
    }

    [Fact]
    public void Version_prints_tool_and_schema()
    {
        Assert.Equal((0, $"{Versions.Tool} {Versions.Schema}{Environment.NewLine}", ""), Run(["--version"]));
    }

    [Theory]
    [InlineData]
    [InlineData("serve", "extra")]
    [InlineData("identity")]
    public void Anything_else_prints_usage(params string[] args)
    {
        var (code, _, error) = Run(args);
        Assert.Equal(ExitCodes.Usage, code);
        Assert.Equal(Program.Usage + Environment.NewLine, error);
    }

    [Fact]
    public void Main_runs_against_the_real_host()
    {
        Assert.Equal(0, Program.Main(["--version"]));
    }
}

public class HostTests
{
    private static readonly TimeSpan Short = TimeSpan.FromSeconds(10);

    [Fact]
    public void Runner_captures_output_and_exit_code()
    {
        var result = new SystemProcessRunner().Run("/bin/sh", ["-c", "printf out; printf err >&2; exit 3"], Short);
        Assert.Equal(new ProcessResult(true, false, 3, "out", "err"), result);
    }

    [Fact]
    public void Runner_reports_a_missing_program()
    {
        Assert.Equal(new ProcessResult(false, false, -1, "", ""), new SystemProcessRunner().Run("/nonexistent/tool", [], Short));
    }

    [Fact]
    public void Runner_kills_on_timeout()
    {
        var result = new SystemProcessRunner().Run("/bin/sh", ["-c", "printf early; sleep 30"], TimeSpan.FromMilliseconds(500));
        Assert.True(result.TimedOut);
        Assert.Equal("early", result.Stdout);
    }

    [Fact]
    public void Runner_caps_captured_output()
    {
        var result = new SystemProcessRunner().Run("/bin/sh", ["-c", $"head -c {SystemProcessRunner.MaxCapturedChars + 10} /dev/zero | tr '\\0' x"], Short);
        Assert.Equal(SystemProcessRunner.MaxCapturedChars, result.Stdout.Length);
    }

    [Fact]
    public void Facts_describe_this_host()
    {
        var facts = new SystemFacts();
        Assert.Equal("linux", facts.Platform);
        Assert.True(facts.UptimeSeconds > 0);
        Assert.True(facts.OsBuild >= 0);
        Assert.Equal("VBoxManage", facts.VBoxManagePath);
        Assert.Contains(facts.FixedVolumes(), volume => volume.Name == "/" && volume.TotalBytes > 0);
    }
}
