using System.ComponentModel;
using System.Diagnostics;
using System.Diagnostics.CodeAnalysis;
using System.Text;

namespace ProjectBuddy.PeerAsk;

public sealed record ProcessResult(bool Started, bool TimedOut, int ExitCode, string Stdout, string Stderr);

public interface IProcessRunner
{
    // stdin, when given, is written to the child and then closed; otherwise the child's input is closed at once.
    ProcessResult Run(string fileName, IReadOnlyList<string> arguments, TimeSpan timeout, byte[]? stdin = null);
}

public sealed record Volume(string Name, long FreeBytes, long TotalBytes);

public interface IHostFacts
{
    string Platform { get; }
    int OsBuild { get; }
    long UptimeSeconds { get; }
    string VBoxManagePath { get; }
    string OllamaPath { get; }
    string LbabusPath { get; }
    string SshPath { get; }

    // projectbuddy-diagnose, its pinned policy, and the operator's deny list: fixed paths, deployed by the operator.
    string DiagnosePath { get; }
    string DiagnosePolicyPath { get; }
    string DenyListPath { get; }

    // infer (#358): the deployment-written model allowlist, the loopback Ollama, and how many prompts run at once.
    string InferAllowlistPath { get; }
    string OllamaUrl { get; }
    int InferConcurrency { get; }

    IReadOnlyList<Volume> FixedVolumes();
}

public static class BoundedFile
{
    // The file's bytes, or null when it holds more than maxBytes; never reads past maxBytes + 1.
    public static byte[]? Read(string path, int maxBytes)
    {
        using var file = File.OpenRead(path);
        var buffer = new byte[maxBytes + 1];
        var read = file.ReadAtLeast(buffer, buffer.Length, throwOnEndOfStream: false);
        return read > maxBytes ? null : buffer[..read];
    }

    // Reads at most maxBytes + 1 bytes of a stream, so an oversized input is detected without reading all of it.
    public static async Task<byte[]?> ReadAsync(Stream stream, int maxBytes, CancellationToken cancel)
    {
        var buffer = new byte[maxBytes + 1];
        var read = await stream.ReadAtLeastAsync(buffer, buffer.Length, throwOnEndOfStream: false, cancel);
        return read > maxBytes ? null : buffer[..read];
    }
}

public sealed class SystemProcessRunner : IProcessRunner
{
    // Above the infer answer limit (1 MiB plus a line end), so a full answer is never cut short.
    public const int MaxCapturedChars = 2 * 1024 * 1024;
    public static readonly TimeSpan KillDrain = TimeSpan.FromSeconds(2);

    public ProcessResult Run(string fileName, IReadOnlyList<string> arguments, TimeSpan timeout, byte[]? stdin = null)
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
        catch (Exception error) when (error is Win32Exception or InvalidOperationException)
        {
            return new ProcessResult(false, false, -1, "", "");
        }

        var deadline = Stopwatch.StartNew();
        var stdout = ReadCapped(process.StandardOutput);
        var stderr = ReadCapped(process.StandardError);

        // Written while the output is drained, so a child that answers before reading all its input cannot deadlock.
        _ = Feed(process.StandardInput.BaseStream, stdin);

        // A grandchild can hold the pipes open after the child exits, so the reads share the deadline.
        var exited = process.WaitForExit(timeout);
        var drained = exited && Task.WhenAll(stdout, stderr).Wait(Remaining(timeout, deadline));
        if (!drained)
        {
            Kill(process);
            Task.WhenAll(stdout, stderr).Wait(KillDrain);
            return new ProcessResult(true, true, -1, Completed(stdout), Completed(stderr));
        }

        return new ProcessResult(true, false, process.ExitCode, stdout.Result, stderr.Result);
    }

    internal static TimeSpan Remaining(TimeSpan timeout, Stopwatch elapsed)
    {
        var left = timeout - elapsed.Elapsed;
        return left > TimeSpan.Zero ? left : TimeSpan.Zero;
    }

    internal static async Task Feed(Stream input, byte[]? bytes)
    {
        try
        {
            await input.WriteAsync(bytes ?? []);
        }
        catch (IOException)
        {
            // The child exited, or was killed, without reading all of its input.
        }
        finally
        {
            Close(input);
        }
    }

    internal static async Task<string> ReadCapped(TextReader reader)
    {
        var text = new StringBuilder();
        var buffer = new char[8192];
        int read;
        while ((read = await reader.ReadAsync(buffer, 0, buffer.Length)) > 0)
        {
            text.Append(buffer, 0, Math.Min(read, MaxCapturedChars - text.Length));
        }

        return text.ToString();
    }

    [ExcludeFromCodeCoverage(Justification = "A pipe inherited by a process outside the killed tree can outlive the drain wait.")]
    private static string Completed(Task<string> read) => read.IsCompletedSuccessfully ? read.Result : "";

    [ExcludeFromCodeCoverage(Justification = "Closing a pipe whose reader is gone fails only on some platforms.")]
    private static void Close(Stream input)
    {
        try
        {
            input.Dispose();
        }
        catch (IOException)
        {
        }
    }

    [ExcludeFromCodeCoverage(Justification = "Races with process exit; both outcomes leave the process gone.")]
    private static void Kill(Process process)
    {
        try
        {
            process.Kill(entireProcessTree: true);
        }
        catch (Exception error) when (error is InvalidOperationException or Win32Exception)
        {
        }
    }
}

[ExcludeFromCodeCoverage(Justification = "Operating-system probes; each platform takes one branch.")]
public sealed class SystemFacts : IHostFacts
{
    public string Platform => OperatingSystem.IsWindows() ? "windows" : OperatingSystem.IsLinux() ? "linux" : "other";

    public int OsBuild => Environment.OSVersion.Version.Build;

    public long UptimeSeconds => Environment.TickCount64 / 1000;

    public string VBoxManagePath => OperatingSystem.IsWindows()
        ? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), "Oracle", "VirtualBox", "VBoxManage.exe")
        : "/usr/bin/VBoxManage";

    public string OllamaPath => OperatingSystem.IsWindows()
        ? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Programs", "Ollama", "ollama.exe")
        : "/usr/local/bin/ollama";

    public string LbabusPath => OperatingSystem.IsWindows()
        ? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".local", "bin", "lbabus.exe")
        : Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".local", "bin", "lbabus");

    public string SshPath => OperatingSystem.IsWindows()
        ? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), "OpenSSH", "ssh.exe")
        : "/usr/bin/ssh";

    public string DiagnosePath => Path.Combine(DiagnoseDirectory, OperatingSystem.IsWindows() ? "projectbuddy-diagnose.exe" : "projectbuddy-diagnose");

    public string DiagnosePolicyPath => Path.Combine(DiagnoseDirectory, "policy.json");

    public string DenyListPath => OperatingSystem.IsWindows()
        ? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData), "ProjectBuddy", "peer-link", "redaction-denylist.json")
        : Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".local", "share", "projectbuddy", "peer-link", "redaction-denylist.json");

    public string InferAllowlistPath => OperatingSystem.IsWindows()
        ? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData), "ProjectBuddy", "peer-ask", "infer", "allowlist.json")
        : Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".local", "share", "projectbuddy", "peer-ask", "infer", "allowlist.json");

    public string OllamaUrl => "http://127.0.0.1:11434/";

    public int InferConcurrency => 4;

    private static string DiagnoseDirectory => OperatingSystem.IsWindows()
        ? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData), "ProjectBuddy", "peer-ask", "diagnose")
        : Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".local", "share", "projectbuddy", "peer-ask", "diagnose");

    public IReadOnlyList<Volume> FixedVolumes() =>
        DriveInfo.GetDrives()
            .Where(drive => drive.DriveType == DriveType.Fixed && drive.IsReady)
            .Select(drive => new Volume(drive.Name, drive.AvailableFreeSpace, drive.TotalSize))
            .ToList();
}
