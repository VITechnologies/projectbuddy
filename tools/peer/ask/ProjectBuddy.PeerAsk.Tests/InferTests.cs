using System.Diagnostics;
using System.Net;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using Xunit;

namespace ProjectBuddy.PeerAsk.Tests;

public class InferTests
{
    private const string Schema = "projectbuddy/peer-infer-request@1";

    private static readonly string Allowlisted =
        $$"""{"schema":"projectbuddy/peer-infer-allowlist@1","models":[{"name":"{{FakeOllama.Model}}","digest":"{{FakeOllama.Digest}}"}]}""";

    private static string AllowlistedOn(string ports) =>
        $$"""{"schema":"projectbuddy/peer-infer-allowlist@2","models":[{"name":"other:1","digest":"{{new string('b', 64)}}"},{"name":"{{FakeOllama.Model}}","digest":"{{FakeOllama.Digest}}"{{ports}}}]}""";

    private static readonly InferLimits Fast = new(TimeSpan.FromSeconds(10), TimeSpan.FromSeconds(20));

    private static string Request(string items = """[{"id":"a","prompt":"Is the sky blue?"}]""", string? options = null, string model = FakeOllama.Model) =>
        $$"""{"schema":"{{Schema}}","model":"{{model}}",{{(options is null ? "" : $"\"options\":{options},")}}"items":{{items}}}""";

    private static string Items(int count, Func<int, string>? prompt = null) =>
        "[" + string.Join(",", Enumerable.Range(0, count).Select(index => $$"""{"id":"item-{{index}}","prompt":"{{(prompt ?? (i => $"prompt {i}"))(index)}}"}""")) + "]";

    private static FakeFacts Facts(string? allowlist = null, int? concurrency = null)
    {
        var facts = new FakeFacts { InferAllowlistPath = Fixture.Write(allowlist ?? Allowlisted) };
        foreach (var port in Infer.AllowedPorts)
        {
            facts.Workers[port] = concurrency ?? facts.Workers[port];
        }

        return facts;
    }

    private static (int Code, string Out, string Err) Run(string request, FakeOllama ollama, FakeFacts? facts = null, InferLimits? limits = null) =>
        Run(new MemoryStream(Encoding.UTF8.GetBytes(request)), ollama, facts, limits);

    private static (int Code, string Out, string Err) Run(Stream stdin, FakeOllama ollama, FakeFacts? facts = null, InferLimits? limits = null)
    {
        var stdout = new StringWriter();
        var stderr = new StringWriter();
        var code = Serve.Execute("infer", new FakeRunner(), facts ?? Facts(), stdin, ollama, stdout, stderr, limits ?? Fast);
        return (code, stdout.ToString(), stderr.ToString());
    }

    private static JsonElement Data(string output)
    {
        using var document = JsonDocument.Parse(output);
        var root = document.RootElement;
        Assert.Equal(Versions.Schema, root.GetProperty("schema").GetString());
        Assert.Equal(Versions.Tool, root.GetProperty("tool").GetString());
        Assert.Equal("infer", root.GetProperty("verb").GetString());
        return root.GetProperty("data").Clone();
    }

    private static void AssertRefused((int Code, string Out, string Err) result)
    {
        Assert.Equal(ExitCodes.Refused, result.Code);
        Assert.Empty(result.Out);
        Assert.Equal(Serve.RefusedMessage + Environment.NewLine, result.Err);
    }

    private static void AssertInternal((int Code, string Out, string Err) result)
    {
        Assert.Equal(ExitCodes.Internal, result.Code);
        Assert.Empty(result.Out);
        Assert.Equal(Serve.InternalMessage + Environment.NewLine, result.Err);
    }

    [Fact]
    public void Runs_every_prompt_and_answers_in_request_order()
    {
        var ollama = new FakeOllama
        {
            // The first item finishes last, so the answer order cannot follow completion order.
            Chat = async (body, _, token) =>
            {
                var prompt = FakeOllama.Prompt(body);
                await Task.Delay(prompt == "first" ? 200 : 0, token);
                return FakeOllama.Ok(FakeOllama.Reply($"answer: {prompt}", prompt.Length, 3));
            },
        };
        var request = Request(
            """[{"id":"first","prompt":"first","format":"json"},{"id":"b.2:x_y-z","prompt":"second","format":null},{"id":"C","prompt":"thïrd"}]""",
            """{"temperature":0.5,"seed":42,"num_ctx":4096}""");
        var (code, output, error) = Run(request, ollama);
        Assert.Equal(0, code);
        Assert.Empty(error);
        var data = Data(output);
        Assert.Equal(FakeOllama.Model, data.GetProperty("model").GetString());
        Assert.Equal(FakeOllama.Digest, data.GetProperty("digest").GetString());
        var items = data.GetProperty("items").EnumerateArray().ToList();
        Assert.Equal(["first", "b.2:x_y-z", "C"], items.Select(item => item.GetProperty("id").GetString()));
        Assert.Equal(["answer: first", "answer: second", "answer: thïrd"], items.Select(item => item.GetProperty("answer").GetString()));
        Assert.Equal([3, 3, 3], items.Select(item => item.GetProperty("evalCount").GetInt32()));
        Assert.Equal([5, 6, 5], items.Select(item => item.GetProperty("promptEvalCount").GetInt32()));
        Assert.All(items, item => Assert.Equal(1.235, item.GetProperty("seconds").GetDouble()));
        Assert.Equal(["id", "answer", "evalCount", "promptEvalCount", "seconds", "port"], items[0].EnumerateObject().Select(property => property.Name));
        Assert.All(items, item => Assert.Equal(11434, item.GetProperty("port").GetInt32()));
        Assert.Equal(["model", "digest", "items"], data.EnumerateObject().Select(property => property.Name));

        // Each chat carries one user message and only the options the request set: no system prompt, tools or images.
        var chats = ollama.Chats.OrderBy(FakeOllama.Prompt, StringComparer.Ordinal).ToList();
        Assert.Equal(3, chats.Count);
        foreach (var chat in chats)
        {
            Assert.Equal(FakeOllama.Model, chat["model"]!.GetValue<string>());
            Assert.False(chat["stream"]!.GetValue<bool>());
            var message = Assert.Single(chat["messages"]!.AsArray())!.AsObject();
            Assert.Equal(["role", "content"], message.Select(pair => pair.Key));
            Assert.Equal("user", message["role"]!.GetValue<string>());
            Assert.Equal("""{"temperature":0.5,"seed":42,"num_ctx":4096,"num_predict":4096}""", chat["options"]!.ToJsonString());
        }

        Assert.Equal(["model", "stream", "messages", "options", "format"], chats[0].Select(pair => pair.Key));
        Assert.All(chats, chat => Assert.False(chat.ContainsKey("think")));
        Assert.Equal("json", chats[0]["format"]!.GetValue<string>());
        Assert.False(chats[1].ContainsKey("format"));
        Assert.False(chats[2].ContainsKey("format"));
        Assert.Equal("/api/tags", ollama.Requests[0].Path);
    }

    [Fact]
    public void Sends_no_options_the_request_left_out()
    {
        // Only the peer's generation cap is always sent.
        var ollama = new FakeOllama();
        Assert.Equal(0, Run(Request(), ollama).Code);
        var only = Assert.Single(ollama.Chats);
        Assert.Equal(["model", "stream", "messages", "options"], only.Select(pair => pair.Key));
        Assert.Equal("""{"num_predict":4096}""", only["options"]!.ToJsonString());

        foreach (var (options, sent) in new[]
                 {
                     ("""{}""", """{"num_predict":4096}"""),
                     ("""{"temperature":2}""", """{"temperature":2,"num_predict":4096}"""),
                     ("""{"seed":0}""", """{"seed":0,"num_predict":4096}"""),
                     ("""{"num_ctx":512}""", """{"num_ctx":512,"num_predict":4096}"""),
                 })
        {
            var each = new FakeOllama();
            Assert.Equal(0, Run(Request(options: options), each).Code);
            Assert.Equal(sent, Assert.Single(each.Chats)["options"]!.ToJsonString());
        }
    }

    [Fact]
    public void Accepts_every_limit_exactly()
    {
        var prompt = new string('p', Infer.MaxPromptBytes);
        var request = Request(Items(Infer.MaxItems), """{"temperature":0,"seed":2147483647,"num_ctx":32768,"think":true}""");
        var parsed = PeerAsk.Infer.ParseRequest(Encoding.UTF8.GetBytes(request));
        Assert.Equal(Infer.MaxItems, parsed.Items.Count);
        Assert.Equal(new InferOptions(0, int.MaxValue, Infer.MaxNumCtx, true), parsed.Options);

        var longest = Request($$"""[{"id":"{{new string('i', 64)}}","prompt":"{{prompt}}"}]""", model: new string('m', Infer.MaxModelChars));
        var item = Assert.Single(PeerAsk.Infer.ParseRequest(Encoding.UTF8.GetBytes(longest)).Items);
        Assert.Equal(Infer.MaxPromptBytes, item.Prompt.Length);

        var ollama = new FakeOllama();
        var (code, output, _) = Run(request, ollama);
        Assert.Equal(0, code);
        Assert.Equal(Infer.MaxItems, Data(output).GetProperty("items").GetArrayLength());
    }

    public static TheoryData<string> RefusedRequests()
    {
        var valid = """[{"id":"a","prompt":"p"}]""";
        return new TheoryData<string>
        {
            "",
            "not json",
            "[]",
            "null",
            "\"text\"",
            Request() + " x",
            Request().Replace("{\"schema\"", "{\"extra\":1,\"schema\""),
            $$"""{"model":"{{FakeOllama.Model}}","items":{{valid}}}""",
            Request().Replace("@1", "@2"),
            Request().Replace($"\"{Schema}\"", "1"),
            Request().Replace("\"model\"", "\"schema\""),
            $$"""{"schema":"{{Schema}}","items":{{valid}}}""",
            Request().Replace($"\"{FakeOllama.Model}\"", "1"),
            Request(model: ""),
            Request(model: new string('m', Infer.MaxModelChars + 1)),
            Request(options: "null"),
            Request(options: "[]"),
            Request(options: """{"top_k":1}"""),
            Request(options: """{"temperature":0,"temperature":1}"""),
            Request(options: """{"temperature":-0.1}"""),
            Request(options: """{"temperature":2.01}"""),
            Request(options: """{"temperature":"0"}"""),
            Request(options: """{"temperature":true}"""),
            Request(options: """{"temperature":1e400}"""),
            Request(options: """{"temperature":[[[[[[[[0]]]]]]]]}"""),
            Request(options: """{"seed":-1}"""),
            Request(options: """{"seed":2147483648}"""),
            Request(options: """{"seed":1.5}"""),
            Request(options: """{"seed":"1"}"""),
            Request(options: """{"num_ctx":511}"""),
            Request(options: """{"num_ctx":32769}"""),
            Request(options: """{"num_ctx":1024.0}"""),
            Request(options: """{"num_ctx":"2048"}"""),
            Request(options: """{"num_predict":8192}"""),
            Request(options: """{"num_predict":16}"""),
            Request(options: """{"think":"true"}"""),
            Request(options: """{"think":1}"""),
            Request(options: """{"think":null}"""),
            Request(options: """{"think":"high"}"""),
            $$"""{"schema":"{{Schema}}","model":"{{FakeOllama.Model}}"}""",
            Request("{}"),
            Request("null"),
            Request("[]"),
            Request(Items(Infer.MaxItems + 1)),
            Request("[1]"),
            Request("[[]]"),
            Request("""[{"id":"a","prompt":"p","system":"s"}]"""),
            Request("""[{"id":"a","prompt":"p","images":["x"]}]"""),
            Request("""[{"prompt":"p"}]"""),
            Request("""[{"id":"a"}]"""),
            Request("""[{"id":"","prompt":"p"}]"""),
            Request("""[{"id":"a b","prompt":"p"}]"""),
            Request("""[{"id":"a/b","prompt":"p"}]"""),
            Request("""[{"id":"-x\n","prompt":"p"}]"""),
            Request($$"""[{"id":"{{new string('i', 65)}}","prompt":"p"}]"""),
            Request("""[{"id":1,"prompt":"p"}]"""),
            Request("""[{"id":"a","prompt":"p"},{"id":"a","prompt":"q"}]"""),
            Request("""[{"id":"a","prompt":1}]"""),
            Request("""[{"id":"a","prompt":""}]"""),
            Request("""[{"id":"a","prompt":"\ud800"}]"""),
            Request($$"""[{"id":"a","prompt":"{{new string('p', Infer.MaxPromptBytes + 1)}}"}]"""),
            Request($$"""[{"id":"a","prompt":"{{string.Concat(Enumerable.Repeat("é", (Infer.MaxPromptBytes / 2) + 1))}}"}]"""),
            Request("""[{"id":"a","prompt":"p","format":"xml"}]"""),
            Request("""[{"id":"a","prompt":"p","format":"JSON"}]"""),
            Request("""[{"id":"a","prompt":"p","format":1}]"""),
            Request("""[{"id":"a","prompt":"p","format":{"type":"object"}}]"""),
        };
    }

    [Theory]
    [MemberData(nameof(RefusedRequests))]
    public void Refuses_a_malformed_request_before_asking_ollama(string request)
    {
        var ollama = new FakeOllama();
        AssertRefused(Run(request, ollama));
        Assert.Empty(ollama.Requests);
        Assert.Throws<RefusedException>(() => PeerAsk.Infer.ParseRequest(Encoding.UTF8.GetBytes(request)));
    }

    [Theory]
    [InlineData(new byte[] { 0xEF, 0xBB, 0xBF }, false)]
    [InlineData(new byte[] { 0xFF }, false)]
    [InlineData(new byte[] { 0xFF }, true)]
    [InlineData(new byte[] { 0xC3 }, true)]
    public void Refuses_a_request_that_is_not_plain_utf8(byte[] bytes, bool insidePrompt)
    {
        // A byte-order mark before the request; an invalid byte or a truncated sequence before it or inside a prompt.
        var valid = Encoding.UTF8.GetBytes(Request());
        var at = insidePrompt ? valid.Length - 10 : 0;
        byte[] request = [.. valid[..at], .. bytes, .. valid[at..]];
        var ollama = new FakeOllama();
        AssertRefused(Run(new MemoryStream(request), ollama));
        Assert.Empty(ollama.Requests);
        Assert.Equal("the request is not UTF-8 JSON", Assert.Throws<RefusedException>(() => PeerAsk.Infer.ParseRequest(request)).Message);
    }

    [Fact]
    public void Refuses_a_request_over_one_mebibyte_without_reading_all_of_it()
    {
        var oversized = new byte[(Infer.MaxRequestBytes * 2) + 7];
        var stdin = new MemoryStream(oversized);
        var ollama = new FakeOllama();
        AssertRefused(Run(stdin, ollama));
        Assert.Equal(Infer.MaxRequestBytes + 1, stdin.Position);
        Assert.Empty(ollama.Requests);

        var refusal = Assert.Throws<RefusedException>(() => PeerAsk.Infer.ParseRequest(new byte[Infer.MaxRequestBytes + 1]));
        Assert.Equal($"the request is over {Infer.MaxRequestBytes} bytes", refusal.Message);
    }

    [Fact]
    public void Gives_the_client_a_reason_for_each_refusal()
    {
        string Reason(string request) => Assert.Throws<RefusedException>(() => PeerAsk.Infer.ParseRequest(Encoding.UTF8.GetBytes(request))).Message;
        Assert.Equal("the request needs [schema, model, items] and may have [options], nothing else", Reason(Request().Replace("{\"schema\"", "{\"x\":1,\"schema\"")));
        Assert.Equal($"schema must be {Schema}", Reason(Request().Replace("@1", "@2")));
        Assert.Equal("options needs [] and may have [temperature, seed, num_ctx, think], nothing else", Reason(Request(options: """{"x":1}""")));
        Assert.Equal("options.think must be true or false", Reason(Request(options: """{"think":"yes"}""")));
        Assert.Equal("options.num_ctx must be an integer from 512 to 32768", Reason(Request(options: """{"num_ctx":1}""")));
        Assert.Equal("options.temperature must be a number from 0 to 2", Reason(Request(options: """{"temperature":3}""")));
        Assert.Equal("item id a is not unique", Reason(Request("""[{"id":"a","prompt":"p"},{"id":"a","prompt":"q"}]""")));
        Assert.Equal("the format of a must be \"json\" or null", Reason(Request("""[{"id":"a","prompt":"p","format":"yaml"}]""")));
        Assert.Equal("the request must be a JSON object", Reason("[]"));
    }

    [Theory]
    [InlineData("other:7b")]
    [InlineData("bespoke-minicheck:latest")]
    [InlineData("BESPOKE-MINICHECK:7B")]
    public void Refuses_a_model_off_the_allowlist(string model)
    {
        var ollama = new FakeOllama { Tags = _ => FakeOllama.Ok(FakeOllama.TagList((FakeOllama.Model, FakeOllama.Digest), (model, FakeOllama.Digest))) };
        AssertRefused(Run(Request(model: model), ollama));
        Assert.Empty(ollama.Requests);
    }

    [Theory]
    [InlineData("0000000000000000000000000000000000000000000000000000000000000000")]
    [InlineData("845DBDA0EA48ED749CAAFD9E6037047AA19ACFCFD82E704D7CA97D631A0B697E")]
    [InlineData("sha256:845dbda0ea48ed749caafd9e6037047aa19acfcfd82e704d7ca97d631a0b697e")]
    public void Refuses_a_model_whose_installed_digest_is_not_the_pinned_one(string installed)
    {
        var ollama = new FakeOllama { Tags = _ => FakeOllama.Ok(FakeOllama.TagList((FakeOllama.Model, installed))) };
        AssertRefused(Run(Request(), ollama));
        Assert.Equal(["/api/tags"], ollama.Requests.Select(request => request.Path));
    }

    [Fact]
    public void Refuses_a_model_that_is_not_installed_and_never_pulls_it()
    {
        var ollama = new FakeOllama { Tags = _ => FakeOllama.Ok(FakeOllama.TagList(("qwen2.5:7b", FakeOllama.Digest))) };
        AssertRefused(Run(Request(), ollama));
        Assert.Equal(["/api/tags"], ollama.Requests.Select(request => request.Path));
    }

    public static TheoryData<string> UnusableAllowlists()
    {
        static string Models(string models) => $$"""{"schema":"projectbuddy/peer-infer-allowlist@1","models":{{models}}}""";
        var entry = $$"""{"name":"{{FakeOllama.Model}}","digest":"{{FakeOllama.Digest}}"}""";
        return new TheoryData<string>
        {
            "",
            "not json",
            "[]",
            Models($"[{entry}]").Replace("{\"schema\"", "{\"extra\":1,\"schema\""),
            Models($"[{entry}]").Replace("@1", "@3"),
            Models($"[{entry}]").Replace("@1", "@0"),
            Models($$"""[{"name":"{{FakeOllama.Model}}","digest":"{{FakeOllama.Digest}}","ports":[11434]}]"""),
            $"{{\"models\":[{entry}]}}",
            Models("{}"),
            Models("[]"),
            Models("[" + string.Join(",", Enumerable.Range(0, Infer.MaxAllowlistModels + 1).Select(index => entry.Replace(":7b", $":{index}")) ) + "]"),
            Models("[1]"),
            Models($$"""[{"name":"{{FakeOllama.Model}}","digest":"{{FakeOllama.Digest}}","pull":true}]"""),
            Models($$"""[{"name":"{{FakeOllama.Model}}"}]"""),
            Models($$"""[{"name":"Bespoke-MiniCheck:7B","digest":"{{FakeOllama.Digest}}"}]"""),
            Models($$"""[{"name":"bespoke-minicheck","digest":"{{FakeOllama.Digest}}"}]"""),
            Models($$"""[{"name":1,"digest":"{{FakeOllama.Digest}}"}]"""),
            Models($$"""[{"name":"{{FakeOllama.Model}}","digest":"sha256:{{FakeOllama.Digest}}"}]"""),
            Models($$"""[{"name":"{{FakeOllama.Model}}","digest":null}]"""),
            Models($"[{entry},{entry}]"),
            Models($"[{entry}]") + new string(' ', Infer.MaxAllowlistBytes),
        };
    }

    public static TheoryData<string> UnusablePorts() => new()
    {
        "[]",
        "null",
        "11434",
        "{}",
        "[8080]",
        "[11436]",
        "[0]",
        "[11434,11434]",
        "[11435,11434,11435]",
        "[\"11434\"]",
        "[11434.0]",
        "[11434.5]",
        "[1e400]",
        "[null]",
        "[[11434]]",
    };

    [Theory]
    [MemberData(nameof(UnusablePorts))]
    public void Refuses_ports_outside_the_fixed_set(string ports)
    {
        var ollama = new FakeOllama();
        AssertRefused(Run(Request(), ollama, Facts(AllowlistedOn($",\"ports\":{ports}"))));
        Assert.Empty(ollama.Requests);
    }

    [Theory]
    [MemberData(nameof(UnusableAllowlists))]
    public void Refuses_when_the_allowlist_is_unusable(string allowlist)
    {
        var ollama = new FakeOllama();
        AssertRefused(Run(Request(), ollama, Facts(allowlist)));
        Assert.Empty(ollama.Requests);
    }

    [Fact]
    public void Refuses_when_the_allowlist_is_missing_or_unreadable()
    {
        foreach (var path in new[] { Fixture.Missing, Fixture.AnyDirectory })
        {
            var ollama = new FakeOllama();
            AssertRefused(Run(Request(), ollama, new FakeFacts { InferAllowlistPath = path }));
            Assert.Empty(ollama.Requests);
        }
    }

    [Fact]
    public void Accepts_an_allowlist_at_its_size_limit_with_several_models()
    {
        var models = string.Join(",", Enumerable.Range(0, Infer.MaxAllowlistModels - 1).Select(index => $$"""{"name":"model-{{index}}:7b","digest":"{{new string('a', 64)}}"}"""));
        var allowlist = $$"""{"schema":"projectbuddy/peer-infer-allowlist@1","models":[{{models}},{"name":"{{FakeOllama.Model}}","digest":"{{FakeOllama.Digest}}"}]}""";
        allowlist += new string(' ', Infer.MaxAllowlistBytes - Encoding.UTF8.GetByteCount(allowlist));
        Assert.Equal(Infer.MaxAllowlistBytes, Encoding.UTF8.GetByteCount(allowlist));
        Assert.Equal(0, Run(Request(), new FakeOllama(), Facts(allowlist)).Code);
        Assert.Equal(Infer.MaxAllowlistModels, PeerAsk.Infer.ParseAllowlist(Encoding.UTF8.GetBytes(allowlist)).Count);
    }

    [Theory]
    [InlineData(HttpStatusCode.InternalServerError)]
    [InlineData(HttpStatusCode.NotFound)]
    public void An_ollama_that_cannot_list_its_models_is_an_internal_error(HttpStatusCode status)
    {
        var ollama = new FakeOllama { Tags = _ => new HttpResponseMessage(status) { Content = new StringContent("secret detail D:\\customer") } };
        AssertInternal(Run(Request(), ollama));
        Assert.Empty(ollama.Chats);
    }

    public static TheoryData<string> FailedChats() => new()
    {
        "status",
        "not json",
        """{"error":"model runner crashed at D:\\customer\\x"}""",
        """{"message":"text"}""",
        """{"message":{"role":"assistant","content":"\ud800"}}""",
    };

    [Theory]
    [MemberData(nameof(FailedChats))]
    public void A_failed_prompt_fails_the_whole_call_with_no_partial_answer(string reply)
    {
        var ollama = new FakeOllama
        {
            Chat = (body, _, _) => Task.FromResult(FakeOllama.Prompt(body) != "prompt 2" ? FakeOllama.Ok(FakeOllama.Reply("fine"))
                : reply == "status" ? new HttpResponseMessage(HttpStatusCode.InternalServerError) { Content = new StringContent("secret") }
                : new HttpResponseMessage(HttpStatusCode.OK) { Content = new StringContent(reply, Encoding.UTF8, "application/json") }),
        };
        var result = Run(Request(Items(5)), ollama);
        AssertInternal(result);
        Assert.DoesNotContain("fine", result.Out + result.Err);
    }

    [Fact]
    public void A_failed_prompt_stops_the_batch_at_once()
    {
        var ollama = new FakeOllama
        {
            Chat = async (body, _, token) =>
            {
                if (FakeOllama.Prompt(body) == "prompt 0")
                {
                    return new HttpResponseMessage(HttpStatusCode.InternalServerError);
                }

                await Task.Delay(TimeSpan.FromSeconds(30), token);
                return FakeOllama.Ok(FakeOllama.Reply("late"));
            },
        };
        var watch = Stopwatch.StartNew();
        AssertInternal(Run(Request(Items(20)), ollama, Facts(concurrency: 2), new InferLimits(TimeSpan.FromSeconds(60), TimeSpan.FromSeconds(60))));
        Assert.True(watch.Elapsed < TimeSpan.FromSeconds(15), $"took {watch.Elapsed}");

        // No prompt waiting for a slot starts after the failure, and the one still running is cancelled.
        Assert.True(ollama.Chats.Count() < 20);
        Assert.True(SpinWait.SpinUntil(() => ollama.Cancelled >= 1, TimeSpan.FromSeconds(10)));
    }

    [Theory]
    [InlineData(1, 6, 1)]
    [InlineData(3, 12, 3)]
    [InlineData(4, 4, 4)]
    [InlineData(0, 3, 1)]
    [InlineData(-5, 2, 1)]
    [InlineData(1000, 64, 64)]
    public void Runs_at_most_the_configured_number_of_prompts_at_once(int concurrency, int items, int bound)
    {
        // Each prompt holds its slot until every allowed slot is busy (or 2 s pass), so a looser bound would show.
        var ollama = new FakeOllama();
        ollama.Chat = async (body, _, token) =>
        {
            var deadline = Stopwatch.StartNew();
            while (ollama.InFlight < bound && deadline.Elapsed < TimeSpan.FromSeconds(2))
            {
                await Task.Delay(5, token);
            }

            await Task.Delay(20, token);
            return FakeOllama.Ok(FakeOllama.Reply("ok"));
        };
        var (code, output, _) = Run(Request(Items(items)), ollama, Facts(concurrency: concurrency));
        Assert.Equal(0, code);
        Assert.Equal(items, Data(output).GetProperty("items").GetArrayLength());
        Assert.Equal(bound, ollama.MaxInFlight);
    }

    [Fact]
    public void A_prompt_over_its_timeout_fails_the_call()
    {
        var ollama = new FakeOllama
        {
            Chat = async (body, _, token) =>
            {
                await Task.Delay(FakeOllama.Prompt(body) == "prompt 1" ? Timeout.InfiniteTimeSpan : TimeSpan.Zero, token);
                return FakeOllama.Ok(FakeOllama.Reply("ok"));
            },
        };
        var watch = Stopwatch.StartNew();
        AssertInternal(Run(Request(Items(3)), ollama, limits: new InferLimits(TimeSpan.FromMilliseconds(300), TimeSpan.FromSeconds(30))));
        Assert.True(watch.Elapsed < TimeSpan.FromSeconds(10), $"took {watch.Elapsed}");
        Assert.Equal(1, ollama.Cancelled);
    }

    [Fact]
    public void A_batch_over_the_whole_call_timeout_fails_and_cancels_what_is_running()
    {
        var ollama = new FakeOllama
        {
            Chat = async (body, _, token) =>
            {
                await Task.Delay(TimeSpan.FromSeconds(1), token);
                return FakeOllama.Ok(FakeOllama.Reply("ok"));
            },
        };
        var watch = Stopwatch.StartNew();
        AssertInternal(Run(Request(Items(8)), ollama, Facts(concurrency: 1), new InferLimits(TimeSpan.FromSeconds(5), TimeSpan.FromMilliseconds(1500))));
        Assert.True(watch.Elapsed < TimeSpan.FromSeconds(5), $"took {watch.Elapsed}");
        Assert.True(SpinWait.SpinUntil(() => ollama.Cancelled == 1, TimeSpan.FromSeconds(10)));
        Thread.Sleep(1500);
        Assert.InRange(ollama.Chats.Count(), 2, 3);
    }

    [Fact]
    public void The_whole_call_timeout_covers_reading_the_request()
    {
        var ollama = new FakeOllama();
        var watch = Stopwatch.StartNew();
        AssertInternal(Run(new StallingStream(), ollama, limits: new InferLimits(TimeSpan.FromSeconds(5), TimeSpan.FromMilliseconds(300))));
        Assert.True(watch.Elapsed < TimeSpan.FromSeconds(5), $"took {watch.Elapsed}");
        Assert.Empty(ollama.Requests);
    }

    [Fact]
    public void Answers_may_exceed_the_other_verbs_limit_up_to_one_mebibyte()
    {
        var answer = new string('a', 15 * 1024);
        var ollama = new FakeOllama { Chat = (_, _, _) => Task.FromResult(FakeOllama.Ok(FakeOllama.Reply(answer))) };
        var (code, output, _) = Run(Request(Items(Infer.MaxItems)), ollama);
        Assert.Equal(0, code);
        Assert.InRange(Encoding.UTF8.GetByteCount(output), Serve.MaxOutputBytes, Infer.MaxOutputBytes);
    }

    [Fact]
    public void Answers_over_one_mebibyte_together_are_refused_as_the_output_limit()
    {
        // Every answer is exactly at its own limit; only the envelope is over.
        var answer = new string('a', Infer.MaxAnswerBytes);
        var ollama = new FakeOllama { Chat = (_, _, _) => Task.FromResult(FakeOllama.Ok(FakeOllama.Reply(answer))) };
        var (code, output, error) = Run(Request(Items(Infer.MaxItems)), ollama);
        Assert.Equal(ExitCodes.OutputLimit, code);
        Assert.Empty(output);
        Assert.Equal(Serve.OutputLimitMessage + Environment.NewLine, error);
    }

    [Theory]
    [InlineData(Infer.MaxAnswerBytes, 'a', 0)]
    [InlineData(Infer.MaxAnswerBytes / 2, 'é', 0)]
    [InlineData(Infer.MaxAnswerBytes + 1, 'a', ExitCodes.OutputLimit)]
    [InlineData((Infer.MaxAnswerBytes / 2) + 1, 'é', ExitCodes.OutputLimit)]
    public void One_answer_over_16_KiB_of_utf8_fails_the_call(int length, char letter, int expected)
    {
        var answer = new string(letter, length);
        var ollama = new FakeOllama
        {
            Chat = (body, _, _) => Task.FromResult(FakeOllama.Ok(FakeOllama.Reply(FakeOllama.Prompt(body) == "prompt 1" ? answer : "short"))),
        };
        var (code, output, error) = Run(Request(Items(3)), ollama);
        Assert.Equal(expected, code);
        if (expected != 0)
        {
            Assert.Empty(output);
            Assert.Equal(Serve.OutputLimitMessage + Environment.NewLine, error);
        }
    }

    [Theory]
    [InlineData("true", true)]
    [InlineData("false", false)]
    public void Passes_think_to_ollama_and_returns_only_the_final_content(string think, bool sent)
    {
        var ollama = new FakeOllama
        {
            Chat = (_, _, _) =>
            {
                var reply = FakeOllama.Reply("Yes");
                reply["message"]!["thinking"] = "secret reasoning about D:\\customer";
                return Task.FromResult(FakeOllama.Ok(reply));
            },
        };
        var (code, output, _) = Run(Request(options: $$"""{"think":{{think}}}"""), ollama);
        Assert.Equal(0, code);
        var chat = Assert.Single(ollama.Chats);
        Assert.Equal(sent, chat["think"]!.GetValue<bool>());
        Assert.Equal("""{"num_predict":4096}""", chat["options"]!.ToJsonString());
        Assert.Equal("Yes", Data(output).GetProperty("items")[0].GetProperty("answer").GetString());
        Assert.DoesNotContain("secret", output);
        Assert.DoesNotContain("thinking", output);
    }

    [Fact]
    public void Runs_each_port_with_its_own_worker_count()
    {
        // The deployment's slots: 4 on the RTX server, 2 on the Arc iGPU server. Each prompt holds its slot until all six
        // are busy (or 2 s pass), so a port running more workers than its slots would show.
        var ollama = new FakeOllama();
        ollama.Chat = async (body, _, token) =>
        {
            var deadline = Stopwatch.StartNew();
            while (ollama.InFlight < 6 && deadline.Elapsed < TimeSpan.FromSeconds(2))
            {
                await Task.Delay(5, token);
            }

            await Task.Delay(20, token);
            return FakeOllama.Ok(FakeOllama.Reply("ok"));
        };
        var (code, output, _) = Run(Request(Items(18)), ollama, Facts(AllowlistedOn(",\"ports\":[11434,11435]")));
        Assert.Equal(0, code);
        Assert.Equal(4, ollama.MaxInFlightOn(11434));
        Assert.Equal(2, ollama.MaxInFlightOn(11435));
        var ports = Data(output).GetProperty("items").EnumerateArray().Select(item => item.GetProperty("port").GetInt32()).ToList();
        Assert.Equal(12, ports.Count(port => port == 11434));
        Assert.Equal(6, ports.Count(port => port == 11435));
    }

    [Theory]
    [InlineData("", new[] { 11434 })]
    [InlineData(",\"ports\":[11434]", new[] { 11434 })]
    [InlineData(",\"ports\":[11435]", new[] { 11435 })]
    [InlineData(",\"ports\":[11435,11434]", new[] { 11435, 11434 })]
    public void Checks_the_pin_on_every_listed_port_and_uses_only_those(string ports, int[] expected)
    {
        var ollama = new FakeOllama();
        var (code, output, _) = Run(Request(Items(12)), ollama, Facts(AllowlistedOn(ports), concurrency: 2));
        Assert.Equal(0, code);
        Assert.Equal(expected, ollama.Requests.Where(request => request.Path == "/api/tags").Select(request => request.Port));
        Assert.All(ollama.Requests, request => Assert.Equal("127.0.0.1", request.Host));
        Assert.All(ollama.Requests, request => Assert.Contains(request.Port, expected));
        var answered = Data(output).GetProperty("items").EnumerateArray().Select(item => item.GetProperty("port").GetInt32()).ToList();
        Assert.Equal(12, answered.Count);
        Assert.All(answered, port => Assert.Contains(port, expected));
        Assert.Equal(answered.Count(port => port == 11435), ollama.ChatsOn(11435).Count);
    }

    [Fact]
    public void A_digest_mismatch_on_either_port_refuses_the_batch()
    {
        foreach (var (wrong, checkedPorts) in new[] { (11434, new[] { 11434 }), (11435, new[] { 11434, 11435 }) })
        {
            var ollama = new FakeOllama
            {
                Tags = port => FakeOllama.Ok(FakeOllama.TagList((FakeOllama.Model, port == wrong ? new string('0', 64) : FakeOllama.Digest))),
            };
            AssertRefused(Run(Request(), ollama, Facts(AllowlistedOn(",\"ports\":[11434,11435]"))));
            Assert.Equal(checkedPorts, ollama.Requests.Select(request => request.Port));
            Assert.Empty(ollama.Chats);
        }
    }

    [Fact]
    public void A_listed_port_whose_server_is_down_is_an_internal_error()
    {
        var ollama = new FakeOllama
        {
            Tags = port => port == 11435 ? new HttpResponseMessage(HttpStatusCode.BadGateway) : FakeOllama.Ok(FakeOllama.TagList((FakeOllama.Model, FakeOllama.Digest))),
        };
        AssertInternal(Run(Request(), ollama, Facts(AllowlistedOn(",\"ports\":[11434,11435]"))));
        Assert.Empty(ollama.Chats);
    }

    [Fact]
    public void Keeps_both_servers_busy_with_the_configured_workers_on_each()
    {
        // Each prompt holds its slot until all four slots (two per port) are busy, so a looser bound would show.
        var ollama = new FakeOllama();
        ollama.Chat = async (body, _, token) =>
        {
            var deadline = Stopwatch.StartNew();
            while (ollama.InFlight < 4 && deadline.Elapsed < TimeSpan.FromSeconds(2))
            {
                await Task.Delay(5, token);
            }

            await Task.Delay(20, token);
            return FakeOllama.Ok(FakeOllama.Reply("ok"));
        };
        var (code, output, _) = Run(Request(Items(16)), ollama, Facts(AllowlistedOn(",\"ports\":[11434,11435]"), concurrency: 2));
        Assert.Equal(0, code);
        Assert.Equal(2, ollama.MaxInFlightOn(11434));
        Assert.Equal(2, ollama.MaxInFlightOn(11435));
        var ports = Data(output).GetProperty("items").EnumerateArray().Select(item => item.GetProperty("port").GetInt32()).ToList();
        Assert.Equal(8, ports.Count(port => port == 11434));
        Assert.Equal(8, ports.Count(port => port == 11435));
    }

    [Fact]
    public void A_faster_server_takes_more_of_the_shared_queue()
    {
        // The Arc iGPU (11435) is slow here: while it answers one prompt, the RTX GPU (11434) answers all the rest.
        var ollama = new FakeOllama
        {
            Chat = async (body, port, token) =>
            {
                await Task.Delay(port == 11435 ? TimeSpan.FromSeconds(2) : TimeSpan.FromMilliseconds(10), token);
                return FakeOllama.Ok(FakeOllama.Reply($"answer: {FakeOllama.Prompt(body)}"));
            },
        };
        var (code, output, _) = Run(Request(Items(10)), ollama, Facts(AllowlistedOn(",\"ports\":[11434,11435]"), concurrency: 1));
        Assert.Equal(0, code);
        var items = Data(output).GetProperty("items").EnumerateArray().ToList();
        Assert.Equal(Enumerable.Range(0, 10).Select(index => $"item-{index}"), items.Select(item => item.GetProperty("id").GetString()));
        Assert.Equal(Enumerable.Range(0, 10).Select(index => $"answer: prompt {index}"), items.Select(item => item.GetProperty("answer").GetString()));
        Assert.Equal(1, items.Count(item => item.GetProperty("port").GetInt32() == 11435));
        Assert.Equal(9, items.Count(item => item.GetProperty("port").GetInt32() == 11434));
    }

    [Fact]
    public void A_failure_on_one_port_stops_the_other_port_too()
    {
        var ollama = new FakeOllama
        {
            Chat = async (body, port, token) =>
            {
                if (port == 11435)
                {
                    return new HttpResponseMessage(HttpStatusCode.InternalServerError);
                }

                await Task.Delay(TimeSpan.FromSeconds(30), token);
                return FakeOllama.Ok(FakeOllama.Reply("late"));
            },
        };
        var watch = Stopwatch.StartNew();
        AssertInternal(Run(Request(Items(20)), ollama, Facts(AllowlistedOn(",\"ports\":[11434,11435]"), concurrency: 1),
            new InferLimits(TimeSpan.FromSeconds(60), TimeSpan.FromSeconds(60))));
        Assert.True(watch.Elapsed < TimeSpan.FromSeconds(15), $"took {watch.Elapsed}");
        Assert.True(SpinWait.SpinUntil(() => ollama.Cancelled >= 1, TimeSpan.FromSeconds(10)));
        Assert.True(ollama.Chats.Count() < 20);
    }

    [Fact]
    public async Task A_worker_takes_nothing_once_the_batch_is_abandoned()
    {
        var request = new InferRequest(FakeOllama.Model, new InferOptions(null, null, null, null), [new InferItem("a", "p", false)]);
        var queue = new System.Collections.Concurrent.ConcurrentQueue<int>([0]);
        var answers = new InferAnswer[1];
        var ollama = new FakeOllama();
        using var client = new ProjectBuddy.PeerOllama.OllamaClient(ollama, new Uri("http://127.0.0.1:11434/"), TimeSpan.FromSeconds(5));
        await PeerAsk.Infer.WorkAsync(11434, client, request, queue, answers, new CancellationToken(canceled: true));
        Assert.Single(queue);
        Assert.Null(answers[0]);
        Assert.Empty(ollama.Requests);

        await PeerAsk.Infer.WorkAsync(11434, client, request, queue, answers, CancellationToken.None);
        Assert.Empty(queue);
        Assert.Equal(11434, answers[0].Port);
    }

    [Fact]
    public void Parses_ports_per_model_and_defaults_to_the_rtx_server()
    {
        var allowlist = PeerAsk.Infer.ParseAllowlist(Encoding.UTF8.GetBytes(AllowlistedOn(",\"ports\":[11435,11434]")));
        Assert.Equal([11434], allowlist["other:1"].Ports);
        Assert.Equal([11435, 11434], allowlist[FakeOllama.Model].Ports);
        Assert.Equal([11434], PeerAsk.Infer.ParseAllowlist(Encoding.UTF8.GetBytes(Allowlisted))[FakeOllama.Model].Ports);
        Assert.Equal([11434, 11435], PeerAsk.Infer.AllowedPorts);
    }

    [Fact]
    public void Builds_one_user_message_per_prompt()
    {
        var request = new InferRequest("m:1", new InferOptions(null, 7, null, false), [new InferItem("a", "hello", true)]);
        Assert.Equal(
            """{"model":"m:1","stream":false,"messages":[{"role":"user","content":"hello"}],"options":{"seed":7,"num_predict":4096},"think":false,"format":"json"}""",
            PeerAsk.Infer.ChatRequest(request, request.Items[0]).ToJsonString());
    }

    [Fact]
    public void The_public_entry_point_uses_the_default_limits()
    {
        Assert.Equal(new InferLimits(TimeSpan.FromSeconds(120), TimeSpan.FromSeconds(600)), InferLimits.Default);
        var stdout = new StringWriter();
        var code = Serve.Execute("infer", new FakeRunner(), Facts(), new MemoryStream(Encoding.UTF8.GetBytes(Request())), new FakeOllama(), stdout, new StringWriter());
        Assert.Equal(0, code);
        Assert.Equal("answer: Is the sky blue?", Data(stdout.ToString()).GetProperty("items")[0].GetProperty("answer").GetString());
    }
}
