"""The public wire contract of POST /v1/embed: texts in, one vector per text out.

The API layer translates it into the internal ``EmbeddingsRequest``
(schemas/models.py) the router and adapters speak.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from air_model_gateway.constants import Provider
from air_model_gateway.schemas.gateway import EMBED_NAME_PATTERN
from air_model_gateway.schemas.models import Usage

_EXAMPLES: list[JsonValue] = [{"model": "local-768-v1", "input": ["hello", "world"]}]

#: Texts per call: OpenAI's own per-request limit, the tightest of the embedding
#: providers; Ollama served 2,500 here and crashed its runner at 3,000. A caller
#: with more splits the batch. Checked up front so an oversized request is an
#: immediate 422 rather than a provider tied up for seconds before a 502.
MAX_EMBED_INPUTS = 2048
#: Characters per text: about 8k tokens, OpenAI's per-input limit. Ollama's
#: nomic-embed-text reads less (2k tokens) and truncates the rest silently.
MAX_EMBED_TEXT_CHARS = 32_000


class EmbedInput(BaseModel):
    # Unknown fields are the caller's 422, as on /v1/generate.
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": _EXAMPLES})

    model: str = Field(
        pattern=EMBED_NAME_PATTERN,
        description=(
            "A published embedding model, `<local|cloud>-<dimensions>-v<version>`, e.g. "
            "`local-768-v1`. Required: it is your index's identity. A name never changes "
            "meaning, so vectors from it always stay comparable; `/v1/capabilities` lists "
            "the published names."
        ),
    )
    input: list[Annotated[str, Field(max_length=MAX_EMBED_TEXT_CHARS)]] = Field(
        min_length=1,
        max_length=MAX_EMBED_INPUTS,
        description=(
            f"The texts to embed, at most {MAX_EMBED_INPUTS:,} of at most "
            f"{MAX_EMBED_TEXT_CHARS:,} characters each; one vector comes back per text."
        ),
    )


class EmbedOutput(BaseModel):
    id: str = Field(description="Gateway-assigned id.")
    embedding_model: str = Field(description="The published name the vectors belong to.")
    dimensions: int = Field(description="Length of every vector; check it against your index.")
    model: str = Field(description="The provider's model behind that name.")
    provider: Provider = Field(description="The provider that answered.")
    embeddings: list[list[float]] = Field(description="One vector per input text, in order.")
    usage: Usage
    cost_usd: float = Field(
        default=0.0, description="Priced from the gateway's table; 0 for local models."
    )
