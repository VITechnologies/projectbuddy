using ProjectBuddy.SourceConfig;

if (args is ["--help"])
{
	Console.WriteLine("source-config --root CHECKOUT --manifest vipm.toml --build KEY --repository HTTPS_URL --revision COMMIT --schema pipeline-config.schema.json --output NEW_DIRECTORY --ollama-url http://127.0.0.1:PORT --model NAME:TAG --model-digest SHA256 --review-budget POLICY_JSON --review-privacy POLICY_JSON --review-lifecycle POLICY_JSON --review-execution POLICY_JSON");
	return 0;
}
try
{
	var required = new[] { "root", "manifest", "build", "repository", "revision", "schema", "output", "ollama-url", "model", "model-digest", "review-budget", "review-privacy", "review-lifecycle", "review-execution" };
	Configuration.Require(args.Length == required.Length * 2, "All options are required; use --help.");
	var options = new Dictionary<string, string>();
	for (var index = 0; index < args.Length; index += 2)
	{
		Configuration.Require(args[index].StartsWith("--", StringComparison.Ordinal)
			&& required.Contains(args[index][2..]) && !string.IsNullOrWhiteSpace(args[index + 1])
			&& options.TryAdd(args[index][2..], args[index + 1]), "Unknown, duplicate, or empty option.");
	}
	var settings = new Settings(options["root"], options["manifest"], options["build"], options["repository"],
																options["revision"], options["schema"], options["output"], options["ollama-url"], options["model"], options["model-digest"], ReviewBudget.Read(options["review-budget"]), ReviewPrivacy.Read(options["review-privacy"]), ReviewLifecyclePolicy.Read(options["review-lifecycle"]), ReviewExecutionPolicy.Read(options["review-execution"]));
					using var cancellation = new CancellationTokenSource();
					ConsoleCancelEventHandler cancel = (_, value) => { value.Cancel = true; cancellation.Cancel(); };
					Console.CancelKeyPress += cancel;
					try
					{
						var valid = await Generation.RunAsync(settings, (selected, draft) => OllamaReview.RunAsync(selected, draft, cancellation.Token));
						Console.WriteLine(valid ? "Configuration valid; native build remains unproven." : "Draft generated; model review blocks configuration validity.");
						return valid ? 0 : 2;
					}
					finally { Console.CancelKeyPress -= cancel; }
}
catch (Exception error)
{
	Console.Error.WriteLine($"source-config: refused ({error.GetType().Name}); no success; server completion may be unknown.");
	return error is OperationCanceledException ? 130 : 1;
}