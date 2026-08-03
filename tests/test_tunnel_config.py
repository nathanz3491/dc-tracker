"""Which tunnel `tracker cloudflare` publishes through, and where that is decided.

Once a hostname is permanent the command should need no arguments, but the
defaulting has to be exact: printing a configured hostname next to a different
tunnel would hand somebody a URL that does not point at what is running.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from tracker.cli import app
from tracker.config import Settings

runner = CliRunner()


@pytest.fixture
def configured(monkeypatch):
    """A machine with a named tunnel set up, as `.env` would supply it."""
    settings = Settings(tunnel_name="dc-console", tunnel_hostname="mastri.app")
    for module in ("tracker.cli", "tracker.config"):
        monkeypatch.setattr(f"{module}.get_settings", lambda: settings, raising=False)
    return settings


def _plan(monkeypatch) -> dict:
    """Capture what would be published, instead of publishing it."""
    seen: dict = {}

    def fake_run_console(**kwargs):
        seen.update(kwargs)

    monkeypatch.setattr("tracker.cli._run_console", fake_run_console)
    monkeypatch.setattr("tracker.webui.tunnel.find_cloudflared", lambda: "cloudflared")
    return seen


def test_a_configured_tunnel_needs_no_arguments(configured, monkeypatch):
    seen = _plan(monkeypatch)
    assert runner.invoke(app, ["cloudflare"]).exit_code == 0
    assert seen["publish"] == "named"
    assert seen["tunnel_name"] == "dc-console"
    assert seen["hostname"] == "mastri.app"


def test_quick_ignores_the_configured_tunnel(configured, monkeypatch):
    """Still reachable once a permanent one exists — a throwaway URL has uses."""
    seen = _plan(monkeypatch)
    assert runner.invoke(app, ["cloudflare", "--quick"]).exit_code == 0
    assert seen["publish"] == "quick"
    assert seen["tunnel_name"] is None
    assert seen["hostname"] is None


def test_quick_and_a_named_tunnel_together_are_refused(configured, monkeypatch):
    _plan(monkeypatch)
    result = runner.invoke(app, ["cloudflare", "--quick", "--name", "other"])
    assert result.exit_code != 0


def test_an_explicit_name_does_not_inherit_the_configured_hostname(configured, monkeypatch):
    """The load-bearing one.

    Filling `mastri.app` in behind `--name staging` would print a URL pointing at
    the *other* tunnel — and it would look right, because the hostname is real.
    The pair is taken together or not at all.
    """
    seen = _plan(monkeypatch)
    assert runner.invoke(app, ["cloudflare", "--name", "staging"]).exit_code == 0
    assert seen["tunnel_name"] == "staging"
    assert seen["hostname"] is None


def test_flags_override_both(configured, monkeypatch):
    seen = _plan(monkeypatch)
    result = runner.invoke(
        app, ["cloudflare", "--name", "staging", "--hostname", "staging.example.com"]
    )
    assert result.exit_code == 0
    assert seen["tunnel_name"] == "staging"
    assert seen["hostname"] == "staging.example.com"


def test_with_nothing_configured_it_is_still_a_quick_tunnel(monkeypatch):
    """The behaviour every checkout without a domain keeps."""
    settings = Settings()
    monkeypatch.setattr("tracker.cli.get_settings", lambda: settings)
    seen = _plan(monkeypatch)
    assert runner.invoke(app, ["cloudflare"]).exit_code == 0
    assert seen["publish"] == "quick"
    assert seen["tunnel_name"] is None


def test_a_hostname_without_a_tunnel_name_is_refused(monkeypatch):
    """Half-configured is a mistake worth naming rather than quietly ignoring."""
    settings = Settings(tunnel_hostname="mastri.app")
    monkeypatch.setattr("tracker.cli.get_settings", lambda: settings)
    _plan(monkeypatch)
    result = runner.invoke(app, ["cloudflare"])
    assert result.exit_code != 0
    assert "TRACKER_TUNNEL_NAME" in result.output


def test_serve_tunnel_uses_the_same_configured_tunnel(configured, monkeypatch):
    """`serve --tunnel` used to be hard-wired to a quick tunnel.

    Leaving it that way would mean the two ways of publishing land on different
    URLs, which is exactly the surprise a permanent hostname was meant to remove.
    """
    seen = _plan(monkeypatch)
    assert runner.invoke(app, ["serve", "--tunnel"]).exit_code == 0
    assert seen["publish"] == "named"
    assert seen["tunnel_name"] == "dc-console"
    assert seen["hostname"] == "mastri.app"


def test_serve_without_tunnel_publishes_nothing(configured, monkeypatch):
    seen = _plan(monkeypatch)
    assert runner.invoke(app, ["serve"]).exit_code == 0
    assert seen["publish"] is None
    assert seen["tunnel_name"] is None
