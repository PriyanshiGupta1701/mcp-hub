"""
log_parsing.py
---------------
Shared log-line parsing utilities used by both the Azure and Kubernetes
checks: timestamp extraction and traceback-block detection.
"""

import re
from datetime import datetime


# Matches leading ISO8601 timestamps like '2026-07-10T10:31:18.8333172Z' or
# '2026-07-10T08:05:14.5912469+00:00' that Azure prefixes onto log lines.
LOG_TIMESTAMP_PATTERN = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?)(Z|[+-]\d{2}:\d{2})?")


def parse_log_line_timestamp(line):
    match = LOG_TIMESTAMP_PATTERN.match(line)
    if not match:
        return None
    ts_str = match.group(1)
    tz_str = match.group(2) or "+00:00"
    if tz_str == "Z":
        tz_str = "+00:00"
    try:
        # Truncate sub-second precision to microseconds (fromisoformat's limit)
        if "." in ts_str:
            head, frac = ts_str.split(".")
            ts_str = f"{head}.{frac[:6]}"
        return datetime.fromisoformat(ts_str + tz_str)
    except ValueError:
        return None


TRACEBACK_START_PATTERN = re.compile(r"traceback \(most recent call last\)", re.IGNORECASE)
# The line that actually ends a Python traceback: 'SomeError: message' or
# 'module.SomeException: message', with no leading whitespace.
TRACEBACK_END_PATTERN = re.compile(r"^[A-Za-z_][\w.]*(Error|Exception|Warning)\b")
MAX_TRACEBACK_BLOCK_LINES = 200  # safety cap only, in case no end line is found
