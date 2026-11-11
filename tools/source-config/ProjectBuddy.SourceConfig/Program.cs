using ProjectBuddy.SourceConfig;

if (args is ["--help"])
{
    Console.WriteLine("source-config --root CHECKOUT --manifest vipm.toml --build KEY --repository HTTPS_URL --revision COMMIT --schema pipeline-config.schema.json --output NEW_DIRECTORY --ollama-url http://127.0.0.1:PORT --model NAME:TAG --model-digest SHA256 --review-budget POLICY_JSON --review-privacy POLICY_JSON --review-lifecycle POLICY_JSON --review-execution POLICY_JSON [--failure-observation NEW_PRIVATE_JSON]");
    return 0;
}
var observer = new ReviewRefusalObserver();
string? failureObservation = null;
try
{
    var required = new[] { "root", "manifest", "build", "repository", "revision", "schema", "output", "ollama-url", "model", "model-digest", "review-budget", "review-privacy", "review-lifecycle", "review-execution" };
    Configuration.Require(args.Length % 2 == 0 && args.Length >= required.Length * 2
        && args.Length <= (required.Length + 1) * 2, "All required options are needed; use --help.");
    var options = new Dictionary<string, string>();
    for (var index = 0; index < args.Length; index += 2)
    {
        Configuration.Require(args[index].StartsWith("--", StringComparison.Ordinal)
                        && (required.Contains(args[index][2..]) || args[index] == "--failure-observation") && !string.IsNullOrWhiteSpace(args[index + 1])
            && options.TryAdd(args[index][2..], args[index + 1]), "Unknown, duplicate, or empty option.");
    }
    Configuration.Require(required.All(options.ContainsKey), "All required options are needed; use --help.");
    var requestedObservation = options.GetValueOrDefault("failure-observation");
    if (requestedObservation is not null)
    {
        var inputPaths = new[] { options["schema"], options["review-budget"], options["review-privacy"],
                options["review-lifecycle"], options["review-execution"] };
        var comparison = OperatingSystem.IsWindows() ? StringComparison.OrdinalIgnoreCase : StringComparison.Ordinal;
        Configuration.Require(!Generation.Inside(options["output"], requestedObservation)
            && !Generation.Inside(options["root"], requestedObservation)
            && !inputPaths.Any(path => string.Equals(Path.GetFullPath(path), Path.GetFullPath(requestedObservation), comparison)),
            "Refusal output must be separate from input/configuration paths.");
        failureObservation = requestedObservation;
    }
    var settings = new Settings(options["root"], options["manifest"], options["build"], options["repository"],
                                                                options["revision"], options["schema"], options["output"], options["ollama-url"], options["model"], options["model-digest"], ReviewBudget.Read(options["review-budget"]), ReviewPrivacy.Read(options["review-privacy"]), ReviewLifecyclePolicy.Read(options["review-lifecycle"]), ReviewExecutionPolicy.Read(options["review-execution"]));
    observer.ConfigureLimits(settings.Lifecycle);
    using var cancellation = new CancellationTokenSource();
    ConsoleCancelEventHandler cancel = (_, value) => { value.Cancel = true; cancellation.Cancel(); };
    Console.CancelKeyPress += cancel;
    try
    {
        var valid = await Generation.RunAsync(settings, (selected, draft) => OllamaReview.RunAsync(selected, draft, cancellation.Token, observer), observer);
        Console.WriteLine(valid ? "Configuration valid; native build remains unproven." : "Draft generated; model review blocks configuration validity.");
        return valid ? 0 : 2;
    }
    finally { Console.CancelKeyPress -= cancel; }
}
catch (Exception error)
{
    Console.Error.WriteLine($"source-config: refused ({error.GetType().Name}); no success; server completion may be unknown.");
    var exitCode = error is OperationCanceledException ? 130 : 1;
    if (failureObservation is not null)
    {
        try { Generation.WritePrivateRefusal(failureObservation, observer.Snapshot(error, exitCode)); }
        catch { Console.Error.WriteLine("source-config: non-success observation unavailable; original outcome preserved."); }
    }
    return exitCode;
}