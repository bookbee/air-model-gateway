"""Enums shared across air-model-gateway. Kept dependency-free so anything can import them."""

from __future__ import annotations

from enum import StrEnum


class Environment(StrEnum):
    LOCAL = "local"
    STAGING = "staging"
    PROD = "prod"


class Action(StrEnum):
    """Policy actions. GENERATE and EMBED are named after the routes they guard
    (POST /v1/generate, POST /v1/embed), so a grant reads the same as the call
    it allows: one vocabulary across routes, policy files, metrics and the
    audit line."""

    GENERATE = "generate"
    EMBED = "embed"
    ADMIN_READ = "admin_read"


class Provider(StrEnum):
    OLLAMA = "ollama"
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    GEMINI = "gemini"
    #: Any OpenAI-wire-compatible self-hosted backend — currently llama.cpp's
    #: `llama-server` (the value names today's concrete backend, matching how
    #: OLLAMA does — not "self_hosted", the generic category). Deliberately
    #: its own identity, not OPENAI pointed at a different base_url:
    #: PriceBook.is_free() keys off Provider identity, and conflating the two
    #: would risk billing a free local model at OpenAI's cloud rate the moment
    #: its name didn't happen to match a local prefix. Trade-off worth naming:
    #: if this slot points at vLLM/TGI in some environment, this label should
    #: change to match; the settings slot underneath
    #: (config.py::SelfHostedSettings, named generically) stays as it is.
    SELF_HOSTED = "llama.cpp"


class ModelClass(StrEnum):
    """What a caller of POST /v1/generate chooses instead of a model: where the
    model runs. The owner decides, in deploy/routing.yaml, which models serve
    each class (per service, if needed)."""

    #: Our own model servers only; the prompt never leaves the network.
    LOCAL = "local"
    #: The gateway's choice (local first today, cloud as fallback). The default.
    AUTO = "auto"
    #: Cloud providers only.
    CLOUD = "cloud"


#: Providers that run on our own servers. A `local` chain or a `local-*`
#: embedding model may only use these; a `cloud` one only the others.
LOCAL_PROVIDERS: frozenset[Provider] = frozenset({Provider.OLLAMA, Provider.SELF_HOSTED})
