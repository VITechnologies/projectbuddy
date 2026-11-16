using System.Text.Json;
using System.Text.RegularExpressions;

namespace ProjectBuddy.SourceConfig;

public sealed record ReviewRefusalObservation(string Schema, bool Complete, string Outcome, int ExitCode,
    string FailureKind, string Phase, ReviewLifecyclePolicy? Limits, int RequestCount, int RetryCount,
    int ReservedUnknownRequestCount, double? ConsumedSeconds, bool RequestMayHaveBeenInFlight,
    string? LastRequestSha256, string ServerCompletion)
{
    internal static readonly string[] Phases = ["input-validation", "orchestration", "mcp-context", "capture-validation",
        "review-preflight", "checkpoint-validation", "identity-preflight", "model-metadata", "inference", "identity-postflight", "finalization"];

    internal static ReviewRefusalObservation Create(Exception error, int exitCode, string phase, ReviewLifecyclePolicy? limits = null,
        int requests = 0, int retries = 0, double? consumed = 0, bool inFlight = false, string? requestHash = null)
    {
        var kind = error switch
        {
            OperationCanceledException => "cancelled", TimeoutException => "deadline", HttpRequestException => "transport",
            IOException or UnauthorizedAccessException => "input", JsonException or InvalidDataException or ArgumentException => "validation",
            _ => "internal"
        };
        var record = new ReviewRefusalObservation("projectbuddy/assisted-review-refusal@1", false,
            kind == "cancelled" ? "cancelled" : "refused", exitCode, kind, phase, limits, requests, retries, 0,
            consumed is not null && double.IsFinite(consumed.Value) ? Math.Max(0, consumed.Value) : null,
            inFlight, requestHash, requests > 0 ? "unknown" : "not-started");
        record.Validate();
        return record;
    }

    public void Validate()
    {
        Configuration.Require(Schema == "projectbuddy/assisted-review-refusal@1" && !Complete
            && Outcome is "refused" or "cancelled" && ExitCode is 1 or 2 or 130
            && FailureKind is "deadline" or "cancelled" or "transport" or "validation" or "input" or "internal"
            && Phases.Contains(Phase) && RequestCount is >= 0 and <= 256 && RetryCount is >= 0 and <= 256
            && ReservedUnknownRequestCount is >= 0 and <= 256
            && (ConsumedSeconds is null || double.IsFinite(ConsumedSeconds.Value) && ConsumedSeconds >= 0)
            && (LastRequestSha256 is null || Regex.IsMatch(LastRequestSha256, "\\A[a-f0-9]{64}\\z"))
            && ServerCompletion is "unknown" or "not-started", "Invalid minimized non-success observation.");
        Limits?.Validate();
    }
}

public sealed class ReviewRefusalObserver
{
    private ReviewExecution? execution;
    private string phase = "input-validation";
    private ReviewLifecyclePolicy? limits;

    public void ConfigureLimits(ReviewLifecyclePolicy policy)
    {
        policy.Validate();
        limits = policy;
    }

    public void Begin(string value)
    {
        execution = null;
        limits = null;
        Mark(value);
    }

    public void Mark(string value)
    {
        Configuration.Require(ReviewRefusalObservation.Phases.Contains(value), "Unsupported host observation phase.");
        phase = value;
        execution?.Mark(value);
    }

    internal void Bind(ReviewExecution value) => execution = value;

    public ReviewRefusalObservation Snapshot(Exception error, int exitCode) => execution?.Refusal(error, exitCode)
        ?? ReviewRefusalObservation.Create(error, exitCode, phase, limits);
}