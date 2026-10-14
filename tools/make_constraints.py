#!/usr/bin/env python3
"""Write a constraints file (for `uv pip install -c`) for a container build:

  * every internal package pinned to the version from versions.json
  * every external package pinned to the version locked in uv.lock

  make_constraints.py versions.json -o constraints.txt
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

# Constraints must not carry extras: "name[extra]==1.0" -> "name==1.0"
EXTRAS = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\[[^\]]*\]")


def external_constraints() -> list[str]:
    out = subprocess.run(
        ["uv", "export", "--frozen", "--all-packages", "--no-emit-workspace",
         "--no-hashes", "--no-header", "--no-annotate", "--format", "requirements-txt"],
        check=True, capture_output=True, text=True,
    ).stdout
    lines = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-e") or line.startswith("."):
            sys.exit(f"error: unexpected local requirement in uv export: {line}")
        lines.append(EXTRAS.sub(r"\1", line))
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("versions", help="output of compute_versions.py")
    parser.add_argument("-o", "--output", default="constraints.txt")
    args = parser.parse_args()

    data = json.loads(Path(args.versions).read_text())
    internal = [f"{name}=={pkg['version']}" for name, pkg in data["packages"].items()]

    content = "\n".join([
        f"# generated for commit {data['commit']} (release={data['release']})",
        "# internal packages",
        *internal,
        "# external packages (uv.lock)",
        *external_constraints(),
    ]) + "\n"
    Path(args.output).write_text(content)
    print(f"wrote {args.output}: {len(internal)} internal pins")


if __name__ == "__main__":
    main()
