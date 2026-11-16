using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;
using ProjectBuddy.PeerOllama;
using Xunit;

namespace ProjectBuddy.PeerDiagnose.Tests;

public sealed class PolicyTests
{
    [Fact]
    public void ParsesTheShippedPolicy()
    {
        var bytes = File.ReadAllBytes("policy.json");
        var policy = Policy.Parse(bytes);
        Assert.Equal(Convert.ToHexStringLower(SHA256.HashData(bytes)), policy.Sha256);
        Assert.Equal(new ModelPin("qwen2.5:7b", FakeOllama.Digest), policy.Model);
        Assert.Equal(["github-unreachable", "runner-offline", "ollama-empty", "vm-states", "disk-full", "general-health"], policy.Questions.Select(question => question.Id));
        Assert.All(policy.Questions, question => Assert.Contains("inconclusive", question.Classes));
        Assert.All(policy.Questions, question => Assert.All(question.Classes, name => Assert.Contains(name, question.Text)));
        Assert.Equal((16384, 384, 42, 4, 2, 6, 12000, 400, 5, 200, 2), (policy.NumCtx, policy.NumPredict, policy.Seed, policy.MaxRounds, policy.MaxCallsPerRound,
            policy.MaxCalls, policy.MaxEvidenceBytes, policy.MaxFindingChars, policy.MaxCitations, policy.MaxValueChars, policy.MaxAnswerAttempts));
    }

    public static TheoryData<string, string> Invalid() => new()
    {
        { "schema", "policy schema must be" },
        { "extra", "policy keys must be exactly" },
        { "numCtx", "numCtx must be an integer" },
        { "numPredict", "numPredict must be an integer from 64 to 2048" },
        { "maxAnswerAttempts", "maxAnswerAttempts must be an integer from 1 to 3" },
        { "digest", "needs a name:tag and a 64-hex digest" },
        { "blank", "system must not be blank" },
        { "noQuestions", "at least one question" },
        { "questionKeys", "question keys must be exactly" },
        { "badId", "needs a unique [a-z0-9-]+ id" },
        { "duplicateId", "needs a unique [a-z0-9-]+ id" },
        { "noClasses", "non-empty list of unique" },
        { "duplicateClass", "non-empty list of unique" },
        { "badClass", "non-empty list of unique" },
    };

    [Theory]
    [MemberData(nameof(Invalid))]
    public void RefusesInvalidPolicies(string change, string message)
    {
        JsonObject Question(string id) => new() { ["id"] = id, ["text"] = "t", ["classes"] = new JsonArray("a") };
        var refused = Assert.Throws<PeerException>(() => Fixtures.Parse(policy =>
        {
            switch (change)
            {
                case "schema": policy["schema"] = "projectbuddy/diagnose-policy@2"; break;
                case "extra": policy["extra"] = 1; break;
                case "numCtx": policy["numCtx"] = 1024; break;
                case "numPredict": policy["numPredict"] = 4096; break;
                case "maxAnswerAttempts": policy["maxAnswerAttempts"] = 4; break;
                case "digest": policy["model"]!["digest"] = "abc"; break;
                case "blank": policy["system"] = "   "; break;
                case "noQuestions": policy["questions"] = new JsonArray(); break;
                case "questionKeys": policy["questions"]![0]!["extra"] = 1; break;
                case "badId": policy["questions"]![0]!["id"] = "Bad_Id"; break;
                case "duplicateId": policy["questions"]!.AsArray().Add((JsonNode)Question("github-unreachable")); break;
                case "noClasses": policy["questions"]![0]!["classes"] = new JsonArray(); break;
                case "duplicateClass": policy["questions"]![0]!["classes"] = new JsonArray("a", "a"); break;
                default: policy["questions"]![0]!["classes"] = new JsonArray("Not OK"); break;
            }
        }));
        Assert.Equal(ExitCodes.Refused, refused.ExitCode);
        Assert.Contains(message, refused.Message);
    }

    [Fact]
    public void FindsQuestionsById()
    {
        var policy = Fixtures.Parse();
        Assert.Equal("Is the link the cause?", policy.Find("github-unreachable").Text);
        var refused = Assert.Throws<PeerException>(() => policy.Find("shell"));
        Assert.Equal(ExitCodes.Refused, refused.ExitCode);
        Assert.Equal("unknown question 'shell'; the policy has github-unreachable", refused.Message);
    }
}

public sealed class GroundTests
{
    private static readonly Policy Policy = Fixtures.Parse();

    private static readonly Question Question = Policy.Questions[0];

    private const string Disk = """{"schema":"projectbuddy/peer-tool-result@1","tool":"disk","truncated":false,"redaction":{},"data":{"drives":[{"name":"C:\\","freePercent":12,"ok":true,"none":null,"a/b":1,"m~n":2,"list":[1]}]}}""";

    private static readonly IReadOnlyList<Evidence> Evidence =
    [
        new("disk", [], Disk),
        new("ollama", [], """{"schema":"projectbuddy/peer-tool-result@1","tool":"ollama","truncated":true,"error":"the probe failed: IOException"}"""),
    ];

    private static string Answer(JsonNode? citations, string answerClass = "link-up", string finding = "A finding.") =>
        new JsonObject { ["class"] = answerClass, ["finding"] = finding, ["citations"] = citations }.ToJsonString();

    private static JsonArray Cite(string tool, string pointer, JsonNode? value) => [new JsonObject { ["tool"] = tool, ["pointer"] = pointer, ["value"] = value }];

    public static TheoryData<string, string> Rejected() => new()
    {
        { "not json", "not exactly {class, finding, citations}" },
        { """{"class":"link-up","finding":"f"}""", "not exactly" },
        { """{"class":1,"finding":"f","citations":[]}""", "not exactly" },
        { """{"class":"link-up","finding":1,"citations":[]}""", "not exactly" },
        { """{"class":"link-up","finding":"f","citations":{}}""", "not exactly" },
        { Answer(Cite("disk", "/data/drives/0/freePercent", 12), "shell"), "class \"shell\" is not one of github-unreachable's classes" },
        { Answer(Cite("disk", "/data/drives/0/freePercent", 12), finding: "   "), "the finding must be 1 to 60 characters" },
        { Answer(Cite("disk", "/data/drives/0/freePercent", 12), finding: new string('x', 61)), "the finding must be 1 to 60 characters" },
        { Answer(new JsonArray()), "the answer needs 1 to 2 citations" },
        { Answer(new JsonArray(Cite("disk", "/x", 1)[0]!.DeepClone(), Cite("disk", "/x", 1)[0]!.DeepClone(), Cite("disk", "/x", 1)[0]!.DeepClone())), "needs 1 to 2" },
        { Answer(new JsonArray("x")), "each citation must be exactly {tool, pointer, value}" },
        { Answer(new JsonArray(new JsonObject { ["tool"] = "disk", ["pointer"] = "/x" })), "each citation" },
        { Answer(new JsonArray(new JsonObject { ["tool"] = 1, ["pointer"] = "/x", ["value"] = 1 })), "each citation" },
        { Answer(new JsonArray(new JsonObject { ["tool"] = "disk", ["pointer"] = 1, ["value"] = 1 })), "each citation" },
        { Answer(new JsonArray(new JsonObject { ["tool"] = "disk", ["pointer"] = "/x", ["other"] = 1 })), "each citation" },
        { Answer(Cite("disk", "/data/drives/0", new JsonObject())), "each citation" },
        { Answer(Cite("disk", "/data/drives/0/list", new JsonArray(1))), "each citation" },
        { Answer(Cite("disk", "/data/drives/0/name", new string('x', 17))), "with a scalar value of at most 16 characters" },
        { Answer(Cite("disk", "", 12)), "the citation \"disk\" \"\" = 12 matches no result of that tool the model was given" },
        { Answer(Cite("disk", "data/drives/0/freePercent", 12)), "matches no result of that tool" },
        { Answer(Cite("network-link", "/data/drives/0/freePercent", 12)), "matches no result of that tool" },
        { Answer(Cite("disk", "/data/drives/0/freePercent", 13)), "matches no result of that tool" },
        { Answer(Cite("disk", "/data/drives/0/freePercent", null)), "the citation \"disk\" \"/data/drives/0/freePercent\" = null matches no result" },
        { Answer(Cite("disk", "/data/drives/0/freePercent", "13")), "matches no result of that tool" },
        { Answer(Cite("disk", "/data/drives/0/name", "c:\\")), "matches no result of that tool" },
        { Answer(Cite("disk", "/data/drives/0", "x")), "matches no result of that tool" },
        { Answer(Cite("disk", "/data/drives/00/freePercent", 12)), "matches no result of that tool" },
        { Answer(Cite("disk", "/data/drives/1/freePercent", 12)), "matches no result of that tool" },
        { Answer(Cite("disk", "/data/drives/-/freePercent", 12)), "matches no result of that tool" },
        { Answer(Cite("disk", "/data/drives/1234567890/freePercent", 12)), "matches no result of that tool" },
        { Answer(Cite("disk", "/data/drives/0/freePercent/x", 12)), "matches no result of that tool" },
        { Answer(Cite("ollama", "/data/reachable", true)), "matches no result of that tool" },
        { Answer(Cite("disk", "/data/drives/0/freePercent", JsonNode.Parse("1e99999999999"))), "= 1e99999999999 matches no result" },
        { Answer(Cite("disk", "/data/drives/0/freePercent", JsonNode.Parse("12.0"))), "= 12.0 matches no result" },
        { Answer(Cite("disk", "/data/drives/0/ok", JsonNode.Parse("1"))), "matches no result" },
        { Answer(Cite("disk", "/data/drives/0/none", 0)), "matches no result" },
        { Answer(Cite("disk", "/data/drives/0/freePercent", JsonNode.Parse("1" + new string('0', 200)))), "= 1" + new string('0', 119) + "… matches no result" },
        { Answer(Cite("disk", "/data/drives/0/freePercent", 12), "x\u001b[2J\n::error::injected"), "class \"x\\u001B[2J\\n::error::injected\" is not one of" },
    };

    [Theory]
    [MemberData(nameof(Rejected))]
    public void GivesNoAdviceForUngroundedAnswers(string content, string message)
    {
        var rejected = Assert.Throws<PeerException>(() => Diagnosis.Ground(Policy, Question, Evidence, content));
        Assert.Equal(DiagnoseExitCodes.NoAdvice, rejected.ExitCode);
        Assert.Contains(message, rejected.Message);
    }

    public static TheoryData<string, string> Grounded() => new()
    {
        { "/data/drives/0/freePercent", "12" },
        { "/drives/0/freePercent", "12" },
        { "/drives/0/freePercent", "\"12\"" },
        { "/drives/0/ok", "true" },
        { "/drives/0/ok", "\"true\"" },
        { "/drives/0/none", "null" },
        { "/drives/0/a~1b", "1" },
        { "/drives/0/m~0n", "2" },
        { "/drives/0/name", "\"C:\\\\\"" },
        { "/truncated", "false" },
        { "/tool", "\"disk\"" },
        { "/data/drives/0/list/0", "1" },
    };

    [Theory]
    [MemberData(nameof(Grounded))]
    public void AcceptsCitationsThatMatchExactly(string pointer, string value)
    {
        var cited = JsonNode.Parse(value);
        var answer = Diagnosis.Ground(Policy, Question, Evidence, Answer(Cite("disk", pointer, cited), finding: "  Disk is fine.  "));
        Assert.Equal("Disk is fine.", (string?)answer["finding"]);
        Assert.Equal("link-up", (string?)answer["class"]);
        Assert.True(JsonNode.DeepEquals(Cite("disk", pointer, cited?.DeepClone()), answer["citations"]));
    }

    [Fact]
    public void ResolvesOnlyWellFormedPointers()
    {
        var root = JsonNode.Parse("""{"a":[{"b":null}]}""");
        Assert.True(Diagnosis.Resolve(root, "/a/0/b", out var found));
        Assert.Null(found);
        Assert.False(Diagnosis.Resolve(root, "/a/0/b/c", out _));
        Assert.False(Diagnosis.Resolve(null, "/a", out _));
        Assert.False(Diagnosis.Resolve(root, "a", out _));
    }
}
