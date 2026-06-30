"""
Audit log — the permanent, structured record of every decision.

Per spec (notes.md step 7 "Recording the decision"): for each decision we store
the result, the confidence, *which signals were used and what each one said*, and
a timestamp. Served for inspection via GET /log, and the same place appeals get
attached in M5. Backed by a JSON file for the MVP (SQLite is the documented
alternative) — structured, not print() statements.
"""

import json
import os
from threading import Lock

_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audit_log.json")

# Guards the read-modify-write cycle so concurrent requests can't clobber the
# file. Flask's dev server is threaded by default.
_lock = Lock()


def _load():
    if not os.path.exists(_LOG_PATH):
        return []
    try:
        with open(_LOG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        # Corrupt/empty file shouldn't take down the endpoint; start fresh.
        return []


def _save(entries):
    with open(_LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2)


def record_decision(*, decision_id, creator_id, timestamp, result, confidence, signals, status):
    """Append one decision record to the log and return it.

    `signals` is the full per-signal detail dict (M3: just llm; M4 adds
    stylometry) so the appeal queue can show both signals' verdicts/scores.
    """
    record = {
        "decision_id": decision_id,
        "creator_id": creator_id,
        "timestamp": timestamp,
        "result": result,
        "confidence": confidence,
        "signals": signals,
        "status": status,
        "appeals": [],  # M5 attaches appeals here, beside the decision they contest
    }
    with _lock:
        entries = _load()
        entries.append(record)
        _save(entries)
    return record


def get_entries(limit=None):
    """Return audit entries most-recent-first, optionally capped at `limit`."""
    with _lock:
        entries = _load()
    entries.reverse()
    if limit is not None:
        entries = entries[:limit]
    return entries
