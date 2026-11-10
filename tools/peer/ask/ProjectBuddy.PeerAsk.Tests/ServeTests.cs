using System.Text.Json;
using Xunit;

namespace ProjectBuddy.PeerAsk.Tests;

internal sealed class FakeRunner : IProcessRunner
{
    private readonly Dictionary<string, ProcessResult> _results = new(StringComparer.Ordinal);

    public List<(string File, IReadOnlyList<string> Args, TimeSpan Timeout)> Calls { get; } = [];

    public FakeRunner On(string command, ProcessResult result)
    {
        _results[command] = result;
        return this;
    }

    public ProcessResult Run(string fileName, IReadOnlyList<string> arguments, TimeSpan timeout)
    {
        Calls.Add((fileName, arguments, timeout));
        var key = $"{fileName} {string.Join(' ', arguments)}";
        return _results.TryGetValue(key, out var result) ? result : new ProcessResult(false, false, -1, "", "");
    }

    public static ProcessResult Ok(string stdout) => new(true, false, 0, stdout, "");
}

internal sealed class FakeFacts : IHostFacts
{
    public string Platform => "windows";
    public int OsBuild => 26200;
    public long UptimeSeconds => 4242;
    public string VBoxManagePath => "VBoxManage.exe";
    public string OllamaPath => "ollama";
    public string LbabusPath => "lbabus";
    public string SshPath => "/usr/bin/ssh";
    public List<Volume> Volumes { get; } = [new("C:\\", 100, 200)];
    public Exception? VolumesError { get; set; }
    public IReadOnlyList<Volume> FixedVolumes() => VolumesError is null ? Volumes : throw VolumesError;
}

public class ServeTests
{
    private static (int Code, string Out, string Err) Serve(string? command, FakeRunner? runner = null, FakeFacts? facts = null)
    {
        var stdout = new StringWriter();
        var stderr = new StringWriter();
        var code = PeerAsk.Serve.Execute(command, runner ?? new FakeRunner(), facts ?? new FakeFacts(), stdout, stderr);
        return (code, stdout.ToString(), stderr.ToString());
    }

    private static JsonElement Data(string json)
    {
        using var document = JsonDocument.Parse(json);
        var root = document.RootElement;
        Assert.Equal(Versions.Schema, root.GetProperty("schema").GetString());
        Assert.Equal(Versions.Tool, root.GetProperty("tool").GetString());
        return root.GetProperty("data").Clone();
    }

    [Theory]
    [InlineData(null)]
    [InlineData("")]
    [InlineData("identity ")]
    [InlineData("IDENTITY")]
    [InlineData("identity; whoami")]
    [InlineData("powershell")]
    [InlineData("vbox && del")]
    public void Refuses_anything_off_the_allow_list(string? command)
    {
        var runner = new FakeRunner();
        var (code, output, error) = Serve(command, runner);
        Assert.Equal(ExitCodes.Refused, code);
        Assert.Equal("", output);
        Assert.Equal(PeerAsk.Serve.RefusedMessage + Environment.NewLine, error);
        Assert.Empty(runner.Calls);
    }

    [Fact]
    public void Identity_reports_platform_build_and_uptime()
    {
        var (code, output, _) = Serve("identity");
        Assert.Equal(0, code);
        var data = Data(output);
        Assert.Equal("windows", data.GetProperty("platform").GetString());
        Assert.Equal(26200, data.GetProperty("osBuild").GetInt32());
        Assert.Equal(4242, data.GetProperty("uptimeSeconds").GetInt64());
    }

    [Fact]
    public void Disk_reports_each_fixed_volume()
    {
        var facts = new FakeFacts();
        facts.Volumes.Add(new Volume("D:\\", 5, 10));
        var data = Data(Serve("disk", facts: facts).Out);
        var volumes = data.GetProperty("volumes").EnumerateArray().ToList();
        Assert.Equal(2, volumes.Count);
        Assert.Equal("D:\\", volumes[1].GetProperty("name").GetString());
        Assert.Equal(5, volumes[1].GetProperty("freeBytes").GetInt64());
        Assert.Equal(10, volumes[1].GetProperty("totalBytes").GetInt64());
    }

    [Fact]
    public void Vbox_reports_only_projectbuddy_vms_and_counts_the_rest()
    {
        var runner = new FakeRunner()
            .On("VBoxManage.exe --version", FakeRunner.Ok("7.2.14r174565\r\n"))
            .On("VBoxManage.exe list vms", FakeRunner.Ok(
                "\"projectbuddy-lane-1\" {11111111-1111-1111-1111-111111111111}\r\n" +
                "\"customer-secret-vm\" {22222222-2222-2222-2222-222222222222}\r\n" +
                "\"projectbuddy-lane-2\" {33333333-3333-3333-3333-333333333333}\r\n"))
            .On("VBoxManage.exe list runningvms", FakeRunner.Ok(
                "\"projectbuddy-lane-2\" {33333333-3333-3333-3333-333333333333}\r\n" +
                "\"customer-secret-vm\" {22222222-2222-2222-2222-222222222222}\r\n"));
        var (code, output, _) = Serve("vbox", runner);
        Assert.Equal(0, code);
        Assert.DoesNotContain("customer-secret-vm", output);
        var data = Data(output);
        Assert.True(data.GetProperty("installed").GetBoolean());
        Assert.Equal("7.2.14r174565", data.GetProperty("version").GetString());
        var vms = data.GetProperty("vms").EnumerateArray().ToList();
        Assert.Equal(["projectbuddy-lane-1", "projectbuddy-lane-2"], vms.Select(vm => vm.GetProperty("name").GetString()));
        Assert.Equal([false, true], vms.Select(vm => vm.GetProperty("running").GetBoolean()));
        Assert.Equal(1, data.GetProperty("otherVmCount").GetInt32());
        Assert.All(runner.Calls, call => Assert.Equal(PeerAsk.Serve.ToolTimeout, call.Timeout));
    }

    [Fact]
    public void Vbox_counts_unparsed_lines_and_matches_running_by_id()
    {
        var runner = new FakeRunner()
            .On("VBoxManage.exe --version", FakeRunner.Ok("7.2.14"))
            .On("VBoxManage.exe list vms", FakeRunner.Ok(
                "\"projectbuddy-a\" {aaaaaaaa-0000-0000-0000-000000000000}\n" +
                "garbage line with a \"name\"\n\n" +
                "\"projectbuddy-b\" {bbbbbbbb-0000-0000-0000-000000000000}\n"))
            .On("VBoxManage.exe list runningvms", FakeRunner.Ok(
                "\"projectbuddy-a\" {cccccccc-0000-0000-0000-000000000000}\n" +
                "\"renamed\" {BBBBBBBB-0000-0000-0000-000000000000}\n"));
        var (_, output, _) = Serve("vbox", runner);
        Assert.DoesNotContain("garbage", output);
        var data = Data(output);
        var vms = data.GetProperty("vms").EnumerateArray().ToList();
        Assert.Equal([false, true], vms.Select(vm => vm.GetProperty("running").GetBoolean()));
        Assert.Equal(1, data.GetProperty("otherVmCount").GetInt32());
    }

    [Fact]
    public void Internal_errors_never_reach_the_client()
    {
        var facts = new FakeFacts { VolumesError = new IOException("secret path D:\\customer\\x") };
        var (code, output, error) = Serve("disk", facts: facts);
        Assert.Equal(ExitCodes.Internal, code);
        Assert.Equal("", output);
        Assert.Equal(PeerAsk.Serve.InternalMessage + Environment.NewLine, error);
    }

    [Fact]
    public void Vbox_not_installed()
    {
        var data = Data(Serve("vbox").Out);
        Assert.False(data.GetProperty("installed").GetBoolean());
        Assert.False(data.TryGetProperty("vms", out _));
    }

    [Theory]
    [InlineData(true)]
    [InlineData(false)]
    public void Vbox_list_failure_is_reported_without_names(bool runningFails)
    {
        var failed = new ProcessResult(true, false, 1, "", "boom");
        var runner = new FakeRunner()
            .On("VBoxManage.exe --version", FakeRunner.Ok("7.2.14"))
            .On("VBoxManage.exe list vms", runningFails ? FakeRunner.Ok("") : failed)
            .On("VBoxManage.exe list runningvms", runningFails ? failed : FakeRunner.Ok(""));
        var data = Data(Serve("vbox", runner).Out);
        Assert.Equal("list-failed", data.GetProperty("error").GetString());
        Assert.False(data.TryGetProperty("vms", out _));
    }

    [Theory]
    [InlineData(false, false)]
    [InlineData(true, true)]
    public void Vbox_list_that_did_not_run_is_reported(bool started, bool timedOut)
    {
        var runner = new FakeRunner()
            .On("VBoxManage.exe --version", FakeRunner.Ok("7.2.14"))
            .On("VBoxManage.exe list vms", new ProcessResult(started, timedOut, 0, "\"projectbuddy-x\" {1-2}", ""));
        var data = Data(Serve("vbox", runner).Out);
        Assert.Equal("list-failed", data.GetProperty("error").GetString());
    }

    [Fact]
    public void Ollama_reports_version_and_loaded_models()
    {
        var runner = new FakeRunner()
            .On("ollama --version", FakeRunner.Ok("ollama version is 0.32.3\n"))
            .On("ollama ps", FakeRunner.Ok("NAME    ID    SIZE\nllama3.1:8b  abc  5 GB\n\nqwen2.5:14b def 9 GB\n"));
        var data = Data(Serve("ollama", runner).Out);
        Assert.True(data.GetProperty("installed").GetBoolean());
        Assert.Equal("ollama version is 0.32.3", data.GetProperty("version").GetString());
        Assert.Equal(["llama3.1:8b", "qwen2.5:14b"], data.GetProperty("loadedModels").EnumerateArray().Select(m => m.GetString()));
    }

    [Fact]
    public void Ollama_ps_failure_gives_an_empty_model_list()
    {
        var runner = new FakeRunner().On("ollama --version", FakeRunner.Ok("ollama version is 0.32.3"));
        var data = Data(Serve("ollama", runner).Out);
        Assert.Empty(data.GetProperty("loadedModels").EnumerateArray());
    }

    [Theory]
    [InlineData("ollama")]
    [InlineData("lbabus")]
    public void Missing_tool_is_reported_as_not_installed(string verb)
    {
        var data = Data(Serve(verb, new FakeRunner().On($"{verb} --version", new ProcessResult(true, true, -1, "", ""))).Out);
        Assert.False(data.GetProperty("installed").GetBoolean());
        Assert.False(data.TryGetProperty("version", out _));
    }

    [Fact]
    public void Lbabus_reports_its_version()
    {
        var data = Data(Serve("lbabus", new FakeRunner().On("lbabus --version", FakeRunner.Ok("lbabus 0.15.12\nextra"))).Out);
        Assert.Equal("lbabus 0.15.12", data.GetProperty("version").GetString());
    }

    [Fact]
    public void Long_fields_are_truncated()
    {
        var data = Data(Serve("lbabus", new FakeRunner().On("lbabus --version", FakeRunner.Ok(new string('x', 500)))).Out);
        Assert.Equal(PeerAsk.Serve.MaxFieldChars, data.GetProperty("version").GetString()!.Length);
    }

    [Fact]
    public void Output_over_the_limit_is_refused()
    {
        var facts = new FakeFacts();
        for (var i = 0; i < 400; i++)
        {
            facts.Volumes.Add(new Volume(new string('v', 40), i, i));
        }

        var (code, output, error) = Serve("disk", facts: facts);
        Assert.Equal(ExitCodes.OutputLimit, code);
        Assert.Equal("", output);
        Assert.Equal(PeerAsk.Serve.OutputLimitMessage + Environment.NewLine, error);
    }
}
