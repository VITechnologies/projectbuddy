namespace ProjectBuddy.Wiki;

// The ecosystem wiki standard this checker implements, pinned to the RSR release that ships it
// (docs/architecture/ecosystem-wiki-standard.md). Updating the pin is a deliberate change: the finding catalogues below
// are copied from that release, and a test fails when the installed functions' findings differ from them, catalogue by
// catalogue and in order.
public static class Standard
{
    public const string Id = "rsr/ecosystem-wiki-standard@8";

    public const string RsrRelease = "v0.12.0";

    public const string RsrCommit = "49dfa312040b338a61b95ad9022f1ddddd318ab1";

    // Each catalogue is ordered: the first finding that applies names a page or edge.
    public static readonly string[] PageFindings =
    [
        "unexpected-file", "not-utf8", "no-id-heading", "not-canonical", "id-does-not-match-path", "id-outside-scope",
        "id-too-long", "unexpected-sections", "unexpected-page-kind", "text-too-long",
    ];

    public static readonly string[] ArchitectureFindings =
    [
        "unexpected-file", "not-utf8", "no-title", "no-final-newline", "not-canonical", "misplaced-index-marker", "dangling-child",
    ];

    public static readonly string[] EdgeFindings = ["link-outside-field", "link-outside-scope", "dangling-link"];

    public static readonly string[] ProfileFindings = ["invalid-profile"];

    public static readonly string[] WikiFindings = ["not-utf8", "page-not-indexed", "page-over-budget"];
}
