# syntax=docker/dockerfile:1.10
# Lives in the service directory (e.g. services/api/Dockerfile); the build
# context is that directory. Built by tools/build_container.sh, which provides:
#   --build-context pins=<dir with patched pyproject.toml + uv.lock>
#   --secret id=uv_default_index   (Artifactory index URL incl. credentials)
#   --build-arg APP_VERSION        (version of the service itself)
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv

ARG APP_VERSION
WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

# 1. Dependencies only: this layer is cached until the lock changes.
RUN --mount=type=bind,from=pins,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,from=pins,source=uv.lock,target=uv.lock \
    --mount=type=secret,id=uv_default_index,env=UV_DEFAULT_INDEX \
    --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# 2. The service itself, built from source. The patched pyproject.toml and
#    uv.lock replace the ones in the build context. No UV_NO_SOURCES: uv
#    refuses it with --frozen; make_service_lock.py already removed the
#    workspace sources of internal packages from the patched pyproject.toml.
COPY . /app
COPY --from=pins pyproject.toml uv.lock /app/
RUN --mount=type=secret,id=uv_default_index,env=UV_DEFAULT_INDEX \
    --mount=type=cache,target=/root/.cache/uv \
    SETUPTOOLS_SCM_PRETEND_VERSION="${APP_VERSION}" \
    uv sync --frozen --no-dev --no-editable

CMD ["python", "-m", "my_api"]
