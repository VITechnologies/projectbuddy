using System.Text.Json.Nodes;
using ProjectBuddy.PeerOllama;
using Xunit;

namespace ProjectBuddy.PeerDiagnose.Tests;

public sealed class RulesTests
{
    private static readonly string[] Classes = ["a", "b", "c"];

    private static IReadOnlyList<Rule> Parse(string rules) => Rules.Parse(JsonNode.Parse(rules), "q", Classes);

    private static string Condition(string quantifier, string tests, string tool = "disk", string pointer = "/data/drives/*") =>
        $$"""{"tool":"{{tool}}","pointer":"{{pointer}}","quantifier":"{{quantifier}}","test":{{tests}}}""";

    private static Evidence Result(string tool, string data, bool truncated = false) =>
        new(tool, [], $$"""{"schema":"projectbuddy/peer-tool-result@1","tool":"{{tool}}","truncated":{{(truncated ? "true" : "false")}},"redaction":{},"data":{{data}}}""");

    private static Evidence Failure(string tool) => new(tool, [], $$"""{"schema":"projectbuddy/peer-tool-result@1","tool":"{{tool}}","truncated":true,"error":"the probe failed: IOException"}""");

    private const string Drives = """{"drives":[{"name":"C:","freePercent":5,"format":"NTFS","label":null},{"name":"D:","freePercent":60,"format":"NTFS","label":null}]}""";

    private static string? Decide(string rules, params Evidence[] evidence) => Rules.Decide(Parse(rules), evidence).Class;

    private static string One(string cls, string condition, bool all = true) => $$"""[{"class":"{{cls}}","{{(all ? "all" : "any")}}":[{{condition}}]}]""";

    public static TheoryData<string, string> Invalid() => new()
    {
        { "{}", "q rules must be a JSON array" },
        { "[]", "q needs at least one rule" },
        { """[{"class":"a","all":[],"any":[]}]""", "q rule keys must be exactly: class, all" },
        { """[{"class":"a"}]""", "q rule keys must be exactly: class, any" },
        { """[{"class":"z","all":[]}]""", "q rule class z is not one of its classes" },
        { One("a", """{"tool":"disk","pointer":"/data","quantifier":"some","test":[],"x":1}"""), "q a condition keys must be exactly" },
        { One("a", Condition("some", """[{"pointer":"","op":"present"}]""", tool: "shell")), "needs a registry tool" },
        { One("a", Condition("some", """[{"pointer":"","op":"present"}]""", pointer: "data")), "a pointer starting with /" },
        { One("a", Condition("most", """[{"pointer":"","op":"present"}]""")), "a quantifier of some|every|none" },
        { One("a", Condition("some", "[]")), "q a needs at least one test" },
        { One("a", Condition("some", """[{"pointer":"","op":"eq"}]""")), "q a test keys must be exactly: pointer, op, value" },
        { One("a", Condition("some", """[{"pointer":"","op":"present","value":1}]""")), "q a test keys must be exactly: pointer, op" },
        { One("a", Condition("some", """[{"pointer":1,"op":"present"}]""")), "q a test pointer must be a string" },
        { One("a", Condition("some", """[{"pointer":"","op":"like","value":1}]""")), "needs an op of eq|ne|lt|le|gt|ge|prefix|present" },
        { One("a", Condition("some", """[{"pointer":"x","op":"present"}]""")), "a relative pointer without *" },
        { One("a", Condition("some", """[{"pointer":"/*","op":"present"}]""")), "a relative pointer without *" },
        { One("a", Condition("some", """[{"pointer":"","op":"lt","value":"1"}]""")), "a scalar value of the op's type" },
        { One("a", Condition("some", """[{"pointer":"","op":"prefix","value":1}]""")), "a scalar value of the op's type" },
        { One("a", Condition("some", """[{"pointer":"","op":"eq","value":{}}]""")), "a scalar value of the op's type" },
        { One("a", Condition("some", """[{"pointer":"","op":"ne","value":[1]}]""")), "a scalar value of the op's type" },
        { """[{"class":"a","any":[]}]""", "q rule a has an empty any, which never holds" },
        { """[{"class":"a","all":[]},{"class":"b","all":[]}]""", "q rule b follows an otherwise rule and can never be reached" },
    };

    [Theory]
    [MemberData(nameof(Invalid))]
    public void RefusesInvalidRules(string rules, string message)
    {
        var refused = Assert.Throws<PeerException>(() => Parse(rules));
        Assert.Equal(ExitCodes.Refused, refused.ExitCode);
        Assert.Contains(message, refused.Message);
    }

    [Fact]
    public void DescribesTheRulesForTheModel()
    {
        var rules = Parse("[" + string.Join(",",
            $$"""{"class":"a","any":[{{Condition("none", """[{"pointer":"","op":"present"}]""")}},{{Condition("every", """[{"pointer":"/freePercent","op":"ge","value":10},{"pointer":"/name","op":"prefix","value":"C"}]""")}}]}""",
            $$"""{"class":"b","all":[{{Condition("some", """[{"pointer":"","op":"eq","value":null},{"pointer":"/label","op":"present"},{"pointer":"/x","op":"ne","value":"y"},{"pointer":"/n","op":"lt","value":1},{"pointer":"/n","op":"le","value":1},{"pointer":"/n","op":"gt","value":1}]""")}}]}""",
            """{"class":"c","all":[]}""") + "]");
        Assert.Equal(
            "Classes, decided by the first rule that holds:\n"
                + "- a: no disk /data/drives/* exists or every disk /data/drives/* with /freePercent >= 10 and /name starting with \"C\"\n"
                + "- b: some disk /data/drives/* with value = null and /label present and /x != \"y\" and /n < 1 and /n <= 1 and /n > 1\n"
                + "- c: otherwise",
            Rules.Describe(rules));
    }

    public static TheoryData<string, string, bool, string?> Quantified() => new()
    {
        // quantifier, tests, truncated, expected class ("a" when the condition holds, "c" when it does not, null when undecided)
        { "some", """[{"pointer":"/freePercent","op":"lt","value":10}]""", false, "a" },
        { "some", """[{"pointer":"/freePercent","op":"lt","value":10}]""", true, "a" },
        { "some", """[{"pointer":"/freePercent","op":"lt","value":1}]""", false, "c" },
        { "some", """[{"pointer":"/freePercent","op":"lt","value":1}]""", true, null },
        { "every", """[{"pointer":"/freePercent","op":"le","value":60}]""", false, "a" },
        { "every", """[{"pointer":"/freePercent","op":"le","value":60}]""", true, null },
        { "every", """[{"pointer":"/freePercent","op":"gt","value":5}]""", true, "c" },
        { "none", """[{"pointer":"/freePercent","op":"gt","value":60}]""", false, "a" },
        { "none", """[{"pointer":"/freePercent","op":"gt","value":60}]""", true, null },
        { "none", """[{"pointer":"/freePercent","op":"ge","value":60}]""", false, "c" },
        { "some", """[{"pointer":"/name","op":"eq","value":"D:"},{"pointer":"/freePercent","op":"eq","value":60}]""", false, "a" },
        { "some", """[{"pointer":"/name","op":"eq","value":"D:"},{"pointer":"/freePercent","op":"eq","value":5}]""", false, "c" },
        { "some", """[{"pointer":"/name","op":"ne","value":"C:"}]""", false, "a" },
        { "some", """[{"pointer":"/missing","op":"ne","value":"C:"}]""", false, "c" },
        { "some", """[{"pointer":"/name","op":"prefix","value":"D"}]""", false, "a" },
        { "some", """[{"pointer":"/freePercent","op":"prefix","value":"6"}]""", false, "c" },
        { "some", """[{"pointer":"/name","op":"lt","value":10}]""", false, "c" },
        { "some", """[{"pointer":"/label","op":"eq","value":null}]""", false, "a" },
        { "some", """[{"pointer":"/label","op":"present"}]""", false, "a" },
        { "some", """[{"pointer":"/size","op":"present"}]""", false, "c" },
    };

    [Theory]
    [MemberData(nameof(Quantified))]
    public void DecidesFromTheEvidence(string quantifier, string tests, bool truncated, string? expected)
    {
        var rules = One("a", Condition(quantifier, tests))[..^1] + """,{"class":"c","all":[]}]""";
        Assert.Equal(expected, Decide(rules, Result("disk", Drives, truncated)));
    }

    [Fact]
    public void DecidesNothingWithoutTheResults()
    {
        var rules = One("a", Condition("some", """[{"pointer":"","op":"present"}]"""))[..^1] + """,{"class":"c","all":[]}]""";
        Assert.Null(Decide(rules));
        Assert.Null(Decide(rules, Result("ollama", """{"reachable":false}""")));
        Assert.Null(Decide(rules, Failure("disk")));
        Assert.Null(Decide(rules, Result("disk", Drives), Failure("disk")));
        Assert.Equal("c", Decide(rules, Result("disk", """{"drives":[]}""")));
        Assert.Equal("c", Decide(rules, Result("disk", """{"drives":{"0":1}}""")));
        var every = One("a", Condition("every", """[{"pointer":"","op":"present"}]"""))[..^1] + """,{"class":"c","all":[]}]""";
        Assert.Equal("c", Decide(every, Result("disk", """{"drives":[]}""")));

        // A trimmed result may have lost every item, so an empty "every" is undecided there.
        Assert.Null(Decide(every, Result("disk", """{"drives":[]}""", truncated: true)));

        // No rule holds: the class is undecided rather than guessed.
        Assert.Null(Decide(One("a", Condition("some", """[{"pointer":"/freePercent","op":"gt","value":99}]""")), Result("disk", Drives)));
    }

    [Fact]
    public void CombinesConditionsInThreeValues()
    {
        var holds = Condition("some", """[{"pointer":"/freePercent","op":"eq","value":5}]""");
        var fails = Condition("some", """[{"pointer":"/freePercent","op":"eq","value":7}]""");
        var undecided = Condition("some", """[{"pointer":"","op":"present"}]""", tool: "ollama", pointer: "/data/models/*");
        string Rule(bool all, params string[] conditions) => $$"""[{"class":"a","{{(all ? "all" : "any")}}":[{{string.Join(",", conditions)}}]},{"class":"c","all":[]}]""";
        var disk = Result("disk", Drives);

        Assert.Equal("a", Decide(Rule(false, undecided, holds), disk));
        Assert.Equal("c", Decide(Rule(false, fails, fails), disk));
        Assert.Null(Decide(Rule(false, fails, undecided), disk));
        Assert.Equal("c", Decide(Rule(true, undecided, fails), disk));
        Assert.Null(Decide(Rule(true, holds, undecided), disk));
        Assert.Equal("a", Decide(Rule(true, holds, holds), disk));
    }

    [Fact]
    public void ReportsTheFactsADecisionRestsOn()
    {
        var any = $$"""[{"class":"a","any":[{{Condition("some", """[{"pointer":"/freePercent","op":"gt","value":99}]""")}},{{Condition("some", """[{"pointer":"/name","op":"eq","value":"D:"},{"pointer":"/label","op":"eq","value":null}]""")}}]}]""";
        var decision = Rules.Decide(Parse(any), [Result("disk", Drives)]);
        Assert.Equal("a", decision.Class);
        Assert.Equal("a", decision.Rule!.Class);
        Assert.Null(Rules.Decide(Parse(any), []).Rule);
        Assert.Equal([("disk", "/data/drives/1/name", "\"D:\""), ("disk", "/data/drives/1/label", (string?)null)],
            decision.Basis.Select(fact => (fact.Tool, fact.Pointer, fact.Value?.ToJsonString())));

        // The element itself is not a scalar fact; every match of a wide "every" is capped.
        var objects = Rules.Decide(Parse(One("a", Condition("some", """[{"pointer":"","op":"present"}]"""))), [Result("disk", Drives)]);
        Assert.Empty(objects.Basis);
        var many = "[" + string.Join(",", Enumerable.Range(0, 12).Select(index => $$"""{"n":{{index}}}""")) + "]";
        var capped = Rules.Decide(Parse(One("a", Condition("every", """[{"pointer":"/n","op":"ge","value":0}]""", pointer: "/data/x~1y/*"))), [Result("disk", $$"""{"x/y":{{many}}}""")]);
        Assert.Equal(Rules.MaxBasis, capped.Basis.Count);
        Assert.Equal("/data/x~1y/7/n", capped.Basis[^1].Pointer);

        // Two results of one tool are read together.
        var both = Rules.Decide(Parse(One("a", Condition("every", """[{"pointer":"/freePercent","op":"ge","value":5}]"""))), [Result("disk", Drives), Result("disk", """{"drives":[{"freePercent":9}]}""")]);
        Assert.Equal(3, both.Basis.Count);
    }

    private static readonly Policy Shipped = Policy.Parse(File.ReadAllBytes("policy.json"));

    private const string Up = """{"adapters":[{"status":"up","gateways":["<ip>"]}]}""";
    private const string Ok = """{"drives":[{"freePercent":60}]}""";
    private const string Reachable = """{"reachable":true,"models":[{"identity":true}]}""";
    private const string Running = """{"available":true,"services":[{"name":"<host>","githubRunner":true,"state":"RUNNING"}],"tasks":[]}""";
    private const string Quiet = """{"available":true,"hours":24,"groups":[],"errors":[]}""";

    public static TheoryData<string, string[], string?> ShippedCases() => new()
    {
        { "github-unreachable", ["network-link", """{"adapters":[],"omitted":3}"""], "no-active-link" },
        { "github-unreachable", ["network-link", Up], "link-up" },
        { "github-unreachable", ["network-link", """{"adapters":[{"status":"up","gateways":[]}]}"""], "no-default-route" },
        { "github-unreachable", [], null },
        { "disk-full", ["disk", """{"drives":[]}"""], "inconclusive" },
        { "disk-full", ["disk", """{"drives":[{"freePercent":5},{"freePercent":60}]}"""], "disk-low" },
        { "disk-full", ["disk", Ok], "disk-ok" },
        { "disk-full", ["disk!", Ok], null },
        { "ollama-empty", ["ollama", """{"reachable":false}"""], "ollama-unreachable" },
        { "ollama-empty", ["ollama", """{"reachable":true,"models":[]}"""], "no-models" },
        { "ollama-empty", ["ollama", """{"reachable":true,"models":[{"identity":false},{"identity":true}]}"""], "identity-present" },
        { "ollama-empty", ["ollama", """{"reachable":true,"models":[{"identity":false}]}"""], "identity-missing" },
        { "vm-states", ["vm-states", """{"available":false,"reason":"VBoxManage is not installed"}"""], "virtualbox-missing" },
        { "vm-states", ["vm-states", """{"available":true,"error":"list vms exited 1"}"""], "inconclusive" },
        { "vm-states", ["vm-states", """{"available":true,"vms":[]}"""], "no-vms-visible" },
        { "vm-states", ["vm-states", """{"available":true,"vms":[{"running":false},{"running":true}]}"""], "vms-running" },
        { "vm-states", ["vm-states", """{"available":true,"vms":[{"running":null}]}"""], "inconclusive" },
        { "vm-states", ["vm-states", """{"available":true,"vms":[{"running":false,"error":"showvminfo exited 1"}]}"""], "no-vm-running" },
        { "runner-offline", ["services", """{"available":false,"reason":"Windows only"}"""], "inconclusive" },
        { "runner-offline", ["services", """{"available":true,"services":[],"error":"sc.exe query failed","tasks":[]}"""], "inconclusive" },
        { "runner-offline", ["services", """{"available":true,"services":[{"name":"gitlab-runner","githubRunner":false,"state":"RUNNING"}],"tasks":[]}"""], "runner-service-missing" },
        { "runner-offline", ["services", """{"available":true,"services":[{"name":"<host>","githubRunner":true,"state":"STOPPED"}],"tasks":[]}"""], "runner-service-stopped" },
        { "runner-offline", ["services", Running], null },
        { "runner-offline", ["services", Running, "event-errors", """{"available":true,"hours":24,"groups":[{"provider":"Microsoft-Windows-Kernel-Power","id":41}],"errors":[]}"""], "recent-crash" },
        { "runner-offline", ["services", Running, "event-errors", """{"available":true,"hours":24,"groups":[{"provider":"EventLog","id":6008}],"errors":[]}"""], "recent-crash" },
        { "runner-offline", ["services", Running, "event-errors", """{"available":true,"hours":24,"groups":[],"errors":[{"log":"System","error":"wevtutil query failed"}]}"""], "inconclusive" },
        { "runner-offline", ["services", Running, "event-errors", Quiet], "runner-service-running" },
        { "general-health", ["network-link", Up, "disk", Ok, "ollama", Reachable, "services", Running], "healthy" },
        { "general-health", ["network-link", Up, "disk", """{"drives":[{"freePercent":5}]}"""], "degraded" },
        { "general-health", ["network-link", Up, "disk", Ok, "ollama", Reachable, "services", """{"available":true,"services":[],"error":"sc.exe query failed","tasks":[]}"""], null },
        { "general-health", ["network-link", Up, "disk", Ok, "ollama", Reachable, "services", """{"available":false,"reason":"Windows only"}"""], null },
        { "general-health", ["network-link", Up, "disk", Ok, "services", Running], null },
    };

    [Theory]
    [MemberData(nameof(ShippedCases))]
    public void TheShippedPolicyDecidesOnlyWhatTheEvidenceSupports(string question, string[] results, string? expected)
    {
        // A tool name ending in "!" stands for a truncated result.
        var evidence = Enumerable.Range(0, results.Length / 2)
            .Select(index => Result(results[2 * index].TrimEnd('!'), results[2 * index + 1], truncated: results[2 * index].EndsWith('!')))
            .ToList();
        Assert.Equal(expected, Rules.Decide(Shipped.Find(question).Rules, evidence).Class);
    }
}
