"""Shared fixtures.

Every DB fixture uses a real file under `tmp_path` rather than `:memory:`.
In-memory SQLite rejects `PRAGMA journal_mode=WAL` and, more importantly, hides
file-level behaviour (the read-only `mode=ro` guard, WAL sibling files) that we
specifically want covered. Each file is a copy of one database migrated once per
run, rather than migrated again per test — see `migrated_template`.
"""

from __future__ import annotations

import errno
import ipaddress
import os
import shutil
import socket
from pathlib import Path

import pytest
from sqlalchemy import Engine

from tracker.config import Settings, get_settings
from tracker.db import init_db, make_engine, session_scope

FIXTURES = Path(__file__).parent / "fixtures"


def _is_loopback(host) -> bool:
    """Whether a name or address stays on this machine. None and "" are a bind."""
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    host = str(host).strip("[]").lower()
    if host in ("", "localhost") or host.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    return address.is_loopback or address.is_unspecified


@pytest.fixture(autouse=True)
def _no_network(request, monkeypatch):
    """Fail any test that reaches past this machine, and refuse the attempt at once.

    **A fresh clone with no network must produce a green run**, and the suite did
    not check it. Two `sync` tests walked every configured sitemap for real — the
    archive sweep was not stubbed in either — and spent 117 s and 106 s doing it,
    over a third of the whole run, while passing: the code under test treats a fetch
    that fails as a fetch that failed, so nothing ever said "this went outside".

    So the refusal is immediate — a DNS lookup or a connection to anything but
    loopback raises the error an unplugged machine would, rather than waiting out
    a timeout — and the test fails at teardown whether or not the code swallowed
    that error. Tests marked `network` or `llm` are exempt; they are deselected by
    default and exist to go outside. Loopback stays open because the console tests
    run a real server on 127.0.0.1.

    **The proxy variables go too, or the guard sees nothing.** A machine that
    reaches the internet through a local proxy — this one: `HTTPS_PROXY` is
    `http://127.0.0.1:8080` — sends httpx's traffic to loopback, and the proxy
    makes the outside connection where no hook here can see it. That is exactly
    how both sweeps got past the first version of this guard. With no proxy the
    same request goes direct and meets the refused lookup, which names the host.
    `NO_PROXY` is set to the loopback names rather than removed or made `*`:
    with no proxy variable at all, urllib and httpx fall back to the system's
    settings (the Windows registry, macOS's network configuration) and the hiding
    place is back; and `*` also cancels a proxy a test passes explicitly, which is
    how the tunnel relay's test watches where the relay goes.

    Covers what goes through Python's `socket` module and asyncio's loops, which is
    everything installed here: httpx sync and async, `http.client`, `smtplib`.
    asyncio needs its own hook because the Windows loop connects with `ConnectEx`,
    never calling `socket.connect`. A subprocess, or a C library with sockets of
    its own (`curl_cffi`), is outside it.
    """
    if request.node.get_closest_marker("network") or request.node.get_closest_marker("llm"):
        yield
        return

    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1,::1")
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1,::1")

    reached: list[str] = []
    real_getaddrinfo = socket.getaddrinfo
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def outside(sock: socket.socket, address) -> bool:
        families = (socket.AF_INET, socket.AF_INET6)
        return sock.family in families and not _is_loopback(address[0])

    def refused(address) -> OSError:
        reached.append(f"connect {address[0]}:{address[1]}")
        return OSError(errno.ENETUNREACH, f"the test suite refused to connect to {address}")

    def getaddrinfo(host, *args, **kwargs):
        if not _is_loopback(host):
            reached.append(f"lookup {host}")
            raise socket.gaierror(socket.EAI_NONAME, f"the test suite refused to resolve {host}")
        return real_getaddrinfo(host, *args, **kwargs)

    def connect(sock, address):
        if outside(sock, address):
            raise refused(address)
        return real_connect(sock, address)

    def connect_ex(sock, address):
        if outside(sock, address):
            refused(address)
            return errno.ENETUNREACH
        return real_connect_ex(sock, address)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)

    import asyncio.proactor_events
    import asyncio.selector_events

    for loop_class in (
        asyncio.selector_events.BaseSelectorEventLoop,
        asyncio.proactor_events.BaseProactorEventLoop,
    ):

        async def sock_connect(loop, sock, address, _real=loop_class.sock_connect):
            if outside(sock, address):
                raise refused(address)
            return await _real(loop, sock, address)

        monkeypatch.setattr(loop_class, "sock_connect", sock_connect)
    yield
    if reached:
        pytest.fail(
            "this test reached for the network, which a fresh clone must not need: "
            + ", ".join(sorted(set(reached)))
            + ". Stub the call, or mark the test `network`.",
            pytrace=False,
        )


@pytest.fixture
def real_home(monkeypatch):
    """The checkout as `home()`, for the few tests *about* where home resolves.

    Every other test gets a temporary home (`_fast_and_keyless_settings`), so that
    no cache it writes lands in the repository for a later run to be served.
    """
    from tracker.config import home

    monkeypatch.delenv("TRACKER_HOME", raising=False)
    home.cache_clear()
    yield
    home.cache_clear()


@pytest.fixture(autouse=True)
def _fast_and_keyless_settings(monkeypatch, tmp_path_factory):
    """Isolate every test from the operator's real environment.

    Three things this guarantees:

    * **No politeness sleep.** `fetch_all` waits between requests to avoid
      hammering a newsroom. Against a fake fetcher that is pure dead time, and it
      dominated the suite's runtime.
    * **No API key**, even if the developer has one exported. Tests that reach the
      LLM must do so through an injected fake, and this makes a missed injection
      fail loudly rather than quietly spending money.
    * **No `.env` leakage** into settings, by any route.
    """
    # Every TRACKER_* variable goes, not a hand-listed few.
    #
    # **Why the list was not enough.** Neutralizing `env_file` below only stops
    # pydantic reading the file; it cannot stop something else having already
    # copied it into `os.environ`, which pydantic always consults. Installing the
    # `[crawl]` extra does exactly that: `import crawl4ai` pulls in a litellm fork
    # that calls `load_dotenv()` at import time, so the developer's whole `.env` —
    # search keys, provider pin, tunnel hostname, and the API key itself — lands
    # in the process environment. Measured: `TRACKER_SERPER_API_KEY` absent before
    # the import and present after it.
    #
    # Four tests then failed, all of them asserting "nothing is configured", and
    # they failed only on a machine that had both the extra and a real `.env`.
    # That is the same class of bug as the colour probes reading the developer's
    # own database: a suite whose result depends on the operator's setup.
    for name in [key for key in os.environ if key.startswith("TRACKER_")]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    monkeypatch.setenv("TRACKER_POLITENESS_DELAY_S", "0")
    monkeypatch.setenv("TRACKER_RETRY_BACKOFF_BASE_S", "0")

    # **One LLM call at a time, unless a test asks otherwise.** Same reasoning as
    # the two lines above: a setting whose whole purpose is to change timing has no
    # business changing it under the suite. `parallel.map_ordered` runs inline at 1,
    # so every existing test takes the path it always took, and the tests that are
    # *about* concurrency set the value themselves.
    #
    # Not merely tidiness. The canned-reply fakes here hand out replies with
    # `replies.pop(0)`, so with several articles in flight which reply lands on
    # which article stops being defined — a test would pass or fail on thread
    # scheduling. The fakes are locked as well (see `FakeLLM`), but a lock cannot
    # make a queue of positional replies order-deterministic; only serialising can.
    monkeypatch.setenv("TRACKER_LLM_CONCURRENCY", "1")
    monkeypatch.setenv("TRACKER_LLM_RETRY_JITTER", "0")

    # Deleting the environment variables is not enough: `.env` is also read, and
    # once a real one exists on the developer's machine the suite silently starts
    # depending on it — a test asserting "no key configured" passed on CI and
    # failed locally. Neutralize the file itself so tests see defaults only.
    monkeypatch.setitem(Settings.model_config, "env_file", None)

    # **A home of its own, so no test writes into the checkout.** `home()` resolves
    # to the repository for an editable install, and every cache — fetched articles,
    # the console's reader pages — lives under it. So a test that cached a page left
    # it in `.cache/` for every later run to be served instead of its own stub,
    # which is a result depending on what ran before. A test that means a particular
    # home still sets `TRACKER_HOME` itself, after this.
    from tracker.config import home

    monkeypatch.setenv("TRACKER_HOME", str(tmp_path_factory.mktemp("home")))
    home.cache_clear()
    # And no browser, so no test's escalation ladder grows a real Chrome, whose
    # own traffic the no-network guard below cannot see. The one test that means
    # to launch it sets the path itself (`tests/test_browser_fetch.py`).
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)

    # And on DeepSeek, not the reserve: the switch is per process, so one test that
    # triggers it would otherwise send every later test's calls to OpenCode Go.
    from tracker import llm

    llm._ON_RESERVE.clear()

    get_settings.cache_clear()
    yield
    llm._ON_RESERVE.clear()
    get_settings.cache_clear()
    home.cache_clear()


@pytest.fixture(scope="session", autouse=True)
def _build_each_cli_once():
    """Build the Click command tree from a Typer app once per run, not per invoke.

    `typer.testing.CliRunner.invoke` calls `get_command(app)` every time, and for
    this CLI that is 49 ms of a 57 ms invocation — rebuilding every command's
    Click objects to run one of them — in a suite that invokes the CLI several
    hundred times. The tree is built from the callbacks the app registered at
    import, so a cached one runs exactly the same functions: a monkeypatched
    module attribute is looked up when the command runs, either way.

    Keyed on the app object and holding it, so an id cannot be reused by a test
    that builds an app of its own. `raising=False` because `_get_command` is
    typer's private name for it: if a release renames it, this stops helping and
    nothing breaks.
    """
    import typer.testing

    real = typer.testing._get_command
    built: dict[int, tuple[object, object]] = {}

    def get_command(app):
        hit = built.get(id(app))
        if hit is None or hit[0] is not app:
            hit = built[id(app)] = (app, real(app))
        return hit[1]

    patch = pytest.MonkeyPatch()
    patch.setattr(typer.testing, "_get_command", get_command, raising=False)
    yield
    patch.undo()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "tracker.db"


@pytest.fixture(scope="session")
def migrated_template(tmp_path_factory) -> Path:
    """One fully migrated, empty database per run, for fixtures to copy.

    **Building a database per test was a third of the suite.** It is every
    migration, each in its own transaction, on a new file — about 100 ms — and
    1,390 setups did it: `engine` 1,086 times, the CLI's `initialized` 169, the
    console's `seeded_db` 106, the TUI's `curated` 29. That was 179 s of a 575 s
    run deriving the same empty schema again. Copying a file takes about a
    millisecond and isolates exactly as well: every test still gets a file of its
    own, and nothing a test writes can reach the template or another test.

    Tests *about* migrating — `test_db.py`, `tracker init` on a new path — build
    from nothing as before, which is what keeps the migrations themselves covered.

    Checkpointed before anything copies it, and asserted to be: a WAL-mode main
    file on its own can be missing committed pages — the mistake `CLAUDE.md` §3 is
    about — and copies of a half-checkpointed template would pass every test that
    did not happen to look for what was missing. Left in WAL rather than switched
    out, because switching each copy back cost its first connection 2.6 ms.
    """
    path = tmp_path_factory.mktemp("migrated") / "tracker.db"
    engine, applied = init_db(path)
    assert applied, "the template is built from nothing"
    with engine.connect() as conn:
        busy, _, _ = conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)").one()
    engine.dispose()
    wal = Path(f"{path}-wal")
    assert busy == 0 and (not wal.exists() or wal.stat().st_size == 0), "not checkpointed"
    return path


@pytest.fixture
def migrated_copy(migrated_template: Path):
    """Put a fully migrated, empty database at a path, and return the path."""

    def copy(path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(migrated_template, path)
        return path

    return copy


@pytest.fixture
def engine(db_path: Path, migrated_copy) -> Engine:
    """A fully migrated, empty database of this test's own. See `migrated_template`.

    `make_engine` rather than `init_db`, which is `make_engine` plus a migration
    pass: the template was built from these same files this run, so the pass
    would only re-read every one of them to find nothing to do — 7 ms a test,
    measured, against 1.3 ms for the copy itself.
    """
    return make_engine(migrated_copy(db_path))


@pytest.fixture
def session(engine: Engine):
    with session_scope(engine) as s:
        yield s


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


#: The password every test account is created with. Long enough to clear
#: `accounts.MIN_PASSWORD_LEN`, and the same everywhere so a test that needs to
#: sign in does not have to invent one.
ACCOUNT_PASSWORD = "correct horse"


@pytest.fixture
def account(session):
    """One account, because a watchlist now needs an owner.

    Most tests that touch `watch` do not care whose list it is — they care about
    the matching rules — so this exists to keep `account_id=account.id` a short
    thing to type rather than four lines of setup per test.
    """
    from tracker import accounts

    return accounts.create(session, "reader@example.com", ACCOUNT_PASSWORD, name="Reader")


@pytest.fixture
def logical_snapshot():
    """Every row of every table, for comparing data rather than bytes.

    Raw file bytes are the wrong instrument for "did this write?": SQLite in WAL
    mode checkpoints the write-ahead log into the main file at times of its own
    choosing, so the file legitimately changes without any data changing. A byte
    comparison then passes or fails depending on when garbage collection closed
    the previous connection.

    A fixture rather than a module-level helper so the CLI tests and the console
    tests share one definition of the guarantee.
    """
    import sqlite3
    from contextlib import closing

    def snapshot(db: Path) -> dict[str, list[tuple]]:
        # `sqlite3.connect` as a context manager commits but does NOT close,
        # which leaks the handle and raises ResourceWarning under pytest.
        with closing(sqlite3.connect(f"file:{Path(db).as_posix()}?mode=ro", uri=True)) as conn:
            tables = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            ]
            return {t: conn.execute(f"SELECT * FROM {t}").fetchall() for t in sorted(tables)}

    return snapshot


@pytest.fixture(autouse=True)
def _clear_the_source_policy_cache():
    """`policy.load` is `lru_cache`d because every filtered URL consults it.

    That makes it a trap: a test that writes a policy file passes on its own and
    fails inside a suite, because a neighbour already cached the empty default.
    Cleared on both sides so neither direction can leak. `crawl.operator_hosts` has
    the identical shape and the identical hazard.
    """
    from tracker import policy

    policy.load.cache_clear()
    yield
    policy.load.cache_clear()


@pytest.fixture(autouse=True)
def _clear_the_overview_cache():
    """The briefing cache is module-global and keyed on (project id, content hash).

    Correct in production — one process, one database, and the hash is what makes
    reuse safe. In a test run it means one test's briefing is served to the next
    test's project #1, because the fixtures build the same row and the hash
    matches. Cleared between tests so a cache hit is never accidental.
    """
    from tracker import overview

    overview._cache.clear()
    yield
    overview._cache.clear()
