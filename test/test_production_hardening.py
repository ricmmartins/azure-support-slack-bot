"""Regression tests for issues found during the production readiness review."""
import io
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask
from slack_sdk.errors import SlackApiError

from service_health.auth import (
    InvalidWebhookIdentity,
    authorize_easy_auth,
    encode_test_principal,
)
from service_health.config import (
    InvalidServiceHealthConfiguration,
    ServiceHealthSettings,
)
from service_health.models import LifecycleStatus
from service_health.parser import parse_service_health_alert
from service_health.routes import create_service_health_blueprint
from service_health.routing import RoutingConfig
from service_health.slack import (
    PermanentSlackError,
    SlackIncidentNotifier,
    render_incident_message,
)
from test.test_service_health import StubProcessor, common_alert


AZNS_APP_ID = "461e8683-5575-4561-ac7f-899cc907d62a"
API_CLIENT_ID = "11111111-2222-3333-4444-555555555555"
BASE_ENV = {
    "AZURE_TABLE_ENDPOINT": "https://example.table.core.windows.net",
    "SERVICE_HEALTH_ROUTES_JSON": json.dumps(
        {"default_channel_id": "CDEFAULT", "rules": []}),
}


def _principal_headers(audience):
    return {
        "X-MS-CLIENT-PRINCIPAL-IDP": "aad",
        "X-MS-CLIENT-PRINCIPAL": encode_test_principal([
            ("appid", AZNS_APP_ID),
            ("aud", audience),
            ("roles", "ActionGroupsSecureWebhook"),
        ]),
    }


# --- Authentication -------------------------------------------------------

@pytest.mark.parametrize("token_audience", [
    f"api://{API_CLIENT_ID}",   # v1 access token
    API_CLIENT_ID,              # v2 access token
])
def test_easy_auth_accepts_v1_and_v2_audiences(token_audience):
    authorize_easy_auth(
        _principal_headers(token_audience),
        AZNS_APP_ID,
        "ActionGroupsSecureWebhook",
        f"api://{API_CLIENT_ID},{API_CLIENT_ID}",
    )


def test_easy_auth_rejects_unknown_audience():
    with pytest.raises(InvalidWebhookIdentity):
        authorize_easy_auth(
            _principal_headers("api://someone-else"),
            AZNS_APP_ID,
            "ActionGroupsSecureWebhook",
            f"api://{API_CLIENT_ID},{API_CLIENT_ID}",
        )


def test_settings_require_easy_auth_when_app_env_is_missing():
    with pytest.raises(InvalidServiceHealthConfiguration):
        ServiceHealthSettings.from_env(dict(BASE_ENV))
    settings = ServiceHealthSettings.from_env({
        **BASE_ENV,
        "SERVICE_HEALTH_EXPECTED_AUDIENCE": API_CLIENT_ID,
    })
    assert settings.app_environment == "production"
    assert settings.require_easy_auth


def test_invalid_routing_is_reported_as_configuration_error():
    with pytest.raises(InvalidServiceHealthConfiguration):
        ServiceHealthSettings.from_env({
            **BASE_ENV,
            "APP_ENV": "test",
            "SERVICE_HEALTH_ROUTES_JSON": json.dumps({"rules": "nope"}),
        })


# --- HTTP surface ---------------------------------------------------------

def _client(get_runtime):
    flask_app = Flask(__name__)
    flask_app.register_blueprint(create_service_health_blueprint(get_runtime))
    return flask_app.test_client()


def _runtime(max_payload_bytes=262_144):
    settings = ServiceHealthSettings(
        table_endpoint="https://example.table.core.windows.net",
        table_name="ServiceHealthIncidents",
        routing=RoutingConfig.from_dict(
            {"default_channel_id": "CDEFAULT", "rules": []}),
        app_environment="test",
        max_payload_bytes=max_payload_bytes,
    )
    return SimpleNamespace(settings=settings, processor=StubProcessor())


def test_readyz_returns_503_for_unexpected_initialization_errors():
    def broken_runtime():
        raise RuntimeError("credential failure")

    assert _client(broken_runtime).get("/readyz").status_code == 503


def test_payload_limit_applies_without_content_length():
    runtime = _runtime(max_payload_bytes=64)
    body = json.dumps(common_alert()).encode("utf-8")

    # Simulates chunked transfer encoding: no Content-Length header.
    response = _client(lambda: runtime).post(
        "/api/service-health",
        input_stream=io.BytesIO(body),
        headers={"Content-Type": "application/json"},
        environ_overrides={"wsgi.input_terminated": True},
    )
    assert response.status_code == 413
    assert runtime.processor.events == []


def test_invalid_json_body_returns_422():
    response = _client(lambda: _runtime()).post(
        "/api/service-health",
        data=b"{not json",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422


# --- Parsing and rendering ------------------------------------------------

def test_html_communication_is_rendered_as_text():
    payload = common_alert()
    payload["data"]["alertContext"]["properties"]["communication"] = (
        "<p><strong>Summary of impact:</strong> Between 10:00 and 11:00"
        "<br>customers saw errors &amp; timeouts.</p>"
        "<ul><li>East US</li><li>West US</li></ul>"
    )
    event = parse_service_health_alert(payload)
    assert "<" not in event.communication
    assert "Summary of impact: Between 10:00 and 11:00\n" in event.communication
    assert "errors & timeouts." in event.communication
    assert "• East US" in event.communication

    text, blocks = render_incident_message(event, LifecycleStatus.ACTIVE)
    communication_block = blocks[3]["text"]["text"]
    assert "errors &amp; timeouts." in communication_block
    assert "<strong>" not in communication_block


def test_fallback_text_is_escaped():
    payload = common_alert()
    payload["data"]["alertContext"]["properties"]["title"] = "A <b>&</b> B"
    event = parse_service_health_alert(payload)
    text, _ = render_incident_message(event, LifecycleStatus.ACTIVE)
    assert "<b>" not in text
    assert "&amp;" in text


@pytest.mark.parametrize(("stage", "status", "expected"), [
    ("Planned", "Active", LifecycleStatus.ACTIVE),
    ("InProgress", "Active", LifecycleStatus.UPDATED),
    ("Rescheduled", "Active", LifecycleStatus.UPDATED),
    ("Complete", "Active", LifecycleStatus.RESOLVED),
    ("Canceled", "Active", LifecycleStatus.RESOLVED),
    ("Active", "Resolved", LifecycleStatus.RESOLVED),
])
def test_planned_maintenance_stages_are_supported(stage, status, expected):
    event = parse_service_health_alert(common_alert(stage, status))
    assert event.lifecycle_status == expected


# --- Slack update fallback ------------------------------------------------

def _slack_error(code):
    return SlackApiError("error", SimpleNamespace(
        get=lambda key, default=None: code if key == "error" else default,
        status_code=200,
    ))


def test_update_reposts_when_original_message_was_deleted():
    client = MagicMock()
    client.chat_update.side_effect = _slack_error("message_not_found")
    client.chat_postMessage.return_value = {"ts": "2.0"}
    event = parse_service_health_alert(common_alert("Updated"))

    ts = SlackIncidentNotifier(client).update(
        event, "C1", "1.0", LifecycleStatus.UPDATED)

    assert ts == "2.0"
    client.chat_postMessage.assert_called_once()


def test_update_still_raises_other_permanent_errors():
    client = MagicMock()
    client.chat_update.side_effect = _slack_error("channel_not_found")
    event = parse_service_health_alert(common_alert("Updated"))

    with pytest.raises(PermanentSlackError):
        SlackIncidentNotifier(client).update(
            event, "C1", "1.0", LifecycleStatus.UPDATED)
    client.chat_postMessage.assert_not_called()
