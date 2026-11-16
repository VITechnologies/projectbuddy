using System.Security.Cryptography;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using ProjectBuddy.PeerOllama;

namespace ProjectBuddy.PeerDiagnose;

// A question the loop may be asked, with the closed list of classes its answer may take.
public sealed record Question(string Id, string Text, IReadOnlyList<string> Classes);

// Everything that shapes a diagnosis is pinned here and hashed into the report: the model by digest, the context and
// output budgets, the loop's bounds, the prompts, and the closed list of questions.
public sealed partial record Policy(
    string Sha256,
    ModelPin Model,
    int NumCtx,
    int NumPredict,
    int Seed,
    int MaxRounds,
    int MaxCallsPerRound,
    int MaxCalls,
    int MaxEvidenceBytes,
    int MaxFindingChars,
    int MaxCitations,
    int MaxValueChars,
    int MaxAnswerAttempts,
    string System,
    string AnswerPrompt,
    IReadOnlyList<Question> Questions)
{
    public const string Schema = "projectbuddy/diagnose-policy@1";

    public static Policy Parse(byte[] bytes)
    {
        var root = Json.Object(Json.Parse(bytes, "policy", ExitCodes.Refused), "policy");
        Json.Keys(root, "policy", "schema", "model", "numCtx", "numPredict", "seed", "maxRounds", "maxCallsPerRound", "maxCalls",
            "maxEvidenceBytes", "maxFindingChars", "maxCitations", "maxValueChars", "maxAnswerAttempts", "system", "answerPrompt", "questions");
        if (Json.String(root["schema"], "policy schema") != Schema)
        {
            throw new PeerException(ExitCodes.Refused, $"policy schema must be {Schema}");
        }

        var model = Json.Object(root["model"], "model");
        Json.Keys(model, "model", "name", "digest");
        var numCtx = (int)Json.Integer(root["numCtx"], "numCtx", 4096, 131072);
        var questions = new List<Question>();
        foreach (var item in Json.Array(root["questions"], "questions"))
        {
            questions.Add(ParseQuestion(Json.Object(item, "question"), questions));
        }

        if (questions.Count == 0)
        {
            throw new PeerException(ExitCodes.Refused, "the policy needs at least one question");
        }

        return new Policy(
            Convert.ToHexStringLower(SHA256.HashData(bytes)),
            ModelPin.Create(Json.String(model["name"], "model name"), Json.String(model["digest"], "model digest")),
            numCtx,
            (int)Json.Integer(root["numPredict"], "numPredict", 64, numCtx / 8),
            (int)Json.Integer(root["seed"], "seed", 0, int.MaxValue),
            (int)Json.Integer(root["maxRounds"], "maxRounds", 1, 8),
            (int)Json.Integer(root["maxCallsPerRound"], "maxCallsPerRound", 1, 6),
            (int)Json.Integer(root["maxCalls"], "maxCalls", 1, 24),
            // Only caps how much tool output is sent; the truncation guard, not this bound, proves each prompt fit.
            (int)Json.Integer(root["maxEvidenceBytes"], "maxEvidenceBytes", 1024, (long)numCtx * 4),
            (int)Json.Integer(root["maxFindingChars"], "maxFindingChars", 40, 2000),
            (int)Json.Integer(root["maxCitations"], "maxCitations", 1, 10),
            (int)Json.Integer(root["maxValueChars"], "maxValueChars", 16, 1024),
            (int)Json.Integer(root["maxAnswerAttempts"], "maxAnswerAttempts", 1, 3),
            Text(root["system"], "system"),
            Text(root["answerPrompt"], "answerPrompt"),
            questions);
    }

    public Question Find(string id) =>
        Questions.FirstOrDefault(question => question.Id == id)
            ?? throw new PeerException(ExitCodes.Refused, $"unknown question '{id}'; the policy has {string.Join(", ", Questions.Select(question => question.Id))}");

    private static Question ParseQuestion(JsonObject node, List<Question> earlier)
    {
        Json.Keys(node, "question", "id", "text", "classes");
        var id = Json.String(node["id"], "question id");
        if (!Name().IsMatch(id) || earlier.Any(question => question.Id == id))
        {
            throw new PeerException(ExitCodes.Refused, $"question {id} needs a unique [a-z0-9-]+ id");
        }

        var classes = Json.Strings(node["classes"], $"{id} classes");
        if (classes.Count == 0 || classes.Distinct(StringComparer.Ordinal).Count() != classes.Count || !classes.All(Name().IsMatch))
        {
            throw new PeerException(ExitCodes.Refused, $"question {id} needs a non-empty list of unique [a-z0-9-]+ classes");
        }

        return new Question(id, Text(node["text"], $"{id} text"), classes);
    }

    private static string Text(JsonNode? node, string what)
    {
        var text = Json.String(node, what);
        return text.Trim().Length > 0 ? text : throw new PeerException(ExitCodes.Refused, $"{what} must not be blank");
    }

    [GeneratedRegex("^[a-z0-9-]+$")]
    private static partial Regex Name();
}
