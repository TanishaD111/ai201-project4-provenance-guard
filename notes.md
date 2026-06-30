# Project Required Features

- **Content Submission Endpoint**: Build an API endpoint that accepts a piece of text-based content (a poem, a short story excerpt, a blog post) for attribution analysis. The endpoint must return a structured response including the attribution result, confidence score, and the transparency label text that would be shown to the user.

- **Multi-Signal Detection Pipeline**: Your detection pipeline must use at least 2 distinct signals to classify content. Single-signal detection is not acceptable. Your planning.md and README must explain what each signal captures and why you chose them.

- **Confidence Scoring with Uncertainty**: Your system must return a confidence score, not just a binary label. The score should reflect genuine uncertainty — a 0.51 confidence should produce a meaningfully different transparency label than a 0.95. Your README must explain how you approached this and how you tested whether your scores are meaningful.

- **Transparency Label**: Design and implement the label that would be displayed to a reader on the platform. It must communicate the attribution result in plain language and make the confidence level meaningful to a non-technical reader. Include a typed description of all three label variants (high-confidence AI, high-confidence human, uncertain) in your README — write out the exact text each one displays. You're welcome to include a screenshot or mockup as well, but the written description is what's required.

- **Appeals Workflow**: Implement a mechanism for creators to contest a classification. At minimum, an appeal must: capture the creator's reasoning, log the appeal alongside the original decision, and update the content's status to "under review." Automated re-classification is not required.

- **Rate Limiting**: Implement rate limiting on your submission endpoint. Your README must document the limits you chose and your reasoning for those specific values.

- **Audit Log**: Every attribution decision — including confidence score, signals used, and any appeals — must be captured in a structured audit log. Document the log in your README (or via the GET /log output) with at least 3 entries visible.

---

# Architecture Narrative — The Path of a Single Submission

This is the journey one piece of text takes, from the moment it's submitted to the label a reader finally sees. Each step names the component it touches and what that component does.

## 1. Arrival — `POST /submit` (Flask API endpoint)
A creator (or the host platform acting on their behalf) sends a piece of text to the **Flask** submission endpoint. Flask is the front door: it receives the HTTP request, pulls the text out of the JSON body, and is responsible for handing back a single structured response at the end. Nothing else talks to the outside world — Flask is the only public surface.

## 2. The bouncer — `Flask-Limiter` (rate limiting)
Before the text gets to do any real work, **Flask-Limiter** checks how many requests this client has made recently. If they're under the limit, the request passes through. If they've exceeded it, the request is rejected immediately with a `429 Too Many Requests` and never reaches the detection pipeline. This protects the (free-tier) Groq quota and stops anyone from hammering the endpoint. The text only continues if it clears this gate.

## 3. Input validation (Flask)
Flask does a quick sanity check: is there actually text? Is it long enough to analyze meaningfully (very short snippets can't be classified reliably)? Is it within a maximum length? If the input is malformed or unusable, Flask returns an error here and the text goes no further. Valid text moves into the detection pipeline.

## 4. The detection pipeline — two distinct signals run in parallel
The text is now handed to the core of the system, which runs **two independent detectors** on the same input:
- **Signal A — Groq LLM (`llama-3.3-70b-versatile`):** a *semantic* read — does the writing read as human or AI, holistically (voice, coherence, "feel")?
- **Signal B — Stylometric heuristics (pure Python):** a *structural* read — measurable statistics of form (sentence-length variance, vocabulary diversity, punctuation density, complexity).

One is semantic, one is structural — independent by design, so combining them is more informative than either alone. Full treatment of what each measures, why, and their blind spots is in the **Detection Signals** section below.

## 5. Fusion & confidence scoring
The two signals' outputs flow into a **scoring/fusion step**. This component:
- Reconciles the two verdicts (do they agree on human vs. AI?).
- Produces a **single confidence score** (not a binary). When both signals strongly agree, confidence is high (e.g. ~0.95). When they disagree or both are weak, confidence is low (e.g. ~0.51), which is the system's honest way of saying "uncertain."
- Decides the final attribution result based on the fused score.
The output of this step is: a result (AI / human / uncertain) and a confidence number that genuinely reflects how sure the system is.

## 6. Transparency label generation
The result + confidence are passed to the **label generator**, which translates the math into plain language a non-technical reader understands. It picks one of three variants:
- **High-confidence AI** — clearly flags likely AI authorship.
- **High-confidence human** — clearly indicates likely human authorship.
- **Uncertain** — honestly communicates that the system isn't sure.
This label text is what will ultimately be shown to a reader on the platform.

## 7. Recording the decision — Audit log (SQLite / structured JSON)
Before responding, the system writes the full decision to the **audit log**. This is the permanent, structured record: the submitted content (or a reference to it), the final result, the confidence score, *which signals were used and what each one said*, and a timestamp. This log is what makes every decision reviewable later — and it's the same place appeals get attached. It's exposed for inspection via `GET /log`.

## 8. The response (Flask)
Flask assembles everything into one structured JSON response and returns it to the caller: the attribution result, the confidence score, and the transparency label text. This is the end of the submission path — what the platform receives and renders to the user.

---

## The appeals path (a separate, later journey)
If a creator believes their content was misclassified, they hit a separate endpoint (e.g. `POST /appeal`) with a reference to the original decision and their reasoning. This component:
- **Captures the creator's reasoning** (free text).
- **Logs the appeal alongside the original decision** in the audit log, so the contest lives next to what it contests.
- **Updates the content's status to "under review."**
Automated re-classification isn't required — the appeal simply flags the decision and preserves a paper trail for a human reviewer.

---

## Component summary
| Component | Role in the path |
|---|---|
| **Flask** | Public API: receives submissions, validates input, returns the final structured response. |
| **Flask-Limiter** | Gatekeeper: enforces rate limits before any detection runs. |
| **Groq (`llama-3.3-70b-versatile`)** | Signal A — semantic classification (holistic human-vs-AI read). |
| **Stylometric heuristics (pure Python)** | Signal B — structural statistics (variance, diversity, punctuation, complexity). |
| **Fusion / scoring step** | Combines both signals into one confidence score and final result. |
| **Label generator** | Turns score + result into one of three plain-language transparency labels. |
| **Audit log (SQLite / JSON)** | Permanent structured record of every decision and appeal; served via `GET /log`. |
| **Appeals endpoint** | Captures creator reasoning, logs it against the original decision, sets status to "under review." |

---

# Detection Signals — Decided Before Code

Two signals, each measuring a genuinely different property. For each: what it measures, why that property differs between human and AI writing, and — most importantly — what it **cannot** capture.

## Signal A — Groq LLM classifier (`llama-3.3-70b-versatile`)

**What property it measures:** Semantic and stylistic coherence. We send the text to the model and ask it to judge, holistically, whether the writing reads as human- or AI-generated, and how strongly. It's effectively a very well-read reader giving an impression of voice, originality, idea flow, and "feel."

**Why that property differs between human and AI:** AI writing tends to be smooth, evenly hedged, and generic — it reaches for the most probable next phrase, so it produces safe transitions, balanced both-sides framing, and a polished-but-flavorless register. Human writing more often carries idiosyncratic voice, surprising word choices, uneven emphasis, real lived specifics, and the occasional rough edge. The LLM can perceive these holistic qualities the way a person skimming the text would.

**Blind spot — what it cannot capture:**
- **It's not deterministic or fully explainable.** The same text can get slightly different verdicts across calls, and it can't always tell us *why* — so we can't audit its reasoning the way we can a number.
- **It can be fooled by surface fluency.** A heavily AI-edited human draft, or AI text deliberately roughened up, can read "human" to it. It judges the polished result, not the process.
- **Training-distribution bias.** It's better at spotting writing that resembles the AI patterns it has seen; novel generators, non-English-influenced English, or unusual human styles (a quirky poet, a non-native writer) can read as "AI" simply for being atypical — a false-positive risk.
- **It doesn't measure structure numerically.** It has no reliable internal ruler for sentence-length variance or vocabulary diversity; it gives an impression, not a statistic. That's exactly the gap Signal B fills.

## Signal B — Stylometric heuristics (pure Python)

**What property it measures:** Measurable *structural* statistics of the text's form — sentence-length variance, type-token ratio (vocabulary diversity), punctuation density, and average sentence complexity. No meaning, just shape and rhythm, computed deterministically.

**Why that property differs between human and AI:** AI text is statistically *uniform* — sentences cluster around similar lengths, punctuation is regular, vocabulary is broad but evenly distributed. Human writing is *bursty and variable*: a three-word sentence next to a forty-word one, repeated favorite words, irregular punctuation, abrupt shifts in complexity. These differences show up in the numbers even when the prose reads smoothly.

**Blind spot — what it cannot capture:**
- **It's blind to meaning entirely.** Grammatically uniform nonsense and a polished human essay can produce similar stats. It can't tell whether the text is *coherent*, only how its surface is shaped.
- **It's fragile on short or unusual text.** Variance and type-token ratio are unstable on a few sentences, and naturally uniform forms (a haiku, a technical spec, a list) look "AI" by structure alone — another false-positive source.
- **It's gameable with simple edits.** Inserting a few short/long sentences or varying punctuation can shift the stats without changing authorship.
- **Thresholds are heuristic, not learned.** The cutoffs that separate "human-like" from "AI-like" variance are hand-tuned, so they generalize imperfectly across genres (poetry vs. blog vs. fiction).

**Why pairing them works:** A's blind spot (no numeric structure, can be fooled by fluency) is B's strength (pure structure, immune to fluency). B's blind spot (no meaning, fragile, gameable) is A's strength (holistic semantic read). When they *agree*, we're genuinely confident; when they *disagree*, that disagreement is the most useful signal we have — it's the system honestly hitting "uncertain."

---

# The False-Positive Problem — Tracing a Misclassified Human

**Scenario:** A human poet submits a tight, formally structured poem. Its lines are deliberately uniform in length and its vocabulary is spare and repetitive (a real stylistic choice). This is exactly the profile that fools structural analysis.

**Step 1 — Submission.** The poem arrives at `POST /submit`, clears rate limiting and validation, and enters the pipeline.

**Step 2 — Signal A (Groq).** The LLM reads it and, sensing genuine voice and intent, leans **human** with moderate strength — say ~0.7 toward human.

**Step 3 — Signal B (stylometry).** The uniform line lengths and low type-token ratio look machine-like to the structural rules, so B leans **AI** with moderate strength — say ~0.65 toward AI. **This is the false positive forming**: a structural artifact of good poetry being read as machine uniformity.

**Step 4 — Fusion & confidence.** The two signals **disagree**, and neither is overwhelming. The fusion step does *not* paper over this — disagreement drives the combined confidence *down*, landing near **0.50–0.55**. The system refuses to commit to a wrong, confident "AI" verdict. This is the design working as intended: the false positive gets caught not by being right, but by the system being *honestly unsure*.

**Step 5 — Label.** Because confidence is low, the label generator selects the **Uncertain** variant — not "high-confidence AI." The reader sees something like "Our analysis was inconclusive for this piece," never a damaging confident accusation. **The cost of the false positive is contained by the confidence score, not eliminated by perfect detection.**

**Step 6 — Audit log.** The decision is recorded with both signals' verdicts visible, so the disagreement is preserved and reviewable.

**Step 7 — Appeal.** If the poet still objects (even an "uncertain" label can feel unfair), they hit `POST /appeal` with the decision ID and their reasoning ("This is an original formal poem; the uniform meter is intentional"). The system captures their reasoning, logs it next to the original decision, and flips status to **"under review"** for a human.

**What this teaches (used in Milestone 2):** The defense against false positives is **not** a better binary classifier — it's making sure disagreement *lowers* confidence and that low confidence *changes the label*. The scoring math must be built so a 0.51 and a 0.95 are meaningfully different outcomes, and the uncertain label must never sound like an accusation. This directly shapes the fusion logic and label thresholds.

---

# API Surface — The Contract

Defining the contract before any implementation. All endpoints accept/return JSON.

### `POST /submit`
Classify a piece of content.
- **Accepts:**
  ```json
  { "text": "the content to analyze", "creator_id": "optional, for attribution" }
  ```
- **Returns (`200`):**
  ```json
  {
    "decision_id": "uuid",
    "result": "ai | human | uncertain",
    "confidence": 0.87,
    "label": {
      "variant": "high_confidence_ai | high_confidence_human | uncertain",
      "text": "Plain-language transparency label shown to readers."
    },
    "signals": {
      "llm": { "verdict": "ai|human", "confidence": 0.9 },
      "stylometry": { "verdict": "ai|human", "confidence": 0.7, "metrics": { "...": 0 } }
    },
    "status": "classified",
    "timestamp": "ISO-8601"
  }
  ```
- **Errors:** `400` (missing/too-short/too-long text), `429` (rate limit exceeded), `503` (Groq unavailable — may fall back to stylometry-only with lowered confidence).

### `POST /appeal`
Contest a prior classification.
- **Accepts:**
  ```json
  { "decision_id": "uuid of the decision being appealed", "reasoning": "creator's explanation" }
  ```
- **Returns (`200`):**
  ```json
  {
    "appeal_id": "uuid",
    "decision_id": "uuid",
    "status": "under_review",
    "logged_at": "ISO-8601"
  }
  ```
- **Errors:** `400` (missing reasoning), `404` (unknown decision_id).

### `GET /log`
Inspect the audit trail.
- **Accepts:** optional query params `?limit=N`.
- **Returns (`200`):** an array of audit entries — each a full decision record (result, confidence, both signals, status) plus any attached appeals.

### `GET /health`
Liveness check.
- **Returns (`200`):** `{ "status": "ok" }`.

---

# Flow Diagrams

## Flow 1 — Submission (`POST /submit`)

```
  Client
    │  raw text (JSON)
    ▼
┌─────────────┐   request allowed?   ┌──────────────────┐
│ Flask-Limiter├────── 429 if over ──▶│  reject response │
└──────┬──────┘                       └──────────────────┘
       │ raw text (under limit)
       ▼
┌─────────────┐   invalid?   ┌──────────────────┐
│  Validation ├──── 400 ────▶│  error response   │
└──────┬──────┘              └──────────────────┘
       │ clean text
       ├───────────────────────────────┐
       │ raw text                       │ raw text
       ▼                                ▼
┌──────────────────┐           ┌────────────────────────┐
│ Signal 1: Groq   │           │ Signal 2: Stylometry   │
│ LLM (semantic)   │           │ (structural, pure Py)  │
└────────┬─────────┘           └───────────┬────────────┘
         │ verdict + signal score          │ verdict + signal score + metrics
         └──────────────┬──────────────────┘
                        ▼
              ┌───────────────────┐
              │ Confidence Scoring│
              │   / Fusion        │
              └─────────┬─────────┘
                        │ combined score + result
                        ▼
              ┌───────────────────┐
              │  Label Generator  │
              └─────────┬─────────┘
                        │ result + score + label text
                        ▼
              ┌───────────────────┐
              │   Audit Log       │  ◀── writes decision record (id, result,
              │ (SQLite / JSON)   │       score, both signals, timestamp)
              └─────────┬─────────┘
                        │ decision_id
                        ▼
              ┌───────────────────┐
              │   Flask Response  │
              └─────────┬─────────┘
                        │ JSON: result + confidence + label
                        ▼
                     Client
```

## Flow 2 — Appeal (`POST /appeal`)

```
  Creator
    │  decision_id + reasoning (JSON)
    ▼
┌───────────────────┐   unknown id?   ┌──────────────────┐
│  Flask /appeal    ├───── 404 ──────▶│  error response   │
└─────────┬─────────┘                 └──────────────────┘
          │ valid decision_id + reasoning
          ▼
┌───────────────────┐
│  Status Update    │  sets content status → "under review"
└─────────┬─────────┘
          │ appeal record (appeal_id, decision_id, reasoning, status)
          ▼
┌───────────────────┐
│   Audit Log       │  ◀── appends appeal NEXT TO the original decision
│ (SQLite / JSON)   │
└─────────┬─────────┘
          │ appeal_id + new status
          ▼
┌───────────────────┐
│  Flask Response   │
└─────────┬─────────┘
          │ JSON: appeal_id + status "under_review"
          ▼
       Creator
```

**Arrow legend:** *raw text* = the original submitted content · *signal score* = one detector's verdict + its confidence · *combined score* = fused confidence after reconciling both signals · *label text* = the plain-language transparency string shown to readers · *decision_id* = the key tying a response, its audit record, and any future appeal together.