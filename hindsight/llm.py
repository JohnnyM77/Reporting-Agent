"""One LLM client per run: call cap, two model tiers, strict JSON, one retry.

Mirrors ``agent.py::_call_anthropic`` (streaming above a token threshold)
without importing ``agent.py``, which drags in weasyprint and Playwright.

Every call ends in exactly one status, and none of them is a placeholder:

- ``OK``: parsed and validated.
- ``SKIPPED_CAP``: the run hit ``HINDSIGHT_MAX_LLM_CALLS``; nothing was sent.
- ``FAILED_API``: the API raised. The real error class and message are kept.
- ``FAILED_INVALID``: the model answered twice and neither parsed/validated.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
from typing import Any, Callable, Optional, Type

from pydantic import BaseModel, ValidationError

# transport(model, system, messages, max_tokens) -> (text, input_tokens, output_tokens, stop_reason)
Transport = Callable[[str, str, list, int], tuple]


@dataclasses.dataclass
class LLMResult:
    status: str
    data: Optional[BaseModel] = None
    error: str = ""
    raw: str = ""
    model: str = ""


def extract_json(text: str) -> Any:
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        return json.loads(t)
    except ValueError:
        pass
    start, end = t.find("{"), t.rfind("}")
    if start != -1 and end > start:
        return json.loads(t[start:end + 1])
    raise ValueError("response contained no JSON object")


def anthropic_transport(streaming_min: int = 8192) -> Transport:
    def _call(model: str, system: str, messages: list, max_tokens: int):
        import anthropic

        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        kwargs = dict(model=model, max_tokens=max_tokens, system=system, messages=messages)
        if max_tokens >= streaming_min:
            with client.messages.stream(**kwargs) as stream:
                resp = stream.get_final_message()
        else:
            resp = client.messages.create(**kwargs)
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        return text, resp.usage.input_tokens, resp.usage.output_tokens, resp.stop_reason

    return _call


class LLMClient:
    def __init__(self, cfg: dict, transport: Transport | None = None):
        llm = cfg.get("llm", {})
        self.models = {"triage": llm.get("model_triage"), "full": llm.get("model_full")}
        self.max_tokens = {"triage": int(llm.get("max_tokens_triage", 600)), "full": int(llm.get("max_tokens_full", 16000))}
        self.max_calls = int(llm.get("max_calls_per_run", 10))
        self.pricing = llm.get("pricing", {}) or {}
        self.transport = transport or anthropic_transport(int(llm.get("streaming_min_tokens", 8192)))
        self.calls = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self.cost_usd = 0.0
        self.log: list[dict] = []

    def can_call(self) -> bool:
        return self.calls < self.max_calls

    def _price(self, model: str, tin: int, tout: int) -> float:
        pin, pout = self.pricing.get(model) or self.pricing.get("default") or [3.0, 15.0]
        return tin / 1e6 * float(pin) + tout / 1e6 * float(pout)

    def _send(self, model: str, system: str, messages: list, max_tokens: int, label: str) -> tuple[str, str]:
        text, tin, tout, stop = self.transport(model, system, messages, max_tokens)
        self.calls += 1
        tin, tout = int(tin or 0), int(tout or 0)
        self.tokens_in += tin
        self.tokens_out += tout
        cost = self._price(model, tin, tout)
        self.cost_usd += cost
        self.log.append({"label": label, "model": model, "in": tin, "out": tout, "stop": stop, "cost_usd": round(cost, 4)})
        return text, str(stop or "")

    def structured(
        self,
        system: str,
        user: str,
        schema: Type[BaseModel],
        tier: str = "full",
        label: str = "",
        shape_check: Callable[[Any], None] | None = None,
    ) -> LLMResult:
        model = self.models[tier]
        if not self.can_call():
            return LLMResult("SKIPPED_CAP", error=f"run call cap of {self.max_calls} reached", model=model)
        messages: list = [{"role": "user", "content": user}]
        last_error, raw = "", ""
        for attempt in range(2):
            try:
                raw, stop = self._send(model, system, messages, self.max_tokens[tier], label)
            except Exception as exc:  # the real error, verbatim, never a placeholder
                status = getattr(exc, "status_code", None)
                return LLMResult("FAILED_API", error=f"{type(exc).__name__}{f' ({status})' if status else ''}: {exc}",
                                 raw=raw, model=model)
            try:
                if stop == "max_tokens":
                    raise ValueError(f"response truncated at max_tokens={self.max_tokens[tier]}")
                data = schema.model_validate(extract_json(raw))
                if shape_check:
                    shape_check(data)
                return LLMResult("OK", data=data, raw=raw, model=model)
            except (ValueError, ValidationError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt == 0:
                    messages = messages + [
                        {"role": "assistant", "content": raw or "(empty)"},
                        {"role": "user", "content": "Your JSON failed validation with this error:\n"
                                                    f"{last_error[:2000]}\nReturn the corrected JSON object only."},
                    ]
        return LLMResult("FAILED_INVALID", error=last_error, raw=raw, model=model)

    def summary(self) -> dict:
        return {
            "calls": self.calls,
            "cap": self.max_calls,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "cost_usd_estimate": round(self.cost_usd, 4),
            "log": self.log,
        }
