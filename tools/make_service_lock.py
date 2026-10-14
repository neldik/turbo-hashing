#!/usr/bin/env python3
"""Prepare a service for `uv sync --frozen` in a container build.

uv sync / uv lock don't accept a constraints file; they only honour
`[tool.uv] constraint-dependencies` in the project's pyproject.toml. So this
script:

  1. copies the service directory (including its committed uv.lock) to a
     scratch dir,
  2. writes `constraint-dependencies` into its pyproject.toml, pinning
     EVERY internal package (direct and transitive) to the version from
     versions.json,
  3. runs `uv lock --no-sources` there against the Artifactory index. uv keeps
     the versions already in uv.lock wherever it can, so only the internal
     packages change,
  4. writes the patched pyproject.toml and the resulting uv.lock to --output,
  5. reports any external package whose version changed compared to the
     committed lock (--strict turns that into an error).

  make_service_lock.py versions.json services/api -o build/pins [--strict]

Externals can move when a new internal version needs a dependency the
committed lock doesn't satisfy (a new or newer library). That is reported, so
the service's committed lock can be refreshed deliberately.

--wheels DIR      file-based registry (prototype): DIR is copied to ../wheels
                  next to the service and used as uv's find-links path, so
                  the lock refers to `../wheels` and works unchanged in an
                  image with the same layout (/app + /wheels).

--externals root  ignores the service's committed lock and pins externals to
                  the root uv.lock instead (for services that are workspace
                  members without a lock of their own).

Env: UV_DEFAULT_INDEX  Artifactory index URL incl. credentials
                       (set by build_container.sh). uv does not write the
                       credentials into uv.lock.
     (not needed with --wheels)
Requires: tomlkit, packaging (e.g. `uv run --with tomlkit --with packaging`).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

import tomlkit
from packaging.utils import canonicalize_name

sys.path.insert(0, str(Path(__file__).resolve().parent))
from make_constraints import external_constraints  # noqa: E402

IGNORE = shutil.ignore_patterns(".venv", "__pycache__", "dist", "build",
                                "node_modules", ".turbo", "*.egg-info")


def locked_versions(lock: Path) -> dict[str, set[str]]:
    """package name -> versions in a uv.lock (a name can appear more than once
    for different markers)."""
    if not lock.is_file():
        return {}
    result: dict[str, set[str]] = {}
    for pkg in tomllib.loads(lock.read_text()).get("package", []):
        if "version" in pkg:
            result.setdefault(canonicalize_name(pkg["name"]), set()).add(pkg["version"])
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("versions", help="output of compute_versions.py")
    parser.add_argument("service_dir", help="directory with the service's pyproject.toml")
    parser.add_argument("-o", "--output", required=True, help="directory for pyproject.toml + uv.lock")
    parser.add_argument("--externals", choices=["lock", "root"], default="lock",
                        help="lock: keep externals from the service's committed uv.lock "
                             "(default); root: pin externals to the root uv.lock")
    parser.add_argument("--wheels", help="file-based registry dir, see above")
    parser.add_argument("--strict", action="store_true",
                        help="fail if any external package version changes")
    args = parser.parse_args()

    if not (os.environ.get("UV_DEFAULT_INDEX") or args.wheels):
        sys.exit("error: set UV_DEFAULT_INDEX or pass --wheels")

    data = json.loads(Path(args.versions).read_text())
    service_dir = Path(args.service_dir).resolve()
    committed_lock = service_dir / "uv.lock"
    doc = tomlkit.parse((service_dir / "pyproject.toml").read_text())
    service_name = canonicalize_name(doc["project"]["name"])

    if args.externals == "lock" and not committed_lock.is_file():
        sys.exit(f"error: {committed_lock} not found; commit a lock for the service "
                 "or use --externals root")

    # The service itself is built from source, so it is not pinned.
    internal_names = {canonicalize_name(n) for n in data["packages"]}
    internal = [f"{name}=={pkg['version']}" for name, pkg in data["packages"].items()
                if canonicalize_name(name) != service_name]
    constraints = internal + (external_constraints() if args.externals == "root" else [])

    tool = doc.setdefault("tool", tomlkit.table())
    uv = tool.setdefault("uv", tomlkit.table())
    # Internal packages come from the registry, so drop their workspace/path
    # sources. The image can then run `uv sync --frozen` without
    # --no-sources (uv refuses that combination).
    sources = uv.get("sources", {})
    for name in [n for n in sources if canonicalize_name(n) in internal_names]:
        del sources[name]
    if "sources" in uv and not uv["sources"]:
        del uv["sources"]
    array = tomlkit.array()
    array.multiline(True)
    for c in list(uv.get("constraint-dependencies", [])) + constraints:
        array.append(c)
    uv["constraint-dependencies"] = array

    service = next((p for n, p in data["packages"].items()
                    if canonicalize_name(n) == service_name), None)
    service_version = service["version"] if service else "0.0.0"

    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / service_dir.name
        shutil.copytree(service_dir, work, ignore=IGNORE)
        (work / "pyproject.toml").write_text(tomlkit.dumps(doc))
        if args.externals == "root":
            (work / "uv.lock").unlink(missing_ok=True)

        env = {**os.environ,
               "SETUPTOOLS_SCM_PRETEND_VERSION": service_version,
               "UV_NO_SOURCES": "1"}
        if args.wheels:
            shutil.copytree(args.wheels, Path(tmp) / "wheels",
                            ignore=shutil.ignore_patterns("*.json", ".*"))
            env["UV_FIND_LINKS"] = "../wheels"
        # No --upgrade: uv keeps every locked version that still satisfies
        # the requirements, so only the internal packages move.
        subprocess.run(["uv", "lock", "--no-sources"], cwd=work, check=True, env=env)

        shutil.copy(work / "pyproject.toml", out / "pyproject.toml")
        shutil.copy(work / "uv.lock", out / "uv.lock")

    (out / "APP_VERSION").write_text(service_version + "\n")

    # Report externals that moved compared to the committed lock.
    if args.externals == "lock":
        before = locked_versions(committed_lock)
        after = locked_versions(out / "uv.lock")
        changed = []
        for name in sorted(set(before) | set(after)):
            if name in internal_names or name == service_name:
                continue
            if before.get(name) != after.get(name):
                old = ",".join(sorted(before.get(name, []))) or "-"
                new = ",".join(sorted(after.get(name, []))) or "-"
                changed.append(f"{name}: {old} -> {new}")
        if changed:
            print("external packages changed vs. the committed uv.lock "
                  "(refresh the service's lock to make this explicit):", file=sys.stderr)
            for line in changed:
                print(f"  {line}", file=sys.stderr)
            if args.strict:
                sys.exit("error: --strict and external packages changed")

    print(f"wrote {out}/pyproject.toml and uv.lock: {len(internal)} internal pins, "
          f"externals from {'service uv.lock' if args.externals == 'lock' else 'root uv.lock'}, "
          f"service version {service_version}")


if __name__ == "__main__":
    main()
