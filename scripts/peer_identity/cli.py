"""Command line for New-PeerModelfile.py."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .modelfile import IdentityError, model_tag, parse_manifest, render


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="New-PeerModelfile.py", description="Generate the projectbuddy identity Modelfile.")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--base", required=True, help="Ollama base model, for example qwen2.5:7b")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="repository root that source paths are relative to")
    parser.add_argument("--output", type=Path, help="write the Modelfile here instead of stdout")
    args = parser.parse_args(argv)
    try:
        manifest = parse_manifest(args.manifest.read_text(encoding="utf-8"))
        text = render(manifest, args.base, args.root)
        if args.output is None:
            # Raw UTF-8 bytes keep stdout byte-identical to --output on every platform.
            sys.stdout.buffer.write(text.encode("utf-8"))
            sys.stdout.flush()
        else:
            args.output.write_text(text, encoding="utf-8", newline="\n")
    except (IdentityError, OSError, UnicodeDecodeError) as error:
        print(f"New-PeerModelfile.py: {error}", file=sys.stderr)
        return 1
    print(model_tag(manifest.identity_version, args.base), file=sys.stderr)
    return 0
