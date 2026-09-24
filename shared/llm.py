# shared/llm.py
#
# One Claude transport for every agent. It owns the boring part of an
# Anthropic call: API key, client construction, streaming for long
# generations, pulling text / stop_reason / token usage out of the response,
# a readable error string, and a retry helper.
#
# It deliberately does NOT own prompts, model choice, call caps, JSON
# parsing or outcome sentinels. Those are each agent's decisions (Bob's
# LLM_SKIPPED/LLM_FAILED, Harry's OK/SKIPPED_CAP/FAILED_API/FAILED_INVALID
# and validation retry) and stay in the agent.
#
# ``anthropic`` is imported inside ``make_client`` so importing this module
# never requires the SDK, and a test that swaps ``sys.modules["anthropic"]``
# is honoured at call time.

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, TypeVar

# Anthropic's Python SDK refuses non-streaming ``messages.create`` calls whose
# expected duration exceeds ~10 minutes ("Streaming is required for
# operations that may take longer than 10 minutes"). Bob's 50k-token results
# budget hits that, so any call at or above this many output tokens goes
# through ``messages.stream``; below it the plain ``create`` path is used.
STREAMING_MIN_TOKENS = 8192

T = TypeVar("T")


class LLMConfigError(RuntimeError):
    """ANTHROPIC_API_KEY is not set."""


def get_api_key(env: Optional[Mapping[str, str]] = None) -> str:
    """ANTHROPIC_API_KEY, stripped; "" when unset."""
    e = os.environ if env is None else env
    return (e.get("ANTHROPIC_API_KEY") or "").strip()


def make_client(api_key: Optional[str] = None) -> Any:
    """Build an ``anthropic.Anthropic`` client. Raises LLMConfigError without a key."""
    key = api_key if api_key is not None else get_api_key()
    if not key:
        raise LLMConfigError("ANTHROPIC_API_KEY is not set")
    import anthropic

    return anthropic.Anthropic(api_key=key)


@dataclass
class LLMResponse:
    text: str
    stop_reason: Optional[str]
    input_tokens: int
    output_tokens: int
    message: Any = None  # the SDK's final Message, for callers that need more

    @property
    def truncated(self) -> bool:
        """True when the model was cut off at max_tokens."""
        return self.stop_reason == "max_tokens"


def response_text(message: Any) -> str:
    """All text blocks of a Message joined, skipping thinking/tool blocks.

    Objects without typed blocks (test doubles, very old SDKs) fall back to
    the first block's ``.text``.
    """
    blocks = list(getattr(message, "content", None) or [])
    texts = [
        b.text for b in blocks
        if getattr(b, "type", None) == "text" and isinstance(getattr(b, "text", None), str)
    ]
    if texts:
        return "".join(texts)
    first = getattr(blocks[0], "text", None) if blocks else None
    return first if isinstance(first, str) else ""


def _usage(message: Any) -> tuple[int, int]:
    usage = getattr(message, "usage", None)
    out = []
    for field in ("input_tokens", "output_tokens"):
        try:
            out.append(int(getattr(usage, field, 0) or 0))
        except (TypeError, ValueError):
            out.append(0)
    return out[0], out[1]


def send(
    client: Any,
    *,
    model: str,
    max_tokens: int,
    messages: list,
    system: Optional[str] = None,
    streaming_min_tokens: int = STREAMING_MIN_TOKENS,
    stream: Optional[bool] = None,
    **extra: Any,
) -> LLMResponse:
    """Make one Messages API call and return its text, stop reason and usage.

    Streams when ``max_tokens >= streaming_min_tokens``, unless *stream*
    forces it one way or the other. *extra* is passed
    straight through (e.g. Wally's ``thinking={"type": "adaptive"}``).
    Exceptions from the SDK propagate unchanged; retrying and turning them
    into a status is the caller's job (see ``call_with_retry``).
    """
    kwargs: dict = dict(model=model, max_tokens=max_tokens, messages=messages, **extra)
    if system is not None:
        kwargs["system"] = system

    if stream is None:
        stream = max_tokens >= streaming_min_tokens
    if stream:
        # Streaming keeps the HTTP connection alive with periodic events, so
        # the SDK's non-streaming ceiling doesn't apply. get_final_message()
        # drains the stream and returns the same Message create() would have.
        with client.messages.stream(**kwargs) as s:
            message = s.get_final_message()
    else:
        message = client.messages.create(**kwargs)
    text = response_text(message)

    stop = getattr(message, "stop_reason", None)
    tin, tout = _usage(message)
    return LLMResponse(
        text=text or "",
        stop_reason=str(stop) if stop else None,
        input_tokens=tin,
        output_tokens=tout,
        message=message,
    )


def describe_error(exc: BaseException) -> str:
    """``ClassName (status): message`` -- the real error, for logs and cards."""
    status = getattr(exc, "status_code", None)
    return f"{type(exc).__name__}{f' ({status})' if status else ''}: {exc}"


def call_with_retry(
    fn: Callable[[], T],
    *,
    max_retries: int = 1,
    delay_seconds: float = 2.0,
    on_retry: Optional[Callable[[int, BaseException], None]] = None,
) -> T:
    """Call *fn*, retrying up to *max_retries* times on any exception.

    ``on_retry(attempt_number, exc)`` is called before each retry (attempt
    numbers start at 1). The last exception is re-raised when every attempt
    fails.
    """
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except Exception as exc:
            if attempt >= max_retries:
                raise
            if on_retry:
                on_retry(attempt + 1, exc)
            time.sleep(delay_seconds)
    raise AssertionError("unreachable")  # pragma: no cover
