# Mac Photos Library Reader (Windows)

> **This project is fully developed by AI.** Every line of code, tests, and
> documentation was written by an AI agent (Hermes Agent, running a
> DeepSeek-V4.1-Flash model) through conversational direction, including the
> debugging and fixes. It has not been reviewed line-by-line by a human, so
> treat it accordingly — inspect before relying on it for anything critical.

Browse a `.photoslibrary` that was copied off a Mac — on Windows, without a Mac
and without iTunes, iCloud, or the Photos app.

Point it at the folder, and you get the library's real structure: photos,
videos, Live Photos, screenshots, albums, favourites, edits, EXIF, GPS, and the
original filenames (`IMG_7258.HEIC`) rather than Apple's internal UUIDs.

The library is opened **strictly read-only**. It is never moved, renamed,
re-encoded, or written to.

---

## Quick start

1. Double-click **`run.bat`**.
   The first run creates a virtual environment and installs dependencies, then
   opens the app in your default browser.
2. Pick a library from the list, or click **Open Library…** to browse to one.
3. Optionally pass a path to skip the picker:

```bat
run.bat "D:\Pictures\2026.09.20-Disney.photoslibrary"
```

On startup the app scans `PHOTOS_READER_ROOTS` (an `os.pathsep`-separated list;
this is where your libraries live) plus `~/Pictures`, and lists every
`.photoslibrary` it finds. Set it before launching, e.g.:

```bat
set PHOTOS_READER_ROOTS=D:\Pictures;E:\PhotoBackup
run.bat
```

It only opens one library automatically when there is exactly one — with
several, it lists them and waits, rather than guessing and showing you someone
else's photos.

If a second copy is launched while the app is already running, it reuses the
running instance and just opens the browser; it does not start a rival server.
This holds even if the first copy landed on a random port because the default
was busy — the running port is recorded in
`%LOCALAPPDATA%\MacPhotosReader\server.port` and health-checked before reuse.

Requirements: Windows 10/11 and Python 3.10+. `ffmpeg` on `PATH` is needed only
to play videos (see below).

---

## What it reads

A `.photoslibrary` is a macOS bundle — a folder with a fixed layout:

```
<Library>.photoslibrary/
├── database/
│   ├── Photos.sqlite        ← the whole index (Core Data)
│   ├── Photos.sqlite-wal    ← recent changes live here
│   └── DataModelVersion.plist
├── originals/<D>/<UUID>.<ext>          ← the real photo/video files
└── resources/
    ├── derivatives/masters/<D>/<UUID>_4_5005_c.jpeg   ← small JPEG
    ├── derivatives/<D>/<UUID>_1_102_o.jpeg            ← larger JPEG
    └── renders/<D>/<UUID>_1_201_a.*                   ← edited full-size render
```

Everything that matters is in `Photos.sqlite`. Key mappings the app uses:

| Concept | Source |
| --- | --- |
| Original path | `ZASSET.ZDIRECTORY` + `ZASSET.ZFILENAME` |
| Real camera filename | `ZADDITIONALASSETATTRIBUTES.ZORIGINALFILENAME` |
| Capture time | `ZASSET.ZDATECREATED` (Apple epoch, UTC) + `ZTIMEZONEOFFSET` |
| Photo vs video | `ZASSET.ZKIND` (0 = photo, 1 = video) |
| Live Photo | `ZKINDSUBTYPE = 2` + the sibling `_3.mov` |
| Screenshot | `ZKINDSUBTYPE = 10` or `ZISDETECTEDSCREENSHOT` |
| Edited | `ZADJUSTMENTSSTATE ≠ 0` → use `resources/renders/` |
| Favourites / hidden / trashed | `ZFAVORITE`, `ZHIDDEN`, `ZTRASHEDSTATE` |
| Camera & exposure | `ZEXTENDEDATTRIBUTES` (make, model, lens, ISO, ƒ, shutter) |
| Albums | `ZGENERICALBUM` + the `Z_<entity>ASSETS` join tables |

Two details that are easy to get wrong and that this app handles:

- **Timestamps are UTC.** `ZDATECREATED` is seconds since 2001-01-01 UTC;
  `ZTIMEZONEOFFSET` turns it into the wall-clock time the photo was taken.
  Displaying the raw value shifts every photo by the timezone offset.
- **`ZWIDTH`/`ZHEIGHT` are display dimensions**, already rotated. The files on
  disk may be stored sideways with an EXIF orientation tag, so anything decoded
  directly must be EXIF-transposed to match.

Because column sets drift between Photos releases, every optional column is
probed rather than assumed, and album join tables are discovered by name.

---

## Features

- **Timeline grid** with sticky month headings, incremental loading, and a
  year scrubber for multi-year libraries.
- **Four thumbnail sizes**, served from Photos' own derivatives when present
  (4 ms each), decoded from the original only when necessary.
- **Full-screen viewer** with arrow-key navigation, EXIF panel, GPS map link,
  and the original filename.
- **Live Photos play.** The paired `_3.mov` is located and played on demand.
- **Videos play.** HEVC `.mov` files are transcoded to H.264 once and cached,
  because Windows browsers cannot decode HEVC in a `<video>` tag.
- **Edited photos show their edits**, by preferring `resources/renders/`.
- **Search** across filenames, titles, camera, lens, and timezone.
- **Filters**: favourites, videos, Live Photos, screenshots, year.
- **Export** copies originals out with their real names, never overwriting.

---

## Video and HEVC

Photos stores iPhone video as HEVC in a `.mov` container. Chrome, Edge, and
Firefox on Windows generally cannot play that. When you open such a video the
app transcodes it with `ffmpeg` to H.264/AAC, caches the result, and serves
that. First play of a long clip takes a few seconds; later plays are instant.

Without `ffmpeg`, videos are listed with metadata and thumbnails but will not
play. Install it from [ffmpeg.org](https://ffmpeg.org/download.html) or
`winget install Gyan.FFmpeg`, then restart the app.

---

## Caching

Generated files go to `%LOCALAPPDATA%\MacPhotosReader\`, keyed by library path:

```
thumbs/grid/<uuid>_<size>.jpg     thumbnail cache
previews/<uuid>_<size>.jpg        viewer images
video/<uuid>.mp4                  transcoded video
```

Delete that folder any time to reclaim space; it is rebuilt on demand. Set
`PHOTOS_READER_CACHE` to put it somewhere else (useful for a small SSD).

---

## Why the app copies the database in some cases

SQLite needs to write to a `-shm` sidecar file to read a WAL-mode database. On
a read-only or network volume that fails, so the app falls back to reading a
snapshot copied into its cache. The bundle itself is never touched either way.

---

## If a library does not open

| Symptom | Cause |
| --- | --- |
| “does not look like a Photos library” | Wrong folder — pick the `.photoslibrary` itself, not a parent |
| Opens but no thumbnails | `resources/derivatives` was pruned; originals are decoded instead (slower) |
| “missing originals” | iCloud Optimise Storage left files in the cloud; they must be downloaded on the Mac first |
| No albums | The library genuinely has none — Photos' implicit “All Photos” is not a stored album. This is the common case: of 12 libraries here, only one had a real album |
| An album named “Untitled Album 53” | The album exists and holds photos but has no stored title. Left visible rather than hidden, since hiding it would lose real photos |
| Nothing from 2019 or earlier | Very old libraries use a pre-Core-Data schema; only Photos 5+ (macOS Catalina+) is supported |

Photos also writes AppleDouble files (`._Name.photoslibrary`) next to bundles
on non-HFS volumes. These are metadata, not libraries; the app filters them out.

---

## Command line

```bat
python -m app.launcher <library> [--port 8760] [--no-browser] [--host 127.0.0.1]
```

The server binds to `127.0.0.1` only, so nothing is exposed to your network.

---

## API

The UI is a thin client over a JSON API — useful for scripting:

| Endpoint | Purpose |
| --- | --- |
| `GET /api/library` | Library info, stats, albums |
| `GET /api/assets?offset=&limit=&kind=&q=&year=&sort=` | Paged asset list |
| `GET /api/assets/{id}` | One asset with EXIF and albums |
| `GET /api/thumb/{id}?size=xs\|s\|m\|l` | Thumbnail JPEG |
| `GET /api/preview/{id}` | Viewer-size JPEG |
| `GET /api/video/{id}` | Playable MP4 |
| `GET /api/original/{id}?download=true` | The untouched original |
| `GET /api/timeline` | Year/month counts |
| `POST /api/export` | `{"dest": "...", "kind": "all"}` |

Interactive docs at `http://127.0.0.1:8760/docs` while running.

---

## Development

```
app/library.py    schema-tolerant reader (SQLite → dataclasses)
app/media.py      thumbnails, previews, HEVC→H.264 transcoding
app/server.py     FastAPI routes
app/static/       the UI (vanilla JS, no build step)
tools/            verification and timing scripts
```

Verification scripts, all run against real libraries:

```bat
.venv\Scripts\python.exe tools\verify_readonly.py "<library>" [port]   REM proves nothing is written
.venv\Scripts\python.exe tools\test_all_libraries.py <port>           REM opens every discovered library
.venv\Scripts\python.exe tools\test_discovery.py <port>               REM root discovery + /api/recent
.venv\Scripts\python.exe tools\test_fallbacks.py "<library>"          REM no-derivative decode path
.venv\Scripts\python.exe tools\test_concurrency.py <port>             REM 80 parallel thumbnails
.venv\Scripts\python.exe tools\test_albums.py                         REM album/folder logic (needs fixture)
.venv\Scripts\python.exe tools\test_reuse.py "<library>"              REM second launch reuses first
.venv\Scripts\python.exe tools\test_portfile_cleanup.py               REM stale port file recovery
.venv\Scripts\python.exe tools\test_runbat.py "<library>"             REM end-to-end launcher check
.venv\Scripts\python.exe tools\make_fixture.py "<src>" "<dst>"        REM builds an album fixture
```

No library path is baked into the code; every script that needs one takes it as
an argument.

Scripts that talk to a running server take the port as an argument; start one
first (e.g. `.venv\Scripts\python.exe -m app.launcher --port 8840 --no-browser`).

`verify_readonly.py` snapshots every file's size and mtime, exercises
thumbnails, previews, transcoding, search, and export, then re-snapshots and
diffs. On a 13,179-file library it reports zero added, removed, or changed
files.

`test_all_libraries.py` opens every library found on disk and checks that each
produces assets, dates, a thumbnail and a preview. Last run: 12/12 libraries,
12,119 assets, 0 failures, slowest open 0.58s.

---

## Provenance and disclaimer

This codebase was **fully developed by AI** — including the tests that back the
claims above. That matters for how you read the rest of this document: the
numbers are from real runs, but the design and its review came from a model, not
a person.

What that means in practice:

- **It has not had a human line-by-line review.** Read the code before trusting
  it with anything you care about.
- **It is read-only by design**, and `tools/verify_readonly.py` exists to prove
  that against your own library — run it rather than taking the claim on trust.
- **No warranty of any kind.** Use at your own risk; keep backups of any library
  you point it at, as you would with any third-party tool.
- **No license is declared yet.** Without one, the code is technically "all
  rights reserved" — readable, not reusable.
