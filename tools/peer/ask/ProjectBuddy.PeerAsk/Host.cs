using System.ComponentModel;
using System.Diagnostics;
using System.Diagnostics.CodeAnalysis;
using System.Text;

namespace ProjectBuddy.PeerAsk;

public sealed record ProcessResult(bool Started, bool TimedOut, int ExitCode, string Stdout, string Stderr);

public interface IProcessRunner
{
    ProcessResult Run(string fileName, IReadOnlyList<string> arguments, TimeSpan timeout);
}

public sealed record Volume(string Name, long FreeBytes, long TotalBytes);

public interface IHostFacts
{
    string Platform { get; }
    int OsBuild { get; }
    long UptimeSeconds { get; }
    string VBoxManagePath { get; }
    IReadOnlyList<Volume> FixedVolumes();
}

public sealed class SystemProcessRunner : IProcessRunner
{
    public const int MaxCapturedChars = 1024 * 1024;

    public ProcessResult Run(string fileName, IReadOnlyList<string> arguments, TimeSpan timeout)
    {
        var info = new ProcessStartInfo(fileName)
        {
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            RedirectStandardInput = true,
            UseShellExecute = false,
            CreateNoWindow = true,
            StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8,
        };
        foreach (var argument in arguments)
        {
            info.ArgumentList.Add(argument);
        }

        using var process = new Process { StartInfo = info };
        try
        {
            process.Start();
        }
        catch (Win32Exception)
        {
            return new ProcessResult(false, false, -1, "", "");
        }

        process.StandardInput.Close();
        var stdout = process.StandardOutput.ReadToEndAsync();
        var stderr = process.StandardError.ReadToEndAsync();
        if (!process.WaitForExit(timeout))
        {
            process.Kill(entireProcessTree: true);
            process.WaitForExit();
            return new ProcessResult(true, true, -1, Cap(stdout.Result), Cap(stderr.Result));
        }

        process.WaitForExit();
        return new ProcessResult(true, false, process.ExitCode, Cap(stdout.Result), Cap(stderr.Result));
    }

    private static string Cap(string text) => text.Length <= MaxCapturedChars ? text : text[..MaxCapturedChars];
}

public sealed class SystemFacts : IHostFacts
{
    [ExcludeFromCodeCoverage(Justification = "Operating-system probe; each platform takes one branch.")]
    public string Platform => OperatingSystem.IsWindows() ? "windows" : OperatingSystem.IsLinux() ? "linux" : "other";

    public int OsBuild => Environment.OSVersion.Version.Build;

    public long UptimeSeconds => Environment.TickCount64 / 1000;

    [ExcludeFromCodeCoverage(Justification = "Operating-system probe; each platform takes one branch.")]
    public string VBoxManagePath => OperatingSystem.IsWindows()
        ? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), "Oracle", "VirtualBox", "VBoxManage.exe")
        : "VBoxManage";

    public IReadOnlyList<Volume> FixedVolumes() =>
        DriveInfo.GetDrives()
            .Where(drive => drive.DriveType == DriveType.Fixed && drive.IsReady)
            .Select(drive => new Volume(drive.Name, drive.AvailableFreeSpace, drive.TotalSize))
            .ToList();
}
