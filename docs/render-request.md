# Render request

`POST /api/v1/render` returns the fully rendered HTML or Markdown of a URL. Only `url` is required;
the service decides on its own how long to wait and whether plain HTTP or the browser is used.

## Full example

The selectors `#resultListItems` and `.listing-card` are placeholders; check them against the real
page.

```bash
curl -sS -X POST "https://<your-domain>/api/v1/render" \
  -H "Authorization: Bearer <API_KEY>" \
  -H "Content-Type: application/json" \
  -H "X-Request-ID: immo-berlin-001" \
  -D - \
  -d '{
    "url": "https://www.immobilienscout24.de/Suche/de/berlin/berlin/wohnung-mieten",
    "format": "html",
    "selector": "#resultListItems",
    "wait_for": ".listing-card",
    "mode": "auto",
    "use_proxy": false,
    "timeout": 30,
    "block_resources": ["image", "font", "media"]
  }'
```

## Fields

| Field | Required | Default | Values |
| --- | --- | --- | --- |
| `url` | yes | - | Absolute http(s) URL, at most 2048 characters |
| `format` | no | `"html"` | `"html"` or `"markdown"` |
| `selector` | no | - | CSS selector; only this element is returned. Aliases: `element`, `target` |
| `wait_for` | no | - | CSS selector that must be present. Once it is, the response follows within 1 s; if it never appears, the service waits until `timeout` and returns 504 |
| `mode` | no | `"auto"` | `"auto"`: plain HTTP when the site section is verified for it, otherwise the browser. `"browser"`: always Chrome |
| `use_proxy` | no | `false` | `true` routes the request through `HOME_PROXY` from the start; 400 when `HOME_PROXY` is not set. Rarely needed: with `HOME_PROXY` set, the service retries a render blocked by bot protection through it on its own |
| `timeout` | no | `30` | 5-120 seconds, an upper bound; the response comes as soon as the page is complete |
| `block_resources` | no | `false` | `false` or a list of `"image"`, `"font"`, `"media"`, `"stylesheet"` without duplicates; browser only |

Selectors are limited to 500 characters and must not nest `:has()`, `:not()`, `:is()` or `:where()`.

## Request headers

| Header | Required | Content |
| --- | --- | --- |
| `Authorization` | yes | `Bearer <API_KEY>`; keys have at least 32 characters |
| `Content-Type` | yes | `application/json` |
| `X-Request-ID` | no | Trace id of at most 64 characters from `A-Z a-z 0-9 . _ : -`; echoed in the response and the log, generated when missing or invalid |

The body may be at most 64 KiB, otherwise the service answers 413.

## Recommendation

In most cases the URL is enough:

```json
{"url": "https://www.immobilienscout24.de/Suche/de/berlin/berlin/wohnung-mieten"}
```

- Add `wait_for` when a page loads its content late, e.g. `.listing-card` on ImmoScout24.
- Leave out `block_resources` for sites with bot protection: CDN-based bot protection sees that no
  image is ever requested.
- Use `mode: "browser"` only when a page must never be served over plain HTTP.

## Response

The body is the HTML or Markdown (`text/html` or `text/markdown`). The headers explain how the page
was rendered.

| Header | Values |
| --- | --- |
| `X-Render-Engine` | `http` or `browser` |
| `X-Render-Stable` | `true`; `false` when the page did not settle before the timeout |
| `X-Render-Ready-Reason` | `settled`, `wait-for-found`, `load-budget-expired`, `deadline` or `verified-http` |
| `X-Render-Profile` | `cold`, `learned` or `n/a` (HTTP path) |
| `X-Render-Route` | `direct` (the server's own IP) or `proxy` (`HOME_PROXY`: `use_proxy`, an automatic retry after a bot block, or a host remembered as needing it) |
| `X-Final-Url` | URL after all redirects, without credentials |
| `X-Upstream-Status` | Status code of the target page |
| `X-Request-ID` | Trace id of this request |

Errors always use this envelope; a 400 additionally lists `fields` per invalid field:

```json
{"error": {"code": "TIMEOUT", "message": "...", "traceId": "immo-berlin-001"}}
```

See the README for the full list of error codes.
