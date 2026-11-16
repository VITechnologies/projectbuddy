using ProjectBuddy.PeerRedact;

namespace ProjectBuddy.PeerProbes;

// The redactor every peer tool result passes through: the operator's deny list plus the host's own machine and account
// names, which are always private. A deny list that cannot be read refuses rather than redacting with fewer terms.
public static class HostRedactor
{
    public static Redactor Load(string? denyList, string machine, string user)
    {
        if (denyList is null)
        {
            return Redactor.ForHost([], machine, user);
        }

        byte[] bytes;
        try
        {
            bytes = File.ReadAllBytes(denyList);
        }
        catch (Exception error)
        {
            throw new RedactionException($"the deny list cannot be read: {error.GetType().Name}");
        }

        return Redactor.ForHost(Redactor.ParseDenyList(bytes), machine, user);
    }
}
