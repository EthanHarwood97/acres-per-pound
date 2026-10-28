"""Extract land area (in acres) from UK property listing text.

Listings rarely state land size in a structured field, so this module
mines free text: key features, summaries and full descriptions.

Handles:
  - acres:  "2.5 acres", "0.4 of an acre", "half an acre", "one and a
    half acres", "2.5 - 3 acres", "between 2 and 3 acres"
  - hectares: "0.75 hectares" / "1.2 ha"   (x 2.47105)
  - sq ft / sq m of grounds (converted, only with land context words)
  - qualifiers: about / approx / just over / in excess of / in all /
    extending to / stms (subject to measured survey)

Confidence levels: exact, approx, range, partial, converted.
"""

import re

ACRES_PER_HECTARE = 2.47105
SQFT_PER_ACRE = 43560.0
SQM_PER_ACRE = 4046.86

_WORD_NUM = {
    "one": 1.0, "a": 1.0, "an": 1.0, "two": 2.0, "three": 3.0,
    "four": 4.0, "five": 5.0, "six": 6.0, "seven": 7.0, "eight": 8.0,
    "nine": 9.0, "ten": 10.0, "eleven": 11.0, "twelve": 12.0,
    "thirteen": 13.0, "fourteen": 14.0, "fifteen": 15.0, "sixteen": 16.0,
    "seventeen": 17.0, "eighteen": 18.0, "nineteen": 19.0, "twenty": 20.0,
    "quarter": 0.25, "half": 0.5, "three quarters": 0.75, "three-quarters": 0.75,
    "an eighth": 0.125,
}

# Word-number alternative for regex (longest first to avoid partial hits).
# NOTE: a/an excluded here — handled separately below to avoid stealing
# "half an acre" / "0.4 of an acre".
_WORD_NUM_PATTERN = (
    r"(?:twenty|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|"
    r"eleven|twelve|three|seven|eight|ten|one|two|four|five|six|nine)"
)

# Road-type words right after a measurement usually mean a street name,
# e.g. "1 Acre Lane" or "on 5 Acres Road" - reject those candidates.
_ROAD_WORDS = re.compile(
    r"^(?:road|street|lane|close|avenue|drive|way|gardens|court|crescent|"
    r"hill|park|walk|fields|rise|mews|terrace|boulevard)$"
)

_NUM = r"(\d+(?:[.,]\d+)?)"
_NUM_THOU = r"(\d[\d,]*(?:\.\d+)?)"
_RANGE_SEP = r"\s*(?:to|-|–|—)\s*"

LAND_CONTEXT = (
    r"(?:garden|gardens|grounds|land|plot|plots|paddock|paddocks|field|fields|"
    r"pasture|meadow|meadows|orchard|woodland|woodlands|grounds|smallholding|"
    r"equestrian|surrounding)"
)

# Phrases where the measured land is NOT owned by the buyer: communal
# grounds, estate parkland, access rights, and estate-agent copy about
# the wider development ("set within 500 acres of clay country", "a
# private 550 acre estate"). Left unflagged, those make a terraced house
# on a housing estate claim hundreds of acres.
_ESTATE_NOUN = (r"(?:estate|village|resort|development|country\s+park|park|"
                r"nature\s+reserve|parkland|landscape|countryside|country|"
                r"common|moor|heath|golf|leisure|amenity)")

_COMMUNAL = re.compile(
    r"\b(?:communal|shared)\s+(?:grounds|gardens|land|amenity|parkland)\b|"
    r"\bin\s+the\s+grounds\s+of\b|"
    r"\b(?:access|use|rights?)\s+(?:to|of|over)\s+(?:the\s+)?\d+(?:\.\d+)?\s*acres?\b|"
    r"\boverlooking\s+\d+(?:\.\d+)?\s*acres?\b|"
    # "550 acre estate", "150-acre country park", "200 acres of rolling
    # countryside" - the acreage describes the surroundings, not the plot
    r"\b\d[\d,]*(?:\.\d+)?\s*(?:-|\s)?(?:acres?|hectares?)\s+(?:of\s+)?"
    r"(?:\w+\s+){0,2}?" + _ESTATE_NOUN + r"\b|"
    # "set within 500 acres", "situated within a 300 hectare estate"
    r"\b(?:set|situated|nestled|located|positioned)\s+within\b[^.]{0,60}?"
    r"\b\d[\d,]*(?:\.\d+)?\s*(?:-|\s)?(?:acres?|hectares?)\b|"
    # "part of a 40 acre development"
    r"\b(?:part\s+of|on)\s+(?:a|the|this)\s+\d[\d,]*(?:\.\d+)?\s*(?:-|\s)?acre\b|"
    # "the estate extends to 500 acres" - deliberately excludes
    # site/development, which in land listings is the property itself
    # ("the site extends to 0.27 hectares" is the plot for sale)
    r"\b(?:estate|village|resort)\b[^.]{0,40}?"
    r"\b\d[\d,]*(?:\.\d+)?\s*(?:-|\s)?(?:acres?|hectares?)\b",
    re.I)


def has_land_keyword(text):
    """True if the text hints at land, used to decide detail-page fetches."""
    t = (text or "").lower()
    return bool(re.search(
        r"\b(?:acres?|hectares?|\bha\b|paddock|smallholding|equestrian|"
        r"pasture|meadow|orchard|woodland|plots?|grounds|acreage|"
        r"perch(?:es)?|roods?|poles?)\b", t))


class _Cand:
    __slots__ = ("value", "unit", "start", "end", "qualifier", "total", "stms")

    def __init__(self, value, unit, start, end, qualifier="", total=False, stms=False):
        self.value = value
        self.unit = unit
        self.start = start
        self.end = end
        self.qualifier = qualifier
        self.total = total
        self.stms = stms


def _normalize(text):
    t = (text or "").lower()
    t = t.replace("\u00bd", " half ").replace("\u00bc", " quarter ")
    t = t.replace("\u00be", " three quarters ")
    t = t.replace("&amp;", "&")
    return t


def _num(s):
    s = s.strip().lower()
    if s in _WORD_NUM:
        return _WORD_NUM[s]
    # European decimal "0,5" -> 0.5, but "5,000" -> 5000.
    # Single comma with 1-2 trailing digits and no dot = decimal.
    if "," in s and "." not in s:
        if re.match(r"^\d+,\d{1,2}$", s):
            s = s.replace(",", ".")
        else:
            s = s.replace(",", "")
    else:
        s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def _stms_after(t, end):
    return bool(re.search(r"\bstms\b|\bsubject to measured survey\b|\btbv\b", t[end:end + 90]))


def _annotate(t, c):
    """Tag a candidate with qualifier/total flags from surrounding text."""
    pre = t[max(0, c.start - 45):c.start]
    post = t[c.end:c.end + 45]
    if re.search(r"(?:just\s+over|in\s+excess\s+of|more\s+than|at\s+least)\s*$", pre):
        c.qualifier = "min"
    elif re.search(r"(?:just\s+under|up\s+to|approaching)\s*$", pre):
        c.qualifier = "max"
    elif re.search(r"(?:about|approx(?:imately)?|around|circa|nearly|some|in\s+the\s+region\s+of)\s*$", pre):
        c.qualifier = "approx"
    if re.search(r"(?:in\s+all|in\s+total)\s*(?:to\s+)?$", pre) or \
       re.search(r"^\s*(?:in\s+all|in\s+total)\b", post):
        c.total = True
    return c


def _candidates(text):
    t = _normalize(text)
    out = []

    # word fractions: "half an acre", "three quarters of an acre"
    for m in re.finditer(r"(?<![\w])(three\s*[- ]?quarters|an eighth|a quarter|quarter|half)\s+(?:of\s+)?an?\s+acre\b", t):
        v = _WORD_NUM.get(re.sub(r"\s+", " ", m.group(1).strip()))
        if v is not None:
            out.append(_annotate(t, _Cand(v, "acre", m.start(), m.end(), stms=_stms_after(t, m.end()))))

    # "one and a half acres"
    for m in re.finditer(r"(?<![\w])(one|two|three|four|five|six|seven|eight|nine|ten)\s+and\s+a\s+half\s+acres?\b", t):
        base = _WORD_NUM.get(m.group(1))
        if base is not None:
            out.append(_annotate(t, _Cand(base + 0.5, "acre", m.start(), m.end(), stms=_stms_after(t, m.end()))))

    # numeric fractions: "1/2 acre", "3/4 acres" (must run before plain number)
    for m in re.finditer(r"(?<![\w])(\d+)\s*/\s*(\d+)\s*acres?\b", t):
        try:
            num, den = int(m.group(1)), int(m.group(2))
        except ValueError:
            continue
        if den == 0 or num > den * 20:
            continue  # likely a date or reference, not a fraction
        v = num / den
        out.append(_annotate(t, _Cand(v, "acre", m.start(), m.end(), stms=_stms_after(t, m.end()))))

    # word-number + acres: "five acres", "one acre", "twenty acres"
    for m in re.finditer(
            rf"(?<![\w])({_WORD_NUM_PATTERN})\s*(?:\+?\s*)acres?\b", t):
        v = _num(m.group(1))
        if v is None:
            continue
        tail = t[m.end():m.end() + 25].strip()
        tail_word = re.split(r"[\s,.;:!()]+", tail)[0] if tail else ""
        if _ROAD_WORDS.match(tail_word):
            continue
        out.append(_annotate(t, _Cand(v, "acre", m.start(), m.end(), stms=_stms_after(t, m.end()))))

    # singular "an acre" / "a acre" = 1.0 — but not when part of
    # "half an acre" / "quarter of an acre" / "0.4 of an acre".
    for m in re.finditer(r"(?<![\w])an?\s+acres?\b", t):
        pre = t[max(0, m.start() - 20):m.start()]
        if re.search(r"(?:half|quarter|quarters|eighth|of|\d)\s*$", pre):
            continue
        out.append(_annotate(t, _Cand(1.0, "acre", m.start(), m.end(), stms=_stms_after(t, m.end()))))

    # "a couple of acres" ~= 2
    for m in re.finditer(r"(?<![\w])a\s+couple\s+of\s+acres?\b", t):
        out.append(_annotate(t, _Cand(2.0, "acre", m.start(), m.end(), qualifier="approx",
                                        stms=_stms_after(t, m.end()))))

    # number + acres / hectares (allows trailing "+" = minimum, e.g. "2+ acres")
    for unit, factor in (("acre", 1.0), ("hectare", ACRES_PER_HECTARE)):
        for m in re.finditer(rf"(?<![\w\d/]){_NUM}\s*\+?\s*(?:[-–]\s*)?{unit}s?\b", t):
            v = _num(m.group(1))
            if v is None:
                continue
            tail = t[m.end():m.end() + 25].strip()
            tail_word = re.split(r"[\s,.;:!()]+", tail)[0] if tail else ""
            if _ROAD_WORDS.match(tail_word):
                continue
            # guard against the denominator of a fraction leaking through
            # (fraction branch already handled "1/2 acre")
            pre = t[max(0, m.start() - 4):m.start()]
            if re.search(r"/\s*$", pre):
                continue
            cand = _Cand(v * factor, unit, m.start(), m.end(), stms=_stms_after(t, m.end()))
            cand = _annotate(t, cand)
            if "+" in m.group(0) and cand.qualifier == "":
                cand.qualifier = "min"
            out.append(cand)

    # "1.2 ha" standalone
    for m in re.finditer(rf"(?<![\w]){_NUM}\s*ha\b", t):
        v = _num(m.group(1))
        if v is not None:
            out.append(_annotate(t, _Cand(v * ACRES_PER_HECTARE, "hectare", m.start(), m.end(), stms=_stms_after(t, m.end()))))

    # "0.4 of an acre"
    for m in re.finditer(rf"(?<![\w]){_NUM}\s+of\s+an?\s+acre\b", t):
        v = _num(m.group(1))
        if v is not None:
            out.append(_annotate(t, _Cand(v, "acre", m.start(), m.end(), stms=_stms_after(t, m.end()))))

    # ranges: "2.5 - 3 acres", "between 2 and 3 acres"
    for m in re.finditer(
        rf"(?<![\w])(?:between\s+{_NUM}\s+and\s+{_NUM}|{_NUM}(?:{_RANGE_SEP}){_NUM})\s*acres?\b", t):
        nums = re.findall(_NUM, m.group(0))
        a, b = _num(nums[0]), _num(nums[1])
        if a is None or b is None:
            continue
        lo, hi = min(a, b), max(a, b)
        # reject joined-up ranges of unrelated numbers, e.g.
        # "7,351 sq ft - 0.96 acres" or "10 to 500 acres" (scale mismatch)
        mtext = m.group(0)
        between = mtext[mtext.find(nums[0]) + len(nums[0]):mtext.find(nums[1])]
        if re.search(r"\b(?:sq|ft|m\b|metres?|meters?|ha)\b", between) or hi / max(lo, 1e-9) > 50 \
                or hi > 5000:
            continue
        out.append(_annotate(t, _Cand((lo + hi) / 2.0, "acre", m.start(), m.end(),
                                      qualifier="range", stms=_stms_after(t, m.end()))))

    # traditional units: perches/poles/rods (1/160 ac), roods (1/4 ac), sq yards (1/4840 ac)
    for m in re.finditer(rf"(?<![\w]){_NUM_THOU}\s*(perch(?:es)?|poles?|rods?|roods?)\b", t):
        raw = _num(m.group(1))
        if raw is None:
            continue
        unit_word = m.group(2).lower()
        div = 4.0 if unit_word.startswith("rood") else 160.0
        v = raw / div
        window = t[max(0, m.start() - 80):m.end() + 80]
        # perches need land context (else "fishing rods" etc); roods almost always land
        if unit_word.startswith("rood") or re.search(LAND_CONTEXT, window):
            out.append(_annotate(t, _Cand(v, "acre", m.start(), m.end(), stms=_stms_after(t, m.end()))))
    for m in re.finditer(rf"(?<![\w]){_NUM_THOU}\s*(?:sq\.?\s*(?:yds?|yards?)|square\s*yards?)\b", t):
        raw = _num(m.group(1))
        if raw is None or raw < 500:
            continue
        window = t[max(0, m.start() - 120):m.end() + 120]
        if re.search(LAND_CONTEXT, window):
            out.append(_Cand(raw / 4840.0, "sqyd", m.start(), m.end(), qualifier="converted"))

    # sq ft / sq m of land (converted) — incl. "sq metres", "sqm", decimals
    for m in re.finditer(
            rf"(?<![\w]){_NUM_THOU}\s*(?:sq\.?\s*ft|sq\.?\s*feet|sqft|square\s*feet|square\s*foot)\b", t):
        raw = _num(m.group(1))
        if raw is None or raw < 3000:
            continue
        window = t[max(0, m.start() - 120):m.end() + 120]
        if re.search(LAND_CONTEXT, window):
            out.append(_Cand(raw / SQFT_PER_ACRE, "sqft", m.start(), m.end(), qualifier="converted"))
    for m in re.finditer(
            rf"(?<![\w]){_NUM_THOU}\s*(?:sq\.?\s*m(?:etres?|eters?)?|sqm|square\s*metres?|square\s*meters?)\b", t):
        raw = _num(m.group(1))
        if raw is None or raw < 300:
            continue
        window = t[max(0, m.start() - 120):m.end() + 120]
        if re.search(LAND_CONTEXT, window):
            out.append(_Cand(raw / SQM_PER_ACRE, "sqm", m.start(), m.end(), qualifier="converted"))

    # plot dimensions L x W (e.g. "plot 100ft x 50ft", "garden 30m x 20m").
    # Requires land context to avoid room sizes ("kitchen 20 x 15 ft").
    for m in re.finditer(
            r"(?<![\w])(\d+(?:\.\d+)?)\s*(ft|feet|m|metres?|meters?|yds?|yards?)\s*"
            r"[x×]\s*(\d+(?:\.\d+)?)\s*(ft|feet|m|metres?|meters?|yds?|yards?)\b", t):
        try:
            a, b = float(m.group(1)), float(m.group(3))
        except ValueError:
            continue
        u1, u2 = m.group(2).lower(), m.group(4).lower()
        # mixed units (rare) — normalise both to feet
        def _to_ft(v, u):
            if u.startswith("m"):
                return v * 3.28084
            if u.startswith("yd") or u.startswith("yd"):
                return v * 3.0
            if u.startswith("y"):
                return v * 3.0
            return v
        a_ft, b_ft = _to_ft(a, u1), _to_ft(b, u2)
        sqft = a_ft * b_ft
        if sqft < 3000 or sqft > 435600:  # <0.07ac or >10ac unlikely a dimension plot
            continue
        window = t[max(0, m.start() - 120):m.end() + 120]
        if re.search(LAND_CONTEXT + r"|plot|site", window):
            out.append(_Cand(sqft / SQFT_PER_ACRE, "sqft", m.start(), m.end(), qualifier="converted"))

    # drop plain candidates swallowed by a range candidate
    ranges = [c for c in out if c.qualifier == "range"]
    out = [c for c in out
           if not (c.qualifier == "" and any(r.start <= c.start and r.end >= c.end for r in ranges))]
    return out


def _score(c):
    s = 0
    if c.unit == "acre":
        s += 6
    if c.qualifier == "range":
        s += 3
    if c.total:
        s += 4
    if c.qualifier in ("min", "max"):
        s -= 2
    if c.qualifier == "converted":
        s -= 4
    if c.stms:
        s -= 1
    if c.qualifier in ("", "approx"):
        s += min(2.0, c.value / 10.0)
    return s


def parse_acres(text):
    """Return (acres_min, acres_max, acres_mid, unit, confidence, matched, candidates, communal).

    None when no land size can be extracted. `communal` is True when the
    text suggests the measured land is shared/communal rather than owned.
    """
    t = _normalize(text)
    cands = _candidates(text)
    if not cands:
        return None
    best = max(cands, key=_score)
    matched = t[best.start:best.end].strip()
    communal = bool(_COMMUNAL.search(t))

    if best.qualifier == "range":
        m = re.search(rf"{_NUM}(?:{_RANGE_SEP}|\s+and\s+){_NUM}", matched)
        lo = hi = best.value
        if m:
            lo_v, hi_v = _num(m.group(1)), _num(m.group(2))
            if lo_v is not None and hi_v is not None:
                lo, hi = min(lo_v, hi_v), max(lo_v, hi_v)
        return (round(lo, 3), round(hi, 3), round(best.value, 3), best.unit,
                "range", matched, _cand_info(cands), communal)
    if best.qualifier == "min":
        return (round(best.value, 3), None, round(best.value, 3), best.unit,
                "partial", matched, _cand_info(cands), communal)
    if best.qualifier == "max":
        return (0.0, round(best.value, 3), round(best.value * 0.9, 3), best.unit,
                "partial", matched, _cand_info(cands), communal)
    if best.qualifier == "converted":
        v = round(best.value, 3)
        return (v, v, v, best.unit, "converted", matched, _cand_info(cands), communal)
    conf = "exact" if (best.qualifier == "" and not best.stms) else "approx"
    v = round(best.value, 3)
    return (v, v, v, best.unit, conf, matched, _cand_info(cands), communal)


def _cand_info(cands):
    return [
        {"value": round(c.value, 3), "unit": c.unit, "qualifier": c.qualifier or "plain",
         "total": c.total, "stms": c.stms}
        for c in sorted(cands, key=_score, reverse=True)[:5]
    ]
