#!/usr/bin/env python3
"""Convert-VipbJson.py: inspect and declaratively patch VI Package Builder
(.vipb) XML files without hand-editing opaque, deeply-nested XML.

Adapted from the approach pioneered by LabVIEW-Community-CI-CD/openvipbcli
(private repo, used with the owner's explicit authorization) -- that
project's VipbJsonTool (.NET/C#) establishes XML<->JSON transcoding for
human-readable .vipb inspection plus a YAML dot-path patch mechanism for
targeted, reviewable field changes. This is a from-scratch Python
implementation of that same approach (not a direct port of the C# source),
chosen to avoid introducing a new .NET build toolchain into a repository
that otherwise only uses bash/PowerShell/Python.

Motivation (SandboxSBOMReporting#263): a project's .vipb file is a single,
deeply-nested XML blob that is painful to hand-edit or code-review a diff
of. This tool lets you:
  1. `inspect`  -- dump a .vipb to readable JSON, for understanding its
     current structure (e.g. finding out a field is empty/misconfigured)
     without needing VIPM Desktop's GUI.
  2. `patch`    -- apply a small, declarative YAML patch (dot-path -> value)
     directly onto a .vipb's XML tree, changing only the fields named in
     the patch and leaving everything else byte-for-byte untouched. This is
     far lower-risk than a full XML<->JSON round-trip for the common case
     of fixing a handful of known-wrong fields (e.g. Library_Source_Folder
     pointing at a stale path).

Usage:
    Convert-VipbJson.py inspect <in.vipb> [out.json]
    Convert-VipbJson.py patch <in.vipb> <patch.yaml> <out.vipb>

Patch file format (YAML, dot-path -> new text value):
    Library_General_Settings.Library_Source_Folder: src
    Library_General_Settings.Library_Output_Folder: builds

Dot-paths are resolved as XML element tag names relative to the
<VI_Package_Builder_Settings> root, matching the exact tag hierarchy you'd
see in the raw .vipb XML (view with `inspect` first to find the right
path). Only leaf element text content can be patched; this tool does not
add/remove elements or attributes.
"""
from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

try:
    import yaml  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover - environment-dependent
    yaml = None


def _element_to_json(element: ET.Element) -> object:
    """Recursively converts an XML element into a JSON-friendly structure.

    Convention (matches common XML<->JSON tooling, e.g. Newtonsoft.Json's
    SerializeXmlNode): attributes become `@name` keys, element text content
    (when the element also has children/attributes) becomes a `#text` key,
    and a tag repeated among siblings becomes a JSON array. This is used
    only for `inspect` output -- it is not round-tripped back to XML by
    this tool (see `patch` instead for making actual changes)."""
    result: dict[str, object] = {}
    for name, value in element.attrib.items():
        result[f"@{name}"] = value

    children_by_tag: dict[str, list[object]] = {}
    for child in element:
        children_by_tag.setdefault(child.tag, []).append(_element_to_json(child))

    for tag, values in children_by_tag.items():
        result[tag] = values[0] if len(values) == 1 else values

    text = (element.text or "").strip()
    if text:
        if result:
            result["#text"] = text
        else:
            return text

    return result if result else None


def cmd_inspect(vipb_path: Path, json_path: Path | None) -> int:
    tree = ET.parse(vipb_path)
    data = _element_to_json(tree.getroot())
    rendered = json.dumps(data, indent=2)
    if json_path:
        json_path.write_text(rendered, encoding="utf-8")
        print(f"Wrote {json_path}")
    else:
        print(rendered)
    return 0


def _find_leaf(root: ET.Element, dot_path: str) -> ET.Element:
    """Resolves a dot-path (e.g. 'Library_General_Settings.Library_Source_Folder')
    to the matching leaf element, starting from `root`. Raises a clear error
    if any segment is not found, rather than silently creating new elements
    -- this tool only patches EXISTING leaf values, by design, to keep the
    blast radius of a patch obvious and reviewable."""
    current = root
    segments = dot_path.split(".")
    for index, segment in enumerate(segments):
        found = current.find(segment)
        if found is None:
            walked = ".".join(segments[: index + 1])
            raise KeyError(
                f"Patch path '{dot_path}' not found: no '<{segment}>' element under "
                f"'{walked}' in this .vipb. Run 'inspect' first to confirm the exact "
                "path/tag names."
            )
        current = found
    return current


def cmd_patch(vipb_path: Path, patch_path: Path, out_path: Path) -> int:
    if yaml is None:
        print("PyYAML is required for 'patch' mode: pip install pyyaml", file=sys.stderr)
        return 2
    patch = yaml.safe_load(patch_path.read_text(encoding="utf-8")) or {}
    if not isinstance(patch, dict):
        print(f"Patch file must be a YAML mapping of dot-path -> value: {patch_path}", file=sys.stderr)
        return 2

    tree = ET.parse(vipb_path)
    root = tree.getroot()
    applied = []
    for dot_path, value in patch.items():
        leaf = _find_leaf(root, str(dot_path))
        leaf.text = "" if value is None else str(value)
        applied.append(dot_path)

    tree.write(out_path, encoding="utf-8", xml_declaration=False)
    print(f"Applied {len(applied)} patch field(s) to {out_path}: {', '.join(applied)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="mode", required=True)

    inspect_parser = subparsers.add_parser("inspect", help="Dump a .vipb's structure as readable JSON.")
    inspect_parser.add_argument("vipb_path", type=Path)
    inspect_parser.add_argument("json_path", type=Path, nargs="?", default=None)

    patch_parser = subparsers.add_parser("patch", help="Apply a YAML dot-path patch to a .vipb.")
    patch_parser.add_argument("vipb_path", type=Path)
    patch_parser.add_argument("patch_path", type=Path)
    patch_parser.add_argument("out_path", type=Path)

    args = parser.parse_args()

    if not args.vipb_path.is_file():
        print(f".vipb file not found: {args.vipb_path}", file=sys.stderr)
        return 2

    if args.mode == "inspect":
        return cmd_inspect(args.vipb_path, args.json_path)
    if args.mode == "patch":
        if not args.patch_path.is_file():
            print(f"Patch file not found: {args.patch_path}", file=sys.stderr)
            return 2
        return cmd_patch(args.vipb_path, args.patch_path, args.out_path)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
