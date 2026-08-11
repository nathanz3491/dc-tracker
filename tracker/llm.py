"""LLM access behind a protocol, with DeepSeek as one implementation.

**The JSON contract is still enforced here, not by the provider.** DeepSeek does
support ``response_format={"type": "json_object"}`` — unlike MiniMax, which
accepted it and silently ignored it — but the docs attach two conditions to it
that make it the wrong foundation for this codebase: the literal word ``json``
must appear in the prompt (ours say ``JSON``, and the requirement is documented
case-sensitively), and the endpoint "has a probability of returning empty
content" in that mode. An extraction run that occasionally returns *nothing* is
worse than one that returns prose-wrapped JSON, because the tolerant reader below
recovers from the second and cannot recover from the first. So the contract stays
where it has always been: parse → repair → validate → one corrective retry, in
code we can test. :data:`Settings.deepseek_json_mode` turns the provider flag on
for anyone who wants to measure it; it is off by default.

Three DeepSeek details worth writing down, because two of them are the opposite
of what this file used to do:

* the parameter is ``max_tokens`` — MiniMax wanted ``max_completion_tokens``;
* thinking is a **request flag**, ``thinking={"type": "enabled"|"disabled"}`` with
  ``reasoning_effort``. It is honoured, which is why there is no longer a separate
  no-think model in the roster (see `Settings.deepseek_fast_model`);
* reasoning may come back either in its own ``reasoning_content`` field or inline
  in ``<think>`` tags depending on the surface. Both are handled — the field is
  read where the API offers it, and :func:`split_thinking` / :class:`_ThinkFilter`
  still strip the tags, so neither shape can leak a model's deliberation into a
  stored value or onto the page.

One platform, one host (``https://api.deepseek.com``), OpenAI-compatible.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from tracker.config import Settings, get_settings

log = logging.getLogger(__name__)

KEY_HELP = """TRACKER_DEEPSEEK_API_KEY is not set.

  Recommended -- add it to the .env file beside pyproject.toml, which is
  gitignored and is read no matter which directory you run `tracker` from:
    TRACKER_DEEPSEEK_API_KEY=your-key

  Or just for this shell:
    PowerShell   $env:TRACKER_DEEPSEEK_API_KEY = 'your-key'
    Git Bash     export TRACKER_DEEPSEEK_API_KEY=your-key

  Note the TRACKER_ prefix: every setting this tool reads carries it.

Keys are issued at platform.deepseek.com and work against the one host,
https://api.deepseek.com. A MiniMax key will NOT work here: this tool moved off
MiniMax, and TRACKER_MINIMAX_* settings are no longer read at all.

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
    """The reply could not be parsed as a JSON object.

    Says *which* failure it was, because the two need opposite responses and the
    generic message sent a real investigation down the wrong path. A reply that
    opens `<think>` and never closes it did not "fail to return JSON" — it never
    got as far as answering, having spent the whole completion budget reasoning.
    Retrying that verbatim reproduces it; the fix is a bigger budget or a shorter
    prompt. Every path that parses JSON raises this, so naming the cause here
    names it in all of them.
    """

    def __init__(self, head: str) -> None:
        self.ran_out_thinking = "<think>" in head.lower() and "</think>" not in head.lower()
        if self.ran_out_thinking:
            message = (
                "model never finished thinking, so it never answered — its reasoning "
                f"used the whole completion budget; reply began: {head[:200]!r}"
            )
        else:
            message = f"model did not return a JSON object; reply began: {head[:300]!r}"
        super().__init__(message)
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

    Narrow on purpose: tests inject a fake in one line, and swapping the provider
    touches only this file. The MiniMax → DeepSeek move proved it — outside this
    module and `config`, the change was a rename.
    """

    def complete(self, *, system: str, user: str, max_tokens: int | None = None) -> LLMReply: ...


class StreamingExtractor(Extractor, Protocol):
    """An extractor that can also hand back the answer as it arrives.

    Separate from `Extractor` because only one caller needs it. Everything that
    parses JSON wants the whole reply before it can do anything, so streaming
    would buy those paths nothing; the drawer's briefing is prose a person reads
    top to bottom, and there the wait is the entire cost.
    """

    def stream(self, *, system: str, user: str, max_tokens: int | None = None) -> Iterator[str]: ...


# --- JSON recovery ----------------------------------------------------------

_THINK = re.compile(r"<think>(.*?)</think>", re.DOTALL | re.IGNORECASE)
_FENCE_OPEN = re.compile(r"^\s*```(?:json|JSON)?\s*", re.MULTILINE)
_FENCE_CLOSE = re.compile(r"\s*```\s*$", re.MULTILINE)


_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"


def _sse_delta(line: str) -> str:
    """The content fragment in one `data:` line of an OpenAI-style stream."""
    if not line.startswith("data:"):
        return ""
    payload = line[len("data:") :].strip()
    if not payload or payload == "[DONE]":
        return ""
    try:
        choices = json.loads(payload).get("choices") or []
        return (choices[0].get("delta") or {}).get("content") or ""
    except (json.JSONDecodeError, AttributeError, IndexError, TypeError, KeyError):
        # A malformed frame is not worth killing a stream over; the reply is prose
        # and one dropped fragment costs a few words, not correctness.
        return ""


def _held_back(text: str, tag: str) -> int:
    """How much of `text`'s tail could still turn out to be the start of `tag`.

    `split_thinking` gets the whole reply and can just regex it. A stream cannot:
    `<think>` arrives as `<th` then `ink>`, and emitting the first half means the
    reader watches an angle bracket appear and then get taken away again.
    """
    for size in range(min(len(text), len(tag) - 1), 0, -1):
        if text[-size:].lower() == tag[:size]:
            return size
    return 0


class _ThinkFilter:
    """Strips `<think>` blocks from a stream, across chunk boundaries."""

    def __init__(self) -> None:
        self._buffer = ""
        self._inside = False

    def feed(self, chunk: str) -> Iterator[str]:
        self._buffer += chunk
        while True:
            if self._inside:
                cut = self._buffer.lower().find(_THINK_CLOSE)
                if cut == -1:
                    keep = _held_back(self._buffer, _THINK_CLOSE)
                    self._buffer = self._buffer[len(self._buffer) - keep :] if keep else ""
                    return
                self._buffer = self._buffer[cut + len(_THINK_CLOSE) :]
                self._inside = False
                continue

            cut = self._buffer.lower().find(_THINK_OPEN)
            if cut != -1:
                head = self._buffer[:cut]
                self._buffer = self._buffer[cut + len(_THINK_OPEN) :]
                self._inside = True
                if head:
                    yield head
                continue

            hold = _held_back(self._buffer, _THINK_OPEN)
            ready = self._buffer[: len(self._buffer) - hold]
            self._buffer = self._buffer[len(self._buffer) - hold :] if hold else ""
            if ready:
                yield ready
            return

    def finish(self) -> Iterator[str]:
        """Flush the tail.

        A reply truncated inside its own reasoning yields nothing, which is the
        honest outcome: there was no answer, only a cut-off thought.
        """
        if not self._inside and self._buffer:
            yield self._buffer
        self._buffer = ""


def split_thinking(text: str) -> tuple[str, str]:
    """Separate a reply into (answer, chain-of-thought).

    Reasoning reaches this function as a ``<think>`` block inside the text either
    because the model emitted it inline or because :meth:`DeepSeekExtractor.complete`
    folded a ``reasoning_content`` field back into that shape. One reader for both.

    Returned rather than discarded so `--check` can report whether the model is
    spending completion tokens on reasoning — which is what decides whether the JSON
    budget needs raising, and, since DeepSeek actually honours `thinking:
    disabled`, whether the flag is taking effect at all.
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


# --- DeepSeek ---------------------------------------------------------------


#: Models that reject the budget the rest accept.
#:
#: Empty against the current roster, and kept anyway. It exists because the budget
#: is chosen for the *task* while the ceiling belongs to the *model*, and nothing
#: that picks a budget should have to know the roster — MiniMax's `M2-her` answered
#: HTTP 400 to anything over 2048, so a caller asking for the ordinary 4096 got
#: nothing at all rather than a shorter reply. The v4 models take 384K, far above
#: anything this tool asks for, so today the clamp is a no-op.
MODEL_TOKEN_CAP: dict[str, int] = {}


class DeepSeekExtractor:
    """OpenAI-compatible chat completions against DeepSeek.

    The key check lives in ``__init__``, deliberately not at module import: the
    test suite must be importable and runnable on a machine with no key.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        model: str | None = None,
        thinking: bool = False,
    ) -> None:
        self.settings = settings or get_settings()
        if not self.settings.has_api_key():
            raise MissingApiKey(KEY_HELP)
        self.base_url = self.settings.deepseek_base_url.rstrip("/")
        # `model` overrides the configured extraction model. On DeepSeek the
        # extraction/reasoning split is mostly `thinking` rather than the model
        # name, but the override stays: an operator can point the reasoning tier at
        # `deepseek-v4-pro` without moving the high-volume path onto it too.
        self.model = model or self.settings.deepseek_model
        #: On for extraction and inference, off for the drawer briefing. The
        #: constructor default is off because a caller that has not thought about
        #: it is usually a cheap one; the three factory functions below are where
        #: the real policy lives.
        self.thinking = thinking

    def _budget(self, max_tokens: int | None) -> int:
        """The completion budget, clamped to what this model will accept."""
        asked = max_tokens or self.settings.max_completion_tokens
        return min(asked, MODEL_TOKEN_CAP.get(self.model, asked))

    def _payload(self, *, system: str, user: str, max_tokens: int | None, stream: bool) -> dict:
        """The request body both `complete` and `stream` send.

        One builder rather than two near-identical literals: the pair drifted under
        MiniMax, and a `thinking` flag set on one path and not the other is exactly
        the kind of difference that shows up as "the drawer is slow" months later.
        """
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                # "system", never "developer": DeepSeek documents system/user/
                # assistant/tool and no developer role.
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.1,
            "top_p": 0.9,
            # max_tokens — MiniMax wanted max_completion_tokens.
            "max_tokens": self._budget(max_tokens),
            "stream": stream,
            "thinking": (
                {"type": "enabled", "reasoning_effort": self.settings.deepseek_reasoning_effort}
                if self.thinking
                else {"type": "disabled"}
            ),
        }
        # Off by default; see `Settings.deepseek_json_mode` for why. Never on the
        # streaming path, which returns prose a person reads, not an object.
        if self.settings.deepseek_json_mode and not stream:
            payload["response_format"] = {"type": "json_object"}
        return payload

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    def complete(self, *, system: str, user: str, max_tokens: int | None = None) -> LLMReply:
        data = self._post(
            self._payload(system=system, user=user, max_tokens=max_tokens, stream=False)
        )
        try:
            choice = data["choices"][0]
            message = choice["message"]
            content = message.get("content")
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"unexpected response shape from {self.endpoint}: {data!r}") from exc

        # DeepSeek may return reasoning in its own field rather than inline in
        # `<think>` tags. Folded back into the text in the tag form the rest of this
        # module already understands, so `split_thinking` keeps working and
        # `--check` can still report whether tokens went to deliberation. Doing it
        # the other way — teaching every caller about a second field — would put
        # the same knowledge in a dozen places.
        reasoning = message.get("reasoning_content")
        text = content or ""
        if reasoning:
            text = f"{_THINK_OPEN}{reasoning}{_THINK_CLOSE}{text}"

        usage = data.get("usage") or {}
        return LLMReply(
            text=text,
            finish_reason=choice.get("finish_reason"),
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            model=data.get("model") or self.model,
        )

    def stream(self, *, system: str, user: str, max_tokens: int | None = None) -> Iterator[str]:
        """Yield the answer as it is generated.

        Deliberately not retried. `_post` can replay a failed request because
        nothing has been shown yet; once the first token has reached the reader,
        a retry would restart the paragraph mid-sentence. A stream that breaks is
        reported as broken and the caller can ask again.

        Reasoning is filtered out *as it arrives*. With `thinking` disabled — which
        is how the drawer calls this — there should be none to filter, but the
        filter stays: it is cheap, and a naive passthrough would type a model's
        private deliberation into the drawer and then have to erase it.
        """
        payload = self._payload(system=system, user=user, max_tokens=max_tokens, stream=True)
        headers = {
            "Authorization": f"Bearer {self.settings.deepseek_api_key.get_secret_value()}",
            "Content-Type": "application/json",
        }
        filter_ = _ThinkFilter()
        try:
            with httpx.stream(
                "POST",
                self.endpoint,
                json=payload,
                headers=headers,
                timeout=httpx.Timeout(120.0, connect=10.0),
            ) as response:
                if response.status_code >= 400:
                    response.read()
                    if response.status_code == 401:
                        raise MissingApiKey(KEY_HELP)
                    raise LLMError(
                        f"DeepSeek returned HTTP {response.status_code}: {response.text[:500]}"
                    )
                for line in response.iter_lines():
                    piece = _sse_delta(line)
                    if piece:
                        yield from filter_.feed(piece)
        except httpx.RequestError as exc:
            raise LLMError(f"LLM stream failed: {exc}") from exc
        yield from filter_.finish()

    def _post(self, payload: dict[str, Any], *, attempts: int = 3) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self.settings.deepseek_api_key.get_secret_value()}",
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
                        "DeepSeek rejected the key (HTTP 401).\n\n"
                        f"Base URL in use: {self.base_url}\n"
                        "The most common cause after the MiniMax migration is a "
                        "leftover MiniMax key in TRACKER_DEEPSEEK_API_KEY: the two "
                        "providers issue their own keys and neither accepts the "
                        "other's.\n\n" + KEY_HELP
                    )
                if response.status_code >= 400:
                    raise LLMError(
                        f"DeepSeek returned HTTP {response.status_code}: {response.text[:500]}"
                    )
                return response.json()
            if attempt < attempts:
                time.sleep(min(2**attempt, 30))
        raise LLMError(f"LLM request failed after {attempts} attempts: {last}")

    def check(self) -> dict[str, Any]:
        """Cheap round-trip, for `tracker ingest crawl --check`.

        Worth having on day one: it distinguishes "bad key" from "wrong host" from
        "no network" before an operator spends a run's worth of fetches. After the
        MiniMax migration it also answers the first question anyone will have —
        `thinking_tokens` reports whether `thinking: disabled` is actually being
        honoured, which MiniMax never did.
        """
        started = time.monotonic()
        # Not a tiny budget. Reasoning is disabled by default so this should answer
        # in a few tokens, but a 16-token cap would report a truncated thought
        # rather than the answer the moment anything is thinking; 512 is enough to
        # see a real reply and still costs almost nothing.
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
    """Extraction, **with reasoning on**.

    Reading an article for twelve fields is not transcription. It is deciding
    which of three megawatt figures is the data center's rather than the utility's,
    which of four dollar figures is this site's rather than the programme's, and
    whether "since breaking ground" describes a building site or a running one. The
    `_industry.txt` block gives the model what it needs to make those calls;
    thinking is what lets it actually make them instead of pattern-matching the
    nearest number.

    The evidence for turning it on is indirect but points one way: the only
    accuracy comparison this project has measured is a no-think model against a
    thinking one on the briefing prompt, and the no-think model inverted a track
    reading and invented a utility. Extraction is the path where a wrong value gets
    *stored*, so it is the last place to economise.

    Costs completion tokens. See `Settings.max_completion_tokens`, which was raised
    to leave room for it, and `crawl.py`'s starvation retry for what happens when
    there still is not enough.
    """
    return DeepSeekExtractor(settings, thinking=True)


def reasoning_extractor(settings: Settings | None = None) -> Extractor:
    """The tier for judgement rather than transcription. See `tracker.infer`.

    One call per project against a whole row, asking for a conclusion the database
    cannot look up. If anything gets to think, this does.
    """
    settings = settings or get_settings()
    return DeepSeekExtractor(settings, model=settings.deepseek_reasoning_model, thinking=True)


def fast_extractor(settings: Settings | None = None) -> Extractor:
    """The tier for the one call a person sits and waits for. **The only one that
    does not think.**

    Used by the drawer's briefing, where latency *is* the feature: the panel
    generates when a row is opened, so the model's speed is the page's speed. This
    is the role `M2-her` played on MiniMax and it is kept unchanged — the briefing
    is a reading of values already on the page, it is labelled as a model's
    opinion, it is never stored, and it cannot move confidence. Nothing here
    reaches the database, so speed is worth more than depth.

    On DeepSeek the same behaviour is a request flag rather than a different and
    less accurate model, which is the one part of the arrangement that improves.
    """
    settings = settings or get_settings()
    return DeepSeekExtractor(settings, model=settings.deepseek_fast_model)


__all__ = [
    "KEY_HELP",
    "RETRYABLE_STATUS",
    "DeepSeekExtractor",
    "Extractor",
    "LLMError",
    "LLMJsonError",
    "LLMReply",
    "MissingApiKey",
    "ResponseTruncated",
    "default_extractor",
    "fast_extractor",
    "parse_json_object",
    "reasoning_extractor",
    "split_thinking",
]
