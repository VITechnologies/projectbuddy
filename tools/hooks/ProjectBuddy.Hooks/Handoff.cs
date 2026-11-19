using System.Diagnostics;
using System.Globalization;
using System.Security.Cryptography;
using System.Text.Json.Nodes;
using Npgsql;

namespace ProjectBuddy.Hooks;

// Writes to a handoff issue (#23) are compare-and-swap on the issue's updatedAt, under a PostgreSQL advisory lock, so a stale copy
// cannot overwrite a newer rewrite. Reads hand out the updatedAt that a later write must present as --expected.
internal static class Handoff
{
    public static JsonNode Read(Options options)
    {
        var (repository, number) = Split(options.Get("--work-ref")!);
        var (updatedAt, body) = View(options.Get("--gh")!, repository, number);
        return new JsonObject { ["work_ref"] = options.Get("--work-ref"), ["updated_at"] = updatedAt.ToString("O"), ["body"] = body };
    }

    public static JsonNode Write(Database database, Options options)
    {
        var workRef = options.Get("--work-ref")!;
        var gh = options.Get("--gh")!;
        var (repository, number) = Split(workRef);
        if (!DateTimeOffset.TryParse(options.Get("--expected"), CultureInfo.InvariantCulture, DateTimeStyles.RoundtripKind, out var expected))
        {
            throw Program.UsageError("--expected must be the updated_at that handoff-read printed");
        }

        var bodyFile = options.Get("--body-file")!;
        if (!File.Exists(bodyFile))
        {
            throw Program.UsageError($"--body-file {bodyFile} does not exist");
        }

        var seconds = Program.Integer(options, "--lock-seconds");
        var hash = Convert.ToHexStringLower(SHA256.HashData(File.ReadAllBytes(bodyFile)));

        using var connection = database.Open();
        Lock(connection, workRef, seconds);
        var (current, _) = View(gh, repository, number);
        if (current != expected)
        {
            throw new HooksException(ExitCodes.Refused, $"stale: {workRef} changed at {current:O}, but the write was based on {expected:O}; read it again and merge");
        }

        Run(gh, ["issue", "edit", number, "-R", repository, "--body-file", bodyFile]);
        var (updated, _) = View(gh, repository, number);
        using (var record = new NpgsqlCommand(
            "INSERT INTO hooks.handoffs AS h (work_ref, issue_updated_at, body_sha256, writer_session) VALUES ($1, $2, $3, $4) "
            + "ON CONFLICT (work_ref) DO UPDATE SET issue_updated_at = EXCLUDED.issue_updated_at, body_sha256 = EXCLUDED.body_sha256, "
            + "writer_session = EXCLUDED.writer_session, written_at = clock_timestamp()", connection)
        {
            Parameters =
            {
                new() { Value = workRef },
                new() { Value = updated.UtcDateTime },
                new() { Value = hash },
                new() { Value = (object?)options.Get("--session") ?? DBNull.Value, NpgsqlDbType = NpgsqlTypes.NpgsqlDbType.Text },
            },
        })
        {
            record.ExecuteNonQuery();
        }

        return new JsonObject { ["work_ref"] = workRef, ["previous_updated_at"] = current.ToString("O"), ["updated_at"] = updated.ToString("O"), ["body_sha256"] = hash };
    }

    private static void Lock(NpgsqlConnection connection, string workRef, int seconds)
    {
        var deadline = Stopwatch.StartNew();
        while (true)
        {
            using var attempt = new NpgsqlCommand("SELECT pg_try_advisory_lock(hashtext($1))", connection) { Parameters = { new() { Value = "hooks.handoff:" + workRef } } };
            if ((bool)attempt.ExecuteScalar()!)
            {
                return;
            }

            if (deadline.Elapsed.TotalSeconds >= seconds)
            {
                throw new HooksException(ExitCodes.Refused, $"another writer holds the {workRef} lock; try again");
            }

            Thread.Sleep(100);
        }
    }

    private static (string Repository, string Number) Split(string workRef)
    {
        var hash = workRef.LastIndexOf('#');
        return (workRef[..hash], workRef[(hash + 1)..]);
    }

    private static (DateTimeOffset UpdatedAt, string Body) View(string gh, string repository, string number)
    {
        var json = JsonNode.Parse(Run(gh, ["issue", "view", number, "-R", repository, "--json", "updatedAt,body"]))!;
        return (DateTimeOffset.Parse((string)json["updatedAt"]!, CultureInfo.InvariantCulture, DateTimeStyles.RoundtripKind), (string)json["body"]!);
    }

    private static string Run(string gh, string[] arguments)
    {
        var start = new ProcessStartInfo(gh) { RedirectStandardOutput = true, RedirectStandardError = true };
        foreach (var argument in arguments)
        {
            start.ArgumentList.Add(argument);
        }

        try
        {
            using var process = Process.Start(start)!;
            var output = process.StandardOutput.ReadToEndAsync();
            var error = process.StandardError.ReadToEnd();
            process.WaitForExit();
            return process.ExitCode == 0
                ? output.GetAwaiter().GetResult()
                : throw new HooksException(ExitCodes.Forge, $"{gh} {string.Join(' ', arguments.Take(3))} failed: {error.Trim()}");
        }
        catch (System.ComponentModel.Win32Exception)
        {
            throw new HooksException(ExitCodes.Forge, $"cannot run {gh}");
        }
    }
}
