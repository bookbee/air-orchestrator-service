# air-orchestrator-service — multi-stage, non-root runtime.
#
# The wheel is built in the first stage and installed into a clean second stage, so
# the shipped image carries no build toolchain and no source tree.

# ── build ─────────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS build

WORKDIR /build

RUN pip install --no-cache-dir hatchling

# Copied before the source so that a source-only change does not invalidate the
# layer that resolved the build backend.
COPY pyproject.toml README.md ./
COPY src ./src

RUN pip wheel --no-cache-dir --no-deps --wheel-dir /wheels .

# ── runtime ───────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# PYTHONUNBUFFERED: structlog writes to stdout, and a buffered stream means a
# container's logs arrive in bursts — or not at all if it is killed.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# A fixed uid rather than a name only: a volume mounted from the host has numeric
# ownership, and `useradd` without --uid picks whatever is next.
RUN groupadd --gid 10001 air \
 && useradd --uid 10001 --gid air --no-create-home --shell /usr/sbin/nologin air

WORKDIR /app

COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl "air-orchestrator-service[all]" \
 && rm -rf /wheels

USER air

EXPOSE 8081

# No HEALTHCHECK: /v1/health is dependency-free and orchestrators (compose,
# Kubernetes) declare their own probes with the right timings for their
# environment. Baking one in here would be a second, weaker source of truth —
# compose.yml below defines it.

# Exec form, so the process receives SIGTERM directly and uvicorn's graceful
# shutdown actually runs. The console script reads host/port/workers from
# AIR_ORCHESTRATOR_SERVICE__APP__*.
ENTRYPOINT ["air-orchestrator-service"]
