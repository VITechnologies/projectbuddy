using ProjectBuddy.PeerOllama;

namespace ProjectBuddy.PeerProbes.Tests;

// A scriptable ISources: commands are answered by "program arg arg..." keys, and every call is recorded.
internal sealed class FakeSources : ISources
{
    public bool IsWindows { get; set; } = true;

    public List<Adapter> AdapterList { get; } = [];

    public List<Drive> DriveList { get; } = [];

    public OllamaState? Ollama { get; set; }

    public Dictionary<string, CommandResult?> Commands { get; } = new(StringComparer.Ordinal);

    public List<string> Calls { get; } = [];

    public Exception? Fault { get; set; }

    public IReadOnlyList<Adapter> Adapters() => Fault is null ? AdapterList : throw Fault;

    public IReadOnlyList<Drive> Drives() => DriveList;

    public Task<OllamaState?> OllamaAsync() => Task.FromResult(Ollama);

    public CommandResult? Run(string program, IReadOnlyList<string> arguments)
    {
        var key = string.Join(' ', [program, .. arguments]);
        Calls.Add(key);
        return Commands.TryGetValue(key, out var result) ? result : null;
    }

    public FakeSources Answer(string key, string output, int exitCode = 0, bool truncated = false)
    {
        Commands[key] = new CommandResult(exitCode, output, truncated);
        return this;
    }

    public static OllamaState State(int models = 1, int loaded = 1) => new(
        "0.40.16",
        [.. Enumerable.Range(0, models).Select(index => new OllamaModel(index == 0 ? "projectbuddy:0.1-qwen2.5-7b" : $"model-{index:D3}:latest", $"sha256:{index:D64}"))],
        [.. Enumerable.Range(0, loaded).Select(index => new LoadedModel(index == 0 ? "projectbuddy:0.1-qwen2.5-7b" : $"model-{index:D3}:latest", $"sha256:{index:D64}", 4_683_087_332, 4_683_087_332, 8192))]);
}
