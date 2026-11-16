# `tools/wiki/`: `projectbuddy-wiki`

`projectbuddy-wiki` keeps a local working copy of the projectbuddy GitHub wiki
in PostgreSQL. It is slice 1
([#308](https://github.com/VITechnologies/projectbuddy/issues/308)) of
[#307](https://github.com/VITechnologies/projectbuddy/issues/307), local
AI-assisted generation and regeneration of the wiki.

The published wiki is canonical. The database is a rebuildable copy that seeds
itself from the wiki's committed bytes. It renders the wiki back byte for
byte, so later slices can build projections and AI drafts on it without
re-reading the wiki. Rebuilding the copy loses nothing: the wiki still holds
every byte.

It is a .NET 10 command-line tool. The logic lives in PL/pgSQL procedures in
the schema `wiki_v1` ([`Sql/0001_wiki_v1.sql`](ProjectBuddy.Wiki/Sql/0001_wiki_v1.sql)),
and the CLI only reads git and moves bytes.

```bash
dotnet build ProjectBuddy.Wiki -c Release -o out
out/projectbuddy-wiki migrate --create-database --port 55432 --password-file "$HOME/.config/<instance>/postgres-password"
out/projectbuddy-wiki seed --wiki "$HOME/repos/projectbuddy.wiki" --port 55432 --password-file ...
out/projectbuddy-wiki check --wiki "$HOME/repos/projectbuddy.wiki" --port 55432 --password-file ...
out/projectbuddy-wiki render --out /tmp/wiki-render --port 55432 --password-file ...
```

## Commands

Every command prints one JSON object on standard output.

| Command | What it does |
| --- | --- |
| `migrate [--create-database]` | Applies the embedded migrations in order. With `--create-database` it creates the database first if it is missing. |
| `seed --wiki <clone>` | Records the clone's `HEAD` commit. A recorded commit changes nothing (`up-to-date`). Otherwise only bytes the database does not hold yet are stored. The result counts what was added, modified and deleted since the previous state. |
| `check --wiki <clone>` | Compares the clone's `HEAD` tree with the latest recorded state, byte for byte. It lists at most 100 differences plus their total. Drift exits 1. |
| `render --out <dir> [--commit <sha>]` | Writes the latest recorded state, or the state of a recorded commit, as files. |

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
| 1 | `check` found drift |
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

## Round-trip proof

On the workstation, against the projectbuddy wiki (352 files, 593,340 bytes):

1. `seed` recorded every file;
2. a second `seed` reported `up-to-date`;
3. `check` found no drift;
4. `render` into a new directory matched `git archive HEAD` byte for byte;
5. `render` into the recorded clone left every file unchanged;
6. `render` into the clone's `.git`, and into the projectbuddy repository, was
   refused (exit 3), and `git fsck` of the clone stayed clean.

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
- concurrent first migrations.

CI runs them as `Wiki Tool Tests` against the same pinned
`postgres:16-alpine` image, at 100% line and branch coverage.
