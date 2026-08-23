"""LLM access behind a protocol: DeepSeek over the API, or Ollama on this machine.

Two implementations of one `Extractor` protocol, chosen by
:data:`Settings.llm_provider` or per run with `--llm-provider`. Everything downstream —
the JSON contract, the thinking filter, the tier policy in the factories — is
provider-independent, which is the property that makes the switch a setting
rather than a fork.

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

No key at all? A local model works instead of the API: `--llm-provider ollama` on any
command that spends LLM calls, against an Ollama server. See docs/ingesting.md.
"""

#: HTTP statuses worth retrying.
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


#: The providers `--llm` and TRACKER_LLM_PROVIDER accept.
LLM_PROVIDERS = ("deepseek", "ollama")


class LLMUnavailable(RuntimeError):
    """The configured provider cannot answer; the message says how to fix it.

    The parent every call site catches. Before Ollama existed here the only way a
    provider failed *before spending anything* was a missing key, so twenty call
    sites caught :class:`MissingApiKey` by name. A local server adds a second way
    — not running, model not pulled — and teaching each site both names would
    guarantee the next provider misses one. They all catch this instead.
    """


class MissingApiKey(LLMUnavailable):
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
        effort: str | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        if not self.settings.has_api_key():
            raise MissingApiKey(KEY_HELP)
        self.base_url = self.settings.deepseek_base_url.rstrip("/")
        # `model` overrides the configured extraction model. On DeepSeek the
        # tiers differ by reasoning rather than by model name, but the override
        # stays: an operator can point one tier at `deepseek-v4-pro` without moving
        # the high-volume path onto it too.
        self.model = model or self.settings.deepseek_model
        #: Reasoning effort, or None for no reasoning at all. ONE field rather than
        #: a `thinking` flag beside it, because the two are not independent: an
        #: effort is meaningless with reasoning off, and reasoning on without one
        #: is a state the API has no way to express. Collapsing them means the
        #: invalid combination cannot be constructed. The three factories below are
        #: where the actual per-tier policy lives.
        self.effort = effort

    @property
    def thinking(self) -> bool:
        """Whether this tier reasons. Derived, so it cannot disagree with `effort`."""
        return self.effort is not None

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
                {"type": "enabled", "reasoning_effort": self.effort}
                if self.effort is not None
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


OLLAMA_HELP = """the local LLM server did not answer at {base_url}.

  Is it running?     The Ollama app starts it; headless, `ollama serve`.
  Is the model there?  `ollama list` should show {model}; if not:
    ollama pull {model}
  Serving from another machine?  Point this tool at it:
    TRACKER_OLLAMA_BASE_URL=http://that-host:11434

This run asked for the local provider (`--llm-provider ollama`, or
TRACKER_LLM_PROVIDER=ollama in the environment or .env). The API provider
still works in the meantime: pass `--llm-provider deepseek`.
"""


class OllamaUnavailable(LLMUnavailable):
    """The Ollama server did not answer, or does not have the model."""


class OllamaExtractor:
    """Chat against a local Ollama server, through its native ``/api/chat``.

    Native rather than Ollama's OpenAI-compatible shim, for two properties the
    shim does not expose. ``options.num_ctx`` is settable per request — Ollama's
    default context is a few thousand tokens and input beyond it is TRUNCATED
    SILENTLY, which against this codebase's article-sized prompts would mean
    extractions quietly reading half the article and the evidence gate rejecting
    quotes that were never seen. And ``think`` is a first-class flag, so the
    no-think tier is a request parameter here exactly as it is on DeepSeek.

    The reachability probe lives in ``__init__`` for the same reason DeepSeek's
    key check does: every caller wants to fail before polling feeds or fetching
    pages, not after. One GET against ``/api/version``, about a millisecond when
    the server is local and a fast refusal when it is not.

    **Every request here sets ``trust_env=False``, and it was measured, not
    guessed.** httpx with its default honours not only ``HTTP_PROXY`` but the
    operating system's proxy configuration — `urllib.request.getproxies()` reads
    macOS's SystemConfiguration — so on a machine running a system-wide proxy,
    requests to ``127.0.0.1:11434`` were routed to the proxy at 127.0.0.1:7897,
    which answered 502 for a loopback destination it cannot reach. curl worked
    and httpx failed on the same URL, with ``os.environ`` showing no proxy at
    all. Local inference must never transit a proxy; the API provider keeps the
    default, because reaching the API may be exactly what the proxy is for.

    The JSON contract stays in code here too — parse, repair, validate, one
    corrective retry — rather than on Ollama's ``format: json`` mode, for the
    same reason it is not on DeepSeek's: a provider-side mode that constrains
    decoding can starve a thinking model into emptiness, and the tolerant reader
    recovers from prose-wrapped JSON but not from nothing.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        model: str | None = None,
        effort: str | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.base_url = self.settings.ollama_base_url.rstrip("/")
        self.model = model or self.settings.ollama_model
        #: Same field the DeepSeek extractor carries, collapsed to a boolean at
        #: request time: qwen-class models think or do not, there is no dial. Kept
        #: as the string so the factories apply ONE tier policy to both providers.
        self.effort = effort
        try:
            httpx.get(
                f"{self.base_url}/api/version", timeout=5.0, trust_env=False
            ).raise_for_status()
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            probe = f"(probe failed: {exc})"
            raise OllamaUnavailable(f"{self._help()}{probe}") from exc

    def _help(self) -> str:
        return OLLAMA_HELP.format(base_url=self.base_url, model=self.model)

    @property
    def thinking(self) -> bool:
        """Whether this tier reasons. Derived, so it cannot disagree with `effort`."""
        return self.effort is not None

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/api/chat"

    def _payload(self, *, system: str, user: str, max_tokens: int | None, stream: bool) -> dict:
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": stream,
            "think": self.thinking,
            "options": {
                # The same sampling the DeepSeek payload asks for, so switching
                # provider does not also switch temperament.
                "temperature": 0.1,
                "top_p": 0.9,
                "num_predict": max_tokens or self.settings.max_completion_tokens,
                # The silent-truncation guard this class exists for.
                "num_ctx": self.settings.ollama_num_ctx,
            },
        }

    def complete(self, *, system: str, user: str, max_tokens: int | None = None) -> LLMReply:
        data = self._post(
            self._payload(system=system, user=user, max_tokens=max_tokens, stream=False)
        )
        message = data.get("message")
        if not isinstance(message, dict):
            raise LLMError(f"unexpected response shape from {self.endpoint}: {data!r}")

        # Ollama returns thinking in its own field when `think` is on. Folded back
        # into the tag form the rest of this module already understands, exactly as
        # DeepSeek's `reasoning_content` is, and for the same reason: one place
        # knows about provider shapes, and `split_thinking` keeps working.
        text = message.get("content") or ""
        reasoning = message.get("thinking")
        if reasoning:
            text = f"{_THINK_OPEN}{reasoning}{_THINK_CLOSE}{text}"

        return LLMReply(
            text=text,
            finish_reason=data.get("done_reason"),
            prompt_tokens=data.get("prompt_eval_count"),
            completion_tokens=data.get("eval_count"),
            model=data.get("model") or self.model,
        )

    def stream(self, *, system: str, user: str, max_tokens: int | None = None) -> Iterator[str]:
        """Yield the answer as it is generated. NDJSON, not SSE.

        Same non-retry contract as the DeepSeek stream: once the first token has
        reached a reader, a replay would restart the paragraph mid-sentence.
        """
        payload = self._payload(system=system, user=user, max_tokens=max_tokens, stream=True)
        filter_ = _ThinkFilter()
        try:
            with httpx.stream(
                "POST",
                self.endpoint,
                json=payload,
                timeout=httpx.Timeout(self.settings.ollama_timeout_s, connect=10.0),
                trust_env=False,
            ) as response:
                if response.status_code >= 400:
                    response.read()
                    raise LLMError(
                        f"Ollama returned HTTP {response.status_code}: {response.text[:500]}"
                    )
                for line in response.iter_lines():
                    if not line.strip():
                        continue
                    try:
                        chunk = json.loads(line)
                    except ValueError:
                        continue
                    piece = (chunk.get("message") or {}).get("content")
                    if piece:
                        yield from filter_.feed(piece)
        except httpx.RequestError as exc:
            raise LLMError(f"LLM stream failed: {exc}") from exc
        yield from filter_.finish()

    def _post(self, payload: dict[str, Any], *, attempts: int = 3) -> dict[str, Any]:
        """POST with the same retry discipline as the API provider.

        A local server earns fewer excuses than a remote one, but model loading is
        real: the first request after an idle period can 503 while 18 GB maps in,
        and retrying through that is strictly better than failing an ingest on it.
        """
        last: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = httpx.post(
                    self.endpoint,
                    json=payload,
                    timeout=httpx.Timeout(self.settings.ollama_timeout_s, connect=10.0),
                    trust_env=False,
                )
            except httpx.RequestError as exc:
                last = exc
                log.warning("Ollama request error (attempt %d/%d): %s", attempt, attempts, exc)
            else:
                if response.status_code in RETRYABLE_STATUS and attempt < attempts:
                    delay = _retry_after(response) or min(2**attempt, 30)
                    log.warning(
                        "Ollama HTTP %d, retrying in %.1fs (attempt %d/%d)",
                        response.status_code,
                        delay,
                        attempt,
                        attempts,
                    )
                    time.sleep(delay)
                    continue
                if response.status_code == 404:
                    # The one status with an unambiguous meaning here: the server is
                    # up and the model is not on it.
                    detail = (
                        f"(the server answered, but not for {self.model!r}: {response.text[:200]})"
                    )
                    raise OllamaUnavailable(f"{self._help()}{detail}")
                if response.status_code >= 400:
                    raise LLMError(
                        f"Ollama returned HTTP {response.status_code}: {response.text[:500]}"
                    )
                return response.json()
            if attempt < attempts:
                time.sleep(min(2**attempt, 30))
        tail = f"(last error: {last})"
        raise OllamaUnavailable(f"{self._help()}{tail}")

    def check(self) -> dict[str, Any]:
        """Cheap round-trip, shaped exactly like the DeepSeek one.

        Same keys on purpose: `tracker ingest crawl --check` prints whatever this
        returns, and the operator comparing providers should be reading one table.
        """
        started = time.monotonic()
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


def build_extractor(
    settings: Settings | None = None,
    *,
    model: str | None = None,
    effort: str | None = None,
) -> Extractor:
    """The configured provider, with no tier policy applied.

    `--llm-provider` and :data:`Settings.llm_provider` decide which class this returns;
    `model` and `effort` pass through to it unchanged, so `effort=None` still
    means "no thinking" on both. Every call site that used to name
    `DeepSeekExtractor` directly goes through here instead — choosing a provider
    has to be one setting, not twenty constructor sites.

    The three factories below stay the tier policy; this is only the switch.
    """
    settings = settings or get_settings()
    if settings.llm_provider == "ollama":
        return OllamaExtractor(settings, model=model, effort=effort)
    return DeepSeekExtractor(settings, model=model, effort=effort)


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

    Runs at `high`, not `max`, and that is the one place the two reasoning tiers
    part company: this is the high-volume path, so effort here multiplies by the
    size of the corpus. `high` buys the judgement the job needs without paying
    `max` several thousand times over.

    Costs completion tokens. See `Settings.max_completion_tokens`, which was raised
    to leave room for it, and `crawl.py`'s starvation retry for what happens when
    there still is not enough.
    """
    settings = settings or get_settings()
    return build_extractor(settings, effort=settings.deepseek_extraction_effort)


def reasoning_extractor(settings: Settings | None = None) -> Extractor:
    """The tier for judgement rather than transcription. See `tracker.infer`.

    ONE call per project, against a whole row, asking for the conclusion the
    database cannot look up: which obstacle actually binds, and what would show the
    project still moving. The most reasoning-shaped question in the tool and the
    cheapest place to pay for depth — one call per project rather than one per
    article — so it runs at `max` while extraction runs at `high`.
    """
    settings = settings or get_settings()
    if settings.llm_provider == "ollama":
        # One local model plays every tier — there is one installed — so the
        # DeepSeek-specific reasoning-model name must not leak through here. The
        # effort still does: on Ollama it collapses to think-or-not.
        return OllamaExtractor(settings, effort=settings.deepseek_infer_effort)
    return DeepSeekExtractor(
        settings,
        model=settings.deepseek_reasoning_model,
        effort=settings.deepseek_infer_effort,
    )


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
    if settings.llm_provider == "ollama":
        # No effort means no thinking, on both providers: this is the tier a
        # person sits and waits for, and a local model at tens of tokens a second
        # needs the no-think flag more than the API does, not less.
        return OllamaExtractor(settings)
    return DeepSeekExtractor(settings, model=settings.deepseek_fast_model)


__all__ = [
    "KEY_HELP",
    "LLM_PROVIDERS",
    "OLLAMA_HELP",
    "RETRYABLE_STATUS",
    "DeepSeekExtractor",
    "Extractor",
    "LLMError",
    "LLMJsonError",
    "LLMReply",
    "LLMUnavailable",
    "MissingApiKey",
    "OllamaExtractor",
    "OllamaUnavailable",
    "ResponseTruncated",
    "build_extractor",
    "default_extractor",
    "fast_extractor",
    "parse_json_object",
    "reasoning_extractor",
    "split_thinking",
]
