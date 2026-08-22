"""The SEC company list, and the two ways it can silently do nothing.

`ingest edgar`'s failure mode is not an exception, it is zero hits. An unpadded
CIK returns nothing and does not complain; a class of filer searched with the
wrong phrases returns nothing worth reading. Both look exactly like "this source
had no data", which is the most expensive wrong conclusion available here.
"""

from __future__ import annotations

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
