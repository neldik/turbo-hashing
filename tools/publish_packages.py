#!/usr/bin/env python3
"""Build and upload every package version from versions.json that is missing
in Artifactory. Idempotent: safe to rerun after a failed or aborted pipeline.

  publish_packages.py versions.json              # build + upload missing
  publish_packages.py versions.json --check-only # exit 1 if anything is missing

  publish_packages.py versions.json --wheel-dir dist
                                    # also keep a copy of each built wheel

Target repo: prod if versions.json says release=true, otherwise dev. There is
deliberately no flag to override it. See registry.py for the backends
(LOCAL_REGISTRY=<dir> for the file-based prototype, else Artifactory).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import Version

sys.path.insert(0, str(Path(__file__).resolve().parent))
from registry import AlreadyExists, get_registry  # noqa: E402


def pretend_env(name: str, version: str) -> dict[str, str]:
    """Env that makes setuptools-scm / hatch-vcs use our version."""
    key = re.sub(r"[-_.]+", "_", name).upper()
    return {
        **os.environ,
        f"SETUPTOOLS_SCM_PRETEND_VERSION_FOR_{key}": version,
        # Generic fallback; safe because each build is its own process.
        "SETUPTOOLS_SCM_PRETEND_VERSION": version,
    }


def build_wheel(directory: str, name: str, version: str, out_dir: Path) -> Path:
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(out_dir), directory],
        check=True, env=pretend_env(name, version),
    )
    wheels = list(out_dir.glob("*.whl"))
    if len(wheels) != 1:
        sys.exit(f"error: expected one wheel for {name}, got {wheels}")
    wheel = wheels[0]

    # Guard: never upload a wheel whose version isn't the one we computed.
    built_name, built_version, _, _ = parse_wheel_filename(wheel.name)
    if canonicalize_name(built_name) != canonicalize_name(name) or built_version != Version(version):
        sys.exit(f"error: built {wheel.name}, expected {name}=={version}. "
                 "Does the build backend honour SETUPTOOLS_SCM_PRETEND_VERSION?")
    return wheel


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("versions", help="output of compute_versions.py")
    parser.add_argument("--check-only", action="store_true",
                        help="only verify that all versions exist")
    parser.add_argument("--wheel-dir", help="also copy each built wheel here")
    args = parser.parse_args()

    data = json.loads(Path(args.versions).read_text())
    wheel_dir = Path(args.wheel_dir).resolve() if args.wheel_dir else None
    # Package directories in versions.json are relative to the repo root.
    os.chdir(subprocess.run(["git", "rev-parse", "--show-toplevel"], check=True,
                            capture_output=True, text=True).stdout.strip())
    art = get_registry(data["release"])
    print(f"target repo: {art} (release={data['release']}, commit={data['commit']})")

    published = {n: p for n, p in data["packages"].items() if p.get("publish", True)}
    missing = [(n, p) for n, p in published.items() if not art.exists(n, p["version"])]
    print(f"{len(published) - len(missing)} present, {len(missing)} missing")

    if args.check_only:
        for name, pkg in missing:
            print(f"  missing: {name}=={pkg['version']}", file=sys.stderr)
        sys.exit(1 if missing else 0)

    failed: list[str] = []
    for name, pkg in missing:
        version = pkg["version"]
        print(f"--- {name}=={version}")
        with tempfile.TemporaryDirectory() as tmp:
            wheel = build_wheel(pkg["directory"], name, version, Path(tmp))
            if wheel_dir:
                wheel_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy(wheel, wheel_dir)
            try:
                art.upload(wheel)
            except AlreadyExists:
                # A concurrent pipeline may have uploaded the same version
                # (redeploy is disallowed). Same version = same content.
                if not art.exists(name, version):
                    failed.append(f"{name}=={version}")
                    continue
                print(f"    already uploaded by another pipeline")

        for item in art.find_wheels(name, version):
            try:
                art.set_property(item, "vcs.revision", data["commit"])
            except Exception as exc:  # informational only, never fatal
                print(f"    warning: could not set vcs.revision: {exc}")

    if failed:
        sys.exit("error: failed to publish: " + ", ".join(failed))
    print("all packages published")


if __name__ == "__main__":
    main()
