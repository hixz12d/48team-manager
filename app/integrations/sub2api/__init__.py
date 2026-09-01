"""Sub2API binding and runtime only. HTTP Admin API, never compose."""

from app.integrations.sub2api.client import Sub2ApiClient, sub2api_client

__all__ = ["Sub2ApiClient", "sub2api_client"]
