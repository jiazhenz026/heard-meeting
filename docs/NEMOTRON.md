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

See [`eval/results/latest.md`](../eval/results/latest.md) for the full table
and per-case failures. The headline numbers from the run in this PR are
summarised there and in the PR description.

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
