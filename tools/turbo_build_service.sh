#!/usr/bin/env bash
# `build` task of a service, run by Turborepo in the service directory.
#
# Prototype stand-in for tools/build_container.sh: same steps, but instead of
# `docker buildx build` the "image" is a scratch directory outside the repo
# into which the service files, the pinned pyproject.toml + uv.lock and the
# registry's wheels are copied. There it runs exactly what the Dockerfile
# runs (`uv sync --frozen --no-sources`), so it cannot see the workspace.
#
# dist/ (the turbo output) gets versions.json, the pinned lock, and
# image.json: what was actually installed in the "image".
set -euo pipefail
tools=$(cd "$(dirname "$0")" && pwd)
service_dir=$PWD
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

rm -rf dist && mkdir -p dist

# 1. versions of every package for this commit (same computation as publish)
"$tools/py" "$tools/compute_versions.py" -o dist/versions.json

# 2. fail early and clearly if this commit's publish didn't complete
if ! "$tools/py" "$tools/publish_packages.py" dist/versions.json --check-only; then
  echo "error: not all package versions for this commit are published; rerun the publish job" >&2
  exit 1
fi

# 3. registry access for uv. Artifactory: UV_DEFAULT_INDEX (a BuildKit secret
#    in the real build). Local registry: its wheels are copied into the image
#    as /wheels and the lock refers to them as ../wheels.
eval "$("$tools/py" "$tools/registry.py" uv-env dist/versions.json)"
lock_args=()
if [[ -n ${UV_FIND_LINKS:-} ]]; then
  lock_args=(--wheels "$UV_FIND_LINKS")
fi

# 4. patched pyproject.toml + uv.lock: internal packages re-pinned, externals
#    kept from the service's committed uv.lock
"$tools/py" "$tools/make_service_lock.py" dist/versions.json "$service_dir" \
  -o dist/pins ${lock_args[@]+"${lock_args[@]}"} ${LOCK_ARGS:-}
app_version=$(cat dist/pins/APP_VERSION)

# 5. the "container build" (see examples/Dockerfile); nothing outside $image
#    is used, the workspace in particular is out of reach
image=$work/image
mkdir -p "$image/app"
rsync -a --exclude .venv --exclude dist --exclude '*.egg-info' --exclude __pycache__ \
  "$service_dir/" "$image/app/"                              # COPY . /app
cp dist/pins/pyproject.toml dist/pins/uv.lock "$image/app/"  # COPY --from=pins
if [[ -n ${UV_FIND_LINKS:-} ]]; then                         # COPY wheels /wheels
  mkdir -p "$image/wheels" && cp "$UV_FIND_LINKS"/*.whl "$image/wheels/"
  unset UV_FIND_LINKS                                        # the lock has ../wheels
fi
(
  cd "$image/app"
  UV_PROJECT_ENVIRONMENT=$image/venv UV_PYTHON_DOWNLOADS=never \
  SETUPTOOLS_SCM_PRETEND_VERSION="$app_version" \
    uv sync --frozen --no-dev --no-editable --python 3.12
)

# 6. smoke test + record what is installed
"$image/venv/bin/python" -m pkg_c
uv pip list --python "$image/venv/bin/python" --format json > dist/image.json
echo "image built: pkg-c $app_version"
