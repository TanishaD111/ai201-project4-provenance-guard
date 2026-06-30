"""
Provenance Guard — Flask app skeleton (Milestone 3).

This wires the public surface from the API contract (notes.md "API Surface")
and the submission flow from the Architecture diagram: rate limit -> validate
-> Signal 1. Fusion, Signal 2 (stylometry), the label generator, and the audit
log are stubbed with TODO markers and land in M4/M5.
"""

import uuid
from datetime import datetime, timezone

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

import audit_log
from llm_signal import GroqUnavailableError, classify_with_llm

load_dotenv()

app = Flask(__name__)

# Rate limiting (Architecture: Flask-Limiter is the first gate). The /submit
# limit is intentionally tight to protect the free-tier Groq quota — every
# submission costs one LLM call. Tune + document final values in the README.
limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=["100 per hour"],
)

# Input-validation bounds. MIN guards the spec's "very short submissions"
# edge case (stylometry + LLM are unreliable on a couple of sentences); MAX
# caps cost/latency. Both are tunable.
MIN_CHARS = 100
MAX_CHARS = 10_000


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


@app.get("/health")
def health():
    """Liveness check (API contract: GET /health)."""
    return jsonify({"status": "ok"}), 200


@app.post("/submit")
@limiter.limit("10 per minute")  # per-endpoint cap on top of the default
def submit():
    """Classify a piece of content (API contract: POST /submit).

    M3 STUB: validates input and runs Signal 1 (Groq LLM) only. The fused
    `result`/`confidence`, the second signal, the transparency label, and the
    audit-log write are TODO for M4/M5 — so this returns the live LLM signal
    plus an explicit "partial" status rather than faking a final verdict.
    """
    body = request.get_json(silent=True) or {}
    text = body.get("text")
    creator_id = body.get("creator_id")  # optional, for attribution

    # ---- Input validation (400 cases from the contract) ----
    if not isinstance(text, str) or not text.strip():
        return jsonify({"error": "Field 'text' is required and must be a non-empty string."}), 400
    text = text.strip()
    if len(text) < MIN_CHARS:
        return jsonify({
            "error": f"Text too short to classify reliably (min {MIN_CHARS} characters)."
        }), 400
    if len(text) > MAX_CHARS:
        return jsonify({
            "error": f"Text too long (max {MAX_CHARS} characters)."
        }), 400

    # ---- Signal 1: Groq LLM (semantic) ----
    try:
        llm = classify_with_llm(text)
    except GroqUnavailableError as exc:
        # API contract: 503 when Groq is unavailable. M4 may fall back to
        # stylometry-only here with lowered confidence.
        return jsonify({"error": "Detection service unavailable.", "detail": str(exc)}), 503

    # ---- M4 TODO: Signal 2 (stylometry) ----
    # ---- M4 TODO: fusion -> combined_p_ai -> result + confidence ----
    # ---- M5 TODO: label generator -> {variant, text} ----
    # ---- M5 TODO: write the full decision record (incl. decision_id) to the
    #              audit log so /appeal and /log can look it up by this key ----

    # decision_id: the unique key for this submission. The instruction calls
    # this "content_id"; we use the contract's name (notes.md API Surface) so
    # /appeal and /log line up. It must appear here AND, once the audit log
    # exists (M5), in the stored record.
    decision_id = str(uuid.uuid4())

    # result currently comes from Signal 1 alone. M4 replaces this with the
    # fused verdict over both signals.
    result = llm["verdict"]

    # PLACEHOLDER confidence — provisional single-signal value so the field type
    # is stable. M4 replaces it with the real fused confidence = max(p, 1-p)
    # over combined_p_ai (planning.md "Uncertainty Representation").
    p_ai = llm["ai_probability"]
    confidence = round(max(p_ai, 1 - p_ai), 4)

    # PLACEHOLDER label — the real three-variant generator lands in M5.
    label = {
        "variant": "placeholder",
        "text": "Transparency label not yet generated (M5).",
    }

    status = "partial"  # becomes "classified" once fusion lands in M4
    timestamp = _now_iso()
    signals = {
        "llm": {
            "verdict": llm["verdict"],
            "ai_probability": llm["ai_probability"],
            "reasoning": llm["reasoning"],
        },
    }

    # Record the decision before responding (Architecture step 7). The same
    # decision_id keys the response, this record, and any future appeal.
    audit_log.record_decision(
        decision_id=decision_id,
        creator_id=creator_id,
        timestamp=timestamp,
        result=result,
        confidence=confidence,
        signals=signals,
        status=status,
    )

    return jsonify({
        "decision_id": decision_id,
        "result": result,
        "confidence": confidence,
        "label": label,
        "status": status,
        "creator_id": creator_id,
        "signals": signals,
        "timestamp": timestamp,
    }), 200


@app.get("/log")
def get_log():
    """Inspect the audit trail (API contract: GET /log).

    Returns an array of decision records, most recent first. Optional ?limit=N
    caps the count. No auth in this MVP — it exists for documentation and
    grading visibility.
    """
    limit = request.args.get("limit", type=int)
    return jsonify(audit_log.get_entries(limit=limit)), 200


if __name__ == "__main__":
    app.run(debug=True, port=5000)
