using ProjectBuddy.PeerOllama;

namespace ProjectBuddy.PeerProbes;

public sealed record Adapter(string Name, string Kind, string Status, long SpeedMbps, bool Ipv4, bool Ipv6, IReadOnlyList<string> Gateways);

public sealed record Drive(string Name, string Format, long TotalBytes, long FreeBytes);

public sealed record OllamaModel(string Name, string Digest);

public sealed record OllamaState(string Version, IReadOnlyList<OllamaModel> Models, IReadOnlyList<LoadedModel> Loaded);

// A command's exit code and standard output, cut at the runner's byte cap (Truncated).
public sealed record CommandResult(int ExitCode, string Output, bool Truncated);

// Every read the probes make, behind one seam: the registry runs on Linux CI against fakes, and the operating-system
// adapters (Probes.cs) stay thin.
public interface ISources
{
    bool IsWindows { get; }

    IReadOnlyList<Adapter> Adapters();

    IReadOnlyList<Drive> Drives();

    // Null when Ollama does not answer on loopback.
    Task<OllamaState?> OllamaAsync();

    // Runs one of the fixed commands (VBoxManage, sc.exe, schtasks.exe, wevtutil.exe) without a shell; null when the
    // program is not installed.
    CommandResult? Run(string program, IReadOnlyList<string> arguments);
}

// Where each fixed program may come from. Only fully qualified paths are ever returned: never PATH, never the working
// directory, and never a path that another drive or user could resolve differently.
public static class ProgramPaths
{
    public const string VBoxManage = "VBoxManage";

    public static IReadOnlyList<string> Candidates(string program, string? vboxManage, bool isWindows, string systemDirectory, string programFiles)
    {
        IEnumerable<string> candidates = program != VBoxManage ? isWindows ? [Path.Combine(systemDirectory, program)] : []
            : vboxManage is not null ? [vboxManage]
            : isWindows ? [Path.Combine(programFiles, "Oracle", "VirtualBox", "VBoxManage.exe")]
            : ["/usr/bin/VBoxManage"];
        return [.. candidates.Where(Path.IsPathFullyQualified)];
    }
}
