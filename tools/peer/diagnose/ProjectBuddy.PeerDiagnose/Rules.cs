using System.Globalization;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using ProjectBuddy.PeerOllama;
using ProjectBuddy.PeerProbes;

namespace ProjectBuddy.PeerDiagnose;

// One test on an element: its relative pointer ("" is the element itself), the operator, and the policy's value.
public sealed record Test(string Pointer, string Op, JsonNode? Value);

// Some, every or no element matched by Pointer (which may use * over arrays) in Tool's result passes all its tests.
public sealed record Condition(string Tool, string Pointer, string Quantifier, IReadOnlyList<Test> Tests);

// The class a question's answer must have when its conditions hold: all of them, or any of them.
public sealed record Rule(string Class, bool All, IReadOnlyList<Condition> Conditions);

// A fact a decided class rests on: a concrete pointer in a tool result and the scalar found there.
public sealed record Basis(string Tool, string Pointer, JsonNode? Value);

// The class the rules decide from the evidence, with its basis; Class is null when the rules cannot decide.
public sealed record Decision(string? Class, IReadOnlyList<Basis> Basis);

// The closed rule language that makes an answer's class machine-checkable (#299). Rules are tried in order and the first
// that holds decides. A condition is undecided when its tool was not called or failed, or when a truncated result could
// change it; an undecided rule before the deciding one leaves the class undecided, so it is reported as advisory.
public static class Rules
{
    public const int MaxBasis = 8;

    public static readonly string[] Quantifiers = ["some", "every", "none"];

    public static readonly string[] Ops = ["eq", "ne", "lt", "le", "gt", "ge", "prefix", "present"];

    private static readonly string[] Numeric = ["lt", "le", "gt", "ge"];

    public static IReadOnlyList<Rule> Parse(JsonNode? node, string question, IReadOnlyList<string> classes)
    {
        var rules = new List<Rule>();
        foreach (var item in Json.Array(node, $"{question} rules"))
        {
            var rule = Json.Object(item, $"{question} rule");
            var all = rule.ContainsKey("all");
            Json.Keys(rule, $"{question} rule", "class", all ? "all" : "any");
            var name = Json.String(rule["class"], $"{question} rule class");
            if (!classes.Contains(name))
            {
                throw Refused($"{question} rule class {name} is not one of its classes");
            }

            rules.Add(new Rule(name, all, [.. Json.Array(rule[all ? "all" : "any"], $"{question} {name} conditions").Select(condition => ParseCondition(condition, $"{question} {name}"))]));
        }

        return rules.Count > 0 ? rules : throw Refused($"{question} needs at least one rule");
    }

    // The rules as the model reads them, generated so the prompt and the check cannot drift apart.
    public static string Describe(IReadOnlyList<Rule> rules)
    {
        var text = new StringBuilder("Classes, decided by the first rule that holds:");
        foreach (var rule in rules)
        {
            var conditions = rule.Conditions.Select(Describe).ToList();
            text.Append("\n- ").Append(rule.Class).Append(": ")
                .Append(conditions.Count == 0 ? "otherwise" : string.Join(rule.All ? " and " : " or ", conditions));
        }

        return text.ToString();
    }

    internal static Decision Decide(IReadOnlyList<Rule> rules, IReadOnlyList<Evidence> evidence)
    {
        foreach (var rule in rules)
        {
            var (holds, basis) = Evaluate(rule, evidence);
            if (holds is null)
            {
                return new Decision(null, []);
            }

            if (holds.Value)
            {
                return new Decision(rule.Class, [.. basis.Take(MaxBasis)]);
            }
        }

        return new Decision(null, []);
    }

    private static Condition ParseCondition(JsonNode? node, string what)
    {
        var condition = Json.Object(node, $"{what} condition");
        Json.Keys(condition, $"{what} condition", "tool", "pointer", "quantifier", "test");
        var tool = Json.String(condition["tool"], $"{what} tool");
        var pointer = Json.String(condition["pointer"], $"{what} pointer");
        var quantifier = Json.String(condition["quantifier"], $"{what} quantifier");
        if (!Registry.Tools.Any(spec => spec.Name == tool) || pointer[0] != '/' || !Quantifiers.Contains(quantifier))
        {
            throw Refused($"{what} needs a registry tool, a pointer starting with / and a quantifier of {string.Join("|", Quantifiers)}");
        }

        var tests = Json.Array(condition["test"], $"{what} test").Select(test => ParseTest(test, what)).ToList();
        return tests.Count > 0 ? new Condition(tool, pointer, quantifier, tests) : throw Refused($"{what} needs at least one test");
    }

    private static Test ParseTest(JsonNode? node, string what)
    {
        var test = Json.Object(node, $"{what} test");
        var op = Json.String(test["op"], $"{what} op");
        Json.Keys(test, $"{what} test", op == "present" ? ["pointer", "op"] : ["pointer", "op", "value"]);
        var pointer = Json.TryString(test["pointer"]) ?? throw Refused($"{what} test pointer must be a string");
        var value = test["value"];
        var kind = value?.GetValueKind();
        if (!Ops.Contains(op) || (pointer.Length > 0 && pointer[0] != '/') || pointer.Contains('*')
            || (Numeric.Contains(op) && kind != JsonValueKind.Number) || (op == "prefix" && kind != JsonValueKind.String)
            || value is JsonObject or JsonArray)
        {
            throw Refused($"{what} test needs an op of {string.Join("|", Ops)}, a relative pointer without *, and a scalar value of the op's type");
        }

        return new Test(pointer, op, value?.DeepClone());
    }

    private static string Describe(Condition condition)
    {
        var quantifier = condition.Quantifier == "none" ? "no" : condition.Quantifier;
        if (condition.Tests is [{ Pointer: "", Op: "present" }])
        {
            return $"{quantifier} {condition.Tool} {condition.Pointer} exists";
        }

        return $"{quantifier} {condition.Tool} {condition.Pointer} with " + string.Join(" and ", condition.Tests.Select(test =>
            (test.Pointer.Length == 0 ? "value" : test.Pointer) + test.Op switch
            {
                "present" => " present",
                "prefix" => $" starting with {test.Value!.ToJsonString()}",
                _ => $" {Symbol(test.Op)} {test.Value?.ToJsonString() ?? "null"}",
            }));
    }

    private static string Symbol(string op) => op switch
    {
        "eq" => "=",
        "ne" => "!=",
        "lt" => "<",
        "le" => "<=",
        "gt" => ">",
        _ => ">=",
    };

    // Three-valued: true, false, or null when undecided.
    private static (bool? Holds, List<Basis> Basis) Evaluate(Rule rule, IReadOnlyList<Evidence> evidence)
    {
        var basis = new List<Basis>();
        var undecided = false;
        foreach (var condition in rule.Conditions)
        {
            var (holds, witnesses) = Evaluate(condition, evidence);
            if (holds == !rule.All)
            {
                // The first true condition of "any", or the first false one of "all", settles the rule.
                return (holds, rule.All ? [] : witnesses);
            }

            undecided |= holds is null;
            basis.AddRange(witnesses);
        }

        return undecided ? (null, []) : (rule.All, rule.All ? basis : []);
    }

    private static (bool? Holds, List<Basis> Basis) Evaluate(Condition condition, IReadOnlyList<Evidence> evidence)
    {
        var results = evidence.Where(item => item.Tool == condition.Tool).Select(item => JsonNode.Parse(item.Envelope)!.AsObject()).ToList();
        if (results.Count == 0 || results.Any(result => result["data"] is null))
        {
            return (null, []);
        }

        var truncated = results.Any(result => (bool)result["truncated"]!);
        var elements = results.SelectMany(result => Expand(result, condition.Pointer)).ToList();
        var passing = elements.Where(element => condition.Tests.All(test => Passes(element.Node, test))).ToList();
        var witnesses = passing.SelectMany(element => Witnesses(condition, element)).ToList();
        return condition.Quantifier switch
        {
            "some" => passing.Count > 0 ? (true, witnesses) : truncated ? (null, []) : (false, []),
            "every" => elements.Count == 0 || passing.Count < elements.Count ? (false, []) : truncated ? (null, []) : (true, witnesses),
            _ => passing.Count > 0 ? (false, []) : truncated ? (null, []) : (true, []),
        };
    }

    // The scalars an element's tests looked at, as concrete pointers into the tool result.
    private static IEnumerable<Basis> Witnesses(Condition condition, (string Pointer, JsonNode? Node) element)
    {
        foreach (var test in condition.Tests)
        {
            var node = element.Node;
            if (test.Pointer.Length > 0)
            {
                // A passing element resolved every test's pointer already.
                Diagnosis.Resolve(element.Node, test.Pointer, out node);
            }

            if (node is null or JsonValue)
            {
                yield return new Basis(condition.Tool, element.Pointer + test.Pointer, node?.DeepClone());
            }
        }
    }

    private static bool Passes(JsonNode? element, Test test)
    {
        JsonNode? node;
        if (test.Pointer.Length == 0)
        {
            node = element;
        }
        else if (!Diagnosis.Resolve(element, test.Pointer, out node))
        {
            return false;
        }

        return test.Op switch
        {
            "present" => true,
            "eq" => Diagnosis.Same(node, test.Value),
            "ne" => !Diagnosis.Same(node, test.Value),
            "prefix" => Json.TryString(node) is { } text && text.StartsWith(test.Value!.GetValue<string>(), StringComparison.Ordinal),
            _ => node is JsonValue value && value.GetValueKind() == JsonValueKind.Number && Compare(Number(value), Number(test.Value!), test.Op),
        };
    }

    private static double Number(JsonNode node) => double.Parse(node.ToJsonString(), NumberStyles.Float, CultureInfo.InvariantCulture);

    private static bool Compare(double left, double right, string op) => op switch
    {
        "lt" => left < right,
        "le" => left <= right,
        "gt" => left > right,
        _ => left >= right,
    };

    // Every node a pointer with * reaches, with its concrete pointer.
    private static List<(string Pointer, JsonNode? Node)> Expand(JsonNode? root, string pointer)
    {
        List<(string Pointer, JsonNode? Node)> current = [("", root)];
        foreach (var raw in pointer[1..].Split('/'))
        {
            var next = new List<(string Pointer, JsonNode? Node)>();
            foreach (var (path, node) in current)
            {
                if (raw != "*")
                {
                    if (Diagnosis.Resolve(node, "/" + raw, out var child))
                    {
                        next.Add(($"{path}/{raw}", child));
                    }
                }
                else if (node is JsonArray array)
                {
                    next.AddRange(array.Select((child, index) => ($"{path}/{index}", child)));
                }
            }

            current = next;
        }

        return current;
    }

    private static PeerException Refused(string message) => new(ExitCodes.Refused, message);
}
