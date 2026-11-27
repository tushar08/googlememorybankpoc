"""Run the 9-step storyline benchmark and score the 7 metrics.

    BACKEND=vertex python eval/run_storyline.py --runs 3      # live, repeat runs (closes "repeat runs open")
    python eval/run_storyline.py --fake                       # offline smoke with the fake model

Writes eval/results/<timestamp>.json and prints a summary table.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cx_memory.gateway import OmnichannelGateway  # noqa: E402
from eval.metrics import METRICS, aggregate, score_step  # noqa: E402

STORY = json.loads((ROOT / "eval" / "storyline.json").read_text())


async def _reset_memories(gw: OmnichannelGateway, customers: list[str]) -> None:
    """Start each run from source-system facts only, so runs are comparable."""
    if gw.cfg.use_vertex:
        from scripts.ingest_profiles import reset_customer

        for c in customers:
            reset_customer(gw, c)
    for c in customers:
        await gw.seed_customer(c)


async def run_once(gw: OmnichannelGateway, run_idx: int) -> dict:
    sessions: dict[int, str] = {}
    rows = []
    for step in STORY["steps"]:
        if wait := step.get("wait_for_memory_s"):
            if gw.cfg.use_vertex:
                print(f"  waiting {wait}s for Memory Bank extraction/consolidation ...")
                await asyncio.sleep(wait)
        t0 = time.perf_counter()
        if step.get("concurrent"):
            replies = await asyncio.gather(*[gw.handle(q["channel"], q["identifier"], q["text"]) for q in step["requests"]])
        else:
            sid = sessions.get(step["same_session_as"]) if step.get("same_session_as") else None
            replies = [await gw.handle(step["channel"], step["identifier"], step["text"], sid)]
            sessions[step["id"]] = replies[0].session_id
        latency = time.perf_counter() - t0
        for r in replies:
            from cx_memory.profiles import load_profile

            first = load_profile(r.customer_id)["first_name"] if r.customer_id else None
            rd = {**r.__dict__, "first_name": first}
            sc = score_step(step, rd)
            rows.append({"run": run_idx, "step": step["id"], "channel": r.channel, "latency_s": round(latency, 2),
                         "reply": r.text, "tool_calls": r.tool_calls, "scores": sc})
            flag = "OK " if all(v is not False for k, v in sc.items()) else "!! "
            print(f"  {flag}step {step['id']} [{r.channel:8}] {latency:5.2f}s  {r.text[:110]!r}")
    return {"rows": rows, "summary": aggregate([r["scores"] for r in rows])}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--fake", action="store_true", help="use the offline fake model")
    a = ap.parse_args()

    model = None
    if a.fake:
        from tests.fake_llm import FakeLlm

        model = FakeLlm()
    all_runs = []
    for i in range(1, a.runs + 1):
        gw = OmnichannelGateway(model=model)
        await _reset_memories(gw, ["CUST-1001", "CUST-1002"])
        print(f"\n=== run {i}/{a.runs} (backend={gw.cfg.backend}, model={model.model if model else gw.cfg.model}) ===")
        all_runs.append(await run_once(gw, i))

    print("\nmetric                          " + "  ".join(f"run{i+1:<3}" for i in range(a.runs)) + "  mean")
    for m in METRICS:
        rates = [r["summary"][m]["rate"] for r in all_runs]
        cells = "  ".join(f"{(x if x is not None else float('nan')):6.0%}" for x in rates)
        valid = [x for x in rates if x is not None]
        print(f"{m:32}{cells}  {statistics.mean(valid) if valid else float('nan'):.0%}")
    lat = [row["latency_s"] for r in all_runs for row in r["rows"]]
    print(f"\nlatency p50={statistics.median(lat):.2f}s max={max(lat):.2f}s")

    out = ROOT / "eval" / "results"
    out.mkdir(exist_ok=True)
    path = out / f"storyline_{time.strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps(all_runs, indent=2, default=str))
    print(f"saved {path.relative_to(ROOT)}")


if __name__ == "__main__":
    asyncio.run(main())
