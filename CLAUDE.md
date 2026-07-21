# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# install (editable, with dev tools)
uv venv && uv pip install -e ".[audio,stt,batch,dev]"

# lint + test (the CI gate)
ruff check src tests && pytest

# single test file / single test
pytest tests/test_identify.py
pytest tests/test_identify.py::test_match_catalog_finds_alias -v

# run the CLI without installing the console script
python -m soopts <subcommand> ...
```

There is no separate build step — this is a pure-Python package (`setuptools`, src layout).

## Architecture

### Two entry points into the same lower-level modules

- **Manual CLI pipeline** (`cli.py`): a human runs `collect` → `songs`/`clips` one VOD at a time,
  reviewing/editing `clips.json` between steps. `clips.json` holds song spans + lyrics, not files.
- **Batch pipeline** (`batch.py`, driven by `soopts daily`): **timeline-parse only, no media.** It
  fetches comments, parses the fan timeline into songs (`analyzers/comment_timeline.py`), matches each
  by title/artist against the catalog (`analyzers/identify.py`), and writes `performances` to Supabase
  (`db.py`). It does **not** download HLS, run `inaSpeechSegmenter`, or STT — those live only in the
  manual CLI now. A VOD without a timeline is marked `manual` and left for local processing.
- Shared low-level helpers that both entry points need (e.g. `collector/media.py`'s `map_to_part`)
  live in the module layer, not in `cli.py`. The manual CLI (`songs`/`clips`) still uses the download/
  segment/STT helpers; the batch path no longer touches them.
- **Local processing of no-timeline VODs — `soopts ingest <id> spans.json`** → `batch.ingest_vod`.
  A human runs the external [claude-video](https://github.com/bradautomates/claude-video) `/watch`
  skill (or the `vod-video-ingest` skill) locally on the VOD to find sung segments + on-screen titles,
  emitting song spans (`{"songs": [{start_s, end_s, title?, artist?, lyrics?}]}`). `ingest` matches
  each against the catalog (reusing `resolve_song_match`/`identify_song` via the shared `_record_songs`
  core) and records `performances`. This **writes to the DB** (unlike `songs`/`clips`, local files
  only) and promotes the VOD out of `manual`. No download/STT/segment — Claude already did the finding.

### Config system (`config.py`)

One dataclass per concern (`Endpoints`, `CollectorConfig`, `AudioConfig`, `SttConfig`, `ClipConfig`,
`StationConfig`, `CommentConfig`), assembled into `Config`. `load_config()` reads `soopts.toml` and
merges only the keys present per section (`_build_section` drops unknown keys) — so partial overrides
in `soopts.toml` don't require repeating every field. `work_root` can also be overridden via CLI flag.

### Work directory as cache (`paths.py`)

Every VOD gets `work/{vod_id}/` (`WorkPaths`): `meta.json`, `chat.jsonl`, `raw/` (chat XML cache),
`audio_segmentation.json` (expensive STT segmentation cache), `songs.json`/`songs_{id}.txt`, and
`clips/` (downloaded region files — inputs to boundary-detection and STT, not media output).
Steps check these files before re-fetching/re-computing; `--force` bypasses the cache. The batch
pipeline uses the exact same layout, but treats it as a pure cache — the truth is Supabase (see below).

### Lazy imports for heavy/optional dependencies

`inaSpeechSegmenter`, `supabase`, `groq`, and `rapidfuzz` are **only imported inside the functions
that use them**, never at module top level.
This keeps `import soopts` cheap and lets `pyproject.toml`'s optional-dependency extras (`audio`, `stt`,
`batch`) actually be optional — a plain `pip install soopts` with no extras can still run
`collect`. Preserve this pattern when adding new functionality that touches one of these libraries.

### The Supabase boundary (`db.py`)

`db.py` is the *only* module that talks to Supabase. The schema (`vods`, `performances`, `song_aliases`,
and the read-only `songs` catalog) is owned by a separate private repo
(`singgyul_sing_book`) — this codebase only consumes it and never creates migrations. `songs` rows are
never created from this repo; unmatched songs always land as `needs_review` for a human to resolve in
the separate review UI. `vods.status` (per-VOD progress) and `performances.identify_status` (per-song
review state) are the actual state machine — treat them as the source of truth, not local files
(see next point).
YouTube-era objects were dropped on 2026-07-18 (`performances.youtube_video_id`/`synced_at`/
`clip_status`, the `youtube_deletion_queue` table) once both repos had stopped referencing them.
`song_performance_counts` was recreated without its `youtube_video_id IS NOT NULL` filter — every
performance is now reachable via deep link, so the old "only count uploaded ones" condition no longer
made sense (3 songs → 47).

### Volatile-runner design (`batch.py`)

GitHub-hosted runners don't persist disk between workflow runs, so no pipeline stage may depend on
files surviving between `daily` invocations. The truth is Supabase: `vods.status` for how far a VOD
got, and one `performances` row per detected song (`start_s`/`end_s`/`identify_status`). Local `work/`
files are a cache that may vanish at any time. Don't reintroduce state that only lives on the runner's disk.

The deliverable is the **timestamp**, not a media file. `song_link(cfg, title_no, start_s)` builds the
SOOP deep link that viewers actually follow, computed from DB columns alone — nothing is uploaded
anywhere, so there is no per-song artifact to keep in sync or clean up.

**The batch path touches no media at all.** Since the runner only parses the comment timeline and
writes DB rows, there is no download, no `inaSpeechSegmenter`, no STT, no ffmpeg on the `daily` path
— a run is comments-fetch + Groq-identify + DB-write, on the order of seconds per VOD. `daily.yml`
installs only `.[batch]` (no `audio`/`stt`/ffmpeg/yt-dlp). The HLS downloader + segmenter + Whisper
still exist for the **manual CLI** (`songs`/`clips`) and are lazy-imported there.

A run killed mid-VOD (timeout, cancel, runner reset) leaves `vods.status = 'pending'` because
`mark_vod` never runs. `select_targets()` therefore treats **both** `failed` and `pending` as
retryable. Any `pending` seen at selection time is necessarily stale: `concurrency: soopts-daily`
forbids overlapping runs, and within one run selection happens once, before processing.
Because retries are routine, **reprocessing must be idempotent**. `insert_performances` upserts on
`(vod_id, start_s)` with `ignore_duplicates`, and `_process_vod` calls
`clear_machine_performances()` first to drop the previous run's rows for that VOD. Rows with
`identify_status = 'confirmed'` are spared — a human decided those and the machine cannot recreate
them. Before this, every reprocess appended a fresh copy of each span (201142227 ended up with two
sets of identical `start_s`/`end_s`).

Retrying `pending` bumps `retry_count` inside `select_targets` — `mark_vod` only bumps it on
`failed`, so without this a VOD that kills the runner every time would never reach `MAX_RETRIES`
and would block the queue forever.

Every row `select_targets` returns must carry the **same keys** (`_vod_row` enforces this).
PostgREST builds the column list from the union of keys across the batch and writes an explicit
NULL wherever a row lacks one — so mixing a retry row (rich, straight from `select *`) with a new
row (sparse) made `retry_count` NULL on the new row and Postgres rejected the whole batch with
`23502`. Never build the upsert dicts per-row-shape.

### Candidate detection (`_find_candidates`, `_process_vod`, `analyzers/comment_timeline.py`)

The runner enumerates a VOD's songs by **marker-parsing the fan comment timeline** — no LLM, no
media. `parse_song_timeline()` (pure, regex) pulls lines of the form `HH:MM:SS 🎤 아티스트 - 제목`.
Only the **🎤** marker counts — it means the BJ actually sang. `🎵/🎶` (guest/collab/concert
performances, or clip references) and **icon-less** lines (older timeline format) are deliberately
**not** counted: whether they're a real BJ performance is ambiguous, and a wrong guess just adds
noise. Clip/teaser references (`편집본`/`클립이슈`/`티저`/`뮤비`/…) are denylisted.

`timeline_songs_to_spans()` turns each 🎤 song into a span: `start = timestamp`, `end = next song's
start` capped at 6 min (deep-links only need the start; the cap keeps inter-song talk out of the
"length"). `_record_songs()` then matches each by title/artist (`resolve_song_match`) and writes
`performances`. This replaced the old Groq `extract_song_timeline` (missed songs — 201933359 got 2 of
12) + per-region download + `inaSpeechSegmenter` + Whisper + Groq lyric-guess pipeline.

**No timeline (0 🎤 songs) → `manual`, not processed.** `_process_vod` returns `{"no_timeline": True}`
and `run_daily` marks `vods.status = 'manual'`. This covers game-only streams, all-🎵 concert VODs,
and old icon-less timelines alike — anything the marker parser can't confidently read goes local.
`manual` is terminal for the queue: `fetch_retryable` pulls only `failed`/`pending`, and
`select_targets` skips any title_no already in `existing_by_no`, so a `manual` VOD is never retried
or re-picked. A human processes it via `soopts ingest` (claude-video), promoting it to `analyzed`/`done`.
Reprocessing is idempotent: `clear_machine_performances()` drops the prior run's rows (sparing
`confirmed`) before re-inserting.

**Auditing processed VODs — the `vod-audit` skill, not code.** The 🎤-only rule is safe (no false
positives) but incomplete: a 🎤+🎵 mixed VOD records only its 🎤 songs and is marked `analyzed`, so the
🎵 ones are silently missed. Deciding whether such a VOD (or a game/chat/teaser timeline) was handled
right is a judgment call, so it lives in `.claude/skills/vod-audit/`, where **Claude reads the raw
comments and decides**. Code only exposes deterministic primitives the skill orchestrates:
- `soopts vods --status analyzed,done --json` (`db.fetch_vods_by_status` + perf counts) — worklist.
- `soopts comments <id> --json` (`fetch_comments`, no Groq) — raw comments, the judgment input.
- `soopts set-manual <id> [--clear-machine]` (`batch.set_vod_manual`) — apply one Claude-approved
  decision: clear machine `performances` (sparing `confirmed`) and set `status = 'manual'`.

The skill never applies destructive changes without user approval. Don't re-add a code-based `recheck`
that auto-judges and deletes — that's what this replaced.

### VOD selection (`_select_vods` in `batch.py`, `select_targets` in `db.py`)

Priority is **retry > new > backfill**, with no floor — SOOP list expiry is the natural bottom.

`fetch_retryable()` pulls `failed`/`pending` rows (under `MAX_RETRIES`) straight from the DB, so a
`pending` that scrolled out of the recent-list window still gets retried — the old fixed
`count * 3` window silently stranded them. New and backfill both come from paging the SOOP list
newest-first via `iter_vod_pages()`: `_select_vods` keeps paging and skipping already-processed VODs
until it has `count` targets or the list runs out, so when the newest are all done it walks backward
into history automatically. No separate "new vs backfill" branch — newest-first traversal makes that
ordering fall out. `select_targets` is the pure core (retry list + candidate list → picks); the impure
paging loop lives in `batch.py` to keep `db.py` Supabase-only and `vod_list.py` SOOP-only.

### Quality gate (`quality_warning`)

`quality_warning` measures the STT success rate (`stt_ok / stt_attempted`) and raises below
`cfg.stt.min_success_rate`. With the batch path no longer running STT, `stt_attempted` is always 0 on
`daily`, so the gate is a no-op there — it's kept (harmless) for the manual CLI and any future audio
path. Every timeline song carries a title/artist hint, so batch auto-matches are all `hint_available`
and `lyrics_only` stays 0 by design.

### GitHub Actions workflows

- `verify-env.yml` (manual): confirms SOOP's API/streams are reachable from a hosted-runner IP before
  relying on `daily.yml`. If it starts failing, only `runs-on` needs to change to a
  self-hosted runner — nothing else.
- `daily.yml`: scheduled every 6h (04/10/16/22 KST) + `workflow_dispatch`. It pipes
  `soopts ... | tee *.log` and relies
  on the exit code to detect failure — any `run:` step doing this **must** start with
  `set -o pipefail`, or a crash in `soopts` gets masked by `tee`'s always-zero exit status (this has
  bitten this repo once already).

### Testing philosophy

Tests exercise pure functions only — no network, no DB, no ML model loading. One file per module
under test (`tests/test_<module>.py`). Functions that call out to Groq/Supabase are kept
thin and tested by isolating the pure logic around them (e.g. `test_db.py` tests `select_targets`'s
row-filtering logic directly, without touching Supabase; `test_stt.py` passes a fake client object
into `_transcribe_best` to test the language-selection logic without a real API call). Fixtures
under `tests/fixtures/` that capture real SOOP API/chat responses are anonymized (no real viewer
usernames) — keep it that way when adding new fixtures.
