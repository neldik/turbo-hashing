#!/usr/bin/env bash
# Build a service container that installs the service from its own
# pyproject.toml with `uv sync --frozen`, with every dependency (internal and
# external) pinned for the current commit.
#
#   tools/build_container.sh <service-dir> <image-tag> [extra docker args...]
#   tools/build_container.sh services/api registry.example.com/api:latest
#
# Externals come from the service's committed uv.lock; only internal packages
# are re-pinned. Set LOCK_ARGS to pass options to make_service_lock.py, e.g.
#   LOCK_ARGS="--strict"            fail if any external would change
#   LOCK_ARGS="--externals root"    pin externals to the root uv.lock instead
#
# Env: ARTIFACTORY_URL, ARTIFACTORY_DEV_REPO, ARTIFACTORY_PROD_REPO,
#      ARTIFACTORY_USER, ARTIFACTORY_TOKEN
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "usage: $0 <service-dir> <image-tag> [docker args...]" >&2
  exit 2
fi
service_dir=$1; image_tag=$2; shift 2

repo_root=$(git rev-parse --show-toplevel)
tools="$repo_root/tools"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

# 1. versions for this commit (same computation as the publish job)
python "$tools/compute_versions.py" -o "$work/versions.json"

release=$(python -c 'import json,sys; print(str(json.load(open(sys.argv[1]))["release"]).lower())' \
          "$work/versions.json")
if [[ $release == true ]]; then repo=$ARTIFACTORY_PROD_REPO; else repo=$ARTIFACTORY_DEV_REPO; fi
echo "building $service_dir from $repo (release=$release)"

# 2. fail early and clearly if this commit's publish didn't complete
if ! python "$tools/publish_packages.py" "$work/versions.json" --check-only; then
  echo "error: not all package versions for this commit are published; rerun the publish job" >&2
  exit 1
fi

# 3. index URL with credentials; used for locking here and as a BuildKit
#    secret in the build (never stored in an image layer or in uv.lock)
export UV_DEFAULT_INDEX
UV_DEFAULT_INDEX=$(TARGET_REPO="$repo" python - <<'PY'
import os, urllib.parse as u
base = u.urlsplit(os.environ["ARTIFACTORY_URL"].rstrip("/"))
cred = f"{u.quote(os.environ['ARTIFACTORY_USER'], safe='')}:{u.quote(os.environ['ARTIFACTORY_TOKEN'], safe='')}"
print(f"{base.scheme}://{cred}@{base.netloc}{base.path}/api/pypi/{os.environ['TARGET_REPO']}/simple")
PY
)

# 4. patched pyproject.toml + uv.lock for the service
(cd "$repo_root" && uv run --no-project --with tomlkit --with packaging \
   python "$tools/make_service_lock.py" "$work/versions.json" "$service_dir" \
     -o "$work/pins" ${LOCK_ARGS:-})
app_version=$(cat "$work/pins/APP_VERSION")

docker buildx build \
  --build-context pins="$work/pins" \
  --secret id=uv_default_index,env=UV_DEFAULT_INDEX \
  --build-arg APP_VERSION="$app_version" \
  --label org.opencontainers.image.revision="$(git rev-parse HEAD)" \
  --label org.opencontainers.image.version="$app_version" \
  -t "$image_tag" \
  "$@" \
  "$service_dir"
