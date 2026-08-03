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

    # --- MiniMax ----------------------------------------------------------
    # Global and China are separate platforms whose keys are NOT interchangeable;
    # the wrong host returns "invalid api key". See .env.example.
    minimax_api_key: SecretStr | None = None
    minimax_base_url: str = "https://api.minimax.io/v1"
    minimax_model: str = "MiniMax-M2.5"

    #: Model used for *reasoning* rather than extraction — `tracker infer`.
    #:
    #: A separate setting because the two jobs want different models. Extraction is
    #: a high-volume transcription task where a fast model is right; inferring what
    #: is obstructing a project is one call per project and wants the strongest
    #: reasoning available. Verified present on both MiniMax platforms.
    minimax_reasoning_model: str = "MiniMax-M3"

    #: Model for the drawer's written briefing — the one call a person waits for.
    #:
    #: A third setting, because this job's constraint is neither volume nor depth
    #: but *latency*: the panel generates when a row is opened, so the model's
    #: speed is the page's speed. `M2-her` is the only MiniMax model that does not
    #: emit a `<think>` block, and on this prompt that is the whole race.
    #:
    #: Time to the first *visible* word, measured on this prompt. Tokens spent
    #: inside `<think>` are invisible to a reader, so a model that streams
    #: instantly and then deliberates is not fast:
    #:
    #:     MiniMax-M3               46.6s, and returned nothing at all
    #:     MiniMax-M2.5-highspeed   17.9s
    #:     MiniMax-M2.7             16.0s
    #:     MiniMax-M2.7-highspeed   15.5s
    #:     MiniMax-M2.1-highspeed   12.5s
    #:     MiniMax-M2               12.4s
    #:     M2-her                    2.7s   <- this, and it does not think
    #:
    #: Note that plain `MiniMax-M2` beats every `-highspeed` variant. "Highspeed"
    #: is output tokens per second, and this job emits ~70 words after a fixed slab
    #: of reasoning, so throughput is the one thing that barely matters. Only
    #: removing the reasoning changes the number, and only `M2-her` does that.
    #:
    #: Three things make `M2-her` usable, and all three live outside this setting:
    #: the prompt asks for an `[[END]]` sentinel (the API's own `stop` parameter is
    #: accepted and ignored), `overview.RUNAWAY` cuts the stream there, and
    #: `MODEL_TOKEN_CAP` clamps the budget to the 2048 it accepts. Without them it
    #: writes 756-982 words against a 110-word instruction, repeats itself under
    #: "Final answer (last round)", and narrates its own word count. With them:
    #: 65 words on average, nothing leaking.
    #:
    #: **Known cost of this choice.** `M2-her` is built for dialogue, and it
    #: sometimes gets the data wrong in a way `MiniMax-M2` did not. On Fairwater —
    #: construction track `nothing reached`, the other four passed — it wrote "All
    #: tracks complete; construction the last to finish", inverting the most
    #: informative field in the row. It has also named a utility and a permit
    #: process that appear nowhere in the data ("WEPCO", "Wind chill plant
    #: licensing is pending") and written phrases that mean nothing ("Major capex
    #: is confirmed via gas"). The behaviour is variable: four consecutive runs on
    #: that same row came back clean.
    #:
    #: This is a deliberate trade, made with the failure measured rather than
    #: assumed — the panel is labelled as a model's reading, is never stored, and
    #: cannot move confidence, so a wrong briefing is a wrong *opinion* beside
    #: correct cited values rather than a wrong value. `TRACKER_MINIMAX_FAST_MODEL`
    #: = `MiniMax-M2` buys the accuracy back for about ten seconds a row.
    #:
    #: Thinking cannot be switched off on the others: `thinking`,
    #: `reasoning_effort` and `enable_thinking` are all accepted by the API and all
    #: ignored, and an assistant prefill of `</think>` does not suppress it.
    #: Shrinking the prompt does not help either — measured with the provenance
    #: quotes stripped, 1300 fewer characters moved the first word by less than the
    #: run-to-run noise.
    minimax_fast_model: str = "M2-her"

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

    def resolve_db(self, override: Path | None = None) -> Path:
        """Absolute DB path. Precedence: --db > TRACKER_DB > data/tracker.db."""
        chosen = override or self.db
        return chosen if chosen.is_absolute() else (find_project_root() / chosen).resolve()

    def has_api_key(self) -> bool:
        return bool(self.minimax_api_key and self.minimax_api_key.get_secret_value().strip())

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
