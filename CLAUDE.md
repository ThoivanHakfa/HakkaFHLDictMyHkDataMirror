# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Data mirror of the [客台MY小詞典 (FHL trilingual Hak-fa/Taigi/Mandarin mini-dictionary)](https://hakka.fhl.net/dicmyhk/). Scrapes ~17,000 entries in Unicode-diacritic form, derives canonical PFS (Pha̍k-fa-sṳ) and Taigi POJ columns, generates Hak-fa IPA, and publishes versioned CSV/JSON to GitHub Pages.

Sister project: [HakkaFHLDictDataMirror](https://github.com/ThoivanHakfa/HakkaFHLDictDataMirror) (mirrors the larger Hak-fa-only `/dict/` endpoint).

## Commands

```bash
# Run scraper (~10 min — 17,000 IDs × 1 HTTP request each, 10 workers)
python3 script/scraper.py

# Test the conversion helpers
python3 test_conversion.py
```

No external dependencies — Python 3 stdlib only.

## Architecture

- `script/scraper.py` — single-file scraper, conversion pipeline, manifest updater
- `public/manifest.json` — source of truth for latest version
- Each scraper run creates a new timestamped version directory (YYYYMMDD-HHMM) with two siblings:
  - `public/{version_id}/tangloo/HakfaFHLDictMyHk.csv` — **raw mirror** (dicmyhk verbatim): `ID, Hakfa_FHL_Unicode, Hanzi, Hakfa_HanLo, Taigi_FHL_Unicode, Taigi_HanLo, Hua-gi`
  - `public/{version_id}/bunji/HakfaFHLDictMyHk.csv` — **derived**: raw + `PFS_Unicode`, `PFS_Numeric`, `Hakfa_IPA`, `POJ_Unicode`
- CSV is generated first, then converted to JSON
- The scraper fetches each ID **once** (dicmyhk's `graph` parameter is decorative — always returns Unicode)
- DNS for `hakka.fhl.net` is overridden to the Google Cloud origin IP (`35.221.176.32`) to bypass an nginx 403 returned via Cloudflare's HK edge. SNI and cert verification still go via `hakka.fhl.net`.

## Upstream specifics (dicmyhk)

- **Endpoint**: `https://hakka.fhl.net/dicmyhk/search.php?DETAIL=1&LIMIT=id={N}&dbname=myktdic&graph=`
- **dbname**: `myktdic` (not `hakka`)
- **HTML field labels** (parsed via td/th pairs):
  - `編號` → ID
  - `客語/pha̍kfâsṳ` → Hak-fa Unicode
  - `客家字/Hakkasṳ` → Hanji
  - `客語漢羅版` → Hak-fa Han-Lo
  - `TOJ/台灣字` → Taigi POJ Unicode
  - `台語漢羅` → Taigi Han-Lo
  - `華語解說` → Mandarin
- **Max ID**: ~17000 (17033+ returns `鍵值設定有問題` error). The scraper binary-searches this on startup via `probe_max_id`.
- **`graph` is decorative**: `graph=`, `graph=0`, `graph=2` all return identical Unicode-diacritic output. There is no upstream numeric form.

## Terminology

- Use **Hak-fa** (not "Hakka") in prose; use **Hakfa** (no hyphen) in identifiers and filenames
- Use **Si-yen** (not "Sixian") for 四縣
- Use **Taigi** (not "Taiwanese" or "Hokkien") for the Taigi language
- Use **Roman Orthography** (not "Romanization")
- Use **POJ** for Pe̍h-ōe-jī, **PFS** for Pha̍k-fa-sṳ

## Hak-fa pipeline (`bunji/` derivation)

Upstream serves only Unicode diacritics, so the derivation is the **inverse** of the sister project:

```
Hakfa_FHL_Unicode
  → fhl_unicode_to_pfs_unicode (POJ Section 21 placement)
  → normalize_kppy_uv_onglide_to_pfs (uV → oV)
  → PFS_Unicode
  → pfs_unicode_to_pfs_numeric (digits 1~6, ṳ → ii)
  → PFS_Numeric
  → pfs_to_ipa (Chao tone letters)
  → Hakfa_IPA
```

### `pfs_unicode_to_pfs_numeric` (new in this project)

Inverse of `fhl_unicode_to_pfs_unicode`. Walks syllables; per syllable: NFD-decompose, find the combining tone mark, strip it, NFC-recompose, attach digit. Digit map (combining → PFS digit): `◌̂ → 1, ◌̀ → 2, ◌́ → 3, ◌̍ → 5`. No tone + open coda → 4; no tone + `[ptk]$` → 6. Converts `ṳ` to `ii` to match sister project's numeric spelling convention.

## Taigi pipeline (`bunji/` derivation)

Upstream Taigi POJ is inconsistently hyphenated and uses FHL-style tone-mark placement. The derivation:

```
Taigi_FHL_Unicode
  → hyphenate_poj (greedy maximum-munch syllable segmenter with maximum-onset tiebreaker; apostrophes → hyphens)
  → normalize_poj_unicode (POJ Section 21 placement; POJ-specific digit-to-diacritic table; ⁿ handled as syllable-final)
  → POJ_Unicode
```

### `hyphenate_poj` (new in this project)

Splits whitespace-separated POJ tokens into hyphenated syllables. Algorithm: for each whitespace/hyphen-separated token of length N, find all valid prefixes whose remainder is also segmentable. Picks the break using the **maximum-onset principle**: prefer a break where the next syllable starts with a consonant initial (e.g. `iúhān` → `iú-hān`, NOT `iúh-ān`, because `h` is normally an onset, not a `-h` checked coda). Failure-safe: a token that does not decompose into POJ syllables is emitted unchanged. Apostrophes are normalized to hyphens up-front (dicmyhk uses both interchangeably).

Syllable validator follows `KonvertToPOJ`'s `isValidSyllable`: `<onset>?<medial-glide>?<nucleus><coda>?<nasalization>?`. Onset list (longest-first): `chh, ch, kh, ph, th, ng, b, g, h, j, k, l, m, n, p, s, t`. Coda: `h, k, m, n, ng, p, t` (longest-first). **`ⁿ` (U+207F) is a syllable-final marker, not a syllable break.**

### `normalize_poj_unicode` (new in this project)

Structurally identical to PFS's `_reposition_syllable` (same POJ Section 21 rule). POJ-specific differences:
- Uses `_POJ_TONE_COMBINING = {'́', '̀', '̂', '̄', '̍'}` (POJ tone 7 = macron, which PFS lacks — kept strictly separate from `_PFS_TONE_COMBINING`).
- `o͘` (o + U+0358) is a single vowel for placement purposes.
- `ⁿ` (U+207F) is non-letter for placement; preserved at syllable end.
- Accepts input digits 1–8 and converts to diacritic per POJ map: 1: none, 2: ́, 3: ̀, 4: none+checked, 5: ̂, 7: ̄, 8: ̍.

## Data Schema

See [README.md](./README.md) for the user-facing schema. Internal column constants live near the top of `scraper.py`.

## Tone Systems (Critical)

The Hak-fa `PFS_Numeric` column uses **PFS tone numbers 1~6**. The Taigi `POJ_Unicode` column uses **POJ tone numbers 1–8** for input parsing (but emits Unicode). **Do not equate PFS and POJ digits** — the digit↔diacritic correspondence differs:

| Diacritic | PFS digit | POJ digit |
|:---:|:---:|:---:|
| ˆ circumflex | 1 | 5 |
| ` grave | 2 | 3 |
| ´ acute | 3 | 2 |
| (none) | 4 | 1 |
| ̄ macron | — | 7 |
| ̍ vertical + stop | 5 | 8 |
| (none) + stop | 6 | 4 |

Keep `_PFS_TONE_COMBINING` and `_POJ_TONE_COMBINING` as separate constants — cross-bleed silently corrupts data.

## License

CC BY-NC-SA 3.0 Taiwan (data). The Python implementation in `script/scraper.py` is independent code; `lib/KonvertToPOJ` is GPL-3.0 reference, not invoked at runtime.
