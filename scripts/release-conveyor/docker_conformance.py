"""Docker-to-Docker conformance run for the synthetic conveyor harness.

Builds the harness image, then drives D0 -> V1 -> D1 -> V2 -> D2 with
synthetic stage records. Every station runs in a fresh container with no
network, a read-only root and input, no capabilities, and no credentials.
This is harness conformance (TEST-088), not a release qualification.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
TAG = "conveyor-harness-synthetic:local"
CANDIDATE = "0123456789abcdef0123456789abcdef01234567"
EVIDENCE = "sha256:" + "ab" * 32


def sh(args, **kwargs):
    return subprocess.run(args, capture_output=True, text=True, **kwargs)


def build():
    result = sh(["docker", "build", "--pull=false", "-q", "-t", TAG, HERE])
    if result.returncode != 0:
        raise SystemExit("build failed: " + result.stderr)
    return sh(["docker", "image", "inspect", "--format", "{{.Id}}", TAG]).stdout.strip()


def station(image, name, conveyor, digest, records, extra=(), env=None, writable_input=False,
            network="none"):
    """Run one station in a fresh container; return (exit code, record or None)."""
    root = tempfile.mkdtemp(prefix="conveyor-")
    try:
        inputs, outputs = os.path.join(root, "in"), os.path.join(root, "out")
        os.mkdir(inputs)
        os.mkdir(outputs)
        for key, value in records.items():
            with open(os.path.join(inputs, key + ".json"), "w", encoding="utf-8") as handle:
                json.dump(value, handle)
        os.chmod(inputs, 0o777 if writable_input else 0o555)
        os.chmod(outputs, 0o777)
        args = ["docker", "run", "--rm", "--network", network, "--read-only",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "-v", inputs + ":/in" + ("" if writable_input else ":ro"), "-v", outputs + ":/out"]
        for key, value in (env or {}).items():
            args += ["-e", key + "=" + value]
        args += list(extra) + [image, name, conveyor, CANDIDATE, digest, "/in", "/out"]
        result = sh(args)
        path = os.path.join(outputs, name + ".json")
        record = None
        if os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                record = json.load(handle)
        return result.returncode, record, result.stderr.strip()
    finally:
        os.chmod(os.path.join(root, "in"), 0o755)
        shutil.rmtree(root)


def lunit(conveyor, linux="PASS", windows="PASS"):
    return {"schema": "conveyor-lunit/v1", "conveyorId": conveyor, "candidate": CANDIDATE,
            "linux": linux, "windows": windows}


def stage(conveyor, name, predecessor, lanes=None, cleanup="complete"):
    names = sorted(lanes if lanes is not None else harness.STAGE_LANES[name])
    return {"schema": "conveyor-stage/v1", "conveyorId": conveyor, "candidate": CANDIDATE,
            "stage": name, "predecessorDigest": harness.digest(predecessor),
            "lanes": [{"lane": lane, "verdict": "PASS", "cleanup": cleanup,
                       "evidenceDigest": EVIDENCE} for lane in names]}


def full_belt(image, digest, conveyor):
    records = {"lunit": lunit(conveyor)}
    code, records["D0"], _ = station(image, "D0", conveyor, digest, records)
    assert code == 0, ("D0", code, records["D0"])
    records["V1"] = stage(conveyor, "V1", records["D0"])
    code, records["D1"], _ = station(image, "D1", conveyor, digest, records)
    assert code == 0, ("D1", code, records["D1"])
    records["V2"] = stage(conveyor, "V2", records["D1"])
    code, records["D2"], _ = station(image, "D2", conveyor, digest, records)
    assert code == 0, ("D2", code, records["D2"])
    return records


def main():
    image = build()
    digest = image
    results = []

    def expect(label, outcome, code, refusal=None):
        got, record, stderr = outcome
        reason = (record or {}).get("reasons", [stderr])
        ok = got == code and (refusal is None or (record is None and refusal in stderr))
        results.append(ok)
        print(("ok   " if ok else "FAIL ") + label + " -> exit " + str(got) + " " + ",".join(reason))

    records = full_belt(image, digest, "belt-a")
    chain_ok = (records["D2"]["predecessorDigest"] == harness.digest(records["V2"])
                and records["D1"]["predecessorDigest"] == harness.digest(records["V1"])
                and all(records[s]["harnessDigest"] == digest for s in ("D0", "D1", "D2")))
    results.append(chain_ok)
    print(("ok   " if chain_ok else "FAIL ") + "happy path D0->V1->D1->V2->D2 chained with one harness digest")

    base = {k: records[k] for k in ("lunit", "D0", "V1")}
    expect("D1 with a different harness digest", station(image, "D1", "belt-a", "sha256:" + "0" * 64, base), 1)
    expect("D1 with networking enabled", station(image, "D1", "belt-a", digest, base, network="bridge"), 3, "network-unproven")
    expect("D1 with writable input", station(image, "D1", "belt-a", digest, base, writable_input=True), 3,
           "input-not-read-only")
    expect("D1 with GitHub credential env", station(image, "D1", "belt-a", digest, base, env={"GH_TOKEN": "canary-not-a-secret"}), 3,
           "unexpected-env")
    with tempfile.NamedTemporaryFile(prefix="fake-docker-sock-") as fake:
        expect("D1 with a docker socket path present (dummy file, not the host daemon)",
               station(image, "D1", "belt-a", digest, base, extra=["-v", fake.name + ":/run/docker.sock:ro"]), 3, "docker-socket")

    with tempfile.TemporaryDirectory() as rw:
        os.chmod(rw, 0o755)
        writable = os.path.join(rw, "V1.json")
        with open(writable, "w", encoding="utf-8") as handle:
            json.dump(records["V1"], handle)
        os.chmod(writable, 0o666)
        expect("D1 with one writable file inside a read-only input mount",
               station(image, "D1", "belt-a", digest, base, extra=["-v", writable + ":/in/V1.json"]), 3,
               "input-not-read-only")
    expect("D1 with a hypervisor device path (/dev/null mapped as /dev/kvm)",
           station(image, "D1", "belt-a", digest, base, extra=["--device", "/dev/null:/dev/kvm"]), 3,
           "hypervisor-device")

    pending = dict(base, V1=stage("belt-a", "V1", records["D0"], cleanup="pending"))
    expect("D1 with V1 cleanup pending", station(image, "D1", "belt-a", digest, pending), 2)
    missing_lane = dict(base, V1=stage("belt-a", "V1", records["D0"], lanes=["ubuntu-linux"]))
    expect("D1 with a V1 lane missing", station(image, "D1", "belt-a", digest, missing_lane), 2)

    other = full_belt(image, digest, "belt-b")
    borrowed = dict(base, V1=other["V1"])
    expect("D1 with V1 borrowed from another conveyor", station(image, "D1", "belt-a", digest, borrowed), 1)
    expect("D2 with V2 but no D1 (skipped station)",
           station(image, "D2", "belt-a", digest, dict(base, V2=stage("belt-a", "V2", records["V1"]))), 2)
    expect("D1 rerun with D1 already present", station(image, "D1", "belt-a", digest, dict(base, D1=records["D1"])), 1)
    expect("D1 with V2 present before D1 (reordered)",
           station(image, "D1", "belt-a", digest, dict(base, V2=records["V2"])), 1)

    failed = {"lunit": lunit("belt-c", linux="FAIL")}
    code, d0, err = station(image, "D0", "belt-c", digest, failed)
    expect("D0 with a failed Linux LUnit gate", (code, d0, err), 1)
    failed["D0"] = d0
    failed["V1"] = stage("belt-c", "V1", d0)
    expect("D1 after a halted D0 (LUnit is re-verified first)", station(image, "D1", "belt-c", digest, failed), 1)

    print(str(sum(results)) + "/" + str(len(results)) + " conformance checks passed")
    print("harness image " + digest)
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
