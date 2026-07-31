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
