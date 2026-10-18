"""Error messages stay short: an HTML error page is summarised, not quoted."""

from __future__ import annotations

from mini_app_polis.api.errors import (
    ApiUnavailable,
    KaianoApiError,
    error_for,
    summarize_body,
)

# Trimmed from the page Cloudflare served for a 502 on 2026-10-18.
CLOUDFLARE_502 = """<!DOCTYPE html>
<!--[if lt IE 7]> <html class="no-js ie6 oldie" lang="en-US"> <![endif]-->
<!--[if gt IE 8]><!--> <html class="no-js" lang="en-US"> <!--<![endif]-->
<head>
<title>kaianolevine.com | 502: Bad gateway</title>
<meta charset="UTF-8" />
</head>
<body>
<div id="cf-wrapper">
  <h1><span class="inline-block">Bad gateway</span></h1>
  <p class="text-13">
    <span class="cf-footer-item">Cloudflare Ray ID: <strong class="font-semibold">a4c93d925a39fc74</strong></span>
  </p>
</div>
</body>
</html>"""


def test_an_html_error_page_becomes_its_title_and_ray_id() -> None:
    assert summarize_body(CLOUDFLARE_502) == (
        "HTML error page: kaianolevine.com | 502: Bad gateway, "
        "Cloudflare ray a4c93d925a39fc74"
    )


def test_the_exception_message_is_one_short_line() -> None:
    err = error_for(502, CLOUDFLARE_502, "/v1/wcs/wiki/export")
    assert isinstance(err, ApiUnavailable)
    assert str(err) == (
        "KaianoApiError 502 on /v1/wcs/wiki/export: HTML error page: "
        "kaianolevine.com | 502: Bad gateway, Cloudflare ray a4c93d925a39fc74"
    )
    assert "\n" not in str(err)


def test_a_page_with_no_title_or_ray_still_says_what_it_was() -> None:
    assert summarize_body("<html><body>oops</body></html>") == "HTML error page"


def test_the_apis_json_envelope_is_kept_whole() -> None:
    body = '{"error": {"code": "page_not_found", "message": "Wiki page not found"}}'
    err = error_for(404, body, "/v1/wcs/wiki/pages/x")
    assert type(err) is KaianoApiError
    assert err.message == body


def test_long_plain_text_is_cut_short_and_says_how_long_it_was() -> None:
    summary = summarize_body("x" * 1000)
    assert summary.endswith("… (1000 chars)")
    assert len(summary) < 320


def test_short_plain_text_and_an_empty_body() -> None:
    assert summarize_body("bad gateway") == "bad gateway"
    assert summarize_body("  ") == "(empty body)"


def test_edge_not_found_still_reads_the_raw_body() -> None:
    # Classification looks at the body itself, not the summary.
    body = '{"status": "error", "code": 404, "message": "Application not found"}'
    assert isinstance(error_for(404, body, "/v1/x"), ApiUnavailable)
