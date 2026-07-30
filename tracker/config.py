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
    model_config = SettingsConfigDict(
        env_prefix="TRACKER_",
        env_file=".env",
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

    # --- Database ---------------------------------------------------------
    #: Relative paths resolve against the project root, not the CWD.
    db: Path = Path("data/tracker.db")

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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
