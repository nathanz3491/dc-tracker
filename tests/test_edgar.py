"""The SEC company list, and the two ways it can silently do nothing.

`ingest edgar`'s failure mode is not an exception, it is zero hits. An unpadded
CIK returns nothing and does not complain; a class of filer searched with the
wrong phrases returns nothing worth reading. Both look exactly like "this source
had no data", which is the most expensive wrong conclusion available here.
"""

from __future__ import annotations

import datetime as dt
import logging

import httpx
import pytest

from tracker.ingest import edgar


def test_the_shipped_company_list_loads():
    companies, phrases, forms = edgar.load_companies()
    assert companies and phrases and forms
    assert {"hyperscaler", "neocloud", "landlord", "utility", "contractor"} <= {
        c.kind for c in companies
    }


def test_every_shipped_cik_is_ten_digits():
    """An unpadded CIK returns zero hits and no error.

    Measured in the file's own header: `1326801` finds nothing where
    `0001326801` finds 105. A typo here removes a company from the run and
    reports success, so the loader rejects it and this proves the shipped list
    passes.
    """
    for company in edgar.load_companies()[0]:
        assert company.cik.isdigit() and len(company.cik) == 10, company.name


def test_a_short_cik_is_refused_rather_than_searched(tmp_path):
    path = tmp_path / "companies.toml"
    path.write_text('[[company]]\nname = "Meta"\ncik = "1326801"\n', encoding="utf-8")
    with pytest.raises(edgar.EdgarError, match="10 digits"):
        edgar.load_companies(path)


def test_each_class_is_asked_its_own_question():
    """A utility does not write "build-to-suit" and a contractor does not either.

    Both write about data centers, so the shared phrases are not wrong — they
    just do not ask for the thing each class uniquely knows. A utility discloses
    an interconnection agreement and a large load; an E&C contractor discloses
    backlog, which leads energisation. Adding a class without adding its
    vocabulary is how a new source looks like it contributed nothing.
    """
    companies, shared, _forms = edgar.load_companies()
    by_kind = {c.kind: c for c in companies}

    utility = by_kind["utility"].phrases
    assert utility and utility != tuple(shared)
    assert any("interconnection" in p for p in utility)
    assert any("large load" in p for p in utility)

    contractor = by_kind["contractor"].phrases
    assert contractor and any("backlog" in p for p in contractor)

    # A kind with no entry keeps the shared list, rather than searching nothing.
    assert by_kind["hyperscaler"].phrases == ()


def test_prepare_actually_searches_the_per_kind_phrases(tmp_path, monkeypatch):
    """Loading them is not using them.

    The first version of this file asserted only that `Company.phrases` was
    populated, and a change making `prepare` fall back to the shared list for
    everybody passed it — the utilities would have been searched with
    "anchor tenant" and quietly returned nothing.
    """
    path = tmp_path / "companies.toml"
    path.write_text(
        '[[company]]\nname = "Dominion"\ncik = "0000715957"\nkind = "utility"\n'
        '[[company]]\nname = "Meta"\ncik = "0001326801"\nkind = "hyperscaler"\n'
        "[search]\n"
        'phrases = ["\\"data center\\""]\n'
        "[search.by_kind]\n"
        'utility = ["\\"large load\\""]\n',
        encoding="utf-8",
    )

    asked: list[tuple[str, str]] = []

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(edgar, "_Client", lambda _settings: FakeClient())
    monkeypatch.setattr(
        edgar,
        "search",
        lambda _client, company, phrase, _forms, **_kw: asked.append((company.name, phrase)) or [],
    )

    edgar.prepare(companies_path=path, cache_dir=tmp_path / "cache")
    assert ("Dominion", '"large load"') in asked
    assert ("Dominion", '"data center"') not in asked, "the utility got the shared phrases"
    assert ("Meta", '"data center"') in asked


def test_an_unknown_kind_says_which_kinds_exist():
    """Before any network call, so a typo costs nothing."""
    with pytest.raises(edgar.EdgarError) as exc:
        edgar.prepare(cache_dir=None, kind="powerco")  # type: ignore[arg-type]
    message = str(exc.value)
    assert "no companies of kind" in message
    assert "utility" in message and "contractor" in message


def test_a_utility_is_a_source_and_never_a_buyer():
    """Adding utilities and contractors must not move anybody's capacity.

    `capex.attribute` treats an operator as its own customer when it is an end
    user, and end users are read out of this same file. A power company appearing
    in the list must not therefore start being credited with the capacity it
    merely connects — Southern Company already appears in the database as the
    operator of a project, so this is live rather than hypothetical.
    """
    from tracker.capex import end_user_keys
    from tracker.dedup import company_key

    keys = end_user_keys()
    companies = {c.name: c for c in edgar.load_companies()[0]}
    for name in ("Southern Company", "Dominion Energy", "Quanta Services", "EMCOR Group"):
        assert name in companies, f"{name} missing from the shipped list"
        assert company_key(name) not in keys, f"{name} must not be treated as an end user"

    # And the classes that *are* end users still are.
    assert company_key("Meta") in keys
    assert company_key("CoreWeave") in keys


# --- Per-company forms: the foreign private issuers -------------------------


class _RecordingClient:
    """Captures the query parameters `search` would have sent."""

    def __init__(self) -> None:
        self.params: dict = {}

    def get(self, _url: str, **params):
        self.params = params

        class _Response:
            status_code = 200

            @staticmethod
            def json():
                return {"hits": {"hits": []}}

        return _Response()


def _company(**kwargs):
    defaults = {"name": "Somebody", "cik": "0001513845", "kind": "neocloud"}
    return edgar.Company(**{**defaults, **kwargs})


def test_a_companys_own_forms_replace_the_shared_list():
    """A foreign private issuer files 20-F and 6-K and never a 10-K.

    Asking it for the shared list is not a reduced yield, it is zero: Nebius was on
    this list from the start and contributed no filings at all, which is half the
    reason the database held no Nebius projects.
    """
    client = _RecordingClient()
    edgar.search(client, _company(forms=("20-F", "6-K")), '"data center"', ["10-K", "10-Q"])
    assert client.params["forms"] == "20-F,6-K"


def test_a_company_without_its_own_forms_uses_the_shared_list():
    client = _RecordingClient()
    edgar.search(client, _company(), '"data center"', ["10-K", "10-Q", "8-K"])
    assert client.params["forms"] == "10-K,10-Q,8-K"


def test_per_company_forms_are_read_from_the_file(tmp_path):
    path = tmp_path / "companies.toml"
    path.write_text(
        '[[company]]\nname = "Nebius"\ncik = "0001513845"\nkind = "neocloud"\n'
        'forms = ["20-F", "6-K"]\n'
        '[[company]]\nname = "Meta"\ncik = "0001326801"\nkind = "hyperscaler"\n',
        encoding="utf-8",
    )
    companies = {c.name: c for c in edgar.load_companies(path)[0]}
    assert companies["Nebius"].forms == ("20-F", "6-K")
    assert companies["Meta"].forms == (), "empty means 'use the shared list'"


def test_forms_must_be_a_list(tmp_path):
    path = tmp_path / "companies.toml"
    path.write_text(
        '[[company]]\nname = "Nebius"\ncik = "0001513845"\nkind = "neocloud"\nforms = "20-F"\n',
        encoding="utf-8",
    )
    with pytest.raises(edgar.EdgarError, match="list"):
        edgar.load_companies(path)


def test_the_shipped_list_asks_nebius_for_the_forms_it_actually_files():
    companies = {c.name: c for c in edgar.load_companies()[0]}
    assert companies["Nebius"].forms == ("20-F", "6-K")


# --- Filing dates: SEC says when it was filed, the page never does ----------
#
# 172 of the 173 citations of sec.gov were undated on 2026-10-26. A filing is an
# inline-XBRL document with no `datePublished`, no `<meta>` and no `<time>`, so the
# fetcher finds nothing in it and an undated citation is ranked as if it had been
# published the day it was read. SEC's submissions record has the filing date.

CIK = 1326801
ACCESSION = "0001628280-26-050705"
FILING = f"https://www.sec.gov/Archives/edgar/data/{CIK}/000162828026050705"
PAGE = f"{FILING}/meta-20260630.htm"


def submissions(recent: dict[str, str], *, files: list[dict] | None = None) -> httpx.Response:
    """SEC's answer for one company: its newest filings, and where the older ones are."""
    return httpx.Response(
        200,
        json={
            "cik": "0001326801",
            "filings": {
                "recent": {"accessionNumber": list(recent), "filingDate": list(recent.values())},
                "files": files or [],
            },
        },
    )


def older(filings: dict[str, str]) -> httpx.Response:
    """One of the pages SEC keeps the rest of a company's filings on."""
    return httpx.Response(
        200, json={"accessionNumber": list(filings), "filingDate": list(filings.values())}
    )


def record_url(cik: int) -> str:
    return f"{edgar.SUBMISSIONS}/CIK{cik:010d}.json"


def older_page(cik: int, n: int, first: str, last: str) -> dict:
    """An entry of `filings.files`: a page of older filings, and the span it covers."""
    return {
        "name": f"CIK{cik:010d}-submissions-{n:03d}.json",
        "filingFrom": first,
        "filingTo": last,
    }


def older_url(cik: int, n: int) -> str:
    return f"{edgar.SUBMISSIONS}/CIK{cik:010d}-submissions-{n:03d}.json"


def meta() -> httpx.Response:
    """Meta's record. Its 10-Q was accepted at 6:58 pm Eastern on 2026-07-29, after
    the day's cut-off, so SEC dates it the 30th: the date used is SEC's, not the
    acceptance time's."""
    return submissions({ACCESSION: "2026-07-30", "0001326801-26-000012": "2026-04-30"})


def page_of(cik: int, accession: str = "000162828026050705") -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/x.htm"


class FakeSEC:
    """SEC's submissions API from a dict of URL to answer, counting what is asked."""

    def __init__(self, routes: dict[str, httpx.Response | Exception]) -> None:
        self.routes = routes
        self.urls: list[str] = []
        self.closed = False

    def get(self, url: str, **_params) -> httpx.Response:
        self.urls.append(url)
        answer = self.routes.get(url, httpx.Response(404))
        if isinstance(answer, Exception):
            raise answer
        return answer

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize(
    "url",
    [
        # The primary document, an exhibit, and an XBRL viewer page.
        PAGE,
        f"{FILING}/meta-20260630xex99d1.htm",
        f"{FILING}/R8.htm",
        # A tracking parameter on the end, as one cited address carries.
        f"{PAGE}?ref=wheresyoured.at",
        # The filing's own pages: its index, in both spellings, and its folder.
        f"{FILING}/{ACCESSION}-index.htm",
        f"{FILING}/{ACCESSION}-index.html",
        f"{FILING}/",
        FILING,
        # The whole submission as one file, beside the folder rather than in it.
        f"https://www.sec.gov/Archives/edgar/data/{CIK}/{ACCESSION}.txt",
        f"https://www.sec.gov/Archives/edgar/data/{CIK}/{ACCESSION}-index.htm",
        # The inline viewer, wrapping an archive address.
        f"https://www.sec.gov/ix?doc=/Archives/edgar/data/{CIK}/000162828026050705/meta.htm",
        f"https://www.sec.gov/ixviewer/ix.html?doc=/Archives/edgar/data/{CIK}/000162828026050705/m.htm",
        # Another spelling of the host and the path.
        PAGE.replace("https://www.", "http://"),
        PAGE.replace("https://www.sec.gov/Archives", "https://WWW.SEC.GOV/archives"),
    ],
)
def test_every_page_of_a_filing_names_the_filing(url):
    assert edgar.parse_filing_url(url) == edgar.FilingRef(CIK, ACCESSION)


def test_the_cik_is_the_one_in_the_url_and_the_accession_the_one_in_the_folder():
    """Digital Realty's 10-Q is cited under the REIT's CIK and its operating
    partnership's: one accession, two companies, and the filer agent's number is in
    the accession's first ten digits, not the CIK's."""
    ref = edgar.parse_filing_url(
        "https://www.sec.gov/Archives/edgar/data/1494877/000155837025009991/R13.htm"
    )
    assert ref == edgar.FilingRef(1494877, "0001558370-25-009991")


@pytest.mark.parametrize(
    "url",
    [
        "https://www.sec.gov/Archives/x",
        # A company's directory lists filings; it is not one.
        f"https://www.sec.gov/Archives/edgar/data/{CIK}/",
        f"https://www.sec.gov/ix?doc=/Archives/edgar/data/{CIK}/",
        "https://www.sec.gov/news/press-release/2026-12",
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=1326801",
        # SEC's own API is not a page of a filing either.
        "https://data.sec.gov/submissions/CIK0001326801.json",
        # Not SEC, however much the path looks like it.
        PAGE.replace("www.sec.gov", "example.com"),
        PAGE.replace("www.sec.gov", "notsec.gov.example"),
        # A folder that is not an accession number, and a CIK that is not one.
        f"https://www.sec.gov/Archives/edgar/data/{CIK}/00016282802605070/x.htm",
        "https://www.sec.gov/Archives/edgar/data/0/000162828026050705/x.htm",
        "",
        "not a url",
        "http://[::1",
    ],
)
def test_an_address_that_is_not_a_page_of_a_filing_names_none(url):
    assert edgar.parse_filing_url(url) is None


def test_a_filing_page_is_dated_by_the_day_sec_filed_it():
    sec = FakeSEC({record_url(CIK): meta()})
    when = edgar.FilingDates(client=sec).of(PAGE)
    assert when == dt.datetime(2026, 7, 30), "the day at midnight, naive, like every other date"
    assert sec.urls == [f"{edgar.SUBMISSIONS}/CIK0001326801.json"], (
        "the CIK is padded to ten digits"
    )


def test_the_filings_index_page_is_dated_the_same_way():
    sec = FakeSEC({record_url(CIK): meta()})
    asking = edgar.FilingDates(client=sec)
    assert asking.of(f"{FILING}/{ACCESSION}-index.htm") == dt.datetime(2026, 7, 30)
    whole = f"https://www.sec.gov/Archives/edgar/data/{CIK}/{ACCESSION}.txt"
    assert asking.of(whole) == dt.datetime(2026, 7, 30)


def test_a_company_is_asked_about_once_however_many_of_its_pages_are_cited():
    """172 citations were pages of 90 filings by 24 companies: the cost of dating them
    is the 24, not the 172."""
    sec = FakeSEC({record_url(CIK): meta()})
    asking = edgar.FilingDates(client=sec)
    got = [asking.of(url) for url in (PAGE, f"{FILING}/R8.htm", f"{FILING}/{ACCESSION}-index.htm")]
    got.append(asking.of(page_of(CIK, "000132680126000012")))
    assert got == [dt.datetime(2026, 7, 30)] * 3 + [dt.datetime(2026, 4, 30)]
    assert len(sec.urls) == 1


def test_an_older_filing_is_looked_for_only_on_the_pages_that_cover_its_year():
    """A company's newest filings come with it; the rest sit on pages that each say
    what span of dates they hold. Digital Realty's 2005 exhibit is on the oldest page,
    and the page between is never asked for."""
    files = [
        older_page(1297996, 1, "2017-05-17", "2024-06-19"),
        older_page(1297996, 2, "2005-05-06", "2017-05-16"),
    ]
    sec = FakeSEC(
        {
            record_url(1297996): submissions({"0001193125-26-288761": "2026-10-01"}, files=files),
            older_url(1297996, 2): older({"0001193125-05-121824": "2005-06-02"}),
        }
    )
    asking = edgar.FilingDates(client=sec)
    exhibit = "https://www.sec.gov/Archives/edgar/data/1297996/000119312505121824/dex992.htm"
    assert asking.of(exhibit) == dt.datetime(2005, 6, 2)
    assert sec.urls == [record_url(1297996), older_url(1297996, 2)]
    assert asking.of(exhibit) == dt.datetime(2005, 6, 2)
    assert len(sec.urls) == 2, "what was read is remembered"


def test_a_filing_sec_does_not_list_is_not_dated_and_is_not_asked_after_twice():
    sec = FakeSEC(
        {
            record_url(CIK): submissions(
                {}, files=[older_page(CIK, 1, "2017-05-17", "2024-06-19")]
            ),
            older_url(CIK, 1): older({"0001326801-18-000001": "2018-01-02"}),
        }
    )
    asking = edgar.FilingDates(client=sec)
    unlisted = page_of(CIK, "000132680118000099")
    assert asking.of(unlisted) is None
    assert asking.of(unlisted) is None
    assert len(sec.urls) == 2, "the company, and the one page that could hold 2018 — once each"


def test_a_company_with_no_older_pages_costs_one_request_to_say_a_filing_is_unknown():
    sec = FakeSEC({record_url(CIK): meta()})
    assert edgar.FilingDates(client=sec).of(page_of(CIK, "000162828026099999")) is None
    assert len(sec.urls) == 1


def test_a_company_sec_has_no_record_of_is_an_answer_and_not_a_failure():
    """A 404 is SEC saying it knows no such company — a mistyped CIK in a cited
    address. It must not count towards giving up, or four such pages would silence
    the run's dates for every real filing after them."""
    sec = FakeSEC({record_url(CIK): meta()})
    asking = edgar.FilingDates(client=sec)
    unknown = [page_of(cik) for cik in (11, 12, 13, 14)]
    assert [asking.of(url) for url in unknown] == [None] * 4
    assert asking.of(unknown[0]) is None
    assert len(sec.urls) == 4, "each unknown company is asked about once"
    assert asking.of(PAGE) == dt.datetime(2026, 7, 30)


@pytest.mark.parametrize(
    "answer",
    [
        httpx.ConnectError("no route to host"),
        httpx.ReadTimeout("slow"),
        OSError("connection reset"),
        httpx.Response(503),
        httpx.Response(403, text="Undeclared automated tool"),
        httpx.Response(429),
        httpx.Response(200, text="<html>Request Rate Threshold Exceeded</html>"),
        httpx.Response(200, json=["not", "an", "object"]),
    ],
    ids=["connect", "timeout", "reset", "503", "403", "429", "html", "list"],
)
def test_a_lookup_that_fails_leaves_the_date_empty_and_raises_nothing(answer, caplog):
    """A date is metadata about a page that was read; failing to find one must not
    fail the read."""
    sec = FakeSEC({record_url(CIK): answer})
    with caplog.at_level(logging.WARNING, logger="tracker.ingest.edgar"):
        assert edgar.FilingDates(client=sec).of(PAGE) is None
    assert any("SEC filing dates" in record.getMessage() for record in caplog.records)


def test_three_failures_in_a_row_end_the_asking():
    """A block or a rate limit answers every later request the same way; sending one a
    page into it is how a run gets its address refused for the night."""
    sec = FakeSEC({record_url(cik): httpx.Response(503) for cik in range(1, 7)})
    asking = edgar.FilingDates(client=sec)
    assert [asking.of(page_of(cik)) for cik in range(1, 7)] == [None] * 6
    assert len(sec.urls) == edgar.FilingDates.GIVE_UP_AFTER


def test_a_success_between_failures_keeps_the_asking_going():
    routes = {
        record_url(cik): httpx.Response(503) if cik % 2 else submissions({ACCESSION: "2026-07-30"})
        for cik in range(1, 7)
    }
    sec = FakeSEC(routes)
    asking = edgar.FilingDates(client=sec)
    got = [asking.of(page_of(cik)) for cik in range(1, 7)]
    assert got == [None, dt.datetime(2026, 7, 30)] * 3
    assert len(sec.urls) == 6


def test_a_placeholder_user_agent_asks_nothing_and_raises_nothing(caplog):
    """SEC blocks the whole run for sending it, so the first question says so and no
    request is made. No client is injected: the suite's network guard would fail this
    test if one went out."""
    with caplog.at_level(logging.WARNING, logger="tracker.ingest.edgar"):
        asking = edgar.FilingDates()
        assert asking.of(PAGE) is None
        assert asking.of(f"{FILING}/R8.htm") is None
    said = [r.getMessage() for r in caplog.records if "not asked for" in r.getMessage()]
    assert len(said) == 1, "once, not once a page"


def test_a_client_it_built_is_closed_and_one_it_was_given_is_not(monkeypatch):
    built = FakeSEC({record_url(CIK): meta()})
    monkeypatch.setattr(edgar, "_Client", lambda _settings: built)
    with edgar.FilingDates() as asking:
        assert asking.of(PAGE) == dt.datetime(2026, 7, 30)
    assert built.closed

    given = FakeSEC({record_url(CIK): meta()})
    with edgar.FilingDates(client=given) as asking:
        assert asking.of(PAGE) == dt.datetime(2026, 7, 30)
    assert not given.closed


@pytest.mark.parametrize(
    "record",
    [
        {},
        {"filings": None},
        {"filings": {"recent": {"accessionNumber": [ACCESSION]}}},
        {"filings": {"recent": {"accessionNumber": ACCESSION, "filingDate": "2026-07-30"}}},
        {"filings": {"recent": {"accessionNumber": [ACCESSION], "filingDate": [None]}}},
        {"filings": {"recent": {"accessionNumber": [ACCESSION], "filingDate": ["yesterday"]}}},
        # Before EDGAR existed, and a day that has not happened: not SEC's.
        {"filings": {"recent": {"accessionNumber": [ACCESSION], "filingDate": ["1850-01-01"]}}},
        {"filings": {"recent": {"accessionNumber": [ACCESSION], "filingDate": ["2999-01-01"]}}},
        {"filings": {"recent": {"accessionNumber": [ACCESSION], "filingDate": []}, "files": "x"}},
    ],
)
def test_a_record_that_does_not_say_leaves_the_date_empty(record):
    sec = FakeSEC({record_url(CIK): httpx.Response(200, json=record)})
    assert edgar.FilingDates(client=sec).of(PAGE) is None


def test_an_older_page_named_other_than_secs_way_is_never_requested():
    """The name goes into a request URL, so it is checked rather than trusted."""
    span = {"filingFrom": "2017-01-01", "filingTo": "2030-01-01"}
    files = [
        {"name": "https://evil.example/x.json", **span},
        {"name": "../CIK0000000001.json", **span},
    ]
    sec = FakeSEC({record_url(CIK): submissions({}, files=files)})
    assert edgar.FilingDates(client=sec).of(PAGE) is None
    assert sec.urls == [record_url(CIK)]


def test_dates_of_answers_only_for_the_filings_sec_dates():
    sec = FakeSEC({record_url(CIK): meta()})
    urls = [
        PAGE,
        page_of(CIK, "000162828026099999"),
        "https://news.example/article",
        "https://www.sec.gov/news/press-release/2026-12",
    ]
    asking = edgar.FilingDates(client=sec)
    assert edgar.dates_of(urls, resolver=asking) == {PAGE: dt.datetime(2026, 7, 30)}
    assert len(sec.urls) == 1


def test_dates_of_makes_no_request_when_nothing_is_a_filing(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("a resolver was made for a list with no filing in it")

    monkeypatch.setattr(edgar, "FilingDates", refuse)
    assert edgar.dates_of(["https://news.example/article"]) == {}
