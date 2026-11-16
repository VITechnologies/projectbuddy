using System.Diagnostics.CodeAnalysis;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using ProjectBuddy.PeerOllama;
using ProjectBuddy.PeerRedact;

namespace ProjectBuddy.PeerProbes;

public sealed class UsageException(string message) : Exception(message);

// projectbuddy-probes list | run <tool> [--args <json>] | run-all [--hours <n>]
//   [--deny-list <path>] [--ollama <url>] [--vboxmanage <path>] [--identity <model>]
public static class Program
{
    public const string ListSchema = "projectbuddy/peer-tools@1";
    public const string RunSchema = "projectbuddy/peer-tool-run@1";
    public const string DefaultOllama = "http://127.0.0.1:11434/";

    internal sealed record Cli(string Command, string? Tool, JsonObject? Arguments, string? DenyList, string Ollama, string? VBoxManage, string Identity, int Hours);

    [ExcludeFromCodeCoverage(Justification = "The process entry point; RunAsync takes the host and the sources as parameters.")]
    public static Task<int> Main(string[] args) =>
        RunAsync(args, Console.Out, Console.Error, (Environment.MachineName, Environment.UserName), (cli, ollama) => new SystemSources(ollama, cli.VBoxManage));

    internal static async Task<int> RunAsync(string[] args, TextWriter output, TextWriter error, (string Machine, string User) host, Func<Cli, Uri, ISources> sources)
    {
        try
        {
            var cli = Parse(args);
            if (cli.Command == "list")
            {
                output.WriteLine(List());
                return 0;
            }

            var ollama = OllamaClient.RequireLoopback(cli.Ollama);
            var registry = new Registry(sources(cli, ollama), LoadRedactor(cli.DenyList, host).Redact, new ProbeOptions(cli.Identity));
            if (cli.Command == "run")
            {
                output.WriteLine(await registry.InvokeAsync(cli.Tool!, cli.Arguments));
                return 0;
            }

            // run-all: every tool once, one result per line, then their SHA-256 (the live proof records only these). The
            // arguments are checked before any tool runs.
            var calls = Registry.Tools.Select(spec => (spec, Arguments: spec.Arguments.Count > 0 ? new JsonObject { ["hours"] = cli.Hours } : null)).ToList();
            calls.ForEach(call => Registry.Validate(call.spec, call.Arguments));
            var hashes = new JsonObject();
            foreach (var (spec, arguments) in calls)
            {
                var result = await registry.InvokeAsync(spec.Name, arguments);
                output.WriteLine(result);
                hashes[spec.Name] = Convert.ToHexStringLower(SHA256.HashData(Encoding.UTF8.GetBytes(result)));
            }

            output.WriteLine(new JsonObject { ["schema"] = RunSchema, ["sha256"] = hashes }.ToJsonString(Registry.Output));
            return 0;
        }
        catch (UsageException usage)
        {
            error.WriteLine(usage.Message);
            error.WriteLine("usage: projectbuddy-probes list | run <tool> [--args <json>] | run-all [--hours <n>] [--deny-list <path>] [--ollama <url>] [--vboxmanage <path>] [--identity <model>]");
            return ExitCodes.Usage;
        }
        catch (Exception refused) when (refused is ToolRefusedException or RedactionException)
        {
            error.WriteLine($"refused: {refused.Message}");
            return ExitCodes.Refused;
        }
        catch (PeerException peer)
        {
            error.WriteLine(peer.Message);
            return peer.ExitCode;
        }
    }

    // The tool list a model is offered: names, descriptions and argument ranges.
    internal static string List() => new JsonObject
    {
        ["schema"] = ListSchema,
        ["tools"] = new JsonArray([.. Registry.Tools.Select(spec => (JsonNode)new JsonObject
        {
            ["name"] = spec.Name,
            ["description"] = spec.Description,
            ["arguments"] = new JsonArray([.. spec.Arguments.Select(argument => (JsonNode)new JsonObject
            {
                ["name"] = argument.Name,
                ["type"] = "integer",
                ["min"] = argument.Min,
                ["max"] = argument.Max,
                ["default"] = argument.Default,
                ["description"] = argument.Description,
            })]),
        })]),
    }.ToJsonString(Registry.Output);

    // The host's own machine and account names are always private; an unreadable deny list refuses rather than
    // redacting with fewer terms.
    internal static Redactor LoadRedactor(string? denyList, (string Machine, string User) host)
    {
        if (denyList is null)
        {
            return Redactor.ForHost([], host.Machine, host.User);
        }

        byte[] bytes;
        try
        {
            bytes = File.ReadAllBytes(denyList);
        }
        catch (Exception error)
        {
            throw new RedactionException($"the deny list cannot be read: {error.GetType().Name}");
        }

        return Redactor.ForHost(Redactor.ParseDenyList(bytes), host.Machine, host.User);
    }

    internal static Cli Parse(string[] args)
    {
        if (args.Length == 0 || args[0] is not ("list" or "run" or "run-all"))
        {
            throw new UsageException("expected a command: list, run or run-all");
        }

        var command = args[0];
        var index = 1;
        string? tool = null;
        if (command == "run")
        {
            if (args.Length < 2 || args[1].StartsWith("--", StringComparison.Ordinal))
            {
                throw new UsageException("run needs a tool name");
            }

            tool = args[1];
            index = 2;
        }

        var options = new Dictionary<string, string>(StringComparer.Ordinal);
        string[] allowed = command switch
        {
            "list" => [],
            "run" => ["--args", "--deny-list", "--ollama", "--vboxmanage", "--identity"],
            _ => ["--hours", "--deny-list", "--ollama", "--vboxmanage", "--identity"],
        };
        for (; index < args.Length; index += 2)
        {
            if (!allowed.Contains(args[index]) || options.ContainsKey(args[index]))
            {
                throw new UsageException($"unexpected argument '{args[index]}'");
            }

            if (index + 1 >= args.Length)
            {
                throw new UsageException($"{args[index]} needs a value");
            }

            options[args[index]] = args[index + 1];
        }

        JsonObject? arguments = null;
        if (options.TryGetValue("--args", out var json))
        {
            try
            {
                arguments = JsonNode.Parse(json) as JsonObject ?? throw new ToolRefusedException("--args must be a JSON object");
            }
            catch (JsonException)
            {
                throw new ToolRefusedException("--args is not valid JSON");
            }
        }

        var hours = 24;
        if (options.TryGetValue("--hours", out var text) && !int.TryParse(text, out hours))
        {
            throw new UsageException("--hours needs a whole number");
        }

        return new Cli(command, tool, arguments, options.GetValueOrDefault("--deny-list"), options.GetValueOrDefault("--ollama") ?? DefaultOllama,
            options.GetValueOrDefault("--vboxmanage"), options.GetValueOrDefault("--identity") ?? new ProbeOptions().IdentityModel, hours);
    }
}
