# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# install (editable, with dev tools)
uv venv && uv pip install -e ".[audio,stt,youtube,batch,dev]"

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

- **Manual CLI pipeline** (`cli.py`): a human runs `collect` → `songs`/`clips` → `clips --upload`
  one VOD at a time, reviewing/editing `clips.json` between steps before anything goes to YouTube.
- **Batch pipeline** (`batch.py`, driven by `soopts daily`/`soopts sync`): the same detection/clip/
  identify building blocks (`analyzers/audio_analyzer.py`, `collector/media.py`, `export/clips.py`,
  `analyzers/stt.py`, `analyzers/identify.py`) are composed directly, with Supabase (`db.py`) standing
  in for the human review step. `batch.py` does not reuse `cli.py`'s `_produce_clips`/`_songs_slice`
  helpers (those are argparse/print-oriented for interactive review) — it re-implements the same
  region → download → cut → transcribe flow against a `vod_row` from the DB instead of a CLI `args`
  namespace.
- Shared low-level helpers that both entry points need (e.g. `collector/media.py`'s `map_to_part`,
  which maps a global time range onto the right HLS part + local offsets) live in the module layer,
  not in `cli.py`, specifically so `batch.py` can call them without importing the CLI.

### Config system (`config.py`)

One dataclass per concern (`Endpoints`, `CollectorConfig`, `AudioConfig`, `SttConfig`, `ClipConfig`,
`YouTubeConfig`, `StationConfig`), assembled into `Config`. `load_config()` reads `soopts.toml` and
merges only the keys present per section (`_build_section` drops unknown keys) — so partial overrides
in `soopts.toml` don't require repeating every field. `work_root` can also be overridden via CLI flag.

### Work directory as cache (`paths.py`)

Every VOD gets `work/{vod_id}/` (`WorkPaths`): `meta.json`, `chat.jsonl`, `raw/` (chat XML cache),
`audio_segmentation.json` (expensive STT segmentation cache), `songs.json`/`songs_{id}.txt`, `clips/`.
Steps check these files before re-fetching/re-computing; `--force` bypasses the cache. The batch
pipeline uses the exact same layout, which is what makes clip-file resumability possible (see below).

### Lazy imports for heavy/optional dependencies

`inaSpeechSegmenter`, `faster-whisper`, the `google-api-python-client` stack, `supabase`, `anthropic`,
and `rapidfuzz` are **only imported inside the functions that use them**, never at module top level.
This keeps `import soopts` cheap and lets `pyproject.toml`'s optional-dependency extras (`audio`, `stt`,
`youtube`, `batch`) actually be optional — a plain `pip install soopts` with no extras can still run
`collect`. Preserve this pattern when adding new functionality that touches one of these libraries.

### The Supabase boundary (`db.py`)

`db.py` is the *only* module that talks to Supabase. The schema (`vods`, `performances`, `song_aliases`,
and the read-only `songs` catalog) is owned by a separate private repo (`singgyul_sing_book`) — this
codebase only consumes it and never creates migrations. `songs` rows are never created from this repo;
unmatched songs always land as `needs_review` for a human to resolve in the separate review UI.
`vods.status`/`performances.clip_status`/`performances.identify_status` are the actual state machine —
treat them as the source of truth, not local files (see next point).

### Volatile-runner design (`batch.py`)

GitHub-hosted runners don't persist disk between workflow runs. The upload queue therefore cannot
depend on files surviving between `daily` invocations — its truth is `performances.clip_status` plus
`start_s`/`end_s` in Supabase. `clip_file_path(work_root, title_no, start_s)` is a pure function that
reconstructs the expected clip path deterministically from those DB columns; if the file is missing
(runner reset since it was cut), `_reslice_clip()` re-downloads just that segment and re-cuts it rather
than re-running full detection. Don't reintroduce any state that only lives in the runner's filesystem
into the upload-queue logic.

### GitHub Actions workflows

- `verify-env.yml` (manual): confirms SOOP's API/streams are reachable from a hosted-runner IP before
  relying on `daily.yml`/`sync.yml`. If it starts failing, only `runs-on` needs to change to a
  self-hosted runner — nothing else.
- `daily.yml` / `sync.yml`: scheduled + `workflow_dispatch`. Both pipe `soopts ... | tee *.log` and rely
  on the exit code to detect failure — any `run:` step doing this **must** start with
  `set -o pipefail`, or a crash in `soopts` gets masked by `tee`'s always-zero exit status (this has
  bitten this repo once already).

### Testing philosophy

Tests exercise pure functions only — no network, no DB, no ML model loading (`test_identify.py`,
`test_db.py`, `test_vod_list.py`, `test_audio.py`, `test_clips.py`, `test_dedup.py`,
`test_stt_filter.py`, `test_xml_parse.py`, `test_real_schema.py`). Fixtures under `tests/fixtures/`
that capture real SOOP API/chat responses are anonymized (no real viewer usernames) — keep it that way
when adding new fixtures.
