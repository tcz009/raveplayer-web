# Web source listing and pagination — 1.6.53

Every browsable list now pages at **40 rows per page** with the same numbered
pager (`Page N of M · X items`, `Previous`, `1 2 3 … N`, `Next`).  No route
silently truncates a source and no route builds a 400-link page: the numbered
strip is a bounded window (`1`, two neighbours either side, `N`, with `…`).

| Source | 1.6.53 paging |
|---|---|
| Toronto Rave Mixtape Archive | 40/page over the whole genre (679/2,640 rows) or search; the page filter + slice happens before item dicts are built. `?limit=` remains an explicit one-shot cap for callers that ask for it. |
| Moxser Mixes | 40/page over the complete sitemap set (1,001 mixes); sitemaps 1 h, mix pages 6 h, ≤1 req/s. |
| Hardscore (mixes + events) | 40/page over the combined feed. |
| Podcasts & Mixes episodes | 40/page over the feed document (a 5,000-episode feed is 125 pages, not one response). |
| JungleTapes shows | 40/page over the 310-show sitemap (was 50). |
| Rave Nights | 40/page over the 55-row registry. |
| Rave Archive UK / GetDarker | 40/page. |
| Pirate Radio Archives | curated finite lists (unchanged, usually < 40). |
| Internet Archive sections / Every Pack / By Year / Quick Filters | 40/page with `total`, `pager` and the legacy `next` link. |
| Archive item packs | 40/page; a 1,645-track pack is 42 pages with absolute track numbers on the SPA queue. |
| Search | 40/page; the title/scene widen still only runs on page one, and page one keeps `next` disabled so widened rows cannot repeat. |
| Downloads | 40/page; `storage.items` reports the whole library, not the page. |
| Live radio genre groups | 40/page over up to 500 stations. |
| RaveTapePacks (RTP) | WordPress pages are 10/page and only expose “is there a next page”; those routes carry a cursor pager (`pages: null`, Previous/Next, “Page N”) instead of inventing M. The RTP category index pages at 40 with a numbered pager. `?limit=&offset=` remains for explicit callers. |

## Response contract

```json
{
  "title": "Jungle",
  "items": [],
  "total": 2640,
  "pager": {
    "page": 2, "pages": 66, "total": 2640, "per_page": 40,
    "prev": "/api/toronto?genre=jungle&page=1",
    "next": "/api/toronto?genre=jungle&page=3",
    "links": [1, 2, 3, 4, 66]
  },
  "next": "/api/toronto?genre=jungle&page=3"
}
```

`next` is kept for older callers.  The SPA uses `pager` and keeps its URL state
in the hash (`#/api/toronto?genre=jungle&page=2`), so browser Back/Forward
restores the previous page and its rows.

## Why

The old behaviour returned the complete list where it was small and used
“Load more” where it was not.  That made the first response for Toronto/Moxser
large, made the number of visible rows depend on the source, and gave the owner
no page numbers.  Uniform 40/page keeps every response bounded regardless of
the source, while the total and page count stay in the response.

Tests: `test/test_pagination_web.py` walks each large list page by page and
asserts the union is the whole collection with no duplicate or missing rows,
plus ARIA/URL behaviour in `test_e2e/test_smoke.py`.
