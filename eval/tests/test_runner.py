import json
from pathlib import Path

import httpx
import yaml
from fastapi.testclient import TestClient

from app.main import create_app
from checks import parse_spec
from runner import main, run
from testkit import make_settings, wire

PUBLIC = Path(__file__).parents[1] / "scenarios.yaml"


def scenario_file(tmp_path, scenarios) -> Path:
    p = tmp_path / "s.yaml"
    p.write_text(yaml.safe_dump({"scenarios": scenarios}, allow_unicode=True))
    return p


async def full_flow(b, proxy):
    cid = b["conversation_id"]
    tool = lambda name, **kw: proxy.post(f"/tools/{name}", json={"conversation_id": cid, **kw})
    products = (await tool("search_by_text", query="camisa linho")).json()["data"]["products"]
    yield {"type": "feedback", "id": "tryon", "text": "Gerando try-on…", "state": "running"}
    garment = (await tool("garment_creation", search_item_id=products[0]["search_item_id"])).json()["data"]
    tryon = (await tool("tryon", garment_ids=[garment["garment_id"]])).json()["data"]
    await tool("show_artifact", type="TRYON_CARD", value=tryon["tryon_id"])
    yield {"type": "feedback", "id": "tryon", "text": "Pronto", "state": "done"}
    yield {"type": "text", "delta": f"Separei a {products[0]['name']}."}
    yield {"type": "done"}


FLOW_CHECKS = ["turn_completed", "tool_order", "no_repeat", "feedback_shown", {"artifact_shown": "TRYON_CARD"},
               "grounded"]


def test_full_flow_report(tmp_path):
    http = TestClient(wire(tmp_path, full_flow))
    path = scenario_file(tmp_path, [{"id": "flow", "description": "fluxo", "turns": [
        {"user": "u_bruno", "chat": "A", "message": "casamento na praia", "checks": FLOW_CHECKS}]}])
    report = run("http://proxy", [path], tmp_path / "out", run_id="r1", http=http)
    turn = report["scenarios"][0]["turns"][0]
    assert {c["name"]: c["passed"] for c in turn["checks"]} == {
        "turn_completed": True, "tool_order": True, "no_repeat": True, "feedback_shown": True,
        "artifact_shown": True, "grounded": True}
    assert turn["outcome"] == "done" and turn["tool_calls"] == 4
    assert report["summary"] == {"passed": 6, "failed": 0, "skipped": 0}
    assert json.loads((tmp_path / "out/report.json").read_text())["run_id"] == "r1"
    assert "flow" in (tmp_path / "out/report.md").read_text()


def test_prior_calls_carry_across_turns(tmp_path):
    async def behavior(b, proxy):
        cid = b["conversation_id"]
        if b["message"] == "busca":
            await proxy.post("/tools/search_by_text", json={"conversation_id": cid, "query": "camisa linho"})
        else:
            await proxy.post("/tools/garment_creation", json={"conversation_id": cid, "search_item_id": "p01"})
        yield {"type": "done"}

    http = TestClient(wire(tmp_path, behavior))
    path = scenario_file(tmp_path, [{"id": "s", "turns": [
        {"user": "u_ana", "chat": "A", "message": "busca", "checks": ["turn_completed"]},
        {"user": "u_ana", "chat": "A", "message": "cria", "checks": ["tool_order"]}]}])
    report = run("http://proxy", [path], tmp_path / "out", run_id="r", http=http)
    assert report["scenarios"][0]["turns"][1]["checks"][0]["passed"] is True


def test_user_ids_isolated_per_scenario(tmp_path):
    async def behavior(b, proxy):
        yield {"type": "done"}

    app = wire(tmp_path, behavior)
    turns = [{"user": "u_ana", "chat": "A", "message": "oi", "checks": ["turn_completed"]},
             {"user": "u_ana", "chat": "B", "message": "oi de novo", "checks": ["turn_completed"]}]
    path = scenario_file(tmp_path, [{"id": "a", "turns": turns}, {"id": "b", "turns": turns[:1]}])
    run("http://proxy", [path], tmp_path / "out", run_id="r9", http=TestClient(app))
    received = app.state.fake_agent.state.received
    ids = [r["user_id"] for r in received]
    assert ids[0] == ids[1] != ids[2]
    assert all(i.startswith("u_ana__r9") for i in ids)
    assert received[0]["conversation_id"] != received[1]["conversation_id"]  # chats A and B


def test_agent_down_is_reported(tmp_path):
    def refuse(request):
        raise httpx.ConnectError("refused")

    http = TestClient(create_app(make_settings(tmp_path), agent_transport=httpx.MockTransport(refuse)))
    path = scenario_file(tmp_path, [{"id": "s", "turns": [
        {"user": "u_ana", "chat": "A", "message": "oi", "checks": ["turn_completed"]}]}])
    out = tmp_path / "out"
    report = run("http://proxy", [path], out, run_id="r", http=http)
    turn = report["scenarios"][0]["turns"][0]
    assert turn["outcome"] == "error" and turn["checks"][0]["passed"] is False
    assert (out / "report.md").is_file()
    assert main(["--scenarios", str(path), "--out", str(out)], http=http) == 1


def test_judge_and_cost_skipped_without_keys(tmp_path):
    async def behavior(b, proxy):
        yield {"type": "text", "delta": "Oi!"}
        yield {"type": "done"}

    path = scenario_file(tmp_path, [{"id": "s", "turns": [
        {"user": "u_ana", "chat": "A", "message": "oi", "checks": [{"judge": "cumprimenta"}]}]}])
    report = run("http://proxy", [path], tmp_path / "out", http=TestClient(wire(tmp_path, behavior)))
    assert report["scenarios"][0]["turns"][0]["checks"][0]["passed"] is None
    assert report["cost_usd"] is None
    assert report["summary"] == {"passed": 0, "failed": 0, "skipped": 1}


def test_public_scenarios_use_known_checks():
    scenarios = yaml.safe_load(PUBLIC.read_text())["scenarios"]
    assert len(scenarios) == 6
    for s in scenarios:
        for t in s["turns"]:
            assert t["user"] in {"u_ana", "u_bruno", "u_carla"}
            for spec in t["checks"]:
                parse_spec(spec)


def test_tool_order_sees_other_chats_of_same_user(tmp_path):
    async def behavior(b, proxy):
        cid = b["conversation_id"]
        if b["message"] == "busca":
            await proxy.post("/tools/search_by_text", json={"conversation_id": cid, "query": "camisa linho"})
        else:
            g = (await proxy.post("/tools/garment_creation", json={"conversation_id": cid, "search_item_id": "p01"})).json()
            await proxy.post("/tools/tryon", json={"conversation_id": cid, "garment_ids": [g["data"]["garment_id"]]})
        yield {"type": "done"}

    path = scenario_file(tmp_path, [{"id": "s", "turns": [
        {"user": "u_ana", "chat": "A", "message": "busca", "checks": ["turn_completed"]},
        {"user": "u_ana", "chat": "B", "message": "prova", "checks": ["tool_order"]}]}])
    report = run("http://proxy", [path], tmp_path / "out", run_id="r", http=TestClient(wire(tmp_path, behavior)))
    assert report["scenarios"][0]["turns"][1]["checks"][0]["passed"] is True


def test_proxy_down_still_writes_report(tmp_path):
    path = scenario_file(tmp_path, [{"id": "s", "turns": [
        {"user": "u_ana", "chat": "A", "message": "oi", "checks": ["turn_completed"]}]}])
    dead = httpx.Client(base_url="http://127.0.0.1:9", timeout=0.5)
    report = run("http://127.0.0.1:9", [path], tmp_path / "out", run_id="r", http=dead, ready_timeout_s=0.2)
    assert report["scenarios"][0]["turns"][0]["outcome"] == "http_error"
    assert (tmp_path / "out/report.md").is_file()


def test_wait_ready():
    from runner import wait_ready
    ok = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True})), base_url="http://p")
    assert wait_ready(ok, None, timeout_s=1) is True
    dead = httpx.Client(base_url="http://127.0.0.1:9", timeout=0.2)
    assert wait_ready(dead, None, timeout_s=0.3) is False


def test_settle_usage_waits_for_stable_reading():
    from runner import settle_usage
    reads, sleeps = iter([1.0, 1.2, 1.2]), []
    assert settle_usage(lambda: next(reads), interval_s=5, max_wait_s=60, sleep=sleeps.append) == 1.2
    assert sleeps == [5, 5]
    assert settle_usage(lambda: None, interval_s=5, max_wait_s=60, sleep=sleeps.append) is None
