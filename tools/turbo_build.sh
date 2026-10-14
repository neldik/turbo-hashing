#!/usr/bin/env bash
# Entry point for `turbo run build` (npm run build).
#
# A package's version is <last release>+t<hash>, or the tag on a release commit.
# The tag state is not a file, so turbo would not see it change: a release
# commit would be all cache hits (nothing published to prod), and after a
# release an unchanged package would get a new version (0.1.0+t<same hash>)
# that is never built. VERSIONING_TAGS puts the tag state into every task hash
# (turbo.json "env"); compute_versions.py checks it matches git.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
export VERSIONING_TAGS
VERSIONING_TAGS=$(tools/py tools/compute_versions.py --tag-state)
exec turbo run build "$@"
