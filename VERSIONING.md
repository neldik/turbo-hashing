# Package versioning for the Python monorepo

## Problem

Today every commit on `main` is tagged, and a follow-up commit updates the
versions of all internal dependencies in every `pyproject.toml`. That second
commit touches every package, so Turborepo considers *all* packages changed
and rebuilds everything.

We want:

- no version numbers committed in `pyproject.toml` files,
- Turborepo change detection to keep working,
- container builds (which install from Artifactory, outside the workspace) to
  install exactly the right version of every internal and external package,
- robustness against failed, partial and aborted pipelines.

## Solution in one sentence

**The version of a package is a pure function of the commit:** on a release
tag it is the tag's version, otherwise it is the last release plus the
package's Turborepo hash.

```
tagged commit (v1.5.0)  ->  every package: 1.5.0                    -> prod repo
any other commit        ->  each package:  1.4.0+t<turbo-hash[:12]>  -> dev repo
```

Because Turborepo's hash for a package covers its sources and (via
`dependsOn: ["^build"]`) the hashes of its internal dependencies, the version
changes exactly when Turborepo thinks the package changed. If Turborepo's change
detection works, the versioning works.

## Versioning rules

| Situation | Version | Repository |
|---|---|---|
| `HEAD` has a tag matching `v*` | tag without the `v` (e.g. `1.5.0`) for **all** packages | prod |
| Any other commit | `<last release>+t<first 12 chars of turbo hash>` | dev |
| No release tag exists yet | `0.0.0+t<hash>` | dev |

Why these choices:

- **Same content -> same version.** An unchanged package keeps its version and
  its already-published wheel. Nothing is re-tagged or republished.
- **Last release as the dev base**, not the next one. PEP 440 orders
  `1.4.0 < 1.4.0+tabc... < 1.5.0`, so dev builds read as "after 1.4.0".
- **Releases publish every package** at the release version. Releases are rare,
  and it keeps prod stateless: one release = one number, every internal pin is
  `==1.5.0`.
- **Prod and dev never mix.** The target repository is derived from the same
  computation as the version (`release` flag), not from a separate CI variable,
  so a hash version cannot end up in prod by misconfiguration.
- The `+local` segment means dev versions can never be uploaded to public PyPI.
  They are internal only, which is fine.

## No state, so failures heal themselves

Nothing is stored between pipelines: no manifest, no per-package git tags, no
"last successful build" bookkeeping. Every step recomputes the versions from the
commit.

- **Failed / aborted publish:** rerun the pipeline. It computes the same versions,
  finds which wheels already exist in Artifactory, and builds and uploads only
  the missing ones.
- **Two pipelines publishing the same package version at once** (e.g. two commits
  that didn't touch `pkg-b`): the second upload is rejected because the repo
  disallows redeploys; the script re-checks, sees the version exists, and
  treats that as success.
- **Container build for a commit whose publish failed:** the container script
  checks that every pinned version exists before building and fails with a clear
  message. It never builds an image with wrong versions.

## Pipeline flows

### Every commit on `main` (dev)

```bash
python tools/compute_versions.py -o versions.json   # release=false
python tools/publish_packages.py versions.json       # builds+uploads missing wheels to dev
```

Turborepo still runs tests/lint as usual. Publishing does **not** use Turborepo's
build cache (it builds wheels directly), so a cached wheel with a stale version
can never be restored.

### Release tag `vX.Y.Z` (prod)

Same two commands. `compute_versions.py` detects the tag, assigns `X.Y.Z` to every
package and sets `release=true`; `publish_packages.py` then targets the prod repo.

A commit that is first pushed to `main` and later tagged runs twice: once as dev,
once as release. That is correct and needs no special handling.

Release wheels are fresh builds of the same source as the tested dev wheels
(the version is baked into a wheel, so artifacts can't be "promoted").

### Container builds

Services are installed from their own `pyproject.toml` with `uv sync`, with
the build context being the service directory.

```bash
tools/build_container.sh services/api registry.example.com/api:latest
```

**Principle: the service's committed `uv.lock` keeps locking all external
packages; only the internal packages are re-pinned at build time.**

`uv sync` and `uv lock` don't accept a constraints file. They only honour
`[tool.uv] constraint-dependencies` in the project's own `pyproject.toml`. So
CI adds the internal pins there, relocks, and the container only runs
`uv sync --frozen` against the result. The script:

1. computes the versions for the current commit,
2. verifies that every internal package version exists in the right repo,
3. runs `make_service_lock.py`, which copies the service (with its committed
   `uv.lock`) to a scratch dir, adds `constraint-dependencies` pinning **every**
   internal package to its version from step 1, and runs
   `uv lock --no-sources` against Artifactory. Without `--upgrade`, uv keeps
   every locked version that still satisfies the requirements, so only the
   internal packages change,
4. runs `docker buildx build`, passing the patched `pyproject.toml` and the
   updated `uv.lock` as a named build context (`--build-context pins=...`), the
   index URL with credentials as a BuildKit secret, and the service's own
   version as `APP_VERSION`.

Why `constraint-dependencies` for all internal packages instead of rewriting
the service's direct dependencies to `pkg-a==...`: the service depends on
`pkg-a`, which depends on `pkg-b`, and `pkg-a`'s wheel declares `pkg-b`
unpinned. Rewriting only direct dependencies would leave `pkg-b` free to
resolve to any version. Constraints pin direct and transitive internals alike
and don't change what the service declares.

**When externals still move.** If a new internal version needs a library the
committed lock doesn't satisfy (a new dependency, or a higher minimum), uv adds
or bumps exactly that package. `make_service_lock.py` compares the result with
the committed lock and lists every external that changed. Refresh the
service's committed lock to make the change explicit. Set
`LOCK_ARGS="--strict"` to fail the build instead.

For a service without its own lock (a workspace member), use
`LOCK_ARGS="--externals root"`: externals are then pinned to the root
`uv.lock` via `uv export --no-emit-workspace`.

The Dockerfile (in the service dir) then does:

```dockerfile
# dependencies only, cached until the lock changes
RUN --mount=type=bind,from=pins,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,from=pins,source=uv.lock,target=uv.lock \
    --mount=type=secret,id=uv_default_index,env=UV_DEFAULT_INDEX \
    uv sync --frozen --no-dev --no-install-project

# the service itself, built from source
COPY . /app
COPY --from=pins pyproject.toml uv.lock /app/
RUN --mount=type=secret,id=uv_default_index,env=UV_DEFAULT_INDEX \
    SETUPTOOLS_SCM_PRETEND_VERSION="${APP_VERSION}" \
    uv sync --frozen --no-dev --no-editable
```

Details:

- `UV_NO_SOURCES=1` / `--no-sources` makes uv ignore
  `[tool.uv.sources] ... { workspace = true }`, so internal packages come from
  Artifactory instead of a workspace that doesn't exist in the container.
- The service is built from source without `.git`, so its version comes from
  `APP_VERSION` via `SETUPTOOLS_SCM_PRETEND_VERSION`.
- The index URL comes in through `UV_DEFAULT_INDEX` as a BuildKit secret. uv
  doesn't write credentials into `uv.lock`, and the secret never ends up in an
  image layer. If the Artifactory repo is a virtual repo that also proxies PyPI,
  external packages resolve from the same index.
- The service's committed `uv.lock` may have been created with workspace
  sources (internal packages as local paths). Relocking with `--no-sources`
  turns those entries into registry packages; that is expected and doesn't
  count as an external change.
- The committed lock in the repo is never modified; the updated one only
  exists in the build.

Alternative with a smaller change: `uv pip install --constraint constraints.txt .`
(with `make_constraints.py`) also installs the service from its
`pyproject.toml`, without a generated lockfile, at the cost of losing
`uv sync`'s lockfile semantics and hashes.

## Requirements and assumptions

- Each Python package has a `package.json` (so Turborepo sees it) and a
  `pyproject.toml` with a static `name` and `dynamic = ["version"]`, using
  setuptools-scm or hatch-vcs. The scripts set
  `SETUPTOOLS_SCM_PRETEND_VERSION_FOR_<NAME>` for each build; both backends
  honour it. setuptools-scm remains the fallback for local builds.
- Internal dependencies are declared **unpinned** in `pyproject.toml`
  (`dependencies = ["pkg-b"]`) with `[tool.uv.sources] pkg-b = { workspace = true }`.
  Pins come from the generated `constraint-dependencies`, not from wheel metadata.
- Turborepo package names may differ from Python distribution names; the
  scripts read the distribution name from each package's `pyproject.toml`.
- CI checks out full history including tags (`fetch-depth: 0`).
  `compute_versions.py` refuses to run in a shallow clone.
- Both Artifactory PyPI repos are configured to **disallow redeploy**, so a
  version can never be overwritten with different content.
- Tools on the CI runner: `git`, `turbo`, `uv` (builds with `uv build`,
  uploads with `uv publish`), `python >= 3.11` with `packaging` (and
  `tomlkit`, pulled in via `uv run --with`), `docker` with buildx.
  No pip or twine needed.

Environment variables used by the scripts:

| Variable | Example |
|---|---|
| `ARTIFACTORY_URL` | `https://acme.jfrog.io/artifactory` |
| `ARTIFACTORY_DEV_REPO` | `pypi-dev-local` |
| `ARTIFACTORY_PROD_REPO` | `pypi-prod-local` |
| `ARTIFACTORY_USER` / `ARTIFACTORY_TOKEN` | credentials |
| `TURBO_BIN` (optional) | `npx turbo` (default `turbo`) |
| `VERSION_TURBO_TASK` (optional) | task whose hash is used (default `build`) |

## Gotchas

- **Hash stability across jobs.** The publish job and the container job must
  compute the same hashes. Keep the `build` task's `env` list in `turbo.json`
  free of variables that differ between CI jobs, and run both jobs with the same
  Turborepo version.
- **Make the hash cover everything that ends up in the wheel:** sources,
  `pyproject.toml`, package data. Check the task's `inputs`.
- **Global changes republish everything once**: upgrading Turborepo, or changing
  a file in `globalDependencies` (e.g. `uv.lock`). Harmless, just more uploads.
- **Artifactory index latency.** Artifactory recalculates the PyPI index
  asynchronously. A container build started seconds after publishing may not
  see the new version yet; the existence check uses AQL (database), so if pip
  fails right after a publish, a short retry is enough.
- **Traceability.** Hash versions aren't human-readable. `publish_packages.py`
  sets the commit SHA as an Artifactory property (`vcs.revision`) on each upload,
  for information only.

## Files

| File | Purpose |
|---|---|
| `tools/compute_versions.py` | Version for every package at `HEAD` -> `versions.json` |
| `tools/publish_packages.py` | Build and upload missing wheels; `--check-only` to verify |
| `tools/make_constraints.py` | Internal pins + locked externals (`constraints.txt`); used by the next script |
| `tools/make_service_lock.py` | Service lock with internal packages re-pinned; reports moved externals |
| `tools/build_container.sh` | Container build using the above (`uv sync --frozen`) |
| `examples/turbo.json` | Turborepo task config the hashes rely on |
| `examples/Dockerfile` | Container that installs from Artifactory with constraints |
| `examples/pyproject.toml` | Package config: dynamic version, unpinned internal deps |
| `examples/package.json` | Makes a Python package visible to Turborepo |
