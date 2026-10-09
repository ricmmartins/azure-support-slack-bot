"""Regressions from the end-to-end public-readiness review."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re

import pytest

from service_health.config import (
    InvalidServiceHealthConfiguration,
    ServiceHealthSettings,
)
from service_health.models import ImpactedService, LifecycleStatus
from service_health.parser import parse_service_health_alert
from service_health.routing import RoutingConfig
from service_health.slack import render_incident_message
from service_health.storage import AzureTableIncidentStore, TransientStoreError
from test_service_health import FakeTableClient, common_alert


@pytest.mark.parametrize(("region", "expected"), [
    ("East US", "CDEFAULT"),
    ("West US", "CDATABASE"),
])
def test_service_and_region_must_match_same_impacted_service(region, expected):
    event = replace(
        parse_service_health_alert(common_alert()),
        impacted_services=(
            ImpactedService("Database", ("West US",)),
            ImpactedService("Compute", ("East US",)),
        ),
    )
    routing = RoutingConfig.from_dict({
        "default_channel_id": "CDEFAULT",
        "rules": [{
            "channel_id": "CDATABASE",
            "services": ["database"],
            "regions": [region],
        }],
    })
    assert routing.channel_for(event) == expected


def test_slack_all_text_objects_respect_block_kit_limits():
    event = replace(
        parse_service_health_alert(common_alert()),
        incident_type="&" * 4000,
        subscription_id="&" * 4000,
        tracking_id="&" * 4000,
        communication="&" * 4000,
        title="&" * 4000,
    )
    text, blocks = render_incident_message(event, LifecycleStatus.ACTIVE)
    assert len(text) <= 4000
    for block in blocks:
        if block["type"] == "header":
            assert len(block["text"]["text"]) <= 150
        elif block["type"] == "section":
            for field in block.get("fields", []):
                assert len(field["text"]) <= 2000
            if "text" in block:
                assert len(block["text"]["text"]) <= 3000
        elif block["type"] == "context":
            for element in block["elements"]:
                assert len(element["text"]) <= 3000


def test_explicit_empty_environment_does_not_use_process_secrets(monkeypatch):
    monkeypatch.setenv("AZURE_TABLE_ENDPOINT", "https://real.table.core.windows.net")
    monkeypatch.setenv("SERVICE_HEALTH_ROUTES_JSON", '{"default_channel_id":"C1"}')
    monkeypatch.setenv("APP_ENV", "test")
    with pytest.raises(InvalidServiceHealthConfiguration, match="AZURE_TABLE_ENDPOINT"):
        ServiceHealthSettings.from_env({})


@pytest.mark.parametrize("existing", [False, True])
def test_store_never_adopts_another_requests_lease_after_read(existing):
    class StolenLeaseTable(FakeTableClient):
        steal = False

        def get_entity(self, **kwargs):
            if self.steal:
                self.entity["leaseOwner"] = "another-request"
            return super().get_entity(**kwargs)

        def update_entity(self, **kwargs):
            super().update_entity(**kwargs)
            self.steal = True

    table = StolenLeaseTable()
    store = AzureTableIncidentStore(table)
    event = parse_service_health_alert(common_alert())
    if existing:
        table.create_entity({
            "PartitionKey": event.partition_key,
            "RowKey": event.row_key,
            "channelId": "C1",
            "processingState": "failed",
        })
    else:
        table.steal = True
    with pytest.raises(TransientStoreError, match="changed owner"):
        store.begin(event, "C1")


def test_store_does_not_send_after_reservation_consumes_entire_lease():
    now = datetime.now(timezone.utc)
    times = iter([now, now + timedelta(seconds=31)])
    store = AzureTableIncidentStore(FakeTableClient(), now=lambda: next(times))
    with pytest.raises(TransientStoreError, match="expired"):
        store.begin(parse_service_health_alert(common_alert()), "C1")


@pytest.mark.parametrize("document", ["README.md", "docs/blog-post.md"])
def test_tutorial_json_snippets_are_valid(document):
    root = Path(__file__).resolve().parent.parent
    text = (root / document).read_text(encoding="utf-8")
    snippets = re.findall(r"```json\n(.*?)\n```", text, re.DOTALL)
    assert snippets
    for snippet in snippets:
        value = json.loads(snippet)
        if "schemaId" in value:
            parse_service_health_alert(value)
        else:
            RoutingConfig.from_dict(value)
