#!/usr/bin/env python3
"""Compute the version of every Python package in the monorepo for HEAD.

Rules:
  * HEAD carries a tag matching ``v*``  -> every package gets the tag version
    (e.g. ``1.5.0``) and ``release`` is true (publish to prod).
  * otherwise -> each package gets ``<last release>+t<turbo hash[:12]>`` and
    ``release`` is false (publish to dev).

The output is deterministic for a given commit, so every CI job can recompute
it instead of passing state around.

  compute_versions.py -o versions.json          # all packages (turbo --dry=json)
  compute_versions.py --self -o versions.json   # inside a turbo task: only the
                                                # package in the current dir,
                                                # hash taken from $TURBO_HASH

Output (JSON):
{
  "commit": "<sha>",
  "release": false,
  "packages": {
    "pkg-b": {"version": "1.4.0+t3f9a2c1b7e04", "directory": "packages/pkg-b",
              "turbo_package": "pkg-b", "publish": true},
    ...
  }
}
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path

from packaging.version import InvalidVersion, Version

TAG_PATTERN = "v*"
HASH_LENGTH = 12
TURBO_TASK = os.environ.get("VERSION_TURBO_TASK", "build")
TURBO_BIN = shlex.split(os.environ.get("TURBO_BIN", "turbo"))


def run(*cmd: str) -> str:
    return subprocess.run(
        cmd, check=True, capture_output=True, text=True
    ).stdout.strip()


def git(*args: str) -> str:
    return run("git", *args)


def normalize(raw: str) -> str:
    try:
        return str(Version(raw))
    except InvalidVersion:
        sys.exit(f"error: '{raw}' is not a valid PEP 440 version")


def release_version() -> str | None:
    """Version from a v* tag pointing exactly at HEAD, if any."""
    try:
        tag = git("describe", "--tags", "--exact-match", "--match", TAG_PATTERN, "HEAD")
    except subprocess.CalledProcessError:
        return None
    return normalize(tag.removeprefix("v"))


def last_release_version() -> str:
    """Most recent v* tag reachable from HEAD, or 0.0.0."""
    try:
        tag = git("describe", "--tags", "--abbrev=0", "--match", TAG_PATTERN, "HEAD")
    except subprocess.CalledProcessError:
        return "0.0.0"
    # Drop any local segment just in case; the dev version adds its own.
    return Version(normalize(tag.removeprefix("v"))).public


def turbo_tasks() -> list[dict]:
    # Same VERSIONING_TAGS as turbo_build.sh, so the hashes match the build's.
    out = subprocess.run(
        [*TURBO_BIN, "run", TURBO_TASK, "--dry=json"], check=True,
        capture_output=True, text=True,
        env={**os.environ, "VERSIONING_TAGS": tag_state()},
    ).stdout
    return json.loads(out)["tasks"]


def project_info(pyproject: Path) -> tuple[str, bool]:
    """Distribution name, and whether it is published as a wheel
    (`[tool.versioning] publish = false` for services, which only get a
    version for their image)."""
    data = tomllib.loads(pyproject.read_text())
    try:
        name = data["project"]["name"]
    except KeyError:
        sys.exit(f"error: {pyproject} has no [project].name")
    return name, data.get("tool", {}).get("versioning", {}).get("publish", True)


def tag_state() -> str:
    """Release state of HEAD as one string, part of every turbo task hash
    (VERSIONING_TAGS, set by turbo_build.sh)."""
    return f"release={release_version() or ''};base={last_release_version()}"


def check_tag_state() -> None:
    expected = tag_state()
    actual = os.environ.get("VERSIONING_TAGS")
    if actual != expected:
        sys.exit(f"error: VERSIONING_TAGS={actual!r}, but git says {expected!r}; "
                 "run the build via tools/turbo_build.sh (npm run build)")


def self_task(package_dir: Path, root: Path) -> list[dict]:
    """The current turbo task, described like an entry of --dry=json.
    TURBO_HASH is the same hash `turbo run --dry=json` reports for it."""
    turbo_hash = os.environ.get("TURBO_HASH")
    if not turbo_hash:
        sys.exit("error: --self needs TURBO_HASH (run it as a turbo task)")
    package = json.loads((package_dir / "package.json").read_text())["name"]
    return [{"task": TURBO_TASK, "package": package, "hash": turbo_hash,
             "directory": package_dir.relative_to(root).as_posix()}]


def compute(tasks: list[dict]) -> dict:
    if git("rev-parse", "--is-shallow-repository") == "true":
        sys.exit("error: shallow clone; fetch full history and tags (fetch-depth: 0)")

    release = release_version()
    base = last_release_version()

    packages: dict[str, dict] = {}
    for task in tasks:
        if task.get("task") != TURBO_TASK:
            continue
        directory = task.get("directory", "")
        pyproject = Path(directory) / "pyproject.toml"
        if not pyproject.is_file():
            continue  # not a Python package (e.g. a JS package)

        name, publish = project_info(pyproject)
        version = release or f"{base}+t{task['hash'][:HASH_LENGTH]}"
        if name in packages:
            sys.exit(f"error: distribution name '{name}' used by two packages")
        packages[name] = {
            "version": version,
            "directory": directory,
            "turbo_package": task["package"],
            "publish": publish,
        }

    if not packages:
        sys.exit(f"error: turbo reported no '{TURBO_TASK}' tasks for Python packages")

    return {
        "commit": git("rev-parse", "HEAD"),
        "release": release is not None,
        "packages": dict(sorted(packages.items())),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("-o", "--output", help="write JSON here (default: stdout)")
    parser.add_argument("--self", action="store_true", dest="self_only",
                        help="only the package in the current directory, using $TURBO_HASH")
    parser.add_argument("--tag-state", action="store_true",
                        help="print the value for VERSIONING_TAGS and exit")
    args = parser.parse_args()
    if args.tag_state:
        print(tag_state())
        return
    output = Path(args.output).resolve() if args.output else None

    cwd = Path.cwd()
    root = Path(git("rev-parse", "--show-toplevel"))
    os.chdir(root)
    if args.self_only or "TURBO_HASH" in os.environ:  # inside a turbo task
        check_tag_state()
    tasks = self_task(cwd, root) if args.self_only else turbo_tasks()
    result = json.dumps(compute(tasks), indent=2) + "\n"

    if output:
        output.write_text(result)
    else:
        sys.stdout.write(result)


if __name__ == "__main__":
    main()
