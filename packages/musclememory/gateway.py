"""Stable stdio transport forwarding to the authenticated, reloadable REST service."""

import hmac
from urllib.parse import urlsplit

import httpx

from .core import DomainError
from .sdk import MuscleMemoryClient
from .security import Principal, digest_key


class RESTGateway:
    def __init__(self, url, key, *, client=None):
        endpoint = urlsplit(url)
        if (
            endpoint.username
            or endpoint.password
            or endpoint.query
            or endpoint.fragment
            or not (
                endpoint.scheme == "https"
                or endpoint.scheme == "http"
                and endpoint.hostname in {"localhost", "127.0.0.1", "::1"}
            )
        ):
            raise ValueError("Use HTTPS or loopback without URL credentials")
        self.client = client or MuscleMemoryClient(url, key, timeout=10)
        self.key_digest = digest_key(key)

    def close(self):
        self.client.close()

    @staticmethod
    def _call(call):
        try:
            return call()
        except httpx.HTTPStatusError as error:
            try:
                detail = error.response.json()["error"]
                raise DomainError(
                    detail["code"], detail["message"], error.response.status_code
                ) from None
            except (ValueError, KeyError, TypeError):
                raise DomainError(
                    "remote_error",
                    "Memory service rejected the request",
                    error.response.status_code,
                ) from None
        except httpx.HTTPError:
            raise DomainError(
                "service_unavailable", "Local memory service is unavailable", 503
            ) from None

    def authenticate(self, key, rate_limit=True):
        if not hmac.compare_digest(digest_key(key), self.key_digest):
            raise DomainError("unauthorized", "A valid bearer credential is required", 401)

        def identity():
            response = self.client.http.get("/v1/session")
            response.raise_for_status()
            return response.json()

        result = self._call(identity)
        return Principal(result["key_id"], result["tenant"], result["grants"])

    def invoke(self, method, request):
        return self._call(
            lambda: getattr(self.client, "musclememory_" + method)(
                **request.model_dump(mode="json")
            )
        )

    def recall(self, principal, request):
        return self.invoke("recall", request)

    def inspect(self, principal, request):
        return self.invoke("inspect", request)

    def observe(self, principal, request):
        return self.invoke("observe", request)

    def learn(self, principal, request):
        return self.invoke("learn", request)

    def evaluate(self, principal, request):
        return self.invoke("evaluate", request)
