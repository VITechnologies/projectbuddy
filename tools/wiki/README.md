# `tools/wiki/`: `projectbuddy-wiki`

`projectbuddy-wiki` keeps a local working copy of the projectbuddy GitHub wiki
in PostgreSQL. It also projects the requirement and test pages into the RSR
requirement registry. It serves
[#307](https://github.com/VITechnologies/projectbuddy/issues/307), local
AI-assisted generation and regeneration of the wiki:
- slice 1, [#308](https://github.com/VITechnologies/projectbuddy/issues/308):
  the working copy;
- slice 2, [#309](https://github.com/VITechnologies/projectbuddy/issues/309):
  the governed projection, and its export to the RSR registry.

The published wiki is canonical. The database is a rebuildable copy that seeds
itself from the wiki's committed bytes. It renders the wiki back byte for
byte, so later slices can build projections and AI drafts on it without
re-reading the wiki. Rebuilding the copy loses nothing: the wiki still holds
every byte.

It is a .NET 10 command-line tool. The logic lives in PL/pgSQL procedures in
the schema `wiki_v1`:
- [`Sql/0001_wiki_v1.sql`](ProjectBuddy.Wiki/Sql/0001_wiki_v1.sql): the
  working copy;
- [`Sql/0002_ingest_parent_check.sql`](ProjectBuddy.Wiki/Sql/0002_ingest_parent_check.sql):
  a faster file-and-directory check for `ingest_snapshot`;
- [`Sql/0003_wiki_projection.sql`](ProjectBuddy.Wiki/Sql/0003_wiki_projection.sql):
  the projection;
- [`Sql/0004_architecture_projection.sql`](ProjectBuddy.Wiki/Sql/0004_architecture_projection.sql):
  the Architecture Packet projection
  ([#315](https://github.com/VITechnologies/projectbuddy/issues/315)).

The CLI only reads git and moves bytes.

```bash
dotnet build ProjectBuddy.Wiki -c Release -o out
out/projectbuddy-wiki migrate --create-database --port 55432 --password-file "$HOME/.config/<instance>/postgres-password"
out/projectbuddy-wiki seed --wiki "$HOME/repos/projectbuddy.wiki" --port 55432 --password-file ...
out/projectbuddy-wiki check --wiki "$HOME/repos/projectbuddy.wiki" --port 55432 --password-file ...
out/projectbuddy-wiki render --out /tmp/wiki-render --port 55432 --password-file ...
out/projectbuddy-wiki project --port 55432 --password-file ...
out/projectbuddy-wiki export-registry --approval <approval record> --importer-password-file <path> --port 55432 --password-file ...
```

## Commands

Every command prints one JSON object on standard output. Every command but
`migrate` first checks that the database has all the migrations this tool
embeds, and otherwise refuses with "run migrate" (exit 3).

| Command | What it does |
| --- | --- |
| `migrate [--create-database]` | Applies the embedded migrations in order. With `--create-database` it creates the database first if it is missing. |
| `seed --wiki <clone>` | Records the clone's `HEAD` commit. A recorded commit changes nothing (`up-to-date`). Otherwise only bytes the database does not hold yet are stored. The result counts what was added, modified and deleted since the previous state. |
| `check --wiki <clone>` | Compares the clone's `HEAD` tree with the latest recorded state, byte for byte. It lists at most 100 differences plus their total. Drift exits 1. |
| `render --out <dir> [--commit <sha>]` | Writes the latest recorded state, or the state of a recorded commit, as files. |
| `export-registry --approval <file> --importer-password-file <path> [--importer <name>] [--registry-database <name>] [--commit <sha>]` | Exports the latest recorded state, or a recorded commit, to the RSR registry; see [Registry export](#registry-export). |
| `project [--commit <sha>]` | Reports the [governed projection](#governed-projection) of the latest recorded state, or of a recorded commit: pages per scope and kind, trace edges per type, and at most 100 pages or links that cannot be exported, plus their total. Any such page or link exits 1. |

`seed` and `check` read the **committed** tree (`git ls-tree` and
`git cat-file`), not the working tree. Uncommitted edits in the clone are
ignored, and line-ending or filter settings cannot change the bytes. Only
regular files (`100644`) whose paths are valid UTF-8 are accepted; a symbolic
link, executable, submodule or empty tree is refused.

Git runs with `GIT_CONFIG_NOSYSTEM=1` and a 120-second timeout, with every
inherited `GIT_*` variable removed. An inherited `GIT_DIR` or `GIT_WORK_TREE`
would otherwise make git answer for a different repository.

### Render targets

`render` writes only into a target that is one of:

- **missing**: it is created (`"target": "new"`);
- **an empty directory** (`"empty"`);
- **the top of a git clone whose `HEAD` is a commit this database recorded**
  (`"clone"`). The target must have its own `.git` directory and be the top of
  its work tree. Together with the recorded commit, this proves the target is
  this wiki, and not another repository, a clone's `.git`, or a subdirectory.
  Pruning therefore cannot touch anything else. A linked work tree, whose
  `.git` is a file, is refused.

Anything else is refused. That includes a file, a target that is a symbolic
link, and any symbolic link inside the target.

In a target, `render` first deletes the files the state does not hold
(outside the top-level `.git`) and removes directories left empty. Then it
writes only the files whose bytes differ, so a file can replace a directory
of the same name and the reverse. **Uncommitted edits in a recorded clone are
overwritten**; render into a clone only to reset it to the recorded state.

## Connection

| Option | Default |
| --- | --- |
| `--password-file <path>` | required, fully qualified; trailing CR/LF are trimmed |
| `--host` | `127.0.0.1` |
| `--port` | `5432` |
| `--user` | `postgres` |
| `--database` | `wiki_local` |
| `--git` | `/usr/bin/git` (fully qualified) |

The password is read from a file and never taken from the command line.
`--user` and `--database` must be plain identifiers (`[a-z_][a-z0-9_]*`). All
paths must be fully qualified.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | done; `check` found no drift |
| 1 | a finding: `check` found drift, or `project` found pages or links that cannot be exported |
| 2 | usage error |
| 3 | refused (git, the clone, the render target, an unrecorded commit, or a file-system error) |
| 5 | database error |

## Schema `wiki_v1`

- **`blobs`**: each distinct content once, keyed by its SHA-256. The database
  checks that the key matches the bytes.
- **`snapshots`**: a state of the wiki, keyed by its digest. The digest is the
  SHA-256 of `path<TAB>sha256<LF>` lines in byte order of the path, so the
  same files always give the same digest.
- **`snapshot_files`**: the paths of a snapshot. A path must be relative, with
  no empty, `.` or `..` segment, no `.git` segment in any case, no backslash or
  control character, and at most 512 bytes. A snapshot cannot hold both a
  file `x` and a path under `x/`. Every state can therefore be written as
  files, and nothing renders into a git directory.
- **`snapshot_commits`**: the wiki commits recorded, in order. A commit
  recorded again with different content is refused.

Every table is append-only: triggers refuse `UPDATE`, `DELETE` and
`TRUNCATE`, so history is never rewritten. Two commits with the same files
share one snapshot (`"status": "reused"`).

| Procedure | Returns |
| --- | --- |
| `ingest_snapshot(commit, paths, contents)` | the snapshot, its digest, whether it was reused, and the number of new blobs; takes an advisory lock |
| `latest_snapshot()` | the snapshot of the most recently recorded commit |
| `commit_snapshot(commit)` | the snapshot a commit recorded |
| `export_snapshot(snapshot)` | every path with its bytes |
| `changed_paths(from, to)` | each added, modified or deleted path |

Migrations are recorded in `public.wiki_schema_migrations` with the SHA-256 of
their text. A migration edited after it was applied is refused, so a database
can never silently differ from the source. Advisory locks serialize
concurrent `migrate` runs, including the first one, which creates the
database.

## Governed projection

The files under three directories are governed. Each directory holds one
registry scope:

| Directory | Scope | IDs | Sections, in order | Marker kind |
| --- | --- | --- | --- | --- |
| `system-requirements/` | system | `SYS-REQ-NNN` | Requirement, Rationale, Fit Criterion, Verification, Source, Priority, TestID, TestArtifact, CodeRef, Status | `retired` |
| `software-requirements/` | software | `REQ-NNN` | the same | `retired` |
| `tests/` | test | `TEST-NNN` | Stimulus, Required observation | `stub` |

`wiki_v1.parse_page` accepts exactly one layout, and `wiki_v1.render_page`
writes it. Rendering a parsed page therefore always gives its bytes back, and
#310's drafts can edit fields and render them deterministically. A page with
sections is:

```text
# <id>[: <title>]

## <heading>

<body>

## <heading>

<body>
```

It ends with one newline. A body may be empty or hold any text that is not
another `\n\n## ` heading. A marker page is
`# <id>[: <title>]\n\n<!-- page-kind: <kind> -->\n<body>\n`. `sections` is
never a marker kind, because it names the layout with sections.

The projection is computed from the stored bytes by immutable functions and
is never stored beside them, so it cannot disagree with them. The governed
functions are:
- `governed_pages(snapshot)`: each page's kind, sections and registry text, or
  its problem;
- `governed_edges(snapshot)`: each trace edge, or its problem;
- `projection_report(snapshot)`: the summary that `project` prints.

A page that cannot be governed is listed with one of these problems, never
dropped:

| Problem | Meaning |
| --- | --- |
| `unexpected-file` | not `<directory>/<name>.md` |
| `not-utf8` | the bytes are not UTF-8 |
| `no-id-heading` | no `# <ID>` first line |
| `not-canonical` | not the canonical layout |
| `id-does-not-match-path` | the heading's ID is not the file name |
| `id-outside-scope` | the ID's prefix belongs to another directory |
| `id-too-long` | the ID exceeds the registry's 128 characters |
| `unexpected-sections` | the headings differ from the directory's |
| `unexpected-page-kind` | a marker kind other than the directory's |
| `text-too-long` | the registry text exceeds the registry's 16,384 characters |

### Architecture Packet

`Architecture-Packet.md` and the `architecture/*.md` pages have one canonical
layout of their own. `wiki_v1.parse_architecture_page` and
`wiki_v1.render_architecture_page` give the bytes back, so #310's drafts can
edit a page's title, body or children and render it deterministically.

A page is `# <title>\n\n<body>\n`. A **hub** ends instead with the index
block that `scripts/Assemble-WikiSpec.py` fills:
`\n\n<!-- sections -->\n`, then `- [[<child>]]\n` per child, then
`<!-- /sections -->\n`. A hub may have no body, and table markers stay in the
body.

These pages have no registry scope. `project` reports them under
`architecture`, with the counts of pages and hubs, and lists their problems
under `architectureProblems`. Those problems are findings (exit 1), but they
never keep the requirement and test pages out of the registry.

| Problem | Meaning |
| --- | --- |
| `unexpected-file` | not `Architecture-Packet.md` or `architecture/<name>.md` |
| `not-utf8` | the bytes are not UTF-8 |
| `no-title` | no `# <title>` line followed by a blank line |
| `not-canonical` | a title with nothing after it |
| `no-final-newline` | the page does not end with a newline |
| `misplaced-index-marker` | a `<!-- sections -->` or `<!-- /sections -->` marker outside a hub's final index block, which needs a blank line before it |
| `dangling-child` | a hub child names no Markdown page outside dot directories, the pages `Assemble-WikiSpec.py` resolves |

### Trace edges

Edges come from wiki links: `[[ID]]`, `[[ID|label]]` or `[[ID#section]]`.

| Edge | From the field | Held by the scope |
| --- | --- | --- |
| `TEST verifies SYS-REQ` or `REQ` | the requirement's `TestID` | test |
| `SYS-REQ allocates REQ` | the system requirement's `CodeRef` | system |
| `REQ supersedes` a retired `REQ`; `SYS-REQ supersedes` a retired `SYS-REQ` | the retired page's links | the page's own |

An edge is a problem when:
- its link has the wrong kind for its field (`link-outside-field`);
- a retired page links another scope, or links itself (`link-outside-scope`);
- either end is not a governed page (`dangling-link`).

### Registry envelopes

`wiki_v1.registry_envelope(commit, scope, identity, targets)` builds the
`rsr/registry-snapshot@1` envelope that RSR's
`registry_v1.ingest_snapshot(jsonb)` accepts:
- **Nodes:** one per page. A retired page's `status` is `superseded`; every
  other page is `active`. Its `text` follows RSR's convention: the page's
  fields (`ID`, `Title`, its sections, or `Page kind` and `Body`) as compact
  JSON with keys in code-point order, byte-identical to Python
  `json.dumps(..., sort_keys=True, separators=(",", ":"), ensure_ascii=False)`.
  Its `content_digest` is that text's SHA-256, and its `location` is the page's
  path.
- **Ingest order:** an edge's two ends must already be registry nodes, so the
  scopes are ingested in the order software, then system, then test. `targets`
  names the registry snapshots that a scope's edges point into: software needs
  none, system needs `software`, and test needs `software` and `system`.
- **Identity:** `identity` names the registered source: `source_uuid`,
  `constellation_uuid`, `manifest_digest` and `source_ref`. The wiki
  publishes on `master`.
- **Idempotency:** the registry's idempotency key includes the
  `tree_digest`. That is the SHA-256 of
  `projectbuddy/wiki-scope-tree@1\n<scope>\n<parser version>\n`, followed by
  `<path>\t<sha256>\n` for each of the scope's files and
  `target\t<scope>\t<uuid>\n` for each target. A changed page or target
  therefore gives a new key. The commit is not part of the key.
- **Reuse before ingesting:** a later commit with the same governed content
  has the same key but a different `source_revision`, and the registry
  refuses a changed payload under an existing key. The exporter therefore
  looks the key up first and reuses the existing snapshot, as RSR's
  `registry_local_bootstrap.py` does. That snapshot keeps the revision that
  first introduced its content.
- **Fail closed:** any page or edge problem refuses every scope. The registry
  never holds part of a wiki. An envelope over the registry's limits (10,000
  nodes, 50,000 edges or 16 MiB) is refused too.

## Registry export

`export-registry` writes the governed projection into RSR's `registry_v1`, by
default the database `registry_local` on the same server. A run goes like
this:

1. **Refuse a wiki that cannot be exported.** If `project` would list any
   problem, the export stops before any registry call.
2. **Read the source identity** from an operator-approved
   `rsr/source-authority-proposal@1` record (`--approval`):
   - `approval_status` must be `APPROVED`;
   - `authority_manifest.source_uuid` and `constellation_uuid`,
     `authority_manifest_digest`, and `source_lock.accepted_ref` give the
     identity.

   The registry refuses a manifest it has not admitted
   (`source_authority_not_admitted`, exit 5). The approval is the one-time
   registration of the source: it authorizes the `master` ref, not one
   revision, so later wiki commits are exported under it.
3. **Export the scopes in the order software, system, test.** For each scope,
   the run builds the envelope, looks up its content key, and then either
   reuses the snapshot that holds it or ingests a new one. The next scope's
   edges point into that snapshot.
   - The lookup reads as the `--user` login. That is the administrator, as in
     RSR's own bootstrap, because the importer role cannot read snapshots
     (svelderrainruiz/repo-standards-review work item 2).
   - The ingest runs as `--importer` (default `registry_importer_local`), with
     its own password file. It needs an active importer authorization for the
     constellation.

The result names each scope's registry snapshot, whether it was reused, and
its node and edge counts. Exporting unchanged content again, even from a later
commit, reuses all three snapshots. A changed requirement gives a new software
snapshot, and new system and test snapshots, because their edges point into
it.

## Round-trip proof

On the workstation, against the projectbuddy wiki (352 files, 593,340 bytes):

1. `seed` recorded every file;
2. a second `seed` reported `up-to-date`;
3. `check` found no drift;
4. `render` into a new directory matched `git archive HEAD` byte for byte;
5. `render` into the recorded clone left every file unchanged;
6. `render` into the clone's `.git`, and into the projectbuddy repository, was
   refused (exit 3), and `git fsck` of the clone stayed clean.

At wiki commit `d21fe10`, `project` reported in 0.28 s:
- **Pages:** 35 system pages; 132 software pages plus 1 retired; 95 test
  pages plus 38 stubs.
- **Edges:** 243 `verifies`, 127 `allocates` and 1 `supersedes`, with no
  problems.
- **Round trip:** all 301 pages re-rendered byte for byte, and every registry
  text matched Python's `json.dumps`.
- **Architecture Packet:** 43 pages, 6 of them hubs. The 34 canonical pages
  re-render byte for byte. The 9 `Native-Qualification-*` pages are listed as
  `no-final-newline`.

RSR's migrations at `1ac2f2a`, applied to a scratch database with a
synthetic source (`allowed_refs: ["master"]`), gave this:
- **Ingest:** `ingest_snapshot` accepted all three envelopes (software 301 KB).
  A second run reused all three.
- **Queries:** `find_requirements` found the items by ID and by text, and
  `dependency_lineage` from `SYS-REQ-001` followed `allocates` and `verifies`
  edges.

## Tests

The tests need a PostgreSQL 16 server. Each test creates its own
`wiki_test_<guid>` database and drops it afterwards.

```bash
export PBW_TEST_PASSWORD_FILE=/path/to/password PBW_TEST_PORT=55432 # PBW_TEST_HOST and PBW_TEST_USER are optional
dotnet test ProjectBuddy.Wiki.Tests --settings coverage.runsettings --collect:"XPlat Code Coverage"
```

They use temporary git repositories and a fake `git` for failures and
timeouts. They cover:
- every command and option, and every refusal;
- byte-exact round trips, including files without a final newline;
- deduplication, append-only history and the digest;
- migration tampering;
- every render target and pruning, including a clone's `.git`, its
  subdirectories, and a file replacing a directory and the reverse;
- an inherited `GIT_DIR`, and paths that are not valid UTF-8;
- concurrent first migrations;
- every page kind and edge type;
- every projection problem, the envelopes of each scope with their exact node
  texts, and every envelope refusal;
- the export into a registry:
  - ingest, then reuse when the content is unchanged, including from a later
    commit;
  - the new snapshots that follow a changed requirement, and their edges;
  - every refusal: of the approval record, of an unadmitted manifest, and of
    a wiki with problems.

The registry tests also need `PBW_TEST_REGISTRY_MIGRATIONS`, which names a
directory of SQL files that build `registry_v1`. It can be either of two:
- **RSR's own `db/registry-v1`**, the real registry;
- **the contract stub**,
  [`RegistryContract/`](ProjectBuddy.Wiki.Tests/RegistryContract/registry_v1_contract.sql).
  It holds only the tables and `ingest_snapshot` rules that the export relies
  on, with RSR's error messages.

RSR is private, so public CI uses the stub. The same tests pass against RSR
`v0.4.2`'s migrations, and the live export to `registry_local` is checked
with RSR's own `find_requirements` and `dependency_lineage`.

CI runs all the tests as `Wiki Tool Tests`:
- against the same pinned `postgres:16-alpine` image;
- at 100% line and branch coverage.
