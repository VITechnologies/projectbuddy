using System.Text;
using System.Text.Json.Nodes;
using ProjectBuddy.PeerRedact;
using Xunit;

namespace ProjectBuddy.PeerProbes.Tests;

public sealed class RegistryTests
{
    private const string Identity = "projectbuddy:0.1-qwen2.5-7b";

    internal static Redaction Same(string text) => new(text, new Dictionary<string, int>());

    internal static async Task<(JsonObject Envelope, string Text)> Invoke(FakeSources sources, string tool, JsonObject? arguments = null, ProbeOptions? options = null, Func<string, Redaction>? redact = null)
    {
        var text = await new Registry(sources, redact ?? Same, options ?? new ProbeOptions()).InvokeAsync(tool, arguments);
        var envelope = JsonNode.Parse(text)!.AsObject();
        Assert.Equal(Registry.Schema, (string?)envelope["schema"]);
        Assert.Equal(tool, (string?)envelope["tool"]);
        Assert.True(Encoding.UTF8.GetByteCount(text) <= (options ?? new ProbeOptions()).MaxResultBytes);
        return (envelope, text);
    }

    private static JsonObject Data((JsonObject Envelope, string Text) result) => result.Envelope["data"]!.AsObject();

    private static bool Truncated((JsonObject Envelope, string Text) result) => (bool)result.Envelope["truncated"]!;

    private static FakeSources Windows() => new();

    [Fact]
    public void OffersAClosedListOfTools()
    {
        Assert.Equal(["network-link", "disk", "ollama", "vm-states", "services", "event-errors"], Registry.Tools.Select(tool => tool.Name));
        Assert.All(Registry.Tools.Where(tool => tool.Name != "event-errors"), tool => Assert.Empty(tool.Arguments));
        Assert.Equal(new IntArgument("hours", 1, 72, 24, "How many hours back to count."), Assert.Single(Registry.Tools[^1].Arguments));
        Assert.Equal(Identity, new ProbeOptions().IdentityModel);
        Assert.Equal(16 * 1024, new ProbeOptions().MaxResultBytes);
    }

    [Theory]
    [InlineData("disk", """{"hours":1}""", "disk takes no argument 'hours'")]
    [InlineData("event-errors", """{"days":1}""", "event-errors takes no argument 'days'")]
    [InlineData("event-errors", """{"hours":"5"}""", "must be a whole number")]
    [InlineData("event-errors", """{"hours":1.5}""", "must be a whole number")]
    [InlineData("event-errors", """{"hours":10000000000}""", "must be a whole number")]
    [InlineData("event-errors", """{"hours":true}""", "must be a whole number")]
    [InlineData("event-errors", """{"hours":null}""", "must be a whole number")]
    [InlineData("event-errors", """{"hours":{}}""", "must be a whole number")]
    [InlineData("event-errors", """{"hours":0}""", "must be 1-72")]
    [InlineData("event-errors", """{"hours":73}""", "must be 1-72")]
    public async Task RefusesArgumentsOutsideTheSpec(string tool, string arguments, string message)
    {
        var sources = Windows();
        var refused = await Assert.ThrowsAsync<ToolRefusedException>(() => Invoke(sources, tool, JsonNode.Parse(arguments)!.AsObject()));
        Assert.Contains(message, refused.Message);
        Assert.Empty(sources.Calls);
    }

    [Fact]
    public async Task RefusesRepeatedArguments()
    {
        var repeated = JsonNode.Parse("""{"hours":1,"hours":2}""")!.AsObject();
        var refused = await Assert.ThrowsAsync<ToolRefusedException>(() => Invoke(Windows(), "event-errors", repeated));
        Assert.Equal("event-errors arguments repeat a name", refused.Message);
    }

    [Fact]
    public async Task RefusesUnknownTools()
    {
        var refused = await Assert.ThrowsAsync<ToolRefusedException>(() => Invoke(Windows(), "shell", null));
        Assert.Equal("unknown tool 'shell'", refused.Message);
        await Assert.ThrowsAsync<ToolRefusedException>(() => Invoke(Windows(), "Disk", null));
    }

    [Fact]
    public void AcceptsArgumentsAtTheEdgesOfTheirRange()
    {
        var spec = Registry.Tools[^1];
        Assert.Equal(24, Registry.Validate(spec, null)["hours"]);
        Assert.Equal(24, Registry.Validate(spec, [])["hours"]);
        Assert.Equal(1, Registry.Validate(spec, JsonNode.Parse("""{"hours":1}""")!.AsObject())["hours"]);
        Assert.Equal(72, Registry.Validate(spec, new JsonObject { ["hours"] = 72 })["hours"]);
        Assert.Empty(Registry.Validate(Registry.Tools[0], null));
    }

    [Fact]
    public async Task ReportsNetworkAdaptersWithoutLoopback()
    {
        var sources = Windows();
        sources.AdapterList.Add(new Adapter("Wi-Fi", "wifi", "up", 866, true, false, ["fe80::1", "192.168.1.1"]));
        sources.AdapterList.Add(new Adapter("Loopback Pseudo-Interface 1", "loopback", "up", 1073, true, true, []));
        sources.AdapterList.Add(new Adapter("Ethernet", "ethernet", "down", 0, true, true, []));
        var result = await Invoke(sources, "network-link");
        Assert.False(Truncated(result));
        Assert.Equal(
            """{"adapters":[{"name":"Wi-Fi","kind":"wifi","status":"up","speedMbps":866,"ipv4":true,"ipv6":false,"gateways":["192.168.1.1","fe80::1"]},{"name":"Ethernet","kind":"ethernet","status":"down","speedMbps":0,"ipv4":true,"ipv6":true,"gateways":[]}],"omitted":0}""",
            Data(result).ToJsonString());
        Assert.Equal("{}", result.Envelope["redaction"]!.ToJsonString());

        // The cap keeps the live physical link and drops the down adapter last in order.
        sources.AdapterList.AddRange(Enumerable.Range(0, Registry.MaxAdapters).Select(index => new Adapter($"vEthernet {index:D2}", "other", "up", 10000, true, true, [])));
        var capped = await Invoke(sources, "network-link");
        Assert.True(Truncated(capped));
        var kept = Data(capped)["adapters"]!.AsArray();
        Assert.Equal(Registry.MaxAdapters, kept.Count);
        Assert.Equal("Wi-Fi", (string?)kept[0]!["name"]);
        Assert.DoesNotContain(kept, adapter => (string?)adapter!["name"] == "Ethernet");
    }

    [Fact]
    public async Task LeavesOutAdaptersThatCarryNoTraffic()
    {
        // The shape .NET reports on a Windows 11 laptop with Hyper-V, VirtualBox and Bluetooth: virtual switches report
        // as Ethernet, so the default route, not the kind, decides what comes first.
        var sources = Windows();
        sources.AdapterList.AddRange(
        [
            new("6to4 Adapter", "tunnel", "notpresent", 0, false, false, []),
            new("Bluetooth Network Connection", "ethernet", "down", 3, true, true, []),
            new("Ethernet", "ethernet", "up", 100, true, false, []),
            new("Ethernet (Kernel Debugger)", "ethernet", "notpresent", 0, false, false, []),
            new("Ethernet 2", "ethernet", "notpresent", 0, true, true, []),
            new("Ethernet-QoS Packet Scheduler-0000", "ethernet", "up", 100, false, false, []),
            new("Ethernet-WFP 802.3 MAC Layer LightWeight Filter-0000", "ethernet", "up", 100, false, false, []),
            new("Local Area Connection 2", "other", "down", 1000, true, true, []),
            new("Teredo Tunneling Pseudo-Interface", "tunnel", "up", 0, false, true, []),
            new("VirtualBox Host-Only Ethernet Adapter", "ethernet", "up", 1000, true, true, []),
            new("vEthernet (Default Switch)", "ethernet", "up", 10000, true, true, []),
            new("Wi-Fi", "wifi", "up", 866, true, true, ["192.168.1.1"]),
            new("Mystery", "fddi", "up", 100, true, false, []),
        ]);
        var data = Data(await Invoke(sources, "network-link"));
        Assert.Equal(
            ["Wi-Fi", "Ethernet", "VirtualBox Host-Only Ethernet Adapter", "vEthernet (Default Switch)", "Teredo Tunneling Pseudo-Interface", "Mystery",
                "Bluetooth Network Connection", "Local Area Connection 2", "Ethernet 2"],
            data["adapters"]!.AsArray().Select(adapter => (string?)adapter!["name"]));
        Assert.Equal(4, (int)data["omitted"]!);

        // The cap keeps the default-route link even when more virtual Ethernet switches are up.
        sources.AdapterList.AddRange(Enumerable.Range(0, Registry.MaxAdapters).Select(index => new Adapter($"vEthernet {index:D2}", "ethernet", "up", 10000, true, true, [])));
        var capped = await Invoke(sources, "network-link");
        Assert.True(Truncated(capped));
        Assert.Equal("Wi-Fi", (string?)Data(capped)["adapters"]![0]!["name"]);
    }

    [Fact]
    public async Task ListsUnaddressedAdaptersOffWindows()
    {
        // Off Windows, Supports(IPv4/IPv6) means "has an address": an unplugged NIC has none and must still be listed.
        var sources = Windows();
        sources.IsWindows = false;
        sources.AdapterList.Add(new Adapter("enp3s0", "ethernet", "down", 0, false, false, []));
        sources.AdapterList.Add(new Adapter("lo", "loopback", "up", 0, true, true, []));
        var data = Data(await Invoke(sources, "network-link"));
        Assert.Equal("enp3s0", (string?)Assert.Single(data["adapters"]!.AsArray())!["name"]);
        Assert.Equal(0, (int)data["omitted"]!);
    }

    [Fact]
    public async Task ReportsFixedDrives()
    {
        var sources = Windows();
        sources.DriveList.Add(new Drive("D:\\", "NTFS", 2_000_000_000_000, 1_500_000_000_000));
        sources.DriveList.Add(new Drive("C:\\", "NTFS", 1_000_000_000_000, 120_000_000_000));
        sources.DriveList.Add(new Drive("/snap/core24/1643", "squashfs", 70_123_520, 0));
        sources.DriveList.Add(new Drive("/cdrom", "ISO9660", 5_000_000_000, 0));
        var result = await Invoke(sources, "disk");
        Assert.False(Truncated(result));
        Assert.Equal(
            """{"drives":[{"name":"C:\\","format":"NTFS","totalBytes":1000000000000,"freeBytes":120000000000,"freePercent":12},{"name":"D:\\","format":"NTFS","totalBytes":2000000000000,"freeBytes":1500000000000,"freePercent":75}]}""",
            Data(result).ToJsonString());

        sources.DriveList.Clear();
        sources.DriveList.Add(new Drive("Z:\\", "NTFS", 0, 0));
        Assert.Null(Data(await Invoke(sources, "disk"))["drives"]![0]!["freePercent"]);
        sources.DriveList.Clear();
        sources.DriveList.AddRange(Enumerable.Range(0, Registry.MaxDrives + 1).Select(index => new Drive($"{(char)('A' + index % 26)}{index}:\\", "NTFS", 1, 1)));
        var capped = await Invoke(sources, "disk");
        Assert.True(Truncated(capped));
        Assert.Equal(Registry.MaxDrives, Data(capped)["drives"]!.AsArray().Count);
    }

    [Fact]
    public async Task ReportsOllamaModelsByDigestAndMarksTheIdentity()
    {
        var unreachable = await Invoke(Windows(), "ollama");
        Assert.Equal("""{"reachable":false}""", Data(unreachable).ToJsonString());
        Assert.False(Truncated(unreachable));

        var sources = Windows();
        sources.Ollama = FakeSources.State(models: 2, loaded: 1);
        var data = Data(await Invoke(sources, "ollama"));
        Assert.True((bool)data["reachable"]!);
        Assert.Equal("0.40.16", (string?)data["version"]);
        Assert.Equal(
            $$"""[{"digest":"sha256:{{1.ToString("D64")}}","name":"model-001:latest","identity":false},{"digest":"sha256:{{0.ToString("D64")}}","name":"{{Identity}}","identity":true}]""",
            data["models"]!.ToJsonString());
        Assert.Equal(
            $$"""[{"digest":"sha256:{{0.ToString("D64")}}","name":"{{Identity}}","sizeBytes":4683087332,"vramBytes":4683087332,"contextLength":8192}]""",
            data["loaded"]!.ToJsonString());

        var other = Data(await Invoke(sources, "ollama", options: new ProbeOptions("model-001:latest")));
        Assert.Equal([true, false], other["models"]!.AsArray().Select(model => (bool)model!["identity"]!));

        sources.Ollama = FakeSources.State(models: Registry.MaxModels + 1, loaded: 1);
        var manyModels = await Invoke(sources, "ollama");
        Assert.True(Truncated(manyModels));
        Assert.Equal(Registry.MaxModels, Data(manyModels)["models"]!.AsArray().Count);

        sources.Ollama = FakeSources.State(models: 1, loaded: Registry.MaxModels + 1);
        var manyLoaded = await Invoke(sources, "ollama");
        Assert.True(Truncated(manyLoaded));
        Assert.Equal(Registry.MaxModels, Data(manyLoaded)["loaded"]!.AsArray().Count);
    }

    private const string Info = "name=\"ubuntu-labview\"\nostype=\"Ubuntu (64-bit)\"\nmemory=8192\ncpus=4\nVMState=\"running\"\nVMStateChangeTime=\"2026-11-15T10:00:00.000000000\"\n"
        + "vrde=\"on\"\nvrdeproperty[VNCPassword]=\"hunter2\"\nCurrentSnapshotName=\"base\"\nCurrentSnapshotUUID=\"5f3c2a10-0000-4000-8000-000000000001\"\n";

    private static FakeSources WithVms(string list) => Windows().Answer("VBoxManage list vms", list);

    [Fact]
    public async Task ReportsVirtualMachinesFromAllowedKeysOnly()
    {
        var a = ParserTests.VmA;
        var b = ParserTests.VmB;
        const string c = "0a1b2c3d-0000-4000-8000-00000000000c";
        var sources = WithVms($"\"ubuntu-labview\" {{{a}}}\n\"win11\" {{{b}}}\n\"broken\" {{{c}}}\n")
            .Answer("VBoxManage list runningvms", $"\"ubuntu-labview\" {{{a}}}\n")
            .Answer($"VBoxManage showvminfo {a} --machinereadable", Info)
            .Answer($"VBoxManage showvminfo {b} --machinereadable", "memory=lots\nVMState=\"poweroff\"\n")
            .Answer($"VBoxManage showvminfo {c} --machinereadable", "", exitCode: 1);
        var result = await Invoke(sources, "vm-states");
        Assert.False(Truncated(result));
        Assert.DoesNotContain("hunter2", result.Text);
        Assert.Equal(
            $$"""{"available":true,"vms":[{"name":"broken","uuid":"{{c}}","running":false,"error":"showvminfo exited 1"},"""
            + $$"""{"name":"ubuntu-labview","uuid":"{{a}}","running":true,"state":"running","stateChanged":"2026-11-15T10:00:00.000000000","ostype":"Ubuntu (64-bit)","memoryMb":8192,"cpus":4,"vrde":"on","currentSnapshot":"base","currentSnapshotUuid":"5f3c2a10-0000-4000-8000-000000000001"},"""
            + $$"""{"name":"win11","uuid":"{{b}}","running":false,"state":"poweroff"}]}""",
            Data(result).ToJsonString());

        // Without a running list, "running" is unknown rather than false.
        sources.Commands["VBoxManage list runningvms"] = new CommandResult(1, "", false);
        sources.Commands.Remove($"VBoxManage showvminfo {c} --machinereadable");
        var unknown = Data(await Invoke(sources, "vm-states"))["vms"]!.AsArray();
        Assert.All(unknown, vm => Assert.Null(vm!["running"]));
        Assert.Equal("VBoxManage is not installed", (string?)unknown[0]!["error"]);
        sources.Commands.Remove("VBoxManage list runningvms");
        Assert.All(Data(await Invoke(sources, "vm-states"))["vms"]!.AsArray(), vm => Assert.Null(vm!["running"]));
    }

    [Fact]
    public async Task ReportsWhenVirtualBoxIsMissingOrFailing()
    {
        Assert.Equal("""{"available":false,"reason":"VBoxManage is not installed"}""", Data(await Invoke(Windows(), "vm-states")).ToJsonString());

        var failing = await Invoke(Windows().Answer("VBoxManage list vms", "partial", exitCode: 1, truncated: true), "vm-states");
        Assert.Equal("""{"available":true,"error":"list vms exited 1"}""", Data(failing).ToJsonString());
        Assert.True(Truncated(failing));
    }

    [Fact]
    public async Task MarksVirtualMachineReadsThatWereCut()
    {
        var a = ParserTests.VmA;
        Assert.True(Truncated(await Invoke(WithVms("").Answer("VBoxManage list vms", "", truncated: true), "vm-states")));
        Assert.True(Truncated(await Invoke(WithVms($"\"a\" {{{a}}}\n").Answer("VBoxManage list runningvms", "", truncated: true), "vm-states")));
        Assert.True(Truncated(await Invoke(WithVms($"\"a\" {{{a}}}\n").Answer($"VBoxManage showvminfo {a} --machinereadable", Info, truncated: true), "vm-states")));
        Assert.False(Truncated(await Invoke(WithVms($"\"a\" {{{a}}}\n").Answer("VBoxManage list runningvms", ""), "vm-states")));

        var many = string.Concat(Enumerable.Range(0, Registry.MaxVms + 1).Select(index => $"\"vm{index:D2}\" {{0a1b2c3d-0000-4000-8000-{index:D12}}}\n"));
        var sources = WithVms(many);
        var capped = await Invoke(sources, "vm-states");
        Assert.True(Truncated(capped));
        Assert.Equal(Registry.MaxVms, Data(capped)["vms"]!.AsArray().Count);
        Assert.Equal(Registry.MaxVms, sources.Calls.Count(call => call.Contains("showvminfo", StringComparison.Ordinal)));
    }

    private const string ScQuery = """
        SERVICE_NAME: Spooler
                STATE              : 4  RUNNING
        SERVICE_NAME: actions.runner.Org.BUILDHOST-01
                STATE              : 4  RUNNING
        SERVICE_NAME: GitLab-Runner
                STATE              : 1  STOPPED
        SERVICE_NAME: ProjectBuddy-GitLabRunner-mprr
                STATE              : 4  RUNNING
        SERVICE_NAME: my-gitlab-runner
                STATE              : 4  RUNNING
        """;

    [Fact]
    public async Task ReportsOnlyAllowedServicesAndTasks()
    {
        var sources = Windows().Answer("sc.exe query type= service state= all", ScQuery)
            .Answer("sc.exe qc actions.runner.Org.BUILDHOST-01", "        START_TYPE         : 2   AUTO_START  (DELAYED)\n")
            .Answer("sc.exe qc GitLab-Runner", "", exitCode: 1060)
            .Answer("schtasks.exe /query /tn ProjectBuddy-Bus /fo csv /v", "\"TaskName\",\"Status\",\"Last Result\"\n\"\\ProjectBuddy-Bus\",\"Running\",\"267009\"\n");
        var result = await Invoke(sources, "services");
        Assert.False(Truncated(result));
        Assert.Equal(
            """{"available":true,"services":[{"name":"GitLab-Runner","githubRunner":false,"state":"STOPPED","startType":"unknown"},{"name":"ProjectBuddy-GitLabRunner-mprr","githubRunner":false,"state":"RUNNING","startType":"unknown"},{"name":"actions.runner.Org.BUILDHOST-01","githubRunner":true,"state":"RUNNING","startType":"AUTO_START_DELAYED"}],"tasks":"""
            + """[{"name":"ProjectBuddy-Bus","state":"Running","lastResult":"267009"},{"name":"ProjectBuddy-Ollama","state":"absent","lastResult":""}]}""",
            Data(result).ToJsonString());
        Assert.DoesNotContain(sources.Calls, call => call.Contains("Spooler", StringComparison.Ordinal) || call.Contains("my-gitlab-runner", StringComparison.Ordinal));
    }

    [Fact]
    public async Task ReportsServiceQueryFailuresAndCaps()
    {
        var failing = Data(await Invoke(Windows().Answer("sc.exe query type= service state= all", "", exitCode: 5), "services"));
        Assert.Equal("""{"available":true,"services":[],"error":"sc.exe query failed","tasks":[{"name":"ProjectBuddy-Bus","state":"absent","lastResult":""},{"name":"ProjectBuddy-Ollama","state":"absent","lastResult":""}]}""", failing.ToJsonString());

        Assert.True(Truncated(await Invoke(Windows().Answer("sc.exe query type= service state= all", ScQuery, truncated: true), "services")));

        var many = string.Concat(Enumerable.Range(0, Registry.MaxServices + 1).Select(index => $"SERVICE_NAME: actions.runner.r{index:D2}\n  STATE : 4  RUNNING\n"));
        var sources = Windows().Answer("sc.exe query type= service state= all", many);
        var capped = await Invoke(sources, "services");
        Assert.True(Truncated(capped));
        Assert.Equal(Registry.MaxServices, Data(capped)["services"]!.AsArray().Count);
        Assert.Equal(Registry.MaxServices, sources.Calls.Count(call => call.StartsWith("sc.exe qc", StringComparison.Ordinal)));
    }

    [Theory]
    [InlineData("services")]
    [InlineData("event-errors")]
    public async Task WindowsToolsAreUnavailableElsewhere(string tool)
    {
        var sources = Windows();
        sources.IsWindows = false;
        var result = await Invoke(sources, tool);
        Assert.Equal("""{"available":false,"reason":"Windows only"}""", Data(result).ToJsonString());
        Assert.Empty(sources.Calls);
    }

    private static string Query(int hours) => $"wevtutil.exe qe {{0}} /q:*[System[(Level=1 or Level=2) and TimeCreated[timediff(@SystemTime) <= {hours * 3_600_000L}]]] /f:xml /c:2000 /rd:true";

    private static FakeSources WithEvents(string system, string application, int hours = 24) =>
        Windows().Answer(string.Format(Query(hours), "System"), system).Answer(string.Format(Query(hours), "Application"), application);

    [Fact]
    public async Task CountsErrorEventsByProviderAndId()
    {
        var system = string.Concat(Enumerable.Repeat(ParserTests.Event("Service Control Manager", "7000", "2"), 3)) + ParserTests.Event("disk", "11", "1") + ParserTests.Event("odd", "5", "4");
        var application = ParserTests.Event("Application Error", "1000", "2") + ParserTests.Event("Service Control Manager", "7000", "2");
        var result = await Invoke(WithEvents(system, application, hours: 2), "event-errors", new JsonObject { ["hours"] = 2 });
        Assert.False(Truncated(result));
        Assert.DoesNotContain("secret.txt", result.Text);
        Assert.Equal(
            """{"available":true,"hours":2,"groups":[{"log":"System","provider":"Service Control Manager","id":7000,"level":"error","count":3},"""
            + """{"log":"Application","provider":"Application Error","id":1000,"level":"error","count":1},{"log":"Application","provider":"Service Control Manager","id":7000,"level":"error","count":1},"""
            + """{"log":"System","provider":"disk","id":11,"level":"critical","count":1},{"log":"System","provider":"odd","id":5,"level":"level-4","count":1}],"errors":[]}""",
            Data(result).ToJsonString());
    }

    [Fact]
    public async Task ReportsEventQueriesThatFailedOrWereCut()
    {
        var sources = Windows().Answer(string.Format(Query(24), "Application"), ParserTests.Event("a", "1", "2"));
        var failed = Data(await Invoke(sources, "event-errors"));
        Assert.Equal("""[{"log":"System","error":"wevtutil query failed"}]""", failed["errors"]!.ToJsonString());
        Assert.Single(failed["groups"]!.AsArray());
        sources.Answer(string.Format(Query(24), "System"), "", exitCode: 15007);
        Assert.Single(Data(await Invoke(sources, "event-errors"))["errors"]!.AsArray());

        Assert.True(Truncated(await Invoke(WithEvents(ParserTests.Event("a", "1", "2") + "<Event><Sys", ""), "event-errors")));
        Assert.True(Truncated(await Invoke(WithEvents("", "").Answer(string.Format(Query(24), "System"), "", truncated: true), "event-errors")));

        var full = WithEvents(string.Concat(Enumerable.Repeat(ParserTests.Event("a", "1", "2"), Registry.MaxEventsPerLog)), "");
        var atCap = await Invoke(full, "event-errors");
        Assert.True(Truncated(atCap));
        Assert.Equal(Registry.MaxEventsPerLog, (int)Data(atCap)["groups"]![0]!["count"]!);

        var groups = WithEvents(string.Concat(Enumerable.Range(0, Registry.MaxGroups + 1).Select(index => ParserTests.Event("p", index.ToString(), "2"))), "");
        var capped = await Invoke(groups, "event-errors");
        Assert.True(Truncated(capped));
        Assert.Equal(Registry.MaxGroups, Data(capped)["groups"]!.AsArray().Count);
    }

    [Fact]
    public async Task TrimsArraysToTheByteCap()
    {
        var sources = Windows();
        sources.DriveList.AddRange(Enumerable.Range(0, 20).Select(index => new Drive($"{(char)('C' + index)}:\\", "NTFS", 1_000_000_000_000, 1)));
        var options = new ProbeOptions(MaxResultBytes: 1000);
        var result = await Invoke(sources, "disk", options: options);
        Assert.True(Truncated(result));
        var kept = Data(result)["drives"]!.AsArray().Count;
        Assert.InRange(kept, 1, 19);
        Assert.Equal("C:\\", (string?)Data(result)["drives"]![0]!["name"]);

        // The first trimmable array is emptied before the next one is touched.
        sources.Ollama = FakeSources.State(models: 10, loaded: 10);
        var ollama = await Invoke(sources, "ollama", options: options);
        Assert.True(Truncated(ollama));
        Assert.Empty(Data(ollama)["models"]!.AsArray());
        Assert.NotEmpty(Data(ollama)["loaded"]!.AsArray());
    }

    [Fact]
    public async Task FailsClosedWhenTheResultCannotBeMadeSafe()
    {
        var floor = new ProbeOptions(MaxResultBytes: Registry.MinResultBytes);
        var untrimmable = await Invoke(Windows().Answer("sc.exe query type= service state= all", "", exitCode: 5), "services", options: floor);
        Assert.Equal("""{"schema":"projectbuddy/peer-tool-result@1","tool":"services","truncated":true,"error":"the result is over 256 bytes"}""", untrimmable.Text);

        var many = string.Concat(Enumerable.Range(0, 8).Select(index => $"SERVICE_NAME: actions.runner.r{index}\n  STATE : 4  RUNNING\n"));
        var emptied = await Invoke(Windows().Answer("sc.exe query type= service state= all", many), "services", options: floor);
        Assert.Equal("the result is over 256 bytes", (string?)emptied.Envelope["error"]);

        var broken = await Invoke(Windows(), "disk", redact: text => new Redaction(text + "\"", new Dictionary<string, int> { ["private"] = 1 }));
        Assert.Equal("""{"schema":"projectbuddy/peer-tool-result@1","tool":"disk","truncated":true,"error":"the redacted result is not valid JSON"}""", broken.Text);

        var throwing = await Invoke(Windows(), "disk", redact: _ => throw new RedactionException("C:\\Users\\builder is too long"));
        Assert.Equal("""{"schema":"projectbuddy/peer-tool-result@1","tool":"disk","truncated":true,"error":"the redaction failed: RedactionException"}""", throwing.Text);

        var refused = Assert.Throws<ArgumentOutOfRangeException>(() => new Registry(Windows(), Same, new ProbeOptions(MaxResultBytes: Registry.MinResultBytes - 1)));
        Assert.Contains("at least 256", refused.Message);
        var longest = Registry.Tools.Max(tool => tool.Name.Length);
        Assert.True(Encoding.UTF8.GetByteCount(untrimmable.Text) - "services".Length + longest - "256".Length + int.MaxValue.ToString().Length <= Registry.MinResultBytes);
    }

    [Fact]
    public async Task ReportsAFailedProbeByExceptionTypeOnly()
    {
        var sources = Windows();
        sources.Fault = new InvalidOperationException("C:\\Users\\builder\\secret");
        var failed = await Invoke(sources, "network-link");
        Assert.Equal("""{"schema":"projectbuddy/peer-tool-result@1","tool":"network-link","truncated":true,"error":"the probe failed: InvalidOperationException"}""", failed.Text);
    }

    [Fact]
    public async Task RedactsEveryResultAndReportsTheCounts()
    {
        var redactor = Redactor.ForHost(["acme-corp"], "BUILDHOST-01", "builder");
        var a = ParserTests.VmA;
        var sources = Windows()
            .Answer("VBoxManage list vms", $"\"acme-corp-build\" {{{a}}}\n")
            .Answer("VBoxManage list runningvms", "")
            .Answer($"VBoxManage showvminfo {a} --machinereadable", Info.Replace("base", "C:\\\\Users\\\\builder\\\\snapshots\\\\x", StringComparison.Ordinal))
            .Answer("sc.exe query type= service state= all", ScQuery)
            .Answer(string.Format(Query(24), "System"), ParserTests.Event("BUILDHOST-01 agent", "7", "2"))
            .Answer(string.Format(Query(24), "Application"), "");
        sources.AdapterList.Add(new Adapter("Ethernet acme-corp", "ethernet", "up", 1000, true, false, ["10.0.0.1"]));
        sources.DriveList.Add(new Drive("C:\\", "NTFS", 1_000_000_000_000, 120_000_000_000));
        sources.Ollama = FakeSources.State();

        foreach (var tool in Registry.Tools.Select(spec => spec.Name))
        {
            var (envelope, text) = await Invoke(sources, tool, redact: redactor.Redact);
            Assert.Null(envelope["error"]);
            Assert.DoesNotContain("BUILDHOST-01", text, StringComparison.OrdinalIgnoreCase);
            Assert.DoesNotContain("acme-corp", text, StringComparison.OrdinalIgnoreCase);
            Assert.DoesNotContain("builder", text, StringComparison.OrdinalIgnoreCase);
            Assert.DoesNotContain("hunter2", text, StringComparison.Ordinal);
        }

        var services = await Invoke(sources, "services", redact: redactor.Redact);
        Assert.True(services.Envelope["redaction"]!.AsObject().Sum(count => (int)count.Value!) > 0);
        var disk = Data(await Invoke(sources, "disk", redact: redactor.Redact));
        Assert.Equal(1_000_000_000_000, (long)disk["drives"]![0]!["totalBytes"]!);
        var counts = (await Invoke(sources, "vm-states", redact: redactor.Redact)).Envelope["redaction"]!.AsObject();
        Assert.Equal(counts.Select(count => count.Key).Order(StringComparer.Ordinal), counts.Select(count => count.Key));
    }
}
