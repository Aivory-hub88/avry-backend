"""GET /api/v1/agent-api/ping: key check without spending credits or reaching the gateway."""
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routes import agent_api_keys as mod


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(mod.router)
    return TestClient(app)


def test_ping_rejects_missing_or_bad_key(client):
    with patch.object(mod, "_resolve_api_key", return_value=None):
        r = client.get("/api/v1/agent-api/ping", headers={"X-Aivory-Api-Key": "nope"})
    assert r.status_code == 401


def test_ping_ok_for_valid_key_and_eligible_tier(client):
    info = {"id": "k1", "user_id": "u1", "agent_type": "customer_service"}
    with patch.object(mod, "_resolve_api_key", return_value=info), \
         patch.object(mod, "load_user_record", return_value={"tier": "pro"}), \
         patch.object(mod, "_tier_ok", return_value=True), \
         patch.object(mod.credit_service, "get_status") as credits, \
         patch.object(mod.requests, "post") as gateway:
        r = client.get("/api/v1/agent-api/ping", headers={"X-Aivory-Api-Key": "avk_live_x"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "agent_type": "customer_service"}
    credits.assert_not_called()
    gateway.assert_not_called()


def test_ping_403_when_tier_no_longer_qualifies(client):
    info = {"id": "k1", "user_id": "u1", "agent_type": "customer_service"}
    with patch.object(mod, "_resolve_api_key", return_value=info), \
         patch.object(mod, "load_user_record", return_value={"tier": "free"}), \
         patch.object(mod, "_tier_ok", return_value=False):
        r = client.get("/api/v1/agent-api/ping", headers={"X-Aivory-Api-Key": "avk_live_x"})
    assert r.status_code == 403
