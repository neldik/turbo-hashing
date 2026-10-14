#!/usr/bin/env bash
# `build` task of a Python package, run by Turborepo in the package directory.
#
# Version: <last release>+t<TURBO_HASH[:12]> (or the tag version on a release
# commit), i.e. a pure function of this task's turbo hash. The wheel is built
# with that version and published if the registry doesn't have it yet.
# dist/ (the turbo output) gets the wheel and its versions.json.
set -euo pipefail
tools=$(cd "$(dirname "$0")" && pwd)

rm -rf dist && mkdir -p dist
"$tools/py" "$tools/compute_versions.py" --self -o dist/versions.json
"$tools/py" "$tools/publish_packages.py" dist/versions.json --wheel-dir dist
