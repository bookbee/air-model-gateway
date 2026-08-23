# syntax=docker/dockerfile:1
# Multi-stage, non-root runtime for air-llm.

FROM python:3.12-slim AS build
WORKDIR /app
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
COPY pyproject.toml README.md ./
COPY src ./src
COPY client ./client
RUN pip install --prefix=/install ".[providers]"

FROM python:3.12-slim AS runtime
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN useradd --create-home --uid 10001 air
COPY --from=build /install /usr/local
COPY src ./src
COPY client ./client
COPY deploy ./deploy
USER air
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8080/v1/health').status==200 else 1)"
CMD ["uvicorn", "air_llm.main:app", "--host", "0.0.0.0", "--port", "8080"]
