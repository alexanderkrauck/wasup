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

# cookie walls hide content and can swallow paginator clicks; the onboarding
# agent dismisses them, so the crawler that executes its recipe must too
COOKIE_SELECTORS = (
    ".cc_btn_accept_all, #onetrust-accept-btn-handler, "
    "[class*='cookie'] button, [id*='cookie'] [class*='accept'], "
    "button[class*='accept'], a[class*='cc_btn']"
)


def _dismiss_cookies(page) -> None:
    try:
        banner = page.query_selector(COOKIE_SELECTORS)
        if banner and banner.is_visible():
            banner.click()
            page.wait_for_timeout(600)
    except Exception:
        pass


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
        _dismiss_cookies(page)

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
) -> tuple[list[bytes], str] | None:
    """next_click pagination: harvest EVERY page state - unlike load-more
    (accumulating DOM, final snapshot suffices), a JSF/PrimeFaces-style
    paginator REPLACES the list in place, so each state must be captured
    before the next click. Returns (states, stop_reason):
      'exhausted' - the next control vanished/disabled: natural end
      'cap'       - max_states cut the walk short with pages still ahead;
                    the caller must NOT let that stay silent
      'noop'      - the click changed nothing: the selector matches a dead
                    control (the failure mode that yielded 15/1134 events
                    on linztermine prod, 2026-07-10)"""
    context = None
    try:
        context = _get_browser().new_context(user_agent=config.USER_AGENT)
        page = context.new_page()
        page.goto(url, timeout=RENDER_TIMEOUT_MS, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        _dismiss_cookies(page)

        states = [page.content()]
        reason = "exhausted"
        stop_detail = ""
        while next_selector:
            button = page.query_selector(next_selector)
            if button is None:
                # under load (shared box, parallel crawls) widgets render
                # late; a session declared working paginations dead over
                # exactly this (prod 2026-07-11) - wait before giving up
                try:
                    page.wait_for_selector(next_selector, state="attached",
                                           timeout=4000)
                except Exception:
                    pass
                button = page.query_selector(next_selector)
                if button is None:
                    stop_detail = "button-missing"
                    break
            if not button.is_visible():
                stop_detail = "button-invisible"
                break
            if button.evaluate(_NEXT_EXHAUSTED_JS):
                stop_detail = "button-disabled"
                break
            if len(states) >= max_states:
                reason = "cap"  # limit hit with pages still ahead
                break
            button.click()
            page.wait_for_timeout(1200)
            content = page.content()
            if content == states[-1]:  # slow AJAX? one more chance
                page.wait_for_timeout(2500)
                content = page.content()
            if content == states[-1]:  # no-op click: not a working paginator
                reason = "noop"
                break
            states.append(content)
        # diagnosis line: sessions kept measuring fewer states than
        # standalone runs of identical recipes (2026-07-11); this names the
        # exact stop branch, page state, and title in the worker journal
        log.info("render_states url=%s states=%d stop=%s%s title=%r len=%d",
                 url, len(states), reason,
                 f"({stop_detail})" if stop_detail else "",
                 page.title()[:80], len(states[-1]))
        return [s.encode() for s in states], reason
    except Exception as e:
        log.warning("headless states failed %s: %s", url, e)
        return None
    finally:
        if context is not None:
            context.close()
