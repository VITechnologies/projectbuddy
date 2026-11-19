using System.Globalization;
using System.Text.Json.Nodes;

namespace ProjectBuddy.Kpi;

// The premium-interactions quota (#373): what the newest ingested snapshot says is spent, the headroom left under the
// budget, how fast it is burning and when it runs out, and which of an ordered list of work items it still covers.
internal static class Quota
{
    // Beyond this many hours (over a century) a projection is no projection.
    private const double MaxProjectionHours = 1_000_000;

    public static JsonObject Run(Database database, Options options, out int exit)
    {
        var window = options.Get("--window-hours") is null ? (double)database.Setting("burn_window_hours") : (double)Program.Number(options, "--window-hours");
        var snapshots = database.QuotaSnapshots(window);
        if (snapshots.Count == 0)
        {
            throw new KpiException(ExitCodes.Refused, "no quota snapshot has been ingested; run ingest");
        }

        var budget = database.OptionalSetting("budget_usd") ?? throw new KpiException(ExitCodes.Refused, "budget_usd is not set; run budget");
        var reserve = database.OptionalSetting("reserve_usd") ?? 0;
        var price = database.Setting("usd_per_unit");
        var newest = snapshots[^1];
        var spend = newest.Overage * price;
        var headroom = budget - spend;
        var allocable = Math.Max(0, headroom - reserve);

        // A decrease in the overage is a new billing cycle: the burn counts only what came after it.
        var from = 0;
        for (var index = 1; index < snapshots.Count; index++)
        {
            from = snapshots[index].Overage < snapshots[index - 1].Overage ? index : from;
        }

        var cycle = snapshots[from..];
        var hours = (newest.At - cycle[0].At).TotalHours;
        decimal? burn = cycle.Count >= 2 && hours > 0 ? (spend - (cycle[0].Overage * price)) / (decimal)hours : null;

        DateTimeOffset? exhaustion = null;
        if (burn is > 0 && (double)(Math.Max(0, headroom) / burn.Value) is var left and <= MaxProjectionHours)
        {
            exhaustion = newest.At.AddHours(left);
        }

        var exhausts = exhaustion < newest.ResetDate;
        var result = new JsonObject
        {
            ["snapshotAt"] = newest.At,
            ["usedRequests"] = newest.UsedRequests,
            ["entitlementRequests"] = newest.EntitlementRequests,
            ["resetDate"] = newest.ResetDate,
            ["spendUsd"] = spend,
            ["budgetUsd"] = budget,
            ["headroomUsd"] = headroom,
            ["reserveUsd"] = reserve,
            ["allocableUsd"] = allocable,
            ["burnWindowHours"] = window,
            ["burnUsdPerHour"] = burn,
            ["projectedExhaustionAt"] = exhaustion,
            ["exhaustsBeforeReset"] = exhausts,
        };
        if (options.Get("--items") is { } list)
        {
            result["scope"] = Scope(Items(list), allocable);
        }

        exit = headroom < reserve || exhausts ? ExitCodes.Alarm : ExitCodes.Ok;
        return result;
    }

    // The ordered work items, name=usd: the order is their priority.
    public static List<(string Name, decimal Usd)> Items(string list)
    {
        var items = new List<(string, decimal)>();
        foreach (var entry in list.Split(','))
        {
            var parts = entry.Split('=');
            if (parts.Length != 2 || parts[0].Length == 0 || items.Any(item => item.Item1 == parts[0])
                || !decimal.TryParse(parts[1], NumberStyles.AllowDecimalPoint, CultureInfo.InvariantCulture, out var usd))
            {
                throw new KpiException(ExitCodes.Usage, "--items must be name=usd pairs, comma-separated, with unique names and usd >= 0");
            }

            items.Add((parts[0], usd));
        }

        return items;
    }

    private static JsonObject Scope(List<(string Name, decimal Usd)> items, decimal allocable)
    {
        var (total, covered, open) = (0m, 0m, true);
        var array = new JsonArray();
        foreach (var (name, usd) in items)
        {
            total += usd;
            open &= total <= allocable;
            covered += open ? usd : 0;
            array.Add(new JsonObject { ["name"] = name, ["usd"] = usd, ["covered"] = open });
        }

        return new JsonObject
        {
            ["items"] = array,
            ["coveredCount"] = array.Count(item => (bool)item!["covered"]!),
            ["coveredUsd"] = covered,
            ["uncoveredUsd"] = total - covered,
        };
    }
}
