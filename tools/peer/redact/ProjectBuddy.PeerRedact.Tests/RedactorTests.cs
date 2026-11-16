using System.Text;
using System.Text.Json;
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
        var redactor = new Redactor(corpus["denyTerms"]!.AsArray().Select(term => term!.GetValue<string>()),
            wholeWordTerms: corpus["wholeWordTerms"]!.AsArray().Select(term => term!.GetValue<string>()));
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

        // Evidence that is JSON stays JSON.
        if (IsJson(Expand(item["input"]!.GetValue<string>())))
        {
            using var document = JsonDocument.Parse(output);
        }
    }

    private static bool IsJson(string text)
    {
        try
        {
            using var document = JsonDocument.Parse(text);
            return true;
        }
        catch (JsonException)
        {
            return false;
        }
    }

    // The same evidence inside JSON, as the agent and Windows tools emit it: System.Text.Json at one and two levels, and
    // PowerShell 5.1 ConvertTo-Json style (\" escapes). Nothing listed may survive in the decoded value, the output must
    // still parse at every level, and redaction stays idempotent. Placeholder entries ("no <host> here") are checks
    // against over-redaction of raw text and are skipped.
    [Theory]
    [MemberData(nameof(Ids))]
    public void WrappedInJsonNothingListedSurvivesAndJsonStaysValid(string id)
    {
        var corpus = Corpus();
        var redactor = new Redactor(corpus["denyTerms"]!.AsArray().Select(term => term!.GetValue<string>()),
            wholeWordTerms: corpus["wholeWordTerms"]!.AsArray().Select(term => term!.GetValue<string>()));
        var item = corpus["items"]!.AsArray().Single(candidate => candidate!["id"]!.GetValue<string>() == id)!;
        var input = Expand(item["input"]!.GetValue<string>());
        var once = JsonSerializer.Serialize(new { v = input });
        var powerShell = "{\"v\":\"" + input.Replace("\\", "\\\\").Replace("\"", "\\\"").Replace("\r", "\\r").Replace("\n", "\\n").Replace("\t", "\\t") + "\"}";
        foreach (var (json, depth) in new[] { (once, 1), (JsonSerializer.Serialize(new { v = once }), 2), (powerShell, 1) })
        {
            var output = redactor.Redact(json).Text;
            Assert.Equal(output, redactor.Redact(output).Text);
            var value = output;
            for (var level = 0; level < depth; level++)
            {
                using var document = JsonDocument.Parse(value);
                value = document.RootElement.GetProperty("v").GetString()!;
            }

            foreach (var gone in item["mustNotContain"]!.AsArray().Select(entry => Expand(entry!.GetValue<string>())).Where(entry => !Redactor.Placeholders.Contains(entry)))
            {
                Assert.DoesNotContain(gone, value, StringComparison.OrdinalIgnoreCase);
            }
        }
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
        var redactor = new Redactor([], allowedHosts: ["corp.example"], allowedNetworks: ["192.168.0.0/16", "0.0.0.0/0"], allowedPaths: [new(@"E:\vms", 1)], allowedProfilePaths: []);
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

    [Fact]
    public void ForHostAcceptsNamesThatOverlapPlaceholders()
    {
        var redactor = Redactor.ForHost([], "PATH-HOST", "User");
        var once = redactor.Redact("User on PATH-HOST wrote C:\\Users\\Jane\\x\n<user> stays");
        Assert.Equal("<private> on <private> wrote C:\\Users\\<user>\\<path>\n<user> stays", once.Text);
        Assert.Equal(once.Text, redactor.Redact(once.Text).Text);
    }

    [Theory]
    [InlineData("vms")]
    [InlineData("E:vms")]
    public void AllowedPathsNeedADriveRoot(string prefix) =>
        Assert.Throws<RedactionException>(() => new Redactor([], allowedPaths: [new(prefix, 1)]));

    [Fact]
    public void AllowedPathsNeedANonNegativeDepth() =>
        Assert.Throws<RedactionException>(() => new Redactor([], allowedPaths: [new(@"E:\vms", -1)]));

    [Theory]
    [InlineData("copy to C:\\", "copy to C:\\")]
    [InlineData("C:\\Users", "C:\\Users")]
    [InlineData("C:\\Windows\\", "C:\\Windows\\")]
    [InlineData("C:\\Windows\\Temp\\", "C:\\Windows\\Temp\\")]
    [InlineData("C:\\Windows\\Temp\\x\\", "C:\\Windows\\Temp\\<path>")]
    public void RootsAndTrailingSeparatorsNameNothing(string input, string expected) => Assert.Equal(expected, Plain().Redact(input).Text);

    [Fact]
    public void OversizedEvidenceIsRefusedWhole()
    {
        // The output cap is checked before any work, so the real size costs nothing.
        Assert.Throws<RedactionException>(() => Plain().Redact(new string('a', Redactor.MaxOutputChars + 1)));
        Assert.Throws<RedactionException>(() => new Redactor([]) { InputLimit = 8, OutputLimit = 40 }.Redact("1.2.3.4 1.2.3.4"));
        Assert.Throws<RedactionException>(() => new Redactor(["abc"]) { OutputLimit = 20 }.Redact("abc abc abc"));
    }

    [Fact]
    public void RedactedOutputOverTheInputCapIsAcceptedOnlyWhenSettled()
    {
        var redactor = new Redactor(["abc"]) { InputLimit = 16, OutputLimit = 48 };
        var once = redactor.Redact("abc abc abc abc").Text;
        Assert.True(once.Length > 16);
        Assert.Equal(once, redactor.Redact(once).Text);
        Assert.Equal("aaaaaaaaaaaaaaaaa", new Redactor([]) { InputLimit = 16, OutputLimit = 48 }.Redact(new string('a', 17)).Text);
    }

    // The same at the real caps, run in Release outside the coverage job (Category=Performance).
    [Fact]
    [Trait("Category", "Performance")]
    public void RedactedOutputOverTheRealInputCapIsAcceptedOnlyWhenSettled()
    {
        var redactor = new Redactor(["abc"]);
        var once = redactor.Redact(string.Concat(Enumerable.Repeat("abc ", Redactor.MaxInputChars / 4))).Text;
        Assert.True(once.Length > Redactor.MaxInputChars);
        Assert.Equal(once, redactor.Redact(once).Text);
        Assert.Throws<RedactionException>(() => Plain().Redact(string.Concat(Enumerable.Repeat("1.2.3.4 ", Redactor.MaxInputChars / 8 + 1))));
    }

    [Fact]
    public void InvisibleAndBrokenCharactersAreCleaned()
    {
        var redactor = new Redactor(["Example Widgets"]);
        Assert.Equal("<private> \uFFFD x \U0001F600", redactor.Redact("Exa\u00ADmple\u200B Widgets \uD800 x \U0001F600").Text);
    }

    // Each shape once made a rule backtrack quadratically, or is the worst case of one; input up to the cap must stay
    // well inside interactive time. Run in Release outside the coverage job (Category=Performance).
    [Theory]
    [InlineData("", "a.")]
    [InlineData("", "a-")]
    [InlineData("", "a\\")]
    [InlineData("", "a/")]
    [InlineData("", "C:\\")]
    [InlineData("", "a@")]
    [InlineData("", "pass" + "word=a ")]
    [InlineData("", "pass" + "word=\"a ")]
    [InlineData("", "--token a ")]
    [InlineData("key=\"", "pwd")]
    [InlineData("key=\"", "token")]
    [InlineData("", ";pass" + "word=a b")]
    [InlineData("", "C:\\a b:\\c ")]
    [InlineData("", "-oD:\\a ")]
    [InlineData("", "a.b.c.it.")]
    [InlineData("net use ", "a ")]
    [InlineData("", "CN=a\\,")]
    [InlineData("", "1", 1000)]
    [InlineData("", "1.2.3.4 ", 1000, '\n')]
    [InlineData("", "Version 1.2.3.4 ", 40)]
    [InlineData("", "/O=a")]
    [InlineData("", "7z -pa ")]
    [InlineData("", "sqlcmd -P a ")]
    [InlineData("", "\\\\u0027")]
    [InlineData("", "\\\\\\\\")]
    [InlineData("", "\"a")]
    [InlineData("7z ", "-p\"a ")]
    [InlineData("", "Subject: s=1, ")]
    [InlineData("", "D:\\a' ")]
    [InlineData("", "DefaultPassword  ")]
    [InlineData("CN=a", "\n O=a")]
    [InlineData("CN=a", ", x=a")]
    [InlineData("", "'D:\\a' b\\")]
    [InlineData("net use ", "\"a ")]
    [InlineData("CN=a, O=\"", "a")]
    [InlineData("", "'D:\\a' because ")]
    [InlineData("\"", "\\\"")]
    [InlineData("", "\\\\u0022a")]
    [InlineData("/user:\\\"", "a")]
    [InlineData("[", "\"a\\nb\",")]
    [InlineData("", "{\"v\":\"a\\nb\"},")]
    [InlineData("", "{\"Key\":\"password\",\"Value\":\"a\"},")]
    [InlineData("", "[\"--password\",\"a\",")]
    [InlineData("", "a,")]
    [InlineData("", "Name : password\nValue : x\n")]
    [InlineData("[\"--token\",\"x\"", ",\"a\"")]
    [InlineData("", "[")]
    [InlineData("", "{\"a\":")]
    [InlineData("", "{\"Key\":\"password\",\"m\":{\"a\":1},\"Value\":\"a\"},")]
    [InlineData("", "Value : x\nName : password\n")]
    [InlineData("", "GPG_PASSPHRASE=a b\n")]
    [InlineData("INFO body={", "\\\"k\\\":\\\"v\\\",")]
    [InlineData("{", "\\\"")]
    [InlineData("", "<S N=\"Name\">x</S>")]
    [InlineData("", "@{Name=DB_PASSWORD; Value=x}")]
    [InlineData("", "'DB_PASSWORD=a ")]
    [InlineData("", "{\"FileName\":\"sqlcmd\",\"Arguments\":\"-P a\"},")]
    [InlineData("level=info msg=\"{", "\\\"k\\\":\\\"v\\\",")]
    [InlineData("<a", " N=\"Name\"")]
    [InlineData("", "[DB_PASSWORD, x")]
    [InlineData("", "setx DB_PASSWORD a ")]
    [InlineData("", "[x] DB_PASSWORD=a\n")]
    [InlineData("", "Set-Item env:DB_PASSWORD a ")]
    [InlineData("", "macaddress1=\"080027A1B2C3\" ")]
    [Trait("Category", "Performance")]
    public void LargeAdversarialInputsStayFast(string head, string unit, int pad = 0, char padding = ' ')
    {
        unit = new string(padding, pad) + unit;
        var text = head + string.Concat(Enumerable.Repeat(unit, (Redactor.MaxInputChars - head.Length) / unit.Length));
        var watch = System.Diagnostics.Stopwatch.StartNew();
        Plain().Redact(text);
        Assert.True(watch.Elapsed < TimeSpan.FromSeconds(30), $"{head}{unit}: {watch.Elapsed}");
    }

    [Fact]
    public void RedactionThatDoesNotSettleIsRefused()
    {
        var redactor = new Redactor([]) { Passes = 1 };
        Assert.Equal("nothing here", redactor.Redact("nothing here").Text);
        Assert.Throws<RedactionException>(() => redactor.Redact("mail a@b.example"));
    }

    [Theory]
    [InlineData("lab", "LabVIEW by lab, lab-01, lab_build, labs", "LabVIEW by <private>, <private>-01, <private>_build, labs")]
    [InlineData("admin", "Administrators and admin", "Administrators and <private>")]
    [InlineData("Jane Doe", "Jane\u00A0 Doe and JaneDoe", "<private> and JaneDoe")]
    public void WholeWordTermsMatchOnlyWholeWords(string term, string input, string expected) =>
        Assert.Equal(expected, new Redactor([], wholeWordTerms: [term]).Redact(input).Text);

    [Theory]
    [InlineData("net use Z: \\\\fs01\\share Sup3r extra", "net use Z: <path> <secret> extra")]
    [InlineData("net user jane Sup3r extra /add", "net user <user> <secret> extra /add")]
    [InlineData("net user", "net user")]
    [InlineData("NET.EXE USE LPT1: \\\\fs01\\printer", "NET.EXE USE LPT1: <path>")]
    [InlineData("schtasks /Query /P /F /RU", "schtasks /Query /P /F /RU")]
    [InlineData("schtasks.exe /Change /TN x /RP Sup3r", "schtasks.exe /Change /TN x /RP <secret>")]
    [InlineData("sqlcmd -S db01 -U sa -P Sup3r -Q x", "sqlcmd -S db01 -U <user> -P <secret> -Q x")]
    [InlineData("sqlcmd -Usa -PSup3r -p", "sqlcmd -U<user> -P<secret> -p")]
    [InlineData("bcp.exe t out f -P<secret>", "bcp.exe t out f -P<secret>")]
    [InlineData("certutil -f -P Sup3r -importpfx x.pfx", "certutil -f -P <secret> -importpfx x.pfx")]
    [InlineData("PsExec64 -u jane -p Sup3r cmd", "PsExec64 -u <user> -p <secret> cmd")]
    [InlineData("docker login -u jane -pSup3r", "docker login -u <user> -p<secret>")]
    [InlineData("7z a -pSup3r b.7z", "7z a -p<secret> b.7z")]
    [InlineData("7za a -p b.7z", "7za a -p b.7z")]
    [InlineData("7zr a -pSup3r b.7z", "7zr a -p<secret> b.7z")]
    [InlineData("osql -U sa -P Sup3r", "osql -U <user> -P <secret>")]
    [InlineData("psexec.exe -p Sup3r cmd", "psexec.exe -p <secret> cmd")]
    [InlineData("openssl pkcs12 -passout pass:Sup3r -passin env:X", "openssl pkcs12 -passout <secret> -passin <secret>")]
    public void CommandLinesHideTheirPasswordArguments(string input, string expected) => Assert.Equal(expected, Plain().Redact(input).Text);

    [Theory]
    [InlineData("{\"password\":\"x\"}", "{\"password\":\"<secret>\"}")]
    [InlineData("--token \"x\"}", "--token <secret>}")]
    [InlineData("User Id=\"sa\";", "User Id=<user>;")]
    public void RedactedValuesAreNotTakenAgain(string input, string expected) => Assert.Equal(expected, Plain().Redact(input).Text);

    [Theory]
    [InlineData("C:\\Windows\\Temp\\ ", "C:\\Windows\\Temp\\ ")]
    [InlineData("C:\\Users\\ ", "C:\\Users\\ ")]
    [InlineData("C:\\Windows\\Temp\\x  /c", "C:\\Windows\\Temp\\<path>  /c")]
    [InlineData("C:\\Windows\\...\\x", "<path>")]
    [InlineData("C:\\Windows\\ .. \\x", "<path>")]
    public void PathEdgesKeepSpacingAndRefuseDotSegments(string input, string expected) => Assert.Equal(expected, Plain().Redact(input).Text);

    [Fact]
    public void ChainsThroughEveryRuleStillSettle() =>
        Assert.Equal("user <private><host><ip><path>", Redactor.ForHost([], "WS-01", "jane").Redact("user jane_fs01.corp.local::5\\\\fs01\\share").Text);

    [Theory]
    [InlineData("$pw = \"Sup3r\"", "$pw = \"<secret>\"")]
    [InlineData("$pass = 'Sup3r'", "$pass = '<secret>'")]
    [InlineData("db_pass=Sup3r", "db_pass=<secret>")]
    [InlineData("bypass=true", "bypass=true")]
    [InlineData("<add key=\\\"ClearTextPassword\\\" value=\\\"Qzx secret9\\\" />", "<add key=\\\"ClearTextPassword\\\" value=\\\"<secret>\\\" />")]
    [InlineData("$pw = Read-Host -AsSecureString", "$pw = Read-Host -AsSecureString")]
    [InlineData("New-Object System.Net.NetworkCredential(\"jane\", \"Sup3r\")", "New-Object System.Net.NetworkCredential(<user>, <secret>)")]
    [InlineData("[NetworkCredential]::new('jane', 'Sup3r')", "[NetworkCredential]::new(<user>, <secret>)")]
    public void ScriptCredentialsAreHidden(string input, string expected) => Assert.Equal(expected, Plain().Redact(input).Text);

    [Theory]
    [InlineData("Subject : CN=fs01.contoso.local, O=Contoso Ltd, L=Austin, S=Texas, C=US", "Subject : CN=<private>, O=<private>, L=<private>, S=<private>, C=US")]
    [InlineData("Issuer: O=Contoso Ltd", "Issuer: O=<private>")]
    [InlineData("/C=US/ST=Texas/O=Contoso/CN=fs01", "/C=US/ST=<private>/O=<private>/CN=<private>")]
    [InlineData("CN=DigiCert Global Root G2, O=Microsoft Corporation", "CN=DigiCert Global Root G2, O=Microsoft Corporation")]
    [InlineData("CN=github.com", "CN=github.com")]
    [InlineData("Issuer: CN=Microsoft Code Signing PCA 2011, O=Microsoft Corporation, ST=Washington, OU=Contoso Dept",
        "Issuer: CN=Microsoft Code Signing PCA 2011, O=Microsoft Corporation, ST=Washington, OU=<private>")]
    [InlineData("so=1 and x,o", "so=1 and x,o")]
    public void CertificateNamesHideTheOwnerButKeepPublicIssuers(string input, string expected) => Assert.Equal(expected, Plain().Redact(input).Text);

    [Theory]
    [InlineData("Primary Dns Suffix . . . : acme.it", "Primary Dns Suffix . . . : <host>")]
    [InlineData("Resolve-DnsName acme.pl; ping acme.in", "Resolve-DnsName <host>; ping <host>")]
    [InlineData("user.id and README.md", "user.id and README.md")]
    public void CompanyDomainsUnderRiskyCountryDomainsCountInDnsContext(string input, string expected) => Assert.Equal(expected, Plain().Redact(input).Text);

    [Theory]
    [InlineData("\"at \\u0027D:/Clients/x\\u0027\"", "\"at '<path>'\"")]
    [InlineData("\"\\u0022q\\u0022 \\u005C\"", "\"\\\"q\\\" \\\\\"")]
    [InlineData("\"\\uD83D\\uDE00 \\uD800 \\u0001 \\u007F\"", "\"\U0001F600 \uFFFD \\u0001 \u007F\"")]
    [InlineData("\"x\\\\u0027 stays\"", "\"x\\\\u0027 stays\"")]
    [InlineData("\"a\\\\u200Bb\"", "\"ab\"")]
    [InlineData("at x\\\\u0027D:/Clients/x", "at x\\\\u0027<path>")]
    [InlineData("at \\\\u0027D:/Clients/x", "at <path><path>")]
    [InlineData("at \\u0027D:/Clients/x", "at \\u0027<path>")]
    [InlineData("C:\\Windows\\Temp\\uadd1\\Acme Bid\\plan.docx", "C:\\Windows\\Temp\\<path>")]
    [InlineData("C:\\Users\\u0123456\\Documents\\x", "C:\\Users\\<user>\\<path>")]
    [InlineData("\"C:\\Temp\\uadd1\\x\"", "\"<path>\"")]
    [InlineData("x\\\u200Bu0041y", "x\\u0041y")]
    public void JsonEscapesCannotHidePaths(string input, string expected) => Assert.Equal(expected, Plain().Redact(input).Text);

    [Fact]
    public void TermsMatchTheirJsonEscapedSpellings()
    {
        Assert.Equal("<private> and <private>", new Redactor(["O'Brien", "Müller"]).Redact("O\\\\u0027Brien and M\\\\u00fcller").Text);
        Assert.Equal("x\\\\u0027<private>", Redactor.ForHost([], "WS-01", "jane").Redact("x\\\\u0027jane").Text);
    }

    [Theory]
    [InlineData("C:\\Users\\ \\Jane\\x", "<path>")]
    [InlineData("C:\\Windows\\ \\Temp\\x", "<path>")]
    [InlineData("ProductVersion     : 1.2.3.4", "ProductVersion     : 1.2.3.4")]
    public void BlankNamesCannotDodgeALimit(string input, string expected) => Assert.Equal(expected, Plain().Redact(input).Text);

    // JSON string literals between JSON punctuation are redacted as the text they stand for and written back well formed.
    [Theory]
    [InlineData(@"{""v"":""a\b\f\n\r\t\""\\\/\u0001z""}", @"{""v"":""a\b\f\n\r\t\""\\/\u0001z""}")]
    [InlineData(@"[""x\nC:\\Users\\Jane\\x""]", @"[""x\nC:\\Users\\<user>\\<path>""]")]
    [InlineData(@"{""k\nC:\\Users\\Jane"": 1}", @"{""k\nC:\\Users\\<user>"": 1}")]
    [InlineData(@"""x\tC:\\Users\\Jane""", @"""x\tC:\\Users\\<user>""")]
    [InlineData(@"{""v"": [""a\nb"", ""c\nd""]}", @"{""v"": [""a\nb"", ""c\nd""]}")]
    [InlineData(@"say ""C:\temp\new"" now", @"say ""<path>"" now")]
    [InlineData(@"{""v"":""a\nC:\\Users\\Jane"" x}", @"{""v"":""a\nC:\\Users\\<user>"" x}")]
    [InlineData(@"log: {\""line\"":\""    DefaultPassword    REG_SZ    Sup3r\""}", @"log: {\""line\"":\""    DefaultPassword    REG_SZ    <secret>\""}")]
    [InlineData(@"key=""Server"" value=""db01""", @"key=""Server"" value=""db01""")]
    [InlineData("a\uFDD0b", "ab")]
    public void JsonStringsAreRedactedAsTheTextTheyStandFor(string input, string expected) => Assert.Equal(expected, Plain().Redact(input).Text);

    // The JSON scanner pairs sibling members at every depth and survives truncated, malformed and deeply nested input.
    [Theory]
    [InlineData(@"{""Key"":""DB_PASSWORD"",""Value"":{""Name"":""API_TOKEN"",""Value"":""x""}}", @"{""Key"":""DB_PASSWORD"",""Value"":""<secret>""}")]
    [InlineData("{\"Key\":\"DB_PASSWORD\",\"Value\":\"x\n", "{\"Key\":\"DB_PASSWORD\",\"Value\":\"<secret>\"\n")]
    [InlineData("{\"Key\":\"DB_PASSWORD\",\"Value\":\"x\r\n", "{\"Key\":\"DB_PASSWORD\",\"Value\":\"<secret>\"\r\n")]
    [InlineData(@"{""Key"":""DB_PASSWORD"",""Value"":""x", @"{""Key"":""DB_PASSWORD"",""Value"":""<secret>""")]
    [InlineData(@"{""Key"":""a", @"{""Key"":""a")]
    [InlineData(@"{""Ke", @"{""Ke")]
    [InlineData(@"[] ""x", @"[] ""x")]
    [InlineData(@"{""Key"":""DB\qPASSWORD"",""Value"":""x""}", @"{""Key"":""DB\qPASSWORD"",""Value"":""<secret>""}")]
    [InlineData(@"{""Key"":""DB_PASSWORD"",""Value"":123}", @"{""Key"":""DB_PASSWORD"",""Value"":123}")]
    [InlineData(@"{""Key"":""DB_PASSWORD"",""Value"":x9}", @"{""Key"":""DB_PASSWORD"",""Value"":""<secret>""}")]
    [InlineData(@"{""Arguments"":[""-P"",""x"",""--password"",""y""]}", @"{""Arguments"":[""-P"",""x"",""--password"",""<secret>""]}")]
    [InlineData(@"[{""Key"":""DB_PASSWORD"",""Value"":""Sup3r""", @"[{""Key"":""DB_PASSWORD"",""Value"":""<secret>""")]
    [InlineData(@"""Credential"": {""UserName"": null, ""Password"": null}", @"""Credential"": {""UserName"": null, ""Password"": null}")]
    [InlineData(@"{""Password"": x9}", @"{""Password"": ""<secret>""}")]
    [InlineData(@"{ code: [1, 2] } ] and [x] and {""a"":1}", @"{ code: [1, 2] } ] and [x] and {""a"":1}")]
    public void JsonLayoutsAreReadAtEveryDepth(string input, string expected) => Assert.Equal(expected, Plain().Redact(input).Text);

    [Theory]
    [InlineData(255)]
    [InlineData(256)]
    [InlineData(257)]
    [InlineData(1000)]
    public void DeepJsonNestingNeitherLeaksNorGrowsWithoutBound(int depth)
    {
        var text = new string('[', depth) + @"{""Key"":""DB_PASSWORD"",""Value"":""Sup3r""}" + new string(']', depth);
        Assert.DoesNotContain("Sup3r", Plain().Redact(text).Text, StringComparison.Ordinal);
    }

    [Theory]
    [InlineData("<Settings><Value>shown</Value></Settings>", "<Settings><Value>shown</Value></Settings>")]
    [InlineData("<add name=\"Server\" value=\"db01\"/>", "<add name=\"Server\" value=\"db01\"/>")]
    [InlineData("<Password><Value>Sup3r</Value></Password>", "<Password><Value><secret></Value></Password>")]
    [InlineData("{\"FileName\":\"C:/tools/\",\"Arguments\":\"-P x\"}", "{\"FileName\":\"<path>\",\"Arguments\":\"-P x\"}")]
    [InlineData("FileName  : \nArguments : -P x", "FileName  : \nArguments : -P x")]
    public void XmlAndProcessRecordsNeedTheirOwnContext(string input, string expected) => Assert.Equal(expected, Plain().Redact(input).Text);
}
