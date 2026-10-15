"""The reader documentation: what it serves, to whom, and on which address.

The properties, in the order a mistake would cost:

* it is public, but only the docs are: on the docs hostname nothing of the
  console answers, and on the console the sign-in still guards everything else;
* the files it serves are the manifest's, compared exactly, so nothing dropped into
  the folder later — or reached with `..` — becomes public;
* every page exists in every language, so the manifest and the folder agree;
* `/help`, the old Help view, lands on the docs.
"""

from __future__ import annotations

import json
import re
import threading
from http.client import HTTPConnection

import pytest

from tracker.webui import docsite
from tracker.webui.server import Console, Handler

DOCS_HOST = "docs.console.example"
CONSOLE_HOST = "console.example"


def _get(address, path, host=None, cookie=None):
    conn = HTTPConnection(*address, timeout=30)
    headers = {}
    if host:
        headers["Host"] = host
    if cookie:
        headers["Cookie"] = cookie
    conn.request("GET", path, headers=headers)
    response = conn.getresponse()
    body = response.read().decode("utf-8", "replace")
    result = (response.status, dict(response.getheaders()), body)
    conn.close()
    return result


@pytest.fixture
def gated(tmp_path, migrated_copy):
    """A console with an account, so the sign-in is closed, and a docs hostname."""
    from http.server import ThreadingHTTPServer

    from tracker import accounts
    from tracker.db import open_db, session_scope

    db = migrated_copy(tmp_path / "tracker.db")
    with session_scope(open_db(db, readonly=False)) as session:
        accounts.create(session, "reader@example.com", "a-long-password")
    console = Console(db, docs_host=DOCS_HOST, console_host=CONSOLE_HOST)
    handler = type("Bound", (Handler,), {"console": console})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield httpd.server_address
    finally:
        httpd.shutdown()
        httpd.server_close()
        console.close()


# --- the content ------------------------------------------------------------------


def test_every_page_is_written_in_every_language():
    site = docsite.site()
    on_disk = {
        path.relative_to(docsite.DOCS_ROOT).as_posix()
        for path in docsite.DOCS_ROOT.rglob("*")
        if path.is_file()
    }
    assert on_disk == set(site.files), "a page the manifest does not list, or the reverse"


def test_no_page_is_a_stub_and_each_opens_with_its_title():
    for page in docsite.site().pages:
        for lang in docsite.LANGUAGES:
            text = (docsite.DOCS_ROOT / lang / f"{page.slug}.md").read_text(encoding="utf-8")
            assert text.startswith("# "), f"{lang}/{page.slug} does not open with its title"
            assert "TODO" not in text, f"{lang}/{page.slug} is unfinished"
            assert len(text) > 600, f"{lang}/{page.slug} is too short to be a page"


def test_links_between_pages_name_real_pages():
    """A relative link is a slug in the same language; a broken one is a dead end."""
    slugs = docsite.site().slugs
    link = re.compile(r"\]\((?!https?:|mailto:|#)([^)#\s]+)(#[^)\s]*)?\)")
    for page in docsite.site().pages:
        for lang in docsite.LANGUAGES:
            text = (docsite.DOCS_ROOT / lang / f"{page.slug}.md").read_text(encoding="utf-8")
            for target, _anchor in link.findall(text):
                name = target.removesuffix(".md").split("/")[-1]
                assert name in slugs, f"{lang}/{page.slug} links to {target!r}"


def test_the_docs_name_no_machine():
    """CLAUDE.md §6: nothing committed names the production host.

    The forbidden words are read from the guard command in CLAUDE.md, the one file
    allowed to name them, so this test does not become a file that does.
    """
    from pathlib import Path

    rules = (Path(__file__).resolve().parents[1] / "CLAUDE.md").read_text(encoding="utf-8")
    forbidden = re.search(r"grep -lE '([^']+)'", rules)
    assert forbidden, "CLAUDE.md no longer carries its guard command"
    for path in docsite.DOCS_ROOT.rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            assert not re.search(forbidden[1], text, re.I), path


def test_a_manifest_naming_a_missing_page_is_refused(tmp_path):
    (tmp_path / "en").mkdir()
    (tmp_path / "zh").mkdir()
    (tmp_path / "en" / "a.md").write_text("# A", encoding="utf-8")
    manifest = {
        "sections": [
            {
                "id": "s",
                "title": {"en": "S", "zh": "S"},
                "pages": [{"slug": "a", "title": {"en": "A", "zh": "A"}}],
            }
        ]
    }
    with pytest.raises(docsite.ManifestError, match=r"zh/a.md"):
        docsite.parse(manifest, tmp_path)
    (tmp_path / "zh" / "a.md").write_text("# A", encoding="utf-8")
    assert docsite.parse(manifest, tmp_path).files == {"manifest.json", "en/a.md", "zh/a.md"}
    manifest["sections"][0]["pages"][0]["slug"] = "../a"
    with pytest.raises(docsite.ManifestError, match="not a slug"):
        docsite.parse(manifest, tmp_path)


@pytest.mark.parametrize("raw", ["docs.example.com", "DOCS.Example.com.", " docs.example.com "])
def test_a_docs_hostname_is_normalised(raw):
    assert docsite.hostname(raw) == "docs.example.com"


@pytest.mark.parametrize("raw", ["docs.example.com/x", "https://docs.example.com", 'a"b.com', "x"])
def test_a_malformed_docs_hostname_is_refused(raw):
    with pytest.raises(ValueError):
        docsite.hostname(raw)


def test_no_docs_hostname_is_none():
    assert docsite.hostname("") is None and docsite.hostname(None) is None


# --- on the console's own address --------------------------------------------------


def test_the_docs_are_public_under_docs_on_a_gated_console(gated):
    first = docsite.site().pages[0].slug
    for path in ("/docs/", f"/docs/en/{first}", f"/docs/zh/{first}", "/docs/en/"):
        status, headers, body = _get(gated, path)
        assert status == 200, path
        assert '<meta name="dc-base" content="/docs/">' in body
        assert "Content-Security-Policy" in headers
    status, headers, _ = _get(gated, "/docs")
    assert status == 303 and headers["Location"] == "/docs/"
    # And the console beside them is still closed.
    assert _get(gated, "/api/dataset")[0] == 401
    assert _get(gated, "/static/app.js")[0] == 401


def test_a_page_that_is_not_there_is_a_404_with_the_shell(gated):
    status, _, body = _get(gated, "/docs/en/no-such-page")
    assert status == 404 and '<meta name="dc-base" content="/docs/">' in body
    assert _get(gated, "/docs/fr/introduction")[0] == 404


def test_content_is_the_manifests_files_and_nothing_else(gated):
    status, headers, body = _get(gated, "/docs/content/manifest.json")
    assert status == 200 and headers["Content-Type"].startswith("application/json")
    assert json.loads(body)["sections"]
    page = docsite.site().pages[0].slug
    status, headers, body = _get(gated, f"/docs/content/en/{page}.md")
    assert status == 200 and headers["Content-Type"].startswith("text/markdown")
    assert body.startswith("# ")
    for path in (
        "/docs/content/en/unlisted.md",
        "/docs/content/../docsite.py",
        "/docs/content/%2e%2e/docsite.py",
        "/docs/content/en/../manifest.json",
        "/docs/content/EN/" + page + ".md",
    ):
        assert _get(gated, path)[0] == 404, path


def test_the_console_link_is_the_console_itself_under_docs(gated):
    _, _, body = _get(gated, "/docs/")
    assert '<meta name="dc-console" content="/">' in body


def test_help_goes_to_the_docs_signed_out(gated):
    status, headers, _ = _get(gated, "/help")
    assert status == 303 and headers["Location"] == f"https://{DOCS_HOST}/"


def test_help_goes_under_docs_with_no_docs_hostname(tmp_path, migrated_copy):
    from http.server import ThreadingHTTPServer

    console = Console(migrated_copy(tmp_path / "tracker.db"))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), type("B", (Handler,), {"console": console}))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        status, headers, _ = _get(httpd.server_address, "/help")
        assert status == 303 and headers["Location"] == "/docs/"
    finally:
        httpd.shutdown()
        httpd.server_close()
        console.close()


# --- on the docs hostname ------------------------------------------------------------


def test_the_docs_hostname_serves_the_docs_at_its_root(gated):
    status, _, body = _get(gated, "/", host=DOCS_HOST)
    assert status == 200 and '<meta name="dc-base" content="/">' in body
    assert "Who is building what" not in body, "the console's front page, not the docs"
    assert f'<meta name="dc-console" content="https://{CONSOLE_HOST}/">' in body
    page = docsite.site().pages[0].slug
    assert _get(gated, f"/en/{page}", host=DOCS_HOST)[0] == 200
    assert _get(gated, f"/content/en/{page}.md", host=f"{DOCS_HOST}:443")[0] == 200
    assert _get(gated, "/static/docs/docs.js", host=DOCS_HOST)[0] == 200


def test_nothing_of_the_console_answers_on_the_docs_hostname(gated):
    """Not the data, not the console's code, not its sign-in: the docs host is docs."""
    for path in (
        "/api/dataset",
        "/api/health",
        "/api/v1/health",
        "/static/app.js",
        "/signin",
        "/projects",
        "/docs/",
    ):
        status, _, body = _get(gated, path, host=DOCS_HOST)
        assert status == 404, path
        assert "sign in first" not in body
