"""
Confidence scoring / signal fusion (Milestone 4).

Combines the two detection signals into ONE confidence score exactly as
specified in planning.md ("Combining them into one confidence score" +
"Uncertainty Representation"). Both signals emit `ai_probability ∈ [0, 1]` on
the same axis, so fusion is a weighted average:

    combined_p_ai = 0.6 * llm_p_ai + 0.4 * stylometry_p_ai
    confidence    = max(combined_p_ai, 1 - combined_p_ai)      # always [0.5, 1.0]

`result` mirrors the label category, so the top-level attribution is tri-valued
(ai | human | uncertain) to match the API contract:

    result = "ai"        if combined_p_ai >= 0.70
             "human"     if combined_p_ai <= 0.30
             "uncertain" otherwise

Threshold -> label category (planning.md "Thresholds" table):

    combined_p_ai >= 0.70   -> likely_ai      (High-confidence AI)
    combined_p_ai <= 0.30   -> likely_human    (High-confidence human)
    0.30 < combined < 0.70  -> uncertain        (Uncertain)

The LLM carries the heavier weight (stronger general detector); stylometry
anchors it with an independent structural check. Disagreement is handled for
free: opposing signals pull the average toward 0.5, which drops confidence into
the Uncertain band — the mechanism that guards against confident false positives.
"""

# --- constants, kept explicit so they can be checked against planning.md ---
LLM_WEIGHT = 0.6
STYLOMETRY_WEIGHT = 0.4
AI_THRESHOLD = 0.70       # combined_p_ai at/above this -> likely AI
HUMAN_THRESHOLD = 0.30    # combined_p_ai at/below this -> likely human


def fuse(llm_p_ai, stylometry_p_ai):
    """Weighted-average fusion of the two signals' AI-probabilities."""
    return LLM_WEIGHT * llm_p_ai + STYLOMETRY_WEIGHT * stylometry_p_ai


def categorize(combined_p_ai):
    """Map a combined probability to one of the three planning.md categories."""
    if combined_p_ai >= AI_THRESHOLD:
        return "likely_ai"
    if combined_p_ai <= HUMAN_THRESHOLD:
        return "likely_human"
    return "uncertain"


def score(llm_p_ai, stylometry_p_ai):
    """Fuse both signals into the final scored decision.

    Returns:
        {
            "combined_p_ai": float,               # fused probability, in [0, 1]
            "result": "ai" | "human" | "uncertain",  # tri-valued attribution
            "confidence": float,                  # certainty in the pick, [0.5, 1.0]
            "label_category": "likely_ai" | "likely_human" | "uncertain",
        }
    """
    combined = fuse(llm_p_ai, stylometry_p_ai)
    category = categorize(combined)
    # result mirrors the category so the top-level attribution is tri-valued,
    # matching the API contract (notes.md "POST /submit").
    result = {"likely_ai": "ai", "likely_human": "human", "uncertain": "uncertain"}[category]
    confidence = max(combined, 1.0 - combined)
    return {
        "combined_p_ai": round(combined, 4),
        "result": result,
        "confidence": round(confidence, 4),
        "label_category": category,
    }


def _verify_thresholds():
    """Assert the scoring logic matches the thresholds defined in planning.md.

    This is the M4 instruction's explicit check: "Verify that the generated
    scoring function actually matches the thresholds you defined in your
    planning document." Boundary cases are pinned so a silent drift (e.g. `>`
    vs `>=`, or a 0.6/0.4 swap) fails loudly.
    """
    # Weights sum to 1 and fuse correctly.
    assert abs(fuse(1.0, 0.0) - 0.6) < 1e-9, "LLM weight must be 0.6"
    assert abs(fuse(0.0, 1.0) - 0.4) < 1e-9, "stylometry weight must be 0.4"
    assert abs(fuse(1.0, 1.0) - 1.0) < 1e-9, "weights must sum to 1.0"

    # Category boundaries (inclusive at 0.70 / 0.30 per the planning.md table).
    assert categorize(0.70) == "likely_ai"
    assert categorize(0.6999) == "uncertain"
    assert categorize(0.30) == "likely_human"
    assert categorize(0.3001) == "uncertain"
    assert categorize(0.50) == "uncertain"

    # A weak lean (0.51) and a strong one (0.95) must yield DIFFERENT labels.
    assert score(0.51, 0.51)["label_category"] == "uncertain"
    assert score(0.95, 0.95)["label_category"] == "likely_ai"

    # result is tri-valued and mirrors the category.
    assert score(0.51, 0.51)["result"] == "uncertain"
    assert score(0.95, 0.95)["result"] == "ai"
    assert score(0.05, 0.05)["result"] == "human"

    # confidence is always the winning side's probability, in [0.5, 1.0].
    s = score(0.2, 0.2)
    assert s["result"] == "human" and abs(s["confidence"] - 0.8) < 1e-9

    print("threshold self-check: PASS (scoring matches planning.md)")


if __name__ == "__main__":
    import json

    _verify_thresholds()

    # --- Calibration harness: run BOTH signals + fusion on the M4 samples and
    # print each signal's score SEPARATELY (per the instruction) so a
    # misbehaving signal is easy to spot.
    import stylometry

    try:
        from dotenv import load_dotenv

        load_dotenv()
        from llm_signal import GroqUnavailableError, classify_with_llm

        llm_available = True
    except Exception:  # pragma: no cover - import/env issues
        llm_available = False

    print("\n=== Combined scoring on M4 calibration set ===")
    for label, sample in stylometry._SAMPLES.items():
        stylo = stylometry.analyze(sample)
        stylo_p = stylo["ai_probability"]

        llm_p = None
        if llm_available:
            try:
                llm_p = classify_with_llm(sample)["ai_probability"]
            except GroqUnavailableError as exc:
                print(f"\n=== {label} ===")
                print(f"  LLM signal unavailable ({exc}); stylometry_p_ai = {stylo_p}")
                continue

        print(f"\n=== {label} ===")
        if llm_p is None:
            print(f"  stylometry_p_ai = {stylo_p}  (LLM not run — set GROQ_API_KEY)")
            continue

        result = score(llm_p, stylo_p)
        print(f"  llm_p_ai        = {round(llm_p, 4)}")
        print(f"  stylometry_p_ai = {stylo_p}")
        print(f"  combined_p_ai   = {result['combined_p_ai']}")
        print(f"  -> {result['result']}  (confidence {result['confidence']}) "
              f"[{result['label_category']}]")
        agree = ("agree" if (llm_p >= 0.5) == (stylo_p >= 0.5) else "DISAGREE")
        print(f"  signals {agree}")
