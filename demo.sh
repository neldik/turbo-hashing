#!/usr/bin/env bash
# End-to-end demo of the versioning concept (see VERSIONING.md).
#
# Clones this repo into a scratch directory (committed state only) with an
# empty file-based registry, then walks through a series of commits, running
# `npm run build` (turbo) after each and checking what was rebuilt and which
# versions ended up in the wheels and in service C's "image".
#
#   ./demo.sh [scratch-dir]
set -euo pipefail
src=$(cd "$(dirname "$0")" && pwd)
demo=${1:-$(mktemp -d)/versioning-demo}

rm -rf "$demo"
git clone -q "$src" "$demo"
cd "$demo"
git config user.name demo && git config user.email demo@example.com
npm ci --silent --no-audit --no-fund

export LOCAL_REGISTRY=$demo/registry TURBO_TELEMETRY_DISABLED=1
log=$demo/build.log

build() {   # build, then check which packages were rebuilt
  if ! npm run -s build -- --summarize >"$log" 2>&1; then
    tail -20 "$log"; echo "build failed (full log: $log)"; return 1
  fi
  tools/py tools/demo_check.py "$1"
}
commit() { git commit -qam "$1"; }
step() { printf '\n=== %s\n' "$*"; }

step "initial build: everything is new"
build pkg-a,pkg-b,pkg-c

step "nothing changed: everything cached"
build -

step "change C: only C rebuilds"
echo '# change C' >> services/svc-c/src/pkg_c/__init__.py; commit "change C"
build pkg-c

step "change B: B and C rebuild"
echo '# change B' >> packages/pkg-b/src/pkg_b/__init__.py; commit "change B"
build pkg-b,pkg-c

step "change A: A, B and C rebuild"
echo '# change A' >> packages/pkg-a/src/pkg_a/__init__.py; commit "change A"
build pkg-a,pkg-b,pkg-c

step "tag v0.1.0: everything rebuilt as 0.1.0 into the prod registry"
git tag v0.1.0
build pkg-a,pkg-b,pkg-c

step "first commit after the release: dev versions are now 0.1.0+t<hash>"
echo '# change B again' >> packages/pkg-b/src/pkg_b/__init__.py; commit "change B again"
build pkg-a,pkg-b,pkg-c

step "change C again: only C rebuilds"
echo '# change C again' >> services/svc-c/src/pkg_c/__init__.py; commit "change C again"
build pkg-c

step "lost publish: pkg-b's wheel is missing, C's build must refuse"
echo '# change C, 3rd' >> services/svc-c/src/pkg_c/__init__.py; commit "change C 3rd"
tools/py tools/compute_versions.py -o versions.json
b_version=$(python3 -c 'import json; print(json.load(open("versions.json"))["packages"]["pkg-b"]["version"])')
rm "$LOCAL_REGISTRY"/dev/pkg_b-"$b_version"-*.whl
if npm run -s build >"$log" 2>&1; then
  echo "  FAIL build succeeded although pkg-b $b_version is missing"; exit 1
fi
grep -E "missing:|error: not all" "$log" | sed 's/^/  /'
echo "  ok   build refused; healing with the publish job (no turbo cache involved)"
tools/py tools/publish_packages.py versions.json | sed 's/^/  /'
build pkg-c

printf '\nall scenarios passed (demo repo: %s)\n' "$demo"
