"""air-model-gateway-client: a reference Python client for air-model-gateway's
``POST /v1/generate`` and ``POST /v1/embed``. Services may use it or speak the wire
contract (docs/openapi.json) directly."""

from air_model_gateway_client.client import AirModelGatewayClient, AirModelGatewayError

__all__ = ["AirModelGatewayClient", "AirModelGatewayError"]
__version__ = "0.1.0"
