from dataclasses import replace

import pytest
from musclememory.config import Settings


def test_hosted_configuration_fails_closed():
    valid = Settings(
        deployment_mode="hosted",
        public_url="https://memory.example",
        database_url="postgresql+psycopg://memory:example@postgres/musclememory",
        allowed_hosts=("memory.example", "127.0.0.1:8080"),
        allowed_origins=("https://memory.example",),
    )
    for changes in [
        {"public_url": "http://memory.example"},
        {"public_url": "https://secret@memory.example"},
        {"database_url": "sqlite:///local.db"},
        {"allowed_hosts": ("*",)},
        {"allowed_hosts": ("testserver",)},
        {"allowed_origins": ("https://*",)},
        {"allowed_origins": ("https://another.example",)},
    ]:
        with pytest.raises(ValueError):
            replace(valid, **changes)


def test_host_boundary_and_private_response_headers(http):
    assert http.get("/health", headers={"Host": "evil.example"}).status_code == 400
    response = http.get("/v1/session")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
