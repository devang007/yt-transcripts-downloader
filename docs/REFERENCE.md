# yt-tx — full reference

Enumerate every video on a list of channels, persist metadata to MySQL, download
the transcript where one exists, and record enough state that a re-run never
redoes completed work. Videos with no transcript are left open with
`needs_audio=1` for a later audio→text pass.

A single-page web UI drives the whole thing: start/pause/stop runs, edit every
knob, watch live logs, and browse transcripts channel by channel.

## Quick start

```bash
python -m venv .venv && .venv/bin/pip install -e '.[dev]'

cp .env.example .env          # set MYSQL_PASSWORD and YOUTUBE_API_KEY
$EDITOR config.yaml           # or config.local.yaml, which takes precedence

.venv/bin/yt-tx init          # create database + schema, seed settings
.venv/bin/yt-tx channels add @3blue1brown @lexfridman
.venv/bin/yt-tx serve         # http://127.0.0.1:8000
```

Or headless:

```bash
yt-tx run                     # discover + fetch (add --hydrate for metadata)
yt-tx run --incremental       # what a daily cron should use
yt-tx stats
```

## Requirements

- **Python 3.11+**
- **MySQL 8.0.20+.** Not negotiable, and not substitutable:
  - `SELECT … FOR UPDATE SKIP LOCKED` (8.0) is what makes concurrent claiming
    safe. Two workers never collide, and a worker killed mid-batch simply lets
    its lease lapse.
  - `INSERT … AS new ON DUPLICATE KEY UPDATE` row aliases (8.0.20).
  - `utf8mb4` throughout. Titles are full of emoji; utf8mb3 hard-errors with
    *Incorrect string value* partway through the first large channel.
- A YouTube Data API key is optional but strongly recommended. Without one,
  enumeration and metadata fall back to yt-dlp: one request per video instead of
  one per fifty.

## How state works

`videos.status` is the single source of truth for what remains.

| Status | Meaning | Picked up by a plain re-run? |
|---|---|---|
| `discovered` | Enumerated, metadata not fetched | yes — fetched directly, or hydrated first with `--hydrate` |
| `metadata_ok` | Metadata present, transcript not attempted | yes — fetch |
| `transcript_ok` | ≥1 transcript stored | **no** |
| `no_transcript` | Captions disabled or absent; `needs_audio=1` | only when `recheck_after` matures, or `--force-recheck` |
| `lang_missing` | Captions exist, none in configured languages | only if languages change, or `--force-recheck` |
| `unavailable` | Private, deleted, members-only, region-locked | no |
| `age_restricted` | Needs authenticated cookies | only with cookies configured |
| `skipped` | Upcoming, live now, or filtered by duration/type | re-evaluated when an `upcoming` date passes |
| `retry` | Transient failure; `next_attempt_at` set | yes, when due |
| `failed` | Retries exhausted | only with `yt-tx retry --failed` |

Illegal transitions raise rather than log. A silent illegal transition is how a
video becomes permanently invisible to every stage.

### Disabling a channel

`is_enabled = 0` means **the pipeline does nothing for that channel**: it is not
enumerated, not hydrated, and not fetched. The existing rows stay exactly as they
are, and re-enabling picks up where it left off — it is a pause switch, not a
delete.

Naming a channel explicitly overrides it. `--channel <id>` (and the UI's
per-channel Start, which sends the same argument) works on a disabled channel,
because typing out one channel id is a deliberate act.

### The one invariant that matters most

**A block is a fetcher condition, not a video condition.** When YouTube refuses
our IP, every video in flight looks broken. If that set a per-video status, one
bad afternoon would mark thousands of perfectly transcribable videos `failed`,
indistinguishable from genuinely caption-less ones, with no way to find them
again.

So a block:

- records `outcome='blocked'` in `fetch_attempts`,
- releases the lease and leaves `status` and `attempts` **untouched**,
- trips the circuit breaker, which pauses all workers and cools down
  5 → 10 → 20 → 40 → 60 minutes, retesting with a single canary request,
- and after `max_reopens` failed retests exits with `exit_reason='circuit_open'`
  and a non-zero code, everything still queued.

This is enforced structurally (`CircuitBreaker` has no video id and no database
handle) and tested end to end.

## Commands

```
yt-tx init [--reseed]                   create schema, seed settings, make dirs
yt-tx serve [--host --port]             FastAPI + UI
yt-tx channels add <ref>... | --file    resolve and store channels
yt-tx channels list | remove <id>
yt-tx discover [--channel] [--incremental] [--limit]
yt-tx hydrate  [--channel] [--limit]
yt-tx fetch    [--channel] [--limit] [--concurrency] [--dry-run] [--hydrate]
yt-tx run      [--incremental] [--hydrate]        discover + fetch
yt-tx retry    [--failed] [--force-recheck] [--age-restricted]
yt-tx stats    [--channel] [--json]
yt-tx control  [--pause|--resume|--stop] [--concurrency N] [--rps N]
yt-tx audio-queue --out queue.jsonl     export needs_audio=1 for phase 2
yt-tx export   --out DIR --format jsonl|txt|csv [--per-video]
yt-tx doctor   [--fix] [--deep]         db↔disk reconciliation
yt-tx prune    --older-than 30d         fetch_attempts + old log files
yt-tx fulltext [--build|--drop]         manage the transcript FULLTEXT index
```

Every networked command is Ctrl-C safe: it stops claiming, drains in-flight work,
commits, closes the `runs` row with `exit_reason='interrupted'`, and exits 130.

### Export order

Exports run **oldest upload first**, then everything with no `published_at`, in
fetch order. Undated rows go last rather than first (MySQL's own NULLS FIRST) so
the chronological run is not buried behind them.

That tail is usually the bulk of a corpus: without a Data API key, enumeration
records no publish date (`discover` stores only id, channel and title), and the
metadata stage that would fill it is skipped by default. If chronological order
across everything matters, harvest with `--hydrate` or configure a key.

The UI's **download all transcripts (.txt)** button is this same stream — one
plain-text file, every transcript, scoped to the open channel when you are
drilled into one.

Exit codes: `0` completed / quota exhausted / stopped, `130` interrupted,
`4` circuit open (work remains), `1` crashed.

### The metadata stage is opt-in

`fetch_video` reads exactly three things off a video row: `video_id`,
`channel_id` and `attempts`. Metadata is *not* a precondition for captions, so
**`run` and `fetch` skip hydration by default**: they claim `discovered` videos
directly, and `discovered` transitions straight to `transcript_ok`.

```bash
yt-tx run                 # discover, then fetch. No metadata pass.
yt-tx run --hydrate       # the old three-stage behaviour
yt-tx hydrate             # or run the stage on its own, whenever
```

The default is what it is because of the no-API-key case. Hydration then costs
one yt-dlp request per video — roughly 50/min, so 15k videos is five hours
*before the first transcript lands*, since `run` finishes each stage before
starting the next. A Start button that downloads nothing for five hours is
indistinguishable from a broken one.

What the default gives up:

- `duration_seconds`, `view_count`, `description`, tags, `is_short`,
  `was_livestream`, `category_id` stay NULL. **`title` and `published_at` do
  not** — enumeration already stores those, where the channel tab provides them.
- The skip rules never run, so `include_streams`, `include_shorts` and
  `max_duration_seconds` have no effect. Premieres and live videos get a fetch
  attempt and land in `no_transcript`.
- With `published_at` NULL, `recheck_after` is never scheduled, so those
  `no_transcript` rows wait for an explicit `yt-tx retry --force-recheck`.
- Phase 2 loses its GPU-time estimate: `yt-tx audio-queue` sorts on
  `duration_seconds`.

**This is currently a one-way door.** `hydrate` selects on
`status = 'discovered'`, so a video that skipped straight to `transcript_ok` is
never revisited and its metadata columns stay NULL for good. If you want the
durations eventually, run with `--hydrate` from the start, or add a backfill
path that selects on `duration_seconds IS NULL` and preserves the existing
status.

In the UI this is the **hydrate metadata** checkbox next to Start, off by
default.

## Knob tiers

Every knob in the UI is labelled with when it takes effect. A rate-limit slider
that silently does nothing until restart is worse than no slider.

- ⚡ **Live** — `concurrency`, `requests_per_second`. Written to
  `runtime_control`; a running worker picks them up within 2 seconds.
- ↻ **Next run** — languages, retries, circuit-breaker params, shorts/streams,
  max duration, fetcher backend, proxy, cookies. Stored in `settings`.
- 🔒 **Secret** — the API key. Write-only; rendered as `••••1a2b` and never
  returned in full by `GET /api/settings`.

`config.yaml` seeds `settings` on `init`. After that **the UI is the source of
truth** and edits to the YAML are ignored unless you run `yt-tx init --reseed`.

## Operating notes

**Datacenter IPs get blocked fast** on the caption endpoint — expect it within a
few hundred requests on a VPS. Both fetchers take a proxy; rotating *residential*
proxies are the practical fix at scale. Set `proxy` in the UI, or `cookies_file`
for age-restricted content.

**A run that reports `completed` with zero progress is worth a look.** Check
`counts_json.blocked` and the breaker state in `runs`, not just the exit code —
hitting `--limit` while blocked exits 0 legitimately.

**Quota.** `playlistItems.list` and `videos.list` cost 1 unit; `search.list`
costs 100 and is therefore never called — `YouTubeAPI` has no method for it. The
ledger is keyed on a *Pacific* date, because that is when Google resets. Runs
stop gracefully at 90% of the daily budget.

**Incremental mode reads RSS first** (`feeds/videos.xml`): ~15 newest videos, zero
quota, no key. If nothing is newer than `channels.newest_published_at`, the
channel is skipped without spending a unit. This is what a daily cron should use.

**Bulk loads.** A FULLTEXT index roughly triples insert cost. For a first pass
over hundreds of thousands of videos: `yt-tx fulltext --drop`, harvest, then
`yt-tx fulltext --build`. Also note `innodb_ft_min_token_size` defaults to 3, so
shorter words are unsearchable until you lower it and rebuild.

**`fetch_attempts` grows by one row per attempt per video.** Run
`yt-tx prune --older-than 30d` from day one rather than discovering it at 40
million rows.

**Disk.** Transcripts gzip to ~5–15 KB each; 100k videos ≈ 1 GB. MySQL itself is
dominated by `plaintext` and `description` — budget ~50–100 KB/video.

**Storage ordering.** The gzip is written, fsynced, and its directory fsynced
*before* the database row is committed. An orphan file is harmless and
reclaimable; a row pointing at a missing file is corruption. `doctor` reconciles
both directions and `--fix` deletes rows whose file is gone so the video can be
re-fetched.

### Behind nginx

SSE needs buffering off, or the log console stays empty until the run ends:

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_buffering off;
    proxy_read_timeout 3600s;
}
```

The app already sends `X-Accel-Buffering: no`.

### Exposing the UI

Binds `127.0.0.1` by default. The settings page holds an API key and a cookies
path, so setting `web.host` to anything routable **requires** `web.auth_token`;
without it the app refuses to start rather than warning.

Runs are spawned with a list argv and `shell=False`. Channel references come from
user input and land next to a command line; they are never interpolated into a
shell string.

## Architecture

```
browser ──REST+SSE──► FastAPI :8000 ──spawn──► worker process (yt-tx fetch …)
   index.html          (control plane)              │            │
                                                    ▼            ▼
                                                  MySQL    logs/run-<id>.jsonl
                                                             data/transcripts/
```

**The pipeline never runs inside the web process.** A hot reload or a browser
disconnect must not kill a four-hour run. The API spawns a subprocess, records
its PID, and afterwards communicates only through MySQL (`runtime_control`) and
the log file. On startup it finds `runs` rows whose PID is gone, marks them
`crashed`, and releases their leases — otherwise the UI shows a phantom RUNNING
forever.

Modules are flat and deliberately shallow:

| File | Role |
|---|---|
| `settings.py` | knob registry (drives DB seeding, API validation, and the UI), bootstrap YAML |
| `db.py` | engine, connection hygiene, deadlock retry, schema DDL |
| `states.py` | status vocabulary and legal transitions |
| `classify.py` | exception → (outcome, status, retryable). The reason this survives |
| `limiter.py` | token bucket, circuit breaker, full-jitter backoff |
| `repo.py` | all SQL: claiming, upserts, stats |
| `youtube_api.py` | Data API v3 client, quota ledger, RSS shortcut |
| `discover.py` | reference parsing, resolution, enumeration with cursor resume |
| `hydrate.py` | batch metadata, skip rules |
| `fetch.py` | `TranscriptFetcher` protocol + two backends, durable storage |
| `worker.py` | thread pool, live control, orderly shutdown |
| `api.py` | REST + SSE, subprocess supervision |
| `cli.py` | typer commands |
| `static/index.html` | the whole UI. Alpine + Tailwind from CDN, no build step |

`TranscriptFetcher` is the only abstraction here with more than one
implementation, which is why it exists at all.

## Tests

```bash
pytest                              # 369 tests; MySQL-backed ones skip without a DSN
export YT_TX_TEST_DSN='mysql+pymysql://user:pass@127.0.0.1:3306/yt_tx_test?charset=utf8mb4'
pytest                              # full suite
pytest -m integration               # opt-in, hits the live network
mypy --strict
```

A disposable server is provided:

```bash
docker compose up -d db             # MySQL 8 on port 3307, correctly configured
```

Notes on how the suite is built:

- **Never run against SQLite and hope.** `SKIP LOCKED`, row-alias upserts, ENUM
  coercion and FULLTEXT have no SQLite equivalent, and those four mechanisms are
  precisely what correctness rests on. A green SQLite suite would be reassuring
  and wrong.
- **No network in unit tests.** An autouse fixture fails any test that opens a
  socket (except MySQL). Integration tests opt out via their marker and are
  deselected by default.
- **The classifier is tested against real exception instances** from the pinned
  `youtube-transcript-api` and `yt-dlp`, not stand-ins. Bumping either version
  must fail loudly in `tests/test_classify.py` rather than silently reclassifying
  thousands of videos.

## Pinned dependencies

All exact. Two are load-bearing and worth knowing about:

- **`yt-dlp`** — review on a schedule. YouTube changes its channel-page markup
  often, and a stale yt-dlp does not error; it returns *zero entries* and
  enumeration silently finds nothing. If discovery starts reporting empty tabs,
  bump this first.
- **`click==8.1.8`** — held below 8.2, which changed
  `Parameter.make_metavar()` to require a `ctx` argument that `typer` 0.15.1 does
  not pass. Unpin only together with typer.

`cryptography` is a hard requirement, not an extra: MySQL 8 defaults to
`caching_sha2_password` and PyMySQL cannot complete that handshake without it.

## Phase 2 (designed, not built)

`needs_audio=1` is the queue; `yt-tx audio-queue` exports it with
`duration_seconds` so a consumer can estimate GPU time and hard-skip long videos
*before* downloading anything. At ~30 MB/hour for 64 kbps mono opus, a
2,000-video channel averaging 20 minutes is ~200 GB of intermediate audio —
delete it after transcription and keep only text.

Whisper output lands in the existing `transcripts` table with `kind='whisper'`,
so nothing downstream changes. Audio downloads will need their own, much
stricter, token bucket.

## Licence and legal

Transcripts are copyrighted material owned by the uploader. Fine for personal
analysis; check YouTube's Terms of Service before redistributing or publishing a
derived corpus.
