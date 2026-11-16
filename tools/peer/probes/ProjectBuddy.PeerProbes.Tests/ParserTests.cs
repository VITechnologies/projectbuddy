using Xunit;

namespace ProjectBuddy.PeerProbes.Tests;

public sealed class ParserTests
{
    public const string VmA = "0a1b2c3d-0000-4000-8000-00000000000a";
    public const string VmB = "0a1b2c3d-0000-4000-8000-00000000000b";

    [Fact]
    public void ReadsVirtualMachineLists()
    {
        var vms = Parsers.VirtualMachines($"\"ubuntu-labview\" {{{VmA.ToUpperInvariant()}}}\r\n\"win \"11\"\" {{{VmB}}}\nnot a vm line\n\"broken\" {{1234}}\n");
        Assert.Equal([new VirtualMachine("ubuntu-labview", VmA), new VirtualMachine("win \"11\"", VmB)], vms);
        Assert.Empty(Parsers.VirtualMachines(""));
    }

    [Fact]
    public void ReadsOnlyTheRequestedMachineReadableKeys()
    {
        var output = string.Join("\r\n",
            "name=\"ubuntu-labview\"",
            "memory=8192",
            "VMState=\"running\"",
            "VMState=\"second value is ignored\"",
            "vrdeproperty[VNCPassword]=\"hunter2\"",
            "\"CurrentSnapshotName\"=\"base \\\"one\\\" \\\\ two\"",
            "\"quoted\\\"key\"=\"x\"",
            "ostype=\"",
            "cpus=\"\\\"",
            "no equals sign here",
            "");
        var values = Parsers.MachineReadable(output, new HashSet<string>(["memory", "VMState", "CurrentSnapshotName", "vrdeproperty", "ostype", "cpus", "quoted\"key"]));
        Assert.Equal("8192", values["memory"]);
        Assert.Equal("running", values["VMState"]);
        Assert.Equal("base \"one\" \\ two", values["CurrentSnapshotName"]);
        Assert.Equal("x", values["quoted\"key"]);
        Assert.Equal("\"", values["ostype"]);
        Assert.Equal("\\", values["cpus"]);
        Assert.False(values.ContainsKey("vrdeproperty"));
        Assert.False(values.ContainsKey("name"));
        Assert.DoesNotContain("hunter2", string.Join(',', values.Values));
    }

    [Fact]
    public void ReadsServiceStatesAndStartTypes()
    {
        var query = string.Join("\r\n",
            "",
            "SERVICE_NAME: actions.runner.Org.BUILDHOST-01",
            "DISPLAY_NAME: GitHub Actions Runner (Org.BUILDHOST-01)",
            "        TYPE               : 10  WIN32_OWN_PROCESS  ",
            "        STATE              : 4  RUNNING ",
            "                                (STOPPABLE, NOT_PAUSABLE, ACCEPTS_SHUTDOWN)",
            "        WIN32_EXIT_CODE    : 0  (0x0)",
            "",
            "SERVICE_NAME: gitlab-runner",
            "        TYPE               : 10  WIN32_OWN_PROCESS  ",
            "        STATE              : 1  STOPPED ",
            "        STATE              : 4  RUNNING ",
            "        STATE              : 4  RUNNING ",
            "SERVICE_NAME: no-state-line",
            "");
        Assert.Equal([new ServiceState("actions.runner.Org.BUILDHOST-01", "RUNNING"), new ServiceState("gitlab-runner", "STOPPED")], Parsers.Services(query));

        Assert.Equal("AUTO_START_DELAYED", Parsers.StartType("[SC] QueryServiceConfig SUCCESS\r\n\r\nSERVICE_NAME: x\r\n        TYPE               : 10  WIN32_OWN_PROCESS\r\n        START_TYPE         : 2   AUTO_START  (DELAYED)\r\n        BINARY_PATH_NAME   : C:\\runner\\bin\\RunnerService.exe\r\n"));
        Assert.Equal("DEMAND_START", Parsers.StartType("        START_TYPE         : 3   DEMAND_START\n"));
        Assert.Equal("unknown", Parsers.StartType("        TYPE               : 10  WIN32_OWN_PROCESS\n"));
    }

    [Fact]
    public void ReadsScheduledTaskStatusByHeader()
    {
        var csv = "\"HostName\",\"TaskName\",\"Next Run Time\",\"Status\",\"Logon Mode\",\"Last Run Time\",\"Last Result\",\"Author\"\r\n"
            + "\"BUILDHOST-01\",\"\\ProjectBuddy-Bus\",\"N/A\",\"Running\",\"Interactive/Background\",\"11/15/2026 10:00:00 AM\",\"267009\",\"say \"\"hi\"\", ok\"\r\n"
            + "\"BUILDHOST-01\",\"\\ProjectBuddy-Bus\",\"N/A\",\"Ready\",\"\",\"\",\"0\",\"\"\r\n";
        Assert.Equal(new TaskState("Running", "267009"), Parsers.Task(csv));
        Assert.Equal(new TaskState("Ready", "unknown"), Parsers.Task("Status,Last Result\nReady\n"));
        Assert.Equal(new TaskState("unknown", "1"), Parsers.Task("Other,Last Result\nReady,1\n"));
        Assert.Equal(new TaskState("\"", "unknown"), Parsers.Task("Status\n\"\n"));
        Assert.Equal(new TaskState("unknown", "unknown"), Parsers.Task("\"Status\",\"Last Result\"\r\n"));
        Assert.Equal(new TaskState("unknown", "unknown"), Parsers.Task(""));
    }

    public static string Event(string provider, string id, string level, string tail = "<Version>0</Version>") =>
        $"<Event xmlns='http://schemas.microsoft.com/win/2004/08/events/event'><System><Provider Name='{provider}' Guid='{{555908D1-A6D7-4695-8E1E-26931D2012F4}}'/>"
        + $"<EventID Qualifiers='49152'>{id}</EventID>{tail}<Level>{level}</Level><Computer>BUILDHOST-01</Computer><Security UserID='S-1-5-18'/></System>"
        + "<EventData><Data Name='param1'>C:\\Users\\builder\\secret.txt</Data></EventData></Event>";

    [Fact]
    public void ReadsOnlyEventHeaders()
    {
        var output = Event("Service Control Manager", "7000", "2") + "\r\n" + Event("disk", "x", "1", tail: "")
            + "<Event><System><Provider/><Level>2</Level></System></Event>"
            + "<Event><System/><UserData><Wrapper><System><Provider Name='nested'/><EventID>1</EventID><Level>1</Level></System></Wrapper></UserData></Event>"
            + "<Provider Name='stray'/><EventID>9</EventID>"
            + "<!-- a comment --><Event><System><Level>3</Level><EventID>42</EventID></System></Event>";
        var (events, complete) = Parsers.Events(output);
        Assert.True(complete);
        Assert.Equal(
            [new EventRecord("Service Control Manager", 7000, 2), new EventRecord("disk", -1, 1), new EventRecord("", -1, 2), new EventRecord("", 42, 3)],
            events);
    }

    [Fact]
    public void KeepsTheEventsReadBeforeBrokenXml()
    {
        var (cut, cutComplete) = Parsers.Events(Event("first", "1", "2") + "<Event><System><Provider Name='second'/><Event");
        Assert.False(cutComplete);
        Assert.Equal([new EventRecord("first", 1, 2)], cut);

        var (dtd, dtdComplete) = Parsers.Events("<!DOCTYPE x [<!ENTITY e 'boom'>]><Event/>");
        Assert.False(dtdComplete);
        Assert.Empty(dtd);

        // Control characters in event data (never read) do not stop the count.
        var control = ParserTests.Event("a", "1", "2").Replace("secret.txt", "bad\u0000\u0001\u001F\uFFFE\uFFFF\uD800x\uDC00&#x0;&#x1;", StringComparison.Ordinal);
        var (controlled, controlComplete) = Parsers.Events(control + Event("b\uD83D\uDE00", "2", "1") + "\uD800");
        Assert.True(controlComplete);
        Assert.Equal([new EventRecord("a", 1, 2), new EventRecord("b\uD83D\uDE00", 2, 1)], controlled);

        var (empty, emptyComplete) = Parsers.Events("");
        Assert.True(emptyComplete);
        Assert.Empty(empty);
    }

    [Fact]
    public void FindsProgramsOnlyAtFixedPaths()
    {
        Assert.Equal(["/win/system32/sc.exe"], ProgramPaths.Candidates("sc.exe", null, isWindows: true, "/win/system32", "/pf"));
        Assert.Empty(ProgramPaths.Candidates("sc.exe", null, isWindows: false, "/win/system32", "/pf"));
        Assert.Empty(ProgramPaths.Candidates("sc.exe", null, isWindows: true, "system32", "/pf"));
        Assert.Equal(["/opt/vbox/VBoxManage"], ProgramPaths.Candidates("VBoxManage", "/opt/vbox/VBoxManage", isWindows: true, "/win/system32", "/pf"));
        Assert.Empty(ProgramPaths.Candidates("VBoxManage", "VBoxManage", isWindows: false, "/win/system32", "/pf"));
        Assert.Equal(["/pf/Oracle/VirtualBox/VBoxManage.exe"], ProgramPaths.Candidates("VBoxManage", null, isWindows: true, "/win/system32", "/pf"));

        // GetFolderPath(ProgramFiles) is empty off Windows; a relative path would resolve against the working directory.
        Assert.Empty(ProgramPaths.Candidates("VBoxManage", null, isWindows: true, "/win/system32", ""));
        Assert.Equal(["/usr/bin/VBoxManage"], ProgramPaths.Candidates("VBoxManage", null, isWindows: false, "", ""));
    }
}
