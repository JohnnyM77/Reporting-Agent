"""shared/llm.py: the one Claude transport. Prompts/caps/parsing stay in agents."""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace as NS
from unittest import mock

import pytest

from shared import llm


def _msg(text="hello", stop="end_turn", blocks=None, tin=10, tout=5):
    return NS(content=blocks if blocks is not None else [NS(type="text", text=text)],
              stop_reason=stop, usage=NS(input_tokens=tin, output_tokens=tout))


def _client(message):
    client = mock.MagicMock()
    client.messages.create.return_value = message
    stream_cm = mock.MagicMock()
    stream_cm.text_stream = iter([message.content[0].text if message.content else ""])
    stream_cm.get_final_message.return_value = message
    stream_cm.__enter__.return_value = stream_cm
    client.messages.stream.return_value = stream_cm
    return client


def test_short_call_uses_create_and_extracts_everything():
    client = _client(_msg("hi", tin=12, tout=3))
    r = llm.send(client, model="m", max_tokens=1000, system="sys", messages=[{"role": "user", "content": "x"}])
    assert (r.text, r.stop_reason, r.input_tokens, r.output_tokens, r.truncated) == ("hi", "end_turn", 12, 3, False)
    kwargs = client.messages.create.call_args.kwargs
    assert kwargs == {"model": "m", "max_tokens": 1000, "system": "sys", "messages": [{"role": "user", "content": "x"}]}
    client.messages.stream.assert_not_called()


def test_long_call_streams():
    client = _client(_msg("long", stop="max_tokens"))
    r = llm.send(client, model="m", max_tokens=llm.STREAMING_MIN_TOKENS, messages=[])
    assert r.text == "long" and r.truncated
    client.messages.create.assert_not_called()
    assert "system" not in client.messages.stream.call_args.kwargs


def test_extra_kwargs_pass_through():
    client = _client(_msg())
    llm.send(client, model="m", max_tokens=100, messages=[], thinking={"type": "adaptive"})
    assert client.messages.create.call_args.kwargs["thinking"] == {"type": "adaptive"}


def test_response_text_skips_thinking_and_joins_text_blocks():
    m = _msg(blocks=[NS(type="thinking", thinking="hmm"), NS(type="text", text="a"), NS(type="text", text="b")])
    assert llm.response_text(m) == "ab"


def test_response_text_untyped_blocks_use_first_text():
    assert llm.response_text(NS(content=[mock.MagicMock(text="first")])) == "first"
    assert llm.response_text(NS(content=[])) == ""


def test_make_client_requires_key_and_imports_lazily(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(llm.LLMConfigError):
        llm.make_client()
    fake = types.ModuleType("anthropic")
    fake.Anthropic = mock.MagicMock(return_value="client")
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", " k \n")
    assert llm.make_client() == "client"
    fake.Anthropic.assert_called_once_with(api_key="k")


def test_describe_error_includes_status():
    exc = RuntimeError("rate limited")
    exc.status_code = 429
    assert llm.describe_error(exc) == "RuntimeError (429): rate limited"
    assert llm.describe_error(ValueError("x")) == "ValueError: x"


def test_call_with_retry(monkeypatch):
    sleeps = []
    monkeypatch.setattr(llm.time, "sleep", sleeps.append)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("once")
        return "ok"

    retried = []
    assert llm.call_with_retry(flaky, on_retry=lambda a, e: retried.append((a, str(e)))) == "ok"
    assert retried == [(1, "once")] and sleeps == [2.0]

    with pytest.raises(RuntimeError, match="always"):
        llm.call_with_retry(lambda: (_ for _ in ()).throw(RuntimeError("always")), max_retries=1)


def test_stream_can_be_forced_either_way():
    client = _client(_msg())
    llm.send(client, model="m", max_tokens=100_000, messages=[], stream=False)
    client.messages.stream.assert_not_called()
    llm.send(client, model="m", max_tokens=10, messages=[], stream=True)
    client.messages.stream.assert_called_once()
