"""Runtime settings, from environment or a gitignored `.env`.

pydantic-settings gives real-env-wins-over-dotenv precedence for free. The API
key is a `SecretStr` for a specific reason: Typer and Rich print local variables
in tracebacks, so a plain `str` would leak the key into any crash output an
operator might paste into a bug report.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Marker used to locate the project root by walking up from the CWD, so
#: `tracker list` works from any subdirectory.
_ROOT_MARKER = "pyproject.toml"


def find_project_root(start: Path | None = None) -> Path:
    """Nearest ancestor of the CWD containing pyproject.toml, else the CWD.

    Use this for things that should follow the *user* — where to put a database
    when none is configured. For things that ship with the code, use
    :func:`install_root`.
    """
    here = (start or Path.cwd()).resolve()
    for candidate in (here, *here.parents):
        if (candidate / _ROOT_MARKER).is_file():
            return candidate
    return here


def install_root() -> Path:
    """The directory containing the `tracker` package.

    Distinct from :func:`find_project_root` on purpose. Repo assets that ship
    with the code — `migrations/`, and the article cache — must be found relative
    to the installed package, not relative to wherever the operator happens to be
    standing. Once the CLI is on PATH, `tracker init` is routinely run from
    another directory, and a CWD-relative lookup sends it hunting for a
    `migrations/` folder in the home directory.
    """
    return Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    # Two .env locations, and the order matters. pydantic-settings resolves a
    # relative env_file against the CURRENT DIRECTORY, so a bare ".env" is
    # invisible the moment `tracker` is run from anywhere but the project root —
    # which is the normal case now that the CLI is on PATH. The project's own
    # .env is therefore given as an absolute path.
    #
    # A .env in the current directory is still read, and listed second so it
    # wins: that makes a per-directory override possible without editing the
    # project's file.
    model_config = SettingsConfigDict(
        env_prefix="TRACKER_",
        env_file=(install_root() / ".env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- DeepSeek ---------------------------------------------------------
    # One platform, one host, OpenAI-compatible. Replaced MiniMax, whose two
    # platforms had non-interchangeable keys, no structured output, and a
    # separate no-think model to work around.
    deepseek_api_key: SecretStr | None = None
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"

    #: Model used for *reasoning* rather than extraction — `tracker infer`.
    #:
    #: Still a separate setting, but on DeepSeek the two jobs no longer need two
    #: *models*: `deepseek-v4-flash` and `deepseek-v4-pro` are one family, and the
    #: depth dial is the `thinking` parameter, not the model name (see
    #: :data:`deepseek_reasoning_effort` and `tracker.llm`). The setting is kept so
    #: an operator who wants the heavier model for judgement calls can say
    #: `TRACKER_DEEPSEEK_REASONING_MODEL=deepseek-v4-pro` without touching the
    #: high-volume extraction path, which is where the token bill is.
    deepseek_reasoning_model: str = "deepseek-v4-flash"

    #: How hard the reasoning tier thinks: "low" | "high" | "max".
    #:
    #: Only consulted for the reasoning extractor. Extraction and the drawer
    #: briefing run with thinking *disabled*, which is a request-time flag rather
    #: than a model choice — see `tracker.llm.DeepSeekExtractor`.
    deepseek_reasoning_effort: str = "high"

    #: Model for the drawer's written briefing — the one call a person waits for.
    #:
    #: A third setting, because this job's constraint is neither volume nor depth
    #: but *latency*: the panel generates when a row is opened, so the model's
    #: speed is the page's speed.
    #:
    #: **This setting used to carry the workaround; DeepSeek carries it natively.**
    #: On MiniMax the only way to stop a model spending the first ten to forty
    #: seconds inside an invisible `<think>` block was to pick the one model that
    #: could not think at all (`M2-her`, 2.7s to first visible word against 12-47s
    #: for every other model in the roster) — and to accept that a dialogue model
    #: got the data wrong: on Fairwater it wrote "All tracks complete" over a
    #: construction track that had reached nothing, and named a utility and a permit
    #: process that appear nowhere in the data. `thinking`, `reasoning_effort` and
    #: `enable_thinking` were all accepted by MiniMax and all silently ignored, so
    #: there was no other lever.
    #:
    #: DeepSeek honours `thinking: {"type": "disabled"}`, so the fast path is now
    #: the *same* model as everything else with reasoning switched off at request
    #: time, and the accuracy trade is gone. Kept as a setting because the fast
    #: path's model is still a legitimate thing to want to change independently.
    #:
    #: Two things outside this setting still bound the reply, and both still earn
    #: their place: the prompt asks for an `[[END]]` sentinel and `overview.RUNAWAY`
    #: cuts the stream there.
    deepseek_fast_model: str = "deepseek-v4-flash"

    #: Send `response_format={"type": "json_object"}` on the JSON-returning calls.
    #:
    #: Off by default, and the default is the recommendation. DeepSeek's own docs
    #: attach two conditions to JSON mode: the prompt must contain the word `json`,
    #: and the endpoint "has a probability of returning empty content". This
    #: codebase already enforces the JSON contract in `tracker.llm` by parse →
    #: repair → validate → retry, which recovers from prose-wrapped JSON but cannot
    #: recover from an empty reply. Turning this on trades a failure we handle for
    #: one we do not, so it stays a measured experiment rather than a default.
    deepseek_json_mode: bool = False

    # --- Web search ------------------------------------------------------------
    # Which backend `tracker search` and `tracker enrich` use: "auto" picks the
    # first one that has a key, in the order google, brave, serper.
    #
    # Every option is an official API. Scraping result pages is deliberately not
    # offered: it breaks the search engines' terms, gets blocked in practice, and
    # would contradict this project's decision not to defeat other sites' access
    # controls either.
    #
    # There is no "bing" option. Microsoft **retired** the standalone Bing Search
    # APIs on 2025-08-11 (their own docs page carries `is_retired: true`), so no
    # new subscription key can be created. Its successor, Grounding with Bing
    # Search in Azure AI Foundry, is licensed for grounding a model's reply rather
    # than for building a database of stored facts and citations, which is exactly
    # what this tool does. Brave is the closest drop-in: an independent index, a
    # free tier, one header, no cloud account.
    search_provider: str = "auto"

    # Google Programmable Search — the official Custom Search JSON API. Two values,
    # both free:
    #   key  https://developers.google.com/custom-search/v1/introduction
    #   cx   https://programmablesearchengine.google.com  (set to search the
    #        whole web, not a fixed site list)
    # Free tier is 100 queries/day, roughly 1000 candidate URLs.
    google_api_key: SecretStr | None = None
    google_cse_id: str | None = None

    # Brave Search — independent index, one key, no cloud account.
    #   https://api-dashboard.search.brave.com  (free tier: 2000 queries/month)
    brave_api_key: SecretStr | None = None

    # Serper — Google results over a simple JSON API, 2500 free credits.
    #   https://serper.dev
    serper_api_key: SecretStr | None = None

    # Bocha (博查) — registers from mainland China with no Cloudflare challenge,
    # which is the whole reason it is here. Its index is Chinese-web-heavy and
    # thin on US trade press at article depth; see BochaProvider for the measured
    # detail before relying on it for citations.
    #   https://open.bochaai.com
    bocha_api_key: SecretStr | None = None

    #: Results requested per query. Google caps a single call at 10; Brave allows
    #: 20 but 10 keeps every backend comparable and the quota predictable.
    search_results_per_query: int = Field(default=10, ge=1, le=10)
    #: Queries per run, so a bad query set cannot exhaust the daily quota.
    search_max_queries: int = Field(default=10, ge=1, le=100)

    # --- Database ---------------------------------------------------------
    #: Relative paths resolve against the project root, not the CWD.
    db: Path = Path("data/tracker.db")

    # --- Console -----------------------------------------------------------
    #: Password for `tracker serve`. Unset means no gate, which is right for a
    #: loopback console — reaching it already means having the machine.
    #:
    #: `SecretStr`, and read from the environment rather than taken as a CLI flag,
    #: for two different reasons. Typer and Rich print local variables in
    #: tracebacks, so a plain `str` would leak it into any crash output pasted
    #: into a bug report; and a `--password` flag would land in shell history and
    #: in `ps` output for every user on the machine.
    #:
    #: Publishing the console (a tunnel, a reverse proxy) without setting this is
    #: refused — see `tracker serve --tunnel`.
    console_password: SecretStr | None = None

    #: A named cloudflared tunnel to publish through, and the hostname routed to
    #: it. Set both or neither.
    #:
    #: Configuration rather than flags retyped every time, because these describe
    #: the machine rather than the run: the tunnel's credentials are already in
    #: the home directory and the DNS record is already in the zone. Once a
    #: hostname is permanent, `tracker cloudflare` should need no arguments.
    #:
    #: Neither is a secret — a hostname is public by definition — but they belong
    #: in `.env` rather than in the repo, because a committed value would have
    #: every checkout trying to publish to one person's domain.
    tunnel_name: str | None = None
    tunnel_hostname: str | None = None

    #: Facility power per H200-equivalent accelerator, in kilowatts, used to
    #: restate a site's megawatts as compute. See `tracker/compute.py` for how
    #: 1.3 is arrived at — 700 W board, 1.06 kW per GPU of node-level IT load
    #: from the DGX H200's 8.5 kW, times a 1.2 PUE.
    #:
    #: A setting because every input to it ages: boards get denser, PUE improves,
    #: and a campus energised in 2028 will not be full of H200s. The stored column
    #: is recomputed from this rather than remembered, so changing it re-bases the
    #: whole table without a migration.
    kw_per_h200: float = Field(default=1.3, gt=0)

    # --- Fetching ---------------------------------------------------------
    fetch_concurrency: int = Field(default=4, ge=1, le=32)
    politeness_delay_s: float = Field(default=1.0, ge=0.0)
    fetch_timeout_s: float = Field(default=30.0, gt=0)
    user_agent: str = "dc-tracker/0.1 (+contact: set-me@example.com)"
    #: Exponential-backoff base for a retryable fetch failure. Set to 0 to disable
    #: waiting entirely, which is what the test suite does — otherwise a single
    #: simulated timeout costs several seconds of real sleep.
    retry_backoff_base_s: float = Field(default=1.0, ge=0.0)
    retry_backoff_max_s: float = Field(default=30.0, ge=0.0)

    # --- LLM cost bounds --------------------------------------------------
    #: Input truncation, in characters of markdown (~4 chars/token).
    max_input_chars: int = Field(default=24_000, ge=1_000)
    max_completion_tokens: int = Field(default=4096, ge=256)
    #: Hard cap on LLM calls per URL: one attempt plus one corrective retry.
    llm_max_attempts: int = Field(default=2, ge=1, le=5)

    # --- merge policy -----------------------------------------------------
    #: Break a merge tie on when the article was *published* rather than on when
    #: the crawler fetched it (`source.published_at`, migration 0014).
    #:
    #: Off by default because it moves stored values, and it has to be measured
    #: before it is trusted. `scripts/measure_extraction.py` reports what it would
    #: change; on the live database that is six values, and they are not uniformly
    #: improvements — Hyperion correctly stops holding Meta's superseded $10B, but
    #: #116 would move from 120 MW to 40 MW because the smaller figure was
    #: published a day later. Publication order is the more *defensible* rule, not
    #: the one that always yields the larger number, and turning it on is a
    #: judgement somebody should make with the report in front of them.
    merge_by_publication_date: bool = False

    def resolve_db(self, override: Path | None = None) -> Path:
        """Absolute DB path. Precedence: --db > TRACKER_DB > data/tracker.db."""
        chosen = override or self.db
        return chosen if chosen.is_absolute() else (find_project_root() / chosen).resolve()

    def has_api_key(self) -> bool:
        return bool(self.deepseek_api_key and self.deepseek_api_key.get_secret_value().strip())

    def has_google_keys(self) -> bool:
        return bool(
            self.google_api_key
            and self.google_api_key.get_secret_value().strip()
            and self.google_cse_id
            and self.google_cse_id.strip()
        )

    def has_brave_key(self) -> bool:
        return bool(self.brave_api_key and self.brave_api_key.get_secret_value().strip())

    def has_serper_key(self) -> bool:
        return bool(self.serper_api_key and self.serper_api_key.get_secret_value().strip())

    def has_bocha_key(self) -> bool:
        return bool(self.bocha_api_key and self.bocha_api_key.get_secret_value().strip())

    def resolve_search_provider(self) -> str | None:
        """Which backend to use, or None when nothing is configured.

        "auto" takes the first backend holding a key. An explicit name is honoured
        even without a key, so the provider itself can raise the error that names
        the missing variable — a silent fallback to a different engine would be
        worse than a clear failure.
        """
        name = (self.search_provider or "auto").strip().lower()
        if name != "auto":
            return name
        if self.has_google_keys():
            return "google"
        if self.has_brave_key():
            return "brave"
        if self.has_serper_key():
            return "serper"
        if self.has_bocha_key():
            return "bocha"
        return None

    def has_search_keys(self) -> bool:
        """True when some search backend is usable."""
        return bool(self.resolve_search_provider())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
