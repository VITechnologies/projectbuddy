#!/usr/bin/env python3
"""Harness for north-star A4-r1 (VITechnologies/projectbuddy#379, #337).

Subcommands: select, workspace, packets, analyse. The design is frozen in
https://github.com/VITechnologies/projectbuddy/issues/337#issuecomment-7012817917
"""
import argparse
import hashlib
import itertools
import json
import os
import random
import re
import shutil
import subprocess
import sys
from pathlib import Path

MEMBER_COMMIT = "6a9ad5cc6bf9751ea3d80467840ab6dd7e48e3f2"
WIKI_COMMIT = "a1e6f4c"
BRIEF_COMMIT = "96494e6"  # the prose scenario of each page, before generation
BLOCKED = {"TEST-127", "TEST-128"}
SEED = 379
PAGES_PER_BLOCK = 6
MARGIN = 0.20
ITEM_CAP = 12  # A4-r2 section 4
ALPHA = 0.025
KIT = {
    "control": "v0.9.0",  # A4-r2: the @5-era standard, which has no observation check
    "treatment": "v0.12.0",
}


def sh(*args, cwd=None, check=True, text=True):
    return subprocess.run(args, cwd=cwd, check=check, capture_output=True, text=text).stdout


def eligible(wiki: Path, member: Path):
    link = {}
    for f in (wiki / "software-requirements").glob("*.md"):
        t = f.read_text()
        m = re.search(r"## TestID\n\n(.*?)\n\n", t, re.S)
        a = re.search(r"## TestArtifact\n\n(.*?)\n\n## ", t, re.S)
        if not m:
            continue
        files = set(re.findall(r"`(scripts/tests/[\w./-]+\.py)`", a.group(1))) if a else set()
        files = {x for x in files if (member / x).exists()}
        for i in re.findall(r"\[\[(TEST-[\w-]+)\]\]", m.group(1)):
            link.setdefault(i, set()).update(files)
    return sorted(t for t, f in link.items() if f and t not in BLOCKED)


def draw(pop):
    rng = random.Random(SEED)
    pages = sorted(pop)
    rng.shuffle(pages)
    sample = pages[:18]
    blocks = [sample[i * PAGES_PER_BLOCK:(i + 1) * PAGES_PER_BLOCK] for i in range(3)]
    order = []
    for _ in blocks:
        arms = ["control", "treatment"]
        rng.shuffle(arms)
        order.append(arms)
    return {"blocks": blocks, "order": order, "unsampled": pages[18:]}


def cmd_select(a):
    d = draw(eligible(Path(a.wiki), Path(a.member)))
    digest = hashlib.sha256(json.dumps({"blocks": d["blocks"], "order": d["order"]}, sort_keys=True).encode()).hexdigest()
    d["digest"] = digest
    print(json.dumps(d, indent=1))


def brief(wiki_git: Path, page: str):
    t = sh("git", "-C", str(wiki_git), "show", f"{BRIEF_COMMIT}:tests/{page}.md")
    m = re.search(r"## Stimulus\n\n(.*?)\n\n## ", t, re.S)
    return m.group(1).strip()


CONTROL_GATE = """#!/bin/sh
# Standard @5 has no observation check: this checks only the page's shape.
cd "$(dirname "$0")" || exit 2
rc=0
for id in "$@"; do
  f=wiki/tests/$id.md
  grep -q '^## Stimulus' "$f" && grep -q '^## Required observation' "$f" && grep -q '^1\\. ' "$f" && ! grep -q '(to be written)' "$f" \\
    && grep -q '{commit_short}' "$f" || {{ echo "FINDING: $f: needs numbered Stimulus steps, a Required observation, and the member commit"; rc=1; }}
done
[ $rc -eq 0 ] && echo "PASS: shape only (standard @5 has no observation check)"
exit $rc
"""

GATE = """#!/bin/sh
cd "$(dirname "$0")" || exit 2
git -C wiki add -A >/dev/null 2>&1
git -C wiki -c user.name=author -c user.email=author@example.invalid commit -qm "author" >/dev/null 2>&1
exec python3 kit/observations.py check --wiki wiki --member-commit {commit} --evidence evidence.json{extra} "$@"
"""

TASK = """You are authoring test pages for the projectbuddy wiki. Work only inside this directory.

Directory:
- `member/`: the projectbuddy source at the member commit `{commit}`. Read it; do not change it. Do not run its tests: `evidence.json` already records the results of its test run at that commit.
- `wiki/`: the wiki, a git repository. The test pages to author are `wiki/tests/{first}.md` and the five other pages named in `PAGES.md`.
- `standard.md`: the ecosystem wiki standard that the pages must follow. Read its Test Page Content section and the sections it points to.
- `gate.sh`: the observation check. `./gate.sh TEST-NNN` checks the named pages. Fix what it reports. Give each page at most 3 gate rounds.

For each page in `PAGES.md`:
1. Read the page's current Stimulus: it is a short brief of what the test exercises. Its Required observation is empty.
2. Read the requirements whose `TestID` section links the page (`wiki/software-requirements/` and `wiki/system-requirements/`), and the tests and code they name in `member/`.
3. Write the page: a Stimulus and a Required observation for it, as the standard requires, for those requirements. Keep each section on a single line per paragraph or list item, as the existing pages do.
4. Run `./gate.sh TEST-NNN`, fix the findings, and repeat up to the limit.

Rules: write only to `wiki/tests/`. Do not use the network, and do not read anything outside this directory. Do not write to any memory or notes store. When all pages are written, reply with `DONE` and one line per page naming its last gate result.
"""


def cmd_workspace(a):
    out = Path(a.out)
    arm = a.arm
    pages = a.pages.split(",")
    assert arm in KIT
    if out.exists():
        raise SystemExit(f"{out} exists")
    out.mkdir(parents=True)
    repo = Path(a.repo)
    commit = sh("git", "-C", str(repo), "rev-parse", "HEAD").strip()
    assert commit == MEMBER_COMMIT, f"member repo is at {commit}"
    member = out / "member"
    if arm == "treatment":
        sh("git", "clone", "-q", "--no-hardlinks", str(repo), str(member))
        sh("git", "-C", str(member), "checkout", "-q", MEMBER_COMMIT)
        sh("git", "-C", str(member), "remote", "remove", "origin")
    else:
        member.mkdir()
        tar = subprocess.run(["git", "-C", str(repo), "archive", MEMBER_COMMIT], capture_output=True, check=True).stdout
        subprocess.run(["tar", "-x", "-C", str(member)], input=tar, check=True)
        for p in (".repo-standards-review", "CHANGELOG.md", "docs/architecture/standards-compliance.md", "tools/wiki", "tools/peer"):
            q = member / p
            if q.is_dir():
                shutil.rmtree(q)
            elif q.exists():
                q.unlink()
    src = Path(a.wiki)
    wiki = out / "wiki"
    tmp = out / "wiki-src"
    sh("git", "clone", "-q", str(src), str(tmp))
    sh("git", "-C", str(tmp), "checkout", "-q", WIKI_COMMIT)
    briefs = {p: brief(tmp, p) for p in pages}
    shutil.rmtree(tmp / ".git")
    shutil.rmtree(tmp / "tests")
    tmp.rename(wiki)
    (wiki / "tests").mkdir()
    for p in pages:
        (wiki / "tests" / f"{p}.md").write_text(f"# {p}\n\n## Stimulus\n\n{briefs[p]}\n\n## Required observation\n\n(to be written)\n")
    sh("git", "init", "-q", cwd=wiki)
    sh("git", "add", "-A", cwd=wiki)
    sh("git", "-c", "user.name=a4", "-c", "user.email=a4@example.invalid", "commit", "-qm", "workspace", cwd=wiki)
    (out / "kit").mkdir()
    kit_dir = Path(a.rsr) / "docs/architecture"
    tag = KIT[arm]
    kit_py = repo / ".repo-standards-review/observations.py"
    if arm == "treatment":
        (out / "kit/observations.py").write_text(kit_py.read_text())
    std = sh("git", "-C", a.rsr, "show", "v0.9.0:docs/architecture/ecosystem-wiki-standard.md") if arm == "control" else (kit_dir / "ecosystem-wiki-standard.md").read_text()
    (out / "standard.md").write_text(std)
    sh("python3", str(kit_py), "evidence", "--format", "pytest-junit", "--report", a.junit, "--platform", "linux-x64", "--root", str(repo), "--output", "evidence.json", cwd=out)
    extra = " --anchored --member-root member" if arm == "treatment" else ""
    if arm == "control":
        (out / "gate.sh").write_text(CONTROL_GATE.format(commit_short=MEMBER_COMMIT[:7]))
    else:
        (out / "gate.sh").write_text(GATE.format(commit=MEMBER_COMMIT, extra=extra))
    (out / "gate.sh").chmod(0o755)
    (out / "PAGES.md").write_text("\n".join(f"- {p}" for p in pages) + "\n")
    task = TASK.format(commit=MEMBER_COMMIT, first=pages[0])
    (out / "TASK.md").write_text(task)
    (out / "task.sha256").write_text(hashlib.sha256(TASK.encode()).hexdigest() + "\n")
    print(json.dumps({"workspace": str(out), "arm": arm, "kit": tag, "pages": pages}))


def cmd_packets(a):
    """Blind the authored pages: random IDs, no arm, no session, no gate output."""
    runs = json.loads(Path(a.runs).read_text())  # [{"workspace":..., "arm":..., "block":...}]
    rng = random.Random(a.seed)
    items = []
    for r in runs:
        w = Path(r["workspace"]) / "wiki"
        for f in sorted((w / "tests").glob("TEST-*.md")):
            items.append({"page": f.stem, "arm": r["arm"], "block": r["block"], "text": f.read_text()})
    rng.shuffle(items)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    key = {}
    for i, it in enumerate(items):
        pid = f"P{i + 1:03d}"
        key[pid] = {k: it[k] for k in ("page", "arm", "block")}
        (out / f"{pid}.md").write_text(it["text"])
        obs = it["text"].split("## Required observation", 1)[-1]
        items = [l for l in obs.split("\n") if re.match(r"- `[^`]+`( ›|$)", l)]
        srng = random.Random(f"{SEED}-{pid}")
        picked = items if len(items) <= ITEM_CAP else [items[i] for i in sorted(srng.sample(range(len(items)), ITEM_CAP))]
        (out / f"{pid}.sample.json").write_text(json.dumps({"items_total": len(items), "sampled_items": picked}, indent=1))
    (out / "KEY.json").write_text(json.dumps(key, indent=1, sort_keys=True))
    print(f"{len(items)} packets in {out}")


def signflip_p(d, delta=0.0):
    """One-sided exact sign-flip p for H0: mean(d) <= -delta."""
    x = [v + delta for v in d]
    obs = sum(x)
    n = len(x)
    ge = 0
    for s in itertools.product((1, -1), repeat=n):
        if sum(si * xi for si, xi in zip(s, x)) >= obs - 1e-12:
            ge += 1
    return ge / 2 ** n


def boot_ci(d, reps=10000, seed=SEED):
    rng = random.Random(seed)
    n = len(d)
    m = sorted(sum(rng.choice(d) for _ in range(n)) / n for _ in range(reps))
    return m[int(0.025 * reps)], m[int(0.975 * reps)]


def cmd_analyse(a):
    key = json.loads(Path(a.key).read_text())
    e1 = json.loads(Path(a.e1).read_text())  # {packet: [{"final": "supported"|"unsupported"}]}
    e2 = json.loads(Path(a.e2).read_text())  # {packet: [{"ref": id, "final": "retained"|"partial"|"absent"}]}
    samples = {pid: json.loads((Path(a.packets) / f"{pid}.sample.json").read_text()) for pid in key}
    page = {}
    for pid, k in key.items():
        page.setdefault(k["page"], {})[k["arm"]] = pid
    rows = []
    for p, arms in sorted(page.items()):
        if len(arms) < 2:
            continue
        row = {"page": p}
        for arm, pid in arms.items():
            c = e1.get(pid, [])
            audited_items = sum(1 for x in c if x.get("stratum") == "item")
            w_item = samples[pid]["items_total"] / audited_items if audited_items else 0.0
            wt = lambda x: w_item if x.get("stratum") == "item" else 1.0
            n = sum(wt(x) for x in c)
            u = sum(wt(x) for x in c if x["final"] == "unsupported")
            r = e2.get(pid, [])
            m = len(r)
            kept = sum(1 for x in r if x["final"] == "retained")
            part = sum(1 for x in r if x["final"] in ("retained", "partial"))
            row[arm] = {"claims": n, "unsupported": u, "u": (u / n if n else 0.0), "ref": m, "retained": kept,
                        "r": (kept / m if m else 0.0), "r_partial": (part / m if m else 0.0)}
        rows.append(row)
    d1 = [r["control"]["u"] - r["treatment"]["u"] for r in rows]
    d2 = [r["treatment"]["r"] - r["control"]["r"] for r in rows]
    d2p = [r["treatment"]["r_partial"] - r["control"]["r_partial"] for r in rows]
    pooled = lambda arm, k, t: sum(r[arm][k] for r in rows) / max(1, sum(r[arm][t] for r in rows))
    res = {
        "pages": len(rows),
        "pooled_unsupported": {arm: pooled(arm, "unsupported", "claims") for arm in ("control", "treatment")},
        "pooled_retention": {arm: pooled(arm, "retained", "ref") for arm in ("control", "treatment")},
        "E1": {"mean_diff": sum(d1) / len(d1), "p": signflip_p(d1), "ci95": boot_ci(d1)},
        "E2": {"mean_diff": sum(d2) / len(d2), "p_noninferior": signflip_p(d2, MARGIN), "ci95": boot_ci(d2)},
        "E2_partial_counted": {"mean_diff": sum(d2p) / len(d2p), "p_noninferior": signflip_p(d2p, MARGIN)},
    }
    res["E1"]["pass"] = res["E1"]["p"] < ALPHA and res["pooled_unsupported"]["treatment"] < res["pooled_unsupported"]["control"]
    res["E2"]["pass"] = res["E2"]["p_noninferior"] < ALPHA
    res["decision"] = "adopt" if res["E1"]["pass"] and res["E2"]["pass"] else (
        "reopen-wording" if res["pooled_unsupported"]["treatment"] > res["pooled_unsupported"]["control"] else (
            "opt-in; content lost" if res["E1"]["pass"] else ("mechanism-only" if res["E2"]["pass"] else "not-adopted")))
    res["rows"] = rows
    print(json.dumps(res, indent=1))


def main():
    p = argparse.ArgumentParser()
    s = p.add_subparsers(dest="cmd", required=True)
    x = s.add_parser("select")
    x.add_argument("--wiki", required=True)
    x.add_argument("--member", required=True)
    x.set_defaults(f=cmd_select)
    x = s.add_parser("workspace")
    x.add_argument("--out", required=True)
    x.add_argument("--arm", required=True)
    x.add_argument("--pages", required=True)
    x.add_argument("--repo", required=True)
    x.add_argument("--wiki", required=True)
    x.add_argument("--rsr", required=True)
    x.add_argument("--junit", required=True)
    x.set_defaults(f=cmd_workspace)
    x = s.add_parser("packets")
    x.add_argument("--runs", required=True)
    x.add_argument("--out", required=True)
    x.add_argument("--seed", type=int, default=SEED)
    x.set_defaults(f=cmd_packets)
    x = s.add_parser("analyse")
    x.add_argument("--key", required=True)
    x.add_argument("--e1", required=True)
    x.add_argument("--e2", required=True)
    x.add_argument("--packets", required=True)
    x.set_defaults(f=cmd_analyse)
    a = p.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
