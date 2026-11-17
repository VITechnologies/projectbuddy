using System.Text;
using Xunit;

namespace ProjectBuddy.PeerAsk.Tests;

public class InferAskTests
{
    private static readonly string[] Required =
        ["--host", "10.0.0.2", "--user", "peer", "--identity", "/k/id", "--known-hosts", "/k/known"];

    private static readonly string RequestJson =
        $$"""{"schema":"projectbuddy/peer-infer-request@1","model":"{{FakeOllama.Model}}","items":[{"id":"a","prompt":"p"},{"id":"b","prompt":"q","format":"json"}]}""";

    private const string GoodItems =
        """[{"id":"a","answer":"x","evalCount":1,"promptEvalCount":2,"seconds":0.5},{"id":"b","answer":"{}","evalCount":0,"promptEvalCount":0,"seconds":0}]""";

    private static string Envelope(string data) => $$"""{"schema":"projectbuddy/peer-ask@1","tool":"0.4.0","verb":"infer","data":{{data}}}""";

    private static string Answer(string items = GoodItems) =>
        Envelope($$"""{"model":"{{FakeOllama.Model}}","digest":"{{FakeOllama.Digest}}","items":{{items}}}""");

    private static InferRequest Parsed => Infer.ParseRequest(Encoding.UTF8.GetBytes(RequestJson));

    [Fact]
    public void Sends_the_request_file_as_ssh_stdin_and_waits_longer_than_the_peer()
    {
        var path = Fixture.Write(RequestJson);
        string[] args = ["infer", "--request", path, .. Required];
        var options = Ask.Parse(args, "/usr/bin/ssh", _ => true, out var problem)!;
        Assert.Equal("", problem);
        Assert.Equal(["-l", "peer", "--", "10.0.0.2", "infer"], Ask.SshArguments(options).TakeLast(5));
        Assert.Equal(File.ReadAllBytes(path), options.RequestBytes);
        Assert.Equal(["a", "b"], options.Request!.Items.Select(item => item.Id));

        var runner = new FakeRunner().On($"{options.Ssh} {string.Join(' ', Ask.SshArguments(options))}", FakeRunner.Ok(Answer() + "\r\n"));
        var stdout = new StringWriter();
        var stderr = new StringWriter();
        Assert.Equal(0, Ask.Execute(args, runner, "/usr/bin/ssh", _ => true, stdout, stderr));
        Assert.Equal(Answer() + Environment.NewLine, stdout.ToString());
        Assert.Empty(stderr.ToString());
        var call = Assert.Single(runner.Calls);
        Assert.Equal(File.ReadAllBytes(path), call.Stdin);
        Assert.Equal(TimeSpan.FromSeconds(5) + Ask.InferAllowance, call.Timeout);
        Assert.True(Ask.InferAllowance > Infer.Timeout);
    }

    [Fact]
    public void Options_may_come_in_any_order()
    {
        var path = Fixture.Write(RequestJson);
        var options = Ask.Parse(["infer", .. Required, "--connect-timeout", "9", "--request", path], "/usr/bin/ssh", _ => true, out _)!;
        Assert.Equal(9, options.ConnectTimeout);
        Assert.NotNull(options.Request);
    }

    [Fact]
    public void Other_verbs_send_no_stdin()
    {
        var runner = new FakeRunner();
        Ask.Execute(["identity", .. Required], runner, "/usr/bin/ssh", _ => true, new StringWriter(), new StringWriter());
        Assert.Null(Assert.Single(runner.Calls).Stdin);
        Assert.Null(Ask.Parse(["identity", .. Required], "/usr/bin/ssh", _ => true, out _)!.Request);
    }

    public static TheoryData<string[], string> BadInferArguments()
    {
        var good = Fixture.Write(RequestJson);
        var unreadable = $"--request must be a readable file of at most {Infer.MaxRequestBytes} bytes";
        return new TheoryData<string[], string>
        {
            { ["infer", .. Required], "missing --request" },
            { ["infer", "--request"], "bad option '--request'" },
            { ["infer", "--request", good, "--request", good, .. Required], "bad option '--request'" },
            { ["identity", "--request", good, .. Required], "bad option '--request'" },
            { ["diagnose", "disk-full", "--request", good, .. Required], "bad option '--request'" },
            { ["infer", "--request", Fixture.Missing, .. Required], unreadable },
            { ["infer", "--request", Fixture.AnyDirectory, .. Required], unreadable },
            { ["infer", "--request", "", .. Required], unreadable },
            { ["infer", "--request", Fixture.Write(new byte[Infer.MaxRequestBytes + 1]), .. Required], unreadable },
            { ["infer", "--request", Fixture.Write("{"), .. Required], "--request is not a valid projectbuddy/peer-infer-request@1 document: the request is not UTF-8 JSON" },
            {
                ["infer", "--request", Fixture.Write(RequestJson.Replace("\"b\"", "\"a\"")), .. Required],
                "--request is not a valid projectbuddy/peer-infer-request@1 document: item id a is not unique"
            },
            { ["infer", "--request", good, "--host", "-x", "--user", "u", "--identity", "/i", "--known-hosts", "/k"], "--host must be an IP address or a host name" },
        };
    }

    [Theory]
    [MemberData(nameof(BadInferArguments))]
    public void Rejects_a_bad_infer_invocation_without_running_ssh(string[] args, string problem)
    {
        var runner = new FakeRunner();
        var stderr = new StringWriter();
        Assert.Equal(ExitCodes.Usage, Ask.Execute(args, runner, "/usr/bin/ssh", _ => true, new StringWriter(), stderr));
        Assert.StartsWith($"projectbuddy-ask: {problem}{Environment.NewLine}{Program.Usage}", stderr.ToString());
        Assert.Empty(runner.Calls);
    }

    public static TheoryData<string, int> InferAnswers()
    {
        const int bad = ExitCodes.Protocol;
        string Data(string model = $"\"{FakeOllama.Model}\"", string digest = $"\"{FakeOllama.Digest}\"", string items = GoodItems) =>
            Envelope($$"""{"model":{{model}},"digest":{{digest}},"items":{{items}}}""");
        string Item(string id = "\"b\"", string answer = "\"y\"", string evalCount = "1", string promptEvalCount = "1", string seconds = "1.5") =>
            $$"""[{"id":"a","answer":"x","evalCount":1,"promptEvalCount":2,"seconds":0.5},{"id":{{id}},"answer":{{answer}},"evalCount":{{evalCount}},"promptEvalCount":{{promptEvalCount}},"seconds":{{seconds}}}]""";
        return new TheoryData<string, int>
        {
            { Answer(), 0 },
            { Data(items: Item()), 0 },
            { Answer("""[{"id":"b","answer":"","evalCount":0,"promptEvalCount":0,"seconds":0},{"id":"a","answer":"x","evalCount":1,"promptEvalCount":2,"seconds":0.5}]"""), 0 },
            { Answer(GoodItems.Replace("\"seconds\":0.5", "\"seconds\":0.5,\"extra\":true")), 0 },
            { Data(model: "\"other:7b\""), bad },
            { Data(model: "1"), bad },
            { Envelope($$"""{"digest":"{{FakeOllama.Digest}}","items":{{GoodItems}}}"""), bad },
            { Envelope($$"""{"model":"{{FakeOllama.Model}}","items":{{GoodItems}}}"""), bad },
            { Data(digest: "1"), bad },
            { Data(digest: $"\"{FakeOllama.Digest.ToUpperInvariant()}\""), bad },
            { Data(digest: $"\"{FakeOllama.Digest[1..]}\""), bad },
            { Data(digest: $"\"sha256:{FakeOllama.Digest}\""), bad },
            { Envelope($$"""{"model":"{{FakeOllama.Model}}","digest":"{{FakeOllama.Digest}}"}"""), bad },
            { Data(items: "{}"), bad },
            { Data(items: "[]"), bad },
            { Data(items: """[{"id":"a","answer":"x","evalCount":1,"promptEvalCount":2,"seconds":0.5}]"""), bad },
            { Data(items: """[{"id":"a","answer":"x","evalCount":1,"promptEvalCount":2,"seconds":0.5},1]"""), bad },
            { Data(items: """[{"id":"a","answer":"x","evalCount":1,"promptEvalCount":2,"seconds":0.5},{"answer":"y","evalCount":1,"promptEvalCount":1,"seconds":1}]"""), bad },
            { Data(items: Item(id: "1")), bad },
            { Data(items: Item(id: "\"c\"")), bad },
            { Data(items: Item(id: "\"a\"")), bad },
            { Data(items: Item(id: "\"\\ud800\"")), bad },
            { Data(items: GoodItems.Replace("]", ",{\"id\":\"c\",\"answer\":\"z\",\"evalCount\":1,\"promptEvalCount\":1,\"seconds\":1}]")), bad },
            { Data(items: Item(answer: "1")), bad },
            { Data(items: Item(answer: "null")), bad },
            { Data(items: """[{"id":"a","answer":"x","evalCount":1,"promptEvalCount":2,"seconds":0.5},{"id":"b","evalCount":1,"promptEvalCount":1,"seconds":1}]"""), bad },
            { Data(items: Item(evalCount: "\"1\"")), bad },
            { Data(items: Item(evalCount: "-1")), bad },
            { Data(items: Item(evalCount: "1.5")), bad },
            { Data(items: """[{"id":"a","answer":"x","evalCount":1,"promptEvalCount":2,"seconds":0.5},{"id":"b","answer":"y","promptEvalCount":1,"seconds":1}]"""), bad },
            { Data(items: Item(promptEvalCount: "null")), bad },
            { Data(items: """[{"id":"a","answer":"x","evalCount":1,"promptEvalCount":2,"seconds":0.5},{"id":"b","answer":"y","evalCount":1,"seconds":1}]"""), bad },
            { Data(items: Item(seconds: "\"1\"")), bad },
            { Data(items: Item(seconds: "-0.001")), bad },
            { Data(items: """[{"id":"a","answer":"x","evalCount":1,"promptEvalCount":2,"seconds":0.5},{"id":"b","answer":"y","evalCount":1,"promptEvalCount":1}]"""), bad },
            { Answer().Replace("\"verb\":\"infer\"", "\"verb\":\"identity\""), bad },
            { Answer().Replace("projectbuddy/peer-ask@1", "projectbuddy/peer-ask@2"), bad },
        };
    }

    [Theory]
    [MemberData(nameof(InferAnswers))]
    public void Checks_every_requested_id_is_answered_exactly_once(string answer, int expected)
    {
        var (code, message) = Ask.Classify(FakeRunner.Ok(answer), "infer", null, Parsed);
        Assert.Equal(expected, code);
        Assert.Equal(expected == 0 ? "" : "protocol: answer is not a projectbuddy/peer-ask@1 document for this verb", message);
    }

    [Fact]
    public void An_infer_answer_needs_the_request_to_be_checked()
    {
        Assert.Equal(ExitCodes.Protocol, Ask.Classify(FakeRunner.Ok(Answer()), "infer").Code);
    }

    [Fact]
    public void Infer_answers_may_be_up_to_one_mebibyte()
    {
        var padded = Answer() + new string(' ', Infer.MaxOutputBytes - Encoding.UTF8.GetByteCount(Answer()));
        Assert.Equal(0, Ask.Classify(FakeRunner.Ok(padded + "\r\n"), "infer", null, Parsed).Code);
        Assert.Equal((ExitCodes.Protocol, "protocol: answer exceeds the output limit"), Ask.Classify(FakeRunner.Ok(padded + "  \r\n"), "infer", null, Parsed));

        // Every other verb keeps its 16 KiB limit.
        var identity = """{"schema":"projectbuddy/peer-ask@1","tool":"0.4.0","verb":"identity","data":{}}""";
        Assert.Equal(ExitCodes.Protocol, Ask.Classify(FakeRunner.Ok(identity + new string(' ', Serve.MaxOutputBytes)), "identity").Code);
    }

    [Fact]
    public void An_unreadable_string_in_any_answer_is_a_protocol_error()
    {
        var answer = """{"schema":"projectbuddy/peer-ask@1","tool":"0.4.0","verb":"\ud800","data":{}}""";
        Assert.Equal(ExitCodes.Protocol, Ask.Classify(FakeRunner.Ok(answer), "identity").Code);
    }

    [Theory]
    [InlineData(64, ExitCodes.PeerRefused)]
    [InlineData(65, ExitCodes.PeerError)]
    [InlineData(66, ExitCodes.PeerError)]
    [InlineData(255, ExitCodes.SshError)]
    public void Keeps_the_exit_code_classification(int peerExit, int expected)
    {
        Assert.Equal(expected, Ask.Classify(new ProcessResult(true, false, peerExit, "", "x"), "infer", null, Parsed).Code);
    }
}

public class HostInputTests
{
    private static readonly TimeSpan Short = TimeSpan.FromSeconds(20);

    [Fact]
    public void Runner_feeds_standard_input_then_closes_it()
    {
        var result = new SystemProcessRunner().Run("/bin/sh", ["-c", "cat"], Short, Encoding.UTF8.GetBytes("hello\nwörld"));
        Assert.Equal(new ProcessResult(true, false, 0, "hello\nwörld", ""), result);
    }

    [Fact]
    public void Runner_feeds_input_larger_than_a_pipe_while_draining_output()
    {
        var input = Encoding.ASCII.GetBytes(new string('x', (SystemProcessRunner.MaxCapturedChars * 3) / 4));
        var result = new SystemProcessRunner().Run("/bin/sh", ["-c", "cat; printf done >&2"], Short, input);
        Assert.Equal(input.Length, result.Stdout.Length);
        Assert.Equal("done", result.Stderr);
    }

    [Fact]
    public void Runner_survives_a_child_that_ignores_its_input()
    {
        var result = new SystemProcessRunner().Run("/bin/sh", ["-c", "exit 4"], Short, new byte[4 * 1024 * 1024]);
        Assert.Equal(new ProcessResult(true, false, 4, "", ""), result);
    }

    [Fact]
    public void Runner_closes_input_at_once_without_stdin()
    {
        var result = new SystemProcessRunner().Run("/bin/sh", ["-c", "cat; printf eof"], Short);
        Assert.Equal("eof", result.Stdout);
    }

    [Fact]
    public async Task Feeding_a_closed_pipe_is_not_an_error()
    {
        var input = new BrokenPipe();
        await SystemProcessRunner.Feed(input, [1, 2, 3]);
        Assert.True(input.Disposed);
    }

    [Fact]
    public async Task Bounded_reads_stop_one_byte_past_the_limit()
    {
        Assert.Equal(new byte[8], BoundedFile.Read(Fixture.Write(new byte[8]), 8));
        Assert.Null(BoundedFile.Read(Fixture.Write(new byte[9]), 8));
        Assert.Empty(BoundedFile.Read(Fixture.Write([]), 8)!);
        Assert.Throws<FileNotFoundException>(() => BoundedFile.Read(Fixture.Missing, 8));

        Assert.Equal(new byte[8], await BoundedFile.ReadAsync(new MemoryStream(new byte[8]), 8, CancellationToken.None));
        var oversized = new MemoryStream(new byte[100]);
        Assert.Null(await BoundedFile.ReadAsync(oversized, 8, CancellationToken.None));
        Assert.Equal(9, oversized.Position);
    }

    [Fact]
    public void Facts_name_the_infer_paths_of_this_host()
    {
        var facts = new SystemFacts();
        Assert.EndsWith(Path.Combine(".local", "share", "projectbuddy", "peer-ask", "infer", "allowlist.json"), facts.InferAllowlistPath);
        Assert.Equal("http://127.0.0.1:11434/", facts.OllamaUrl);
        Assert.Equal(4, facts.InferConcurrency);
    }

    private sealed class BrokenPipe : MemoryStream
    {
        public bool Disposed { get; private set; }

        public override ValueTask WriteAsync(ReadOnlyMemory<byte> buffer, CancellationToken cancellationToken = default) =>
            ValueTask.FromException(new IOException("Broken pipe"));

        protected override void Dispose(bool disposing)
        {
            Disposed = true;
            base.Dispose(disposing);
        }
    }
}
