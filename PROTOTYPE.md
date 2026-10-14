# Prototype of VERSIONING.md

A working monorepo that implements the concept with:

| Package | Location | Depends on | Kind |
|---|---|---|---|
| `pkg-a` | `packages/pkg-a` | `six` (external) | uv workspace member, published wheel |
| `pkg-b` | `packages/pkg-b` | `pkg-a` (unpinned, workspace source) | uv workspace member, published wheel |
| `pkg-c` | `services/svc-c` | `pkg-b` (unpinned), `idna` (external) | detached uv project with its own `uv.lock`; the "container" |

## Run it

```bash
npm install
./demo.sh            # scenarios in a scratch clone with an empty registry
```

Or by hand in this repo:

```bash
export LOCAL_REGISTRY=$PWD/registry
npm run build        # = tools/turbo_build.sh -> turbo run build
```

`demo.sh` walks through these commits and checks after each build what was
rebuilt, that every version follows the rules, that the wheels are in the right
registry, and that C's "image" contains exactly the pinned internals plus the
externals from C's committed lock:

| Step | Rebuilt |
|---|---|
| initial build | A, B, C |
| no change | nothing |
| change C | C |
| change B | B, C |
| change A | A, B, C |
| tag `v0.1.0` | A, B, C as `0.1.0` into `registry/prod` |
| first commit after the tag | A, B, C as `0.1.0+t<hash>` |
| change C | C |
| B's wheel lost from the registry | C's build refuses; `publish_packages.py` heals it |

## How the prototype maps to the concept

- **`build` task of A/B** (`tools/turbo_build_package.sh`): `compute_versions.py --self`
  takes the hash from `$TURBO_HASH` (which turbo gives every task, and which equals
  the hash in `--dry=json`), then `publish_packages.py` builds the wheel and
  publishes it if it's missing.
- **`build` task of C** (`tools/turbo_build_service.sh`): the steps of
  `build_container.sh`, but the "image" is a scratch directory outside the repo:
  service files + pinned `pyproject.toml`/`uv.lock` + the registry's wheels are
  copied in, and `uv sync --frozen --no-editable` runs there. The workspace is
  out of reach.
- **Registry** (`tools/registry.py`): `LOCAL_REGISTRY=<dir>` gives a file-based
  registry (`<dir>/dev`, `<dir>/prod`; refuses overwrites like "disallow
  redeploy"). Without it, the original Artifactory code is used.
- **Services** declare `[tool.versioning] publish = false`: they get a version
  (for the image) but no wheel.

## Findings: changes the real system needs too

1. **Build byproducts pollute the turbo hash.** Explicit `inputs` globs include
   gitignored files, and setuptools writes `src/<pkg>.egg-info/` (including the
   version!) during a build. So every build changed the next hash, and CI and
   local hashes differed. Fix in `turbo.json`: `"!**/*.egg-info/**", "!**/__pycache__/**"`.
2. **Tags are invisible to turbo.** A release commit would be all cache hits (nothing
   published to prod), and after a release an unchanged package gets a new
   version (`0.1.0+t<same hash>`) that is never built. Fix: `tools/turbo_build.sh`
   puts the tag state into the hash (`VERSIONING_TAGS`, listed in the task's
   `env`). `compute_versions.py` checks it matches git. A release therefore
   rebuilds everything once, as does the first commit after it.
3. **`uv sync --frozen` refuses `--no-sources`/`UV_NO_SOURCES`** (uv 0.11). The
   old `examples/Dockerfile` would fail. `make_service_lock.py` now removes
   internal packages from `[tool.uv.sources]` in the patched `pyproject.toml`, so
   the image doesn't need `--no-sources` at all. The Dockerfile is updated.
4. **Unpinned resolution picks inconsistent versions:** resolving C against a
   registry with several dev builds gave pkg-a from one commit and pkg-b from
   another (highest local version wins). This confirms that pinning every internal
   package, transitive ones included, is required.
5. **Turbo inside a turbo task works:** C's build runs `turbo --dry=json` while
   turbo is running, and gets the same hashes as the running tasks.

## Prototype-only shortcuts

- Externals come from PyPI next to the file registry (`UV_FIND_LINKS`) instead
  of one virtual Artifactory index.
- C's committed `uv.lock` refers to the registry as `../../registry/dev`, and the
  pinned lock as `../wheels` (relative paths stay relative in `uv.lock`). Refresh it with
  `cd services/svc-c && UV_FIND_LINKS=../../registry/dev uv lock` after a build.
- No Docker: `examples/Dockerfile` is unchanged apart from finding 3.
