# bowerbird

Production Instagram media scraper. Fully async, fully typed, resumable, and
**HTTP-first**: a browser is a recovery mechanism, not the scraper.

Verified live against `instagram.com/instagram` (8,543 posts) on 2026-07-31.

---

## Why it is fast

Instagram's public timeline answers a plain anonymous `GET` — no cookies, no
CSRF token, no browser. That is the path this scraper takes by default; the
expensive machinery only wakes up when the cheap path stops working.

```
1. anonymous HTTP        httpx, pooled, HTTP/2          ← default
2. impersonated HTTP     curl_cffi, Chrome TLS/JA3
3. authenticated HTTP    curl_cffi + session cookies
4. browser               cloakbrowser Chromium          ← last resort
```

Escalation is automatic and invisible to callers. `AuthenticationError` moves
up a tier; `RateLimitError` does **not** — being throttled means waiting, and
escalating would only burn the next credential too. Once a tier works, the
provider locks onto it instead of re-walking the ladder every request.

---

## Install

Recommended, as an isolated command-line tool:

```bash
pipx install bowerbird
```

That is the whole install. `bowerbird` (and the short alias `bb`) land on your
PATH in their own virtualenv, with nothing leaking into your system Python.

Run it once without installing anything:

```bash
pipx run bowerbird profile nasa
```

Other routes:

```bash
uv tool install bowerbird          # same idea, uv's tool installer
pip install bowerbird              # into whatever environment is active
python -m bowerbird --help         # module form, no console script needed
```

Python 3.13+.

### The browser tier is optional

Tier 4 needs Chromium and is not installed by default; without it the tier is
simply skipped and everything else works. Add it only if you want it:

```bash
pipx install "bowerbird[browser]"
pipx run --spec "bowerbird[browser]" bowerbird ...   # or one-off
playwright install chromium                          # the browser itself
```

Already installed without it? `pipx inject bowerbird cloakbrowser playwright`.

### From a checkout

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"            # editable, with the test dependencies
pytest
```

---

## Use

```bash
# whole profile, resumable
bowerbird profile nasa

# first 50 posts into ./out, 16 download workers
bowerbird profile nasa --limit 50 --output out --workers 16

# single post, reel, stories, highlights
bowerbird post https://www.instagram.com/p/DbbY9pdm6Q2/
bowerbird reel https://www.instagram.com/reel/Dbd9uTISQNu/
bowerbird story nasa      --cookies cookies.json
bowerbird highlights nasa --cookies cookies.json

# resolve a username to its numeric id
bowerbird whoami nasa
```

Installed via pipx you get `bowerbird` and the shorter `bb`. Without a
console script on PATH, `python -m bowerbird ...` is equivalent.

### Options

| Flag | Meaning |
|---|---|
| `--output, -o` | Output directory (default `bowerbird/`) |
| `--workers, -w` | Concurrent downloads (default 8) |
| `--limit, -n` | Stop after N posts |
| `--resume / --no-resume` | Continue from saved cursor (default on) |
| `--proxy` | Proxy URL, applied to every tier including downloads |
| `--cookies` | Cookie file, JSON or Netscape |
| `--json` | Machine-readable report on stdout |
| `--metadata / --no-metadata` | Write `metadata.jsonl` (default on) |
| `--no-browser` | Never launch Chromium; fail instead |
| `--verbose, -v` | Debug logging |
| `--version` | Print the version and exit |

### As a library

```python
from bowerbird import ScraperConfig
from bowerbird.scraper import InstagramScraper

async with InstagramScraper.build(ScraperConfig()) as scraper:
    report = await scraper.scrape_profile("nasa", limit=100)
    print(report.files_downloaded, report.bytes_downloaded)
```

Streaming, without touching the disk:

```python
from bowerbird.api.client import InstagramClient

async with InstagramClient.build() as client:
    async for media in client.iter_posts("nasa"):
        print(media.shortcode, media.media_type, media.media_urls)
```

Nothing is ever fully materialised: one page (12 items) lives in memory at a
time, whatever the account size.

---

## Output

```
bowerbird/nasa/
  20260731_190054_Dbd9uTISQNu.mp4          reel
  20260730_185958_DbbY9pdm6Q2_01.jpg       carousel slide 1
  20260730_185958_DbbY9pdm6Q2_03.mp4       carousel slide 3 (video)
  metadata.jsonl                           one JSON record per post
  nasa_posts.state.json                    resume cursor
```

A carousel writes one file per slide and no more: the container's cover image
is the first slide, so writing it separately would duplicate a file per post.

Filenames are deterministic (`<timestamp>_<shortcode>[_<slide>].<ext>`), which
is what makes a re-run skip existing files without consulting any index.

`metadata.jsonl` records carry: `id`, `shortcode`, `owner_id`, `username`,
`caption`, `hashtags`, `mentions`, `timestamp`, `like_count`, `comment_count`,
`view_count`, `dimensions`, `duration`, `location`, `music`, `thumbnail_url`,
`media_urls`, `media_type`, `children`, `permalink`.

---

## Resume

State is written after **every completed page**. Interrupt a 700-page account
at page 530 and the next run starts at 531 — no re-walking, and no re-spending
530 pages of rate-limit budget.

```bash
bowerbird profile nasa          # ^C at any point
bowerbird profile nasa          # continues from the saved cursor
bowerbird profile nasa --no-resume   # ignore saved state entirely
```

Re-running a **finished** account picks up new posts. The cursor is not reused
there: it points at the oldest post, and new posts arrive at the newest end, so
resuming from it would ask for everything after the last post and be answered
with nothing. A finished job re-walks from the top instead.

`metadata.jsonl` doubles as the archive of finished posts, and that is what
makes an update cheap. A post's record is written **only once every one of its
files has landed**, so its presence means there is no work left. A post in the
archive is never handed to the downloader — no request, not even a `stat`.

Because the timeline is newest-first, a page with nothing new on it means
everything below is already downloaded, so the walk stops there. Measured on a
600-post account:

| Run | Pages fetched | API requests |
|---|---|---|
| Initial scrape | 50 | 50 |
| Re-check, nothing new | 2 | 2 |
| Re-check, one new post | 3 | 3 |
| `--full` | 51 | 51 |

Four layers, cheapest first:

| Layer | Cost of an already-downloaded item |
|---|---|
| Early stop | ends the walk — the rest of the account is never requested |
| Metadata archive | in-memory set lookup; the post never reaches the downloader |
| Saved cursor | skips whole pages on an **interrupted** run |
| Files on disk | one `stat()` by deterministic filename — no CDN request |

A post whose files did not all download is deliberately **not** archived, so
the next run retries it. That is the case `--full` exists for: the early stop
only guarantees the newest posts are current, so if an older post failed and
has since scrolled past the stop window, `--full` walks the whole account to
backfill it. Tune the window with `stop_after_known_pages` (default 2 pages;
0 means never stop early).

---

## Authentication

Credentials are never accepted or stored. Export cookies from a browser you
already logged into and pass the file:

```bash
bowerbird profile private_account --cookies cookies.json
```

Both formats work: JSON (`{"sessionid": "..."}` or an extension's
`[{"name":..., "value":...}]` array) and Netscape `cookies.txt`. When the
browser tier runs, it harvests fresh cookies back into the same file so later
runs can stay on the cheap tiers.

The cookie file is written `0600` — a `sessionid` is full account access, and
the browser tier saves one without being asked.

Session required for: stories, highlights, private accounts, single posts.
Not required for: public profile timelines, which is the bulk of the work.

A private account is attempted whenever a session is configured; Instagram
decides whether that session may see it. Without one, it fails immediately
rather than spending requests to be told no.

---

## Proxies

One `--proxy` applies to every tier, media downloads included. Available on
every command.

```bash
bowerbird profile nasa --proxy http://host:8080
bowerbird profile nasa --proxy socks5://host:1080
bowerbird profile nasa --proxy http://user:pass@host:8080
```

Schemes: `http`, `https`, `socks5`, `socks5h`. The URL is validated
when the config is built, so a typo fails on one line before any request goes
out rather than as an `ImportError` three tiers into a run.

`socks4` is deliberately not accepted. libcurl speaks it, so tier 2 would work,
but httpx wires only `socks5`/`socks5h` — a socks4 URL would pass validation
and then crash on the default tier. The accepted set is the intersection of
what every tier supports, not the union.

SOCKS support comes from [`socksio`](https://github.com/sethmlarson/socksio)
for the httpx tiers (via the `httpx[socks]` extra) and from libcurl for the
curl_cffi tier. PySocks is not used.

Credentials in the URL work on all four tiers. The browser tier splits them
into Playwright's separate `username`/`password` fields, because Chromium's
`--proxy-server` ignores credentials embedded in a URL and would 407 on every
request.

`HTTP_PROXY` / `HTTPS_PROXY` are honoured by the HTTP tiers even without
`--proxy`, since httpx and curl_cffi both trust the environment. The browser
tier does **not** read them — Chromium needs the flag. Pass `--proxy`
explicitly if a run might escalate to tier 4.

No rotation: it is one proxy for the run. A pool means implementing
`Transport` and adding it to `default_transport_factory`.

---

## Rate limiting

Adaptive, closed-loop pacing. Start conservative, multiply the gap on any
throttling response, decay it back only after a run of clean ones, honour
`Retry-After` when the server sends it, and jitter every delay so requests
never form a clean cadence.

API calls and media downloads pace on separate budgets, because the CDN is a
different host with a different quota. Downloads start unthrottled and tighten
only if the CDN itself pushes back; pacing them on the API's 1.5s gap would
serialise thousands of files behind a limit that does not apply to them.

Retries use exponential backoff with full jitter and stop at 5 attempts.
Authentication, parsing and domain errors are never retried — they need a
different tier or different code, not patience.

---

## Architecture

```
CLI (typer)
  │  builds ScraperConfig, wires components, owns nothing
  ▼
InstagramScraper ── StateStore (resume) ── MetadataStore (jsonl)
  │
  ├─ InstagramClient
  │     ├─ CachingUserResolver ── Cache (username → user id, forever)
  │     ├─ TimelinePaginator ──── async iterator, one page in memory
  │     │     └─ extractors ───── raw payload → Media
  │     └─ EscalatingTransportProvider
  │           4 tiers + RateLimiter (API budget) + RetryPolicy
  │
  └─ MediaDownloader ────── worker pool, streaming, atomic writes
        └─ its own cheap transport + its own RateLimiter (CDN budget)
```

```
bowerbird/
  api/          endpoints, transports, escalation ladder, client, resolver
  auth/         cookie loading, persistence, import/export
  browser/      tier 4; imported lazily
  cache/        file and memory caches
  downloader/   streaming, resumable, atomic downloads
  extractors/   GraphQL, /api/v1 and HTML parsers
  models/       Media, Profile, Page, configuration
  pagination/   cursored async iterators
  retry/        backoff policy, adaptive rate limiter
  storage/      metadata (jsonl) and resume state
  utils/        logging, files, urls, shortcode maths
  cli/          typer app
  interfaces.py 11 Protocols; every component is injected
  errors.py     exception hierarchy
```

Design rules that are actually enforced:

- **No global state.** Every component takes its collaborators in its
  constructor. `interfaces.py` defines them as `Protocol`s, so tests pass
  plain fakes without inheriting anything.
- **Transport errors are translated at the boundary.** Nothing above `api/`
  imports curl_cffi or playwright. The one exception is `downloader/`, which
  catches `httpx.HTTPError` mid-stream because it drives the httpx streaming
  response directly; it re-raises as `NetworkError` immediately.
- **Instagram's field names live only in `extractors/`.** A payload change
  stops there instead of reaching storage, the downloader or the CLI.
- **Every file under 400 lines, fully typed, documented.**

---

## Tests

```bash
pytest                          # 232 tests, ~0.6s, no network
pytest --cov                    # with coverage
ruff check . && ruff format --check .
```

Unit, integration, mocked-HTTP, retry, pagination, downloader, CLI. The
network is faked at the httpx transport boundary — everything above it is the
code under test, including the full resolve → paginate → download → store
pipeline, resume across interrupted runs, and tier escalation.

---

## Field notes

Things verified against live Instagram rather than assumed:

- `doc_id=7950326061742207` serves the timeline anonymously. `first` is capped
  server-side at 12 no matter what you ask for.
- Timeline nodes carry **no** `owner` object; the owner id comes from the
  request. Carousel children carry no timestamp, caption or shortcode — they
  inherit the parent's.
- Video nodes report no dimensions for the stream URL, so "highest resolution"
  would silently pick the poster frame. Video streams are flagged and always win.
- CDN URLs are signed and expire; a 403 there means re-fetch metadata, not
  that the post is gone.
- Logged-out post pages embed no media JSON at all, rendered or not — hence
  the shortcode → media-id conversion and the authenticated fallback.

---

## Extension points

- **New collection** (tagged posts, saved, feeds): implement `MediaPaginator`
  and reuse `TimelinePaginator`'s cursor loop.
- **New transport** (a proxy pool, a third-party API): implement `Transport`
  and add it to `default_transport_factory`.
- **New storage** (Postgres, S3, Parquet): implement `MetadataStore`; the
  scraper only calls `save`/`has`/`aclose`.
- **New resolver strategy**: implement `ResolutionStrategy` and prepend it to
  the chain; caching and fallback come for free.
- **Distributed runs**: implement `StateStore` against Redis; the cursor is
  the only shared state.
- **Retired `doc_id`**: add the replacement to `DOC_ID_TIMELINE_FALLBACKS`.
  The paginator tries fallbacks and locks onto the first that parses.

---

## Legal

**Automated scraping is against Instagram's Terms of Service**, whether or not
you are logged in. This tool does not change that, and no amount of TLS
impersonation makes it allowed. Read that as the real constraint, not
boilerplate.

Risk is not uniform across the tiers. The anonymous tier reading public
profiles is the low end. The moment you pass `--cookies` you are attaching a
real account to automated traffic, and accounts get restricted or banned for
exactly that — use one you can afford to lose.

Public data only, and only what a logged-out browser can already see. Respect
Instagram's Terms of Service, robots directives, applicable law and the
privacy of the people whose posts you are downloading. You are responsible for
how you use this.
