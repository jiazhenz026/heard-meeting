"""Benchmark the Heard! classifier across Nemotron models and thinking modes.

The classifier is the one model on the five-second path: every 2-5 s of
speech it has to say whether a new subject was pitched (and mint the card),
whether Heard was addressed, and whether anything is worth investigating.
It runs on Nemotron through NVIDIA NIM. This script asks, for each candidate
model and for reasoning on/off:

    latency     p50 / p95 wall clock per call, and the share under 5 s
    validity    did a JSON object come back at all
    subject     no spurious card, a card when there was a pitch, verbatim name
    addressed   precision and recall on direct addresses
    investigate did it flag the checkable questions
    salience    exact and within-one agreement

    python eval/classifier_bench.py                       # default models, thinking off and on
    python eval/classifier_bench.py --models nvidia/nemotron-3-super-120b-a12b --thinking off
    python eval/classifier_bench.py --repeats 3 --concurrency 2

Writes eval/results/<stamp>.json and .md. The same prompt, parser and
normaliser as the live classifier are used (heard.classifier, heard.nim), so
what is measured is what runs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from heard.classifier import SYSTEM, _normalise, _prompt  # noqa: E402
from heard.config import CONFIG  # noqa: E402
from heard.nim import json_blobs, strip_reasoning  # noqa: E402
from heard.store import Store, Utterance  # noqa: E402

from cases import CASES  # noqa: E402

DEFAULT_MODELS = [
    "nvidia/nemotron-3-super-120b-a12b",
    "nvidia/nemotron-nano-3-30b-a3b",
    "nvidia/nemotron-3.5-lightning-30b-a3b",
    "nvidia/nemotron-3-ultra-550b-a55b",
]
BUDGET_S = 5.0  # the card must be on the board inside this


def build_prompt(case: dict) -> str:
    store = Store(Path(tempfile.mkdtemp()))
    for title in case["existing"]:
        store.create_card(title)
    t = 1000.0
    older, new = [], []
    for i, (spk, text) in enumerate(case["older"]):
        older.append(Utterance(f"u{i + 1:04d}", t + i, spk, text))
    for j, (spk, text) in enumerate(case["new"]):
        new.append(Utterance(f"u{len(older) + j + 1:04d}", t + 100 + j, spk, text))
    return _prompt(older, new, store), new


async def call(client: Any, model: str, thinking: bool, user: str, timeout: float) -> tuple[dict | None, float, str | None]:
    request: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
        "temperature": 0,
        "max_tokens": 900 if thinking else 500,
    }
    if not thinking:
        request["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
    started = time.perf_counter()
    for attempt in range(5):
        try:
            resp = await asyncio.wait_for(client.chat.completions.create(**request), timeout=timeout)
            break
        except Exception as exc:  # noqa: BLE001
            code = getattr(exc, "status_code", None)
            if attempt < 4 and (code in (429, 503) or isinstance(exc, asyncio.TimeoutError)):
                # The provider's window, not the model: wait it out and re-time.
                await asyncio.sleep(4.0 * (attempt + 1))
                started = time.perf_counter()
                continue
            return None, time.perf_counter() - started, f"{type(exc).__name__}:{code or ''}"
    latency = time.perf_counter() - started
    text = ""
    if resp.choices:
        text = resp.choices[0].message.content or ""
        if isinstance(text, list):
            text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
    for b in json_blobs(strip_reasoning(text)):
        if isinstance(b, dict):
            return b, latency, None
    return None, latency, "no-json"


def score(case: dict, signal: dict | None) -> dict[str, Any]:
    e = case["expect"]
    out: dict[str, Any] = {}
    if signal is None:
        return {"valid": False}
    out["valid"] = True
    ns = signal.get("new_subject")
    exp = e["subject"]
    if exp is None:
        out["subject_ok"] = ns is None
        out["spurious_card"] = ns is not None
    elif exp == "*":
        out["subject_ok"] = ns is not None
        out["missed_card"] = ns is None
    else:
        out["subject_ok"] = ns is not None and exp.lower() in ns["title"].lower()
        out["missed_card"] = ns is None
        if ns is not None:
            out["verbatim"] = ns["title"].strip().lower() == exp.lower() and ns["named_by"] == "speaker"
    if e["named_by"] == "heard" and ns is not None:
        out["named_by_heard"] = ns["named_by"] == "heard"
    out["addressed_ok"] = bool(signal.get("addressed")) == e["addressed"]
    out["addressed_pred"] = bool(signal.get("addressed"))
    out["addressed_exp"] = e["addressed"]
    inv = any(q.get("worth_investigating") for q in signal.get("questions") or [])
    out["investigate_ok"] = inv == e["investigate"]
    out["investigate_pred"] = inv
    out["investigate_exp"] = e["investigate"]
    sal = int(signal.get("salience") or 0)
    out["salience_exact"] = sal == e["salience"]
    out["salience_within1"] = abs(sal - e["salience"]) <= 1
    return out


def summarise(rows: list[dict]) -> dict[str, Any]:
    n = len(rows)
    valid = [r for r in rows if r["score"].get("valid")]
    lat = sorted(r["latency"] for r in rows if r["error"] is None)
    def pct(vals: list[float], p: float) -> float:
        if not vals:
            return float("nan")
        k = max(0, min(len(vals) - 1, round(p * (len(vals) - 1))))
        return vals[k]
    def rate(key: str, pool: list[dict] | None = None) -> float | None:
        pool = valid if pool is None else pool
        vals = [r["score"][key] for r in pool if key in r["score"]]
        return (sum(vals) / len(vals)) if vals else None
    tp = sum(1 for r in valid if r["score"]["addressed_pred"] and r["score"]["addressed_exp"])
    fp = sum(1 for r in valid if r["score"]["addressed_pred"] and not r["score"]["addressed_exp"])
    fn = sum(1 for r in valid if not r["score"]["addressed_pred"] and r["score"]["addressed_exp"])
    return {
        "calls": n,
        "errors": sum(1 for r in rows if r["error"]),
        "valid_rate": len(valid) / n if n else 0,
        "latency_p50": pct(lat, 0.5),
        "latency_p95": pct(lat, 0.95),
        "latency_mean": statistics.fmean(lat) if lat else float("nan"),
        "under_budget": (sum(1 for x in lat if x <= BUDGET_S) / len(lat)) if lat else 0,
        "subject_acc": rate("subject_ok"),
        "spurious_card_rate": rate("spurious_card"),
        "missed_card_rate": rate("missed_card"),
        "verbatim_name_rate": rate("verbatim"),
        "named_by_heard_rate": rate("named_by_heard"),
        "addressed_precision": tp / (tp + fp) if (tp + fp) else None,
        "addressed_recall": tp / (tp + fn) if (tp + fn) else None,
        "investigate_acc": rate("investigate_ok"),
        "salience_exact": rate("salience_exact"),
        "salience_within1": rate("salience_within1"),
    }


async def run_config(client: Any, model: str, thinking: bool, repeats: int, concurrency: int, timeout: float) -> list[dict]:
    sem = asyncio.Semaphore(concurrency)
    rows: list[dict] = []

    async def one(case: dict, rep: int) -> None:
        user, new = build_prompt(case)
        async with sem:
            raw, latency, err = await call(client, model, thinking, user, timeout)
            await asyncio.sleep(0.4)  # stay under the per-key RPM
        signal = _normalise(raw, new) if raw else None
        rows.append({"case": case["id"], "rep": rep, "latency": latency, "error": err,
                     "signal": signal, "score": score(case, signal)})
        tag = "ok " if err is None else "ERR"
        print(f"  {tag} {model.split('/')[-1]:<34} think={'on ' if thinking else 'off'} {case['id']:<24} {latency:5.1f}s"
              + (f"  {err}" if err else ""), flush=True)

    await asyncio.gather(*(one(c, r) for r in range(repeats) for c in CASES))
    rows.sort(key=lambda r: (r["case"], r["rep"]))
    return rows


def fmt(v: float | None, pct: bool = True) -> str:
    if v is None or (isinstance(v, float) and v != v):
        return "–"
    return f"{v * 100:.0f}%" if pct else f"{v:.1f}s"


def render(results: list[dict], stamp: str, repeats: int) -> str:
    lines = [f"# Classifier benchmark · {stamp}", "",
             f"{len(CASES)} labelled cases × {repeats} repeat(s) per configuration. Same prompt, parser and normaliser as the live classifier. "
             f"Budget: a card must be on the board inside {BUDGET_S:.0f} s of the commit; the model call has to fit in that with the 2 s debounce.", "",
             "| Model | Thinking | Calls | Errors | Valid JSON | p50 | p95 | ≤5 s | Subject | Spurious card | Verbatim name | Addressed P / R | Investigate | Salience exact / ±1 |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        s = r["summary"]
        lines.append(
            f"| {r['model'].split('/')[-1]} | {'on' if r['thinking'] else 'off'} | {s['calls']} | {s['errors']} | {fmt(s['valid_rate'])} | "
            f"{fmt(s['latency_p50'], False)} | {fmt(s['latency_p95'], False)} | {fmt(s['under_budget'])} | {fmt(s['subject_acc'])} | "
            f"{fmt(s['spurious_card_rate'])} | {fmt(s['verbatim_name_rate'])} | {fmt(s['addressed_precision'])} / {fmt(s['addressed_recall'])} | "
            f"{fmt(s['investigate_acc'])} | {fmt(s['salience_exact'])} / {fmt(s['salience_within1'])} |")
    lines += ["", "## Per-case failures", ""]
    for r in results:
        bad = [row for row in r["rows"] if not row["score"].get("valid") or not row["score"].get("subject_ok", True)
               or not row["score"].get("addressed_ok", True) or not row["score"].get("investigate_ok", True)]
        lines.append(f"**{r['model'].split('/')[-1]} · thinking {'on' if r['thinking'] else 'off'}** — {len(bad)} of {len(r['rows'])} calls off")
        for row in bad:
            sc = row["score"]
            why = []
            if not sc.get("valid"):
                why.append(f"invalid ({row['error']})")
            if not sc.get("subject_ok", True):
                ns = (row["signal"] or {}).get("new_subject")
                why.append(f"subject → {ns['title']!r} ({ns['named_by']})" if ns else "subject → none")
            if not sc.get("addressed_ok", True):
                why.append(f"addressed → {sc['addressed_pred']}")
            if not sc.get("investigate_ok", True):
                why.append(f"investigate → {sc['investigate_pred']}")
            lines.append(f"- `{row['case']}`: " + "; ".join(why))
        lines.append("")
    return "\n".join(lines)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="*", default=DEFAULT_MODELS)
    ap.add_argument("--thinking", choices=["off", "on", "both"], default="both")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--concurrency", type=int, default=1)
    ap.add_argument("--timeout", type=float, default=40.0)
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent / "results"))
    args = ap.parse_args()
    if not CONFIG.has_nvidia:
        print("NVIDIA_API_KEY is unset", file=sys.stderr)
        return 2
    from openai import AsyncOpenAI
    client = AsyncOpenAI(base_url=CONFIG.nvidia_base_url, api_key=CONFIG.nvidia_api_key, timeout=args.timeout, max_retries=0)
    modes = [False, True] if args.thinking == "both" else [args.thinking == "on"]
    results = []
    for model in args.models:
        for thinking in modes:
            print(f"\n== {model} · thinking {'on' if thinking else 'off'}", flush=True)
            rows = await run_config(client, model, thinking, args.repeats, args.concurrency, args.timeout)
            results.append({"model": model, "thinking": thinking, "rows": rows, "summary": summarise(rows)})
            s = results[-1]["summary"]
            print(f"   p50 {s['latency_p50']:.1f}s · p95 {s['latency_p95']:.1f}s · valid {s['valid_rate']:.0%} · subject {fmt(s['subject_acc'])} · addressed P {fmt(s['addressed_precision'])} R {fmt(s['addressed_recall'])}", flush=True)
    stamp = time.strftime("%Y-%m-%d_%H%M")
    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    (out / f"{stamp}.json").write_text(json.dumps({"stamp": stamp, "repeats": args.repeats, "cases": len(CASES),
                                                   "results": [{k: v for k, v in r.items()} for r in results]}, indent=1, default=str))
    md = render(results, stamp, args.repeats)
    (out / f"{stamp}.md").write_text(md)
    (out / "latest.md").write_text(md)
    print(f"\nwrote {out / (stamp + '.md')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
