# HakfaFHLDictMyHkDataMirror Instructions

## Project Overview
Data mirror for the [客台MY小詞典](https://hakka.fhl.net/dicmyhk/), a trilingual FHL mini-dictionary (Hak-fa + Taigi + Mandarin, ~17,000 entries). Sister project to [HakfaFHLDictDataMirror](https://github.com/ThoivanHakfa/HakkaFHLDictDataMirror).

### Core Technologies
- **Python 3 (stdlib only)**: `urllib`, `http.client`, `html.parser`, `csv`, `json`, `concurrent.futures`, `unicodedata`.
- **Data Formats**: CSV and JSON.
- **Hosting**: GitHub Pages (`.nojekyll` + `index.md`).

## Project Architecture
- `public/`
  - `manifest.json`: tracks `latest_version` and all available versions.
  - `{version_id}/tangloo/HakfaFHLDictMyHk.{csv,json}`: raw archive (dicmyhk verbatim).
  - `{version_id}/bunji/HakfaFHLDictMyHk.{csv,json}`: derived (PFS + IPA + canonical POJ).
- `script/scraper.py`: scraper, conversion pipeline, manifest updater.
- `test_conversion.py`: pure-assertion test suite.
- `lib/KonvertToPOJ/`: reference Kotlin Multiplatform library (not invoked at runtime).

## Development Workflows

### Running the Scraper
```bash
python3 script/scraper.py
```
**Behavior:**
1. Probes the upstream max ID via binary search (between known-valid 17000 and known-empty 20000).
2. Generates a new `version_id` (YYYYMMDD-HHMM).
3. Iterates IDs 1..max_id with 10 worker threads, **one HTTP request per ID** (dicmyhk's `graph` parameter is decorative).
4. Writes `tangloo/` (verbatim) and `bunji/` (derived) CSVs.
5. Generates JSON siblings.
6. Updates `public/manifest.json`.

### Data Conventions
- **Roman Orthography**: always use the term "Roman Orthography" (not "Romanization").
- **Hak-fa orthographies**: `PFS_Unicode` (canonical POJ Section 21 placement, `ṳ` preserved) and `PFS_Numeric` (1~6 digits, `ṳ` → `ii`).
- **Taigi orthography**: `POJ_Unicode` (canonical, hyphenated, with Section 21 tone-mark placement).
- **Tone systems**: PFS digits 1~6 and POJ digits 1–8 are **not interchangeable**; see CLAUDE.md tone table.
- **Language references**: **Hak-fa** for the Hak-fa side, **Taigi** for the Taigi side. Never "Hakka", "Taiwanese", or "Hokkien".

## Key Files
- `script/scraper.py`: scraper engine + all conversion helpers.
- `public/manifest.json`: source of truth for the latest mirrored data.
- `README.md`: user-facing schema documentation.
- `CLAUDE.md`: deep-dive on the conversion pipeline and tone systems.
