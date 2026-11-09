# -*- coding: utf-8 -*-
"""UK Raves — event downloads from ukraves.co.uk.

``/mixes/downloads`` and ``/mixes/other-downloads`` are paginated grids of
mix cards.  Each card has a ``/go/mix/<id>`` button that 302s to either a
Mediafire file page or YouTube.  The redirect is resolved once per mix (cached
for six hours) so the room can mark YouTube rows as a hand-off instead of
offering a dead play button.  Mediafire pages are then passed to
:func:`core.ezgarage.resolve_page`, which shares the curated resolver's
single-flight lock, pacing and 429 backoff.

The site paginates at 20 cards per category, so one app page is one site page
from *both* categories (40 rows).  Only the redirect hosts we know are ever
followed; everything else is shown as a source link.
"""

import html
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed, wait

from . import cache, ezgarage
from . import fetch as http

BASE = "https://www.ukraves.co.uk"
SOURCE_LABEL = "UK Raves"
SOURCE_URL = "https://www.ukraves.co.uk/mixes/downloads"
ATTRIBUTION = "Event downloads from UK Raves (ukraves.co.uk)"
CATEGORY_URLS = (
    BASE + "/mixes/downloads",
    BASE + "/mixes/other-downloads",
)
TTL = 6 * 60 * 60
REDIRECT_TTL = 6 * 60 * 60
REDIRECT_NEGATIVE_TTL = 5 * 60
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
TIMEOUT = 15
WORKERS = 4
# A whole room page must never sit on 40 slow redirects: after this deadline
# unresolved rows fall back to their source link.
DESCRIBE_DEADLINE = 20

CARD_RE = re.compile(r'<div class="profile-card" id="mix-(\d+)"', re.IGNORECASE)
PAGE_RE = re.compile(r'href="[^"]*(?:[?&]|&amp;)page=(\d+)"', re.IGNORECASE)
GO_RE = re.compile(r'href="/go/mix/(\d+)"', re.IGNORECASE)
EVENT_RE = re.compile(r'<a[^>]+href="/events/[^"]*"[^>]*>(.*?)</a>',
                      re.IGNORECASE | re.DOTALL)
H2_RE = re.compile(r'<h2 class="profile-card__name">(.*?)</h2>',
                   re.IGNORECASE | re.DOTALL)
SUBTITLE_RE = re.compile(r'<p class="profile-card__subtitle">(.*?)</p>',
                         re.IGNORECASE | re.DOTALL)
DESC_RE = re.compile(r'<p class="profile-card__description">(.*?)</p>',
                     re.IGNORECASE | re.DOTALL)
DATE_RE = re.compile(r"(?:&middot;|·)\s*([0-9]{1,2}\s+[A-Za-z]{3,9}\s+[0-9]{4})")
ZIP_EXTENSIONS = (".mp3", ".m4a")


class UkRavesError(Exception):
    """A resolver failure the view turns into a JSON status code."""

    code = 502

    def __init__(self, message, code=None):
        super(UkRavesError, self).__init__(message)
        self.message = str(message)
        if code is not None:
            self.code = int(code)


class NotMediafire(UkRavesError):
    code = 409


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)
_REDIRECT_SLOTS = threading.BoundedSemaphore(WORKERS)


def _text(value):
    text = re.sub(r"<[^>]+>", " ", value or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def parse_page(text, page=1):
    """Parse one category page into card dicts plus ``has_next``.

    Cards are sliced by the next ``profile-card`` marker rather than by nested
    closing tags: the markup nests a variable number of divs, while the start
    marker is stable.
    """
    if not isinstance(text, str) or not text:
        return [], False
    cards = list(CARD_RE.finditer(text))
    rows = []
    for index, match in enumerate(cards):
        end = cards[index + 1].start() if index + 1 < len(cards) else len(text)
        block = text[match.end():end]
        go = GO_RE.search(block)
        if not go:
            continue
        download_id = go.group(1)
        name = _text(H2_RE.search(block).group(1)) if H2_RE.search(block) else ""
        subtitle = _text(SUBTITLE_RE.search(block).group(1)) if SUBTITLE_RE.search(block) else ""
        description = _text(DESC_RE.search(block).group(1)) if DESC_RE.search(block) else ""
        event = _text(EVENT_RE.search(description).group(1)) if EVENT_RE.search(description) else ""
        if not event and description:
            # Some cards put the event name in the description without a link.
            event = description.split("·", 1)[0].strip()
        date_match = DATE_RE.search(description)
        date = date_match.group(1) if date_match else ""
        title = name or subtitle or "UK Raves mix"
        rows.append({
            "id": download_id,
            "title": title,
            "event": event,
            "date": date,
            "subtitle": subtitle,
            "go_url": "%s/go/mix/%s" % (BASE, download_id),
        })
    pages = [int(value) for value in PAGE_RE.findall(text)
             if value.isdigit() and 1 <= int(value) <= 10000]
    try:
        current = max(1, int(page))
    except (TypeError, ValueError):
        current = 1
    return rows, (current + 1 in pages)


def _page_url(base, page):
    return "%s?sort=date_desc&page=%d" % (base, max(1, int(page)))


def page_entries(page=1):
    """Both category pages for one site page, parsed and cached.

    Returns ``(rows, has_next)``.  One category failing still yields the other;
    both failing yields an empty list (the view shows a graceful empty room).
    """
    try:
        page = max(1, int(page))
    except (TypeError, ValueError):
        page = 1
    rows = []
    has_next = False
    calls = []
    for base in CATEGORY_URLS:
        key = "ukraves:page:v1:%s:%d" % (base, page)
        calls.append((key, base))

    def load(key, base):
        def fetch_parse():
            body = http.fetch(_page_url(base, page), timeout=25, ttl=TTL,
                              headers={"User-Agent": BROWSER_UA,
                                       "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
            if not body:
                return None
            rows, has_next = parse_page(body, page)
            return {"rows": rows, "has_next": has_next}
        return cache.remember(key, TTL, fetch_parse, sync=False)

    results = []
    with ThreadPoolExecutor(max_workers=min(2, len(calls))) as pool:
        futures = [pool.submit(load, key, base) for key, base in calls]
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception:  # noqa: BLE001 - one category must not kill the room
                results.append(None)
    for result in results:
        if not isinstance(result, dict):
            continue
        rows.extend(result.get("rows") or [])
        has_next = has_next or bool(result.get("has_next"))
    return rows, has_next


def _fetch_location(url):
    """Resolve one ``/go/mix`` hop without following it; ``""`` on failure."""
    request = urllib.request.Request(url, headers={
        "User-Agent": BROWSER_UA,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Encoding": "identity",
    })
    with _REDIRECT_SLOTS:
        try:
            response = _OPENER.open(request, timeout=TIMEOUT)
        except urllib.error.HTTPError as exc:
            headers = exc.headers
            location = headers.get("Location") if headers else ""
            try:
                exc.close()
            except Exception:  # noqa: BLE001
                pass
            return str(location or "").strip()
        except (urllib.error.URLError, OSError, ValueError):
            return ""
        try:
            return str(response.geturl() or "").strip()
        finally:
            try:
                response.close()
            except Exception:  # noqa: BLE001
                pass


def redirect_target(mix_id):
    """Cached redirect target for one mix id, or ``""``.

    A success is cached for six hours; a failed hop is only remembered for
    five minutes so a transient network error does not turn a playable row
    into a permanent hand-off.
    """
    mix_id = str(mix_id or "").strip()
    if not re.match(r"^\d{1,10}$", mix_id):
        return ""
    key = "ukraves:redirect:v1:%s" % mix_id
    cached = cache.get(key, REDIRECT_TTL)
    if cached is not cache.MISSING and isinstance(cached, dict):
        return str(cached.get("url") or "")
    missed = cache.get(key + ":miss", REDIRECT_NEGATIVE_TTL)
    if missed is not cache.MISSING:
        return ""
    target = _fetch_location("%s/go/mix/%s" % (BASE, mix_id))
    if target:
        cache.put(key, {"url": target}, sync=False)
    else:
        cache.put(key + ":miss", {"url": ""}, sync=False)
    return target


def classify(target):
    """Classify a redirect target as mediafire / youtube / direct / external."""
    value = str(target or "").strip()
    if not value:
        return {"kind": "unavailable", "url": ""}
    try:
        parts = urllib.parse.urlsplit(value)
    except ValueError:
        return {"kind": "external", "url": value}
    host = (parts.hostname or "").lower().rstrip(".")
    if host == "mediafire.com" or host.endswith(".mediafire.com"):
        return {"kind": "mediafire", "url": value}
    if host in ("youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"):
        return {"kind": "youtube", "url": value}
    if parts.path.lower().endswith(ZIP_EXTENSIONS):
        return {"kind": "direct", "url": value}
    return {"kind": "external", "url": value}


def describe(entry):
    """Add a provider classification to one parsed card."""
    row = dict(entry) if isinstance(entry, dict) else {}
    if not row:
        return row
    row["provider"] = classify(redirect_target(row.get("id")))
    return row


def describe_many(rows, workers=WORKERS):
    """Classify a page's redirect targets with a bounded pool."""
    rows = [row for row in (rows or []) if isinstance(row, dict)]
    if not rows:
        return []
    if len(rows) == 1:
        return [describe(rows[0])]
    results = [None] * len(rows)
    pool = ThreadPoolExecutor(max_workers=max(1, min(workers, len(rows))))
    futures = {pool.submit(describe, row): index for index, row in enumerate(rows)}
    done, not_done = wait(futures, timeout=DESCRIBE_DEADLINE)
    for future in done:
        index = futures[future]
        try:
            results[index] = future.result()
        except Exception:  # noqa: BLE001 - keep the row as a source link
            results[index] = dict(rows[index], provider={
                "kind": "unavailable", "url": rows[index].get("go_url", "")})
    for future in not_done:
        future.cancel()
        index = futures[future]
        results[index] = dict(rows[index], provider={
            "kind": "unavailable", "url": rows[index].get("go_url", "")})
    # Do not wait for a timed-out DNS/HTTP call on the request thread.
    pool.shutdown(wait=False)
    return [row for row in results if row]


def mediafire_url(mix_id):
    """Resolve a mix's Mediafire page to a signed direct URL.

    Raises :class:`NotMediafire` for YouTube/external rows.  The generic
    Mediafire resolver performs the page fetch under the shared single-flight
    lock and returns a short-lived signed URL.
    """
    target = redirect_target(mix_id)
    provider = classify(target)
    if provider["kind"] != "mediafire":
        raise NotMediafire("that UK Raves mix is not a Mediafire download",
                           code=404 if provider["kind"] == "unavailable" else 409)
    page = provider["url"]
    try:
        parts = urllib.parse.urlsplit(page)
    except ValueError:
        raise UkRavesError("Mediafire link is malformed")
    host = (parts.hostname or "").lower().rstrip(".")
    if host.startswith("download") and host.endswith(".mediafire.com"):
        return page
    if parts.scheme == "http":
        page = urllib.parse.urlunsplit(("https", parts.netloc, parts.path,
                                        parts.query, ""))
    try:
        return ezgarage.resolve_page(page)
    except ezgarage.EzGarageError as exc:
        raise UkRavesError(exc.message, code=exc.code)


def mix_id_for_url(value):
    """Return the mix id embedded in a stable go URL, else ``""``."""
    try:
        path = urllib.parse.urlsplit(str(value or "")).path
    except ValueError:
        return ""
    match = re.search(r"/go/mix/(\d{1,10})", path)
    return match.group(1) if match else ""


def summary(rows):
    rows = rows or []
    if not rows:
        return "Source unavailable — try again later"
    events = len({row.get("event") for row in rows if row.get("event")})
    parts = ["%d downloads" % len(rows)]
    if events:
        parts.append("%d events" % events)
    parts.append(SOURCE_LABEL)
    return " · ".join(parts)
