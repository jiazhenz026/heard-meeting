# Classifier benchmark · 2026-09-20_0444

26 labelled cases × 1 repeat(s) per configuration. Same prompt, parser and normaliser as the live classifier. Budget: a card must be on the board inside 5 s of the commit; the model call has to fit in that with the 2 s debounce.

| Model | Thinking | Calls | Errors | Valid JSON | p50 | p95 | ≤5 s | Subject | Spurious card | Verbatim name | Addressed P / R | Investigate | Salience exact / ±1 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| nemotron-3-super-120b-a12b | off | 26 | 4 | 85% | 1.6s | 8.4s | 82% | 100% | 0% | 100% | 83% / 100% | 86% | 82% / 100% |

## Per-case failures

**nemotron-3-super-120b-a12b · thinking off** — 8 of 26 calls off
- `about-heard`: addressed → True
- `about-the-meeting`: invalid (RateLimitError:429)
- `ask-hey-heard`: investigate → False
- `claim-nobody-does-this`: investigate → False
- `pitch-then-react`: invalid (InternalServerError:503)
- `question-price`: invalid (InternalServerError:503)
- `question-rhetorical`: invalid (RateLimitError:429)
- `react-who-for`: investigate → False
