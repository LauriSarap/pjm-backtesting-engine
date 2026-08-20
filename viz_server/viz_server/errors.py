"""Error envelope + named codes for the viz_server API.

Whole-request failures return:
    {"error": {"code": "...", "message": "...", "remediation": "..."}}

Per-feed failures (within an otherwise-successful request) ride along inside the
Arrow stream metadata as `partial=true` plus an `errors` list — see
`arrow_stream.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# --- Codes -----------------------------------------------------------------

UNSUPPORTED_FEED = "unsupported_feed"
MISSING_ZONE_DATA = "missing_zone_data"
PRE_DATA_DECISION_TIME = "pre_data_decision_time"
QUERY_TIMEOUT = "query_timeout"
LOADER_FAILURE = "loader_failure"
INVALID_PARAMS = "invalid_params"


# --- Per-feed error (rides inside Arrow stream) ----------------------------


@dataclass(frozen=True)
class FeedError:
    feed: str
    code: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"feed": self.feed, "code": self.code, "message": self.message}


# --- Whole-request error ---------------------------------------------------


def envelope(code: str, message: str, remediation: str = "") -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "remediation": remediation}}
