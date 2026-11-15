using System.Security.Cryptography;
using System.Text.RegularExpressions;
using ProjectBuddy.PeerOllama;

namespace ProjectBuddy.PeerAdvise;

// Severity is fixed by the rule, never chosen by the model.
public sealed record Rule(string Id, string Severity, string Guidance);

public sealed record PreReviewPolicy(string Prompt, int NumPredict, int MaxFindings, int MaxBatches, IReadOnlyList<Rule> Rules);

public sealed partial record Policy(
    string Sha256,
    ModelPin Model,
    int NumCtx,
    int NumPredict,
    int Seed,
    int MaxInputBytes,
    string TriagePrompt,
    IReadOnlyList<string> TriageClasses,
    PreReviewPolicy PreReview)
{
    public const string Schema = "projectbuddy/advise-policy@1";
    public static readonly string[] Severities = ["blocker", "warning", "nit"];

    public static Policy Parse(byte[] bytes)
    {
        var root = Json.Object(Json.Parse(bytes, "policy", ExitCodes.Refused), "policy");
        Json.Keys(root, "policy", "schema", "model", "numCtx", "numPredict", "seed", "maxInputBytes", "triage", "preReview");
        if (Json.String(root["schema"], "policy schema") != Schema)
        {
            throw new PeerException(ExitCodes.Refused, $"policy schema must be {Schema}");
        }

        var model = Json.Object(root["model"], "model");
        Json.Keys(model, "model", "name", "digest");
        var numCtx = (int)Json.Integer(root["numCtx"], "numCtx", 4096, 131072);
        var triage = Json.Object(root["triage"], "triage");
        Json.Keys(triage, "triage", "prompt", "classes");
        var classes = Json.Strings(triage["classes"], "triage classes");
        if (classes.Count == 0 || classes.Distinct(StringComparer.Ordinal).Count() != classes.Count)
        {
            throw new PeerException(ExitCodes.Refused, "triage classes must be a non-empty list without repeats");
        }

        return new Policy(
            Convert.ToHexStringLower(SHA256.HashData(bytes)),
            ModelPin.Create(Json.String(model["name"], "model name"), Json.String(model["digest"], "model digest")),
            numCtx,
            (int)Json.Integer(root["numPredict"], "numPredict", 16, numCtx / 8),
            (int)Json.Integer(root["seed"], "seed", 0, int.MaxValue),
            // Only caps how much evidence is sent; the truncation guard, not this bound, proves the prompt fit.
            (int)Json.Integer(root["maxInputBytes"], "maxInputBytes", 1024, (long)numCtx * 4),
            Json.String(triage["prompt"], "triage prompt"),
            classes,
            ParsePreReview(Json.Object(root["preReview"], "preReview"), numCtx));
    }

    private static PreReviewPolicy ParsePreReview(System.Text.Json.Nodes.JsonObject node, int numCtx)
    {
        Json.Keys(node, "preReview", "prompt", "numPredict", "maxFindings", "maxBatches", "rules");
        var rules = new List<Rule>();
        foreach (var item in Json.Array(node["rules"], "preReview rules"))
        {
            var rule = Json.Object(item, "rule");
            Json.Keys(rule, "rule", "id", "severity", "guidance");
            var id = Json.String(rule["id"], "rule id");
            var severity = Json.String(rule["severity"], $"{id} severity");
            if (!RuleId().IsMatch(id) || !Severities.Contains(severity) || rules.Any(other => other.Id == id))
            {
                throw new PeerException(ExitCodes.Refused, $"rule {id} needs a unique [a-z0-9-]+ id and a severity of {string.Join("|", Severities)}");
            }

            rules.Add(new Rule(id, severity, Json.String(rule["guidance"], $"{id} guidance")));
        }

        if (rules.Count == 0)
        {
            throw new PeerException(ExitCodes.Refused, "preReview needs at least one rule");
        }

        return new PreReviewPolicy(
            Json.String(node["prompt"], "preReview prompt"),
            (int)Json.Integer(node["numPredict"], "preReview numPredict", 64, numCtx / 8),
            (int)Json.Integer(node["maxFindings"], "maxFindings", 1, 50),
            (int)Json.Integer(node["maxBatches"], "maxBatches", 1, 64),
            rules);
    }

    [GeneratedRegex("^[a-z0-9-]+$")]
    private static partial Regex RuleId();
}
