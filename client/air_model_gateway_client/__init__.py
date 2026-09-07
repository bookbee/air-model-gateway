"""air-model-gateway-client: the SDK every AIR service imports to reach air-model-gateway directly,
in every environment — no broker indirection, no dependency on air-infra."""

from air_model_gateway_client.client import AirModelGatewayClient

__all__ = ["AirModelGatewayClient"]
__version__ = "0.1.0"
