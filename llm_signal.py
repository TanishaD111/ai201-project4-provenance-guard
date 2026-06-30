"""
Signal 1 — Groq LLM classifier (semantic / stylistic coherence).

Per spec (planning.md "Detection Signals — Signal 1"): this signal measures
the *semantic* read — does the writing read as human or AI holistically. The
model is prompted to return JSON and we extract `ai_probability`, a score in
[0, 1] (probability the text is AI). This is deliberately NOT a bare binary —
the score is what feeds the weighted-average fusion in M4.
"""

import json
import os

from groq import Groq

MODEL = "llama-3.3-70b-versatile"

# The model is asked to be a holistic reader, not a rules engine, and to emit a
# strict JSON object. response_format=json_object (set on the call) guarantees
# parseable JSON; the prompt pins the schema.
_SYSTEM_PROMPT = (
    "You are an expert at distinguishing human-written prose from AI-generated "
    "text. Judge the text holistically: voice, originality, idea flow, and "
    '"feel" — not just surface grammar. Respond with ONLY a JSON object of the '
    "form:\n"
    '{"verdict": "ai" | "human", '
    '"ai_probability": <float between 0.0 and 1.0>, '
    '"reasoning": "<one or two sentences>"}\n'
    "ai_probability is the probability the text is AI-generated: 1.0 = certainly "
    "AI, 0.0 = certainly human, 0.5 = genuinely unsure. Calibrate it — do not "
    "default to extreme values when you are not confident."
)


class GroqUnavailableError(Exception):
    """Raised when the Groq call fails or returns something unparseable.

    The /submit route catches this to return a 503 (and, in M4+, to fall back
    to stylometry-only with lowered confidence) per the API contract.
    """


# Lazily constructed so importing this module never requires the key to be set
# (useful for tests that only exercise validation).
_client = None


def _get_client():
    global _client
    if _client is None:
        api_key = os.environ.get("GROQ_API_KEY")
        if not api_key:
            raise GroqUnavailableError("GROQ_API_KEY is not set")
        _client = Groq(api_key=api_key)
    return _client


def classify_with_llm(text):
    """Run the Groq semantic signal on `text`.

    Returns a dict on the same axis the spec defines for every signal:
        {
            "verdict": "ai" | "human",     # derived from ai_probability
            "ai_probability": float,        # SCORE in [0, 1] — the real output
            "reasoning": str,
        }

    Raises GroqUnavailableError if the API call fails or the response can't be
    parsed into a valid ai_probability.
    """
    try:
        resp = _get_client().chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            response_format={"type": "json_object"},
            temperature=0,  # determinism matters for an auditable decision
        )
        raw = resp.choices[0].message.content
        data = json.loads(raw)
    except Exception as exc:  # network error, auth error, bad JSON, etc.
        raise GroqUnavailableError(str(exc)) from exc

    # Validate + clamp the score; trust it as little as possible.
    try:
        p_ai = float(data["ai_probability"])
    except (KeyError, TypeError, ValueError) as exc:
        raise GroqUnavailableError(
            f"LLM response missing/invalid ai_probability: {raw!r}"
        ) from exc
    p_ai = max(0.0, min(1.0, p_ai))

    # Derive the verdict from the score so the two are always consistent, rather
    # than trusting a separately-returned label that could contradict the number.
    verdict = "ai" if p_ai >= 0.5 else "human"

    return {
        "verdict": verdict,
        "ai_probability": p_ai,
        "reasoning": str(data.get("reasoning", "")).strip(),
    }


if __name__ == "__main__":
    # Spec's M3 verification step: call the signal directly on clear samples
    # BEFORE wiring it into the endpoint. Run: python llm_signal.py
    from dotenv import load_dotenv

    load_dotenv()

    samples = {
        "clearly human (bursty, idiosyncratic)": (
            "Look, I tried. I really did. Spent the whole rainy Tuesday hunched "
            "over that stupid carburetor, knuckles bleeding, swearing at a "
            "machine that couldn't care less. My dad would've fixed it in ten "
            "minutes and never said a word."
        ),
        "clearly AI (smooth, hedged, generic)": (
            "In today's fast-paced world, effective time management is essential "
            "for achieving success. By prioritizing tasks and setting clear "
            "goals, individuals can enhance their productivity. Moreover, "
            "maintaining a healthy work-life balance contributes significantly "
            "to overall well-being and long-term satisfaction."
        ),
    }
    for label, sample in samples.items():
        print(f"\n=== {label} ===")
        print(json.dumps(classify_with_llm(sample), indent=2))
