"""Pure-assertion test suite for the dicmyhk-mirror conversion helpers.

Run with `python3 test_conversion.py`. Exit code != 0 on failure. No external
test framework — assertions only, matching the sister project's style.
"""
import sys
import os
import unicodedata

# Make `script/` importable.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'script'))

from scraper import (  # noqa: E402
    pfs_unicode_to_pfs_numeric,
    normalize_poj_unicode,
    hyphenate_poj,
    pfs_to_ipa,
)


def _nfc(s):
    return unicodedata.normalize('NFC', s)


def _assert_eq(label, got, want):
    g = _nfc(got)
    w = _nfc(want)
    if g != w:
        # Show codepoints for easier debugging of combining-mark mismatches.
        def codepoints(s):
            return ' '.join(f'U+{ord(c):04X}' for c in s)
        print(f"FAIL [{label}]")
        print(f"   input:  (see test source)")
        print(f"   got:    {g!r}  ({codepoints(g)})")
        print(f"   want:   {w!r}  ({codepoints(w)})")
        raise AssertionError(label)


# --- (a) PFS Unicode → PFS Numeric ---------------------------------------------
PFS_NUMERIC_CASES = [
    ('sî',          'si1'),
    ('sì',          'si2'),
    ('sí',          'si3'),
    ('si',          'si4'),
    ('se̍k',        'sek5'),
    ('sek',         'sek6'),
    ('liâng',       'liang1'),
    ('siông',       'siong1'),
    ('ǹg',          'ng2'),         # syllabic ng with grave
    ('liu̍k',       'liuk5'),
    ('sṳ̂',         'sii1'),        # ṳ → ii, circumflex → 1
    ('khoán',       'khoan3'),
    ('koe̍t',       'koet5'),
    ('khoài-le̍t',  'khoai2-let5'), # hyphenated compound, mixed tones
]
for src, want in PFS_NUMERIC_CASES:
    got = pfs_unicode_to_pfs_numeric(src)
    _assert_eq(f'pfs_unicode_to_pfs_numeric({src!r})', got, want)

# --- (b) POJ canonicalization (Section 21) -------------------------------------
# Inputs use FHL-style "mark first vowel" mis-placement to verify we move to
# canonical. Already-canonical inputs verify idempotency.
POJ_CANON_CASES = [
    ('kóai',   'koái'),     # oai: mark `a` (2nd of 3)
    ('khòan',  'khoàn'),    # oa+coda: mark `a`
    ('khóang', 'khoáng'),
    ('ko̍et',  'koe̍t'),    # oe+coda: mark `e`
    ('kûan',   'kuân'),     # ua+coda: mark `a`
    ('siá',    'siá'),      # already canonical, Exception 1 (i + V)
    ('liù',    'liù'),      # already canonical, Exception 1
    ('liu̍h',  'liu̍h'),    # iu + stop: special rule, no exception
    ('ǹg',     'ǹg'),       # syllabic ng
    ('o͘',    'o͘'),         # bare o͘ (cluster treated as single vowel)
]
for src, want in POJ_CANON_CASES:
    got = normalize_poj_unicode(src)
    _assert_eq(f'normalize_poj_unicode({src!r})', got, want)

# --- (c) POJ hyphenation -------------------------------------------------------
# Drives the KonvertToPOJ-style greedy maximum-munch segmenter. Includes the
# three user examples that motivated this helper.
POJ_HYPHEN_CASES = [
    ('ū chailān',    'ū chai-lān'),    # user example: split compound, preserve space
    ('ūchîⁿ lâng',   'ū-chîⁿ lâng'),   # user example: `ⁿ` is syllable-final, not a break
    ('iúhān',        'iú-hān'),         # user example: bare two-syllable compound
    ('chai-lān',     'chai-lān'),       # idempotent on already-hyphenated
    ('ū-chîⁿ',       'ū-chîⁿ'),         # idempotent with `ⁿ`
    ('ngó͘kak',      'ngó͘-kak'),       # `o͘` stays inside its syllable
    ('kong-si',      'kong-si'),        # idempotent compound
    ('thiaⁿtio̍h',   'thiaⁿ-tio̍h'),    # medial `ⁿ` correctly delimits
    ('lâng',         'lâng'),           # single syllable, no-op
    ('a',            'a'),              # single vowel, no-op
    ('XYZ',          'XYZ'),            # failure-safe: non-POJ token unchanged
    ('ū chai-lān',   'ū chai-lān'),     # idempotent with whitespace + hyphens
    # Maximum-onset extension: glide-onset rimes (iV / uI / oV) on the next
    # syllable beat a vowel-final prior syllable.
    ('ūiáⁿ',         'ū-iáⁿ'),          # i-glide onset: 有影
    ('chûiông',      'chû-iông'),       # i-glide onset after vowel-ending syl
    ('lāiiông',      'lāi-iông'),       # i-glide after coda-i (-ai → ai keeps `i` as coda; next `iong` wins)
    ('chaiiong',     'chai-iong'),      # same pattern, plain digits-free
    ('hóeoe',        'hóe-oe'),         # o-glide onset wins over `oeo` ambiguity
    # Two-pass safeguard: a CONSONANT onset must beat a glide-onset break even
    # when the glide-onset candidate is longer (regression test for the bug
    # that surfaced when the glide rule was added in single-pass form).
    ('amôa',         'a-môa'),          # consonant `m` beats `am | ôa` glide split
    ('chàtôaⁿ',      'chà-tôaⁿ'),       # consonant `t` beats `chàt | ôaⁿ` glide split
    ('chàniông',     'chà-niông'),      # consonant `n` beats `chàn | iông` glide split
    ('chiàmiú',      'chià-miú'),       # consonant `m` beats `chiàm | iú` glide split
    ('chhiatūi',     'chhia-tūi'),      # consonant `t` beats `chhiat | ūi` glide split
    ('chhapia̍t',    'chha-pia̍t'),     # consonant `p` beats `chhap | ia̍t` glide split
]
for src, want in POJ_HYPHEN_CASES:
    got = hyphenate_poj(src)
    _assert_eq(f'hyphenate_poj({src!r})', got, want)

# --- (d) PFS Numeric → Hak-fa IPA ----------------------------------------------
# Verifies the y-initial palatal-glide handling and a few baseline cases.
PFS_IPA_CASES = [
    ('yu1',          '[iu˨˦]'),         # yû — i-glide, NOT [yu]
    ('yeng3',        '[ieŋ˧˩]'),        # yéng — i-glide + eng coda
    ('yang1',        '[iaŋ˨˦]'),        # yâng — i-glide + ang coda
    ('yim1',         '[im˨˦]'),         # yim — yi collapses to i, NOT [ɨm]
    ('yin1',         '[in˨˦]'),         # yin — yi collapses to i
    ('yu1-yeng3',    '[iu˨˦] [ieŋ˧˩]'), # compound
    ('si1',          '[ɕi˨˦]'),         # baseline: si → ɕi
    ('sii1',         '[sɨ˨˦]'),         # baseline: ii → ɨ
]
for src, want in PFS_IPA_CASES:
    got = pfs_to_ipa(src)
    _assert_eq(f'pfs_to_ipa({src!r})', got, want)

_total = (len(PFS_NUMERIC_CASES) + len(POJ_CANON_CASES)
          + len(POJ_HYPHEN_CASES) + len(PFS_IPA_CASES))
print(f"OK — all PFS-numeric, POJ-canonicalization, hyphenation, and "
      f"PFS→IPA assertions passed ({_total} cases).")
