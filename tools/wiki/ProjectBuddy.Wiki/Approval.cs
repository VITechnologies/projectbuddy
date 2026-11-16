using System.Text.Json;
using System.Text.Json.Nodes;

namespace ProjectBuddy.Wiki;

// The source identity of an operator-approved rsr/source-authority-proposal@1 record. The registry itself refuses a
// manifest it has not admitted, so this only reads the identity and insists on the approval.
internal sealed record Approval(string SourceUuid, string ConstellationUuid, string ManifestDigest, string SourceRef)
{
    public static Approval Read(string path)
    {
        JsonNode? record;
        try
        {
            record = JsonNode.Parse(File.ReadAllText(path));
        }
        catch (JsonException)
        {
            throw new WikiException(ExitCodes.Refused, "the approval record is not JSON");
        }

        if (Field(record, "approval_status") != "APPROVED")
        {
            throw new WikiException(ExitCodes.Refused, "the approval record is not APPROVED");
        }

        return new(
            Field(record, "authority_manifest", "source_uuid"),
            Field(record, "authority_manifest", "constellation_uuid"),
            Field(record, "authority_manifest_digest"),
            Field(record, "source_lock", "accepted_ref"));
    }

    private static string Field(JsonNode? node, params string[] path)
    {
        foreach (var name in path)
        {
            node = node is JsonObject item ? item[name] : null;
        }

        return node is JsonValue value && value.TryGetValue<string>(out var text)
            ? text
            : throw new WikiException(ExitCodes.Refused, $"the approval record has no {string.Join('.', path)}");
    }
}
