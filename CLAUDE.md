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
- **Batch pipeline** (`batch.py`, driven by `soopts daily`): the same detection/clip/
  identify building blocks (`analyzers/audio_analyzer.py`, `collector/media.py`, `export/clips.py`,
  `analyzers/stt.py`, `analyzers/identify.py`) are composed directly, with Supabase (`db.py`) standing
  in for the human review step. `batch.py` does not reuse `cli.py`'s `_produce_clips`/`_songs_slice`
  helpers (those are argparse/print-oriented for interactive review) — it re-implements the same
  region → download → detect-boundary → transcribe flow against a `vod_row` from the DB instead of a CLI `args`
  namespace.
- Shared low-level helpers that both entry points need (e.g. `collector/media.py`'s `map_to_part`,
  which maps a global time range onto the right HLS part + local offsets) live in the module layer,
  not in `cli.py`, specifically so `batch.py` can call them without importing the CLI.

### Config system (`config.py`)

One dataclass per concern (`Endpoints`, `CollectorConfig`, `AudioConfig`, `SttConfig`, `ClipConfig`,
`StationConfig`, `CommentConfig`), assembled into `Config`. `load_config()` reads `soopts.toml` and
merges only the keys present per section (`_build_section` drops unknown keys) — so partial overrides
in `soopts.toml` don't require repeating every field. `work_root` can also be overridden via CLI flag.

### Work directory as cache (`paths.py`)

Every VOD gets `work/{vod_id}/` (`WorkPaths`): `meta.json`, `chat.jsonl`, `raw/` (chat XML cache),
`audio_segmentation.json` (expensive STT segmentation cache), `songs.json`/`songs_{id}.txt`, `clips/`.
Steps check these files before re-fetching/re-computing; `--force` bypasses the cache. The batch
pipeline uses the exact same layout, which is what makes clip-file resumability possible (see below).

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
the separate review UI. `vods.status`/`performances.clip_status`/`performances.identify_status` are the
actual state machine — treat them as the source of truth, not local files (see next point).
The schema still carries YouTube-era columns (`performances.youtube_video_id`, the
`youtube_deletion_queue` table). This repo no longer reads or writes them — the upload path was
removed — but **do not drop them or clear existing values**: they are the only record of which
already-uploaded video corresponds to which performance, and the other repo still owns the schema.

### Volatile-runner design (`batch.py`)

GitHub-hosted runners don't persist disk between workflow runs, so no pipeline stage may depend on
files surviving between `daily` invocations. The truth is Supabase: `vods.status` for how far a VOD
got, `performances.clip_status` plus `start_s`/`end_s` for each detected song. Local `work/` files are
a cache that may vanish at any time. Don't reintroduce state that only lives in the runner's filesystem.

The deliverable is the **timestamp**, not a media file. `song_link(cfg, title_no, start_s)` builds the
SOOP deep link that viewers actually follow, computed from DB columns alone — nothing is uploaded
anywhere, so there is no per-song artifact to keep in sync or clean up.

**No video is produced.** `detect_song_span()` returns boundary times, and STT extracts just that
range from the downloaded region file (`_transcribe_best(..., start=, dur=)`). Re-encoding clips with
ffmpeg used to be 76% of total runtime (6.6 min per song); removing it took a VOD from ~5.5h to ~20min.
`cfg.clip.quality` is deliberately the *lowest* rendition (`hls-hd`, 540p): all three renditions carry
the same AAC audio, and audio is all the segmenter and Whisper ever see, so the higher ones only cost
download time. Don't raise it "for quality" — there is no video output to have quality.

A run killed mid-VOD (timeout, cancel, runner reset) leaves `vods.status = 'pending'` because
`mark_vod` never runs. `select_pending()` therefore treats **both** `failed` and `pending` as
retryable. Any `pending` seen at selection time is necessarily stale: `concurrency: soopts-daily`
forbids overlapping runs, and within one run selection happens once, before processing.
Retrying `pending` bumps `retry_count` inside `select_pending` — `mark_vod` only bumps it on
`failed`, so without this a VOD that kills the runner every time would never reach `MAX_RETRIES`
and would block the queue forever.

### Schema debt owned by the other repo

`singgyul_sing_book` owns the schema, so these can only be cleaned up there — this repo just stops
using them:
- `performances.youtube_video_id`, `youtube_deletion_queue` — dead since the upload path was removed.
  Keep the *data* (only record of past uploads); the columns can be dropped once nothing reads them.
- `performances.clip_status` — dead. With the upload queue gone every row ended up `'clipped'`, so the
  column carried no information; this repo no longer writes it (that also removed a per-song DB
  round-trip). Review state lives in `identify_status`. Safe to drop **after** the admin UI stops
  reading it — the drop order matters: code must stop writing a column before it is dropped, or
  PostgREST fails every insert with `PGRST204` and the whole VOD is marked failed.

### GitHub Actions workflows

- `verify-env.yml` (manual): confirms SOOP's API/streams are reachable from a hosted-runner IP before
  relying on `daily.yml`. If it starts failing, only `runs-on` needs to change to a
  self-hosted runner — nothing else.
- `daily.yml`: scheduled + `workflow_dispatch` (schedule currently commented out). It pipes
  `soopts ... | tee *.log` and relies
  on the exit code to detect failure — any `run:` step doing this **must** start with
  `set -o pipefail`, or a crash in `soopts` gets masked by `tee`'s always-zero exit status (this has
  bitten this repo once already).

### Testing philosophy

Tests exercise pure functions only — no network, no DB, no ML model loading. One file per module
under test (`tests/test_<module>.py`). Functions that call out to Groq/Supabase are kept
thin and tested by isolating the pure logic around them (e.g. `test_db.py` tests `select_pending`'s
row-filtering logic directly, without touching Supabase; `test_stt.py` passes a fake client object
into `_transcribe_best` to test the language-selection logic without a real API call). Fixtures
under `tests/fixtures/` that capture real SOOP API/chat responses are anonymized (no real viewer
usernames) — keep it that way when adding new fixtures.
