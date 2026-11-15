using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace ProjectBuddy.PeerBench;

public enum ItemKind
{
    Triage,
    Security,
    Tool,
}

// Expectation fields that do not apply to an item's kind are empty.
public sealed record CorpusItem(
    string Id,
    ItemKind Kind,
    string Input,
    string Step,
    string ErrorLine,
    string Class,
    IReadOnlyList<string> Classes,
    string Verb);

public sealed partial record Corpus(
    string Sha256,
    int NumCtx,
    int NumPredict,
    int Seed,
    IReadOnlyDictionary<ItemKind, string> Prompts,
    IReadOnlyList<string> TriageClasses,
    IReadOnlyList<string> SecurityClasses,
    IReadOnlyList<string> Verbs,
    IReadOnlyList<CorpusItem> Items)
{
    public const string Schema = "projectbuddy/peer-bench-corpus@1";
    public const int MaxInputBytes = 8 * 1024;

    public static Corpus Parse(byte[] bytes)
    {
        var root = Json.Object(Json.Parse(bytes, "corpus", ExitCodes.Refused), "corpus");
        Json.Keys(root, "corpus", "schema", "numCtx", "numPredict", "seed", "prompts", "triageClasses", "securityClasses", "verbs", "items");
        if (Json.String(root["schema"], "corpus schema") != Schema)
        {
            throw new BenchException(ExitCodes.Refused, $"corpus schema must be {Schema}");
        }

        var numCtx = (int)Json.Integer(root["numCtx"], "numCtx", 2048, 131072);
        var numPredict = (int)Json.Integer(root["numPredict"], "numPredict", 16, numCtx / 4);
        var seed = (int)Json.Integer(root["seed"], "seed", 0, int.MaxValue);
        var prompts = Json.Object(root["prompts"], "prompts");
        Json.Keys(prompts, "prompts", "triage", "security", "tool");
        var promptMap = new Dictionary<ItemKind, string>
        {
            [ItemKind.Triage] = Json.String(prompts["triage"], "prompts.triage"),
            [ItemKind.Security] = Json.String(prompts["security"], "prompts.security"),
            [ItemKind.Tool] = Json.String(prompts["tool"], "prompts.tool"),
        };
        var triageClasses = Distinct(Json.Strings(root["triageClasses"], "triageClasses"), "triageClasses");
        var securityClasses = Distinct(Json.Strings(root["securityClasses"], "securityClasses"), "securityClasses");
        var verbs = Distinct(Json.Strings(root["verbs"], "verbs"), "verbs");

        var ids = new HashSet<string>(StringComparer.Ordinal);
        var items = new List<CorpusItem>();
        foreach (var node in Json.Array(root["items"], "items"))
        {
            var item = ParseItem(Json.Object(node, "item"), triageClasses, securityClasses, verbs);
            if (!ids.Add(item.Id))
            {
                throw new BenchException(ExitCodes.Refused, $"duplicate item id {item.Id}");
            }

            items.Add(item);
        }

        if (items.Count == 0)
        {
            throw new BenchException(ExitCodes.Refused, "corpus has no items");
        }

        return new Corpus(Convert.ToHexStringLower(SHA256.HashData(bytes)), numCtx, numPredict, seed, promptMap, triageClasses, securityClasses, verbs, items);
    }

    private static CorpusItem ParseItem(JsonObject node, List<string> triageClasses, List<string> securityClasses, List<string> verbs)
    {
        Json.Keys(node, "item", "id", "kind", "source", "input", "expect");
        var id = Json.String(node["id"], "item id");
        if (!IdPattern().IsMatch(id))
        {
            throw new BenchException(ExitCodes.Refused, $"item id {id} must match [a-z0-9-]+");
        }

        Json.String(node["source"], $"{id} source");
        var input = Json.String(node["input"], $"{id} input");
        if (Encoding.UTF8.GetByteCount(input) > MaxInputBytes)
        {
            throw new BenchException(ExitCodes.Refused, $"{id} input exceeds {MaxInputBytes} bytes");
        }

        var expect = Json.Object(node["expect"], $"{id} expect");
        switch (Json.String(node["kind"], $"{id} kind"))
        {
            case "triage":
                Json.Keys(expect, $"{id} expect", "step", "errorLine", "class");
                var errorLine = Json.String(expect["errorLine"], $"{id} errorLine");
                Require(input.Contains(errorLine, StringComparison.Ordinal), $"{id} errorLine must appear verbatim in the input");
                var triageClass = Json.String(expect["class"], $"{id} class");
                Require(triageClasses.Contains(triageClass), $"{id} class {triageClass} is not a triage class");
                return new CorpusItem(id, ItemKind.Triage, input, Json.String(expect["step"], $"{id} step"), errorLine, triageClass, [], "");
            case "security":
                Json.Keys(expect, $"{id} expect", "classes");
                var classes = Distinct(Json.Strings(expect["classes"], $"{id} classes"), $"{id} classes");
                Require(classes.All(securityClasses.Contains), $"{id} classes must be security classes");
                return new CorpusItem(id, ItemKind.Security, input, "", "", "", classes, "");
            case "tool":
                Json.Keys(expect, $"{id} expect", "verb");
                var verb = Json.String(expect["verb"], $"{id} verb");
                Require(verbs.Contains(verb), $"{id} verb {verb} is not an allowed verb");
                return new CorpusItem(id, ItemKind.Tool, input, "", "", "", [], verb);
            default:
                throw new BenchException(ExitCodes.Refused, $"{id} kind must be triage, security or tool");
        }
    }

    private static List<string> Distinct(List<string> values, string what)
    {
        Require(values.Distinct(StringComparer.Ordinal).Count() == values.Count, $"{what} must not repeat values");
        return values;
    }

    private static void Require(bool condition, string message)
    {
        if (!condition)
        {
            throw new BenchException(ExitCodes.Refused, message);
        }
    }

    [GeneratedRegex("^[a-z0-9-]+$")]
    private static partial Regex IdPattern();
}

public sealed partial record Candidate(string Name, string Digest)
{
    public const string Schema = "projectbuddy/peer-bench-candidates@1";

    public static IReadOnlyList<Candidate> ParseAll(byte[] bytes)
    {
        var root = Json.Object(Json.Parse(bytes, "candidates", ExitCodes.Refused), "candidates");
        Json.Keys(root, "candidates", "schema", "models");
        if (Json.String(root["schema"], "candidates schema") != Schema)
        {
            throw new BenchException(ExitCodes.Refused, $"candidates schema must be {Schema}");
        }

        var models = new List<Candidate>();
        foreach (var node in Json.Array(root["models"], "models"))
        {
            var model = Json.Object(node, "model");
            Json.Keys(model, "model", "name", "digest");
            var name = Json.String(model["name"], "model name");
            var digest = Json.String(model["digest"], $"{name} digest");
            if (!NamePattern().IsMatch(name) || !DigestPattern().IsMatch(digest) || models.Any(other => other.Name == name))
            {
                throw new BenchException(ExitCodes.Refused, $"model {name} needs a unique name:tag and a 64-hex digest");
            }

            models.Add(new Candidate(name, digest));
        }

        if (models.Count == 0)
        {
            throw new BenchException(ExitCodes.Refused, "candidates has no models");
        }

        return models;
    }

    [GeneratedRegex("^[a-z0-9][a-z0-9._-]*:[a-z0-9][a-z0-9._-]*$")]
    private static partial Regex NamePattern();

    [GeneratedRegex("^[0-9a-f]{64}$")]
    private static partial Regex DigestPattern();
}
