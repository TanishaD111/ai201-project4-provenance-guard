"""
Signal 2 — Stylometric heuristics (pure Python, deterministic).

Per spec (planning.md "Detection Signals — Signal 2"): measures *structural
statistics of form* — sentence-length variance, type-token ratio (vocabulary
diversity), punctuation density, and average sentence complexity. Each metric is
normalized to a 0–1 "AI-likeness" sub-score against hand-tuned human/AI
reference bands, then averaged into one `ai_probability ∈ [0, 1]` — the SAME
axis as Signal 1, so the two are directly comparable for fusion.

Core intuition (notes.md Signal B): AI text is statistically *uniform* (sentences
cluster in length, punctuation is regular); human text is *bursty and variable*.
"""

import re
import statistics


def _sentences(text):
    parts = re.split(r"[.!?]+", text)
    return [s.strip() for s in parts if s.strip()]


def _words(text):
    return re.findall(r"[a-zA-Z']+", text.lower())


def _band(value, human_end, ai_end):
    """Linear-normalize `value` to a 0–1 AI-likeness sub-score.

    `human_end` is the value that reads fully human (→ 0.0), `ai_end` the value
    that reads fully AI (→ 1.0). If ai_end < human_end the band is inverted
    (lower raw value = more AI-like). Clamped to [0, 1].
    """
    if ai_end == human_end:
        return 0.5
    frac = (value - human_end) / (ai_end - human_end)
    return max(0.0, min(1.0, frac))


def analyze(text):
    """Run the stylometry signal on `text`.

    Returns, on the shared signal axis:
        {
            "verdict": "ai" | "human",
            "ai_probability": float,   # averaged sub-scores, in [0, 1]
            "metrics": { <raw metric>: value, ... , <sub>_score: 0-1 },
        }
    """
    sentences = _sentences(text)
    words = _words(text)
    sent_lengths = [len(_words(s)) for s in sentences]

    # --- raw metrics ---
    # 1. Sentence-length variance (std of words per sentence). AI = low (uniform).
    sent_len_std = statistics.pstdev(sent_lengths) if len(sent_lengths) > 1 else 0.0
    # 2. Type-token ratio (unique/total words). AI = broad, even vocab → higher.
    ttr = len(set(words)) / len(words) if words else 0.0
    # 3. Punctuation density (punct chars / total chars). AI = regular/heavier.
    punct = re.findall(r"[,;:\-—()\"'.!?]", text)
    punct_density = len(punct) / len(text) if text else 0.0
    # 4. Avg sentence complexity (avg words per sentence). AI = longer/complex.
    avg_sent_len = statistics.mean(sent_lengths) if sent_lengths else 0.0

    # --- normalize each to a 0–1 AI-likeness sub-score (hand-tuned bands) ---
    std_score = _band(sent_len_std, human_end=11.0, ai_end=3.0)   # inverted: low std = AI
    ttr_score = _band(ttr, human_end=0.55, ai_end=0.85)           # higher TTR = AI
    punct_score = _band(punct_density, human_end=0.015, ai_end=0.055)
    complexity_score = _band(avg_sent_len, human_end=9.0, ai_end=26.0)

    sub_scores = [std_score, ttr_score, punct_score, complexity_score]
    p_ai = round(sum(sub_scores) / len(sub_scores), 4)
    verdict = "ai" if p_ai >= 0.5 else "human"

    return {
        "verdict": verdict,
        "ai_probability": p_ai,
        "metrics": {
            "sentence_length_std": round(sent_len_std, 3),
            "type_token_ratio": round(ttr, 3),
            "punctuation_density": round(punct_density, 4),
            "avg_sentence_length": round(avg_sent_len, 2),
            "std_score": round(std_score, 3),
            "ttr_score": round(ttr_score, 3),
            "punct_score": round(punct_score, 3),
            "complexity_score": round(complexity_score, 3),
        },
    }


# The M4 calibration set (from the instructions).
_SAMPLES = {
    "AI (formal, uniform)": (
        "Artificial intelligence represents a transformative paradigm shift in modern "
        "society. It is important to note that while the benefits of AI are numerous, it "
        "is equally essential to consider the ethical implications. Furthermore, "
        "stakeholders across various sectors must collaborate to ensure responsible "
        "deployment."
    ),
    "HUMAN (casual, irregular)": (
        "ok so i finally tried that new ramen place downtown and honestly? underwhelming. "
        "the broth was fine but they put WAY too much sodium in it and i was thirsty for "
        "like three hours after. my friend got the spicy version and said it was better. "
        "probably won't go back unless someone drags me there"
    ),
    "BORDERLINE (formal human)": (
        "The relationship between monetary policy and asset price inflation has been "
        "extensively studied in the literature. Central banks face a fundamental tension "
        "between their mandate for price stability and the unintended consequences of "
        "prolonged low interest rates on equity and real estate valuations."
    ),
    "BORDERLINE (lightly edited AI)": (
        "I've been thinking a lot about remote work lately. There are genuine tradeoffs — "
        "flexibility and no commute on one side, isolation and blurred work-life "
        "boundaries on the other. Studies show productivity varies widely by individual "
        "and role type."
    ),
}


if __name__ == "__main__":
    import json

    for label, sample in _SAMPLES.items():
        r = analyze(sample)
        print(f"\n=== {label} ===")
        print(f"  ai_probability = {r['ai_probability']}  ({r['verdict']})")
        print("  " + json.dumps(r["metrics"]))
