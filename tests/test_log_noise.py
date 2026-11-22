"""The night's log keeps the requests that failed and drops the ones that worked."""

from __future__ import annotations

import logging

from tracker.cli import _shared


def _request(status: int) -> logging.LogRecord:
    return logging.LogRecord(
        "httpx",
        logging.INFO,
        __file__,
        1,
        'HTTP Request: %s %s "%s %d %s"',
        ("GET", "https://example.com/robots.txt", "HTTP/1.1", status, "Whatever"),
        None,
    )


def test_a_successful_request_is_not_logged_and_a_refused_one_is():
    keep = _shared._FailedRequestsOnly()
    assert not keep.filter(_request(200))
    assert not keep.filter(_request(301))
    assert keep.filter(_request(403)) and keep.filter(_request(429)) and keep.filter(_request(503))


def test_every_other_line_passes():
    keep = _shared._FailedRequestsOnly()
    other = logging.LogRecord("httpx", logging.WARNING, __file__, 1, "pool %s", ("x",), None)
    assert keep.filter(other)


def test_verbose_takes_the_filter_off_again():
    logger = logging.getLogger("httpx")
    try:
        _shared._quiet_http_successes(True)
        _shared._quiet_http_successes(True)
        assert sum(isinstance(f, _shared._FailedRequestsOnly) for f in logger.filters) == 1
        _shared._quiet_http_successes(False)
        assert not any(isinstance(f, _shared._FailedRequestsOnly) for f in logger.filters)
    finally:
        _shared._quiet_http_successes(False)


def test_a_log_file_gets_one_line_per_message_whatever_its_length():
    """Rich wrapped a long warning onto lines of its own at 200 columns, so grep found
    the URL and its error apart."""
    handler = _shared._log_handler(verbose=False)
    assert not isinstance(handler, _shared.RichHandler), "stderr is not a terminal here"
    url = "https://example.com/" + "a" * 300
    record = logging.LogRecord(
        "x", logging.WARNING, __file__, 1, "fetch failed for %s (%s)", (url, "HTTP 403"), None
    )
    text = handler.format(record)
    assert text == f"WARNING  fetch failed for {url} (HTTP 403)"
    assert "\n" not in text


def test_a_message_of_several_lines_is_indented_under_its_first():
    """So no line inside one can start like the loop's own lines, which the status
    reader picks out by their first characters."""
    from tracker import loopstatus

    handler = _shared._log_handler(verbose=False)
    record = logging.LogRecord(
        "x", logging.ERROR, __file__, 1, "browser failed\n[2026-11-23 01:00:00] round 9", (), None
    )
    first, second = handler.format(record).split("\n")
    assert first == "ERROR    browser failed"
    assert second.startswith(" " * 9) and not loopstatus.wanted(second)
