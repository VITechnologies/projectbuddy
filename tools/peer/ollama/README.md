# `ollama/`: shared Ollama guard library

`ProjectBuddy.PeerOllama` holds the code that every ProjectBuddy tool talking
to a local Ollama must share, so the guards cannot drift between copies. It is
used by [`../bench`](../bench/README.md) (#217),
[`../advise`](../advise/README.md) (#284), and `projectbuddy-ask`'s `infer`
verb ([`../README.md`](../README.md), #358).

- **`OllamaClient`**
  - Accepts only `http://` plus a loopback IP literal and a port (#210 D2).
    `LoopbackHandler()` ignores proxy settings and refuses redirects.
  - Any server or transport fault becomes exit 5. It is never scored as model
    output.
  - `ChatRequest` fixes `num_ctx`, `num_predict` and the seed, and always sets
    temperature 0.
- **`TruncationGuard`**
  - Ollama keeps only the last `num_ctx/2 + 2` tokens of an oversized prompt,
    without reporting it.
  - `ProbeAsync` proves the server still behaves that way; otherwise it
    exits 4.
  - `Fits` accepts a reply only when `0 < prompt_eval_count < num_ctx/2` and
    the reply fits in the context.
- **`ModelPin`**: a `name:tag` plus a full 64-hex manifest digest.
  `RequireInstalledAsync` refuses (exit 3) unless every pinned model is
  installed with exactly that digest.
- **`Json`**: strict accessors over `System.Text.Json`. Duplicate keys,
  missing keys and wrong types are refused, while `TryObject`/`TryString`
  read untrusted model output without throwing.
- **`ExitCodes`**: 2 usage, 3 refused, 4 guard broken, 5 unreachable. Tools
  add their own codes above these.

The library has its own tests with a 100% line and branch gate in CI. Each
tool's coverage gate measures only that tool's assembly.
