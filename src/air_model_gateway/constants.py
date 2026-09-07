"""Enums shared across air-model-gateway. Kept dependency-free so anything can import them."""

from __future__ import annotations

from enum import StrEnum


class Environment(StrEnum):
    LOCAL = "local"
    STAGING = "staging"
    PROD = "prod"


class Action(StrEnum):
    """Also the wire value of ``InferenceRequest.task`` — chat and embeddings are
    both a policy action and the request's task discriminator, so one enum
    serves both rather than keeping two vocabularies in sync."""

    CHAT = "chat"
    EMBEDDINGS = "embeddings"
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
    #: if this slot later points at vLLM/TGI in another environment, the value
    #: becomes misleading until renamed again — the settings slot underneath
    #: (config.py::SelfHostedSettings, still named generically) doesn't change,
    #: only this label does.
    SELF_HOSTED = "llama.cpp"
