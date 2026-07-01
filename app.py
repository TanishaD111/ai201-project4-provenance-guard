"""
Provenance Guard — Flask app.

Implements the public surface from the API contract (notes.md "API Surface")
and the full submission flow from the Architecture diagram: rate limit ->
validate -> Signal 1 (LLM) + Signal 2 (stylometry) -> fusion -> transparency
label -> audit log -> response. Also serves /appeal (contest a decision) and
/log (inspect the audit trail).
"""

import uuid
from datetime import datetime, timezone

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

import audit_log
import labels
import scoring
import stylometry
from llm_signal import GroqUnavailableError, classify_with_llm

load_dotenv()

app = Flask(__name__)

# Rate limiting (Architecture: Flask-Limiter is the first gate). Per-route
# limits are applied on the decorators below rather than globally, so read-only
# endpoints (/health, /log) stay unthrottled. storage_uri="memory://" is the
# in-memory backend for local dev (Flask-Limiter >= 3.x requires an explicit
# storage_uri). Chosen limits + reasoning are documented in the README.
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    storage_uri="memory://",
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
@limiter.limit("10 per minute;100 per day")  # burst cap + daily cap (see README)
def submit():
    """Classify a piece of content (API contract: POST /submit).

    Validates input, runs both signals (Groq LLM + stylometry), fuses them into
    one confidence score, generates the transparency label, writes the full
    decision to the audit log, and returns the structured response.
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

    # ---- Signal 2: Stylometry (structural, pure Python) ----
    stylo = stylometry.analyze(text)

    # ---- Fusion -> combined_p_ai -> result + confidence + category ----
    scored = scoring.score(llm["ai_probability"], stylo["ai_probability"])
    result = scored["result"]
    confidence = scored["confidence"]
    combined_p_ai = scored["combined_p_ai"]
    category = scored["label_category"]

    # decision_id: the unique key for this submission. It ties together the
    # response, the stored audit record, and any future appeal.
    decision_id = str(uuid.uuid4())

    # Transparency label (M5): exact reader-facing text for this category, with
    # the confidence percentage filled in. Changes with the score.
    label = labels.generate(category, confidence)

    status = "classified"  # both signals ran and fused successfully
    timestamp = _now_iso()
    signals = {
        "llm": {
            "verdict": llm["verdict"],
            "ai_probability": llm["ai_probability"],
            "reasoning": llm["reasoning"],
        },
        "stylometry": {
            "verdict": stylo["verdict"],
            "ai_probability": stylo["ai_probability"],
            "metrics": stylo["metrics"],
        },
    }

    # Record the decision before responding (Architecture step 7). Stores both
    # signals' individual scores alongside the combined confidence score.
    audit_log.record_decision(
        decision_id=decision_id,
        creator_id=creator_id,
        timestamp=timestamp,
        result=result,
        confidence=confidence,
        combined_p_ai=combined_p_ai,
        signals=signals,
        status=status,
    )

    return jsonify({
        "decision_id": decision_id,
        "result": result,
        "confidence": confidence,
        "combined_p_ai": combined_p_ai,
        "label": label,
        "status": status,
        "creator_id": creator_id,
        "signals": signals,
        "timestamp": timestamp,
    }), 200


@app.post("/appeal")
@limiter.limit("10 per minute")
def appeal():
    """Contest a decision (API contract: POST /appeal; planning.md Flow 2).

    Accepts `content_id` (the decision_id from /submit) and `creator_reasoning`.
    Validates the id exists (else 404), appends the appeal to the audit log
    beside the original decision, flips its status to `under_review`, and
    returns a confirmation. No automated re-classification — this flags the
    decision for a human reviewer.
    """
    body = request.get_json(silent=True) or {}
    # Primary field names come from the M5 contract (content_id / creator_reasoning);
    # decision_id / reasoning are accepted as aliases for consistency with the
    # rest of the system, which keys everything on decision_id.
    content_id = body.get("content_id") or body.get("decision_id")
    reasoning = body.get("creator_reasoning") or body.get("reasoning")

    if not isinstance(content_id, str) or not content_id.strip():
        return jsonify({"error": "Field 'content_id' is required."}), 400
    if not isinstance(reasoning, str) or not reasoning.strip():
        return jsonify({"error": "Field 'creator_reasoning' is required."}), 400
    content_id = content_id.strip()
    reasoning = reasoning.strip()

    appeal_id = str(uuid.uuid4())
    timestamp = _now_iso()
    updated, appeal_record = audit_log.add_appeal(
        decision_id=content_id,
        appeal_id=appeal_id,
        reasoning=reasoning,
        timestamp=timestamp,
    )
    if updated is None:
        return jsonify({"error": f"No decision found for content_id '{content_id}'."}), 404

    return jsonify({
        "appeal_id": appeal_id,
        "decision_id": content_id,   # contract field name
        "content_id": content_id,    # alias (M5 instruction field name)
        "status": updated["status"],  # "under_review"
        "logged_at": timestamp,
        "message": "Appeal received. This decision has been flagged for human review.",
        "appeal": appeal_record,
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
