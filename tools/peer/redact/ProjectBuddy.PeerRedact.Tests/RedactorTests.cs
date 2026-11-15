using System.Text;
using System.Text.Json.Nodes;
using Xunit;

namespace ProjectBuddy.PeerRedact.Tests;

public class CorpusTests
{
    // Token-shaped test values are assembled at run time, so no token-like literal is ever committed.
    private static readonly Dictionary<string, string> Templates = new()
    {
        ["{{gh}}"] = "gh" + "p_" + new string('a', 36),
        ["{{ghpat}}"] = "github" + "_pat_" + new string('B', 40),
        ["{{glpat}}"] = "gl" + "pat-" + new string('c', 20),
        ["{{glrt}}"] = "gl" + "rt-" + new string('d', 24),
        ["{{aws}}"] = "AK" + "IA" + new string('E', 16),
        ["{{slack}}"] = "xo" + "xb-" + new string('1', 12),
        ["{{jwt}}"] = "ey" + "J" + new string('f', 12) + "." + new string('g', 12) + "." + new string('h', 12),
        ["{{npm}}"] = "np" + "m_" + new string('i', 36),
        ["{{gapi}}"] = "AI" + "za" + new string('j', 35),
        ["{{pem}}"] = "-----BEGIN " + "PRIVATE KEY-----\nMIIEvQ" + new string('k', 40) + "\n-----END " + "PRIVATE KEY-----",
    };

    private static string Expand(string text) => Templates.Aggregate(text, (current, pair) => current.Replace(pair.Key, pair.Value, StringComparison.Ordinal));

    private static JsonObject Corpus()
    {
        for (var directory = new DirectoryInfo(AppContext.BaseDirectory); directory is not null; directory = directory.Parent)
        {
            var path = Path.Combine(directory.FullName, "corpus.json");
            if (File.Exists(path) && File.Exists(Path.Combine(directory.FullName, "global.json")))
            {
                return JsonNode.Parse(File.ReadAllText(path))!.AsObject();
            }
        }

        throw new FileNotFoundException("corpus.json");
    }

    public static TheoryData<string> Ids()
    {
        var data = new TheoryData<string>();
        foreach (var item in Corpus()["items"]!.AsArray())
        {
            data.Add(item!["id"]!.GetValue<string>());
        }

        return data;
    }

    [Theory]
    [MemberData(nameof(Ids))]
    public void NothingListedSurvivesAndDiagnosticsStay(string id)
    {
        var corpus = Corpus();
        Assert.Equal("projectbuddy/redaction-corpus@1", corpus["schema"]!.GetValue<string>());
        var redactor = new Redactor(corpus["denyTerms"]!.AsArray().Select(term => term!.GetValue<string>()));
        var item = corpus["items"]!.AsArray().Single(candidate => candidate!["id"]!.GetValue<string>() == id)!;
        var output = redactor.Redact(Expand(item["input"]!.GetValue<string>())).Text;
        foreach (var gone in item["mustNotContain"]!.AsArray().Select(value => Expand(value!.GetValue<string>())))
        {
            Assert.DoesNotContain(gone, output, StringComparison.OrdinalIgnoreCase);
        }

        foreach (var kept in item["mustContain"]!.AsArray().Select(value => value!.GetValue<string>()))
        {
            Assert.Contains(kept, output, StringComparison.Ordinal);
        }

        Assert.Equal(output, redactor.Redact(output).Text);
    }
}

public class RedactorTests
{
    private static Redactor Plain() => new([]);

    [Fact]
    public void CountsEachRuleThatChangedSomething()
    {
        var result = new Redactor(["Example Corp"]).Redact("mail a@b.example from 192.168.1.9 for Example Corp twice Example Corp");
        Assert.Equal(1, result.Counts["email"]);
        Assert.Equal(1, result.Counts["ip"]);
        Assert.Equal(2, result.Counts["private"]);
        Assert.Equal(4, result.Total);
        Assert.Empty(Plain().Redact("nothing here").Counts);
    }

    [Fact]
    public void KeptValuesAreNotCounted()
    {
        var result = Plain().Redact("peer 10.211.0.2 and C:\\Windows\\System32 and https://github.com and ::1 and 999.1.1.1");
        Assert.Equal(0, result.Total);
        Assert.Equal("peer 10.211.0.2 and C:\\Windows\\System32 and https://github.com and ::1 and 999.1.1.1", result.Text);
    }

    [Theory]
    [InlineData("ab")]
    [InlineData("line\nbreak")]
    [InlineData("rivat")]
    [InlineData("<path>")]
    public void InvalidDenyTermsAreRefused(string term) => Assert.Throws<RedactionException>(() => new Redactor([term]));

    [Fact]
    public void DenyListsAreBounded()
    {
        Assert.Throws<RedactionException>(() => new Redactor([new string('x', Redactor.MaxTermLength + 1)]));
        Assert.Throws<RedactionException>(() => new Redactor(Enumerable.Range(0, Redactor.MaxTerms + 1).Select(index => $"term{index:D5}")));
    }

    [Fact]
    public void LongerTermsWinAndMatchingIgnoresCase()
    {
        var result = new Redactor(["Acme", "Acme Widgets"]).Redact("ACME WIDGETS and acme");
        Assert.Equal("<private> and <private>", result.Text);
    }

    [Fact]
    public void ForHostAddsTheMachineAndAccountNames()
    {
        var redactor = Redactor.ForHost(["Example"], "WS-HOST-7", "jdoe");
        Assert.Equal("<private> <private> <private>", redactor.Redact("WS-HOST-7 jdoe example").Text);
        Assert.Equal("ab", Redactor.ForHost([], "ab", " ").Redact("ab").Text);
    }

    [Fact]
    public void DenyListFilesAreStrict()
    {
        Assert.Equal(["a1b", "c2d"], Redactor.ParseDenyList(Encoding.UTF8.GetBytes("""{"schema":"projectbuddy/redaction-denylist@1","terms":["a1b","c2d"]}""")));
        foreach (var bad in new[]
        {
            "not json",
            "[]",
            """{"schema":"other@1","terms":[]}""",
            """{"schema":"projectbuddy/redaction-denylist@1"}""",
            """{"schema":"projectbuddy/redaction-denylist@1","terms":"x"}""",
            """{"schema":"projectbuddy/redaction-denylist@1","terms":[1]}""",
            """{"schema":"projectbuddy/redaction-denylist@1","terms":[],"extra":1}""",
            """{"schema":"projectbuddy/redaction-denylist@1","schema":"projectbuddy/redaction-denylist@1","terms":[]}""",
            """{"schema":1,"terms":[]}""",
        })
        {
            Assert.Throws<RedactionException>(() => Redactor.ParseDenyList(Encoding.UTF8.GetBytes(bad)));
        }
    }

    [Theory]
    [InlineData("C:\\Users\\Jane", "C:\\Users\\<user>")]
    [InlineData("C:\\Users\\Jane\\", "C:\\Users\\<user>\\")]
    [InlineData("C:\\Users\\Jane\\.ollama\\models", "C:\\Users\\<user>\\.ollama\\models")]
    [InlineData("C:\\Users\\Jane\\Desktop\\x", "C:\\Users\\<user>\\<path>")]
    [InlineData("C:\\Users\\<user>\\<path>", "C:\\Users\\<user>\\<path>")]
    [InlineData("C:\\Program Files\\Oracle\\VirtualBox\\VBoxManage.exe --version", "C:\\Program Files\\Oracle\\VirtualBox\\VBoxManage.exe --version")]
    [InlineData("E:\\vms\\x.vdi", "<path>")]
    public void WindowsPathsKeepOnlyAllowedLocations(string input, string expected) => Assert.Equal(expected, Plain().Redact(input).Text);

    [Theory]
    [InlineData("https://github.com", "https://github.com")]
    [InlineData("https://github.com/", "https://github.com/")]
    [InlineData("https://api.github.com:443/repos/x", "https://api.github.com:443/<path>")]
    [InlineData("https://raw.githubusercontent.com/a/b", "https://raw.githubusercontent.com/<path>")]
    [InlineData("https://notgithub.com/a", "<url>")]
    [InlineData("HTTPS://GitHub.com/A", "HTTPS://github.com/<path>")]
    public void UrlsKeepOnlyAllowedHostsAndNeverAPath(string input, string expected) => Assert.Equal(expected, Plain().Redact(input).Text);

    [Fact]
    public void AllowListsCanBeReplaced()
    {
        var redactor = new Redactor([], allowedHosts: ["corp.example"], allowedNetworks: ["192.168.0.0/16", "0.0.0.0/0"], allowedPaths: ["E:\\vms\\"], allowedProfilePaths: []);
        Assert.Equal("https://git.corp.example/<path> <url> 192.168.5.5 8.8.8.8 E:\\vms\\x.vdi C:\\Users\\<user>\\<path>",
            redactor.Redact("https://git.corp.example/a https://github.com/a 192.168.5.5 8.8.8.8 E:\\vms\\x.vdi C:\\Users\\Jane\\AppData\\Local\\Programs\\x").Text);
    }

    [Theory]
    [InlineData("10.0.0.0/33")]
    [InlineData("10.0.0.0")]
    [InlineData("::1/128")]
    [InlineData("nope/8")]
    [InlineData("10.0.0.0/x")]
    public void InvalidNetworksAreRefused(string cidr) => Assert.Throws<RedactionException>(() => new Redactor([], allowedNetworks: [cidr]));

    [Theory]
    [InlineData("a :: b", "a :: b")]
    [InlineData("fe80::1%eth0", "<ip>%eth0")]
    [InlineData("2001:db8::8a2e:370:7334", "<ip>")]
    [InlineData("12:30:45", "12:30:45")]
    [InlineData("[Math]::Round", "[Math]::Round")]
    public void OnlyRealIPv6AddressesAreRedacted(string input, string expected) => Assert.Equal(expected, Plain().Redact(input).Text);

    [Theory]
    [InlineData("/home/jane", "/home/<user>")]
    [InlineData("/home/jane/", "/home/<user>/")]
    [InlineData("/Users/jane/Library/x", "/Users/<user>/<path>")]
    public void PosixHomesHideTheUserAndEverythingBelow(string input, string expected) => Assert.Equal(expected, Plain().Redact(input).Text);

    [Fact]
    public void AnUnterminatedPrivateKeyIsRedactedToTheEnd() =>
        Assert.Equal("x <secret>", Plain().Redact("x -----BEGIN " + "RSA PRIVATE KEY-----\nMIIabc\nrest of log").Text);
}
