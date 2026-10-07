"""Regression tests for failure modes that previously surfaced as HTTP 500.

Azure Monitor Action Groups only retry webhooks on 408/429/503/504 or network
errors, so any bare 500 silently drops a Service Health notification.
"""
import json
import socket
import ssl
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from urllib.error import URLError

import pytest
from azure.core.exceptions import (
    ClientAuthenticationError,
    HttpResponseError,
    ResourceNotFoundError,
)
from flask import Flask

from service_health.auth import encode_test_principal
from service_health.config import ServiceHealthSettings
from service_health.models import LifecycleStatus
from service_health.parser import parse_service_health_alert
from service_health.routes import create_service_health_blueprint
from service_health.routing import RoutingConfig
from service_health.slack import SlackIncidentNotifier, TransientSlackError
from service_health.storage import (
    AzureTableIncidentStore,
    StoreDecision,
    TransientStoreError,
)
from test_service_health import FakeTableClient, common_alert


REPO_ROOT = Path(__file__).resolve().parent.parent

# Payload sent by "Test action group" -> "Service health alert", copied from
# https://learn.microsoft.com/azure/azure-monitor/alerts/alerts-payload-samples
ACTION_GROUP_TEST_PAYLOAD = {
    "schemaId": "azureMonitorCommonAlertSchema",
    "data": {
        "essentials": {
            "alertId": (
                "/subscriptions/aaaa0a0a-bb1b-cc2c-dd3d-eeeeee4e4e4e/"
                "providers/Microsoft.AlertsManagement/alerts/1234abcd"),
            "alertRule": "test-ServiceHealthAlertRule",
            "severity": "Sev4",
            "signalType": "ActivityLog",
            "monitorCondition": "Fired",
            "monitoringService": "ServiceHealth",
            "alertTargetIDs": [
                "/subscriptions/aaaa0a0a-bb1b-cc2c-dd3d-eeeeee4e4e4e"],
            "originAlertId": "eeeeeeee-4444-5555-6666-ffffffffffff",
            "firedDateTime": "2025-04-15T18:20:36.256Z",
            "description": "Alertruledescription",
            "essentialsVersion": "1.0",
            "alertContextVersion": "1.0",
        },
        "alertContext": {
            "authorization": None,
            "channels": 1,
            "claims": None,
            "caller": None,
            "correlationId": "aaaa0000-bb11-2222-33cc-444444dddddd",
            "eventSource": 2,
            "eventTimestamp": "2025-04-15T18:20:36.256Z",
            "httpRequest": None,
            "eventDataId": "eeeeeeee-4444-5555-6666-ffffffffffff",
            "level": 3,
            "operationName": "Microsoft.ServiceHealth/incident/action",
            "operationId": "aaaa0000-bb11-2222-33cc-444444dddddd",
            "properties": {
                "title": "TestActionGroup-TestServiceHealthAlert",
                "service": "AzureServiceName",
                "region": "Global",
                "communication": "<p>ThisisatestfromServiceHealthAlert</p>",
                "incidentType": "Incident",
                "trackingId": "TEST-TTT",
                "impactStartTime": "2025-04-15T18:20:36.256Z",
                "impactMitigationTime": "2025-04-15T18:20:36.256Z",
                "impactedServices": [{
                    "ImpactedRegions": [{"RegionName": "Global"}],
                    "ServiceName": "AzureServiceName",
                }],
                "impactedServicesTableRows": "<tr><td>x</td></tr>",
                "defaultLanguageTitle": "TestActionGroup-TestServiceHealthAlert",
                "defaultLanguageContent": "<p>Test</p>",
                "stage": "Resolved",
                "communicationId": "11223344556677",
                "isHIR": "false",
                "IsSynthetic": "True",
                "impactType": "SubscriptionList",
                "version": "0.1.1",
            },
            "status": "Resolved",
            "subStatus": None,
            "submissionTimestamp": "2025-04-15T18:20:36.256Z",
            "ResourceType": None,
        },
    },
}


# --- Sample payloads used by the documentation ----------------------------

def test_action_group_test_payload_is_accepted():
    event = parse_service_health_alert(ACTION_GROUP_TEST_PAYLOAD)
    assert event.tracking_id == "TEST-TTT"
    assert event.lifecycle_status == LifecycleStatus.RESOLVED
    assert event.impacted_services[0].name == "AzureServiceName"


def test_documented_sample_payload_is_accepted():
    payload = json.loads(
        (REPO_ROOT / "docs" / "sample-service-health-alert.json")
        .read_text(encoding="utf-8"))
    event = parse_service_health_alert(payload)
    assert event.tracking_id == "SAMPLE-0001"
    assert event.lifecycle_status == LifecycleStatus.ACTIVE
    assert "<p>" not in event.communication


def test_routes_example_is_valid():
    example = REPO_ROOT / "config" / "service_health_routes.example.json"
    routing = RoutingConfig.from_dict(
        json.loads(example.read_text(encoding="utf-8")))
    assert routing.default_channel_id


# --- Slack network failures -------------------------------------------------

@pytest.mark.parametrize("error", [
    URLError("name resolution failed"),
    socket.timeout("timed out"),
    TimeoutError("timed out"),
    ssl.SSLError("handshake failed"),
    ConnectionResetError("reset"),
])
def test_slack_network_errors_are_transient(error):
    client = MagicMock()
    client.chat_postMessage.side_effect = error
    client.chat_update.side_effect = error
    event = parse_service_health_alert(common_alert())
    notifier = SlackIncidentNotifier(client)

    with pytest.raises(TransientSlackError):
        notifier.create(event, "C1", LifecycleStatus.ACTIVE)
    with pytest.raises(TransientSlackError):
        notifier.update(event, "C1", "1.0", LifecycleStatus.UPDATED)


# --- Azure Table failures ---------------------------------------------------

def _http_error(cls, status):
    error = cls(message="boom")
    error.status_code = status
    return error


@pytest.mark.parametrize("error", [
    _http_error(HttpResponseError, 403),
    _http_error(ResourceNotFoundError, 404),
    _http_error(ClientAuthenticationError, None),
])
def test_store_permission_and_identity_errors_are_transient(error):
    table = MagicMock()
    table.create_entity.side_effect = error
    store = AzureTableIncidentStore(table)
    event = parse_service_health_alert(common_alert())

    with pytest.raises(TransientStoreError):
        store.begin(event, "C1")


def test_store_read_errors_are_transient():
    table = MagicMock()
    table.get_entity.side_effect = _http_error(HttpResponseError, 403)
    store = AzureTableIncidentStore(table)
    event = parse_service_health_alert(common_alert())

    with pytest.raises(TransientStoreError):
        store.begin(event, "C1")


def test_store_clips_oversized_text_properties():
    table = FakeTableClient()
    store = AzureTableIncidentStore(table)
    payload = common_alert()
    payload["data"]["alertContext"]["properties"]["communication"] = (
        "x" * 100_000)
    event = parse_service_health_alert(payload)

    item = store.begin(event, "C1")

    assert item.decision == StoreDecision.CREATE
    assert len(table.entity["communication"]) <= 16_000
    assert item.entity["pendingFingerprint"] == event.fingerprint


# --- HTTP surface -----------------------------------------------------------

class ExplodingProcessor:
    def process(self, event):
        raise KeyError("unexpected")


def test_rejected_identity_reason_is_logged(caplog):
    settings = ServiceHealthSettings(
        table_endpoint="https://example.table.core.windows.net",
        table_name="ServiceHealthIncidents",
        routing=RoutingConfig.from_dict(
            {"default_channel_id": "CDEFAULT", "rules": []}),
        app_environment="production",
        expected_audience="api://expected",
    )
    runtime = SimpleNamespace(settings=settings, processor=None)
    flask_app = Flask(__name__)
    flask_app.register_blueprint(
        create_service_health_blueprint(lambda: runtime))
    headers = {
        "X-MS-CLIENT-PRINCIPAL-IDP": "aad",
        "X-MS-CLIENT-PRINCIPAL": encode_test_principal([
            ("appid", settings.expected_client_app_id),
            ("aud", "api://someone-else"),
            ("roles", "ActionGroupsSecureWebhook"),
        ]),
    }

    response = flask_app.test_client().post(
        "/api/service-health", json=common_alert(), headers=headers)

    assert response.status_code == 403
    assert "audience is not authorized" in caplog.text


def test_unexpected_processing_errors_return_503_not_500():
    settings = ServiceHealthSettings(
        table_endpoint="https://example.table.core.windows.net",
        table_name="ServiceHealthIncidents",
        routing=RoutingConfig.from_dict(
            {"default_channel_id": "CDEFAULT", "rules": []}),
        app_environment="test",
    )
    runtime = SimpleNamespace(
        settings=settings, processor=ExplodingProcessor())
    flask_app = Flask(__name__)
    flask_app.register_blueprint(
        create_service_health_blueprint(lambda: runtime))

    response = flask_app.test_client().post(
        "/api/service-health", json=common_alert())

    assert response.status_code == 503
    assert response.get_json()["error"] == "service_unavailable"
