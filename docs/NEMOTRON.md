# Nemotron in Heard!

Heard! is an ambient meeting agent. It listens to a working meeting, gives
every idea a card, sends sub-agents to research them, and speaks only when
asked or when a finding changes what the room is about to decide. Two of its
five components run on **Nemotron through NVIDIA NIM**, and they are the two
that sit on the clock.

## Where Nemotron runs

| Component | What it does | Cadence | Model |
|---|---|---|---|
| **Classifier** | Sorts the last 2–5 s of speech: was a new subject pitched (mint the card, take the speaker's name verbatim or invent one), was Heard addressed, is anything worth investigating, how salient is this moment | on every speech commit, debounced 2 s, forced at 5 s | `nvidia/nemotron-3-super-120b-a12b`, reasoning off |
| **Notes agent** | Rewrites the live meeting notes (`notes.md`): what is being discussed, claims and who made them, open questions, decisions. The notes are the shared context every other component reads | every ~20 s while there is new speech | same model, reasoning off |

The rest of the loop (the resident front-desk agent and the web-searching
researchers) runs on the Claude Agent SDK. That split is deliberate:

- **The classifier is on the five-second path.** A card has to be on the board
  within five seconds of an idea being spoken, or the board stops feeling like
  it is in the room. Waking the front desk costs a tool loop (3–8 s); the
  classifier has to answer in about a second and it fires 80–140 times per ten
  minutes. A small, fast, JSON-only model is the right tool, and Nemotron
  Super with reasoning off answers in about 1.3 s median.
- **The classifier is allowed to be wrong.** A bad pass costs one stray card
  or one unnecessary wake, never an utterance: it cannot speak. So it can be
  tuned for speed and let the slower agents adjudicate.
- **The notes agent runs constantly for the whole meeting.** Cost per call
  matters more than peak reasoning; the output is a rewrite of a short
  document, not a decision.

Both are one `chat.completions` call each through the OpenAI-compatible NIM
endpoint (`heard/nim.py`). Reasoning is switched off with
`chat_template_kwargs.enable_thinking=false`; the JSON is pulled out of the
response with a brace scanner that tolerates fences, preambles and stray
`<think>` blocks, because the exact output shape varies between Nemotron
sizes and the model id is configuration (`NEMOTRON_MODEL_ID`).

### What the classifier returns

```json
{
  "new_subject": {"title": "YesChef", "named_by": "speaker", "one_liner": "...", "anchor": "u0016"},
  "questions":   [{"text": "has anyone shipped a voice KDS", "subject": "YesChef", "worth_investigating": true}],
  "addressed":   false,
  "intent":      null,
  "salience":    1
}
```

`new_subject` mints the card directly, on the fast path, before the front desk
is involved. `addressed` opens a direct address the front desk must answer.
`questions` become research briefs. `salience` gates whether the front desk is
woken at all.

## Evaluation and benchmarking

`eval/classifier_bench.py` scores the classifier the way it is used: the same
system prompt, parser and normaliser as the live code (`heard.classifier`,
`heard.nim`), against a labelled set of meeting moments in `eval/cases.py`.

**The set** (26 cases) is built from the demo meeting and from failure modes
seen live: named and unnamed pitches, reactions to an existing card that must
*not* mint a second one, respelt names ("Yes Chef"), direct addresses including
the "herd" mishear, "heard" as a verb, talk about Heard itself, small talk and
logistics, questions worth a web search versus rhetorical ones, and a pitch and
a reaction in one span.

**What is measured**, per model and per reasoning mode:

| Metric | Why it matters |
|---|---|
| latency p50 / p95, share under 5 s | the card budget |
| valid JSON rate | a parse failure is a lost pass |
| subject accuracy, spurious-card rate, missed-card rate | a board with forty cards has lost the room; a missed pitch has no card at all |
| verbatim-name rate | the card must carry the speaker's name, not a respelling |
| addressed precision / recall | a false positive means Heard talks when nobody asked, the failure the whole design exists to prevent |
| investigate accuracy | the research briefs come from here |
| salience exact / within one | gates the front-desk wake |

**Run it**

```bash
.venv/bin/python eval/classifier_bench.py                    # four Nemotron models × thinking off/on
.venv/bin/python eval/classifier_bench.py --models nvidia/nemotron-3-super-120b-a12b --thinking off --repeats 3
```

Results land in `eval/results/<stamp>.md` and `.json`; `eval/results/latest.md`
is the last run. Calls are serial by default to stay under the per-key NIM rate
limit; 429/503 are retried with backoff and counted as errors if they persist.

## Results

Run of 2026-09-20 (`eval/results/2026-09-20_0608.md`): 26 cases, one repeat
per configuration, serial calls against NIM during a noisy evening (55 of 208
calls needed at least one retry for a 503 or a timeout; those retries are not
counted in latency).

| Model | Reasoning | Valid JSON | p50 | p95 | ≤ 5 s | Subject | Spurious card | Verbatim name | Addressed P / R | Investigate |
|---|---|---|---|---|---|---|---|---|---|---|
| **Super 120B** (live) | **off** | **100%** | **1.5 s** | **4.1 s** | **96%** | 100% | 0% | 100% | 71% / 100% | 92% |
| Super 120B | on | 85% | 8.7 s | 17.7 s | 23% | 100% | 0% | 100% | 100% / 75% | 95% |
| Lightning 30B | off | 88% | 2.2 s | 13.0 s | 78% | 87% | 17% | 100% | 100% / 100% | 87% |
| Lightning 30B | on | 0% | – | – | 0% | – | – | – | – | – |
| Ultra 550B | off | 92% | 4.4 s | 16.5 s | 67% | 100% | 0% | 100% | 80% / 100% | 92% |
| Ultra 550B | on | 85% | 10.8 s | 19.1 s | 18% | 100% | 0% | 100% | 100% / 100% | 100% |
| Nano 30B | off / on | 404 from the endpoint for chat completions; not scored | | | | | | | | |

**What it says**

- **Reasoning off is the only way to make the budget.** With reasoning on,
  every model spends most calls past 5 s and 15% or more of them never reach
  a JSON object before the token cap (Lightning: none of 26). The classifier
  runs with `enable_thinking=false`.
- **Nemotron Super with reasoning off is the live configuration and the best
  one for this job.** 26 of 26 valid, p95 under the budget, no spurious cards,
  every named pitch carried verbatim. Its one weakness is addressed
  precision: two false positives, both sentences *about* Heard rather than
  *to* it ("Heard is listening right now", "can you make the sticky notes
  smaller"). The harness treats `addressed` as precision-critical, so the
  live system also runs a regex fast path on the transcript line and the
  front desk can decline to answer; a wrong `addressed` costs a wake, not an
  utterance.
- **Lightning 30B is fast but mints cards it should not.** 17% spurious-card
  rate: it turned "pivoting from YesChef" and a restatement of an existing
  card into new subjects. A board that grows a card per reaction loses the
  room, so the smaller model is not a drop-in.
- **Ultra 550B buys nothing here.** Same subject accuracy as Super, slower
  (p50 4.4 s), and the shared endpoint rate-limits it hardest.
- **Salience** is within one step of the label on every valid call for every
  model; exact agreement is 85–91%. It only gates a wake, so within-one is
  the number that matters.

**How to read the two Super misses on `investigate`.** "Nobody is doing that"
and "who would it be useful for" are questions a search can answer and the
label says so; Super marked them not worth investigating. The front desk
still dispatches research on every new card unprompted, so these are missed
*extra* briefs, not missed cards.

## Configuration

```
NVIDIA_API_KEY=            # one key per person: NIM rate limits are per key
NVIDIA_BASE_URL=https://integrate.api.nvidia.com/v1
NEMOTRON_MODEL_ID=nvidia/nemotron-3-super-120b-a12b
HEARD_DISABLE_THINKING=true
HEARD_CLASSIFY_FLOOR_S=2   # debounce
HEARD_CLASSIFY_CEILING_S=5 # forced pass
HEARD_NOTES_EVERY_S=20
HEARD_NIM_TIMEOUT_S=8
HEARD_MAX_RPM=30           # pacing ceiling below the provider's 40
```
