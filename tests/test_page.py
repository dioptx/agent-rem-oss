"""The page is self-contained and reads the API."""

from __future__ import annotations

from tests.test_serve import call, server  # noqa: F401  (server is a pytest fixture)


def test_page_is_self_contained_and_reads_the_api(server):
    status, raw = call(server, "GET", "/")
    assert status == 200
    assert b"https://" not in raw and b"http://" not in raw     # no CDN, fonts or trackers
    for endpoint in (b"/api/overview", b"/api/settings", b"/api/control", b"/api/triage"):
        assert endpoint in raw
    assert b"'X-REM': '1'" in raw


def test_jump_bar_reaches_every_section_and_outbound_links_open_apart(server):
    html = call(server, "GET", "/")[1].decode()
    for sid in ("s-pile", "s-decide", "s-yes", "s-fixes", "s-friction"):
        assert f'href="#{sid}"' in html and f'id="{sid}"' in html
    assert 'target="_blank" rel="noopener noreferrer"' in html


def test_page_prioritizes_pile_and_collapses_settings(server):
    html = call(server, "GET", "/")[1].decode()
    assert html.index('id="s-pile"') < html.index('id="s-decide"')
    assert html.index('id="s-settings"') < html.index('<nav')
    assert '<details class="panel settings-panel" id="s-settings">' in html
    assert '<summary>Settings</summary>' in html
    assert 'id="s-runs"' not in html and 'renderRuns(' not in html
    assert 'id="alarm"' in html and '/api/control' in html
    assert 'id="fixes" role="region" aria-labelledby="h-fixes" tabindex="0"' in html
    assert '#fixes{max-height:22rem;overflow-y:auto;' in html
