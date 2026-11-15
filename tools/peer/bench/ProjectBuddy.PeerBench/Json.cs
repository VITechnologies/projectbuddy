using System.Text.Json;
using System.Text.Json.Nodes;

namespace ProjectBuddy.PeerBench;

// Strict accessors: every input document is refused unless it has exactly the expected shape.
internal static class Json
{
    public static JsonNode Parse(byte[] bytes, string what, int exitCode)
    {
        try
        {
            return JsonNode.Parse(bytes) ?? throw new BenchException(exitCode, $"{what} is not a JSON object");
        }
        catch (JsonException)
        {
            throw new BenchException(exitCode, $"{what} is not valid JSON");
        }
    }

    public static JsonObject Object(JsonNode? node, string what, int exitCode = ExitCodes.Refused) =>
        node as JsonObject ?? throw new BenchException(exitCode, $"{what} must be a JSON object");

    public static JsonArray Array(JsonNode? node, string what, int exitCode = ExitCodes.Refused) =>
        node as JsonArray ?? throw new BenchException(exitCode, $"{what} must be a JSON array");

    public static void Keys(JsonObject value, string what, params string[] keys)
    {
        if (value.Count != keys.Length || !keys.All(value.ContainsKey))
        {
            throw new BenchException(ExitCodes.Refused, $"{what} keys must be exactly: {string.Join(", ", keys)}");
        }
    }

    public static string String(JsonNode? node, string what, int exitCode = ExitCodes.Refused) =>
        node is JsonValue value && value.TryGetValue<string>(out var text) && text.Length > 0
            ? text
            : throw new BenchException(exitCode, $"{what} must be a non-empty string");

    public static long Integer(JsonNode? node, string what, long min, long max, int exitCode = ExitCodes.Refused) =>
        node is JsonValue value && value.TryGetValue<long>(out var number) && number >= min && number <= max
            ? number
            : throw new BenchException(exitCode, $"{what} must be an integer from {min} to {max}");

    public static List<string> Strings(JsonNode? node, string what) =>
        Array(node, what).Select((item, index) => String(item, $"{what}[{index}]")).ToList();

    // Model output is scored, never trusted: shape problems return null instead of throwing.
    public static JsonObject? TryObject(string text)
    {
        try
        {
            return JsonNode.Parse(text) as JsonObject;
        }
        catch (JsonException)
        {
            return null;
        }
    }

    public static string? TryString(JsonNode? node) =>
        node is JsonValue value && value.TryGetValue<string>(out var text) ? text : null;
}
