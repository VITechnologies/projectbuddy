#!/usr/bin/env python3
"""Linux container SBOM pipeline.

Runs inside the image built from docker/linux-sbom/Dockerfile (NI's LabVIEW
Linux container plus VIPM and Grype). Mirrors the SBOM stages of
Run-Pipeline.ps1 for Linux:

  1. Clone the configured project (git or svn).
  2. Deploy the project's VIPM manifest, then run `vipm init`, `vipm lock`
     and `vipm sbom` (CycloneDX 1.5).
  3. Enrich the SBOM: group components, merge vipm.lock packages, and replace
     the Windows `nipkg` deep scan with a recursive `dpkg` scan of the NI
     Debian packages installed in the LabVIEW container.
  4. Scan the enriched SBOM with Grype and apply the Grype severity gate.
  5. Publish the enriched SBOM to Dependency-Track, export the server-side
     findings, and apply the Critical/High security gate.

/engine, when mounted, is this generic engine's own root (vipm-exit-codes.json);
falls back to /workspace for a standalone run with no separate engine mount.

Environment variables (paths are relative to /workspace, the mounted repo):
  PIPELINE_CONFIG          Pipeline configuration JSON file (or PROJECTS_OVERRIDE).
  PROJECTS_OVERRIDE        Pipeline configuration JSON text; overrides PIPELINE_CONFIG.
  VIPM_TOML                VIPM manifest file for a single enabled project.
  VIPM_TOML_CONFIGS        JSON object of manifests keyed by project name;
                           overrides VIPM_TOML.
  OUTPUT_DIR               Report directory (default: output/linux).
  ALLOW_PACKAGE_DRIFT      "0" makes `vipm sbom` fail when manifest packages are not
                           installed in the container (default: "1").
  SBOM_SOURCE              "vipm" (default) runs vipm init/lock/sbom and requires
                           VIPM Pro. "manifest" builds the base SBOM from the
                           deployed vipm.toml without VIPM (no vipm.lock merge).
  INCLUDE_OS_DEPENDENCIES  "0" keeps only NI packages from the dpkg scan (default: "1").
  SKIP_GRYPE               "1" skips the Grype scan (default: "0").
  GRYPE_FAIL_ON            Lowest Grype severity that fails the run: negligible, low,
                           medium, high (default), or critical. "none" reports only.
  LABVIEW_IMAGE_DIGEST     Resolved base image digest recorded in the SBOM metadata.
  OS_UPDATES_APPLIED       Set by the image: "1" when Ubuntu updates were applied at build.
    DEPENDENCY_TRACK_MODE   "full" (default) or "hosted-grype"; selects reporting policy explicitly.
    DEPENDENCY_TRACK_API_KEY Dependency-Track API key required by full mode.
  DEPENDENCY_TRACK_URL     Dependency-Track base URL (required for full mode; no default).
  DEPENDENCY_TRACK_PROJECT_NAME
                           Project name override (default: "<displayName>:<name>-linux",
                           kept apart from the Windows pipeline's "<displayName>:<name>").
  DEPENDENCY_TRACK_SECURITY_GATE
                           Full mode requires the Critical/High gate to remain enabled.
  CI_URL                   Build link recorded in the project description (default: "Local Build").
  LABVIEW_IMAGE            Base image name recorded in the SBOM metadata.
  HOST_UID / HOST_GID      Owner applied to generated reports (Linux hosts).
  SVN_GLOBAL_USER, SVN_GLOBAL_PASSWORD
                           SVN credentials used when an SVN project's configuration
                           supplies no username and password/token.
"""

import base64
import hashlib
import datetime
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

WORKSPACE = "/workspace"
# /engine is a second, separate mount of the generic engine's own root (see
# Run-LinuxSbom.ps1 -WorkspaceRoot) -- it holds vipm-exit-codes.json even
# when WORKSPACE is a different, real consumer repository. Falls back to
# WORKSPACE for a standalone container run with no separate engine mount.
ENGINE_ROOT = "/engine" if os.path.isdir("/engine") else WORKSPACE
WORK_ROOT = "/tmp/sbom-work"
# Groups match the vipm.lock "manager" values; NI software installed by apt is "ni-deb".
NIPM_GROUP = "nipm"
NI_DEB_GROUP = "ni-deb"
VIPM_GROUP = "vipm"
OS_GROUP = "OS Dependencies"


class PipelineError(Exception):
    pass


class PolicyError(PipelineError):
    pass


def new_pipeline_run_record(project, reporting_mode="full"):
    return {
        "schemaVersion": 1,
        "project": project,
        "pipeline": "linux-container",
        "reportingMode": reporting_mode,
        "status": "running",
        "failureCategory": None,
        "failure": None,
        "source": None,
        "patches": [],
        "stages": {},
        "outputs": [],
    }


def resolve_dependency_track_mode():
    mode = os.environ.get("DEPENDENCY_TRACK_MODE", "full").strip().lower()
    if mode not in ("full", "hosted-grype"):
        raise PipelineError("DEPENDENCY_TRACK_MODE must be 'full' or 'hosted-grype'.")
    if mode == "hosted-grype":
        return mode
    if os.environ.get("SKIP_DEPENDENCY_TRACK", "0") == "1":
        raise PipelineError("SKIP_DEPENDENCY_TRACK conflicts with DEPENDENCY_TRACK_MODE=full.")
    api_key = os.environ.get("DEPENDENCY_TRACK_API_KEY", "").strip()
    if not api_key:
        raise PipelineError("Full Dependency-Track reporting requires DEPENDENCY_TRACK_API_KEY.")
    if os.environ.get("DEPENDENCY_TRACK_SECURITY_GATE", "1").strip() == "0":
        raise PipelineError("Full Dependency-Track reporting cannot disable DEPENDENCY_TRACK_SECURITY_GATE.")
    base_url = os.environ.get("DEPENDENCY_TRACK_URL", "").strip()
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise PipelineError("Full Dependency-Track reporting requires an absolute HTTPS DEPENDENCY_TRACK_URL.")
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/openapi.json",
        headers={"X-Api-Key": api_key, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            openapi = json.loads(response.read())
    except (OSError, ValueError, urllib.error.URLError) as error:
        raise PipelineError("Full Dependency-Track reporting preflight failed: the configured backend API is unavailable.") from error
    # Dependency-Track documents paths relative to its /api server prefix.
    if not {"/v1/bom", "/api/v1/bom"} & set(openapi.get("paths") or {}):
        raise PipelineError("Full Dependency-Track reporting preflight failed: the backend OpenAPI document lacks the BOM upload endpoint.")
    return mode


def set_pipeline_stage(record, name, status, reason=None, gate=None):
    stage = record["stages"].setdefault(name, {})
    stage["status"] = status
    if reason:
        stage["reason"] = reason
    if gate:
        stage["gate"] = gate


def complete_pipeline_run_record(record, failure=None):
    if not failure:
        record["status"] = "succeeded"
        return
    record["status"] = "failed"
    record["failure"] = str(failure)
    running = []
    for name, stage in record["stages"].items():
        if stage.get("status") == "running":
            stage["status"] = "failed"
            running.append(name)
    if any(stage.get("gate") == "failed" for stage in record["stages"].values()):
        record["failureCategory"] = "policy"
    elif "dependencyTrack" in running or record["stages"].get("dependencyTrack", {}).get("status") == "failed":
        record["failureCategory"] = "reporting"
    else:
        record["failureCategory"] = "build"


def write_pipeline_run_record(record, output_dir):
    outputs = []
    for directory, subdirectories, filenames in os.walk(output_dir):
        subdirectories.sort()
        for filename in sorted(filenames):
            path = os.path.join(directory, filename)
            relative = os.path.relpath(path, output_dir).replace(os.sep, "/")
            if relative == "pipeline-run.json":
                continue
            if os.path.islink(path):
                raise PipelineError(f"Refusing to hash linked pipeline output '{relative}'.")
            digest = hashlib.sha256()
            with open(path, "rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
            outputs.append({"path": relative, "sha256": digest.hexdigest()})
    record["outputs"] = outputs
    destination = os.path.join(output_dir, "pipeline-run.json")
    temporary = destination + ".tmp"
    with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(record, handle, indent=2)
        handle.write("\n")
    os.replace(temporary, destination)


def log(level, message):
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{stamp}] [{level}] {message}", flush=True)


def run(args, cwd=None, redact=None, capture=False):
    shown = " ".join(args)
    if redact:
        shown = shown.replace(redact, "***")
    log("TRACE", f"$ {shown}")
    result = subprocess.run(args, cwd=cwd, text=True,
                            stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.STDOUT if capture else None)
    if result.returncode != 0:
        if capture and result.stdout:
            print(result.stdout)
        raise PipelineError(f"Command failed with exit code {result.returncode}: {shown}")
    return result.stdout if capture else ""


def load_vipm_exit_codes():
    path = os.path.join(ENGINE_ROOT, "vipm-exit-codes.json")
    try:
        with open(path, encoding="utf-8") as handle:
            return {key: value for key, value in json.load(handle).items() if key.isdigit()}
    except (OSError, ValueError):
        return {}


VIPM_EXIT_CODES = load_vipm_exit_codes()
VIPM_PROGRESS = re.compile(r"^\[VIPM\]\s+(\d+(?:\.\d+)?)%$")


def vipm_failure_message(command, exit_code, error_lines):
    entry = VIPM_EXIT_CODES.get(str(exit_code)) or {}
    message = f"vipm {command} failed with exit code {exit_code}"
    if entry.get("name"):
        message += f" ({entry['name']})"
    details = [line.strip().rstrip(".") for line in error_lines if line.strip()][:6]
    if details:
        message += ": " + "; ".join(details)
    message += "."
    if entry.get("hint"):
        message += f" Hint: {entry['hint']}"
    return message


def stream_process_lines(process, drain_seconds=2.0):
    """Yield process.stdout lines until EOF, or until the process has exited and
    the pipe has been quiet for drain_seconds. `vipm build` launches LabVIEW,
    which inherits the pipe and keeps it open long after vipm exits."""
    lines = queue.Queue()

    def pump():
        for raw in process.stdout:
            lines.put(raw)
        lines.put(None)

    threading.Thread(target=pump, daemon=True).start()
    quiet_since = None
    while True:
        try:
            raw = lines.get(timeout=0.1)
        except queue.Empty:
            if process.poll() is None:
                continue
            quiet_since = quiet_since or time.monotonic()
            if time.monotonic() - quiet_since >= drain_seconds:
                log("DEBUG", "vipm exited but a child process (for example LabVIEW) still holds its output; "
                             "continuing without waiting for it.")
                return
            continue
        if raw is None:
            return
        quiet_since = None
        yield raw


def run_vipm(args, cwd=None, show_progress=False, json_output=False):
    """Run one vipm command with colorless output and classified log lines.

    The periodic "[VIPM] NN.N%" lines that --show-progress emits outside a TTY are
    condensed into 25% steps; error:/help:/warning: lines are logged at ERROR/WARN.
    A non-zero exit raises PipelineError with the documented exit-code name and a
    remediation hint. With json_output, stdout is returned instead of logged.
    """
    command = args[0]
    vipm_args = ["vipm", *args, "--color-mode", "never"]
    if show_progress:
        vipm_args.append("--show-progress")
    if json_output:
        vipm_args.append("--json")
    log("TRACE", "$ " + " ".join(vipm_args))
    started = time.monotonic()
    process = subprocess.Popen(vipm_args, cwd=cwd, text=True, encoding="utf-8", errors="replace",
                               stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE if json_output else subprocess.STDOUT)
    step, last, bucket_seen, in_error = 1, -1.0, -1, False
    error_lines = []
    stdout = ""
    if json_output:
        # communicate() drains both pipes, so a large JSON document cannot block on a full pipe.
        stdout, stderr = process.communicate()
        lines = stderr.splitlines()
    else:
        lines = stream_process_lines(process)
    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            continue
        progress = VIPM_PROGRESS.match(line)
        if progress:
            percent = float(progress.group(1))
            if percent + 5 < last:
                step, bucket_seen = step + 1, -1
            last = percent
            bucket = int(min(percent, 100) // 25)
            if bucket != bucket_seen:
                bucket_seen = bucket
                log("INFO", f"  vipm {command}: step {step} at {percent:.0f}%")
        elif re.match(r"^\s*error:", line, re.IGNORECASE):
            in_error = True
            error_lines.append(re.sub(r"^\s*error:\s*", "", line, flags=re.IGNORECASE))
            log("ERROR", f"  vipm {command} | {line}")
        elif in_error and re.match(r"^\s+\S", line):
            # Indented lines after "error:" itemize it (for example each drifted package).
            error_lines.append(line)
            log("ERROR", f"  vipm {command} | {line}")
        elif re.match(r"^\s*(help|hint|warning|warn):", line, re.IGNORECASE):
            in_error = False
            log("WARN", f"  vipm {command} | {line}")
        elif line.startswith("[VIPM]"):
            log("INFO", f"  vipm {command}: {line[6:].strip()}")
        else:
            in_error = False
            log("INFO", f"  vipm {command} | {line}")
    exit_code = process.wait()
    log("TRACE", f"vipm {command} finished with exit code {exit_code} in {time.monotonic() - started:.1f}s.")
    if exit_code != 0:
        raise PipelineError(vipm_failure_message(command, exit_code, error_lines))
    return stdout


def write_vipm_override(project_dir, version, bitness):
    """.vipm/config.toml outranks vipm.toml for every vipm command run in the project,
    so it has to exist before the first vipm call."""
    override_dir = os.path.join(project_dir, ".vipm")
    os.makedirs(override_dir, exist_ok=True)
    text = f'[project]\nlabview-version = "{version}"\n'
    if bitness:
        text += f"labview-bitness = {bitness}\n"
    with open(os.path.join(override_dir, "config.toml"), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    log("SUCCESS", f"Wrote LabVIEW override {version}{f' ({bitness}-bit)' if bitness else ''} to .vipm/config.toml.")


def install_project_vipc(project_dir, relative, lv_version, lv_bitness, query_dir):
    """Apply the configured .vipc with `vipm install`, then compare the configuration's
    package list with what is installed, because VIPM can report success for a .vipc
    while skipping a package that failed to install. Returns the shortfall."""
    expected = {}
    if os.path.isabs(relative) or ".." in relative.replace("\\", "/").split("/"):
        raise PipelineError(f"vipcPath '{relative}' must be a path relative to the checkout root.")
    vipc_path = os.path.join(project_dir, relative.replace("\\", "/"))
    if not os.path.isfile(vipc_path):
        raise PipelineError(f"vipcPath '{relative}' was not found in the checkout.")
    if not vipc_path.lower().endswith(".vipc"):
        raise PipelineError(f"vipcPath '{relative}' is not a .vipc file.")
    listing = json.loads(run_vipm(["list", vipc_path], cwd=project_dir, json_output=True))
    packages = [package for package in listing.get("packages") or [] if package]
    for package in packages:
        expected[str(package["name"])] = str(package["version"])
    log("INFO", f"Installing VI Package Configuration '{relative}' "
                f"({len(packages)} packages) into LabVIEW {lv_version} ({lv_bitness}-bit)...")
    run_vipm(["install", vipc_path, "-y"], cwd=project_dir, show_progress=True)

    # Listing from outside the project returns every installed package, not only vipm.toml's.
    installed = json.loads(run_vipm(["list", "--installed", "--labview-version", lv_version,
                                     "--labview-bitness", lv_bitness], cwd=query_dir, json_output=True) or "{}")
    installed_versions = {str(package.get("name")): str(package.get("installed_version") or "")
                          for package in installed.get("dependencies") or [] if package}
    missing = [f"{name} {version} (installed: {installed_versions.get(name) or 'none'})"
               for name, version in expected.items() if installed_versions.get(name) != version]
    if not missing:
        log("SUCCESS", f"Verified all {len(expected)} .vipc package(s) are installed in LabVIEW {lv_version} ({lv_bitness}-bit).")
    else:
        log("WARN", f"vipm install reported success, but {len(missing)} of {len(expected)} .vipc package(s) are not "
                    "installed (a package-level install action probably failed; install the .vip on its own to see the error):")
        for item in missing:
            log("WARN", f"  not installed: {item}")
    return missing


def workspace_path(relative, name):
    if os.path.isabs(relative) or ".." in relative.replace("\\", "/").split("/"):
        raise PipelineError(f"{name} must be a path relative to the repository root: '{relative}'.")
    return os.path.join(WORKSPACE, relative)


def resolve_value(value):
    if value is None:
        return None
    value = str(value).strip()
    match = re.match(r"^\$env:(.+)$", value)
    return os.environ.get(match.group(1)) if match else value


# ---------------------------------------------------------------- TOML helpers
def strip_toml_comment(line):
    quote = None
    escaped = False
    for index, char in enumerate(line):
        if quote:
            if quote == '"' and char == "\\" and not escaped:
                escaped = True
                continue
            if char == quote and not escaped:
                quote = None
            escaped = False
            continue
        if char in "\"'":
            quote = char
        elif char == "#":
            return line[:index]
    return line


def toml_scalar(value):
    value = value.strip()
    if value.startswith('"'):
        return json.loads(value)
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1]
    return value


def read_toml_tables(path):
    """Returns {table-name: {key: value}} for simple key/value TOML tables."""
    tables = {"": {}}
    section = ""
    with open(path, encoding="utf-8") as handle:
        for raw in handle:
            line = strip_toml_comment(raw).strip()
            header = re.match(r"^\[([^\[\]]+)\]$", line)
            if header:
                section = header.group(1).strip()
                tables.setdefault(section, {})
                continue
            pair = re.match(r'^("[^"]+"|[A-Za-z0-9_.-]+)\s*=\s*(.*)$', line)
            if pair and not line.startswith("[["):
                tables.setdefault(section, {})[pair.group(1).strip('"')] = toml_scalar(pair.group(2))
    return tables


def read_lock_packages(path):
    packages = []
    current = None
    in_package = False
    with open(path, encoding="utf-8") as handle:
        for raw in handle:
            line = strip_toml_comment(raw).strip()
            if line.startswith("["):
                in_package = line == "[[package]]"
                if in_package:
                    current = {}
                    packages.append(current)
                continue
            match = re.match(r"^(name|version|manager)\s*=\s*(.*)$", line)
            if in_package and match:
                current[match.group(1)] = toml_scalar(match.group(2))
    return packages


# ---------------------------------------------------------------- dpkg scan
def os_release():
    info = {}
    with open("/etc/os-release", encoding="utf-8") as handle:
        for line in handle:
            if "=" in line:
                key, value = line.rstrip("\n").split("=", 1)
                info[key] = value.strip('"')
    return info


def installed_debs():
    fmt = "${db:Status-Abbrev}\t${Package}\t${Version}\t${Architecture}\t${Provides}\t" \
          "${Depends}\t${Pre-Depends}\t${Maintainer}\t${binary:Summary}\t" \
          "${source:Package}\t${source:Version}\n"
    output = subprocess.run(["dpkg-query", "-W", "-f", fmt], check=True, text=True,
                            stdout=subprocess.PIPE).stdout
    packages, provides = {}, {}
    for line in output.splitlines():
        fields = line.split("\t")
        if len(fields) != 11 or not fields[0].startswith("ii"):
            continue
        _, name, version, arch, prov, depends, pre_depends, maintainer, summary, src, src_version = fields
        packages[name] = {
            "name": name, "version": version, "arch": arch, "maintainer": maintainer,
            "summary": summary, "depends": ", ".join(d for d in (pre_depends, depends) if d),
            "source": src or name, "source_version": src_version or version,
        }
        for item in filter(None, (p.strip() for p in prov.split(","))):
            provides.setdefault(item.split()[0].split(":")[0], []).append(name)
    return packages, provides


def resolve_deb_dependencies(entry, packages, provides):
    resolved = []
    for clause in filter(None, (c.strip() for c in entry["depends"].split(","))):
        for alternative in clause.split("|"):
            dep = alternative.strip().split()[0].split(":")[0]
            target = dep if dep in packages else next(iter(provides.get(dep, [])), None)
            if target:
                if target != entry["name"] and target not in resolved:
                    resolved.append(target)
                break
    return resolved


def map_nipm_root(name, packages):
    """Maps a (Windows) NIPM package name to an installed Debian package."""
    if name in packages:
        return name
    match = re.match(r"^(ni-labview-\d{4})(?:-x86)?-", name)
    if match and f"{match.group(1)}-core" in packages:
        return f"{match.group(1)}-core"
    return None


def deb_component(entry, distro):
    is_ni = "national instruments" in entry["maintainer"].lower()
    version = urllib.parse.quote(entry["version"], safe="")
    if is_ni:
        purl = f"pkg:deb/ni/{entry['name']}@{version}?arch={entry['arch']}"
    else:
        purl = f"pkg:deb/{distro[0]}/{entry['name']}@{version}?arch={entry['arch']}&distro={distro[0]}-{distro[1]}"
        # Ubuntu advisories are keyed by source package; record it as the purl
        # "upstream" qualifier (Syft convention) so scanners can match binaries.
        if entry["source"] != entry["name"] or entry["source_version"] != entry["version"]:
            upstream = entry["source"]
            if entry["source_version"] != entry["version"]:
                upstream += f"@{entry['source_version']}"
            purl += "&upstream=" + urllib.parse.quote(upstream, safe="")
    supplier = "National Instruments" if is_ni else re.sub(r"\s*<.*>$", "", entry["maintainer"]) or "Unknown"
    return {
        "bom-ref": purl,
        "type": "library",
        "group": NI_DEB_GROUP if is_ni else OS_GROUP,
        "name": entry["name"],
        "version": entry["version"],
        "description": entry["summary"],
        "purl": purl,
        "supplier": {"name": supplier},
    }, is_ni


# ---------------------------------------------------------------- enrichment
def enrich_sbom(sbom, manifest, lock_packages, include_os):
    vipm_deps = {k.lower() for k in manifest.get("dependencies", {})}
    nipm_roots = dict(manifest.get("nipm.dependencies", {}))

    components = sbom.get("components") or []
    name_to_ref = {}
    for comp in components:
        name = str(comp.get("name", "")).lower()
        if name in vipm_deps:
            comp["group"] = VIPM_GROUP
        elif name in {r.lower() for r in nipm_roots} or str(comp.get("purl", "")).startswith("pkg:nipkg/"):
            comp["group"] = NIPM_GROUP
        name_to_ref[name] = comp.get("bom-ref")

    for package in lock_packages:
        if not all(str(package.get(k, "")).strip() for k in ("name", "version", "manager")):
            raise PipelineError(f"Generated vipm.lock contains a package missing name, version, or manager: {package}")
        component = next((c for c in components
                          if c.get("name") == package["name"] and c.get("version") == package["version"]), None)
        if component is None:
            purl = "pkg:generic/{}@{}".format(urllib.parse.quote(package["name"], safe=""),
                                              urllib.parse.quote(package["version"], safe=""))
            component = {"bom-ref": purl, "type": "library", "group": package["manager"],
                         "name": package["name"], "version": package["version"], "purl": purl}
            components.append(component)
        else:
            component["type"] = "library"
            component["group"] = package["manager"]
        name_to_ref[package["name"].lower()] = component["bom-ref"]

    dep_nodes = {}
    for node in sbom.get("dependencies") or []:
        dep_nodes[node["ref"]] = list(node.get("dependsOn") or [])
    root_ref = sbom["metadata"]["component"]["bom-ref"]
    dep_nodes.setdefault(root_ref, [])

    def add_edge(parent, child):
        children = dep_nodes.setdefault(parent, [])
        if child and child != parent and child not in children:
            children.append(child)

    added = 0
    if not nipm_roots:
        log("WARN", "No [nipm.dependencies] in the manifest. Skipping the dpkg deep scan.")
    else:
        log("INFO", "Recursively building the NI Debian dependency graph with dpkg...")
        packages, provides = installed_debs()
        release = os_release()
        distro = (release.get("ID", "linux"), release.get("VERSION_ID", ""))
        graph, ni_flags, deb_components = {}, {}, {}

        def visit(name):
            if name in graph:
                return
            entry = packages[name]
            deb_components[name], ni_flags[name] = deb_component(entry, distro)
            graph[name] = resolve_deb_dependencies(entry, packages, provides)
            for child in graph[name]:
                visit(child)

        mapped_roots = {}
        for root in nipm_roots:
            deb = map_nipm_root(root, packages)
            if deb is None:
                log("WARN", f"  NIPM root '{root}' has no installed Debian equivalent in the container.")
                continue
            if deb != root:
                log("INFO", f"  Mapped NIPM root '{root}' to Debian package '{deb}'.")
            mapped_roots[root] = deb
            visit(deb)
        if not mapped_roots:
            ni_cores = sorted(n for n in packages if re.match(r"^ni-labview-\d{4}-core$", n))
            raise PipelineError(
                f"None of the manifest NIPM roots ({', '.join(nipm_roots)}) are installed in the "
                f"container (LabVIEW packages present: {', '.join(ni_cores) or 'none'}). The "
                "LabVIEW image's version likely differs from the manifest's labview-version; "
                "pass a matching -LabVIEWImage tag.")

        keep = set(graph) if include_os else {n for n in graph if ni_flags[n]}
        for name in sorted(keep):
            key = name.lower()
            if key not in name_to_ref:
                components.append(deb_components[name])
                name_to_ref[key] = deb_components[name]["bom-ref"]
                added += 1

        replaced = {}
        for root, deb in mapped_roots.items():
            root_component_ref = name_to_ref.get(root.lower())
            deb_ref = name_to_ref.get(deb.lower())
            if root_component_ref and root_component_ref != deb_ref:
                # The Windows NIPM package is not installed on Linux; record it on the Debian package that replaces it.
                root_component = next(c for c in components if c.get("bom-ref") == root_component_ref)
                components.remove(root_component)
                replaced[root_component_ref] = deb_ref
                name_to_ref[root.lower()] = deb_ref
                deb_component_entry = next(c for c in components if c.get("bom-ref") == deb_ref)
                deb_component_entry.setdefault("properties", []).append(
                    {"name": "nipm:manifest-package", "value": f"{root_component['name']}@{root_component['version']}"})
            add_edge(root_ref, deb_ref)
        rewritten = {}
        for ref, deps in dep_nodes.items():
            target = replaced.get(ref, ref)
            bucket = rewritten.setdefault(target, [])
            for child in deps:
                child = replaced.get(child, child)
                if child != target and child not in bucket:
                    bucket.append(child)
        dep_nodes.clear()
        dep_nodes.update(rewritten)
        for parent in keep:
            for child in graph[parent]:
                if child in keep:
                    add_edge(name_to_ref[parent.lower()], name_to_ref[child.lower()])

        ni_count = sum(1 for n in keep if ni_flags[n])
        log("SUCCESS", f"Resolved {len(keep)} Debian packages ({ni_count} NI, {len(keep) - ni_count} OS); "
                       f"added {added} new components and their dependency relationships to the graph.")

    sbom["components"] = components
    sbom["dependencies"] = [{"ref": ref, "dependsOn": deps} for ref, deps in dep_nodes.items()]

    release = os_release()
    properties = sbom["metadata"].setdefault("properties", [])
    properties.extend([
        {"name": "sbom:platform", "value": "linux"},
        {"name": "sbom:os", "value": release.get("PRETTY_NAME", "linux")},
        {"name": "labview:container-image", "value": os.environ.get("LABVIEW_IMAGE", "unknown")},
        {"name": "labview:container-image-digest", "value": os.environ.get("LABVIEW_IMAGE_DIGEST", "unknown")},
        {"name": "sbom:os-updates-applied", "value": "true" if os.environ.get("OS_UPDATES_APPLIED") == "1" else "false"},
    ])
    return added


# ---------------------------------------------------------------- pipeline
def load_config():
    if os.environ.get("PROJECTS_OVERRIDE"):
        log("INFO", "Using PROJECTS_OVERRIDE configuration.")
        text = os.environ["PROJECTS_OVERRIDE"]
    else:
        relative = os.environ.get("PIPELINE_CONFIG", "")
        if not relative:
            raise PipelineError("Set PIPELINE_CONFIG or PROJECTS_OVERRIDE.")
        log("INFO", f"Using configuration '{relative}'.")
        with open(workspace_path(relative, "PIPELINE_CONFIG"), encoding="utf-8") as handle:
            text = handle.read()
    config = json.loads(text)
    if not isinstance(config, dict) or not config.get("projects"):
        raise PipelineError("Configuration must be an object with a non-empty 'projects' array.")
    for project in config["projects"]:
        if "vipcPaths" in project:
            raise PipelineError("vipcPaths is no longer supported; use one checkout-relative vipcPath string.")
        if "vipcPath" in project and (not isinstance(project["vipcPath"], str) or not project["vipcPath"].strip()):
            raise PipelineError("vipcPath must be a non-blank string.")
    return config


def load_manifests(enabled_projects):
    if os.environ.get("VIPM_TOML_CONFIGS"):
        manifests = json.loads(os.environ["VIPM_TOML_CONFIGS"])
        if not isinstance(manifests, dict):
            raise PipelineError("VIPM_TOML_CONFIGS must be a JSON object keyed by project name.")
        return manifests
    relative = os.environ.get("VIPM_TOML", "")
    if not relative:
        raise PipelineError("Set VIPM_TOML or VIPM_TOML_CONFIGS.")
    if len(enabled_projects) != 1:
        raise PipelineError("VIPM_TOML supplies one manifest; use VIPM_TOML_CONFIGS for multiple enabled projects.")
    with open(workspace_path(relative, "VIPM_TOML"), encoding="utf-8") as handle:
        return {enabled_projects[0]: handle.read()}


def svn_sparse_update_steps(paths):
    """Return (path, depth) `svn update --set-depth` steps for a --depth empty checkout:
    each parent becomes an empty directory once and each configured path is fetched fully."""
    steps, seen = [], set()
    for raw in paths:
        normalized = str(raw).replace("\\", "/")
        segments = normalized.split("/")
        if (not normalized.strip() or normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized)
                or ".." in segments or "" in segments):
            raise PipelineError(f"sparsePaths entry '{raw}' must be a relative path inside the checkout.")
        for i in range(len(segments)):
            prefix = "/".join(segments[: i + 1])
            depth = "infinity" if i == len(segments) - 1 else "empty"
            if depth == "empty" and prefix in seen:
                continue
            seen.add(prefix)
            steps.append((prefix, depth))
    return steps


def checkout(project, config, destination):
    vcs = str(project.get("vcs", "")).strip().lower()
    if vcs not in ("git", "svn"):
        raise PipelineError("Project must define 'vcs' as 'git' or 'svn'.")
    base_urls = config.get("baseUrls") or {}
    if project.get("repoUrl"):
        repo_url = str(project["repoUrl"]).strip()
    elif base_urls.get(vcs):
        repo_url = str(base_urls[vcs]).strip().rstrip("/") + "/" + project["name"].strip()
    else:
        raise PipelineError(f"No 'repoUrl' and no baseUrls.{vcs} for project.")
    revision = str(project.get("revision") or "HEAD").strip()
    sparse_paths = [str(p).strip() for p in (project.get("sparsePaths") or []) if str(p).strip()]
    if sparse_paths and vcs != "svn":
        raise PipelineError("sparsePaths only applies to SVN checkouts.")
    if project.get("patches"):
        raise PipelineError("patches is only supported by the native Windows pipeline (Run-Pipeline.ps1).")
    username = resolve_value(project.get("username") or config.get("username"))
    token = resolve_value(project.get("token") or config.get("token"))
    password = resolve_value(project.get("password") or config.get("password"))
    secret = token or password
    if vcs == "svn" and not (username and secret):
        # Configured credentials win; otherwise use the global SVN credentials passed to the container.
        global_user = os.environ.get("SVN_GLOBAL_USER", "").strip()
        global_password = os.environ.get("SVN_GLOBAL_PASSWORD", "").strip()
        if global_user and global_password:
            log("INFO", "Using SVN_GLOBAL_USER/SVN_GLOBAL_PASSWORD for the SVN checkout.")
            username, secret = global_user, global_password
        elif global_user or global_password:
            log("WARN", "Only one of SVN_GLOBAL_USER and SVN_GLOBAL_PASSWORD is set; checking out without credentials.")

    if vcs == "git":
        clone_url = repo_url
        if secret and repo_url.startswith("https://"):
            clone_url = repo_url.replace("https://", f"https://{secret}@", 1)
        log("INFO", f"Cloning {repo_url}...")
        run(["git", "clone", "--quiet", clone_url, destination], redact=secret)
        if revision not in ("", "HEAD"):
            log("INFO", f"Checking out git revision {revision}...")
            run(["git", "-C", destination, "checkout", "--quiet", revision])
    else:
        log("INFO", f"Checking out {repo_url} at revision {revision}...")
        auth = ["--non-interactive", "--trust-server-cert", "--quiet"]
        if username and secret:
            auth += ["--username", username, "--password", secret]
        args = ["svn", "checkout", repo_url, destination, "--revision", revision] + auth
        if sparse_paths:
            args += ["--depth", "empty"]
        run(args, redact=secret)
    if vcs == "git":
        resolved = run(["git", "-C", destination, "rev-parse", "HEAD"], capture=True).strip()
    else:
        resolved = run(["svn", "info", "--show-item", "revision", destination], capture=True).strip()
        for path, depth in svn_sparse_update_steps(sparse_paths):
            log("INFO", f"Sparse checkout: {path} (depth {depth}) at r{resolved}")
            run(["svn", "update", "--set-depth", depth, "--revision", resolved,
                 os.path.join(destination, *path.split("/"))] + auth, redact=secret)
    log("INFO", f"Checked out {vcs} revision {resolved}.")
    return repo_url, vcs, resolved


def manifest_sbom(manifest, name, version, lv_version, lv_bitness):
    """Builds a CycloneDX 1.5 SBOM from vipm.toml when VIPM Pro is unavailable."""
    def version_of(spec):
        return re.sub(r"^[\s^~=<>!]+", "", str(spec)).split(",")[0].strip() or "unspecified"

    def purl(kind, package, package_version):
        return f"pkg:{kind}/{urllib.parse.quote(package, safe='')}@{urllib.parse.quote(package_version, safe='')}"

    root_ref = purl("generic", name, version)
    components = []
    for section, kind in (("dependencies", "generic"), ("nipm.dependencies", "nipkg")):
        for package, spec in manifest.get(section, {}).items():
            package_version = version_of(spec)
            ref = purl(kind, package, package_version)
            components.append({"bom-ref": ref, "type": "library", "name": package,
                               "version": package_version, "purl": ref})
    timestamp = os.environ.get("SOURCE_DATE_EPOCH")
    timestamp = datetime.datetime.fromtimestamp(int(timestamp), datetime.timezone.utc) if timestamp \
        else datetime.datetime.now(datetime.timezone.utc)
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{uuid.uuid4()}",
        "version": 1,
        "metadata": {
            "timestamp": timestamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "tools": {"components": [{"type": "application", "name": "linux-sbom-pipeline-manifest-mode",
                                      "version": "1.0.0"}]},
            "component": {"bom-ref": root_ref, "type": "application", "name": name, "version": version},
            "properties": [
                {"name": "sbom:source", "value": "vipm.toml (manifest mode, no vipm sbom scan)"},
                {"name": "labview:version", "value": lv_version},
                {"name": "labview:bitness", "value": lv_bitness},
            ],
        },
        "components": components,
        "dependencies": [{"ref": root_ref, "dependsOn": [c["bom-ref"] for c in components]}],
    }


class DependencyTrack:
    def __init__(self, base_url, api_key):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def request(self, method, path, payload=None, raw=False):
        headers = {"X-Api-Key": self.api_key, "Accept": "application/json"}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        log("TRACE", f"Dependency-Track {method} {self.base_url}{path}")
        req = urllib.request.Request(f"{self.base_url}{path}", data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=120) as response:
                body = response.read()
        except urllib.error.HTTPError as error:
            raise PipelineError(f"Dependency-Track {method} {path} failed: HTTP {error.code} {error.reason}") from None
        except urllib.error.URLError as error:
            raise PipelineError(f"Dependency-Track {method} {path} failed: {error.reason}") from None
        if raw:
            return body
        return json.loads(body) if body.strip() else None


def publish_dependency_track(sbom, sbom_path, project_name, project_version, export_path, lv_version, lv_bitness,
                             run_record=None):
    base_url = os.environ.get("DEPENDENCY_TRACK_URL", "").strip()
    client = DependencyTrack(base_url, os.environ["DEPENDENCY_TRACK_API_KEY"].strip())
    log("INFO", f"Pushing enriched SBOM to Dependency-Track ({base_url}) as "
                f"'{project_name}' version '{project_version}'...")

    query = urllib.parse.urlencode({"name": project_name, "version": project_version})
    project_path = f"/api/v1/project/lookup?{query}"
    previous_import = 0
    try:
        previous_project = client.request("GET", project_path) or {}
        previous_import = int(previous_project.get("lastBomImport") or 0)
    except PipelineError as error:
        if "HTTP 404" not in str(error):
            raise PipelineError(f"Dependency-Track project baseline lookup failed: {error}") from error

    with open(sbom_path, "rb") as handle:
        encoded = base64.b64encode(handle.read()).decode("ascii")
    upload = client.request("PUT", "/api/v1/bom", {
        "projectName": project_name, "projectVersion": project_version,
        "autoCreate": True, "bom": encoded,
    })
    token = (upload or {}).get("token")
    if not token:
        raise PipelineError("Dependency-Track did not return a BOM processing token.")

    deadline = time.monotonic() + 180
    while client.request("GET", f"/api/v1/bom/token/{token}").get("processing"):
        if time.monotonic() > deadline:
            raise PipelineError("Timed out waiting for Dependency-Track to process the SBOM.")
        time.sleep(2)
    log("SUCCESS", "Dependency-Track finished processing the SBOM.")

    project = client.request("GET", project_path) or {}
    project_uuid = project.get("uuid")
    if not project_uuid:
        raise PipelineError("Could not resolve the Dependency-Track project UUID.")
    bom_import = int(project.get("lastBomImport") or 0)
    if bom_import <= previous_import:
        raise PipelineError(f"Dependency-Track did not record a fresh BOM import for {project_name}.")

    properties = {p.get("name"): p.get("value") for p in sbom["metadata"].get("properties", [])}
    tools = sbom["metadata"].get("tools") or {}
    tool_list = tools.get("components", []) if isinstance(tools, dict) else tools
    description = [f"Revision: {properties.get('vcs:revision', 'unknown')}",
                   f"CI/CD Link: {os.environ.get('CI_URL') or 'Local Build'}",
                   f"Image: {properties.get('labview:container-image-digest') or properties.get('labview:container-image', 'unknown')}"]
    tags = {"platform-linux", f"vcs-{properties.get('vcs:type', 'unknown')}",
            f"labview-{lv_version}", f"bitness-{lv_bitness}"}
    for tool in tool_list:
        tool_name = re.sub(r"[^a-zA-Z0-9_-]", "", str(tool.get("name", "")))
        if tool_name and tool.get("version"):
            description.append(f"{tool_name}: {tool['version']}")
            tags.add(f"{tool_name.lower()}-{tool['version']}")
    try:
        client.request("PATCH", f"/api/v1/project/{project_uuid}", {
            "description": " | ".join(description),
            "tags": [{"name": tag} for tag in sorted(tags)],
        })
    except PipelineError as error:
        log("WARN", f"Non-fatal error patching project metadata: {error}")

    findings_export_error = None
    try:
        findings = client.request("GET", f"/api/v1/finding/project/{project_uuid}/export", raw=True)
        with open(export_path, "wb") as handle:
            handle.write(findings)
        log("SUCCESS", f"Server-side findings exported to {os.path.relpath(export_path, WORKSPACE)}.")
    except PipelineError as error:
        findings_export_error = str(error)
        if "HTTP 403" in str(error):
            log("ERROR", "403 Forbidden exporting findings. Ensure the API key has the "
                         "VIEW_VULNERABILITY permission and team access to the project.")
        else:
            log("WARN", f"Failed to export findings: {error}")

    # New uploads have no metrics until Dependency-Track recalculates them.
    metrics = None
    try:
        client.request("GET", f"/api/v1/metrics/project/{project_uuid}/refresh")
    except PipelineError as error:
        log("WARN", f"Could not request a metrics refresh: {error}")
    deadline = time.monotonic() + 60
    for attempt in range(20):
        metrics = client.request("GET", f"/api/v1/metrics/project/{project_uuid}/current")
        if metrics and int(metrics.get("lastOccurrence") or 0) >= bom_import:
            break
        if attempt < 19:
            time.sleep(3)
    if not metrics:
        raise PipelineError("Dependency-Track returned no project metrics; the security gate was not evaluated.")
    if int(metrics.get("lastOccurrence") or 0) < bom_import:
        raise PipelineError(f"Dependency-Track metrics did not refresh for the uploaded BOM in {project_name}.")

    counts = {key: metrics.get(key) or 0 for key in ("critical", "high", "medium", "low")}
    log("METRIC", f"Risk Metrics Gate -> Critical: {counts['critical']} | High: {counts['high']} | "
                  f"Medium: {counts['medium']} | Low: {counts['low']}")
    summary = f"Dependency-Track C/H/M/L {counts['critical']}/{counts['high']}/{counts['medium']}/{counts['low']}"
    if counts["critical"] or counts["high"]:
        message = (f"SECURITY GATE FAILED: Found {counts['critical']} Critical and {counts['high']} "
                   f"High vulnerabilities in {project_name}.")
        if run_record is not None:
            run_record["stages"]["dependencyTrack"]["gate"] = "failed"
            if findings_export_error:
                run_record["stages"]["dependencyTrack"]["findingsExportError"] = findings_export_error
        raise PolicyError(message)
    else:
        log("SUCCESS", "Security Gate Passed!")
    if run_record is not None:
        run_record["stages"]["dependencyTrack"]["gate"] = gate
        if findings_export_error:
            run_record["stages"]["dependencyTrack"]["findingsExportError"] = findings_export_error
    return summary


def process_project(project, config, manifest_text, output_base, include_os, skip_grype, source, run_record=None,
                    reporting_mode="full"):
    build_skipped = None
    if project.get("build"):
        # Best-effort Linux mirror of the Windows pipeline: dependency SBOM only.
        build_skipped = ("Windows-only build: signed artifacts need Authenticode and the VI packages "
                         "(for example Windows-only pdf_toolkit) are built on the Windows pipeline")
        log("WARN", f"Skipping configured build on Linux. {build_skipped}; reporting the dependency SBOM only.")
    name = str(project["name"]).strip()
    display = str(project.get("displayName") or name).strip()
    project_dir = os.path.join(WORK_ROOT, name)
    output_dir = os.path.join(output_base, name)
    shutil.rmtree(project_dir, ignore_errors=True)
    if run_record is None:
        shutil.rmtree(output_dir, ignore_errors=True)
    os.makedirs(WORK_ROOT, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)

    def update_stage(stage, status, reason=None, gate=None):
        if run_record is not None:
            set_pipeline_stage(run_record, stage, status, reason, gate)
            write_pipeline_run_record(run_record, output_dir)

    package_skip_reason = build_skipped or "No package build is configured for the Linux container pipeline."
    update_stage("packages", "skipped", package_skip_reason)
    update_stage("checkout", "running")
    repo_url, vcs, resolved_revision = checkout(project, config, project_dir)
    if run_record is not None:
        run_record["source"] = {"vcs": vcs, "url": repo_url, "revision": resolved_revision}
    update_stage("checkout", "completed")

    update_stage("sbom", "running")
    toml_path = os.path.join(project_dir, "vipm.toml")
    if os.path.exists(toml_path):
        os.remove(toml_path)
    lv_override_version = str(project.get("labviewVersion") or "").strip()
    lv_override_bitness = str(project.get("labviewBitness") or "").strip()
    vipc_path = str(project.get("vipcPath") or "").strip()
    if lv_override_version:
        write_vipm_override(project_dir, lv_override_version, lv_override_bitness)
    if source == "vipm":
        log("INFO", f"Initializing VIPM manifest at '{project_dir}'...")
        run_vipm(["init"], cwd=project_dir)
        if not os.path.isfile(toml_path):
            raise PipelineError("vipm init completed without creating vipm.toml.")
    with open(toml_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(manifest_text.replace("\r\n", "\n"))
    log("SUCCESS", "Deployed configured VIPM manifest.")

    manifest = read_toml_tables(toml_path)
    project_table = manifest.get("project", {})
    root_table = manifest.get("", {})
    lv_version = str(project_table.get("labview-version") or root_table.get("labview-version") or "2026")
    lv_bitness = str(project_table.get("labview-bitness") or root_table.get("labview-bitness") or "64")
    product_version = str(project_table.get("version") or "1.0.0")
    log("INFO", f"Resolved target environment from TOML: LabVIEW {lv_version} ({lv_bitness}-bit).")
    requested_lv = f"{lv_version} ({lv_bitness}-bit)"
    if lv_override_version:
        lv_version = lv_override_version
        lv_bitness = lv_override_bitness or lv_bitness
        log("INFO", f"Project override (.vipm/config.toml) selects LabVIEW {lv_version} ({lv_bitness}-bit) for every vipm command.")
    build_properties = [
        {"name": "vipm:labview-requested", "value": requested_lv},
        {"name": "vipm:labview-used", "value": f"{lv_version} ({lv_bitness}-bit)"},
    ]
    if build_skipped:
        build_properties.append({"name": "build:skipped", "value": build_skipped})

    safe_display = re.sub(r"[^a-zA-Z0-9-]", "_", display)
    prefix = os.path.join(output_dir, f"{safe_display}-{name}")
    sbom_path = f"{prefix}-sbom.json"

    if source == "vipm":
        if vipc_path:
            shortfall = install_project_vipc(project_dir, vipc_path, lv_version, lv_bitness, output_dir)
            build_properties += [{"name": "vipm:vipc", "value": vipc_path}]
            build_properties += [{"name": "vipm:vipc-not-installed", "value": item} for item in shortfall]
        log("INFO", "Generating vipm.lock from the deployed manifest...")
        run_vipm(["lock"], cwd=project_dir)
        lock_path = os.path.join(project_dir, "vipm.lock")
        if not os.path.isfile(lock_path):
            raise PipelineError("vipm lock completed without creating vipm.lock.")
        lock_packages = read_lock_packages(lock_path)
        shutil.copyfile(lock_path, os.path.join(output_dir, "vipm.lock"))

        log("INFO", f"Extracting VIPM SBOM for '{name}'...")
        sbom_args = ["sbom", toml_path, "--format", "cyclonedx", "--schema-version", "1.5"]
        if not lv_override_version:
            sbom_args += ["--labview-version", lv_version, "--labview-bitness", lv_bitness]
        sbom_args += ["--product-name", name, "--product-version", product_version, "--output", sbom_path]
        # Manifest NIPM roots are often Windows package names that do not exist in the
        # Linux container; the dpkg deep scan below records what is actually installed.
        if os.environ.get("ALLOW_PACKAGE_DRIFT", "1") != "0":
            sbom_args.append("--allow-package-drift")
        run_vipm(sbom_args, cwd=project_dir)
        with open(sbom_path, encoding="utf-8") as handle:
            sbom = json.load(handle)
    else:
        log("WARN", "SBOM_SOURCE=manifest: building the base SBOM from vipm.toml without VIPM "
                    "(no vipm lock/sbom; VI-level dependency scanning is not performed).")
        lock_packages = []
        sbom = manifest_sbom(manifest, name, product_version, lv_version, lv_bitness)

    base_count = len(sbom.get("components") or [])
    added = enrich_sbom(sbom, manifest, lock_packages, include_os)
    sbom["metadata"].setdefault("properties", []).extend([
        {"name": "vcs:type", "value": vcs},
        {"name": "vcs:url", "value": repo_url},
        {"name": "vcs:revision", "value": resolved_revision},
        *build_properties,
    ])
    with open(sbom_path, "w", encoding="utf-8") as handle:
        json.dump(sbom, handle, indent=2)
        handle.write("\n")
    log("SUCCESS", f"Enriched SBOM written to {os.path.relpath(sbom_path, WORKSPACE)} "
                   f"({base_count} from {source}, {len(sbom['components'])} total components).")
    update_stage("sbom", "completed")

    details = f"{len(sbom['components'])} components ({added} from dpkg)"
    grype_gate_error = None
    if skip_grype:
        log("WARN", "SKIP_GRYPE=1; skipping the vulnerability scan.")
        update_stage("grype", "skipped", "SKIP_GRYPE=1")
    else:
        update_stage("grype", "running")
        report_path = f"{prefix}-vuln-report.txt"
        json_report_path = f"{prefix}-vuln-report.json"
        log("INFO", "Scanning the enriched SBOM with Grype...")
        run(["grype", f"sbom:{sbom_path}", "--quiet",
             "-o", f"table={report_path}", "-o", f"json={json_report_path}"])
        with open(json_report_path, encoding="utf-8") as handle:
            matches = json.load(handle).get("matches") or []
        severities = {}
        for match in matches:
            severity = match.get("vulnerability", {}).get("severity", "Unknown")
            severities[severity] = severities.get(severity, 0) + 1
        breakdown = ", ".join(f"{k}: {v}" for k, v in sorted(severities.items())) or "none"
        log("METRIC", f"Grype findings: {len(matches)} ({breakdown}).")
        log("SUCCESS", f"Grype reports written to {os.path.relpath(report_path, WORKSPACE)} (+ .json).")
        details += f", {len(matches)} Grype findings"
        grype_gate_error = grype_gate(matches, project_name=name)
        if grype_gate_error:
            grype_gate_status = "failed"
        elif os.environ.get("GRYPE_FAIL_ON", "high").strip().lower() == "none":
            # Matches the Windows container pipeline's reporting: "none"
            # means no gate was actually evaluated, which is a materially
            # different, more honest status than "passed".
            grype_gate_status = "not-gated"
        else:
            grype_gate_status = "passed"
        update_stage("grype", "completed", gate=grype_gate_status)

    if reporting_mode == "hosted-grype":
        log("WARN", "DEPENDENCY_TRACK_MODE=hosted-grype; skipping the Dependency-Track upload and gate.")
        update_stage("dependencyTrack", "skipped", "DEPENDENCY_TRACK_MODE=hosted-grype")
    else:
        update_stage("dependencyTrack", "running")
        dt_name = os.environ.get("DEPENDENCY_TRACK_PROJECT_NAME", "").strip() or f"{display}:{name}-linux"
        dt_summary = publish_dependency_track(sbom, sbom_path, dt_name, product_version,
                                              f"{prefix}-dt-findings.json", lv_version, lv_bitness,
                                              run_record=run_record)
        details += ", " + dt_summary
        dt_stage = (run_record or {}).get("stages", {}).get("dependencyTrack", {})
        update_stage("dependencyTrack", "completed", gate=dt_stage.get("gate", "passed"))
    # Raised after the upload so Dependency-Track still receives the inventory.
    if grype_gate_error:
        raise PipelineError(grype_gate_error)
    return details


SEVERITY_ORDER = ["negligible", "low", "medium", "high", "critical"]


def grype_gate(matches, project_name):
    """Returns an error message when findings reach GRYPE_FAIL_ON, else None."""
    threshold = os.environ.get("GRYPE_FAIL_ON", "high").strip().lower() or "high"
    if threshold == "none":
        log("WARN", "GRYPE_FAIL_ON=none; Grype findings are reported without gating.")
        return None
    if threshold not in SEVERITY_ORDER:
        raise PipelineError(f"GRYPE_FAIL_ON must be one of {', '.join(SEVERITY_ORDER)} or none, not '{threshold}'.")
    gated = SEVERITY_ORDER[SEVERITY_ORDER.index(threshold):]
    failing = sorted({f"{m['vulnerability']['id']} ({m['artifact']['name']} {m['artifact']['version']}, "
                      f"{m['vulnerability'].get('severity')})"
                      for m in matches
                      if str(m.get("vulnerability", {}).get("severity", "")).lower() in gated})
    if not failing:
        log("SUCCESS", f"Grype Security Gate Passed (no findings at or above '{threshold}').")
        return None
    for item in failing:
        log("ERROR", f"  Grype gate: {item}")
    return (f"GRYPE SECURITY GATE FAILED: {len(failing)} finding(s) at or above '{threshold}' "
            f"in {project_name}.")


def main():
    output_relative = os.environ.get("OUTPUT_DIR", "output/linux")
    output_base = workspace_path(output_relative, "OUTPUT_DIR")
    host_uid = os.environ.get("HOST_UID", "")
    host_gid = os.environ.get("HOST_GID", "")
    if (host_uid or host_gid) and not (re.fullmatch(r"[0-9]+", host_uid) and re.fullmatch(r"[0-9]+", host_gid)):
        raise PipelineError("HOST_UID and HOST_GID must both be numeric when either is supplied.")
    if os.path.realpath(output_base) != os.path.abspath(output_base):
        raise PipelineError("OUTPUT_DIR must not contain symbolic links.")
    include_os = os.environ.get("INCLUDE_OS_DEPENDENCIES", "1") != "0"
    skip_grype = os.environ.get("SKIP_GRYPE", "0") == "1"
    reporting_mode = resolve_dependency_track_mode()
    source = os.environ.get("SBOM_SOURCE", "vipm").strip().lower()
    if source not in ("vipm", "manifest"):
        raise PipelineError(f"SBOM_SOURCE must be 'vipm' or 'manifest', not '{source}'.")

    log("INFO", "==================================================")
    log("INFO", "Linux Container SBOM Pipeline")
    log("INFO", f"Base image: {os.environ.get('LABVIEW_IMAGE', 'unknown')}")
    log("INFO", f"SBOM source: {source}")
    if source == "vipm":
        log("INFO", f"VIPM:       {run(['vipm', '--version'], capture=True).strip()}")
    log("INFO", f"Output:     {output_relative}")
    log("INFO", "==================================================")

    config = load_config()
    enabled = [str(p.get("name", "")).strip() for p in config["projects"] if p.get("enabled", True) is not False]
    manifests = load_manifests(enabled)
    missing_output_dirs = []
    current = os.path.abspath(output_base)
    while not os.path.exists(current):
        missing_output_dirs.append(current)
        current = os.path.dirname(current)
    for directory in reversed(missing_output_dirs):
        os.mkdir(directory)
        if host_uid:
            os.chown(directory, int(host_uid), int(host_gid), follow_symlinks=False)

    summary = []
    for index, project in enumerate(config["projects"], start=1):
        name = str(project.get("name", "")).strip()
        log("INFO", f"Project [{index}/{len(config['projects'])}]: {name}")
        if project.get("enabled", True) is False:
            summary.append((name, "SKIPPED", "Disabled in config."))
            continue
        if not name or name in (".", "..") or "/" in name or "\\" in name:
            raise PipelineError(f"Project name must be a single directory name: '{name}'.")
        output_dir = os.path.join(output_base, name)
        if os.path.islink(output_dir):
            raise PipelineError(f"Project output directory must not be a symbolic link: '{name}'.")
        if os.path.lexists(output_dir):
            shutil.rmtree(output_dir)
        os.makedirs(output_dir)
        run_record = new_pipeline_run_record(name, reporting_mode)
        try:
            write_pipeline_run_record(run_record, output_dir)
            manifest_text = manifests.get(name)
            if not isinstance(manifest_text, str) or not manifest_text.strip():
                raise PipelineError(f"No VIPM manifest supplied for project '{name}'.")
            summary.append((name, "SUCCESS", process_project(project, config, manifest_text, output_base,
                                                             include_os, skip_grype, source, run_record=run_record,
                                                             reporting_mode=reporting_mode)))
            complete_pipeline_run_record(run_record)
        except (PipelineError, OSError, ValueError, KeyError) as error:
            log("ERROR", f"Project '{name}' failed: {error}")
            complete_pipeline_run_record(run_record, error)
            summary.append((name, "FAILED", str(error)))
        finally:
            try:
                write_pipeline_run_record(run_record, output_dir)
            finally:
                if host_uid:
                    try:
                        subprocess.run(["find", output_dir, "-xdev", "!", "-type", "d", "-links", "+1",
                                        "-exec", "/bin/false", "{}", "+"], check=True)
                        subprocess.run(["find", output_dir, "-xdev", "-exec", "chown", "-h", "--",
                                        f"{host_uid}:{host_gid}", "{}", "+"], check=True)
                    except (OSError, subprocess.CalledProcessError) as error:
                        ownership_error = f"Could not set output ownership for project '{name}'."
                        previous_failure = run_record.get("failure")
                        complete_pipeline_run_record(run_record, f"{previous_failure} {ownership_error}"
                                                     if previous_failure else ownership_error)
                        write_pipeline_run_record(run_record, output_dir)
                        raise PipelineError(ownership_error) from error

    log("INFO", "==================================================")
    for name, status, details in summary:
        log("SUCCESS" if status == "SUCCESS" else ("WARN" if status == "SKIPPED" else "ERROR"),
            f"{name}: {status} - {details}")
    return 1 if any(status == "FAILED" for _, status, _ in summary) else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (PipelineError, OSError, ValueError) as error:
        log("ERROR", str(error))
        sys.exit(1)
