"""Apache Arrow IPC streaming encoder for /api/series responses.

Schema:
    t            timestamp[ns, UTC]   bucket start (target time)
    feed         string               feed id
    value        float64 (nullable)   null = no data
    source_zone  string               denormalized for client

Schema metadata (string -> string):
    resolution     applied resolution (e.g. "5min")
    decision_time  echoed UTC ISO8601
    target_from    echoed UTC ISO8601
    target_to      echoed UTC ISO8601
    partial        "true" if any per-feed errors were collected
    errors_json    JSON-encoded list of {feed, code, message}; "" if none
"""

from __future__ import annotations

import io
import json
from datetime import datetime

import pyarrow as pa
import pyarrow.ipc as ipc

from .series import SeriesResult

_SCHEMA = pa.schema(
    [
        pa.field("t", pa.timestamp("ns", tz="UTC")),
        pa.field("feed", pa.string()),
        pa.field("value", pa.float64()),
        pa.field("source_zone", pa.string()),
    ]
)


def encode_series(
    result: SeriesResult,
    decision_time: datetime,
    target_from: datetime,
    target_to: datetime,
) -> bytes:
    df = result.frame

    if len(df) == 0:
        table = _SCHEMA.empty_table()
    else:
        # pa.Table.from_pandas would honor df dtypes but we want to enforce schema
        # exactly, so build columns explicitly.
        t_arr = pa.array(df["t"].to_numpy(), type=pa.timestamp("ns", tz="UTC"))
        feed_arr = pa.array(df["feed"].astype("string").to_numpy(), type=pa.string())
        value_arr = pa.array(df["value"].to_numpy(), type=pa.float64(), from_pandas=True)
        zone_arr = pa.array(df["source_zone"].astype("string").to_numpy(), type=pa.string())
        table = pa.Table.from_arrays([t_arr, feed_arr, value_arr, zone_arr], schema=_SCHEMA)

    metadata = {
        "resolution": result.resolution,
        "decision_time": decision_time.isoformat(),
        "target_from": target_from.isoformat(),
        "target_to": target_to.isoformat(),
        "partial": "true" if result.errors else "false",
        "errors_json": json.dumps([e.as_dict() for e in result.errors]) if result.errors else "",
    }
    table = table.replace_schema_metadata(
        {k.encode("utf-8"): v.encode("utf-8") for k, v in metadata.items()}
    )

    buf = io.BytesIO()
    with ipc.new_stream(buf, table.schema) as writer:
        writer.write_table(table)
    return buf.getvalue()
