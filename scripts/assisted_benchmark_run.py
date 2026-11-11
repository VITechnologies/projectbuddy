#!/usr/bin/env python3
"""Run an approved, read-only LBA-assisted benchmark assessment."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import stat
from pathlib import Path

from assisted_benchmark_capture import CaptureError, validate_capture
from assisted_benchmark_contract import (
    ContractError,
    authorize_tool_call,
    load_contract,
    validate_authorization,
    validate_snapshot,
)
from assisted_benchmark_review import LocalOllama, ReviewError, review_evidence
from assisted_benchmark_checkpoint import CheckpointError

RUN_SCHEMA = "projectbuddy/assisted-benchmark-run@1"
MAX_MCP_LINE_BYTES = 4 * 1024 * 1024
REQUIRED_CONTEXT_TOOLS = {"get_host_capabilities", "get_benchmark_series"}


class RunError(ValueError):
    pass


def canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate_ppl_build_proof(receipt: object) -> dict:
    if not isinstance(receipt, dict) or receipt.get("schema") != "labview-benchmark-actor/ppl-build-benchmark@1":
        raise RunError("PPL proof schema is unsupported")
    source = receipt.get("source")
    build = receipt.get("build")
    verdict = receipt.get("verdict")
    if not isinstance(source, dict) or not isinstance(build, dict) or not isinstance(verdict, dict):
        raise RunError("PPL proof is missing source, build, or verdict fields")
    if (source.get("repo") != "ni/labview-icon-editor"
            or source.get("project") != "lv_icon_editor.lvproj"
            or not isinstance(source.get("repoHead"), str)
            or not re.fullmatch(r"[0-9a-f]{40}", source["repoHead"])):
        raise RunError("PPL proof does not identify the pinned icon-editor source")
    if build.get("buildSpec") != "Editor Packed Library" or receipt.get("generatedArtifact") != "lv_icon.lvlibp":
        raise RunError("PPL proof does not match the approved lv_icon.lvlibp target")
    if receipt.get("operation") != "LabVIEWCLI -OperationName ExecuteBuildSpec" or receipt.get("operationSucceeded") is not True:
        raise RunError("PPL build operation did not succeed")
    if verdict.get("benchmarkOk") is not True:
        raise RunError("PPL proof verdict is not successful")
    if type(receipt.get("artifactSizeBytes")) is not int or receipt["artifactSizeBytes"] <= 0:
        raise RunError("PPL proof artifact size is invalid")

    result_identity = {
        "project": source["project"],
        "target": build.get("target"),
        "buildSpec": build["buildSpec"],
        "generatedArtifact": receipt["generatedArtifact"],
        "operationSucceeded": receipt["operationSucceeded"],
    }
    expected_result_hash = _sha256(json.dumps(result_identity, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    if receipt.get("resultHash") != expected_result_hash:
        raise RunError("PPL proof resultHash does not match its build identity")
    digest_identity = {
        "schema": receipt["schema"],
        "project": source["project"],
        "target": build.get("target"),
        "buildSpec": build["buildSpec"],
        "generatedArtifact": receipt["generatedArtifact"],
        "operationSucceeded": receipt["operationSucceeded"],
        "resultHash": receipt["resultHash"],
        "verdict": {"benchmarkOk": verdict["benchmarkOk"]},
    }
    expected_digest = _sha256(json.dumps(digest_identity, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    if receipt.get("digest") != expected_digest:
        raise RunError("PPL proof digest is invalid")
    return {
        "plane": receipt.get("plane"),
        "sourceRepo": source["repo"],
        "sourceRevision": source["repoHead"],
        "project": source["project"],
        "buildSpec": build["buildSpec"],
        "artifact": receipt["generatedArtifact"],
        "artifactSizeBytes": receipt["artifactSizeBytes"],
        "resultHash": expected_result_hash,
        "receiptDigest": expected_digest,
    }


class McpStdioClient:
    """Minimal serial JSON-RPC client; command arguments are passed without a shell."""

    def __init__(self, command: list[str], timeout: float = 30.0):
        if not command or any(not isinstance(part, str) or not part for part in command):
            raise RunError("MCP server command is invalid")
        self.timeout = timeout
        self._counter = 0
        self._lines: queue.Queue[object] = queue.Queue()
        try:
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                bufsize=1,
            )
        except OSError as error:
            raise RunError("could not start the approved MCP server command") from error
        if self.process.stdin is None or self.process.stdout is None:
            self.close()
            raise RunError("MCP server stdio is unavailable")
        self._reader = threading.Thread(target=self._read_lines, daemon=True)
        self._reader.start()

    def _read_lines(self) -> None:
        assert self.process.stdout is not None
        try:
            while True:
                line = self.process.stdout.readline(MAX_MCP_LINE_BYTES + 1)
                if not line:
                    self._lines.put(None)
                    return
                if len(line.encode("utf-8")) > MAX_MCP_LINE_BYTES:
                    self._lines.put(RunError("MCP response exceeded the line-size limit"))
                    return
                self._lines.put(line)
        except (OSError, UnicodeError):
            self._lines.put(RunError("MCP response stream failed"))

    def _write(self, message: dict) -> None:
        if self.process.poll() is not None or self.process.stdin is None:
            raise RunError("MCP server exited before the request completed")
        try:
            self.process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            self.process.stdin.flush()
        except (OSError, BrokenPipeError) as error:
            raise RunError("MCP request could not be written") from error

    def _request(self, method: str, params: dict | None = None) -> dict:
        self._counter += 1
        request_id = self._counter
        message = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self._write(message)
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RunError(f"MCP request timed out: {method}")
            try:
                line = self._lines.get(timeout=remaining)
            except queue.Empty as error:
                raise RunError(f"MCP request timed out: {method}") from error
            if line is None:
                raise RunError("MCP server closed its output before replying")
            if isinstance(line, RunError):
                raise line
            try:
                response = json.loads(line)
            except json.JSONDecodeError as error:
                raise RunError("MCP server returned malformed JSON-RPC") from error
            if not isinstance(response, dict) or response.get("jsonrpc") != "2.0":
                raise RunError("MCP server returned an invalid JSON-RPC envelope")
            if response.get("id") != request_id:
                continue
            if "error" in response:
                error = response["error"]
                code = error.get("code") if isinstance(error, dict) else None
                raise RunError(f"MCP request failed with JSON-RPC code {code}")
            result = response.get("result")
            if not isinstance(result, dict):
                raise RunError(f"MCP request returned no result: {method}")
            return result

    def _notify(self, method: str, params: dict | None = None) -> None:
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._write(message)

    def initialize_and_list_tools(self) -> dict:
        initialized = self._request("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "projectbuddy-assisted-benchmark", "version": "0.1.0"},
        })
        info = initialized.get("serverInfo")
        if not isinstance(info, dict):
            raise RunError("MCP initialize result omitted serverInfo")
        protocol = initialized.get("protocolVersion")
        self._notify("notifications/initialized")
        listed = self._request("tools/list")
        tools = listed.get("tools")
        if not isinstance(tools, list):
            raise RunError("MCP tools/list result omitted the tool array")
        return {"protocolVersion": protocol, "serverInfo": info, "tools": tools}

    def call(self, tool: str, arguments: dict) -> dict:
        result = self._request("tools/call", {"name": tool, "arguments": arguments})
        if result.get("isError") is True:
            raise RunError(f"MCP tool reported an execution error: {tool}")
        return result

    def close(self) -> None:
        if getattr(self, "process", None) is None:
            return
        if self.process.stdin is not None and not self.process.stdin.closed:
            try:
                self.process.stdin.close()
            except OSError:
                pass
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


def _summarize_tool_result(result: dict) -> dict:
    if result.get("isError") is True:
        raise RunError("MCP tool returned an error result")
    encoded = canonical_json(result)
    if len(encoded) > MAX_MCP_LINE_BYTES:
        raise RunError("MCP tool result exceeded the summary limit")
    return {"resultSha256": _sha256(encoded), "resultBytes": len(encoded)}


def write_private_receipt(path: Path, receipt: dict) -> None:
    if os.name != "posix":
        raise RunError("private receipt output is not implemented for this platform")
    output = Path(os.path.abspath(path))
    if not output.is_absolute() or output.exists() or output.is_symlink():
        raise RunError("receipt output must be a new absolute file path")
    parent = output.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if parent.is_symlink() or parent.resolve(strict=True) != parent:
        raise RunError("receipt directory must not contain symlinked path components")
    if stat.S_IMODE(parent.stat().st_mode) & 0o077:
        raise RunError("receipt directory must be private (mode 0700 or stricter)")
    if (not isinstance(receipt, dict) or receipt.get("schema") != RUN_SCHEMA
            or "records" in receipt or "complete" in receipt):
        raise RunError("partial checkpoint data cannot be written as a final run receipt")
    payload = canonical_json(receipt) + b"\n"
    descriptor, temporary_name = tempfile.mkstemp(prefix=".assisted-benchmark-", dir=parent)
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary_path, output)
        os.chmod(output, 0o600)
    except FileExistsError as error:
        raise RunError("receipt output already exists") from error
    except OSError as error:
        raise RunError("private receipt could not be committed") from error
    finally:
        temporary_path.unlink(missing_ok=True)


def orchestrate(
    authorization: object,
    contract: dict,
    capture: dict,
    ppl_proof: object,
    mcp_client,
    ollama_transport,
    capture_directory: Path | None = None,
    checkpoint_path: Path | None = None,
    resume: bool = False,
) -> dict:
    print("[assisted-benchmark] validating run authorization", file=sys.stderr, flush=True)
    approved = validate_authorization(authorization, contract)
    if approved["sourceRevision"] != contract["source"]["revision"]:
        raise RunError("authorization source revision does not match the pinned actor contract")
    if capture.get("runId") != approved["runId"] or capture.get("workload") != approved["workload"]:
        raise RunError("capture run identity does not match the per-run authorization")
    if capture.get("actorVersion") != contract["source"]["serverVersion"] or capture.get("actorRevision") != contract["source"]["revision"]:
        raise RunError("capture actor identity does not match the pinned MCP contract")
    proof = validate_ppl_build_proof(ppl_proof)

    print("[assisted-benchmark] initializing LBA MCP and validating tool registry", file=sys.stderr, flush=True)
    snapshot = mcp_client.initialize_and_list_tools()
    snapshot_summary = validate_snapshot(snapshot, contract)
    call_names = {call["tool"] for call in approved["approvedCalls"]}
    if not REQUIRED_CONTEXT_TOOLS.issubset(call_names):
        raise RunError("run approval must include host capabilities and benchmark series")
    call_results = []
    for call in approved["approvedCalls"]:
        decision = authorize_tool_call(call["tool"], call["arguments"], approved, contract)
        result = mcp_client.call(call["tool"], call["arguments"])
        call_results.append({
            "tool": call["tool"],
            "effect": decision["effect"],
            **_summarize_tool_result(result),
        })
        print(f"[assisted-benchmark] completed approved MCP call: {call['tool']}", file=sys.stderr, flush=True)

    print("[assisted-benchmark] validating stopped capture evidence", file=sys.stderr, flush=True)
    review_input = {**capture, "benchmarkResult": proof}
    print("[assisted-benchmark] requesting local Ollama assessment", file=sys.stderr, flush=True)
    assessment = review_evidence(review_input, approved, contract, ollama_transport, capture_directory,
                                 checkpoint_path=checkpoint_path, resume=resume)
    print("[assisted-benchmark] local assessment complete", file=sys.stderr, flush=True)
    core = {
        "schema": RUN_SCHEMA,
        "runId": approved["runId"],
        "workload": approved["workload"],
        "actor": {"serverName": contract["source"]["serverName"], "version": contract["source"]["serverVersion"], "revision": contract["source"]["revision"]},
        "mcp": {**snapshot_summary, "calls": call_results},
        "capture": {
            "runId": capture["runId"],
            "frameCount": capture["frameCount"],
            "durationMs": capture["durationMs"],
            "screen": capture["screen"],
            "resourceSampleCount": capture["resourceSampleCount"],
            "deterministicIdentitySha256": capture["deterministicIdentitySha256"],
        },
        "pplProof": proof,
        "assessment": assessment["assessment"],
        "assessmentSha256": _sha256(canonical_json(assessment["assessment"])),
        "reviewEvidenceSha256": assessment["evidenceSha256"],
        "approvedReviewModel": assessment["approvedModel"],
        "reviewModel": assessment["model"],
        "reviewAggregation": assessment["aggregation"],
        "reviewBudget": assessment["reviewBudget"],
        "reviewExecution": assessment["reviewExecution"],
        "reviewRequestBudgets": assessment["requestBudgets"],
        "reviewModelBudget": assessment["modelBudget"],
        "reviewPrivacy": assessment["reviewPrivacy"],
        "modelAssessmentSha256": assessment["modelAssessmentSha256"],
        "modelAssessmentHashPolicy": assessment["modelAssessmentHashPolicy"],
        "findingSha256": assessment["findingSha256"],
        "reviewSampling": assessment["sampling"],
        "reviewLifecycle": assessment["lifecycle"],
        "reviewCheckpoint": assessment["checkpoint"],
        "reviewPromptVersion": assessment["promptVersion"],
        "reviewedFrameCount": assessment["reviewedFrameCount"],
        "advisoryOnly": assessment["advisoryOnly"],
        "authorizationSha256": _sha256(canonical_json(approved)),
    }
    return {**core, "receiptSha256": _sha256(canonical_json(core))}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--authorization", type=Path, required=True)
    parser.add_argument("--capture-directory", type=Path, required=True)
    parser.add_argument("--ppl-proof", type=Path, required=True)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--ollama-url", required=True)
    parser.add_argument("--output", type=Path, required=True, help="new absolute receipt path in a private directory")
    parser.add_argument("--checkpoint", type=Path, help="private POSIX partial-review state, never a success receipt")
    parser.add_argument("--resume", action="store_true", help="reuse only an exactly matching private checkpoint")
    parser.add_argument("--mcp-command", nargs="+", required=True)
    args = parser.parse_args(argv)
    try:
        if args.resume and args.checkpoint is None:
            raise RunError("resume requires an explicit private checkpoint path")
        if args.checkpoint is not None and args.checkpoint.absolute() == args.output.absolute():
            raise RunError("checkpoint and final receipt paths must be different")
        contract = load_contract(args.contract)
        authorization = json.loads(args.authorization.read_text(encoding="utf-8"))
        capture = validate_capture(args.capture_directory, contract["source"]["serverVersion"], contract["source"]["revision"])
        proof = json.loads(args.ppl_proof.read_text(encoding="utf-8"))
        with McpStdioClient(args.mcp_command) as mcp:
            receipt = orchestrate(
                authorization, contract, capture, proof, mcp, LocalOllama(args.ollama_url), args.capture_directory,
                args.checkpoint, args.resume
            )
        write_private_receipt(args.output, receipt)
        print(json.dumps({"status": "complete", "runId": receipt["runId"], "receiptSha256": receipt["receiptSha256"]}, sort_keys=True))
        return 0
    except KeyboardInterrupt:
        print("assisted-benchmark-run: cancelled; server completion may be unknown", file=sys.stderr)
        return 130
    except (OSError, json.JSONDecodeError, ContractError, CaptureError, ReviewError, RunError, CheckpointError) as error:
        print(f"assisted-benchmark-run: refused ({type(error).__name__}); no success; server completion may be unknown", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())