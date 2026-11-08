import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from types import SimpleNamespace
from unittest import mock

import harness

CAND = "0123456789abcdef0123456789abcdef01234567"
HD = "sha256:" + "11" * 32
EV = "sha256:" + "ab" * 32
CTX = {"conveyorId": "belt-a", "candidate": CAND, "harnessDigest": HD}


def lunit(**kw):
    rec = {"schema": "conveyor-lunit/v1", "conveyorId": "belt-a", "candidate": CAND,
           "linux": "PASS", "windows": "PASS"}
    rec.update(kw)
    return rec


def stage(name, pred, lanes=None, **lane_kw):
    out = []
    for lane in sorted(lanes if lanes is not None else harness.STAGE_LANES[name]):
        item = {"lane": lane, "verdict": "PASS", "cleanup": "complete", "evidenceDigest": EV}
        item.update(lane_kw)
        out.append(item)
    return {"schema": "conveyor-stage/v1", "conveyorId": "belt-a", "candidate": CAND,
            "stage": name, "predecessorDigest": harness.digest(pred), "lanes": out}


class Belt(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.inp = os.path.join(self.tmp.name, "in")
        self.out = os.path.join(self.tmp.name, "out")
        os.mkdir(self.inp)
        os.mkdir(self.out)

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, name, rec):
        with open(os.path.join(self.inp, name + ".json"), "w") as h:
            json.dump(rec, h)

    def put_raw(self, name, text):
        with open(os.path.join(self.inp, name + ".json"), "w") as h:
            h.write(text)

    def run_station(self, name):
        rec = harness.run(name, CTX, self.inp, self.out)
        os.remove(os.path.join(self.out, name + ".json"))
        return rec

    def advance(self, upto):
        self.put("lunit", lunit())
        recs = {}
        for name in harness.ORDER[:harness.ORDER.index(upto)]:
            if name in harness.STATIONS:
                recs[name] = self.run_station(name)
                self.assertEqual(recs[name]["verdict"], "PASS", recs[name])
            else:
                prev = recs[harness.ORDER[harness.ORDER.index(name) - 1]]
                recs[name] = stage(name, prev)
            self.put(name, recs[name])
        return recs

    def verdict(self, name):
        rec = self.run_station(name)
        return rec["verdict"], rec["reasons"]

    def test_happy_path_chain(self):
        recs = self.advance("D2")
        d2 = self.run_station("D2")
        self.assertEqual(d2["verdict"], "PASS")
        self.assertEqual(d2["predecessorDigest"], harness.digest(recs["V2"]))
        self.assertEqual(recs["D1"]["predecessorDigest"], harness.digest(recs["V1"]))
        self.assertIsNone(recs["D0"]["predecessorDigest"])

    def test_lunit_fail_and_unknown(self):
        self.put("lunit", lunit(linux="FAIL"))
        self.assertEqual(self.verdict("D0"), ("FAIL", ["lunit-failed-linux"]))
        self.put("lunit", lunit(windows="SKIPPED"))
        self.assertEqual(self.verdict("D0"), ("UNKNOWN", ["lunit-unknown-windows"]))
        os.remove(os.path.join(self.inp, "lunit.json"))
        self.assertEqual(self.verdict("D0"), ("UNKNOWN", ["missing-lunit"]))

    def test_skipped_station_is_unknown(self):
        recs = self.advance("D1")
        self.put("V2", stage("V2", recs["V1"]))
        self.assertEqual(self.verdict("D2"), ("UNKNOWN", ["missing-D1"]))

    def test_reordered_or_rerun_records_fail(self):
        recs = self.advance("D1")
        self.put("V2", stage("V2", recs["V1"]))
        self.assertEqual(self.verdict("D1"), ("FAIL", ["out-of-order-V2"]))
        os.remove(os.path.join(self.inp, "V2.json"))
        self.put("D1", recs["V1"])
        self.assertEqual(self.verdict("D1"), ("FAIL", ["out-of-order-D1"]))

    def test_broken_future_record_symlink_is_out_of_order(self):
        self.advance("D1")
        os.symlink("missing", os.path.join(self.inp, "V2.json"))
        self.assertEqual(self.verdict("D1"), ("FAIL", ["out-of-order-V2"]))

    def test_cross_conveyor_and_candidate_rejected(self):
        recs = self.advance("D1")
        other = dict(recs["V1"], conveyorId="belt-b")
        self.put("V1", other)
        self.assertEqual(self.verdict("D1"), ("FAIL", ["conveyor-mismatch-V1"]))
        self.put("V1", dict(recs["V1"], candidate="f" * 40))
        self.assertEqual(self.verdict("D1"), ("FAIL", ["candidate-mismatch-V1"]))

    def test_chain_break_detected(self):
        recs = self.advance("D1")
        tampered = dict(recs["D0"], reasons=["edited"])
        self.put("D0", tampered)
        self.assertEqual(self.verdict("D1"), ("FAIL", ["chain-break-V1"]))

    def test_harness_digest_must_match(self):
        recs = self.advance("D1")
        self.put("D0", dict(recs["D0"], harnessDigest="sha256:" + "22" * 32))
        self.assertEqual(self.verdict("D1"), ("FAIL", ["harness-mismatch-D0"]))

    def test_halted_station_blocks(self):
        recs = self.advance("D1")
        self.put("D0", dict(recs["D0"], verdict="UNKNOWN"))
        self.assertEqual(self.verdict("D1"), ("UNKNOWN", ["halted-at-D0"]))

    def test_lane_conditions(self):
        recs = self.advance("D1")
        cases = [
            (stage("V1", recs["D0"], cleanup="pending"), "UNKNOWN", "cleanup-pending-ubuntu-linux"),
            (stage("V1", recs["D0"], verdict="FAIL"), "FAIL", "lane-failed-ubuntu-linux"),
            (stage("V1", recs["D0"], verdict="TIMEOUT"), "UNKNOWN", "lane-unknown-ubuntu-linux"),
            (stage("V1", recs["D0"], evidenceDigest="none"), "UNKNOWN", "evidence-ubuntu-linux"),
            (stage("V1", recs["D0"], lanes=["ubuntu-linux"]), "UNKNOWN", "missing-lane-V1"),
            (stage("V1", recs["D0"], lanes=["ubuntu-linux", "ubuntu-windows-public", "windows-linux"]),
             "FAIL", "foreign-lane-V1"),
        ]
        for rec, verdict, reason in cases:
            self.put("V1", rec)
            self.assertEqual(self.verdict("D1"), (verdict, [reason]))
        dup = stage("V1", recs["D0"])
        dup["lanes"].append(dict(dup["lanes"][0]))
        self.put("V1", dup)
        self.assertEqual(self.verdict("D1"), ("FAIL", ["duplicate-lane-V1"]))

    def test_strict_fields_and_parsing(self):
        recs = self.advance("D1")
        self.put("V1", dict(recs["V1"], extra=1))
        self.assertEqual(self.verdict("D1"), ("FAIL", ["fields-V1"]))
        self.put_raw("V1", '{"schema":"a","schema":"b"}')
        self.assertEqual(self.verdict("D1"), ("FAIL", ["duplicate-field"]))
        self.put_raw("V1", "{not json")
        self.assertEqual(self.verdict("D1"), ("FAIL", ["malformed-V1"]))

    def test_invalid_lane_types_produce_a_failure_record(self):
        recs = self.advance("D1")
        for value in ([], {}, None, 1, True):
            with self.subTest(value=value):
                bad = stage("V1", recs["D0"])
                bad["lanes"][0]["lane"] = value
                self.put("V1", bad)
                self.assertEqual(self.verdict("D1"), ("FAIL", ["fields-V1-lane"]))

    def test_unreadable_input_is_unknown(self):
        self.put("lunit", lunit())
        with mock.patch("builtins.open", side_effect=PermissionError("unreadable")):
            with self.assertRaises(harness.Verdict) as outcome:
                harness.load(self.inp, "lunit")
        self.assertEqual((outcome.exception.verdict, outcome.exception.reason),
                         ("UNKNOWN", "unreadable-lunit"))

    def test_output_redacts_input_values(self):
        recs = self.advance("D1")
        canary = "ghp_CANARYCANARYCANARY"
        bad = stage("V1", recs["D0"], verdict=canary, evidenceDigest=canary)
        bad["lanes"][0]["lane"] = canary
        self.put("V1", bad)
        rec = harness.run("D1", CTX, self.inp, self.out)
        with open(os.path.join(self.out, "D1.json")) as h:
            text = h.read()
        self.assertNotIn(canary, text)
        self.assertEqual(set(rec), harness.STATION_FIELDS)

    def test_failure_binds_last_verified_predecessor(self):
        recs = self.advance("D1")
        self.put("V1", stage("V1", recs["D0"], cleanup="pending"))
        rec = self.run_station("D1")
        self.assertEqual(rec["verdict"], "UNKNOWN")
        self.assertEqual(rec["predecessorDigest"], harness.digest(recs["D0"]))
        self.put("lunit", lunit(linux="FAIL"))
        self.assertIsNone(self.run_station("D1")["predecessorDigest"])

    def test_output_is_create_only(self):
        self.put("lunit", lunit())
        harness.run("D0", CTX, self.inp, self.out)
        with self.assertRaises(FileExistsError):
            harness.run("D0", CTX, self.inp, self.out)


class Environment(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.net = os.path.join(self.tmp.name, "net")
        os.makedirs(os.path.join(self.net, "lo"))
        self.inp = os.path.join(self.tmp.name, "in")
        self.out = os.path.join(self.tmp.name, "out")
        os.mkdir(self.inp)
        os.mkdir(self.out)

    def tearDown(self):
        self.tmp.cleanup()

    def problems(self, environ=None, sockets=(), devices=(), read_only=lambda path: True):
        return harness.environment_problems(self.inp, self.out, environ or {"PATH": "/bin"}, self.net,
                                            sockets, devices, read_only)

    def test_clean_environment(self):
        self.assertEqual(self.problems(), [])

    def test_refusals(self):
        self.assertIn("unexpected-env", self.problems({"PATH": "/bin", "GH_TOKEN": "x"}))
        self.assertIn("unexpected-env", self.problems({"MY_SECRET_FILE": "/run/secret"}))
        marker = os.path.join(self.tmp.name, "marker")
        open(marker, "w").close()
        self.assertIn("docker-socket", self.problems(sockets=(marker,)))
        self.assertIn("hypervisor-device", self.problems(devices=(marker,)))
        self.assertIn("input-not-read-only", self.problems(read_only=lambda path: False))
        os.mkdir(os.path.join(self.net, "eth0"))
        self.assertIn("network-unproven", self.problems())

    def test_real_read_only_check_rejects_writable_mount_and_files(self):
        with open(os.path.join(self.inp, "lunit.json"), "w") as h:
            h.write("{}")
        os.chmod(self.inp, 0o555)
        try:
            self.assertIn("input-not-read-only",
                          harness.environment_problems(self.inp, self.out, {}, self.net, (), ()))
        finally:
            os.chmod(self.inp, 0o755)

    def test_read_only_probe_refuses_errors_and_symlinks(self):
        with mock.patch.object(harness.os, "statvfs", side_effect=PermissionError("unreadable")):
            self.assertFalse(harness._read_only_tree(self.inp))
        os.symlink("missing", os.path.join(self.inp, "broken.json"))
        with mock.patch.object(harness.os, "statvfs", return_value=SimpleNamespace(f_flag=os.ST_RDONLY)):
            self.assertFalse(harness._read_only_tree(self.inp))

    def test_missing_net_dir_is_unproven(self):
        problems = harness.environment_problems(self.inp, self.out, {}, os.path.join(self.tmp.name, "nope"),
                                                (), (), lambda path: True)
        self.assertIn("network-unproven", problems)

    def test_unreadable_or_removed_net_dir_is_refused(self):
        good = ["D0", "belt-a", CAND, HD, self.inp, self.out]
        for error in (PermissionError("unreadable"), FileNotFoundError("removed")):
            with self.subTest(error=type(error).__name__):
                stderr = StringIO()
                with mock.patch.object(harness.os, "listdir", side_effect=error):
                    with redirect_stderr(stderr):
                        result = harness.main(good, {}, self.net, (), (), lambda path: True)
                self.assertEqual(result, harness.REFUSED)
                self.assertIn("network-unproven", stderr.getvalue())
                self.assertFalse(os.path.lexists(os.path.join(self.out, "D0.json")))

    def test_invocation_validation(self):
        good = ["D0", "belt-a", CAND, HD, self.inp, self.out]
        for i, bad in [(0, "V1"), (1, "Belt A"), (2, "abc"), (3, "sha256:xyz")]:
            argv = list(good)
            argv[i] = bad
            self.assertEqual(harness.main(argv, {}, self.net, (), (), lambda path: True), harness.REFUSED)
        self.assertEqual(harness.main(good[:5], {}, self.net, (), (), lambda path: True), harness.REFUSED)

    def test_output_race_and_write_error_are_refused(self):
        good = ["D0", "belt-a", CAND, HD, self.inp, self.out]
        for error, reason in ((FileExistsError(), "output-exists"),
                              (PermissionError(), "output-write-failed")):
            with self.subTest(reason=reason), mock.patch.object(harness, "run", side_effect=error):
                stderr = StringIO()
                with redirect_stderr(stderr):
                    result = harness.main(good, {}, self.net, (), (), lambda path: True)
                self.assertEqual(result, harness.REFUSED)
                self.assertIn("refused: " + reason, stderr.getvalue())

    def test_broken_output_symlink_is_refused(self):
        os.symlink("missing", os.path.join(self.out, "D0.json"))
        good = ["D0", "belt-a", CAND, HD, self.inp, self.out]
        stderr = StringIO()
        with redirect_stderr(stderr):
            result = harness.main(good, {}, self.net, (), (), lambda path: True)
        self.assertEqual(result, harness.REFUSED)
        self.assertIn("refused: output-exists", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
