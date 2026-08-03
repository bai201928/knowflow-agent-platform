from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from knowflow.infrastructure.db.session import Base, UTCDateTime, utc_now


def test_database_metadata_uses_deterministic_constraint_names() -> None:
    convention = Base.metadata.naming_convention
    assert convention is not None
    assert convention["pk"].startswith("pk_")
    assert convention["fk"].startswith("fk_")
    assert convention["uq"].startswith("uq_")


def test_utc_datetime_rejects_naive_values_and_normalizes_offsets() -> None:
    value_type = UTCDateTime()
    with pytest.raises(ValueError, match="timezone-aware"):
        value_type.process_bind_param(datetime(2026, 8, 3, 12, 0), None)

    local = datetime(2026, 8, 3, 20, 0, tzinfo=timezone(timedelta(hours=8)))
    stored = value_type.process_bind_param(local, None)
    assert stored == datetime(2026, 8, 3, 12, 0)

    restored = value_type.process_result_value(stored, None)
    assert restored == datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


def test_utc_now_is_timezone_aware() -> None:
    assert utc_now().tzinfo is UTC
