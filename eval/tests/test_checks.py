import json

import httpx
import pytest

from checks import CHECK_NAMES, TurnResult, make_judge, run_check


def turn(**over) -> TurnResult:
    base = dict(scenario_id="s", turn_index=0, user_id="u_ana__r", chat="A", conversation_id="c", message="oi",
                events=[], text="", artifacts=[], outcome="done", error=None, duration_s=1.0, calls=[],
                prior_calls=[], user_messages=["oi"], transcript=[("user", "oi")])
    base.update(over)
    return TurnResult(**base)


def call(tool, request=None, status=200, data=None, error=None):
    response = {"ok": False, "error": error} if error else {"ok": True, "data": data or {}}
    return {"tool": tool, "request": request or {}, "status": status, "error_code": (error or {}).get("code"),
            "response": response}


SEARCH_P01 = call("search_by_text", {"query": "camisa", "limit": 6},
                  data={"products": [{"search_item_id": "p01", "price": 249.9}]})
GARMENT_P01 = call("garment_creation", {"search_item_id": "p01"}, data={"garment_id": "g1"})
TRYON_G1 = call("tryon", {"garment_ids": ["g1"]}, data={"tryon_id": "t1"})
RETRYABLE = {"code": "upstream_timeout", "message": "x", "retryable": True}
FATAL = {"code": "garment_processing_failed", "message": "x", "retryable": False}


def check(spec, t, **kw):
    return run_check(spec, t, **kw).passed


def test_check_names():
    assert CHECK_NAMES == {"turn_completed", "feedback_shown", "no_tools", "max_tool_calls", "no_repeat", "tool_order",
                           "retry_policy", "artifact_shown", "price_max", "forbidden_tag", "grounded", "judge"}


def test_unknown_check_raises():
    with pytest.raises(ValueError):
        run_check("nope", turn())


def test_turn_completed():
    assert check("turn_completed", turn()) is True
    assert check("turn_completed", turn(outcome="turn_timeout")) is False
    assert check("turn_completed", turn(duration_s=101)) is False


def test_feedback_shown_before_tryon_result():
    evs = [{"type": "feedback", "id": "a", "text": "Gerando…", "state": "running"},
           {"type": "artifact", "artifact": {"kind": "TRYON_CARD"}}, {"type": "done"}]
    assert check("feedback_shown", turn(calls=[TRYON_G1], events=evs)) is True
    assert check("feedback_shown", turn(calls=[SEARCH_P01], events=[{"type": "done"}])) is None  # not applicable


def test_feedback_missing_fails():
    late = [{"type": "artifact", "artifact": {"kind": "TRYON_CARD"}},
            {"type": "feedback", "id": "a", "text": "pronto", "state": "done"}, {"type": "done"}]
    assert check("feedback_shown", turn(calls=[GARMENT_P01], events=late)) is False
    assert check("feedback_shown", turn(calls=[TRYON_G1], events=[{"type": "done"}])) is False


def test_no_tools():
    assert check("no_tools", turn()) is True
    assert check("no_tools", turn(calls=[call("show_artifact")])) is True
    assert check("no_tools", turn(calls=[SEARCH_P01])) is False


def test_max_tool_calls():
    assert check({"max_tool_calls": 2}, turn(calls=[SEARCH_P01, GARMENT_P01])) is True
    assert check({"max_tool_calls": 1}, turn(calls=[SEARCH_P01, GARMENT_P01])) is False


def test_no_repeat():
    assert check("no_repeat", turn(calls=[SEARCH_P01, SEARCH_P01])) is True
    assert check("no_repeat", turn(calls=[SEARCH_P01] * 3)) is False


def test_tool_order_full_flow():
    assert check("tool_order", turn(calls=[SEARCH_P01, GARMENT_P01, TRYON_G1])) is True
    assert check("tool_order", turn(prior_calls=[SEARCH_P01], calls=[GARMENT_P01, TRYON_G1])) is True


def test_tool_order_rejects_unsearched_item():
    assert check("tool_order", turn(calls=[GARMENT_P01])) is False


def test_tool_order_rejects_uncreated_garment():
    assert check("tool_order", turn(calls=[SEARCH_P01, TRYON_G1])) is False
    wardrobe = call("wardrobe", data={"garments": [{"garment_id": "w1"}]})
    assert check("tool_order", turn(calls=[wardrobe, call("tryon", {"garment_ids": ["w1"]})])) is True
    looks = call("looks", data={"looks": [{"look_id": "l1", "garment_ids": ["w2"]}]})
    assert check("tool_order", turn(calls=[looks, call("tryon", {"garment_ids": ["w2"]})])) is True


def test_retry_policy_retryable_within_budget():
    failed = call("tryon", {"garment_ids": ["g1"]}, status=503, error=RETRYABLE)
    assert check({"retry_policy": {"max_retries": 2}}, turn(calls=[failed, failed, TRYON_G1])) is True
    assert check({"retry_policy": {"max_retries": 2}}, turn(calls=[failed, failed, failed, TRYON_G1])) is False


def test_retry_policy_fatal_retried_fails():
    fatal = call("garment_creation", {"search_item_id": "p01"}, status=422, error=FATAL)
    assert check({"retry_policy": {"max_retries": 2}}, turn(calls=[fatal])) is True
    assert check({"retry_policy": {"max_retries": 2}}, turn(calls=[fatal, fatal])) is False


def test_artifact_shown():
    t = turn(artifacts=[{"kind": "PRODUCT", "price": 10, "tags": []}])
    assert check({"artifact_shown": "PRODUCT"}, t) is True
    assert check({"artifact_shown": "TRYON_CARD"}, t) is False


def test_price_max():
    ok = turn(artifacts=[{"kind": "PRODUCT", "price": 249.9, "tags": []}])
    bad = turn(artifacts=[{"kind": "PRODUCT", "price": 249.9, "tags": []}, {"kind": "PRODUCT", "price": 459, "tags": []}])
    assert check({"price_max": 300}, ok) is True
    assert check({"price_max": 300}, bad) is False


def test_forbidden_tag():
    assert check({"forbidden_tag": "amarelo"}, turn(artifacts=[{"kind": "PRODUCT", "tags": ["preto"]}])) is True
    assert check({"forbidden_tag": "amarelo"}, turn(artifacts=[{"kind": "TRYON_CARD", "tags": ["amarelo"]}])) is False


def test_grounded_ignores_user_prices():
    t = turn(calls=[SEARCH_P01], user_messages=["Me acha uma camisa até R$300"],
             text="Dentro do seu limite de R$ 300, achei esta por R$ 249,90.")
    assert check("grounded", t) is True


def test_grounded_flags_invented_price():
    t = turn(calls=[SEARCH_P01], text="Achei uma por R$ 199,90.")
    assert check("grounded", t) is False


def test_grounded_parses_ptbr_formats():
    expensive = call("search_by_text", data={"products": [{"search_item_id": "p07", "price": 1299.9}]})
    assert check("grounded", turn(prior_calls=[expensive], text="Custa R$1.299,90.")) is True
    assert check("grounded", turn(text="Sem preços aqui.")) is True


def test_judge_skipped_without_key():
    r = run_check({"judge": "responde em pt-BR"}, turn())
    assert r.passed is None and r.name == "judge"


def test_judge_parses_score():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        content = '{"score": 4, "reason": "ok"}'
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    judge = make_judge("k", "google/gemini-3.8-flash", httpx.Client(transport=httpx.MockTransport(handler)))
    r = run_check({"judge": "responde em pt-BR"}, turn(text="Olá!"), judge=judge)
    assert r.passed is True and "ok" in r.detail
    assert seen["body"]["model"] == "google/gemini-3.8-flash"
    assert "responde em pt-BR" in json.dumps(seen["body"], ensure_ascii=False)


def test_judge_low_score_and_garbage():
    def reply(content):
        return httpx.Client(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"choices": [{"message": {"content": content}}]})))

    assert run_check({"judge": "x"}, turn(), judge=make_judge("k", "m", reply('{"score": 2, "reason": "ruim"}'))).passed is False
    assert run_check({"judge": "x"}, turn(), judge=make_judge("k", "m", reply("sem json"))).passed is False


def test_vacuous_checks_are_skipped():
    assert check("tool_order", turn(calls=[SEARCH_P01])) is None
    assert check({"retry_policy": {"max_retries": 2}}, turn(calls=[SEARCH_P01, TRYON_G1])) is None
