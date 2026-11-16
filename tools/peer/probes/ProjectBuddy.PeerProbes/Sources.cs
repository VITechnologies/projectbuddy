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
