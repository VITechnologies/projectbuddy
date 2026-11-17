using System.Net;
using System.Text;
using System.Text.Json.Nodes;

namespace ProjectBuddy.PeerAsk.Tests;

// A scripted loopback Ollama: never touches the network, records every request and how many chats ran at once.
internal sealed class FakeOllama : HttpMessageHandler
{
    public const string Model = "bespoke-minicheck:7b";
    public const string Digest = "845dbda0ea48ed749caafd9e6037047aa19acfcfd82e704d7ca97d631a0b697e";

    private readonly Lock gate = new();
    private int inFlight;

    public List<(string Path, JsonObject? Body)> Requests { get; } = [];

    public int MaxInFlight { get; private set; }

    public int InFlight
    {
        get
        {
            lock (gate)
            {
                return inFlight;
            }
        }
    }

    public int Cancelled { get; private set; }

    public Func<HttpResponseMessage> Tags { get; set; } = () => Ok(TagList((Model, Digest)));

    // The default reply echoes the prompt, so each answer can be traced to its item.
    public Func<JsonObject, CancellationToken, Task<HttpResponseMessage>> Chat { get; set; } =
        (body, _) => Task.FromResult(Ok(Reply($"answer: {Prompt(body)}")));

    public IEnumerable<JsonObject> Chats
    {
        get
        {
            lock (gate)
            {
                return Requests.Where(request => request.Path == "/api/chat").Select(request => request.Body!).ToList();
            }
        }
    }

    public static string Prompt(JsonObject body) => body["messages"]![0]!["content"]!.GetValue<string>();

    public static HttpResponseMessage Ok(JsonNode body) =>
        new(HttpStatusCode.OK) { Content = new StringContent(body.ToJsonString(), Encoding.UTF8, "application/json") };

    public static JsonObject TagList(params (string Name, string Digest)[] models) =>
        new() { ["models"] = new JsonArray([.. models.Select(model => (JsonNode)new JsonObject { ["name"] = model.Name, ["digest"] = model.Digest })]) };

    public static JsonObject Reply(string content, int prompt = 100, int output = 20, long totalNanoseconds = 1_234_567_890) => new()
    {
        ["model"] = Model,
        ["message"] = new JsonObject { ["role"] = "assistant", ["content"] = content },
        ["done"] = true,
        ["prompt_eval_count"] = prompt,
        ["eval_count"] = output,
        ["total_duration"] = totalNanoseconds,
        ["eval_duration"] = 1_000_000_000L,
    };

    protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken)
    {
        var body = request.Content is null ? null : JsonNode.Parse(await request.Content.ReadAsStringAsync(cancellationToken)) as JsonObject;
        var path = request.RequestUri!.AbsolutePath;
        lock (gate)
        {
            Requests.Add((path, body));
        }

        if (path == "/api/tags")
        {
            return Tags();
        }

        lock (gate)
        {
            MaxInFlight = Math.Max(MaxInFlight, ++inFlight);
        }

        try
        {
            return await Chat(body!, cancellationToken);
        }
        catch (OperationCanceledException)
        {
            lock (gate)
            {
                Cancelled++;
            }

            throw;
        }
        finally
        {
            lock (gate)
            {
                inFlight--;
            }
        }
    }
}

// Standard input for verbs that must never read it.
internal sealed class UnreadableStream : Stream
{
    public override bool CanRead => true;
    public override bool CanSeek => false;
    public override bool CanWrite => false;
    public override long Length => throw new NotSupportedException();
    public override long Position { get => throw new NotSupportedException(); set => throw new NotSupportedException(); }
    public override void Flush() => throw new NotSupportedException();
    public override int Read(byte[] buffer, int offset, int count) => throw new InvalidOperationException("stdin was read");
    public override long Seek(long offset, SeekOrigin origin) => throw new NotSupportedException();
    public override void SetLength(long value) => throw new NotSupportedException();
    public override void Write(byte[] buffer, int offset, int count) => throw new NotSupportedException();
}

// Standard input whose sender never finishes: a read waits until it is cancelled.
internal sealed class StallingStream : Stream
{
    public override bool CanRead => true;
    public override bool CanSeek => false;
    public override bool CanWrite => false;
    public override long Length => throw new NotSupportedException();
    public override long Position { get => throw new NotSupportedException(); set => throw new NotSupportedException(); }
    public override void Flush() => throw new NotSupportedException();
    public override int Read(byte[] buffer, int offset, int count) => throw new NotSupportedException();

    public override async ValueTask<int> ReadAsync(Memory<byte> buffer, CancellationToken cancellationToken = default)
    {
        await Task.Delay(Timeout.Infinite, cancellationToken);
        return 0;
    }

    public override long Seek(long offset, SeekOrigin origin) => throw new NotSupportedException();
    public override void SetLength(long value) => throw new NotSupportedException();
    public override void Write(byte[] buffer, int offset, int count) => throw new NotSupportedException();
}

// Files the tests write, under the test output directory, one fresh name each.
internal static class Fixture
{
    private static readonly string Directory = Path.Combine(AppContext.BaseDirectory, "infer-fixtures");

    public static string Write(byte[] bytes)
    {
        System.IO.Directory.CreateDirectory(Directory);
        var path = Path.Combine(Directory, $"{Guid.NewGuid():N}.json");
        File.WriteAllBytes(path, bytes);
        return path;
    }

    public static string Write(string text) => Write(Encoding.UTF8.GetBytes(text));

    public static string Missing => Path.Combine(Directory, $"{Guid.NewGuid():N}-missing.json");

    public static string AnyDirectory
    {
        get
        {
            System.IO.Directory.CreateDirectory(Directory);
            return Directory;
        }
    }
}
