"""air-llm-client: the SDK every AIR service imports to reach air-llm directly,
in every environment — no broker indirection, no dependency on air-infra."""

from air_llm_client.client import AirLLMClient

__all__ = ["AirLLMClient"]
__version__ = "0.1.0"
