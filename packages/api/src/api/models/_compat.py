"""Cross-dialect column defaults and UTC-aware datetime type (SQL Server → SQLite compatible for tests)."""
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime
from sqlalchemy.types import TypeDecorator


def new_uuid() -> str:
    return str(uuid.uuid4())


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """Naive DATETIME storage of UTC wall-clock; re-attaches UTC on read.

    SQL Server DATETIME and SQLite have no timezone concept, so tzinfo is lost
    on round-trip. Without it, ``.isoformat()`` serializes offset-less and the
    frontend parses it as local time (off by the browser's UTC offset).
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is not None:
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is not None:
            return value.replace(tzinfo=timezone.utc)
        return value
