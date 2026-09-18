"""Stable keyset pagination with opaque cursors bound to the query scope."""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
from datetime import datetime, timezone

from .errors import DomainConflict


def page_records(items, *, scope, limit=20, cursor=None):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise DomainConflict("INVALID_LIMIT", "limit 必须为 1—100")
    fingerprint = hashlib.sha256(json.dumps(scope, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    after = None
    if cursor is not None:
        try:
            if not isinstance(cursor, str) or not cursor or len(cursor) > 2048:
                raise ValueError
            data = json.loads(base64.b64decode(cursor.encode("ascii"), altchars=b"-_", validate=True))
            if (not isinstance(data, list) or len(data) != 4 or data[0] != 1
                    or data[1] != fingerprint or not isinstance(data[2], str)
                    or not isinstance(data[3], str) or not data[3]):
                raise ValueError
            datetime.fromisoformat(data[2])
            after = (data[2], data[3])
        except (ValueError, TypeError, UnicodeError, binascii.Error):
            raise DomainConflict("INVALID_CURSOR", "分页游标无效或不属于当前查询") from None

    def key(row):
        value = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat(), row["id"]

    rows = sorted(items, key=key)
    if after is not None:
        rows = [row for row in rows if key(row) > after]
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        next_cursor = base64.urlsafe_b64encode(json.dumps([1, fingerprint, *key(page[-1])], separators=(",", ":")).encode()).decode()
    return {"items": page, "next_cursor": next_cursor}
