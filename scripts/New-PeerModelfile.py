#!/usr/bin/env python3
"""New-PeerModelfile.py: generate the projectbuddy identity Modelfile from pinned sources (#217).

Usage:
    New-PeerModelfile.py --manifest tools/peer/identity/identity.json --base qwen2.5:7b [--root .] [--output Modelfile]

Prints the model tag (projectbuddy:<identity-version>-<base>) on stderr. Standard library only.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from peer_identity.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
