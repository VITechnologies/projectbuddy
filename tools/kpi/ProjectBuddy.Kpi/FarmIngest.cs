using System.Globalization;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace ProjectBuddy.Kpi;

// Reads the local AI farm's telemetry export (ALW#10): the file assurance-learning-workbench's `export-farm-telemetry`
// writes from farm_v1. It holds counts and timings only, so any other field refuses the file, and the database's checks
// refuse a value out of range.
internal static class FarmIngest
{
    public const string SchemaVersion = "1.0.0";
    public const string ExportKind = "alw-farm-telemetry";

    public sealed record Job(
        string Source, DateTimeOffset RecordedAt, string Workload, string Instance, string Backend, long Jobs, long CacheHits, long Answered,
        long Failed, long EvalTokens, decimal Window);

    public sealed record Sample(string Source, DateTimeOffset RecordedAt, string Instance, string Backend, decimal Window, decimal Busy);

    private static readonly string[] ExportFields = ["schema_version", "export_kind", "jobs", "device_samples"];
    private static readonly string[] JobFields =
        ["source_sha256", "recorded_at", "workload", "instance_id", "backend", "jobs", "cache_hits", "answered", "failed", "eval_tokens", "window_seconds"];
    private static readonly string[] SampleFields = ["source_sha256", "recorded_at", "instance_id", "backend", "window_seconds", "busy_seconds"];

    public static JsonObject Run(Database database, string path)
    {
        var (jobs, samples) = Read(path);
        var stored = database.StoreFarm(jobs, samples);
        stored["export"] = path;
        return stored;
    }

    internal static (List<Job> Jobs, List<Sample> Samples) Read(string path)
    {
        JsonElement root;
        try
        {
            using var document = JsonDocument.Parse(File.ReadAllText(path));
            root = document.RootElement.Clone();
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or JsonException)
        {
            throw Refused($"cannot read the farm export: {error.GetType().Name}");
        }

        Fields(root, "the export", ExportFields);
        if (Text(root, "schema_version", "the export") != SchemaVersion || Text(root, "export_kind", "the export") != ExportKind)
        {
            throw Refused($"the file is not an {ExportKind} {SchemaVersion} export");
        }

        var jobs = Rows(root, "jobs", JobFields, (row, where) => new Job(
            Text(row, "source_sha256", where), Time(row, where), Text(row, "workload", where), Text(row, "instance_id", where), Text(row, "backend", where),
            Count(row, "jobs", where), Count(row, "cache_hits", where), Count(row, "answered", where), Count(row, "failed", where),
            Count(row, "eval_tokens", where), Seconds(row, "window_seconds", where)));
        var samples = Rows(root, "device_samples", SampleFields, (row, where) => new Sample(
            Text(row, "source_sha256", where), Time(row, where), Text(row, "instance_id", where), Text(row, "backend", where),
            Seconds(row, "window_seconds", where), Seconds(row, "busy_seconds", where)));
        return (jobs, samples);
    }

    private static List<T> Rows<T>(JsonElement root, string name, string[] fields, Func<JsonElement, string, T> read)
    {
        var rows = Field(root, name, JsonValueKind.Array, "the export");
        return [.. rows.EnumerateArray().Select((row, index) =>
        {
            var where = $"{name}[{index}]";
            Fields(row, where, fields);
            return read(row, where);
        })];
    }

    private static void Fields(JsonElement element, string where, string[] names)
    {
        if (element.ValueKind != JsonValueKind.Object
            || !element.EnumerateObject().Select(property => property.Name).Order(StringComparer.Ordinal).SequenceEqual(names.Order(StringComparer.Ordinal)))
        {
            throw Refused($"{where} must be an object with exactly {string.Join(", ", names)}");
        }
    }

    private static JsonElement Field(JsonElement row, string name, JsonValueKind kind, string where)
    {
        var value = row.GetProperty(name);
        return value.ValueKind == kind ? value : throw Refused($"{where}.{name} must be a JSON {kind.ToString().ToLowerInvariant()}");
    }

    private static string Text(JsonElement row, string name, string where) => Field(row, name, JsonValueKind.String, where).GetString()!;

    private static long Count(JsonElement row, string name, string where) =>
        Field(row, name, JsonValueKind.Number, where).TryGetInt64(out var value) ? value : throw Refused($"{where}.{name} must be a whole number");

    private static decimal Seconds(JsonElement row, string name, string where) =>
        Field(row, name, JsonValueKind.Number, where).TryGetDecimal(out var value) ? value : throw Refused($"{where}.{name} is out of range");

    private static DateTimeOffset Time(JsonElement row, string where)
    {
        if (!DateTimeOffset.TryParseExact(Text(row, "recorded_at", where), "yyyy-MM-dd'T'HH:mm:ss.FFFFFFFzzz", CultureInfo.InvariantCulture, DateTimeStyles.None, out var value))
            throw Refused($"{where}.recorded_at must be an ISO 8601 time with an offset");
        // Npgsql writes a timestamptz only from a UTC offset.
        return value.Offset == TimeSpan.Zero ? value : throw Refused($"{where}.recorded_at must be a UTC time (+00:00)");
    }

    private static KpiException Refused(string message) => new(ExitCodes.Refused, message);
}
