# Provenance Guard

A content-provenance detection service. Given a piece of text, it estimates whether the text was **AI-generated** or **human-written**, returns a calibrated confidence score and a plain-language transparency label, records every decision in a structured audit log, and lets a creator appeal a decision.

Detection fuses **two independent signals** — one semantic, one structural — so the combined judgement is more informative than either alone.

## Running locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
echo "GROQ_API_KEY=your-key-here" > .env    # required for Signal 1
python3 app.py                               # serves on http://localhost:5000
```

Quick end-to-end check (server must be running):
```bash
bash test_project.sh
```

## API

| Method + path | Purpose |
|---|---|
| `GET /health` | Liveness check. |
| `POST /submit` | Classify text. Body: `{"text": "...", "creator_id": "..."}` (creator_id optional). Returns `decision_id`, `result`, `confidence`, `combined_p_ai`, `label`, both signals. |
| `POST /appeal` | Contest a decision. Body: `{"content_id": "<decision_id>", "creator_reasoning": "..."}`. Flips the decision to `under_review` and logs the appeal. |
| `GET /log` | Full audit trail, most-recent-first. Optional `?limit=N`. |

Input validation: `text` must be 100–10,000 characters (rationale under *Known limitations*).

---

## Architecture overview — the path of a submission

A single `POST /submit` flows through the pipeline in order; each step can stop the request early (rate limit / validation) or hand off to the next:

```
Client → Flask-Limiter → Validation → Signal 1 (Groq LLM) ─┐
                                     → Signal 2 (Stylometry) ┘
                                            ↓
                                     Fusion / scoring
                                            ↓
                                     Transparency label
                                            ↓
                                       Audit log  →  JSON response → Client
```

1. **Arrival — `POST /submit` (Flask).** The public front door; pulls `text` (and optional `creator_id`) from the JSON body.
2. **Rate limiter (Flask-Limiter).** Checks the client's request count. Over the limit → `429` immediately, before any work runs.
3. **Input validation.** Rejects missing / too-short / too-long text with `400`. Only clean text continues.
4. **Two signals run.** Signal 1 (Groq LLM) gives a semantic read; Signal 2 (stylometry) gives a structural read. Each returns `ai_probability ∈ [0,1]`.
5. **Fusion / scoring.** The two probabilities are combined by weighted average into `combined_p_ai`, which yields `result` (ai / human / uncertain) and a `confidence`.
6. **Transparency label.** `result` + `confidence` map to one of three plain-language label variants — the text a reader would actually see.
7. **Audit log.** The full decision (id, result, confidence, both signals' scores, timestamp) is written to a structured JSON log *before* responding, so every decision is reviewable and appeals can attach to it later.
8. **Response (Flask).** One structured JSON payload — `decision_id`, `result`, `confidence`, `label`, and both signals — is returned to the client.

An **appeal** is a separate, later path: `POST /appeal` looks up the decision by `decision_id`, appends the creator's reasoning to the audit log beside the original decision, and flips its status to `under_review` (no re-classification).

---

## Detection signals — and why these two

Single-signal detection is brittle: any one detector has a blind spot that the target content can fall into. I chose two signals that fail in *different* ways, so where one is weak the other is usually strong.

### Signal 1 — Groq LLM classifier (`llama-3.3-70b-versatile`)

**What it measures:** semantic / stylistic coherence — does the writing *read* as human or AI when judged holistically (voice, originality, idea flow, "feel")? The model returns `{verdict, ai_probability, reasoning}` and I use `ai_probability ∈ [0,1]`.

**Why this signal:** the strongest general-purpose detector available to me is a capable LLM acting as an expert reader. It catches the things rules can't — a generic, hedged, evenly-polished register that "sounds like AI" even when the grammar is flawless.

**Blind spot:** it's non-deterministic and not fully explainable, and it can be fooled by surface fluency (a lightly human-edited AI draft reads "human" to it). It also carries training-distribution bias — atypical human writing (a non-native speaker, an unusual stylist) can read as "AI" simply for being unusual.

### Signal 2 — Stylometry (pure Python, deterministic)

**What it measures:** structural statistics of *form* — sentence-length variance, type-token ratio (vocabulary diversity), punctuation density, and average sentence complexity. Each metric is normalized to a 0–1 AI-likeness sub-score against hand-tuned human/AI reference bands, then averaged into an `ai_probability` on the same axis as Signal 1.

**Why this signal:** it's deterministic, auditable, and immune to the fluency that fools the LLM. The core intuition is that AI text is statistically *uniform* (sentences cluster in length, punctuation is regular) while human text is *bursty and variable*. It gives me an objective, independent number to anchor the LLM's impression.

**Blind spot:** it's blind to meaning (uniform nonsense and a polished essay score alike), fragile on short text, and gameable with a few manual edits.

**Why the pairing works:** Signal 1's weakness (fooled by fluency, no numeric structure) is Signal 2's strength (pure structure); Signal 2's weakness (no meaning, gameable) is Signal 1's strength. When they agree I'm genuinely confident; when they disagree, that disagreement is itself the most useful thing I know — it's the system honestly landing on "uncertain."

**What I'd change deploying for real:** replace the hand-tuned stylometry bands with values fit on a labelled corpus per genre (poetry vs. blog vs. legal read very differently), add a third signal that's harder to game (e.g. a perplexity measure from a base model), and run the two signals concurrently rather than sequentially to cut latency.

---

## Confidence scoring — and why this approach

Both signals emit `ai_probability ∈ [0,1]` on the same axis, so I fuse by **weighted average**:

```
combined_p_ai = 0.6 * llm_p_ai + 0.4 * stylometry_p_ai
```

The LLM gets the heavier weight (0.6) because it's the stronger general detector; stylometry (0.4) anchors it with an objective, independent check.

**Why a weighted average rather than, say, a hard AND/OR of two verdicts:** the average handles disagreement *for free*. When one signal says AI and the other says human, the mean is pulled toward 0.5 — which automatically drops confidence into the "uncertain" band. That is the exact mechanism that protects against confident false positives: I never want a split decision to surface as a confident accusation.

Confidence is the probability of whichever class actually wins, so it always lives in [0.5, 1.0]:
```
result      = "ai"        if combined_p_ai >= 0.70
              "human"     if combined_p_ai <= 0.30
              "uncertain" otherwise
confidence  = max(combined_p_ai, 1 - combined_p_ai)
```

A confident 0.95 and a weak 0.55 therefore produce genuinely different *outcomes*, not just different numbers: the former earns a high-confidence label, the latter is forced into "uncertain." That is what "meaningful uncertainty" means here — the score changes the label, not just the display.

### Worked examples (actual Milestone 4 test scores)

**High-confidence case — a casual, irregular human note** ("ok so i finally tried that new ramen place downtown…"):

| | value |
|---|---|
| Signal 1 (LLM) `ai_probability` | 0.20 |
| Signal 2 (stylometry) `ai_probability` | 0.426 |
| **`combined_p_ai`** | **0.2904** |
| result / confidence | `human` / **71%** |
| label | **high_confidence_human** |

Both signals lean human and agree, so the average sits well below 0.30 and the system makes a confident call.

**Lower-confidence case — a lightly-edited AI paragraph** ("I've been thinking a lot about remote work lately…"):

| | value |
|---|---|
| Signal 1 (LLM) `ai_probability` | 0.20 |
| Signal 2 (stylometry) `ai_probability` | 0.5815 |
| **`combined_p_ai`** | **0.3526** |
| result / confidence | `uncertain` / **65%** |
| label | **uncertain** |

Here the signals **disagree** — the LLM reads it as human (0.20) but stylometry reads it as AI (0.58). Fusion pulls the score into the middle band, so instead of picking a side the system honestly returns *uncertain*. Same inputs, a 6-point confidence drop, and a **different label** — the scoring varies meaningfully rather than emitting a constant.

**How I tested this:** `python3 scoring.py` runs a threshold self-check (asserting the 0.70/0.30 cutoffs and 0.6/0.4 weights match this spec) and then prints both signals separately for four calibration inputs, so a misbehaving signal is easy to isolate.

**What I'd change deploying for real:** the weights (0.6/0.4) are a reasoned guess, not learned — I'd calibrate them (and the thresholds) against a labelled set and check that the confidence numbers are *calibrated* (of everything scored ~0.7, roughly 70% should actually be AI), not just ordered correctly.

---

## Transparency label

Every `/submit` response includes a plain-language `label` chosen from three variants. Which one is returned depends on the fused confidence, so the text **changes with the score** (`{pct}` is confidence as a whole-number percent):

| `combined_p_ai` | `result` | `label.variant` |
|---|---|---|
| ≥ 0.70 | `ai` | `high_confidence_ai` |
| ≤ 0.30 | `human` | `high_confidence_human` |
| otherwise | `uncertain` | `uncertain` |

**High-confidence AI (`high_confidence_ai`)**
> **Likely AI-generated.** Our analysis indicates this content was most likely produced by an AI system (confidence: {pct}%). Two independent checks — a language-model review and a writing-structure analysis — pointed the same way. If you're the creator and believe this is wrong, you can appeal this result.

**High-confidence human (`high_confidence_human`)**
> **Likely human-written.** Our analysis indicates this content was most likely written by a person (confidence: {pct}%). Two independent checks agreed. No action needed.

**Uncertain (`uncertain`)**
> **Inconclusive.** We couldn't determine with confidence whether this content was written by a person or generated by AI (confidence: {pct}%). Our two checks disagreed or found mixed signals, so we're not making a call — please treat this as a weak signal, not a verdict.

---

## Appeals workflow

`POST /appeal` accepts a `content_id` (the `decision_id` from `/submit`) and `creator_reasoning`. It validates the id exists (else `404`), appends the appeal to the audit log **beside the original decision**, flips that decision's status `classified → under_review`, and returns a confirmation. There is no automated re-classification — an appeal flags the decision for a human reviewer and preserves the paper trail.

---

## Rate limiting

Applied with [Flask-Limiter](https://flask-limiter.readthedocs.io/) keyed on client IP, in-memory storage for local dev (`storage_uri="memory://"`).

**Limit on `POST /submit`: `10 per minute; 100 per day`.**

Reasoning — the limits bracket *realistic single-creator usage* while stopping a script from flooding the service:

- **10 / minute (burst cap).** A person checking their own writing works in bursts — paste, read the result, tweak, resubmit. Even an impatient writer rarely exceeds a submission every few seconds, so 10/min leaves ample room for genuine use while cutting off automated hammering almost immediately.
- **100 / day (sustained cap).** A prolific individual might check a few dozen drafts a day; 100 covers that comfortably. Sustained traffic above it looks like scraping or batch abuse, not one writer. This cap also protects the free-tier **Groq quota** — every accepted submission costs one LLM call, so an unbounded endpoint is both an abuse vector and a cost risk.
- **Read-only endpoints (`/health`, `/log`) are unthrottled** — cheap, no LLM calls, and useful for monitoring/grading.

Over a limit, the endpoint returns **HTTP 429**.

### Rate-limit evidence

With the server running, in a second terminal:
```bash
for i in $(seq 1 12); do
  curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:5000/submit \
    -H "Content-Type: application/json" \
    -d '{"text": "This is a longer test submission written specifically to exercise the rate limiter. It is well over one hundred characters so it passes input validation and reaches the classifier.", "creator_id": "ratelimit-test"}'
done
```
Expected: the first 10 return `200`, requests 11–12 return `429`.
```
<PASTE YOUR ACTUAL STATUS-CODE OUTPUT HERE — ten 200s then two 429s>
```

---

## Audit log

Every decision is persisted as **structured JSON** in `audit_log.json` (not console output), served via `GET /log`. Each record captures everything needed to review or appeal without re-running anything:

| Field | Requirement it satisfies |
|---|---|
| `timestamp` | when the decision was made (ISO-8601, UTC) |
| `decision_id` | the content ID |
| `result` | attribution result (`ai` / `human` / `uncertain`) |
| `confidence` | calibrated confidence in the result |
| `combined_p_ai` | fused AI-probability |
| `signals.llm.ai_probability` | Signal 1's individual score |
| `signals.stylometry.ai_probability` | Signal 2's individual score |
| `status` + `appeals[]` | whether an appeal has been filed (`under_review` + the appeal record) |

Example record (post-appeal):
```json
{
  "decision_id": "2ff1d469-9176-4fea-9618-eb3dcdd297b2",
  "creator_id": "m5-test",
  "timestamp": "2026-06-30T05:33:34.953344+00:00",
  "result": "human",
  "confidence": 0.7096,
  "combined_p_ai": 0.2904,
  "signals": {
    "llm": { "verdict": "human", "ai_probability": 0.2, "reasoning": "..." },
    "stylometry": {
      "verdict": "human",
      "ai_probability": 0.426,
      "metrics": { "sentence_length_std": 6.723, "type_token_ratio": 0.873, "...": "..." }
    }
  },
  "status": "under_review",
  "appeals": [
    { "appeal_id": "…", "decision_id": "2ff1d469-…", "reasoning": "I wrote this myself…", "timestamp": "2026-06-30T…" }
  ]
}
```

Generate at least three entries by submitting three different texts, then `GET /log`; appealing one flips its `status` to `under_review`.

---

## Known limitations

**Highly polished, formulaic human writing — the dangerous false positive.** Genres like academic abstracts, legal boilerplate, and corporate press releases are naturally uniform, hedged, and formal. This hurts me on *both* signals at once, and for reasons tied directly to how each signal works:

- **Stylometry** is built on the assumption "uniform = AI." Its strongest metric is low sentence-length variance, and this genre is deliberately uniform with regular punctuation — so it reports high AI-likeness.
- **The LLM** associates a polished, evenly-hedged register with AI output, because that register is exactly what it was trained to recognize as generated text — so it *also* leans AI.

Because both signals agree on the wrong answer, fusion doesn't rescue me here — agreement *raises* confidence, so a genuine human legal brief can produce a confident `high_confidence_ai` label. This is worse than a disagreement case, where fusion would have pulled the score to "uncertain." It's a direct consequence of both signals keying on *surface uniformity* rather than provenance.

**Secondary limitation — very short text.** Below ~100 characters, sentence-length variance and type-token ratio are statistically meaningless (in my Milestone 4 testing, TTR saturated at 1.0 for every short sample, contributing no discrimination), and the LLM has little to judge. I mitigate this with a 100-character minimum at input validation rather than returning a number I don't trust — but it's a refusal, not a solution.

---

## Spec reflection

**Where the spec helped:** writing the fusion formula, thresholds, and exact label text in `planning.md` *before* coding gave the scoring function a precise target. It let me build a `_verify_thresholds()` self-check that literally asserts the planning numbers (0.70 / 0.30 cutoffs, 0.6 / 0.4 weights, and that 0.51 vs 0.95 yield different labels). Because the spec was concrete, "did the AI implement it correctly?" became a runnable check rather than a judgement call — and that caught boundary mistakes immediately.

**Where the implementation diverged:** my API contract described each signal's output field as `confidence`, but I implemented it as `ai_probability`. I diverged deliberately: a signal that leans human returns a *low* AI-probability (e.g. 0.2), and labelling that `confidence: 0.2` is actively misleading — it reads as "not confident" when the signal may be very confident it's human. The accurate quantity is the probability of the AI class, so I kept `ai_probability` in the code and updated the contract doc to match, rather than bend the code to a field name that misrepresents the value.

---

## AI usage

I used an AI coding assistant throughout, following the per-milestone plan in `planning.md`. Specific instances:

**1. Generating the second signal + fusion scoring (Milestone 4).** I fed it the *Detection Signals (Signal 2)* and *Uncertainty Representation* sections plus the architecture diagram, and asked it to produce the stylometry function and the weighted-average scoring logic. It produced working code, and I verified the scoring against my thresholds with a self-check. **What I overrode:** during calibration I noticed the type-token-ratio sub-score saturated at 1.0 for all four test inputs — a dead metric on short text. The tempting fix was to retune the bands, but I confirmed that tuning to rescue the AI sample flipped my human sample out of its category (overfitting to four points), so I *rejected* the recalibration and documented the short-text weakness as a known limitation instead.

**2. Generating the label function + `/appeal` endpoint (Milestone 5).** I asked it to generate the three-variant label generator and the appeal endpoint from the *Transparency Label Design* and *Appeals Workflow* sections. **What I revised:** (a) it initially proposed labels that always claimed "two independent checks agreed"; I noticed this can be false when a weighted average reaches an extreme despite the signals splitting, evaluated an agreement-aware rewrite, and ultimately decided to keep the spec's exact wording rather than expand scope. (b) I also caught that `result` never returned `"uncertain"` even though my contract listed it — so I directed a change to make `result` tri-valued (mirroring the label category) so the top-level attribution is honest about uncertainty.

**3. Building the acceptance test harness.** I asked it for an end-to-end test script. Its first version built JSON payloads with an inline `python -c` snippet whose `{...}` triggered shell brace-expansion, so no submissions were actually sent. I identified the failure from the output and directed a fix to single-quoted JSON literals; I also had it move the rate-limit phase to a short-payload (validation-only) loop so the limiter is tested without spending Groq calls.
