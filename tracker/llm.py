"""LLM access behind a protocol, with MiniMax as one implementation.

**MiniMax has no structured output.** `response_format` — both `json_object` and
`json_schema` — is *silently ignored* on the M2.x and M3 models: no error, no
warning, just prose-wrapped JSON as if you had never asked. So the JSON contract
is enforced here, in code we can test, by parse → repair → validate → one
corrective retry. Anything that promised schema enforcement (including
Crawl4AI's `LLMExtractionStrategy` `schema=` parameter) would be promising
something the provider does not do.

Three MiniMax-specific details that each cost a debugging session if missed:

* the parameter is ``max_completion_tokens``, not ``max_tokens``;
* ``role`` must be ``"system"`` — ``"developer"`` returns *invalid role* (2013);
* the global (``api.minimax.io``) and China (``api.minimaxi.com``) platforms are
  separate, and **their keys are not interchangeable**. The wrong host answers
  *invalid api key*, which reads like a bad key rather than a wrong URL.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from tracker.config import Settings, get_settings

log = logging.getLogger(__name__)

KEY_HELP = """TRACKER_MINIMAX_API_KEY is not set.

  Recommended -- add it to the .env file beside pyproject.toml, which is
  gitignored and is read no matter which directory you run `tracker` from:
    TRACKER_MINIMAX_API_KEY=your-key

  Or just for this shell:
    PowerShell   $env:TRACKER_MINIMAX_API_KEY = 'your-key'
    Git Bash     export TRACKER_MINIMAX_API_KEY=your-key

  Note the TRACKER_ prefix: every setting this tool reads carries it.

MiniMax runs two separate platforms and the keys are NOT interchangeable:
  global  platform.minimax.io   (email signup) -> https://api.minimax.io/v1
  China   platform.minimaxi.com (phone signup) -> https://api.minimaxi.com/v1
Set TRACKER_MINIMAX_BASE_URL to match the platform your key came from; the wrong
host reports "invalid api key", which looks like a bad key but is a bad URL.

Check connectivity without ingesting anything:
  tracker ingest crawl --check
"""

#: HTTP statuses worth retrying.
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class MissingApiKey(RuntimeError):
    """No API key configured. Message is operator-facing."""


class LLMError(RuntimeError):
    """The provider call failed after retries."""


class LLMJsonError(ValueError):
    """The reply could not be parsed as a JSON object."""

    def __init__(self, head: str) -> None:
        super().__init__(f"model did not return a JSON object; reply began: {head[:300]!r}")
        self.head = head


class ResponseTruncated(LLMError):
    """The model hit its token ceiling mid-object."""


@dataclass(frozen=True)
class LLMReply:
    text: str
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    model: str = "unknown"


class Extractor(Protocol):
    """Anything that can turn a system+user prompt into text.

    Narrow on purpose: tests inject a fake in one line, and swapping MiniMax for
    another provider touches only this file.
    """

    def complete(self, *, system: str, user: str, max_tokens: int | None = None) -> LLMReply: ...


# --- JSON recovery ----------------------------------------------------------

_THINK = re.compile(r"<think>(.*?)</think>", re.DOTALL | re.IGNORECASE)
_FENCE_OPEN = re.compile(r"^\s*```(?:json|JSON)?\s*", re.MULTILINE)
_FENCE_CLOSE = re.compile(r"\s*```\s*$", re.MULTILINE)


def split_thinking(text: str) -> tuple[str, str]:
    """Separate a reply into (answer, chain-of-thought).

    The MiniMax M2.x and M3 models put reasoning in ``<think>`` blocks inside the
    *content* field rather than a separate field, so anything reading the reply has
    to strip it. Returned rather than discarded so `--check` can report whether the
    model is spending completion tokens on reasoning — which is what decides
    whether the JSON budget needs raising.
    """
    thinking = " ".join(m.strip() for m in _THINK.findall(text))
    answer = _THINK.sub("", text).strip()
    if not thinking and "<think>" in text:
        # An unterminated block: the reply was cut off mid-thought.
        thinking = text.split("<think>", 1)[1].strip()
        answer = text.split("<think>", 1)[0].strip()
    return answer, thinking


def _outermost_object(text: str) -> str:
    """The first balanced ``{...}`` span, ignoring braces inside strings.

    A brace counter rather than a regex because the payload legitimately contains
    nested objects, and because article text quoted inside it contains braces and
    escaped quotes.
    """
    start = text.find("{")
    if start == -1:
        raise LLMJsonError(text)
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    # Unbalanced: the reply was cut off mid-object.
    raise LLMJsonError(text)


def _repair(text: str) -> str:
    """Fix the malformations these models actually emit."""
    fixed = text.replace("“", '"').replace("”", '"')
    fixed = fixed.replace("‘", "'").replace("’", "'")
    # Trailing commas before a closing bracket or brace.
    fixed = re.sub(r",\s*([}\]])", r"\1", fixed)
    # A reported M2.5 failure: the opening brace of the first object inside an
    # array is dropped, giving `[ "name": ... }` instead of `[ {"name": ... } ]`.
    #
    # Anchored to `[` only, never to `,`. A comma followed by a key is the normal
    # shape of every field inside an object, so including it would insert a brace
    # before every property and destroy well-formed sections of the reply. A `[`
    # followed directly by a key, by contrast, cannot occur in valid JSON at all.
    fixed = re.sub(r'(\[)(\s*)"(\w+)"\s*:', r'\1\2{"\3":', fixed)
    return fixed


def parse_json_object(text: str) -> Any:
    """Extract a JSON object from a reply that may be wrapped in prose.

    Tolerant by necessity: without provider-side schema enforcement, replies
    arrive fenced, prefaced, or with a trailing pleasantry.
    """
    cleaned = _THINK.sub("", text)
    cleaned = _FENCE_OPEN.sub("", cleaned)
    cleaned = _FENCE_CLOSE.sub("", cleaned)

    # Order matters. Brace-scanning a *malformed* document goes wrong: a dropped
    # opening brace makes the depth counter reach zero early, so the scan returns
    # a span that stops short of the real end of the object. Repairing the whole
    # text before scanning is therefore tried as its own candidate, not only after.
    for candidate in _candidates(cleaned):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    raise LLMJsonError(text)


def _candidates(cleaned: str) -> list[str]:
    """Progressively more aggressive readings of a reply, cheapest first."""
    out: list[str] = []

    def add(value: str | None) -> None:
        if value and value not in out:
            out.append(value)

    add(_scan(cleaned))  # well-formed object inside prose
    add(_scan(_repair(cleaned)))  # repair first, then locate
    scanned = _scan(cleaned)
    if scanned:
        add(_repair(scanned))  # span was right, contents needed fixing
    return out


def _scan(text: str) -> str | None:
    try:
        return _outermost_object(text)
    except LLMJsonError:
        return None


# --- MiniMax ----------------------------------------------------------------


class MiniMaxExtractor:
    """OpenAI-compatible chat completions against MiniMax.

    The key check lives in ``__init__``, deliberately not at module import: the
    test suite must be importable and runnable on a machine with no key.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        if not self.settings.has_api_key():
            raise MissingApiKey(KEY_HELP)
        self.base_url = self.settings.minimax_base_url.rstrip("/")
        self.model = self.settings.minimax_model

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    def complete(self, *, system: str, user: str, max_tokens: int | None = None) -> LLMReply:
        payload = {
            "model": self.model,
            "messages": [
                # "system", never "developer": MiniMax rejects the latter (2013).
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.1,
            "top_p": 0.9,
            # max_completion_tokens, not max_tokens.
            "max_completion_tokens": max_tokens or self.settings.max_completion_tokens,
            "stream": False,
            # No response_format on purpose: M2.x/M3 ignore it silently, so
            # sending it would imply a guarantee that does not exist.
        }
        data = self._post(payload)
        try:
            choice = data["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"unexpected response shape from {self.endpoint}: {data!r}") from exc

        usage = data.get("usage") or {}
        return LLMReply(
            text=content or "",
            finish_reason=choice.get("finish_reason"),
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            model=data.get("model") or self.model,
        )

    def _post(self, payload: dict[str, Any], *, attempts: int = 3) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self.settings.minimax_api_key.get_secret_value()}",
            "Content-Type": "application/json",
        }
        last: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = httpx.post(
                    self.endpoint,
                    json=payload,
                    headers=headers,
                    timeout=httpx.Timeout(120.0, connect=10.0),
                )
            except httpx.RequestError as exc:
                last = exc
                log.warning("LLM request error (attempt %d/%d): %s", attempt, attempts, exc)
            else:
                if response.status_code in RETRYABLE_STATUS and attempt < attempts:
                    delay = _retry_after(response) or min(2**attempt, 30)
                    log.warning(
                        "LLM HTTP %d, retrying in %.1fs (attempt %d/%d)",
                        response.status_code,
                        delay,
                        attempt,
                        attempts,
                    )
                    time.sleep(delay)
                    continue
                if response.status_code == 401:
                    raise MissingApiKey(
                        "MiniMax rejected the key (HTTP 401).\n\n"
                        f"Base URL in use: {self.base_url}\n"
                        "The most common cause is a key from the other MiniMax "
                        "platform: global keys work only against api.minimax.io "
                        "and China keys only against api.minimaxi.com.\n\n" + KEY_HELP
                    )
                if response.status_code >= 400:
                    raise LLMError(
                        f"MiniMax returned HTTP {response.status_code}: {response.text[:500]}"
                    )
                return response.json()
            if attempt < attempts:
                time.sleep(min(2**attempt, 30))
        raise LLMError(f"LLM request failed after {attempts} attempts: {last}")

    def check(self) -> dict[str, Any]:
        """Cheap round-trip, for `tracker ingest crawl --check`.

        Worth having on day one: it distinguishes "wrong region host" from "bad
        key" from "no network" before an operator spends a run's worth of fetches.
        """
        started = time.monotonic()
        # Not a tiny budget: the M2.x/M3 models emit chain-of-thought inside
        # <think> blocks in the *content* field, so a 16-token cap is spent
        # entirely on reasoning and the check reports a truncated thought instead
        # of the answer. 512 is enough to see a real reply and still costs almost
        # nothing.
        reply = self.complete(system="Reply with the single word OK.", user="ping", max_tokens=512)
        answer, thinking = split_thinking(reply.text)
        return {
            "base_url": self.base_url,
            "model": reply.model,
            "latency_s": round(time.monotonic() - started, 2),
            "reply": (answer or "(empty)")[:80],
            "finish_reason": reply.finish_reason,
            "thinking_tokens": "yes" if thinking else "no",
            "prompt_tokens": reply.prompt_tokens,
            "completion_tokens": reply.completion_tokens,
        }


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return min(float(raw), 60.0)
    except ValueError:
        return None


def default_extractor(settings: Settings | None = None) -> Extractor:
    return MiniMaxExtractor(settings)


__all__ = [
    "KEY_HELP",
    "RETRYABLE_STATUS",
    "Extractor",
    "LLMError",
    "LLMJsonError",
    "LLMReply",
    "MiniMaxExtractor",
    "MissingApiKey",
    "ResponseTruncated",
    "default_extractor",
    "parse_json_object",
    "split_thinking",
]
