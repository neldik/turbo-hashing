"""Package registries the versioning scripts publish to and check against.

Two backends, chosen by environment:

  * LOCAL_REGISTRY=<dir>  file-based prototype: <dir>/dev and <dir>/prod are
                          flat directories of wheels (usable with uv's
                          --find-links / UV_FIND_LINKS). Writes are refused if
                          the file already exists, like Artifactory with
                          redeploy disallowed.
  * otherwise             Artifactory (ARTIFACTORY_URL, ARTIFACTORY_DEV_REPO,
                          ARTIFACTORY_PROD_REPO, ARTIFACTORY_USER,
                          ARTIFACTORY_TOKEN).

The target (dev or prod) always follows the `release` flag of versions.json;
there is deliberately no way to override it.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from packaging.utils import canonicalize_name


def env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        sys.exit(f"error: environment variable {name} is not set")
    return value


def wheel_name(name: str) -> str:
    """Distribution name as it appears in a wheel filename."""
    return canonicalize_name(name).replace("-", "_")


class AlreadyExists(Exception):
    """Upload rejected because the file is already in the registry."""


class LocalRegistry:
    def __init__(self, root: str, release: bool) -> None:
        self.repo = "prod" if release else "dev"
        self.path = (Path(root) / self.repo).resolve()
        self.path.mkdir(parents=True, exist_ok=True)

    def __str__(self) -> str:
        return str(self.path)

    def find_wheels(self, name: str, version: str) -> list[Path]:
        return sorted(self.path.glob(f"{wheel_name(name)}-{version}-*.whl"))

    def exists(self, name: str, version: str) -> bool:
        return bool(self.find_wheels(name, version))

    def upload(self, wheel: Path) -> None:
        target = self.path / wheel.name
        tmp = self.path / f".{wheel.name}.{os.getpid()}.tmp"
        shutil.copyfile(wheel, tmp)
        try:
            # os.link fails if target exists: atomic "no redeploy".
            os.link(tmp, target)
        except FileExistsError:
            raise AlreadyExists(wheel.name) from None
        finally:
            tmp.unlink()

    def set_property(self, item: Path, key: str, value: str) -> None:
        props = item.with_name(item.name + ".properties.json")
        data = json.loads(props.read_text()) if props.exists() else {}
        data[key] = value
        props.write_text(json.dumps(data, indent=2) + "\n")

    def uv_env(self) -> dict[str, str]:
        """Env that lets uv resolve internal packages from this registry
        (externals still come from the default index, PyPI)."""
        return {"UV_FIND_LINKS": str(self.path)}


class Artifactory:
    def __init__(self, release: bool) -> None:
        self.repo = env("ARTIFACTORY_PROD_REPO" if release else "ARTIFACTORY_DEV_REPO")
        self.base = env("ARTIFACTORY_URL").rstrip("/")
        self.user = env("ARTIFACTORY_USER")
        self.token = env("ARTIFACTORY_TOKEN")

    def __str__(self) -> str:
        return self.repo

    def _request(self, method: str, path: str, body: bytes | None = None,
                 content_type: str | None = None) -> bytes:
        req = urllib.request.Request(f"{self.base}{path}", data=body, method=method)
        creds = base64.b64encode(f"{self.user}:{self.token}".encode()).decode()
        req.add_header("Authorization", f"Basic {creds}")
        if content_type:
            req.add_header("Content-Type", content_type)
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read()

    def find_wheels(self, name: str, version: str) -> list[dict]:
        """Wheels for name==version in the repo, via AQL (database, no index lag)."""
        file_prefix = f"{wheel_name(name)}-{version}-"
        query = "items.find(%s).include(\"repo\",\"path\",\"name\")" % json.dumps({
            "repo": self.repo,
            "name": {"$match": f"{file_prefix}*.whl"},
        })
        out = self._request("POST", "/api/search/aql", query.encode(), "text/plain")
        return json.loads(out)["results"]

    def exists(self, name: str, version: str) -> bool:
        return bool(self.find_wheels(name, version))

    def upload(self, wheel: Path) -> None:
        try:
            subprocess.run(
                ["uv", "publish", "--publish-url", f"{self.base}/api/pypi/{self.repo}",
                 "--username", self.user, "--password", self.token, str(wheel)],
                check=True,
            )
        except subprocess.CalledProcessError:
            # Redeploy is disallowed: a rejected upload may mean a concurrent
            # pipeline already published it. The caller re-checks existence.
            raise AlreadyExists(wheel.name) from None

    def set_property(self, item: dict, key: str, value: str) -> None:
        path = urllib.parse.quote(f"{item['repo']}/{item['path']}/{item['name']}")
        props = urllib.parse.quote(f"{key}={value}")
        self._request("PUT", f"/api/storage/{path}?properties={props}")

    def uv_env(self) -> dict[str, str]:
        base = urllib.parse.urlsplit(self.base)
        cred = (f"{urllib.parse.quote(self.user, safe='')}:"
                f"{urllib.parse.quote(self.token, safe='')}")
        return {"UV_DEFAULT_INDEX":
                f"{base.scheme}://{cred}@{base.netloc}{base.path}/api/pypi/{self.repo}/simple"}


def get_registry(release: bool) -> LocalRegistry | Artifactory:
    local = os.environ.get("LOCAL_REGISTRY")
    if local:
        return LocalRegistry(local, release)
    return Artifactory(release)


if __name__ == "__main__":
    # `registry.py uv-env versions.json` prints shell exports for uv.
    if len(sys.argv) == 3 and sys.argv[1] == "uv-env":
        release = json.loads(Path(sys.argv[2]).read_text())["release"]
        import shlex
        for k, v in get_registry(release).uv_env().items():
            print(f"export {k}={shlex.quote(v)}")
    else:
        sys.exit("usage: registry.py uv-env versions.json")
