"""Scenario checks. Each check reads one turn: its stream events and the proxy's call log."""
import json
import re
from dataclasses import dataclass
from typing import Any, Callable

import httpx

TURN_LIMIT_S = 100.0
SLOW_TOOLS = {"garment_creation", "tryon"}


@dataclass
class TurnResult:
    scenario_id: str
    turn_index: int
    user_id: str
    chat: str
    conversation_id: str
    message: str
    events: list[dict]
    text: str
    artifacts: list[dict]
    outcome: str  # done | error | turn_timeout | http_error
    error: str | None
    duration_s: float
    calls: list[dict]
    prior_calls: list[dict]
    user_messages: list[str]
    transcript: list[tuple[str, str]]


@dataclass
class CheckResult:
    name: str
    passed: bool | None  # None = skipped
    detail: str


Judge = Callable[[TurnResult, str], CheckResult]


def _ok(call: dict) -> bool:
    return call["status"] == 200 and call["response"].get("ok") is True


def _data(call: dict) -> dict:
    return call["response"].get("data") or {}


def _signature(call: dict) -> str:
    return call["tool"] + json.dumps(call["request"], sort_keys=True, ensure_ascii=False)


def turn_completed(t: TurnResult, _) -> CheckResult:
    passed = t.outcome == "done" and t.duration_s < TURN_LIMIT_S
    return CheckResult("turn_completed", passed, f"outcome={t.outcome} duração={t.duration_s:.1f}s {t.error or ''}".strip())


def feedback_shown(t: TurnResult, _) -> CheckResult:
    if not any(c["tool"] in SLOW_TOOLS for c in t.calls):
        return CheckResult("feedback_shown", True, "turno sem garment/try-on")
    for ev in t.events:
        if ev["type"] == "feedback":
            return CheckResult("feedback_shown", True, "feedback antes do resultado")
        if ev["type"] in ("done", "error") or (
                ev["type"] == "artifact" and ev["artifact"].get("kind") in ("GARMENT", "TRYON_CARD")):
            break
    return CheckResult("feedback_shown", False, "nenhum feedback antes do resultado de garment/try-on")


def no_tools(t: TurnResult, _) -> CheckResult:
    used = [c["tool"] for c in t.calls if c["tool"] != "show_artifact"]
    return CheckResult("no_tools", not used, f"tools: {used}" if used else "nenhuma tool")


def max_tool_calls(t: TurnResult, n) -> CheckResult:
    return CheckResult("max_tool_calls", len(t.calls) <= int(n), f"{len(t.calls)} chamadas (limite {n})")


def no_repeat(t: TurnResult, _) -> CheckResult:
    counts: dict[str, int] = {}
    for c in t.calls:
        counts[_signature(c)] = counts.get(_signature(c), 0) + 1
    worst = max(counts.values(), default=0)
    return CheckResult("no_repeat", worst <= 2, f"maior repetição de uma chamada idêntica: {worst}")


def tool_order(t: TurnResult, _) -> CheckResult:
    items: set[str] = set()
    garments: set[str] = set()
    for c in t.prior_calls + t.calls:
        if c["tool"] == "garment_creation":
            if c["request"].get("search_item_id") not in items:
                return CheckResult("tool_order", False, f"garment_creation de {c['request'].get('search_item_id')} sem busca antes")
        if c["tool"] == "tryon":
            missing = [g for g in c["request"].get("garment_ids", []) if g not in garments]
            if missing:
                return CheckResult("tool_order", False, f"try-on com peças não criadas/listadas: {missing}")
        if not _ok(c):
            continue
        data = _data(c)
        if c["tool"] == "search_by_text":
            items |= {p["search_item_id"] for p in data.get("products", [])}
        elif c["tool"] == "garment_creation":
            garments.add(data.get("garment_id"))
        elif c["tool"] == "wardrobe":
            garments |= {g["garment_id"] for g in data.get("garments", [])}
        elif c["tool"] == "looks":
            garments |= {g for lk in data.get("looks", []) for g in lk.get("garment_ids", [])}
    return CheckResult("tool_order", True, "ordem ok")


def retry_policy(t: TurnResult, arg) -> CheckResult:
    max_retries = int((arg or {}).get("max_retries", 2))
    for i, c in enumerate(t.calls):
        err = c["response"].get("error") if not _ok(c) else None
        if not err:
            continue
        sig = _signature(c)
        retries = 0
        for later in t.calls[i + 1:]:
            if _signature(later) != sig:
                continue
            retries += 1
            if _ok(later):
                break
        limit = max_retries if err.get("retryable") else 0
        if retries > limit:
            kind = "retryable" if err.get("retryable") else "não-retryable"
            return CheckResult("retry_policy", False, f"{c['tool']} ({err.get('code')}, {kind}) repetida {retries}x (limite {limit})")
    return CheckResult("retry_policy", True, "retentativas dentro do limite")


def artifact_shown(t: TurnResult, kind) -> CheckResult:
    kinds = [a.get("kind") for a in t.artifacts]
    return CheckResult("artifact_shown", kind in kinds, f"mostrados: {kinds}")


def price_max(t: TurnResult, x) -> CheckResult:
    over = [a.get("price") for a in t.artifacts if a.get("kind") == "PRODUCT" and a.get("price", 0) > float(x)]
    return CheckResult("price_max", not over, f"acima de {x}: {over}" if over else f"todos ≤ {x}")


def forbidden_tag(t: TurnResult, tag) -> CheckResult:
    bad = [a.get("item_id") or a.get("tryon_id") for a in t.artifacts if tag in (a.get("tags") or [])]
    return CheckResult("forbidden_tag", not bad, f"com '{tag}': {bad}" if bad else f"nenhum '{tag}'")


PRICE = re.compile(r"R\$\s*(\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?|\d+(?:[.,]\d{1,2})?)")
NUMBER = re.compile(r"\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?|\d+(?:[.,]\d{1,2})?")


def _parse(s: str) -> float:
    if "," in s:
        return float(s.replace(".", "").replace(",", "."))
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", s):
        return float(s.replace(".", ""))
    return float(s)


def grounded(t: TurnResult, _) -> CheckResult:
    known = [p["price"] for c in t.prior_calls + t.calls if c["tool"] == "search_by_text" and _ok(c)
             for p in _data(c).get("products", [])]
    known += [_parse(n) for m in t.user_messages for n in NUMBER.findall(m)]
    invented = [m for m in PRICE.findall(t.text) if not any(abs(_parse(m) - k) <= 0.5 for k in known)]
    return CheckResult("grounded", not invented, f"preços sem origem: {invented}" if invented else "preços conferem")


def judge(t: TurnResult, rubric, judge_fn: Judge | None) -> CheckResult:
    if judge_fn is None:
        return CheckResult("judge", None, "pulado: sem JUDGE_API_KEY")
    return judge_fn(t, str(rubric))


CHECKS = {"turn_completed": turn_completed, "feedback_shown": feedback_shown, "no_tools": no_tools,
          "max_tool_calls": max_tool_calls, "no_repeat": no_repeat, "tool_order": tool_order,
          "retry_policy": retry_policy, "artifact_shown": artifact_shown, "price_max": price_max,
          "forbidden_tag": forbidden_tag, "grounded": grounded}
CHECK_NAMES = frozenset(CHECKS) | {"judge"}


def parse_spec(spec: str | dict) -> tuple[str, Any]:
    name, arg = (spec, None) if isinstance(spec, str) else next(iter(spec.items()))
    if name not in CHECK_NAMES:
        raise ValueError(f"check desconhecido: {name}")
    return name, arg


def run_check(spec: str | dict, turn: TurnResult, judge: Judge | None = None) -> CheckResult:
    name, arg = parse_spec(spec)
    if name == "judge":
        return globals()["judge"](turn, arg, judge)
    return CHECKS[name](turn, arg)


JUDGE_PROMPT = """Você avalia respostas de um assistente de moda (Doris) em português do Brasil.
Critério deste cenário: {rubric}

Conversa até aqui:
{transcript}

Cards mostrados ao usuário neste turno: {artifacts}

Responda SOMENTE com JSON: {{"score": <1 a 5>, "reason": "<uma frase>"}}.
5 = atende plenamente ao critério; 3 = aceitável; 1 = falha."""


def make_judge(api_key: str, model: str, http: httpx.Client) -> Judge:
    def run(t: TurnResult, rubric: str) -> CheckResult:
        transcript = "\n".join(f"{'Usuário' if role == 'user' else 'Doris'}: {text}" for role, text in t.transcript)
        artifacts = [{k: a.get(k) for k in ("kind", "name", "price", "tags")} for a in t.artifacts]
        prompt = JUDGE_PROMPT.format(rubric=rubric, transcript=transcript,
                                     artifacts=json.dumps(artifacts, ensure_ascii=False))
        try:
            r = http.post("https://openrouter.ai/api/v1/chat/completions",
                          headers={"Authorization": f"Bearer {api_key}"},
                          json={"model": model, "temperature": 0, "messages": [{"role": "user", "content": prompt}]},
                          timeout=60)
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
            verdict = json.loads(re.search(r"\{.*\}", content, re.S).group(0))
            score = int(verdict["score"])
        except Exception as e:  # noqa: BLE001 — any judge failure is a failed check, never a crash
            return CheckResult("judge", False, f"judge falhou: {e}")
        return CheckResult("judge", score >= 3, f"nota {score}: {verdict.get('reason', '')}")

    return run
