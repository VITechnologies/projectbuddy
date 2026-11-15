using System.Security.Cryptography;
using ProjectBuddy.PeerOllama;

namespace ProjectBuddy.PeerAdvise;

public sealed record Policy(
    string Sha256,
    ModelPin Model,
    int NumCtx,
    int NumPredict,
    int Seed,
    int MaxInputBytes,
    string TriagePrompt,
    IReadOnlyList<string> TriageClasses)
{
    public const string Schema = "projectbuddy/advise-policy@1";

    public static Policy Parse(byte[] bytes)
    {
        var root = Json.Object(Json.Parse(bytes, "policy", ExitCodes.Refused), "policy");
        Json.Keys(root, "policy", "schema", "model", "numCtx", "numPredict", "seed", "maxInputBytes", "triage");
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
            classes);
    }
}
