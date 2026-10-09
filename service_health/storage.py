import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from azure.core import MatchConditions
from azure.core.exceptions import (
    HttpResponseError,
    ResourceExistsError,
    ResourceModifiedError,
    ServiceRequestError,
    ServiceResponseError,
)
from azure.data.tables import UpdateMode

from service_health.models import LifecycleStatus, ServiceHealthEvent


class StoreDecision(str, Enum):
    CREATE = "create"
    UPDATE = "update"
    DUPLICATE = "duplicate"
    STALE = "stale"
    BUSY = "busy"


class TransientStoreError(RuntimeError):
    pass


class StoreConsistencyError(RuntimeError):
    pass


@dataclass(frozen=True)
class IncidentWorkItem:
    decision: StoreDecision
    entity: dict
    etag: str = ""

    @property
    def channel_id(self):
        return self.entity["channelId"]

    @property
    def message_ts(self):
        return self.entity.get("messageTs", "")


def _utcnow():
    return datetime.now(timezone.utc)


def _parse_datetime(value):
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _etag(entity):
    metadata = getattr(entity, "metadata", None)
    if metadata and metadata.get("etag"):
        return metadata["etag"]
    return entity.get("etag") or entity.get("odata.etag") or ""


# Azure Table string properties are limited to 64 KiB (32K UTF-16 chars).
# Stored text is informational only, so long values are clipped.
_MAX_STORED_TEXT = 16_000


def _bounded(value, limit=_MAX_STORED_TEXT):
    return value if len(value) <= limit else value[:limit]


def _event_properties(event):
    return {
        "trackingId": _bounded(event.tracking_id, 256),
        "pendingFingerprint": event.fingerprint,
        "pendingSubmissionTime": event.submission_time,
        "pendingLifecycleStatus": event.lifecycle_status.value,
        "level": event.level.value,
        "title": _bounded(event.title, 1024),
        "impactStartTime": event.impact_start_time,
        "communication": _bounded(event.communication),
        "impactedServicesJson": _bounded(json.dumps(
            [item.as_dict() for item in event.impacted_services],
            ensure_ascii=True,
            separators=(",", ":"),
        )),
        "incidentType": _bounded(event.incident_type, 256),
        "communicationId": _bounded(event.communication_id, 256),
        "eventDataId": _bounded(event.event_data_id, 256),
    }


class AzureTableIncidentStore:
    def __init__(self, table_client, lease_seconds=30, now=_utcnow):
        self.table_client = table_client
        self.lease_seconds = lease_seconds
        self.now = now

    def begin(self, event: ServiceHealthEvent, channel_id):
        now = self.now()
        entity = {
            "PartitionKey": event.partition_key,
            "RowKey": event.row_key,
            "channelId": channel_id,
            "messageTs": "",
            "processingState": "processing",
            "leaseUntil": now + timedelta(seconds=self.lease_seconds),
            "leaseOwner": str(uuid.uuid4()),
            "attemptCount": 1,
            "createdAt": now,
            "updatedAt": now,
            "lastFingerprint": "",
            "lastSubmissionTime": None,
            "lastErrorCode": "",
            **_event_properties(event),
        }
        try:
            self.table_client.create_entity(entity)
            created = self._get(event)
            return self._owned_work_item(StoreDecision.CREATE, created, entity)
        except ResourceExistsError:
            return self._begin_existing(event, now)
        except (ServiceRequestError, ServiceResponseError) as exc:
            raise TransientStoreError(
                "Unable to reserve incident state") from exc
        except HttpResponseError as exc:
            # Includes 403 while RBAC propagates, managed identity token
            # failures and a missing table. Retrying is the only safe option.
            raise TransientStoreError(
                "Unable to reserve incident state") from exc

    def _begin_existing(self, event, now):
        for _ in range(3):
            current = self._get(event)
            if (
                current.get("lastFingerprint") == event.fingerprint
                and current.get("processingState") == "complete"
            ):
                return IncidentWorkItem(
                    StoreDecision.DUPLICATE, current, _etag(current))

            last_submission = _parse_datetime(
                current.get("lastSubmissionTime"))
            if last_submission and event.submission_time <= last_submission:
                return IncidentWorkItem(
                    StoreDecision.STALE, current, _etag(current))

            lease_until = _parse_datetime(current.get("leaseUntil"))
            if (
                current.get("processingState") == "processing"
                and lease_until
                and lease_until > now
            ):
                return IncidentWorkItem(
                    StoreDecision.BUSY, current, _etag(current))

            lease_owner = str(uuid.uuid4())
            current.update({
                "processingState": "processing",
                "leaseUntil": now + timedelta(seconds=self.lease_seconds),
                "leaseOwner": lease_owner,
                "attemptCount": int(current.get("attemptCount", 0)) + 1,
                "updatedAt": now,
                "lastErrorCode": "",
                **_event_properties(event),
            })
            try:
                self._replace(current, _etag(current))
                acquired = self._get(event)
                decision = (
                    StoreDecision.UPDATE
                    if acquired.get("messageTs")
                    else StoreDecision.CREATE
                )
                return self._owned_work_item(decision, acquired, current)
            except ResourceModifiedError:
                continue
        raise TransientStoreError(
            "Incident state changed too often to acquire a lease")

    def _owned_work_item(self, decision, acquired, reserved):
        # A slow write/read can outlive the lease and observe a different owner.
        lease_until = _parse_datetime(acquired.get("leaseUntil"))
        if (
            acquired.get("leaseOwner") != reserved["leaseOwner"]
            or not lease_until
            or lease_until <= self.now()
        ):
            raise TransientStoreError("Incident lease expired or changed owner")
        return IncidentWorkItem(decision, acquired, _etag(acquired))

    def finalize(
            self, work_item, message_ts, lifecycle_status: LifecycleStatus):
        entity = dict(work_item.entity)
        entity.update({
            "messageTs": message_ts,
            "lifecycleStatus": lifecycle_status.value,
            "lastFingerprint": entity["pendingFingerprint"],
            "lastSubmissionTime": entity["pendingSubmissionTime"],
            "processingState": "complete",
            "leaseUntil": None,
            "lastErrorCode": "",
            "updatedAt": self.now(),
        })
        try:
            self._replace(entity, work_item.etag)
        except ResourceModifiedError as exc:
            raise StoreConsistencyError(
                "Incident state changed before it could be finalized") from exc

    def mark_failed(self, work_item, error_code):
        entity = dict(work_item.entity)
        entity.update({
            "processingState": "failed",
            "leaseUntil": None,
            "lastErrorCode": str(error_code)[:128],
            "updatedAt": self.now(),
        })
        try:
            self._replace(entity, work_item.etag)
        except ResourceModifiedError as exc:
            raise StoreConsistencyError(
                "Incident state changed before failure could be recorded") from exc

    def _get(self, event):
        try:
            return self.table_client.get_entity(
                partition_key=event.partition_key,
                row_key=event.row_key,
            )
        except (ServiceRequestError, ServiceResponseError) as exc:
            raise TransientStoreError(
                "Unable to read incident state") from exc
        except HttpResponseError as exc:
            raise TransientStoreError(
                "Unable to read incident state") from exc

    def _replace(self, entity, etag):
        if not etag:
            raise StoreConsistencyError(
                "Azure Table entity did not include an ETag")
        try:
            return self.table_client.update_entity(
                entity=entity,
                mode=UpdateMode.REPLACE,
                etag=etag,
                match_condition=MatchConditions.IfNotModified,
            )
        except ResourceModifiedError:
            raise
        except (ServiceRequestError, ServiceResponseError) as exc:
            raise TransientStoreError(
                "Unable to update incident state") from exc
        except HttpResponseError as exc:
            if exc.status_code in {408, 429, 500, 502, 503, 504}:
                raise TransientStoreError(
                    "Unable to update incident state") from exc
            raise StoreConsistencyError(
                "Azure Table rejected the incident state update") from exc
