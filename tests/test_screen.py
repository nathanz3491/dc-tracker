"""Pages no campus story can be in, told by their address and headline.

Every case here is a page from the queue of 2026-10-19 or the reads before it. The
ones that must NOT be set aside matter as much as the ones that must: each rule was
narrowed after it was found to cost a campus.
"""

from __future__ import annotations

import datetime as dt

import pytest

from tracker.ingest import screen
from tracker.ingest.discover import normalize_haystack

PLACES = screen.us_places(["Ashburn", "Loudoun County", "Kuna", "Ada County", "Gallatin"])


def _never(url: str, title: str | None) -> bool:
    return False


# --- files ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://goed.nv.gov/wp-content/uploads/2025/08/Data-Center-2026.pdf",
        "https://h5datacenters.com/slick-denver.PDF",
        "https://cuyahogacms.blob.core.windows.net/docs/datacenterrecommendations.pdf?sfvrsn=7d",
    ],
)
def test_a_pdf_is_set_aside_because_nothing_reads_one(url):
    """The plain client hands a PDF's compressed streams to the model as prose: 384
    were read since August and none produced a citation."""
    assert screen.not_news(url) == "a PDF, which the reader takes no text from"


def test_media_files_and_attachment_pages_are_not_articles():
    assert screen.not_news("https://a.test/wp-content/uploads/2026/01/render.jpg")
    assert screen.not_news("https://a.test/video/site-tour.mp4")
    assert screen.not_news("https://a.test/report.docx")
    assert screen.not_news("https://a.test/?attachment_id=42") == "a WordPress attachment page"
    assert screen.not_news("https://a.test/2020/05/post/attachment/logo-2/")
    assert screen.not_news("https://a.test/news/hyperscale-campus.html") is None


def test_a_translated_copy_of_a_page_is_set_aside_and_the_english_one_kept():
    """Google's data-center site serves every page again in twelve translations."""
    assert screen.not_news("https://datacenters.google/intl/fi_ALL/locations/hamina-finland/")
    assert screen.not_news("https://datacenters.google/intl/zh-TW_ALL/expanding-access/")
    assert screen.not_news("https://datacenters.google/intl/es-419_ALL/locations/") is not None
    assert screen.not_news("https://datacenters.google/intl/en_us/locations/") is None
    assert screen.not_news("https://datacenters.google/locations/storey-county-nevada/") is None


# --- sites and sections ------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://stocktwits.com/news-articles/markets/equity/white-fiber-stock-pops-25",
        "https://simplywall.st/stocks/us/software/nasdaq-cifr/cipher-digital/future",
        "https://seekingalpha.com/article/4949457-core-scientific-texas-data-center-freeze",
        "https://www.advfn.com/stock-market/NASDAQ/WYFI/stock-news/97476101/whitefiber",
        "https://www.marketbeat.com/instant-alerts/nasdaq-cifr-2026-10-01/",
        "https://www.zacks.com/stock/news/123/data-center",
        "https://finance.yahoo.com/quote/CIFR/",
        "https://ca.finance.yahoo.com/quote/CIFR/news/",
        "https://www.investing.com/equities/digital-realty-trust",
        "https://www.marketscreener.com/quote/stock/CIPHER-DIGITAL-INC-116169050/",
    ],
)
def test_stock_chatter_and_quote_pages_are_not_news(url):
    assert screen.not_news(url)


@pytest.mark.parametrize(
    "url",
    [
        # Earnings-call transcripts sit under /quote/ and produced 3 citations in 7 reads.
        "https://finance.yahoo.com/quote/CIFR/earnings/CIFR-Q3-2025-earnings_call-371959.html/",
        "https://finance.yahoo.com/technology/articles/google-outlines-17b-project-tembo-1.html",
        "https://www.investing.com/news/company-news/terawulf-secures-19-billion-ai-lease-93CH",
        "https://www.marketscreener.com/quote/stock/CIPHER-DIGITAL-INC-116169050/news/Expands",
        "https://www.stocktitan.net/news/APLD/applied-digital-brings-an-additional-75-mw.html",
        "https://www.tipranks.com/news/private-companies/stack-infrastructure-plans-51mw",
    ],
)
def test_a_finance_site_that_reads_and_cites_is_left_open(url):
    assert screen.not_news(url) is None


def test_a_site_that_has_refused_every_read_is_not_queued_again():
    """datacenters.com answered all 192 requests with HTTP 429."""
    why = screen.not_news("https://www.datacenters.com/hut-8-corp-river-bend")
    assert why and "refused every read" in why


@pytest.mark.parametrize(
    ("url", "posting"),
    [
        ("https://jobs.apple.com/en-us/details/200653810/data-center-construction-pm", True),
        ("https://careers.equinix.com/jobs/senior-construction-project-manager", True),
        ("https://ats.rippling.com/edgeconnex/jobs/2302b6e9", True),
        ("https://ironmountain.jobs/phoenix-az/data-center-chiller-technician/job/", True),
        ("https://www.tesla.com/careers/search/job/sr-product-manager--271085", True),
        ("https://www.builtinsf.com/job/senior-strategic-sourcing-lead-compute/7435722", True),
        ("https://www.simplyhired.com/job/HlVAlLwoumlcWTlz29", True),
        # Campus news filed under a jobs section, and an article on a jobs site.
        ("https://www.dispatch.com/story/marketplace/jobs/2026/06/01/central-ohio-cologix", False),
        (
            "https://www.dallasnews.com/business/jobs/article/texas-data-center-jobs-22416619.php",
            False,
        ),
        ("https://builtin.com/artificial-intelligence/data-centers-powering-us-ai-boom", False),
    ],
)
def test_a_job_posting_is_not_news_and_a_jobs_section_story_is(url, posting):
    assert bool(screen.not_news(url)) is posting


def test_only_the_directory_listings_that_never_produced_a_campus_are_set_aside():
    """ZIP-code pages: nothing in 16 reads. A county or state page produced a citation
    about as often as the night's average, and some rows rest on nothing else."""
    assert screen.not_news("https://morethanjustparks.com/data-center-tracker/near/43054")
    assert screen.not_news("https://morethanjustparks.com/data-center-tracker/state/idaho") is None
    assert screen.not_news("https://dcmap.us/counties/pecos-county-tx/") is None


def test_a_car_dealers_inventory_is_not_news():
    url = "https://www.ewingsubaruofplano.com/inventory/new-2026-subaru-ascent-touring-awd/"
    assert screen.not_news(url) == "a car dealer's inventory"


# --- local news about something else ------------------------------------------------


def test_off_topic_words_name_a_game_a_killing_or_a_restaurant_and_not_jobs_or_closings():
    assert screen.off_topic("Football Recap: New Albany Extends Home Winning Streak")
    assert screen.off_topic("Beantown Taqueria Closing MIT Storefront in Cambridge")
    assert screen.off_topic("Elon Musk Threatens to Open a Tesla Diner in Palo Alto")
    assert (
        screen.off_topic("Amazon plans $11 billion Madison County expansion, adding 700 jobs")
        is None
    )
    assert screen.off_topic("Nscale closes $3 billion in financing for Texas") is None
    assert screen.off_topic("Bossier Parish Police Jury approves data center project") is None
    assert screen.off_topic("Worker killed in Ashburn trench collapse") is None


# --- a builder's archive ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "where"),
    [
        ("cyrusone announces sixth german facility in frankfurt", "frankfurt"),
        ("odata announces new data center in sao paulo brazil", "brazil"),
        ("hamina finland", "finland"),
        ("cologix opens third datacenter toronto", "toronto"),
        ("slough the land for the hyperscalers", "slough"),
    ],
)
def test_a_post_naming_only_a_place_abroad_is_abroad(text, where):
    assert screen.abroad(normalize_haystack(text), PLACES) == where


@pytest.mark.parametrize(
    "text",
    [
        "databank closes acquisition of zcolo us and uk data center assets",
        "fermi enlists korean firms for texan reactors",
        "meta new mexico los lunas data center",
        "google georgia douglas county",
        "dublin ireland and ashburn",
        "aligned data centers expands north american footprint in northeast ohio",
    ],
)
def test_a_post_naming_a_us_place_too_is_not_abroad(text):
    assert screen.abroad(normalize_haystack(text), PLACES) is None


def test_an_old_post_naming_nothing_is_set_aside_and_one_naming_a_town_or_event_is_read():
    """As first framed the rule would have cost Meta's "Henrico County, we are online!"
    and "Hello again, Los Lunas", each of which created the row held for that campus."""
    award = "https://www.databank.com/resources/news/databank-ceo-named-among-the-2022-capacity-power-100/"
    why = screen.builder_aside(award, None, None, places=PLACES, held=_never)
    assert why and "from 2022" in why
    for url in (
        "https://datacenters.atmeta.com/2020/08/hello-gallatin/",
        "https://datacenters.atmeta.com/2021/10/hello-again-los-lunas/",
        "https://h5datacenters.com/h5-cle-opening-day-2019.html",
        "https://www.novva.com/media-center/launch-of-tahoe-reno-data-center/",
    ):
        assert screen.builder_aside(url, None, None, places=PLACES, held=_never) is None, url
    recent = dt.datetime(2025, 3, 1)
    assert screen.builder_aside(award, None, recent, places=PLACES, held=_never) is None


def test_a_post_naming_a_campus_held_is_always_read():
    url = "https://www.cyrusone.com/resources/press-releases/third-data-center-frankfurt"
    assert screen.builder_aside(url, None, None, places=PLACES, held=_never)
    assert screen.builder_aside(url, None, None, places=PLACES, held=lambda u, t: True) is None


def test_a_post_is_dated_by_its_row_its_address_or_an_old_year_in_its_slug():
    assert screen.post_year("https://a.test/x", dt.datetime(2019, 5, 1)) == 2019
    assert screen.post_year("https://a.test/2021/03/post/", None) == 2021
    assert screen.post_year("https://a.test/h5-cle-opening-day-2018.html", None) == 2018
    assert screen.post_year("https://a.test/sdc-austin-progress-q1-2023", None) is None, (
        "a year's margin: a 2023 report is often published in 2024"
    )


def test_a_run_reports_what_it_set_aside_by_kind():
    reasons = [
        "a PDF, which the reader takes no text from",
        "a PDF, which the reader takes no text from",
        "a translated copy of a page (/intl/fi_ALL)",
        "about Frankfurt, outside the US",
        "seekingalpha.com: stock chatter and quotes",
    ]
    assert screen.summary(reasons) == (
        "2 PDFs, 1 translated copies, 1 posts about places abroad, 1 stock-chatter pages"
    )
