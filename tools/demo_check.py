#!/usr/bin/env python3
"""Checks for demo.sh after a `npm run build`.

  demo_check.py <expected rebuilt packages, comma separated or "-">

Verifies, for the last turbo run:
  * exactly the expected tasks were executed (cache misses),
  * every version is the one the rules prescribe for its turbo hash
    (<base>+t<hash[:12]>, or the tag on a release commit),
  * the published wheels exist in the right registry (dev/prod),
  * the "image" of service C contains exactly those internal versions and
    the externals from C's committed uv.lock.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

from packaging.utils import canonicalize_name

sys.path.insert(0, str(Path(__file__).resolve().parent))
from compute_versions import last_release_version, release_version  # noqa: E402
from registry import get_registry  # noqa: E402

SERVICE = Path("services/svc-c")
errors: list[str] = []


def check(ok: bool, msg: str) -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {msg}")
    if not ok:
        errors.append(msg)


def main() -> None:
    expected = set() if sys.argv[1] == "-" else set(sys.argv[1].split(","))

    run = max(Path(".turbo/runs").glob("*.json"), key=os.path.getmtime)
    tasks = json.loads(run.read_text())["tasks"]
    executed = {t["package"] for t in tasks if t["cache"]["status"] == "MISS"}
    hashes = {t["package"]: t["hash"] for t in tasks}
    check(executed == expected,
          f"rebuilt: {sorted(executed) or 'nothing'} (expected {sorted(expected) or 'nothing'})")

    release = release_version()
    base = last_release_version()
    reg = get_registry(release is not None)

    # The service's versions.json has every package (computed via --dry=json).
    versions = {n: p["version"] for n, p in
                json.loads((SERVICE / "dist/versions.json").read_text())["packages"].items()}
    for name, version in versions.items():
        want = release or f"{base}+t{hashes[name][:12]}"
        check(version == want, f"{name} == {version}")

    for name in ("pkg-a", "pkg-b"):
        own = json.loads(Path(f"packages/{name}/dist/versions.json").read_text())
        check(own["packages"][name]["version"] == versions[name],
              f"{name}: version in its own build == version seen by C's build")
        check(reg.exists(name, versions[name]), f"{name} wheel in {reg.repo} registry")

    image = {canonicalize_name(p["name"]): p["version"]
             for p in json.loads((SERVICE / "dist/image.json").read_text())}
    for name, version in versions.items():
        check(image.get(name) == version, f"image has {name}=={image.get(name)}")

    committed = {p["name"]: p["version"]
                 for p in tomllib.loads((SERVICE / "uv.lock").read_text())["package"]
                 if "version" in p and p["name"] not in versions}
    for name, version in committed.items():
        check(image.get(name) == version,
              f"image has external {name}=={image.get(name)} (committed lock: {version})")
    extra = set(image) - set(versions) - set(committed)
    check(not extra, f"no unexpected packages in image {sorted(extra) if extra else ''}")

    if errors:
        sys.exit(f"{len(errors)} check(s) failed")


if __name__ == "__main__":
    main()
