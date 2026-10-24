# syntax=docker/dockerfile:1
# Multi-stage, non-root runtime for air-model-gateway.
#
# Reproducible: dependencies come from requirements.lock (exported from uv.lock,
# every package pinned and hash-checked), so the image ships exactly what the
# tests ran against. `make lock` updates it; nothing here resolves versions.
# The base image is pinned to a patch release; bump it deliberately.
ARG PYTHON_IMAGE=python:3.12.15-slim

# The tag is pinned in the ARG above; hadolint can't see through the ARG.
# hadolint ignore=DL3006
FROM ${PYTHON_IMAGE} AS build
WORKDIR /app
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
COPY requirements.lock ./
RUN pip install --prefix=/install --require-hashes --no-deps -r requirements.lock
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --prefix=/install --no-deps .

# hadolint ignore=DL3006
FROM ${PYTHON_IMAGE} AS runtime
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN useradd --create-home --uid 10001 air
# The service runs from the installed wheel in site-packages. Beside it, only the
# default routing, pricing and policy files it reads by relative path; named
# one by one, so nothing else under deploy/ (generated secrets included) can
# ever land in an image. .dockerignore enforces the same allowlist.
COPY --from=build /install /usr/local
COPY deploy/routing.yaml deploy/pricing.yaml ./deploy/
COPY deploy/policy ./deploy/policy
USER air
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8080/v1/health').status==200 else 1)"
CMD ["uvicorn", "air_model_gateway.main:app", "--host", "0.0.0.0", "--port", "8080"]
