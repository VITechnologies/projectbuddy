using System.Text;
using System.Text.RegularExpressions;

namespace ProjectBuddy.PeerOllama;

public static class ExitCodes
{
    public const int Usage = 2;
    public const int Refused = 3;
    public const int GuardBroken = 4;
    public const int Unreachable = 5;
}

public sealed class PeerException(int exitCode, string message) : Exception(message)
{
    public int ExitCode { get; } = exitCode;
}

// Ollama keeps only the last num_ctx/2 + 2 tokens of an oversized prompt and drops the system prompt with
// them, without reporting it. A reply is trusted only when its prompt provably fit.
public static class TruncationGuard
{
    // Below half the context, where truncated prompts land, and with room for the reply.
    public static bool Fits(int numCtx, ChatResult result) =>
        result.PromptTokens > 0 && result.PromptTokens < numCtx / 2 && result.PromptTokens + result.OutputTokens <= numCtx;

    public static string ProbeText(int numCtx)
    {
        var text = new StringBuilder();
        for (var index = 0; index < numCtx * 2; index++)
        {
            text.Append('w').Append(index).Append(' ');
        }

        return text.ToString();
    }

    // Prove the server still truncates the way Fits assumes before trusting any reply from this model.
    public static async Task<int> ProbeAsync(OllamaClient ollama, string model, int numCtx, int seed)
    {
        var probe = await ollama.ChatAsync(OllamaClient.ChatRequest(model, "", ProbeText(numCtx), numCtx, 1, seed));
        if (probe.PromptTokens < numCtx / 2)
        {
            throw new PeerException(ExitCodes.GuardBroken, $"{model}: truncation probe reported {probe.PromptTokens} prompt tokens; the guard cannot be trusted");
        }

        return probe.PromptTokens;
    }
}

public sealed partial record ModelPin(string Name, string Digest)
{
    public static ModelPin Create(string name, string digest) =>
        NamePattern().IsMatch(name) && DigestPattern().IsMatch(digest)
            ? new ModelPin(name, digest)
            : throw new PeerException(ExitCodes.Refused, $"model {name} needs a name:tag and a 64-hex digest");

    // Refuses unless every model is installed with exactly its pinned digest.
    public static async Task RequireInstalledAsync(OllamaClient ollama, IEnumerable<ModelPin> pins)
    {
        var installed = await ollama.DigestsAsync();
        foreach (var pin in pins)
        {
            if (!installed.TryGetValue(pin.Name, out var digest) || digest != pin.Digest)
            {
                throw new PeerException(ExitCodes.Refused, $"{pin.Name} is not installed with the pinned digest");
            }
        }
    }

    [GeneratedRegex("^[a-z0-9][a-z0-9._-]*:[a-z0-9][a-z0-9._-]*$")]
    private static partial Regex NamePattern();

    [GeneratedRegex("^[0-9a-f]{64}$")]
    private static partial Regex DigestPattern();
}
