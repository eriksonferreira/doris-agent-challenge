"""Eval runner: drives scenarios through the proxy's /chat (like the frontend), reads /admin/log,
then evaluates the checks. Judge calls run last, so they never count toward the agent's cost."""
import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
import yaml

from checks import CheckResult, TurnResult, make_judge, parse_spec, run_check

CLIENT_TIMEOUT_S = 130.0
ICON = {True: "✅", False: "❌", None: "⏭"}


def load_scenarios(paths: list[Path]) -> list[dict]:
    scenarios = []
    for path in paths:
        for s in yaml.safe_load(Path(path).read_text())["scenarios"]:
            for t in s["turns"]:
                for spec in t.get("checks", []):
                    parse_spec(spec)
            scenarios.append(s)
    return scenarios


def openrouter_usage(api_key: str, http: httpx.Client) -> float | None:
    try:
        r = http.get("https://openrouter.ai/api/v1/key", headers={"Authorization": f"Bearer {api_key}"}, timeout=30)
        r.raise_for_status()
        return float(r.json()["data"]["usage"])
    except Exception:  # noqa: BLE001 — cost is best-effort
        return None


def wait_ready(http: httpx.Client, agent_url: str | None, timeout_s: float = 60.0) -> bool:
    """Poll the proxy's /health (and the agent's, when reachable from here) until both answer 200."""
    deadline = time.monotonic() + timeout_s
    targets = [lambda: http.get("/health")]
    if agent_url:
        targets.append(lambda: httpx.get(f"{agent_url.rstrip('/')}/health", timeout=2))
    while True:
        try:
            if all(probe().status_code == 200 for probe in targets):
                return True
        except httpx.HTTPError:
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)


def settle_usage(read, interval_s: float = 5, max_wait_s: float = 60, sleep=time.sleep) -> float | None:
    """OpenRouter usage lags a little: read until two consecutive readings agree (capped)."""
    prev = read()
    if prev is None:
        return None
    waited = 0.0
    while waited < max_wait_s:
        sleep(interval_s)
        waited += interval_s
        cur = read()
        if cur is None or cur == prev:
            return prev
        prev = cur
    return prev


def drive_turn(http: httpx.Client, payload: dict) -> dict:
    t0 = time.monotonic()
    events, status, error = [], None, None
    try:
        with http.stream("POST", "/chat", json=payload) as r:
            status = r.status_code
            if status != 200:
                error = f"HTTP {status}: {r.read().decode(errors='replace')[:300]}"
            else:
                for line in r.iter_lines():
                    if line.startswith("data:"):
                        try:
                            events.append(json.loads(line[5:]))
                        except ValueError:
                            continue
    except httpx.HTTPError as e:
        error = f"falha de conexão: {e}"
    last = events[-1] if events else {}
    if last.get("type") == "done":
        outcome = "done"
    elif last.get("type") == "error":
        outcome = "turn_timeout" if last.get("code") == "turn_timeout" else "error"
        error = f"{last.get('code')}: {last.get('message')}"
    else:
        outcome = "http_error"
        error = error or "stream terminou sem done/error"
    return {"events": events, "status": status, "outcome": outcome, "error": error,
            "duration_s": time.monotonic() - t0}


def run(proxy_url: str, scenario_paths: list[Path], out_dir: Path, run_id: str | None = None,
        openrouter_key: str | None = None, judge_key: str | None = None,
        judge_model: str = "google/gemini-3.8-flash", http: httpx.Client | None = None,
        agent_url: str | None = None, ready_timeout_s: float = 60.0) -> dict:
    scenarios = load_scenarios(scenario_paths)
    run_id = run_id or datetime.now(timezone.utc).strftime("%m%d%H%M%S")
    http = http or httpx.Client(base_url=proxy_url, timeout=CLIENT_TIMEOUT_S)
    ext = httpx.Client(timeout=60)
    started = time.monotonic()

    if not wait_ready(http, agent_url, ready_timeout_s):
        print("aviso: proxy/agente não responderam ao /health; rodando mesmo assim", file=sys.stderr)
    try:
        http.post("/admin/reset")
    except httpx.HTTPError as e:
        print(f"aviso: não foi possível resetar o proxy: {e}", file=sys.stderr)
    usage_before = openrouter_usage(openrouter_key, ext) if openrouter_key else None

    # Phase 1 — drive every turn.
    driven = []
    for index, s in enumerate(scenarios):
        chats: dict[str, str] = {}
        for turn_index, t in enumerate(s["turns"]):
            user_id = f"{t['user']}__{run_id}s{index}"
            cid = chats.setdefault(t["chat"], str(uuid.uuid4()))
            payload = {"user_id": user_id, "conversation_id": cid, "message": t["message"],
                       "images": t.get("images", [])}
            driven.append({"scenario": s, "index": index, "turn_index": turn_index, "turn": t, "payload": payload,
                           **drive_turn(http, payload)})

    usage_after = settle_usage(lambda: openrouter_usage(openrouter_key, ext)) if openrouter_key else None
    cost = round(usage_after - usage_before, 6) if usage_before is not None and usage_after is not None else None

    # Phase 2 — pair driven turns with logged turns, build TurnResults.
    # Chat history is per conversation; prior tool calls are per user (memory may reuse ids across chats).
    logs: dict[str, list[dict]] = {}
    chat_hist: dict[str, dict] = {}
    user_calls: dict[tuple[int, str], list[dict]] = {}
    results: list[tuple[dict, TurnResult]] = []
    for d in driven:
        cid = d["payload"]["conversation_id"]
        if cid not in logs:
            try:
                logs[cid] = http.get("/admin/log", params={"conversation_id": cid}).json()["turns"]
            except (httpx.HTTPError, ValueError, KeyError):
                logs[cid] = []
        h = chat_hist.setdefault(cid, {"user": [], "transcript": [], "logged": 0})
        prior = user_calls.setdefault((d["index"], d["payload"]["user_id"]), [])
        calls = []
        if d["status"] == 200 and h["logged"] < len(logs[cid]):
            calls = logs[cid][h["logged"]]["calls"]
            h["logged"] += 1
        text = "".join(e.get("delta", "") for e in d["events"] if e.get("type") == "text")
        h["user"].append(d["turn"]["message"])
        h["transcript"] += [("user", d["turn"]["message"]), ("assistant", text)]
        results.append((d, TurnResult(
            scenario_id=d["scenario"]["id"], turn_index=d["turn_index"],
            user_id=d["payload"]["user_id"], chat=d["turn"]["chat"], conversation_id=cid,
            message=d["turn"]["message"], events=d["events"], text=text,
            artifacts=[e["artifact"] for e in d["events"] if e.get("type") == "artifact"],
            outcome=d["outcome"], error=d["error"], duration_s=d["duration_s"], calls=calls,
            prior_calls=list(prior), user_messages=list(h["user"]), transcript=list(h["transcript"]))))
        prior += calls

    # Phase 3 — checks (judge last).
    judge = make_judge(judge_key, judge_model, ext) if judge_key else None
    report = {"run_id": run_id, "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "cost_usd": cost, "scenarios": [], "summary": {"passed": 0, "failed": 0, "skipped": 0}}
    by_scenario: dict[str, dict] = {}
    for d, tr in results:
        s = d["scenario"]
        entry = by_scenario.get(s["id"])
        if entry is None:
            entry = by_scenario[s["id"]] = {"id": s["id"], "description": s.get("description", ""), "turns": []}
            report["scenarios"].append(entry)
        checks: list[CheckResult] = [run_check(spec, tr, judge=judge) for spec in d["turn"].get("checks", [])]
        for c in checks:
            report["summary"][{True: "passed", False: "failed", None: "skipped"}[c.passed]] += 1
        entry["turns"].append({"user_id": tr.user_id, "chat": tr.chat, "message": tr.message, "outcome": tr.outcome,
                               "error": tr.error, "duration_s": round(tr.duration_s, 2), "tool_calls": len(tr.calls),
                               "text": tr.text, "checks": [c.__dict__ for c in checks]})
    report["duration_s"] = round(time.monotonic() - started, 1)

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    (out_dir / "report.md").write_text(render_markdown(report))
    return report


def render_markdown(report: dict) -> str:
    s = report["summary"]
    cost = "n/d (sem OPENROUTER_API_KEY)" if report["cost_usd"] is None else f"US$ {report['cost_usd']:.4f}"
    lines = [f"# Relatório do eval — {report['run_id']}", "",
             f"✅ {s['passed']} · ❌ {s['failed']} · ⏭ {s['skipped']} · custo: {cost} · duração: {report['duration_s']}s", ""]
    for sc in report["scenarios"]:
        lines += [f"## {sc['id']}", sc["description"], ""]
        for i, t in enumerate(sc["turns"]):
            lines.append(f"**Turno {i + 1}** ({t['chat']}) — “{t['message']}” → {t['outcome']}, "
                         f"{t['duration_s']}s, {t['tool_calls']} tool calls")
            if t["error"]:
                lines.append(f"> erro: {t['error']}")
            for c in t["checks"]:
                lines.append(f"- {ICON[c['passed']]} `{c['name']}` — {c['detail']}")
            lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None, http: httpx.Client | None = None) -> int:
    p = argparse.ArgumentParser(description="Roda os cenários contra o proxy e gera o relatório.")
    p.add_argument("--scenarios", action="append", type=Path)
    p.add_argument("--proxy", default=os.environ.get("PROXY_URL", "http://localhost:3000"))
    p.add_argument("--out", type=Path, default=Path("/out"))
    a = p.parse_args(argv)
    report = run(a.proxy, a.scenarios or [Path(__file__).parent / "scenarios.yaml"], a.out,
                 openrouter_key=os.environ.get("OPENROUTER_API_KEY") or None,
                 judge_key=os.environ.get("JUDGE_API_KEY") or None,
                 judge_model=os.environ.get("JUDGE_MODEL") or "google/gemini-3.8-flash", http=http,
                 agent_url=os.environ.get("AGENT_URL") or None)
    print(render_markdown(report))
    return 1 if report["summary"]["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
