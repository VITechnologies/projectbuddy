using System.Text;
using System.Text.Encodings.Web;
using System.Text.Json;
using System.Text.Json.Nodes;
using ProjectBuddy.PeerRedact;

namespace ProjectBuddy.PeerProbes;

public sealed class ToolRefusedException(string message) : Exception(message);

public sealed record IntArgument(string Name, int Min, int Max, int Default, string Description);

public sealed record ToolSpec(string Name, string Description, IReadOnlyList<IntArgument> Arguments);

public sealed record ProbeOptions(string IdentityModel = "projectbuddy:0.1-qwen2.5-7b", int MaxResultBytes = Registry.MaxResultBytes);

// The fixed, read-only tools the P4 diagnostics loop may call (#292). A call names one tool from the closed list with
// typed, range-checked arguments; anything else is refused. Every result is bounded, deterministic for a given machine
// state, redacted before it is returned, and capped in bytes with an explicit truncated flag.
public sealed class Registry(ISources sources, Func<string, Redaction> redact, ProbeOptions options)
{
    public const string Schema = "projectbuddy/peer-tool-result@1";
    public const int MaxResultBytes = 16 * 1024;

    // Every failure envelope (a fixed reason and a tool name from the closed list) fits under this, so no result is
    // ever over the cap.
    public const int MinResultBytes = 256;
    public const int MaxAdapters = 16;
    public const int MaxDrives = 26;
    public const int MaxModels = 64;
    public const int MaxVms = 32;
    public const int MaxServices = 16;
    public const int MaxGroups = 50;
    public const int MaxEventsPerLog = 2000;

    public static readonly IReadOnlyList<ToolSpec> Tools =
    [
        new("network-link", "Network adapters, default-route and live ones first: kind, state, speed, IPv4 and IPv6 binding, and default gateways.", []),
        new("disk", "Fixed drives (no read-only images or pseudo file systems): file system, total and free bytes, and the free share in whole percent.", []),
        new("ollama", "Ollama on loopback: version, installed models with digests (identity is true on the pinned projectbuddy model), and loaded models with memory and context size.", []),
        new("vm-states", "VirtualBox VMs: running or not, state, current snapshot, memory, CPUs and whether VRDE is on.", []),
        new("services", "The GitHub runner (githubRunner true) and gitlab-runner services and the ProjectBuddy scheduled tasks: state and start type.", []),
        new("event-errors", "System and Application error and critical events, counted by provider and event ID (no message text).",
            [new("hours", 1, 72, 24, "How many hours back to count.")]),
    ];

    // Scheduled tasks by exact name, and services by fixed name or prefix; nothing else is ever queried or reported.
    public static readonly IReadOnlyList<string> AllowedTasks = ["ProjectBuddy-Bus", "ProjectBuddy-Ollama"];

    public static readonly IReadOnlyList<string> AllowedServicePrefixes = ["actions.runner.", "ProjectBuddy-GitLabRunner-"];

    public static readonly IReadOnlyList<string> AllowedServices = ["gitlab-runner"];

    // showvminfo keys read by exact name; vrdeproperty[...] and every other key are never read.
    public static readonly IReadOnlySet<string> VmKeys = new HashSet<string>(StringComparer.Ordinal)
    {
        "VMState", "VMStateChangeTime", "ostype", "memory", "cpus", "vrde", "CurrentSnapshotName", "CurrentSnapshotUUID",
    };

    // Read-only images (Linux snap and live-media mounts) and kernel pseudo file systems hold no user data, so their
    // space says nothing about the machine; they are left out.
    public static readonly IReadOnlySet<string> SkippedFormats = new HashSet<string>(
        ["squashfs", "erofs", "cramfs", "iso9660", "efivarfs", "tracefs", "debugfs", "securityfs", "pstore", "bpf", "configfs"], StringComparer.OrdinalIgnoreCase);

    private static readonly string[] EventLogs = ["System", "Application"];

    internal static readonly JsonSerializerOptions Output = new() { Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping };

    private readonly int maxBytes = options.MaxResultBytes >= MinResultBytes ? options.MaxResultBytes
        : throw new ArgumentOutOfRangeException(nameof(options), $"MaxResultBytes must be at least {MinResultBytes}");

    public async Task<string> InvokeAsync(string tool, JsonObject? arguments)
    {
        var spec = Tools.FirstOrDefault(candidate => candidate.Name == tool) ?? throw new ToolRefusedException($"unknown tool '{tool}'");
        var values = Validate(spec, arguments);
        JsonObject data;
        bool truncated;
        string[] trim;
        try
        {
            (data, truncated, trim) = tool switch
            {
                "network-link" => NetworkLink(),
                "disk" => Disk(),
                "ollama" => await OllamaAsync(),
                "vm-states" => VmStates(),
                "services" => Services(),
                _ => EventErrors(values["hours"]),
            };
        }
        catch (Exception error)
        {
            // One failing read does not stop the loop. Only the exception's type is reported: its message can carry
            // paths or other text the redactor has not seen.
            return Failure(tool, $"the probe failed: {error.GetType().Name}");
        }

        return Envelope(tool, data, truncated, trim);
    }

    public static IReadOnlyDictionary<string, int> Validate(ToolSpec spec, JsonObject? arguments)
    {
        var values = spec.Arguments.ToDictionary(argument => argument.Name, argument => argument.Default, StringComparer.Ordinal);
        List<KeyValuePair<string, JsonNode?>> pairs;
        try
        {
            // A JsonObject parsed with duplicate keys throws only when its entries are first read.
            pairs = [.. arguments ?? []];
        }
        catch (ArgumentException)
        {
            throw new ToolRefusedException($"{spec.Name} arguments repeat a name");
        }

        foreach (var (name, node) in pairs)
        {
            var argument = spec.Arguments.FirstOrDefault(candidate => candidate.Name == name)
                ?? throw new ToolRefusedException($"{spec.Name} takes no argument '{name}'");
            if (node is not JsonValue value || value.GetValueKind() != JsonValueKind.Number || !value.TryGetValue<int>(out var number))
            {
                throw new ToolRefusedException($"{spec.Name} argument '{name}' must be a whole number");
            }

            if (number < argument.Min || number > argument.Max)
            {
                throw new ToolRefusedException($"{spec.Name} argument '{name}' must be {argument.Min}-{argument.Max}");
            }

            values[name] = number;
        }

        return values;
    }

    // Windows also lists NDIS filter pseudo-interfaces (QoS Packet Scheduler, WFP filters) and tunnels with no IP
    // binding. There, Supports(IPv4/IPv6) reads the adapter's binding flags, so an adapter bound to neither carries no
    // traffic and is counted in "omitted" rather than listed. Elsewhere Supports means "has an address", which an
    // unplugged NIC lacks, so nothing is left out. Adapters with a default gateway come first, then those that are up,
    // then the rest (absent ones last), so the cap and the byte trim never cut the default-route link.
    private (JsonObject, bool, string[]) NetworkLink()
    {
        var all = sources.Adapters().Where(adapter => adapter.Kind != "loopback").ToList();
        var adapters = all.Where(adapter => !sources.IsWindows || adapter.Ipv4 || adapter.Ipv6)
            .OrderBy(adapter => adapter.Gateways.Count > 0 ? 0 : 1).ThenBy(adapter => StatusOrder(adapter.Status))
            .ThenBy(adapter => KindOrder(adapter.Kind)).ThenBy(adapter => adapter.Name, StringComparer.Ordinal).ToList();
        var data = new JsonObject
        {
            ["adapters"] = Array(adapters.Take(MaxAdapters), adapter => new JsonObject
            {
                ["name"] = adapter.Name,
                ["kind"] = adapter.Kind,
                ["status"] = adapter.Status,
                ["speedMbps"] = adapter.SpeedMbps,
                ["ipv4"] = adapter.Ipv4,
                ["ipv6"] = adapter.Ipv6,
                ["gateways"] = Array(adapter.Gateways.Order(StringComparer.Ordinal), gateway => JsonValue.Create(gateway)),
            }),
            ["omitted"] = all.Count - adapters.Count,
        };
        return (data, adapters.Count > MaxAdapters, ["adapters"]);
    }

    private (JsonObject, bool, string[]) Disk()
    {
        var drives = sources.Drives().Where(drive => !SkippedFormats.Contains(drive.Format)).OrderBy(drive => drive.Name, StringComparer.Ordinal).ToList();
        var data = new JsonObject
        {
            ["drives"] = Array(drives.Take(MaxDrives), drive => new JsonObject
            {
                ["name"] = drive.Name,
                ["format"] = drive.Format,
                ["totalBytes"] = drive.TotalBytes,
                ["freeBytes"] = drive.FreeBytes,

                // Computed here, not by a model: whole percent, rounded down.
                ["freePercent"] = drive.TotalBytes > 0 ? drive.FreeBytes * 100 / drive.TotalBytes : null,
            }),
        };
        return (data, drives.Count > MaxDrives, ["drives"]);
    }

    // Models are identified by digest: a name that looks like a host or path may not survive redaction. The pinned
    // identity is marked by name, so diagnosis does not depend on reading it.
    private async Task<(JsonObject, bool, string[])> OllamaAsync()
    {
        if (await sources.OllamaAsync() is not { } state)
        {
            return (new JsonObject { ["reachable"] = false }, false, []);
        }

        var models = state.Models.OrderBy(model => model.Name, StringComparer.Ordinal).ToList();
        var loaded = state.Loaded.OrderBy(model => model.Name, StringComparer.Ordinal).ToList();
        var data = new JsonObject
        {
            ["reachable"] = true,
            ["version"] = state.Version,
            ["models"] = Array(models.Take(MaxModels), model => new JsonObject
            {
                ["digest"] = model.Digest,
                ["name"] = model.Name,
                ["identity"] = model.Name == options.IdentityModel,
            }),
            ["loaded"] = Array(loaded.Take(MaxModels), model => new JsonObject
            {
                ["digest"] = model.Digest,
                ["name"] = model.Name,
                ["sizeBytes"] = model.SizeBytes,
                ["vramBytes"] = model.SizeVramBytes,
                ["contextLength"] = model.ContextLength,
            }),
        };
        return (data, models.Count > MaxModels || loaded.Count > MaxModels, ["models", "loaded"]);
    }

    private (JsonObject, bool, string[]) VmStates()
    {
        if (sources.Run(ProgramPaths.VBoxManage, ["list", "vms"]) is not { } list)
        {
            return (new JsonObject { ["available"] = false, ["reason"] = "VBoxManage is not installed" }, false, []);
        }

        if (list.ExitCode != 0)
        {
            return (new JsonObject { ["available"] = true, ["error"] = $"list vms exited {list.ExitCode}" }, list.Truncated, []);
        }

        var vms = Parsers.VirtualMachines(list.Output).OrderBy(vm => vm.Name, StringComparer.Ordinal).ThenBy(vm => vm.Uuid, StringComparer.Ordinal).ToList();
        var runningList = sources.Run(ProgramPaths.VBoxManage, ["list", "runningvms"]);
        var running = runningList is { ExitCode: 0 } ? Parsers.VirtualMachines(runningList.Output).Select(vm => vm.Uuid).ToHashSet(StringComparer.Ordinal) : null;
        var truncated = list.Truncated || vms.Count > MaxVms || runningList is { Truncated: true };
        var array = new JsonArray();
        foreach (var vm in vms.Take(MaxVms))
        {
            var entry = new JsonObject { ["name"] = vm.Name, ["uuid"] = vm.Uuid, ["running"] = running is null ? null : running.Contains(vm.Uuid) };
            var info = sources.Run(ProgramPaths.VBoxManage, ["showvminfo", vm.Uuid, "--machinereadable"]);
            if (info is not { ExitCode: 0 })
            {
                entry["error"] = info is null ? "VBoxManage is not installed" : $"showvminfo exited {info.ExitCode}";
            }
            else
            {
                truncated |= info.Truncated;
                var values = Parsers.MachineReadable(info.Output, VmKeys);
                Add(entry, "state", values, "VMState");
                Add(entry, "stateChanged", values, "VMStateChangeTime");
                Add(entry, "ostype", values, "ostype");
                AddNumber(entry, "memoryMb", values, "memory");
                AddNumber(entry, "cpus", values, "cpus");
                Add(entry, "vrde", values, "vrde");
                Add(entry, "currentSnapshot", values, "CurrentSnapshotName");
                Add(entry, "currentSnapshotUuid", values, "CurrentSnapshotUUID");
            }

            array.Add((JsonNode)entry);
        }

        return (new JsonObject { ["available"] = true, ["vms"] = array }, truncated, ["vms"]);
    }

    private (JsonObject, bool, string[]) Services()
    {
        if (!sources.IsWindows)
        {
            return (Unavailable(), false, []);
        }

        var data = new JsonObject { ["available"] = true };
        var truncated = false;
        if (sources.Run("sc.exe", ["query", "type=", "service", "state=", "all"]) is { ExitCode: 0 } query)
        {
            var services = Parsers.Services(query.Output).Where(service => AllowedService(service.Name)).OrderBy(service => service.Name, StringComparer.Ordinal).ToList();
            truncated = query.Truncated || services.Count > MaxServices;
            data["services"] = Array(services.Take(MaxServices), service => new JsonObject
            {
                ["name"] = service.Name,

                // Decided before redaction, which can rewrite a runner's name (an FQDN becomes <host>).
                ["githubRunner"] = service.Name.StartsWith("actions.runner.", StringComparison.OrdinalIgnoreCase),
                ["state"] = service.State,
                ["startType"] = sources.Run("sc.exe", ["qc", service.Name]) is { ExitCode: 0 } config ? Parsers.StartType(config.Output) : "unknown",
            });
        }
        else
        {
            data["services"] = new JsonArray();
            data["error"] = "sc.exe query failed";
        }

        data["tasks"] = Array(AllowedTasks, name =>
        {
            var task = sources.Run("schtasks.exe", ["/query", "/tn", name, "/fo", "csv", "/v"]) is { ExitCode: 0 } result ? Parsers.Task(result.Output) : new TaskState("absent", "");
            return new JsonObject { ["name"] = name, ["state"] = task.State, ["lastResult"] = task.LastResult };
        });
        return (data, truncated, ["services"]);
    }

    private (JsonObject, bool, string[]) EventErrors(int hours)
    {
        if (!sources.IsWindows)
        {
            return (Unavailable(), false, []);
        }

        var query = $"*[System[(Level=1 or Level=2) and TimeCreated[timediff(@SystemTime) <= {hours * 3_600_000L}]]]";
        var counts = new Dictionary<(string Log, string Provider, int Id, string Level), int>();
        var errors = new JsonArray();
        var truncated = false;
        foreach (var log in EventLogs)
        {
            if (sources.Run("wevtutil.exe", ["qe", log, "/q:" + query, "/f:xml", $"/c:{MaxEventsPerLog}", "/rd:true"]) is not { ExitCode: 0 } result)
            {
                errors.Add((JsonNode)new JsonObject { ["log"] = log, ["error"] = "wevtutil query failed" });
                continue;
            }

            var (events, complete) = Parsers.Events(result.Output);
            truncated |= result.Truncated || !complete || events.Count >= MaxEventsPerLog;
            foreach (var record in events)
            {
                var key = (log, record.Provider, record.Id, record.Level switch { 1 => "critical", 2 => "error", _ => $"level-{record.Level}" });
                counts[key] = counts.GetValueOrDefault(key) + 1;
            }
        }

        var groups = counts.OrderByDescending(group => group.Value).ThenBy(group => group.Key.Log, StringComparer.Ordinal)
            .ThenBy(group => group.Key.Provider, StringComparer.Ordinal).ThenBy(group => group.Key.Id).ThenBy(group => group.Key.Level, StringComparer.Ordinal).ToList();
        var data = new JsonObject
        {
            ["available"] = true,
            ["hours"] = hours,
            ["groups"] = Array(groups.Take(MaxGroups), group => new JsonObject
            {
                ["log"] = group.Key.Log,
                ["provider"] = group.Key.Provider,
                ["id"] = group.Key.Id,
                ["level"] = group.Key.Level,
                ["count"] = group.Value,
            }),
            ["errors"] = errors,
        };
        return (data, truncated || groups.Count > MaxGroups, ["groups"]);
    }

    // The data is redacted as JSON text and must still parse afterwards; over the byte cap, items are dropped from the
    // end of the trimmable arrays, in order, and the result is marked truncated.
    private string Envelope(string tool, JsonObject data, bool truncated, string[] trim)
    {
        while (true)
        {
            Redaction redaction;
            JsonNode? redacted;
            try
            {
                redaction = redact(data.ToJsonString(Output));
                redacted = JsonNode.Parse(redaction.Text);
            }
            catch (JsonException)
            {
                return Failure(tool, "the redacted result is not valid JSON");
            }
            catch (Exception error)
            {
                // Nothing unredacted is ever returned: a redactor that fails yields only its exception's type.
                return Failure(tool, $"the redaction failed: {error.GetType().Name}");
            }

            var envelope = new JsonObject
            {
                ["schema"] = Schema,
                ["tool"] = tool,
                ["truncated"] = truncated,
                ["redaction"] = new JsonObject(redaction.Counts.OrderBy(count => count.Key, StringComparer.Ordinal)
                    .Select(count => KeyValuePair.Create<string, JsonNode?>(count.Key, count.Value))),
                ["data"] = redacted,
            };
            var text = envelope.ToJsonString(Output);
            var bytes = Encoding.UTF8.GetByteCount(text);
            if (bytes <= maxBytes)
            {
                return text;
            }

            if (trim.Select(key => data[key]!.AsArray()).FirstOrDefault(array => array.Count > 0) is not { } array)
            {
                return Failure(tool, $"the result is over {maxBytes} bytes");
            }

            // Drop the excess share, at least one item, so a large overrun takes a few rounds rather than one per item.
            for (var drop = Math.Max(1, (int)((long)array.Count * (bytes - maxBytes) / bytes)); drop > 0; drop--)
            {
                array.RemoveAt(array.Count - 1);
            }

            truncated = true;
        }
    }

    private static string Failure(string tool, string reason) =>
        new JsonObject { ["schema"] = Schema, ["tool"] = tool, ["truncated"] = true, ["error"] = reason }.ToJsonString(Output);

    private static int StatusOrder(string status) => status switch
    {
        "up" => 0,
        "notpresent" => 2,
        _ => 1,
    };

    private static int KindOrder(string kind) => kind switch
    {
        "ethernet" => 0,
        "wifi" => 1,
        "other" => 2,
        "tunnel" => 3,
        _ => 4,
    };

    private static JsonObject Unavailable() => new() { ["available"] = false, ["reason"] = "Windows only" };

    private static bool AllowedService(string name) =>
        AllowedServices.Contains(name, StringComparer.OrdinalIgnoreCase) || AllowedServicePrefixes.Any(prefix => name.StartsWith(prefix, StringComparison.OrdinalIgnoreCase));

    private static JsonArray Array<T>(IEnumerable<T> items, Func<T, JsonNode?> map) => new([.. items.Select(map)]);

    private static void Add(JsonObject entry, string name, IReadOnlyDictionary<string, string> values, string key)
    {
        if (values.TryGetValue(key, out var value))
        {
            entry[name] = value;
        }
    }

    private static void AddNumber(JsonObject entry, string name, IReadOnlyDictionary<string, string> values, string key)
    {
        if (values.TryGetValue(key, out var value) && long.TryParse(value, out var number))
        {
            entry[name] = number;
        }
    }
}
