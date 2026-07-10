"""Headless rendering (Playwright, lazy) - only for JS-shell sources.

One synchronous browser per process, started on first use. The worker is
single-threaded; no pooling needed at this scale.
"""

import logging

from eventindex import config

log = logging.getLogger("eventindex.headless")

RENDER_TIMEOUT_MS = 25_000
MAX_CLICKS = 15
MAX_SCROLLS = 15

_browser = None


def _get_browser():
    global _browser
    if _browser is None:
        from playwright.sync_api import sync_playwright

        pw = sync_playwright().start()
        _browser = pw.chromium.launch(headless=True)
    return _browser


def render_page(
    url: str,
    click_selector: str | None = None,
    scroll: bool = False,
) -> bytes | None:
    """Render a page; optionally exhaust a load-more button or infinite
    scroll (bounded), then return the final DOM HTML."""
    context = None
    try:
        context = _get_browser().new_context(user_agent=config.USER_AGENT)
        page = context.new_page()
        page.goto(url, timeout=RENDER_TIMEOUT_MS, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)

        if click_selector:
            for _ in range(MAX_CLICKS):
                button = page.query_selector(click_selector)
                if button is None or not button.is_visible():
                    break
                button.click()
                page.wait_for_timeout(1200)
        elif scroll:
            last_height = 0
            for _ in range(MAX_SCROLLS):
                height = page.evaluate("document.body.scrollHeight")
                if height == last_height:
                    break
                last_height = height
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(1200)

        return page.content().encode()
    except Exception as e:
        log.warning("headless render failed %s: %s", url, e)
        return None
    finally:
        if context is not None:
            context.close()  # failure paths must not accumulate contexts


# a paginator control that is present but exhausted: native disabled, ARIA,
# or the ubiquitous .disabled/.ui-state-disabled on the control or its <li>
_NEXT_EXHAUSTED_JS = """e =>
    e.disabled === true
    || e.getAttribute('aria-disabled') === 'true'
    || e.className.includes('disabled')
    || (e.closest('li,span,div') || e).className.includes('disabled')
"""


def render_states(
    url: str, next_selector: str | None, max_states: int
) -> list[bytes] | None:
    """next_click pagination: harvest EVERY page state - unlike load-more
    (accumulating DOM, final snapshot suffices), a JSF/PrimeFaces-style
    paginator REPLACES the list in place, so each state must be captured
    before the next click. Stops on missing/disabled control or when a
    click no longer changes the DOM."""
    context = None
    try:
        context = _get_browser().new_context(user_agent=config.USER_AGENT)
        page = context.new_page()
        page.goto(url, timeout=RENDER_TIMEOUT_MS, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)

        states = [page.content()]
        while next_selector and len(states) < max_states:
            button = page.query_selector(next_selector)
            if (button is None or not button.is_visible()
                    or button.evaluate(_NEXT_EXHAUSTED_JS)):
                break
            button.click()
            page.wait_for_timeout(1200)
            content = page.content()
            if content == states[-1]:  # no-op click: not a working paginator
                break
            states.append(content)
        return [s.encode() for s in states]
    except Exception as e:
        log.warning("headless states failed %s: %s", url, e)
        return None
    finally:
        if context is not None:
            context.close()
