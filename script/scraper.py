import urllib.request
import csv
import http.client
import threading
import time
import sys
import os
import json
import re
import socket
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from html.parser import HTMLParser

# Concurrency knobs for the per-ID fetch loop. 10 workers with a 30s timeout and
# per-thread persistent HTTPSConnection (keep-alive). dicmyhk has ~17k IDs and
# only one fetch per ID (the `graph` parameter is decorative), so a full run
# finishes in well under 10 min.
SCRAPE_WORKERS = 10
HTTP_TIMEOUT = 30
HTTP_MAX_ATTEMPTS = 2  # try once, retry once on transient failure (no sleep)

# The Cloudflare edge for hakka.fhl.net returns 403 from origin nginx for /dict/*
# (and /dicmyhk/*) when routed via certain regions (e.g. Hong Kong). The Google
# Cloud origin behind south.fhl.net (35.221.176.32) serves the same vhost and
# answers normally, so we override DNS for hakka.fhl.net to hit the origin
# directly with the cert's matching SNI (*.fhl.net Let's Encrypt). If FHL ever
# moves the origin, update _FHL_ORIGIN_IP or remove this block.
_FHL_ORIGIN_IP = '35.221.176.32'
_real_getaddrinfo = socket.getaddrinfo
def _patched_getaddrinfo(host, port, *args, **kwargs):
    if host == 'hakka.fhl.net':
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (_FHL_ORIGIN_IP, port))]
    return _real_getaddrinfo(host, port, *args, **kwargs)
socket.getaddrinfo = _patched_getaddrinfo

class FHLParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_td = False
        self.current_data = []
        self.all_data = []

    def handle_starttag(self, tag, attrs):
        if tag == 'td' or tag == 'th':
            self.in_td = True
            self.current_data = []

    def handle_endtag(self, tag):
        if tag == 'td' or tag == 'th':
            self.in_td = False
            text = ''.join(self.current_data).strip()
            text = ' '.join(text.split())
            self.all_data.append(text)

    def handle_data(self, data):
        if self.in_td:
            self.current_data.append(data)

# --- dicmyhk field labels (resolved by inspecting ID=1 in a browser) -----------
# The dicmyhk page emits these exact <th> label strings. Bake the strings in;
# the scraper picks each field out of the parsed td/th-pair dict by label.
_LBL_ID         = '編號'
_LBL_HAKFA      = '客語/pha̍kfâsṳ'
_LBL_HANZI      = '客家字/Hakkasṳ'
_LBL_HAKFA_HL   = '客語漢羅版'
_LBL_TAIGI      = 'TOJ/台灣字'
_LBL_TAIGI_HL   = '台語漢羅'
_LBL_HUAGI      = '華語解說'
_ERROR_MARKER   = '鍵值設定有問題'  # "key value problem" — emitted past max ID

# --- Hak-fa Unicode helpers (lifted from sister project) -----------------------

_PFS_TONE_COMBINING = {'̂', '̀', '́', '̍'}  # ◌̂ ◌̀ ◌́ ◌̍
_TREMA_BELOW = '̤'  # ṳ = u + U+0324
_VOWELS = set('aeiouṳ')

def _pfs_tone_position(base_lc):
    tokens = []
    i = 0
    while i < len(base_lc):
        if base_lc[i:i+2] == 'ng':
            tokens.append(('ng', i)); i += 2
        else:
            tokens.append((base_lc[i], i)); i += 1
    if not tokens:
        return None
    vowel_ks = [k for k, (t, _) in enumerate(tokens) if t in _VOWELS]
    if not vowel_ks:
        for nasal in ('ng', 'm', 'n'):
            for t, pos in tokens:
                if t == nasal:
                    return pos
        return None
    if len(vowel_ks) == 1:
        return tokens[vowel_ks[0]][1]
    n = len(tokens)
    k = n - 2
    is_checked = tokens[-1][0] in ('p', 't', 'k')
    vowels_only = ''.join(tokens[vk][0] for vk in vowel_ks)
    if is_checked and tokens[k][0] in ('i', 'u') and vowels_only != 'iu':
        if n - 3 >= 0:
            return tokens[n - 3][1]
    if tokens[k][0] == 'i':
        return tokens[-1][1]
    return tokens[k][1]

def _reposition_syllable(fhl_syl, fhl_num_syl=None):
    if not fhl_syl:
        return fhl_syl
    nfd = unicodedata.normalize('NFD', fhl_syl)
    base = []
    combining = []
    for ch in nfd:
        if unicodedata.combining(ch):
            combining.append([len(base) - 1, ch])
        else:
            base.append(ch)
    base_lc = ''.join(c.lower() for c in base)
    if fhl_num_syl:
        num_base = re.sub(r'[0-9]$', '', fhl_num_syl).lower()
        i_n = i_u = 0
        to_add = []
        while i_n < len(num_base) and i_u < len(base_lc):
            cn, cu = num_base[i_n], base_lc[i_u]
            if cn == 'i' and i_n + 1 < len(num_base) and num_base[i_n+1] == 'i' and cu == 'u':
                if not any(p == i_u and c == _TREMA_BELOW for p, c in combining):
                    to_add.append(i_u)
                i_n += 2; i_u += 1
            elif cn == cu:
                i_n += 1; i_u += 1
            else:
                to_add = []
                break
        for pos in to_add:
            combining.append([pos, _TREMA_BELOW])
    tone_slot = next((k for k, (_, c) in enumerate(combining) if c in _PFS_TONE_COMBINING), None)
    if tone_slot is not None:
        target = _pfs_tone_position(base_lc)
        if target is not None:
            combining[tone_slot][0] = target
    by_pos = {}
    for pos, ch in combining:
        by_pos.setdefault(pos, []).append(ch)
    out = []
    for i, b in enumerate(base):
        out.append(b)
        out.extend(by_pos.get(i, []))
    return unicodedata.normalize('NFC', ''.join(out))

def fhl_unicode_to_pfs_unicode(fhl_unicode, fhl_numeric=None):
    if not fhl_unicode:
        return fhl_unicode
    uni_tokens = re.split(r'([-\s]+)', fhl_unicode)
    num_tokens = re.split(r'([-\s]+)', fhl_numeric) if fhl_numeric else []
    uni_syls = [i for i, t in enumerate(uni_tokens) if t and not re.fullmatch(r'[-\s]+', t)]
    num_syls = [t for t in num_tokens if t and not re.fullmatch(r'[-\s]+', t)]
    pair_numerics = len(uni_syls) == len(num_syls)
    for k, idx in enumerate(uni_syls):
        ns = num_syls[k] if pair_numerics else None
        uni_tokens[idx] = _reposition_syllable(uni_tokens[idx], ns)
    return ''.join(uni_tokens)

# KPPY uV → PFS oV normalization (lift from sister project).
_KPPY_UV_RIMES = ('uang', 'uan', 'uai', 'uak', 'uat', 'uet', 'uen', 'ue', 'ua')
_PFS_INITIALS = ('tsh', 'chh', 'ts', 'ch', 'ph', 'th', 'kh', 'ng',
                 'p', 't', 'k', 'm', 'n', 'l', 'f', 'v', 's', 'h')

def _strip_pfs_onset(base_lc):
    for o in _PFS_INITIALS:
        if base_lc.startswith(o):
            return len(o)
    return 0

def _normalize_uv_syllable(syl):
    if not syl:
        return syl
    nfd = unicodedata.normalize('NFD', syl)
    tone_digit = ''
    if nfd and nfd[-1] in '0123456789':
        tone_digit = nfd[-1]
        nfd = nfd[:-1]
    base, combos = [], []
    for ch in nfd:
        if unicodedata.combining(ch):
            combos.append((len(base) - 1, ch))
        else:
            base.append(ch)
    base_lc = ''.join(b.lower() for b in base)
    onset_len = _strip_pfs_onset(base_lc)
    rime_lc = base_lc[onset_len:]
    if rime_lc not in _KPPY_UV_RIMES:
        return syl
    if onset_len >= len(base) or base[onset_len].lower() != 'u':
        return syl
    if any(pos == onset_len and c == _TREMA_BELOW for pos, c in combos):
        return syl
    base[onset_len] = 'O' if base[onset_len].isupper() else 'o'
    by_pos = {}
    for pos, c in combos:
        by_pos.setdefault(pos, []).append(c)
    out = []
    for i, b in enumerate(base):
        out.append(b)
        out.extend(by_pos.get(i, []))
    return unicodedata.normalize('NFC', ''.join(out)) + tone_digit

def normalize_kppy_uv_onglide_to_pfs(text):
    if not text:
        return text
    tokens = re.split(r'([-\s]+)', text)
    for i, t in enumerate(tokens):
        if t and not re.fullmatch(r'[-\s]+', t):
            tokens[i] = _normalize_uv_syllable(t)
    return ''.join(tokens)

# Hak-fa IPA (Si-yen) — lifted from sister project.
def pfs_to_ipa(pfs_text):
    if not pfs_text: return ""
    def convert_syllable(s):
        s = s.lower().strip()
        tone_map = {'1': '˨˦', '2': '˩˩', '3': '˧˩', '4': '˥˥', '5': '˥', '6': '˨'}
        m = re.search(r'([1-6])$', s)
        tone = ""
        if m:
            tone = tone_map.get(m.group(1), "")
            s = s[:-1]
        # PFS 'y' before a vowel is the palatal on-glide /i̯/ (Si-yen Hak-fa).
        # IPA renders it as plain `i`. Collapse `yi` → `i` (the redundant glide
        # before its own vowel, as in `yim` /im/), and rewrite `y` + other
        # vowel as `i` + that vowel (`yâng` → /iaŋ/, `yû` → /iu/, `yéng` →
        # /ieŋ/). Done BEFORE `ii` → `ɨ` so we never spuriously produce /ɨ/.
        if s.startswith('yi'):
            s = s[1:]
        elif s.startswith('y') and len(s) >= 2 and s[1] in 'aeou':
            s = 'i' + s[1:]
        s = s.replace('ii', 'ɨ')
        s = s.replace('ṳ', 'ɨ')
        s = re.sub(r'^tshi', 'tɕʰi', s)
        s = re.sub(r'^chhi', 'tɕʰi', s)
        s = re.sub(r'^tsi', 'tɕi', s)
        s = re.sub(r'^chi', 'tɕi', s)
        s = re.sub(r'^ngi', 'ɲi', s)
        s = re.sub(r'^si', 'ɕi', s)
        s = re.sub(r'^tsh', 'tsʰ', s)
        s = re.sub(r'^chh', 'tsʰ', s)
        s = re.sub(r'^ch', 'ts', s)
        s = re.sub(r'^ph', 'pʰ', s)
        s = re.sub(r'^th', 'tʰ', s)
        s = re.sub(r'^kh', 'kʰ', s)
        s = re.sub(r'^v', 'ʋ', s)
        s = s.replace('ng', 'ŋ')
        s = s.replace('er', 'ɤ')
        s = re.sub(r'p$', 'p̚', s)
        s = re.sub(r't$', 't̚', s)
        s = re.sub(r'k$', 'k̚', s)
        return f"[{s}{tone}]"
    syllables = re.split(r'[-\s]+', pfs_text)
    ipa_syllables = [convert_syllable(s) for s in syllables if s]
    return " ".join(ipa_syllables)

# --- NEW: pfs_unicode_to_pfs_numeric -------------------------------------------
# Inverse of `fhl_unicode_to_pfs_unicode`. dicmyhk only serves Unicode; the
# numeric column is derived. For each whitespace/hyphen-separated syllable:
#   1. NFD-decompose, find any PFS tone diacritic, strip it, NFC-recompose.
#   2. Convert `ṳ` → `ii` (matches sister project's numeric spelling).
#   3. Attach digit: ◌̂→1, ◌̀→2, ◌́→3, ◌̍→5; no-tone+open coda→4; no-tone+ptk→6.

_PFS_COMBINING_TO_DIGIT = {'̂': '1', '̀': '2', '́': '3', '̍': '5'}

def _pfs_syllable_unicode_to_numeric(syl):
    nfd = unicodedata.normalize('NFD', syl)
    tone_digit = None
    base = []
    other_combos = []
    for ch in nfd:
        if unicodedata.combining(ch):
            if ch in _PFS_COMBINING_TO_DIGIT and tone_digit is None:
                tone_digit = _PFS_COMBINING_TO_DIGIT[ch]
            else:
                other_combos.append((len(base) - 1, ch))
        else:
            base.append(ch)
    by_pos = {}
    for pos, c in other_combos:
        by_pos.setdefault(pos, []).append(c)
    out = []
    for i, b in enumerate(base):
        out.append(b)
        out.extend(by_pos.get(i, []))
    nfc = unicodedata.normalize('NFC', ''.join(out))
    nfc_with_ii = nfc.replace('ṳ', 'ii').replace('Ṳ', 'II')
    if tone_digit is None:
        # Open vs checked coda. Last base letter (case-insensitive); ignore the
        # trema-below since ṳ-codas don't exist in Hak-fa.
        last = nfc_with_ii.lower()[-1] if nfc_with_ii else ''
        tone_digit = '6' if last in ('p', 't', 'k') else '4'
    return f"{nfc_with_ii}{tone_digit}"

def pfs_unicode_to_pfs_numeric(text):
    if not text:
        return text
    tokens = re.split(r'([-\s]+)', text)
    for i, tok in enumerate(tokens):
        if tok and not re.fullmatch(r'[-\s]+', tok):
            tokens[i] = _pfs_syllable_unicode_to_numeric(tok)
    return ''.join(tokens)

# --- Taigi POJ constants (NEW; port from lib/KonvertToPOJ) ---------------------
# POJ tone diacritics are NOT the same set as PFS — POJ has tone 7 (macron).
# Keep this constant STRICTLY SEPARATE from _PFS_TONE_COMBINING.
_POJ_TONE_COMBINING = {'́', '̀', '̂', '̄', '̍'}  # acute, grave, circumflex, macron, vertical-line

# Longest-first POJ initials. Mirrors `POJ_INITIALS` from
# `lib/KonvertToPOJ/.../internal/SyllableValidator.kt`.
_POJ_INITIALS = ('chh', 'ch', 'ph', 'th', 'kh', 'ng',
                 'p', 'm', 'b', 't', 'n', 'l', 'k', 'g', 'h', 's', 'j')

# POJ valid rhymes (whitelist). Mirrors `POJ_RHYMES_BASE` from
# `lib/KonvertToPOJ/.../internal/SyllableValidator.kt`. After NFC normalization
# the validator maps `o͘` → `oo` and `ⁿ` → `nn`, so the rhyme set uses input-form
# spellings throughout.
_POJ_RHYMES = frozenset((
    # Single vowels
    'a', 'i', 'u', 'oo', 'e', 'o',
    # Compound vowels
    'ai', 'au', 'ia', 'iu', 'io', 'ioo', 'ui', 'oa', 'oe', 'ei', 'ona',
    'iau', 'oai',
    # Nasal vowels
    'ann', 'inn', 'unn', 'oonn', 'enn', 'onn',
    'ainn', 'aunn', 'iann', 'iunn', 'ionn', 'uinn', 'oann', 'oenn',
    'iaunn', 'oainn', 'oiann',
    # Nasal coda finals
    'am', 'an', 'ang', 'im', 'in', 'iam', 'iang', 'iong',
    'um', 'un', 'en', 'om', 'ong', 'oan', 'oang',
    'ian', 'eng', 'ieng',
    # Checked finals
    'ap', 'at', 'ak', 'ah', 'ip', 'it', 'ek', 'ut',
    'op', 'ok', 'ooh', 'eh', 'oh', 'ih', 'uh',
    'iap', 'iat', 'iak', 'iok',
    'iah', 'ioh', 'iooh', 'iuh',
    'oat', 'oak', 'oah', 'oeh',
    'aih', 'auh', 'iauh', 'uih', 'oaih',
    # Nasal + checked
    'annh', 'innh', 'unnh', 'oonnh', 'ennh', 'onnh',
    'ainnh', 'aunnh', 'iannh', 'iunnh', 'uinnh', 'oannh', 'oainnh',
    'iaunnh', 'oennh',
    # Standalone nasals
    'm', 'ng', 'mh', 'ngh',
))

def _strip_poj_to_input(syl):
    """Convert a POJ-Unicode syllable to its input-form base (no tone digit).
    Maps `o͘` → `oo` (the U+0358 turns the cluster into two `o`s) and `ⁿ` (U+207F)
    → `nn`. Drops all combining marks. Returns lowercase ASCII letters only."""
    nfd = unicodedata.normalize('NFD', syl).lower()
    out = []
    for ch in nfd:
        if unicodedata.combining(ch):
            if ch == '͘':
                out.append('o')  # second `o` of `o͘ → oo`
            # else: tone diacritics, trema-below, etc. — drop
            continue
        if ch == 'ⁿ':
            out.append('nn')
            continue
        out.append(ch)
    return ''.join(out)

def _is_valid_poj_syllable_unicode(syl):
    """True iff `syl` is a structurally valid POJ syllable (Unicode form).
    Accepts standalone nasals (m, ng). Mirrors `SyllableValidator.isValidUnicode`
    minus haikau/traditional-nasal options."""
    if not syl:
        return False
    base = _strip_poj_to_input(syl)
    if not base or not re.fullmatch(r'[a-z]+', base):
        return False
    if base in _POJ_RHYMES:
        return True
    for initial in _POJ_INITIALS:
        if base.startswith(initial):
            candidate = base[len(initial):]
            if candidate and candidate in _POJ_RHYMES:
                return True
    return False

# --- NEW: hyphenate_poj --------------------------------------------------------
# dicmyhk emits Taigi POJ without consistent hyphenation between syllables
# (e.g. `ūchîⁿ lâng` for what should be `ū-chîⁿ lâng`). Greedy maximum-munch
# segmentation: tokenize on existing whitespace + hyphens, then for each
# whitespace/hyphen-separated token try the longest prefix that satisfies
# `_is_valid_poj_syllable_unicode`. Failure-safe: if a token cannot be
# segmented, emit it unchanged (avoids mangling Hanji or noise that leaks in).

# First-letter set for POJ consonant initials (lowercase). All initials in
# _POJ_INITIALS start with one of these. Used by the maximum-onset principle:
# when multiple prefixes are valid syllables, prefer the break that gives the
# next syllable a consonant onset.
_POJ_INITIAL_FIRST_CHARS = frozenset(i[0] for i in _POJ_INITIALS)

# Vowel pairs that form a glide-onset rime — the first vowel acts as an on-glide
# /j/ or /w/, not as the coda of the prior syllable. Extends the maximum-onset
# principle: a break that gives the next syllable a glide onset is preferred
# over one that leaves the glide letter dangling as the prior syllable's coda
# (e.g. `ūiáⁿ` → `ū-iáⁿ`, not `ūi-áⁿ`).
_POJ_GLIDE_ONSET_SECONDS = {
    'i': frozenset('aeou'),  # ia, ie, io, iu (+ iau, iong, iann, etc.)
    'u': frozenset('i'),     # ui
    'o': frozenset('ae'),    # oa, oe (+ oai, oan, etc.)
}

def _next_syllable_onset_letters(token, start, count=2):
    """Return up to `count` lowercase base letters at `token[start:]`, skipping
    combining marks and ⁿ. Used to inspect the start of a candidate next
    syllable for the maximum-onset rule."""
    nfd = unicodedata.normalize('NFD', token[start:start + 4]).lower()
    out = []
    for ch in nfd:
        if unicodedata.combining(ch) or ch == 'ⁿ':
            continue
        out.append(ch)
        if len(out) >= count:
            break
    return out

def _starts_with_consonant_onset(token, start):
    letters = _next_syllable_onset_letters(token, start, 1)
    return bool(letters) and letters[0] in _POJ_INITIAL_FIRST_CHARS

def _starts_with_glide_onset(token, start):
    letters = _next_syllable_onset_letters(token, start, 2)
    if len(letters) < 2:
        return False
    seconds = _POJ_GLIDE_ONSET_SECONDS.get(letters[0])
    return seconds is not None and letters[1] in seconds

def _segment_poj_token(token):
    if not token:
        return token
    # Quick path: already a single valid syllable.
    if _is_valid_poj_syllable_unicode(token):
        return token
    n = len(token)
    syllables = []
    i = 0
    while i < n:
        # Collect ALL valid prefixes whose remainder is also segmentable.
        candidates = []
        for end in range(n, i, -1):
            prefix = token[i:end]
            if _is_valid_poj_syllable_unicode(prefix):
                if end == n or _can_segment(token[end:]):
                    candidates.append(end)
        if not candidates:
            return token  # cannot segment; emit unchanged
        # Maximum-onset principle, two-pass: a CONSONANT onset on the next
        # syllable always beats a glide onset, regardless of where the break
        # falls. Within each pass, longest-first order from `candidates` wins.
        # POJ syllable structure is `<onset?><nucleus>`; when an onset is
        # available right after a break point, it linguistically belongs to
        # the next syllable (`iúhān` → `iú-hān`, not `iúh-ān`; `ūiáⁿ` →
        # `ū-iáⁿ`, not `ūi-áⁿ`, because the `i` of `iáⁿ` is a glide onset,
        # not the coda of `ūi`). The two-pass ordering prevents a longer
        # glide-onset break from outranking a shorter consonant-onset break
        # (`amôa` → `a-môa` via `m`, not `am-ôa` via the `oa` glide).
        # If neither pass matches, fall back to the longest valid prefix.
        best = None
        for end in candidates:
            if end == n or _starts_with_consonant_onset(token, end):
                best = end
                break
        if best is None:
            for end in candidates:
                if _starts_with_glide_onset(token, end):
                    best = end
                    break
        if best is None:
            best = candidates[0]  # longest valid prefix
        syllables.append(token[i:best])
        i = best
    return '-'.join(syllables)

def _can_segment(suffix):
    """Cheap reachability check: can `suffix` be split into valid POJ syllables?"""
    if not suffix:
        return True
    n = len(suffix)
    # Iterative dynamic-programming reach set.
    reachable = [False] * (n + 1)
    reachable[0] = True
    for i in range(n):
        if not reachable[i]:
            continue
        for j in range(i + 1, n + 1):
            if reachable[j]:
                continue
            if _is_valid_poj_syllable_unicode(suffix[i:j]):
                reachable[j] = True
    return reachable[n]

def hyphenate_poj(text):
    """Insert hyphens between concatenated POJ syllables. Tokenize on existing
    whitespace, hyphens, and apostrophes (dicmyhk uses apostrophes
    interchangeably with hyphens as syllable separators, e.g. `iû'á`, `a'kū`);
    segment each letter-run via maximum-munch using the KonvertToPOJ-style
    validator. `ⁿ` is treated as syllable-final, not as a syllable break.
    Failure-safe on non-POJ input. Apostrophes are canonicalized to hyphens
    in the output."""
    if not text:
        return text
    # Normalize apostrophe → hyphen up-front. POJ convention: both are syllable
    # separators; modern usage favors `-`.
    text = text.replace("'", "-")
    tokens = re.split(r'([-\s]+)', text)
    for i, t in enumerate(tokens):
        if t and not re.fullmatch(r'[-\s]+', t):
            tokens[i] = _segment_poj_token(t)
    return ''.join(tokens)

# --- NEW: normalize_poj_unicode ------------------------------------------------
# Canonical POJ Section 21 tone-mark placement. Structurally identical to PFS's
# `_reposition_syllable` (same Section 21 rule). POJ differences:
#   - uses _POJ_TONE_COMBINING (POJ has tone 7 = macron; PFS does not)
#   - `o͘` (o + U+0358) is a single vowel for placement
#   - `ⁿ` (U+207F) is a syllable-final marker, not a vowel/consonant
#   - accepts input digits 1–8 and converts to diacritics
# Reference: lib/KonvertToPOJ/.../internal/ToneMarker.kt + ToneMap.kt.

def _poj_tokens(base_lc):
    """Tokenize a base-letter string for POJ tone placement. Returns a list of
    (token, start_index_in_base_lc). Treats `oo` (from o͘) and `ng` as single
    tokens. `ⁿ` should not be in the base — _strip ahead of this."""
    tokens = []
    i = 0
    while i < len(base_lc):
        if base_lc[i:i+2] == 'oo':
            tokens.append(('oo', i)); i += 2
        elif base_lc[i:i+2] == 'ng':
            tokens.append(('ng', i)); i += 2
        else:
            tokens.append((base_lc[i], i)); i += 1
    return tokens

def _poj_tone_position(base_lc):
    """POJ Section 21 placement. `base_lc` has had `o͘` collapsed to `oo` and
    `ⁿ` stripped. Returns the 0-indexed position in base_lc where the tone
    diacritic should land (start of an `oo`/`ng` token if applicable)."""
    tokens = _poj_tokens(base_lc)
    if not tokens:
        return None
    # POJ-specific vowel set includes `oo` as a single vowel unit.
    vowels = set('aeiou')
    vowel_ks = [k for k, (t, _) in enumerate(tokens) if t in vowels or t == 'oo']
    if not vowel_ks:
        for nasal in ('ng', 'm', 'n'):
            for t, pos in tokens:
                if t == nasal:
                    return pos
        return None
    if len(vowel_ks) == 1:
        return tokens[vowel_ks[0]][1]
    n = len(tokens)
    k = n - 2
    is_checked = tokens[-1][0] in ('p', 't', 'k', 'h')
    vowels_only = ''.join(tokens[vk][0] for vk in vowel_ks)
    if is_checked and tokens[k][0] in ('i', 'u') and vowels_only != 'iu':
        if n - 3 >= 0:
            return tokens[n - 3][1]
    if tokens[k][0] == 'i':
        return tokens[-1][1]
    return tokens[k][1]

def _poj_reposition_syllable(syl):
    """Re-place a POJ Unicode syllable's tone diacritic per Section 21. Input
    is Unicode-only — dicmyhk never serves clean digit-form Taigi. A small
    handful of dicmyhk entries (e.g. `ta lō͘2`, `chhài8`) contain a stray
    digit mixed with Unicode diacritics; rather than guess what they meant,
    we pass those through verbatim."""
    if not syl or any(ch.isdigit() for ch in syl):
        return syl
    nfd = unicodedata.normalize('NFD', syl)
    base = []
    combining = []  # list of [base_index, combining_char]
    for ch in nfd:
        if unicodedata.combining(ch):
            combining.append([len(base) - 1, ch])
        else:
            base.append(ch)
    # Position-mapping: build base_lc with `o͘`→`oo` and `ⁿ` removed, while
    # remembering how positions map back to the original `base` indices.
    base_lc_chars = []
    orig_index_of_lc_char = []
    i = 0
    while i < len(base):
        ch_lc = base[i].lower()
        if ch_lc == 'ⁿ':
            i += 1
            continue
        if ch_lc == 'o' and any(p == i and c == '͘' for p, c in combining):
            base_lc_chars.append('o')
            orig_index_of_lc_char.append(i)
            base_lc_chars.append('o')
            orig_index_of_lc_char.append(i)  # second `o` maps back to the same orig `o`
            i += 1
            continue
        base_lc_chars.append(ch_lc)
        orig_index_of_lc_char.append(i)
        i += 1
    base_lc = ''.join(base_lc_chars)

    tone_slot = next((k for k, (_, c) in enumerate(combining) if c in _POJ_TONE_COMBINING), None)
    if tone_slot is not None:
        target_lc = _poj_tone_position(base_lc)
        if target_lc is not None:
            combining[tone_slot][0] = orig_index_of_lc_char[target_lc]

    by_pos = {}
    for pos, ch in combining:
        by_pos.setdefault(pos, []).append(ch)
    out = []
    for i, b in enumerate(base):
        out.append(b)
        out.extend(by_pos.get(i, []))
    return unicodedata.normalize('NFC', ''.join(out))

def normalize_poj_unicode(text):
    """Apply canonical POJ Section 21 tone-mark placement on each syllable.
    Accepts mixed input (some syllables in input form with digits, some in
    Unicode). Idempotent on already-canonical POJ Unicode."""
    if not text:
        return text
    tokens = re.split(r'([-\s]+)', text)
    for i, t in enumerate(tokens):
        if t and not re.fullmatch(r'[-\s]+', t):
            tokens[i] = _poj_reposition_syllable(t)
    return ''.join(tokens)

# --- HTTP layer (lifted from sister project; path adapted for dicmyhk) ---------

_thread_local = threading.local()

def _get_conn():
    conn = getattr(_thread_local, 'conn', None)
    if conn is None:
        conn = http.client.HTTPSConnection('hakka.fhl.net', 443, timeout=HTTP_TIMEOUT)
        _thread_local.conn = conn
    return conn

def _drop_conn():
    conn = getattr(_thread_local, 'conn', None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
    _thread_local.conn = None

def fetch_id_data(id_val):
    """Fetch one dicmyhk entry by ID. Returns the parsed td/th-pair dict or
    None on HTTP error / no-such-ID. The `graph` query parameter is decorative
    on dicmyhk; passing empty matches the canonical URL shape."""
    path = f"/dicmyhk/search.php?DETAIL=1&LIMIT=id={id_val}&dbname=myktdic&graph="
    headers = {'User-Agent': 'Mozilla/5.0', 'Connection': 'keep-alive'}
    for attempt in range(HTTP_MAX_ATTEMPTS):
        try:
            conn = _get_conn()
            conn.request('GET', path, headers=headers)
            response = conn.getresponse()
            html_bytes = response.read()
            if response.status != 200:
                return None
            html = html_bytes.decode('utf-8', errors='ignore')
            parser = FHLParser()
            parser.feed(html)
            if not parser.all_data:
                return None
            data_dict = {}
            for j in range(0, len(parser.all_data) - 1, 2):
                data_dict[parser.all_data[j]] = parser.all_data[j+1]
            # Verify the row belongs to this ID. Past max-ID, dicmyhk emits a
            # "鍵值設定有問題" error message; the parsed dict either won't have
            # `編號` or will have it pointing to a different ID.
            if _LBL_ID in data_dict and data_dict[_LBL_ID] == str(id_val):
                return data_dict
            return None
        except Exception as e:
            _drop_conn()
            if attempt == HTTP_MAX_ATTEMPTS - 1:
                print(f"Error fetching ID {id_val}: {e}", file=sys.stderr)
                return None
    return None

# --- NEW: probe_max_id ---------------------------------------------------------
# dicmyhk has ~17,000 entries; 17033+ returns the error marker. Binary-search
# the max so a re-run automatically picks up any growth.

def _is_valid_id(data):
    """True iff `data` is a non-empty dict for a real entry. Catches the
    error-marker page (which may have an `編號` value of the queried ID but
    contains the error string in another field) and the empty-page case."""
    if not data:
        return False
    if any(_ERROR_MARKER in (v or '') for v in data.values()):
        return False
    # All seven payload fields empty → no entry.
    payload = [data.get(_LBL_HAKFA, ''), data.get(_LBL_HANZI, ''),
               data.get(_LBL_HAKFA_HL, ''), data.get(_LBL_TAIGI, ''),
               data.get(_LBL_TAIGI_HL, ''), data.get(_LBL_HUAGI, '')]
    return any(p for p in payload)

def probe_max_id(low=17000, high=20000):
    """Binary search for the highest valid ID. Pre-condition (validated by
    widening): `low` is a valid ID and `high` is invalid. Returns the highest
    ID for which `fetch_id_data` returns a real entry."""
    low_data = fetch_id_data(low)
    if not _is_valid_id(low_data):
        # `low` invalid — drop lower bound until we find a valid one.
        cur = low
        while cur > 1:
            cur //= 2
            if _is_valid_id(fetch_id_data(cur)):
                low = cur
                break
        else:
            return 0  # no valid entries at all (shouldn't happen)
    while _is_valid_id(fetch_id_data(high)):
        high *= 2
        if high > 1_000_000:  # sanity cap
            break
    while low + 1 < high:
        mid = (low + high) // 2
        if _is_valid_id(fetch_id_data(mid)):
            low = mid
        else:
            high = mid
    return low

# --- Manifest + CSV→JSON helpers (lifted from sister project) ------------------

def update_manifest(version_id):
    manifest_path = 'public/manifest.json'
    now = datetime.now().strftime('%Y-%m-%d')
    if os.path.exists(manifest_path):
        with open(manifest_path, 'r', encoding='utf-8') as f:
            manifest = json.load(f)
    else:
        manifest = {"latest_version": "", "last_updated": "", "versions": []}
    manifest["latest_version"] = version_id
    manifest["last_updated"] = now
    if version_id not in manifest["versions"]:
        manifest["versions"].append(version_id)
    os.makedirs(os.path.dirname(manifest_path), exist_ok=True)
    with open(manifest_path, 'w', encoding='utf-8') as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

def csv_to_json(csv_path, json_path):
    data = []
    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            data.append(row)
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

# --- Main pipeline -------------------------------------------------------------

def scrape(max_id_override=None):
    version_id = datetime.now().strftime('%Y%m%d-%H%M')
    bunji_dir = f'public/{version_id}/bunji'
    tangloo_dir = f'public/{version_id}/tangloo'
    os.makedirs(bunji_dir, exist_ok=True)
    os.makedirs(tangloo_dir, exist_ok=True)

    bunji_csv = os.path.join(bunji_dir, 'HakfaFHLDictMyHk.csv')
    bunji_json = os.path.join(bunji_dir, 'HakfaFHLDictMyHk.json')
    tangloo_csv = os.path.join(tangloo_dir, 'HakfaFHLDictMyHk.csv')
    tangloo_json = os.path.join(tangloo_dir, 'HakfaFHLDictMyHk.json')

    if max_id_override is not None:
        max_id = max_id_override
        print(f"Using override max_id={max_id} (skipping probe).")
    else:
        print("Probing upstream max ID via binary search...")
        max_id = probe_max_id()
        print(f"Resolved max_id={max_id}")

    tangloo_columns = [
        'ID',
        'Hakfa_FHL_Unicode',
        'Hanzi',
        'Hakfa_HanLo',
        'Taigi_FHL_Unicode',
        'Taigi_HanLo',
        'Hua-gi',
    ]
    bunji_columns = [
        'ID',
        'Hakfa_FHL_Unicode',
        'PFS_Unicode',
        'PFS_Numeric',
        'Hakfa_IPA',
        'Taigi_FHL_Unicode',
        'POJ_Unicode',
        'Hanzi',
        'Hakfa_HanLo',
        'Taigi_HanLo',
        'Hua-gi',
    ]

    print(f"Scraping 客台MY小詞典 (workers={SCRAPE_WORKERS})...")
    print(f"  tangloo: {tangloo_csv} (raw dicmyhk)")
    print(f"  bunji:   {bunji_csv} (PFS+IPA, canonical POJ)")

    def fetch_one(i):
        data = fetch_id_data(i)
        if not _is_valid_id(data):
            return i, None, None

        hakfa_fhl = data.get(_LBL_HAKFA, '')
        hanzi = data.get(_LBL_HANZI, '')
        hakfa_hl = data.get(_LBL_HAKFA_HL, '')
        taigi_fhl = data.get(_LBL_TAIGI, '')
        taigi_hl = data.get(_LBL_TAIGI_HL, '')
        huagi = data.get(_LBL_HUAGI, '')

        tangloo_row = [i, hakfa_fhl, hanzi, hakfa_hl, taigi_fhl, taigi_hl, huagi]

        # Hak-fa pipeline: re-place per POJ Section 21, KPPY uV → PFS oV,
        # derive numeric form, derive IPA.
        pfs_unicode = fhl_unicode_to_pfs_unicode(hakfa_fhl)
        pfs_unicode = normalize_kppy_uv_onglide_to_pfs(pfs_unicode)
        pfs_numeric = pfs_unicode_to_pfs_numeric(pfs_unicode)
        ipa_val = pfs_to_ipa(pfs_numeric)

        # Taigi pipeline: hyphenate compound tokens, canonicalize placement.
        poj_unicode = hyphenate_poj(taigi_fhl)
        poj_unicode = normalize_poj_unicode(poj_unicode)

        bunji_row = [i, hakfa_fhl, pfs_unicode, pfs_numeric, ipa_val,
                     taigi_fhl, poj_unicode,
                     hanzi, hakfa_hl, taigi_hl, huagi]
        return i, tangloo_row, bunji_row

    with open(tangloo_csv, 'w', newline='', encoding='utf-8') as tf, \
         open(bunji_csv, 'w', newline='', encoding='utf-8') as bf, \
         ThreadPoolExecutor(max_workers=SCRAPE_WORKERS) as pool:
        tangloo_writer = csv.writer(tf)
        bunji_writer = csv.writer(bf)
        tangloo_writer.writerow(tangloo_columns)
        bunji_writer.writerow(bunji_columns)

        for i, tangloo_row, bunji_row in pool.map(fetch_one, range(1, max_id + 1)):
            if tangloo_row is None:
                if i % 500 == 0:
                    print(f"Processed up to ID {i} (skipped)...")
                continue
            tangloo_writer.writerow(tangloo_row)
            bunji_writer.writerow(bunji_row)
            if i % 500 == 0:
                tf.flush(); bf.flush()
                print(f"Processed up to ID {i}...")
                sys.stdout.flush()

    print("Generating JSON...")
    csv_to_json(tangloo_csv, tangloo_json)
    csv_to_json(bunji_csv, bunji_json)

    print("Updating manifest...")
    update_manifest(version_id)
    print(f"Done! Version: {version_id}")

if __name__ == '__main__':
    # CLI: `python3 script/scraper.py [MAX_ID_OVERRIDE]` — passing an integer
    # skips the probe and runs against the given max ID (useful for smoke tests).
    override = int(sys.argv[1]) if len(sys.argv) > 1 else None
    scrape(max_id_override=override)
