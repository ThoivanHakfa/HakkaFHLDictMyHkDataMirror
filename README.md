# HakfaFHLDictMyHkDataMirror

Data mirror for the [客台MY小詞典 (Hak-fa / Taigi / Mandarin Mini Dictionary)](https://hakka.fhl.net/dicmyhk/), a trilingual mini-dictionary hosted by the FHL (信望愛) organization.

This project mirrors the dictionary's ~17,000 entries verbatim and produces a parallel **derived** set in which the Hak-fa column is normalized to canonical PFS (Pha̍k-fa-sṳ) Roman Orthography (with IPA) and the Taigi column is normalized to canonical POJ (Pe̍h-ōe-jī) with consistent hyphenation and POJ Section 21 tone-mark placement.

## License

版權聲明
- [創用 CC 姓名標示-非商業性-相同方式分享 3.0 台灣 授權條款](https://creativecommons.org/licenses/by-nc-sa/3.0/tw/)
- Source: [客台MY小詞典](https://hakka.fhl.net/dicmyhk/)

The Python implementation in `script/scraper.py` is independent code; the `lib/KonvertToPOJ` reference submodule is GPL-3.0 and is **not** invoked at runtime.

## Latest Version

Check [**manifest.json**](./public/manifest.json) for the current `latest_version`.

## Accessing Files

Each version directory has two siblings, modeled after [HakkaFHLDictDataMirror](https://github.com/ThoivanHakfa/HakkaFHLDictDataMirror):

- `public/{version_id}/tangloo/HakfaFHLDictMyHk.csv` — **raw archive** (dicmyhk data verbatim, no conversion)
- `public/{version_id}/bunji/HakfaFHLDictMyHk.csv` — **derived output** (Hak-fa → PFS+IPA; Taigi → canonical POJ)

Each CSV has a sibling `.json` of the same name.

## Data Format

### `tangloo/HakfaFHLDictMyHk.csv` (raw archive)

| Column | Source label | Description |
|---|---|---|
| `ID` | 編號 | dicmyhk database ID |
| `Hakfa_FHL_Unicode` | 客語/pha̍kfâsṳ | Hak-fa Roman Orthography as served by dicmyhk (Unicode diacritics) |
| `Hanzi` | 客家字/Hakkasṳ | Hak-fa Hanji form |
| `Hakfa_HanLo` | 客語漢羅版 | Hak-fa mixed-script (Hanji + romanization) |
| `Taigi_FHL_Unicode` | TOJ/台灣字 | Taigi Roman Orthography (POJ-style, Unicode diacritics) |
| `Taigi_HanLo` | 台語漢羅 | Taigi mixed-script |
| `Hua-gi` | 華語解說 | Mandarin explanation |

### `bunji/HakfaFHLDictMyHk.csv` (derived)

| Column | Derivation |
|---|---|
| `ID` | from upstream |
| `Hakfa_FHL_Unicode` | raw, kept for traceability |
| `PFS_Unicode` | re-placed per canonical POJ Section 21 + KPPY-uV→PFS-oV normalized + `ṳ` restored |
| `PFS_Numeric` | derived from `PFS_Unicode` (PFS digits 1~6, with `ṳ` → `ii`) |
| `Hakfa_IPA` | `pfs_to_ipa(PFS_Numeric)` — IPA with Chao tone letters |
| `Taigi_FHL_Unicode` | raw, kept for traceability |
| `POJ_Unicode` | hyphenated + canonicalized Taigi POJ (Section 21 placement) |
| `Hanzi` | 客家字 (verbatim) |
| `Hakfa_HanLo` | 客語漢羅版 (verbatim) |
| `Taigi_HanLo` | 台語漢羅 (verbatim) |
| `Hua-gi` | 華語解說 (verbatim) |

### Why the dicmyhk pipeline differs from `/dict/`

- **Single fetch per ID.** The dicmyhk endpoint's `graph` query parameter is decorative — `graph=`, `graph=0`, `graph=2` all return identical Unicode-diacritic content. There is no upstream numeric form, so `PFS_Numeric` is **derived** from `PFS_Unicode` via the inverse helper `pfs_unicode_to_pfs_numeric`.
- **Taigi-side normalization.** The dicmyhk site emits Taigi POJ without consistent hyphenation between syllables (e.g. `ūchîⁿ lâng` for what should be `ū-chîⁿ lâng`). The scraper applies a greedy maximum-munch syllable segmenter (`hyphenate_poj`) and then canonical Section 21 tone-mark placement (`normalize_poj_unicode`).

## Pha̍k-fa-sṳ (PFS) Roman Orthography & IPA

The Hak-fa column is normalized to **Pha̍k-fa-sṳ (PFS)**, the Presbyterian tradition Hak-fa Roman Orthography (Si-yen / 四縣腔). See [HakfaFHLDictDataMirror's README](https://github.com/ThoivanHakfa/HakkaFHLDictDataMirror#pha̍k-fa-sṳ-pfs-roman-orthography--ipa) for the full PFS→IPA mapping table — the same conversion is applied here, with the inverse Unicode → Numeric derivation added.

## Taigi POJ Normalization

The Taigi-side conversion follows the [KonvertToPOJ](https://github.com/PehoejiKesi/KonvertToPOJ) library's conventions:

- **Hyphenation**: greedy maximum-munch syllable segmentation with a maximum-onset tiebreaker; `ⁿ` (U+207F) is treated as a syllable-final marker, not a syllable break. Apostrophes (`a'kū`, `iû'á`) are normalized to hyphens.
- **Tone-mark placement**: canonical POJ Section 21 (same letter-for-letter rule used for PFS).

The reference Kotlin Multiplatform implementation lives at [`lib/KonvertToPOJ`](./lib/KonvertToPOJ); the Python port in `script/scraper.py` is the runtime.

## Scraping the Data

To update the mirror:

```bash
python3 script/scraper.py
```

This will:
1. Binary-search the upstream max ID (the dictionary contains ~17,000 entries).
2. Iterate IDs 1..max_id with 10 worker threads.
3. Save raw + derived results to a new versioned folder in `public/`.
4. Generate JSON siblings.
5. Update `public/manifest.json`.

## Tests

```bash
python3 test_conversion.py
```

Pure-assertion test suite covering: `pfs_unicode_to_pfs_numeric`, `normalize_poj_unicode`, `hyphenate_poj`.

## Source

Original data source: [客台MY小詞典](https://hakka.fhl.net/dicmyhk/)
