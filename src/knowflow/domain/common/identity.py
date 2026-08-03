from __future__ import annotations

import dataclasses
import hashlib
import json
import uuid
from datetime import UTC, date, datetime
from enum import Enum
from typing import Any, Final

from pydantic import BaseModel

OPERATION_NAMESPACE: Final = uuid.UUID("3d86f311-2197-5d43-9340-5d964577fbfc")


def new_id() -> uuid.UUID:
    return uuid.uuid4()


def _json_default(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_none=False)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("naive datetimes cannot participate in a durable identity")
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    raise TypeError(f"Unsupported canonical JSON value: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        default=_json_default,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def payload_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def operation_id(workflow_id: str | uuid.UUID, task_key: str, payload: Any) -> uuid.UUID:
    if not task_key.strip():
        raise ValueError("task_key must not be blank")
    name = f"{workflow_id}:{task_key}:{payload_hash(payload)}"
    return uuid.uuid5(OPERATION_NAMESPACE, name)


def message_id(event_id: str | uuid.UUID, consumer: str) -> uuid.UUID:
    if not consumer.strip():
        raise ValueError("consumer must not be blank")
    return uuid.uuid5(OPERATION_NAMESPACE, f"message:{event_id}:{consumer}")
