#!/usr/bin/env python3
"""Credit-union knowledge system for the interface.ai knowledge hackathon.

Fully local, standard-library only. No live web calls and no paid APIs.

ingest() keeps every page from every snapshot (with its date) and:
  * parses markdown rate tables into structured facts
  * extracts hours, addresses, phones, routing numbers, branch-count
    claims, closures, and prose rates (e.g. credit cards)
  * chunks pages for BM25 retrieval

answer() filters to snapshots on or before as_of (or the latest
snapshot), ranks facts by page authority + recency, and composes a
short extractive answer. Canonical rate pages beat calculators, blogs,
press releases, and pages with older "effective as of" dates. Standard
/ non-jumbo / lowest loan-amount tiers are led with. If nothing solid
is found, the system refuses ("I don't know", confident=False).

conflicts() reports places the site contradicts itself on the same
qualified fact (same product, term, tier, metric, and model-year band).
Personal vs business, different tiers, and rate vs APY are not conflicts.
"""
from __future__ import annotations

import math
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

def dataclass(cls):
    """Minimal stand-in so the file works when loaded via importlib (no sys.modules)."""
    anns = getattr(cls, "__annotations__", {})
    defaults = {}
    for k, v in list(cls.__dict__.items()):
        if k in anns:
            defaults[k] = v
    fields = list(anns.keys())

    def __init__(self, *args, **kwargs):
        i = 0
        for name in fields:
            if name in kwargs:
                setattr(self, name, kwargs.pop(name))
            elif i < len(args):
                setattr(self, name, args[i])
                i += 1
            elif name in defaults:
                setattr(self, name, defaults[name])
            else:
                raise TypeError(f"missing argument {name}")
        if i < len(args) or kwargs:
            raise TypeError("unexpected arguments")

    def __repr__(self):
        inner = ", ".join(f"{n}={getattr(self, n)!r}" for n in fields)
        return f"{cls.__name__}({inner})"

    cls.__init__ = __init__
    cls.__repr__ = __repr__
    return cls


from urllib.parse import urlparse

try:
    from contract import Answer, Conflict, Snapshot, Source
except ImportError:  # pragma: no cover
    here = Path(__file__).resolve()
    for d in [here.parent, *here.parents]:
        cand = d / "harness" / "contract.py"
        if cand.exists():
            sys.path.insert(0, str(cand.parent))
            break
    from contract import Answer, Conflict, Snapshot, Source  # noqa: E402


WORD_RE = re.compile(r"[a-z0-9$%.]+")
STOP = {
    "a", "an", "the", "and", "or", "of", "to", "for", "on", "in", "at", "by",
    "is", "are", "was", "were", "be", "been", "am", "do", "does", "did",
    "what", "what's", "whats", "which", "who", "whom", "how", "when", "where",
    "can", "could", "would", "should", "i", "me", "my", "you", "your", "we",
    "our", "us", "it", "its", "this", "that", "these", "those", "with", "from",
    "as", "if", "than", "then", "so", "just", "about", "into", "over", "after",
    "tell", "please", "get", "give", "know", "like", "want",
}

MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}

DATE_ISO = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")
DATE_MDY = re.compile(r"\b(\d{1,2})/(\d{1,2})/(20\d{2})\b")
DATE_LONG = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|"
    r"October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|"
    r"Oct|Nov|Dec)\.?\s+(\d{1,2}),?\s+(20\d{2})\b",
    re.I,
)
DATE_MON_YEAR = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|"
    r"October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|"
    r"Oct|Nov|Dec)\.?\s+(20\d{2})\b",
    re.I,
)
EFFECTIVE_RE = re.compile(
    r"effective(?:\s+as\s+of)?\s+(\d{1,2}/\d{1,2}/20\d{2}|20\d{2}-\d{2}-\d{2}|"
    r"(?:January|February|March|April|May|June|July|August|September|"
    r"October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|"
    r"Oct|Nov|Dec)\.?\s+\d{1,2},?\s+20\d{2})",
    re.I,
)
PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")
TERM_MONTHS_RE = re.compile(r"\b(\d+)\s*[\-/]?\s*(month|mo|year|yr)s?\b", re.I)
ARM_RE = re.compile(r"\b(\d+)\s*/\s*(\d+)\b")
MODEL_YR_RE = re.compile(r"\b(20\d{2})\s*[–\-]\s*(?:20)?(\d{2})\s*model\s*year", re.I)
MODEL_YR_RE_FLIP = re.compile(r"\bmodel\s*year\s*(20\d{2})\s*[–\-]\s*(?:20)?(\d{2})\b", re.I)
MODEL_YR_Q = re.compile(r"\b(20\d{2})\s*(?:model[\s-]?year|vehicle|model)\b", re.I)
ROUTING_RE = re.compile(r"\b(routing(?:\s+number)?)\s*[:\s]+(\d{9})\b", re.I)
ROUTING_ANY = re.compile(r"\b(27\d{7})\b")
PHONE_RE = re.compile(r"\b(800|888|877|866)[-.\s]?(\d{3})[-.\s]?(\d{4})\b")

CANONICAL_PATHS = {
    "/personal/savings-account": 9.0,
    "/personal/savings/holiday-club-account": 5.0,
    "/rates": 8.5,
    "/business-rates": 7.5,
    "/personal/loans/auto-motorcycle-loans": 9.0,
    "/personal/loans/auto-loans": 8.0,
    "/personal/loans/home-loans": 9.0,
    "/personal/loans/personal-loans": 9.0,
    "/personal/loans/home-equity": 8.0,
    "/personal/loans/home-improvement-loan": 6.5,
    "/personal/loans/student-loans": 8.0,
    "/personal/credit-cards": 9.0,
    "/business/credit-cards": 8.5,
    "/personal/checking-accounts": 9.0,
    "/checking": 7.0,
    "/contact": 8.0,
    "/about/join": 8.0,
    "/about": 5.0,
    "/business/cash-management": 6.5,
    "/business/checking-accounts": 6.0,
    "/business/savings-accounts": 6.0,
    "/personal/retirement": 5.0,
    "/switch-and-earn": 4.0,
    "/first-time-homebuyer-grant": 5.5,
    "/savings-account/online-savings-account": 6.0,
}


def _norm_path(url: str) -> str:
    p = urlparse(url if "://" in url else "https://" + url)
    return (p.path or "/").rstrip("/").lower() or "/"


def page_authority(url: str, title: str = "") -> float:
    path = _norm_path(url)
    host = urlparse(url if "://" in url else "https://" + url).netloc.lower()
    score = 1.0
    if host.startswith("info."):
        return -4.0
    if path in CANONICAL_PATHS:
        score = CANONICAL_PATHS[path]
    else:
        for pref, s in CANONICAL_PATHS.items():
            if path.startswith(pref):
                score = max(score, s - 1.0)
        if "/personal/" in path or path.endswith("/rates"):
            score = max(score, 4.0)
    title_l = (title or "").lower()
    if "/calculators" in path or "calculator" in path or "calculator" in title_l:
        score -= 6.0
    if "/hubfs/" in path or path.endswith(".pdf") or path.endswith(".xlsx"):
        score -= 5.0
    if re.search(r"/20\d{2}/\d{2}/", path):
        score -= 3.5
    if "/learn/blog" in path or "/learn/" in path:
        score -= 2.0
    if "/newsroom/" in path:
        score -= 1.2
    if "homebuying-guidance" in path:
        score -= 4.0
    if "/promo" in path or path.endswith("/promo"):
        score -= 2.5
    if "privacy" in path or "photo-release" in path:
        score -= 2.0
    if path in ("/", ""):
        score = max(score, 3.0)
    return score


def page_kind(url: str) -> str:
    raw = url or ""
    path = _norm_path(raw)
    host = urlparse(raw if "://" in raw else "https://" + raw).netloc.lower()
    pl = path.lower()
    if "/calculators" in pl or "calculator" in pl:
        return "calculator page"
    if "/hubfs/" in pl or pl.endswith(".pdf") or pl.endswith(".xlsx"):
        return "PDF"
    if host.startswith("info."):
        return "legacy microsite"
    if "homebuying-guidance" in pl:
        return "guidance page"
    if (
        "/promo" in pl or "switch-and-earn" in pl or pl.endswith("-lp")
        or "offer-expired" in pl or "/offer" in pl
    ):
        return "promotional page"
    if re.search(r"/20\d{2}/\d{2}/", pl) or "/learn/blog" in pl:
        return "blog post"
    if "/learn/" in pl:
        return "article"
    if "/newsroom/" in pl:
        return "newsroom page"
    if path == "/business-rates":
        return "business rates page"
    if path == "/rates" or pl.endswith("/rates"):
        return "official rate page"
    if path in CANONICAL_PATHS and CANONICAL_PATHS[path] >= 8.0:
        return "official rate page"
    if "/personal/" in pl or "/business/" in pl:
        return "official product page"
    seg = pl.rstrip("/").split("/")[-1] or "page"
    seg = seg.replace("-", " ")
    if not seg.endswith("page"):
        seg = seg + " page"
    return seg


def _path_segments(url: str):
    return [s for s in _norm_path(url).split("/") if s]


def page_label(url: str, other_url: Optional[str] = None) -> str:
    """Short source label; more specific when both pages would share a kind."""
    kind = page_kind(url)
    if other_url and page_kind(other_url) == kind:
        parts = _path_segments(url)
        other = _path_segments(other_url)
        if parts and other and parts[-1] == other[-1] and len(parts) >= 2:
            seg = parts[-2].replace("-", " ") + " " + parts[-1].replace("-", " ")
        else:
            seg = (parts[-1] if parts else "page").replace("-", " ")
        if not seg.endswith("page"):
            seg = seg + " page"
        return seg
    return kind


def parse_date(text: str) -> Optional[date]:
    if not text:
        return None
    text = text.strip()
    if re.match(r"20\d{2}-\d{2}-\d{2}T", text):
        text = text.split("T", 1)[0]
    m = DATE_ISO.search(text)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    m = re.search(r"\b(20\d{2})[./](\d{1,2})[./](\d{1,2})\b", text)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    m = DATE_MDY.search(text)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
        except ValueError:
            pass
    m = DATE_LONG.search(text)
    if m:
        mon = MONTHS[m.group(1).lower().rstrip(".")]
        try:
            return date(int(m.group(3)), mon, int(m.group(2)))
        except ValueError:
            pass
    return None


def parse_month_end(text: str) -> Optional[date]:
    d = parse_date(text)
    if d:
        return d
    m = DATE_MON_YEAR.search(text or "")
    if not m:
        return None
    mon = MONTHS[m.group(1).lower().rstrip(".")]
    year = int(m.group(2))
    if mon == 12:
        return date(year, 12, 31)
    return date(year, mon + 1, 1) - timedelta(days=1)


def to_date(value) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    return parse_date(str(value)[:32]) or parse_month_end(str(value))


def coerce_as_of(value) -> Optional[date]:
    """Accept ISO strings, US dates, month names, date/datetime objects."""
    if value is None or value == "":
        return None
    return to_date(value)


def money_to_float(s: str) -> Optional[float]:
    if not s:
        return None
    m = re.search(r"(\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?", s.replace(" ", ""))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def pct_to_float(s: str) -> Optional[float]:
    if not s:
        return None
    m = PCT_RE.search(s)
    if not m:
        m = re.search(r"(\d+(?:\.\d+)?)", s)
        if not m:
            return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def fmt_pct(val: float) -> str:
    if abs(val - round(val)) < 1e-9:
        return f"{int(round(val))}%"
    txt = f"{val:.4f}".rstrip("0").rstrip(".")
    if "." in txt and len(txt.split(".")[1]) == 1:
        txt = f"{val:.2f}"
    return txt + "%"


def fmt_money(val: float) -> str:
    if abs(val - round(val)) < 1e-9:
        return f"${int(round(val)):,}"
    return f"${val:,.2f}"


def tokenize(text: str) -> list:
    return [t for t in WORD_RE.findall((text or "").lower()) if t not in STOP and len(t) > 1]


def tokenset(text: str) -> set:
    return set(tokenize(text))


def heading_context(lines, idx, lookback=40) -> str:
    heads = []
    for j in range(idx - 1, max(-1, idx - lookback), -1):
        s = lines[j].strip()
        if s.startswith("#"):
            heads.append(re.sub(r"^#+\s*", "", s))
            if s.startswith("# ") or len(heads) >= 3:
                break
        elif re.match(r"^\*\*[A-Z0-9].{2,80}\*\*$", s):
            if re.search(r"effective as of|subject to change", s, re.I):
                continue
            heads.append(s.strip("*"))
    # Many rate-sheet section titles are plain text ("ATV, Snowmobile & Jet Ski Loans")
    # rather than markdown headings.
    # Always take a plain section title immediately above the table. A parent
    # markdown heading that merely says "Rates" or "Loans" should not hide
    # "ATV, Snowmobile & Jet Ski Loans".
    for j in range(idx - 1, max(-1, idx - 4), -1):
        s = lines[j].strip()
        if not s:
            continue
        if s.startswith("#") or s.startswith("|") or s.startswith("*") or s.startswith("["):
            continue
        if 3 <= len(s) <= 90 and not re.search(r"effective as of|subject to change|illustrative", s, re.I):
            heads.append(s)
            break
    heads.reverse()
    return " / ".join(heads)


def footnote_context(after: str) -> str:
    """Keep only this table's trailing footnotes, not the next section."""
    lines = []
    for line in (after or "").split("\n"):
        s = line.strip()
        if not s:
            continue
        if s.startswith("#") or s.startswith("|"):
            break
        if (
            len(s) <= 90
            and not s.startswith("[")
            and not re.search(r"%|effective|illustrative|subject to|apr|apy|payment example", s, re.I)
            and re.match(r"^[A-Z][A-Za-z0-9,&/' \-]+$", s)
        ):
            # A new plain section title (e.g. the next product on /rates).
            break
        lines.append(s)
        if len(lines) >= 6:
            break
    return " ".join(lines)


def is_separator_row(line: str) -> bool:
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    if not cells:
        return False
    return all(re.match(r"^:?-{3,}:?$", c.replace(" ", "")) for c in cells)


def split_row(line: str) -> list:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def parse_tables(md: str) -> list:
    lines = md.replace("\r\n", "\n").replace("\\r\\n", "\n").split("\n")
    tables = []
    i = 0
    n = len(lines)
    while i < n - 1:
        if "|" in lines[i] and is_separator_row(lines[i + 1]):
            headers = split_row(lines[i])
            rows = []
            j = i + 2
            while j < n and "|" in lines[j] and not is_separator_row(lines[j]):
                row = split_row(lines[j])
                if any(cell.strip() for cell in row):
                    rows.append(row)
                j += 1
            after = "\n".join(lines[j:min(n, j + 10)])
            ctx = heading_context(lines, i)
            tables.append({"headers": headers, "rows": rows, "context": ctx, "after": after})
            i = j
            continue
        i += 1
    return tables


def classify_header(h: str) -> str:
    t = re.sub(r"<[^>]+>", "", h or "").lower()
    t = t.replace("annual percentage yield", "apy").replace("annual percentage rate", "apr")
    if "apy" in t or "yield" in t:
        return "apy"
    if "apr" in t:
        return "apr"
    if "dividend" in t:
        return "dividend"
    if t.strip() in {"rate", "interest rate", "interest"} or t.strip().endswith(" rate"):
        return "rate"
    if "term" in t:
        return "term"
    if "tier" in t or "balance" in t:
        return "tier"
    if "program" in t or "account type" in t or t.strip() in {
        "certificate", "account", "loan program", "card type", "product",
    }:
        return "product"
    if "min" in t and "open" in t:
        return "min_open"
    if "min" in t and "earn" in t:
        return "min_earn"
    if "payment" in t:
        return "payment"
    if "point" in t:
        return "points"
    return "other"


SKIP_TABLE_HINTS = (
    "remaining", "vin", "make/ model", "mileage", "principal",
    "customize my payment", "estimated annual mileage",
)


def table_is_noise(headers, context) -> bool:
    blob = " ".join(headers).lower() + " " + (context or "").lower()
    # Word-boundary checks: the old substring "vin" also matched "savings".
    if re.search(
        r"\bvin\b|make/\s*model|customize my payment|estimated annual mileage",
        blob,
    ):
        return True
    if re.search(r"\b(remaining|principal|mileage)\b", blob) and re.search(
        r"\b(vin|payment|loan amount)\b", blob
    ):
        return True
    if re.search(r"\barmenia\b", blob) or re.search(r"\bdial\b", blob):
        return True
    return False


_ONES = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
_SCALES = {"hundred": 100, "thousand": 1000, "million": 1000000}
_NUM_WORDS = set(_ONES) | set(_TENS) | set(_SCALES)
_WRITTEN_ATOM = (
    "zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    "thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|"
    "twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|"
    "hundred|thousand|million"
)
WRITTEN_TERM_RE = re.compile(
    r"\b((?:(?:%s)(?:[\s-]+(?:and[\s-]+)?(?:%s))*))[\s-]*(month|mo|year|yr)s?\b"
    % (_WRITTEN_ATOM, _WRITTEN_ATOM),
    re.I,
)


def parse_written_int(phrase: str) -> Optional[int]:
    if not phrase:
        return None
    toks = [t for t in re.split(r"[-\s]+", phrase.lower()) if t and t != "and"]
    if not toks or any(t not in _NUM_WORDS for t in toks):
        return None
    total = 0
    current = 0
    for tok in toks:
        if tok in _ONES:
            current += _ONES[tok]
        elif tok in _TENS:
            current += _TENS[tok]
        elif tok == "hundred":
            current = (current or 1) * 100
        else:
            current = (current or 1) * _SCALES[tok]
            total += current
            current = 0
    total += current
    if total == 0 and toks != ["zero"]:
        return None
    return int(total)


def consume_amount(text: str):
    """Parse a leading digit or written amount. Returns (value, chars_consumed) or (None, 0)."""
    s0 = text or ""
    s = s0.lstrip()
    skipped = len(s0) - len(s)
    if s.startswith("$"):
        s = s[1:].lstrip()
        skipped = len(s0) - len(s)
    m = re.match(r"(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\b(?:\s+dollars?)?", s)
    if m:
        return float(m.group(1).replace(",", "")), skipped + m.end()
    pos = 0
    words = []
    for m in re.finditer(r"[A-Za-z]+|[^A-Za-z\s]+|\s+", s):
        tok = m.group()
        low = tok.lower()
        if tok.isspace() or tok in {"-", "–", "—"}:
            if words:
                pos = m.end()
            continue
        # "hundred and five" is one amount; "ten thousand and twenty-five thousand" is a range
        if low == "and":
            if words and words[-1] == "hundred":
                pos = m.end()
                continue
            break
        if low in _NUM_WORDS:
            words.append(low)
            pos = m.end()
            continue
        break
    if not words:
        return None, 0
    n = parse_written_int(" ".join(words))
    if n is None:
        return None, 0
    m2 = re.match(r"\s+dollars?\b", s[pos:])
    if m2:
        pos += m2.end()
    return float(n), skipped + pos


def parse_between_amounts(text: str) -> Optional[tuple]:
    m = re.search(r"\bbetween\b", text or "", re.I)
    if not m:
        return None
    rest = text[m.end():]
    lo, n1 = consume_amount(rest)
    if lo is None:
        return None
    rest = rest[n1:].lstrip()
    if not rest.lower().startswith("and"):
        return None
    hi, n2 = consume_amount(rest[3:])
    if hi is None:
        return None
    return lo, hi


def parse_term_months(text: str) -> Optional[int]:
    m = TERM_MONTHS_RE.search(text or "")
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        if unit.startswith("year") or unit.startswith("yr"):
            return n * 12
        return n
    m = WRITTEN_TERM_RE.search(text or "")
    if not m:
        return None
    n = parse_written_int(m.group(1))
    if n is None:
        return None
    unit = m.group(2).lower()
    if unit.startswith("year") or unit.startswith("yr"):
        return n * 12
    return n


def parse_model_years(text: str) -> Optional[tuple]:
    m = MODEL_YR_RE.search(text or "") or MODEL_YR_RE_FLIP.search(text or "")
    if m:
        a = int(m.group(1))
        b = int(m.group(2))
        if b < 100:
            b = 2000 + b
        return (min(a, b), max(a, b))
    m = re.search(r"\b(20\d{2})\s*model\s*year(?:\s+\(and older\))?", text or "", re.I)
    if m:
        y = int(m.group(1))
        if "older" in (text or "").lower():
            return (1900, y)
        return (y, y)
    return None


def parse_amount_cap(text: str) -> Optional[float]:
    m = re.search(r"\$\s*(\d{1,3}(?:,\d{3})+|\d+)\s+or\s+less", text or "", re.I)
    if m:
        return float(m.group(1).replace(",", ""))
    return None


def parse_balance_tier(text: str) -> tuple:
    t = (text or "").replace("–", "-").replace("—", "-")
    if re.search(r"model\s*year", t, re.I):
        return None, None, ""
    m = re.search(
        r"\$?\s*(\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\s*-\s*\$?\s*(\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?",
        t,
    )
    if m:
        lo, hi = float(m.group(1).replace(",", "")), float(m.group(2).replace(",", ""))
        # "2023-26" is a model-year span, not a dollar tier.
        if 1900 <= lo <= 2100 and hi <= 2100:
            return None, None, ""
        return lo, hi, f"${int(lo):,}-${hi:,.2f}".replace(".00", "")
    m = re.search(r"\$?\s*(\d{1,3}(?:,\d{3})+|\d+)\s*\+", t)
    if m:
        lo = float(m.group(1).replace(",", ""))
        return lo, None, f"${int(lo):,}+"
    m = re.search(r"\$?\s*(\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\s+or\s+less", t, re.I)
    if m:
        hi = float(m.group(1).replace(",", ""))
        return None, hi, f"${int(hi):,} or less"
    return None, None, (text or "").strip()




# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

@dataclass
class Fact:
    family: str
    product: str
    metric: str
    value: str
    value_num: Optional[float] = None
    term_months: Optional[int] = None
    tier: str = ""
    tier_lo: Optional[float] = None
    tier_hi: Optional[float] = None
    model_years: Optional[tuple] = None
    amount_cap: Optional[float] = None
    is_jumbo: bool = False
    is_intro: bool = False
    min_earn: Optional[float] = None
    effective_on: Optional[date] = None
    url: str = ""
    title: str = ""
    snapshot_id: str = ""
    taken_on: Optional[date] = None
    authority: float = 0.0
    quote: str = ""
    context: str = ""
    kind: str = "rate"


@dataclass
class Chunk:
    url: str
    title: str
    snapshot_id: str
    taken_on: Optional[date]
    heading: str
    text: str
    tokens: list
    authority: float
    is_news: bool
    is_calc: bool


@dataclass
class BranchHours:
    branch: str
    kind: str
    schedule: str
    url: str
    snapshot_id: str
    taken_on: Optional[date]
    published_on: Optional[date]
    quote: str
    authority: float


@dataclass
class BranchAddr:
    name: str
    address: str
    city: str
    state: str
    zip: str
    url: str
    snapshot_id: str
    taken_on: Optional[date]
    quote: str
    authority: float


def classify_family(product: str, context: str, url: str = "") -> str:
    p = (product or "").lower()
    t = f"{product} {context}".lower()
    u = (url or "").lower()
    # The row/product label wins over a parent-page heading, so a Share
    # Savings row under "Business Certificates" stays share_savings, and a
    # "Purchase: 30-Year" row on a checking page stays a mortgage.
    if re.search(r"share savings|you name it|launchpad", p):
        return "share_savings"
    if "bright savings" in p:
        return "bright_savings"
    if "holiday club" in p:
        return "holiday_club"
    if "high-yield" in p or "high yield" in p:
        return "high_yield"
    if "money market" in p or "investor" in p:
        return "money_market"
    if re.search(r"certificate|term-share|term share|jumbo special", p):
        return "certificate"
    # Recreational rate sheets label rows "Model Year 2026-23"; auto sheets use
    # "2023-26 Model Year". Treat the former as a different product family.
    if re.match(r"model year\s+20\d{2}\s*-\s*\d{2}\b", p):
        return "recreational"
    if re.search(
        r"recreational|\batvs?\b|snowmobile|jet skis?|boat\s*&\s*rv|boat and rv|"
        r"\bboat\s+loans?\b|\brv\s+loans?\b",
        p + " " + t,
    ) or "recreational" in u or re.search(r"/boat-rv|/recreational", u):
        return "recreational"
    if re.search(r"\bvisa\b|credit card|mastercard", p):
        return "credit_card"
    if "heloc" in p or "home equity" in p:
        return "heloc"
    if "home improvement" in p:
        return "home_improvement"
    if any(k in p for k in ("mortgage", "home loan", "conventional", "purchase:", "refinance:")):
        return "mortgage"
    if "personal loan" in p or "personal-loans" in u:
        return "personal_loan"
    if "student" in p:
        return "student_loan"
    if "construction" in p:
        return "construction"
    if ("checking" in p or "1st rate" in p) and not re.search(r"\bapr\b", p):
        return "checking"
    if re.search(r"\bmodel years?\b|\bauto\b|\bautos\b|\bmotorcycle\b|\bcar loan\b", p + " " + t) or (
        re.search(r"vehicle", u) and re.search(r"\bloans?\b", t)
    ):
        if "personal loan" in t:
            return "personal_loan"
        return "auto"
    # Fall back to the broader context (section heading / page title).
    if any(k in t for k in ("certificate", "term-share", "term share", " cd", "cd ")):
        return "certificate"
    if "bright savings" in t:
        return "bright_savings"
    if "share savings" in t or "you name it" in t or "launchpad" in t:
        return "share_savings"
    if "credit card" in t or "visa" in t or "mastercard" in t:
        return "credit_card"
    if "heloc" in t or "home equity" in t:
        return "heloc"
    if any(k in t for k in ("mortgage", "home loan", "conventional", "purchase:", "refinance:")):
        return "mortgage"
    if ("checking" in t or "1st rate" in t) and not re.search(r"\b(month|year)s?\b.*%|\bapr\b", t):
        return "checking"
    if "savings" in t:
        return "savings"
    return "other"


def is_jumbo_name(product: str, min_earn: Optional[float]) -> bool:
    t = (product or "").lower()
    if "jumbo" in t:
        return True
    if min_earn is not None and min_earn >= 100000 and "certificate" in t:
        return True
    return False


class BM25:
    def __init__(self, docs, k1: float = 1.4, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.N = len(docs)
        self.tf = [Counter(d) for d in docs]
        self.dl = [len(d) or 1 for d in docs]
        self.avgdl = (sum(self.dl) / self.N) if self.N else 1.0
        df: Counter = Counter()
        for c in self.tf:
            for t in c:
                df[t] += 1
        self.idf = {t: math.log(1.0 + (self.N - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def scores(self, query, mask=None):
        out = [0.0] * self.N
        k1, b, avgdl = self.k1, self.b, self.avgdl
        qcounts = Counter(query)
        for term, qf in qcounts.items():
            idf = self.idf.get(term)
            if not idf:
                continue
            for i, tf in enumerate(self.tf):
                if mask is not None and not mask[i]:
                    continue
                f = tf.get(term)
                if not f:
                    continue
                denom = f + k1 * (1 - b + b * self.dl[i] / avgdl)
                out[i] += idf * (f * (k1 + 1) / denom) * (1.0 + 0.1 * (qf - 1))
        return out


@dataclass
class ParsedQ:
    raw: str
    lower: str
    tokens: set
    family: Optional[str] = None
    term_months: Optional[int] = None
    is_jumbo: Optional[bool] = None
    model_year: Optional[int] = None
    balance_lo: Optional[float] = None
    balance_hi: Optional[float] = None
    metric: Optional[str] = None
    wants_list: bool = False
    wants_url: bool = False
    wants_hours: bool = False
    hours_kind: Optional[str] = None
    wants_address: bool = False
    wants_count: bool = False
    wants_highest: bool = False
    wants_routing: bool = False
    wants_membership: bool = False
    wants_join: bool = False
    branch_name: Optional[str] = None
    as_of_in_question: Optional[date] = None
    today_in_question: Optional[date] = None
    is_still_open: bool = False
    asked_value: Optional[float] = None
    product_hint: str = ""


FAMILY_CUES = [
    ("bright_savings", ("bright savings",)),
    ("share_savings", ("share savings", "share-savings")),
    ("holiday_club", ("holiday club",)),
    ("high_yield", ("high-yield savings", "high yield savings")),
    ("money_market", ("money market", "investor's choice", "investors choice")),
    ("checking", ("checking account", "checking rates", "1st rate", "free checking", "current checking")),
    ("certificate", (
        "certificate", "certificates", " cd", "cds ", "cd /", "term-share", "term share",
        "jumbo special", "jumbo certificate", "fixed jumbo", "certificate special", "cd special",
    )),
    ("auto", ("auto loan", "auto-loan", "car loan", "vehicle", "motorcycle", "model-year", "model year")),
    ("mortgage", ("mortgage", "home loan", "30-year", "30 year", "15-year", "15 year", "adjustable-rate", "arm")),
    ("personal_loan", ("personal loan",)),
    ("credit_card", ("credit card", "visa", "platinum visa", "rewards visa")),
    ("heloc", ("heloc", "home equity")),
    ("student_loan", ("student loan", "student loans", "university", "college loan", "i'm a student", "i am a student")),
    ("construction", ("construction loan", "construction-loan")),
    ("savings", ("savings tier", "savings account", "savings rates")),
]


def parse_question(question: str, as_of: Optional[str] = None) -> ParsedQ:
    q = question or ""
    ql = q.lower()
    pq = ParsedQ(raw=q, lower=ql, tokens=tokenset(q))
    for fam, cues in FAMILY_CUES:
        if any(c in ql for c in cues):
            pq.family = fam
            break
    if pq.family is None and re.search(r"\bstudents?\b", ql) and re.search(r"\bloans?\b", ql):
        pq.family = "student_loan"
    if pq.family is None and re.search(r"\b(auto|car|vehicle)\b", ql) and re.search(r"\b(apr|rate|loan|financ)", ql):
        pq.family = "auto"
    pq.term_months = parse_term_months(q)
    # "10-month jumbo special" is a certificate even when the word "certificate" is omitted.
    if pq.family is None and pq.term_months and (
        "jumbo" in ql or re.search(r"\b(cd|certificate|special)\b", ql)
    ) and not re.search(r"\b(auto|car|vehicle|mortgage|home loan|personal loan|visa|heloc)\b", ql):
        pq.family = "certificate"
    m = ARM_RE.search(q)
    if m and ("arm" in ql or "adjustable" in ql):
        pq.product_hint = f"{m.group(1)}/{m.group(2)} ARM"
    if "jumbo" in ql:
        pq.is_jumbo = True
    elif "standard" in ql or "non-jumbo" in ql:
        pq.is_jumbo = False
    my = MODEL_YR_Q.search(q)
    if my:
        pq.model_year = int(my.group(1))
    m2 = re.search(r"\b(20\d{2})\s+(vehicle|car|auto|truck|suv|model)\b", ql)
    if m2:
        pq.model_year = int(m2.group(1))
    if re.search(r"\b(new[- ]?(auto|car|vehicle)|brand[- ]?new)\b", ql):
        pq.product_hint = (pq.product_hint + " new-auto").strip()
    between = parse_between_amounts(ql)
    if between:
        pq.balance_lo, pq.balance_hi = between
    elif pq.family in {
        "share_savings", "bright_savings", "money_market", "checking",
        "holiday_club", "high_yield", "savings",
    }:
        ms = re.search(
            r"(?:about|around|approximately|nearly|roughly|some|of|with|have|balance(?: of)?|if i have)\s+",
            ql,
        )
        amt = None
        if ms:
            rest = ql[ms.end():]
            rest = re.sub(
                r"^(?:(?:a|the)\s+)?(?:about|around|approximately|nearly|roughly|some|balance of|balance)\s+",
                "",
                rest,
                count=1,
            )
            amt, _ = consume_amount(rest)
        if amt is None:
            mdollar = re.search(r"\$\s*(\d{1,3}(?:,\d{3})+|\d+)(?!\s*(?:or less|\+))", ql)
            if mdollar:
                amt = float(mdollar.group(1).replace(",", ""))
        if amt is not None:
            pq.balance_lo = pq.balance_hi = amt
    if re.search(r"\bapy\b|annual percentage yield", ql):
        pq.metric = "apy"
    elif "dividend rate" in ql:
        pq.metric = "dividend"
    elif re.search(r"\bapr\b|annual percentage rate", ql):
        pq.metric = "apr"
    elif re.search(r"\brate|rates|interest\b", ql):
        pq.metric = "rate"
    if re.search(r"\b(url|link|website|web page|locator)\b", ql):
        pq.wants_url = True
    if re.search(
        r"\bwhere (can i|do i|to) (find|see|get|look up|check)\b|"
        r"\b(find|see|look up|check)\b.{0,40}\bonline\b|"
        r"\bonline\b.{0,40}\b(find|see|look up|check)\b",
        ql,
    ) and re.search(r"\b(rates?|apy|apr|join|locator|hours|address|page)\b", ql):
        pq.wants_url = True
    if re.search(r"\bhours\b|what time|lobby|drive-?up", ql) and "still open" not in ql:
        pq.wants_hours = True
        if "drive" in ql:
            pq.hours_kind = "drive-up"
        elif "lobby" in ql:
            pq.hours_kind = "lobby"
    if re.search(r"\baddress\b|where is |located\b", ql):
        pq.wants_address = True
    if re.search(r"how many.{0,40}branch|number of branches|how many locations|branch offices", ql):
        pq.wants_count = True
    if re.search(r"\bhighest\b|\bmost\b.*apy|top .*apy", ql):
        pq.wants_highest = True
    if "routing" in ql:
        pq.wants_routing = True
    if re.search(r"eligible to become|who can join|who is eligible|membership|charter area", ql):
        pq.wants_membership = True
    if re.search(r"how do i join|join .*(online|dupaco)|become a member", ql):
        pq.wants_join = True
    if re.search(
        r"\b(cards? do you offer|what credit cards|rates for the credit cards|"
        r"certificate of accounts|certificate rates|all certificate|"
        r"mortgage rates|checking account interest rates)\b",
        ql,
    ):
        pq.wants_list = True
    if re.search(r"do all .*branches.*same|same lobby hours", ql):
        pq.wants_hours = True
        pq.wants_list = True
        pq.hours_kind = "lobby"
    mb = re.search(
        r"\b(?:the\s+)?([A-Z][A-Za-z0-9$.'\-]+(?:\s+[A-Z][A-Za-z0-9$.'\-]+){0,3})\s+branch\b",
        q,
    )
    if mb:
        pq.branch_name = mb.group(1).strip()
    else:
        mb = re.search(r"\b(hy-?vee|galena|mullan(?:\s+avenue)?|sycamore|key west|asbury)\b", ql)
        if mb:
            pq.branch_name = mb.group(1)
    if re.search(r"still open|when does .* close|will .* close|closing date", ql):
        pq.is_still_open = "still open" in ql or bool(re.search(r"is the .+ still", ql))
    if "as of" in ql:
        tail = q[ql.index("as of") + 5:]
        pq.as_of_in_question = parse_month_end(tail) or parse_date(tail)
    mt = re.search(r"today\s*\(([^)]+)\)", q, re.I)
    if mt:
        pq.today_in_question = parse_month_end(mt.group(1)) or parse_date(mt.group(1))
    if not pq.today_in_question:
        mt = re.search(r"\(([^)]*20\d{2}[^)]*)\)", q)
        if mt:
            pq.today_in_question = parse_month_end(mt.group(1)) or parse_date(mt.group(1))
    mp = PCT_RE.search(q)
    if mp:
        pq.asked_value = float(mp.group(1))
    if as_of and not pq.as_of_in_question:
        pq.as_of_in_question = to_date(as_of)
    if pq.family == "certificate" and pq.term_months is None and re.search(r"\brates\b|certificate of accounts", ql):
        pq.wants_list = True
    if pq.family == "credit_card" and re.search(r"cards|rates", ql):
        pq.wants_list = True
    if pq.family == "mortgage" and "rates" in ql and "30-year" not in ql and "30 year" not in ql and "15-year" not in ql:
        pq.wants_list = True
    return pq



class KnowledgeSystem:
    def __init__(self):
        self.pages = []
        self.facts = []
        self.chunks = []
        self.hours = []
        self.addresses = []
        self.events = []
        self.counts = []
        self.awards = []
        self.urls_by_topic = []
        self.snapshots = []
        self.latest_on = None
        self._bm25 = None
        self._cache = {}
        self._conflicts_cache = None
        self._url_dates = None
        self._ingested_ids = set()

    def ingest(self, snapshot: Snapshot) -> None:
        sid = getattr(snapshot, "snapshot_id", None) or ""
        if sid and sid in self._ingested_ids:
            return
        taken = to_date(snapshot.taken_on)
        self.snapshots.append((sid, taken))
        if sid:
            self._ingested_ids.add(sid)
        if taken and (self.latest_on is None or taken > self.latest_on):
            self.latest_on = taken
        self._bm25 = None
        self._cache.clear()
        self._conflicts_cache = None
        self._url_dates = None
        for p in snapshot.pages:
            url = p.url or ""
            title = p.title or ""
            md = (p.markdown or "").replace("\xa0", " ").replace("\u202f", " ")
            if not md.strip():
                continue
            rec = {
                "url": url, "title": title, "markdown": md,
                "snapshot_id": sid, "taken_on": taken,
                "fetched_at": getattr(p, "fetched_at", None),
                "authority": page_authority(url, title),
            }
            self.pages.append(rec)
            try:
                self._extract_from_page(rec)
            except Exception:
                continue

    def _extract_from_page(self, rec):
        md, url, title = rec["markdown"], rec["url"], rec["title"]
        auth, sid, taken = rec["authority"], rec["snapshot_id"], rec["taken_on"]
        path = _norm_path(url)
        for tbl in parse_tables(md):
            if not table_is_noise(tbl["headers"], tbl["context"]):
                self._facts_from_table(tbl, rec)
        self._facts_from_prose(rec)
        self._extract_hours(rec)
        self._extract_addresses(rec)
        self._extract_contact_facts(rec)
        for m in re.finditer(r"(\d+)\s+(?:full-service\s+)?branch office locations", md, re.I):
            pub = self._publication_date(md, title)
            self.counts.append({
                "value": int(m.group(1)), "url": url, "snapshot_id": sid, "taken_on": taken,
                "published_on": pub, "authority": auth,
                "quote": md[max(0, m.start() - 40):m.end() + 10].strip(),
                "in_about_blurb": "about dupaco" in md[max(0, m.start() - 400):m.start()].lower(),
            })
        for m in re.finditer(r"(\d+)\s+full-service branches", md, re.I):
            pub = self._publication_date(md, title)
            self.counts.append({
                "value": int(m.group(1)), "url": url, "snapshot_id": sid, "taken_on": taken,
                "published_on": pub, "authority": auth,
                "quote": md[max(0, m.start() - 40):m.end() + 10].strip(),
                "in_about_blurb": "about dupaco" in md[max(0, m.start() - 400):m.start()].lower(),
            })
        self._extract_events(rec)
        self._extract_awards(rec)
        if path in {
            "/personal/savings-account", "/contact", "/about/join",
            "/personal/loans/auto-motorcycle-loans", "/personal/loans/home-loans",
            "/personal/credit-cards", "/rates",
        } or "join.dupaco.com" in url:
            self.urls_by_topic.append({
                "url": url, "path": path, "title": title, "authority": auth,
                "snapshot_id": sid, "taken_on": taken,
            })
        if auth > -3.5 and "/hubfs/" not in path and not path.endswith((".pdf", ".xlsx")):
            self._chunk_page(rec)

    def _publication_date(self, md, title):
        for line in md.splitlines()[:20]:
            d = parse_date(line.strip())
            if d and re.match(
                r"^(January|February|March|April|May|June|July|August|"
                r"September|October|November|December|Jan|Feb|Mar|Apr|"
                r"Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\.?\s+\d{1,2},?\s+20\d{2}$",
                line.strip(), re.I,
            ):
                return d
        return parse_date("\n".join(md.splitlines()[:25]))

    def _effective_near(self, text):
        m = EFFECTIVE_RE.search(text or "")
        return parse_date(m.group(1)) if m else None

    def _facts_from_table(self, tbl, rec):
        headers = tbl["headers"]
        kinds = [classify_header(h) for h in headers]
        if "apy" not in kinds and "apr" not in kinds and "rate" not in kinds and "dividend" not in kinds:
            return
        ctx = tbl["context"]
        effective = self._effective_near(tbl["after"]) or self._effective_near(ctx)
        for row in tbl["rows"]:
            cells = row + [""] * max(0, len(headers) - len(row))
            product = ""
            term_txt = ""
            tier_txt = ""
            values = {}
            min_earn = None
            for kind, cell, header in zip(kinds, cells, headers):
                if kind == "product" and not product:
                    product = re.sub(r"<[^>]+>", "", cell).strip()
                elif kind == "term":
                    term_txt = cell
                elif kind == "tier":
                    tier_txt = cell
                elif kind in {"apy", "apr", "rate", "dividend"}:
                    values[kind] = cell
                elif kind == "min_earn":
                    min_earn = money_to_float(cell)
            # Some pages (calculators, promos) reuse a savings-table header
            # template ("Balance Tier", "APY") on auto-rate rows like
            # "2023-26 Model Year | 60 Months | 5.34% APR".
            for kind, cell in zip(kinds, cells):
                c = (cell or "").replace("\xa0", " ").replace("–", "-").strip()
                if not c:
                    continue
                if (
                    re.search(r"\b(month|mo|year|yr)s?\b", c, re.I)
                    and parse_term_months(c)
                    and "$" not in c
                ):
                    term_txt = term_txt or c
                    if kind == "tier" and (tier_txt or "").strip() == (cell or "").strip():
                        tier_txt = ""
                m_unit = re.search(r"(\d+(?:\.\d+)?)\s*%\s*(APR|APY)\b", c, re.I)
                if m_unit:
                    unit = m_unit.group(2).lower()
                    values[unit] = c
                    if kind in {"apy", "apr", "rate", "dividend"} and kind != unit:
                        values.pop(kind, None)
            if not product and cells and kinds[0] in {"other", "product"}:
                product = cells[0].strip()
            # Some rate sheets label the first column "Term" but put the full
            # product name there (e.g. "10 Month Fixed Jumbo Certificate Special").
            if cells and kinds and kinds[0] == "term":
                cell0 = cells[0].strip()
                if (not product or re.search(r"effective as of|subject to change", product, re.I)) and (
                    len(cell0.split()) >= 3
                    or re.search(r"certificate|special|jumbo|savings|visa|checking", cell0, re.I)
                ):
                    product = cell0
                    if not term_txt:
                        term_txt = cell0
            if not product or re.search(r"effective as of|subject to change", product or "", re.I):
                product = ctx.split("/")[-1].strip() if ctx else "unknown"
                if re.search(r"effective as of|subject to change", product, re.I):
                    product = (term_txt or cells[0] if cells else "unknown")
            after = footnote_context(tbl.get("after") or "")
            blob = " ".join([product, term_txt, tier_txt, ctx, rec["title"], after])
            family = classify_family(product, ctx + " " + rec["title"] + " " + after, rec["url"])
            term = parse_term_months(term_txt or product or ctx)
            years = parse_model_years(blob)
            cap = parse_amount_cap(product + " " + tier_txt)
            lo, hi, tier_label = parse_balance_tier(tier_txt or product)
            if cap and not hi:
                hi = cap
                if not tier_label:
                    tier_label = f"${int(cap):,} or less"
            jumbo = is_jumbo_name(product, min_earn)
            quote = " | ".join(c for c in cells if c.strip())[:280]
            for metric, raw in values.items():
                num = pct_to_float(raw)
                if num is None:
                    continue
                amt_cap = cap
                if "or less" in (product + " " + (tier_txt or "")).lower():
                    amt_cap = cap or hi
                self.facts.append(Fact(
                    family=family, product=re.sub(r"\s+", " ", product),
                    metric=metric, value=fmt_pct(num), value_num=num,
                    term_months=term, tier=tier_label or ("jumbo" if jumbo else ""),
                    tier_lo=lo, tier_hi=hi, model_years=years, amount_cap=amt_cap,
                    is_jumbo=jumbo, is_intro="intro" in (product + ctx).lower(),
                    min_earn=min_earn, effective_on=effective, url=rec["url"],
                    title=rec["title"], snapshot_id=rec["snapshot_id"],
                    taken_on=rec["taken_on"], authority=rec["authority"],
                    quote=quote, context=(ctx + " " + after).strip(), kind="rate",
                ))

    def _facts_from_prose(self, rec):
        md, url = rec["markdown"], rec["url"]
        path = _norm_path(url)
        self._extract_prose_rates_and_fees(rec)
        if "credit-cards" in path or "credit card" in (rec["title"] or "").lower():
            self._extract_card_rates(rec)
        if re.search(r"home\s*\$?tart|down payment and closing|first-time homebuyer", md, re.I):
            for m in re.finditer(
                r"up to\s+\$([0-9,]+)\s+(?:toward|in assistance|for|in)\s+(?:down payment|assistance)?",
                md, re.I,
            ):
                amt = money_to_float(m.group(1))
                if amt and 1000 <= amt <= 50000:
                    q = md[max(0, m.start() - 40):m.end() + 40].replace("\n", " ")
                    self.facts.append(Fact(
                        family="grant", product="Home$tart", metric="amount",
                        value=fmt_money(amt), value_num=amt, url=url, title=rec["title"],
                        snapshot_id=rec["snapshot_id"], taken_on=rec["taken_on"],
                        authority=rec["authority"] + (2 if "homebuyer" in path or "hometart" in url.lower() else 0),
                        quote=q[:280], kind="text",
                    ))

    def _extract_prose_rates_and_fees(self, rec):
        """Pull definitional 'Product APY: 3.50%' / 'fee: $2.50' lines (tables already cover most of Dupaco)."""
        md = rec["markdown"]
        skip_line = re.compile(
            r"if you borrow|illustrative|payment example|as low as quoted|customize my payment",
            re.I,
        )
        rate_line = re.compile(
            r"^(?P<label>.{0,100}?)(?P<metric>\bAPY|\bAPR|dividend rate)\s*[:=]?\s*"
            r"(?P<val>\d+(?:\.\d+)?)\s*%",
            re.I,
        )
        fee_line = re.compile(
            r"^(?P<label>.{0,100}?fee)\s*[:=]\s*\$(?P<val>\d{1,3}(?:,\d{3})*(?:\.\d+)?)",
            re.I,
        )
        ended_re = re.compile(
            r"(?:offer ended|ended|expires?|expired|through|until)\s+"
            r"(20\d{2}-\d{2}-\d{2}|(?:January|February|March|April|May|June|July|"
            r"August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|"
            r"Aug|Sep|Sept|Oct|Nov|Dec)\.?\s+\d{1,2},?\s+20\d{2})",
            re.I,
        )
        for raw in md.splitlines():
            line = raw.strip().lstrip("-*# ").strip()
            if not line or len(line) > 180 or skip_line.search(line):
                continue
            ended = None
            em = ended_re.search(line)
            if em:
                ended = parse_date(em.group(1))
            m = rate_line.search(line)
            if m:
                label = m.group("label").strip(" :-")
                metric = m.group("metric").lower()
                if metric == "dividend rate":
                    metric = "dividend"
                else:
                    metric = metric.lower()
                num = float(m.group("val"))
                family = classify_family(label, rec["title"], rec["url"])
                term = parse_term_months(label) or parse_term_months(line)
                jumbo = is_jumbo_name(label, None)
                auth = rec["authority"]
                if "promo" in rec["url"].lower() or ended:
                    auth -= 3.0
                self.facts.append(Fact(
                    family=family if family != "other" else "certificate" if "certificate" in line.lower() else family,
                    product=re.sub(r"\s+", " ", label) or rec["title"],
                    metric=metric, value=fmt_pct(num), value_num=num,
                    term_months=term, is_jumbo=jumbo,
                    effective_on=ended,  # if ended, this is an end date; handled in _facts_as_of as expired if we tag it
                    url=rec["url"], title=rec["title"], snapshot_id=rec["snapshot_id"],
                    taken_on=rec["taken_on"], authority=auth,
                    quote=line[:280], context="expired" if ended else "prose",
                    kind="rate",
                ))
                # Store expiry on the fact via context; also skip later if ended before cutoff
                if ended:
                    self.facts[-1].effective_on = None
                    # attach expiry as a custom attribute
                    self.facts[-1].context = f"expired:{ended.isoformat()}"
                continue
            m = fee_line.search(line)
            if m:
                label = m.group("label").strip(" :-")
                amt = money_to_float(m.group("val"))
                if amt is None:
                    continue
                self.facts.append(Fact(
                    family="fee", product=label, metric="amount",
                    value=fmt_money(amt), value_num=amt,
                    url=rec["url"], title=rec["title"], snapshot_id=rec["snapshot_id"],
                    taken_on=rec["taken_on"], authority=rec["authority"] + 2,
                    quote=line[:280], kind="text",
                ))

    def _extract_card_rates(self, rec):
        text = rec["markdown"]
        low = text.lower()
        patterns = [
            (r"platinum visa(?!.*rewards)", "Platinum Visa"),
            (r"rewards visa", "Rewards Visa"),
            (r"business platinum", "Business Platinum Visa"),
            (r"business rewards", "Business Rewards Visa"),
        ]
        for pat, name in patterns:
            for m in re.finditer(pat, low):
                # Limit the window to this card's section so a later product's
                # APR is not attributed to this one.
                end = min(len(text), m.start() + 500)
                nxt = re.search(r"\n###\s+", text[m.start() + 10:end])
                if nxt:
                    end = m.start() + 10 + nxt.start()
                for other_pat, other_name in patterns:
                    if other_name == name:
                        continue
                    om = re.search(other_pat, low[m.start() + len(name):end])
                    if om:
                        end = min(end, m.start() + len(name) + om.start())
                window = text[m.start():end]
                rng = re.search(
                    r"(\d+(?:\.\d+)?)\s*%\s*[–\-]\s*(\d+(?:\.\d+)?)\s*%\s*APR",
                    window, re.I,
                )
                single = re.search(
                    r"(?:APR|rate)\s+(?:of\s+)?(\d+(?:\.\d+)?)\s*%|"
                    r"(\d+(?:\.\d+)?)\s*%\s*(?:variable\s+)?APR|"
                    r"variable annual percentage rate\s*\(APR\)\s*of\s*(\d+(?:\.\d+)?)\s*%",
                    window, re.I,
                )
                lo = hi = None
                if rng:
                    lo, hi = float(rng.group(1)), float(rng.group(2))
                elif single:
                    val = next(g for g in single.groups() if g)
                    lo = hi = float(val)
                if lo is None:
                    continue
                quote = re.sub(r"\s+", " ", window[:240])
                self.facts.append(Fact(
                    family="credit_card", product=name, metric="apr",
                    value=fmt_pct(lo) if lo == hi else f"{fmt_pct(lo)}-{fmt_pct(hi)}",
                    value_num=lo, tier="standard", url=rec["url"], title=rec["title"],
                    snapshot_id=rec["snapshot_id"], taken_on=rec["taken_on"],
                    authority=rec["authority"], quote=quote, kind="rate",
                ))
                if hi is not None and hi != lo:
                    self.facts.append(Fact(
                        family="credit_card", product=name, metric="apr_high",
                        value=fmt_pct(hi), value_num=hi, tier="high", url=rec["url"],
                        title=rec["title"], snapshot_id=rec["snapshot_id"],
                        taken_on=rec["taken_on"], authority=rec["authority"],
                        quote=quote, kind="rate",
                    ))
                break

    def _extract_hours(self, rec):
        md = rec["markdown"]
        pattern = re.compile(
            r"\*\*(Lobby|Drive-?up|Drive up)\*\*\s*(.+?)(?=\*\*(?:Lobby|Drive-?up|ABOUT)|$)",
            re.I | re.S,
        )
        pub = self._publication_date(md, rec["title"])
        branch = self._branch_from_title(rec["title"], md)
        for m in pattern.finditer(md):
            kind = "drive-up" if "drive" in m.group(1).lower() else "lobby"
            lines = []
            for line in m.group(2).splitlines():
                if re.search(r"\d", line) and re.search(
                    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
                    r"mon|tue|wed|thu|fri|sat|sun|closed|atm|a\.?m\.?|p\.?m\.?",
                    line, re.I,
                ):
                    lines.append(line.strip())
            if not lines:
                continue
            schedule = "; ".join(lines)
            self.hours.append(BranchHours(
                branch=branch or "", kind=kind, schedule=schedule, url=rec["url"],
                snapshot_id=rec["snapshot_id"], taken_on=rec["taken_on"],
                published_on=pub, quote=schedule[:280],
                authority=rec["authority"] + (2.0 if "/newsroom/" in rec["url"] else 0),
            ))

    def _branch_from_title(self, title, md):
        t = title or ""
        m = re.search(r"([A-Z][A-Za-z0-9 .'\-]+?)\s+branch", t, re.I)
        if m:
            return m.group(1).strip()
        m = re.search(r"phone number for the ([^.]+?) (?:branch|Learning Lab) is", md, re.I)
        if m:
            return m.group(1).strip()
        return ""

    def _extract_addresses(self, rec):
        md = rec["markdown"]
        path = _norm_path(rec["url"])
        addr_re = re.compile(
            r"(\d{3,5}\s+[A-Z0-9][A-Za-z0-9.' \-]+?"
            r"(?:St|Street|Ave|Avenue|Dr|Drive|Ln|Lane|Rd|Road|Blvd|Boulevard|Hwy|Highway|Way)\.?)"
            r",\s*([A-Za-z .'\-]+),\s*"
            r"(IA|IL|WI|Iowa|Illinois|Wisconsin),?\s*(\d{5})\b"
        )
        for m in addr_re.finditer(md):
            pre = md[max(0, m.start() - 200):m.start()]
            # Only skip ATMs based on the nearest heading, not a previous listing
            # that happened to mention a "24-hour ATM".
            nearest = ""
            hm = list(re.finditer(r"###\s*([^\n]+)", pre))
            if hm:
                nearest = hm[-1].group(1)
            if re.search(r"\bATM\b", nearest, re.I) and not re.search(r"\bBranch\b", nearest, re.I):
                continue
            if re.search(r"chrysler|dealer|ford|chevrolet|honda|auto group", nearest + " " + pre[-120:], re.I) and "dupaco" not in nearest.lower():
                continue
            name = ""
            nm = re.search(r"(?:###\s*)?([^\n]{0,80})\n(?:Dupaco Branch|Branch)", pre)
            if nm:
                name = nm.group(1).strip("# ").strip()
            if "dupaco branch" in pre.lower()[-80:] and not name:
                name = "Dupaco Branch"
            city, st, z = m.group(2).strip(), m.group(3), m.group(4)
            street = m.group(1).strip()
            self.addresses.append(BranchAddr(
                name=name or city,
                address=f"{street}, {city}, {st} {z}",
                city=city, state=st, zip=z, url=rec["url"],
                snapshot_id=rec["snapshot_id"], taken_on=rec["taken_on"],
                quote=m.group(0),
                authority=rec["authority"] + (3 if path in {"/contact", "/"} else 0),
            ))

    def _extract_contact_facts(self, rec):
        md, url = rec["markdown"], rec["url"]
        auth = rec["authority"]
        for m in ROUTING_RE.finditer(md):
            num = m.group(2)
            self.facts.append(Fact(
                family="contact", product="routing number", metric="routing",
                value=num, value_num=float(num), url=url, title=rec["title"],
                snapshot_id=rec["snapshot_id"], taken_on=rec["taken_on"],
                authority=auth + 2, quote=m.group(0), kind="contact",
            ))
        if _norm_path(url) in {"/contact", "/business/cash-management", "/checking", "/personal/checking-accounts"}:
            for m in ROUTING_ANY.finditer(md):
                num = m.group(1)
                if any(f.value == num and f.metric == "routing" and f.url == url and f.snapshot_id == rec["snapshot_id"]
                       for f in self.facts[-8:]):
                    continue
                self.facts.append(Fact(
                    family="contact", product="routing number", metric="routing",
                    value=num, value_num=float(num), url=url, title=rec["title"],
                    snapshot_id=rec["snapshot_id"], taken_on=rec["taken_on"],
                    authority=auth + 1, quote=num, kind="contact",
                ))
        for m in PHONE_RE.finditer(md):
            phone = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
            extra = 2 if phone == "800-373-7600" else 0
            self.facts.append(Fact(
                family="contact", product="phone", metric="phone", value=phone,
                url=url, title=rec["title"], snapshot_id=rec["snapshot_id"],
                taken_on=rec["taken_on"], authority=auth + extra, quote=phone, kind="contact",
            ))

    def _extract_events(self, rec):
        md, url, title = rec["markdown"], rec["url"], rec["title"]
        pub = self._publication_date(md, title)
        pat = re.compile(
            r"(?:will close|scheduled to close|this location will close|"
            r"last day of service (?:will be|is)|closing date(?: is)?|"
            r"plans to close)\s*"
            r"(?:on\s+|is\s+|be\s+)?(?:[A-Za-z]+,\s+)?"
            r"((?:January|February|March|April|May|June|July|August|"
            r"September|October|November|December|Jan|Feb|Mar|Apr|Jun|"
            r"Jul|Aug|Sep|Sept|Oct|Nov|Dec)\.?\s+\d{1,2},?\s+20\d{2}|"
            r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+"
            r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+20\d{2})",
            re.I,
        )
        for m in pat.finditer(md):
            d = parse_date(m.group(1))
            if not d:
                continue
            window = md[max(0, m.start() - 250):m.end() + 80]
            branch = self._branch_from_title(title, window) or self._branch_from_title(title, md)
            if not branch:
                bm = re.search(r"([A-Z][A-Za-z0-9 .'\-]+?)\s+branch", window, re.I)
                if bm:
                    branch = bm.group(1).strip()
            self.events.append({
                "type": "close", "branch": branch or "", "date": d, "url": url,
                "snapshot_id": rec["snapshot_id"], "taken_on": rec["taken_on"],
                "published_on": pub, "quote": re.sub(r"\s+", " ", window)[:280],
                "authority": rec["authority"] + 3, "title": title,
            })
        if re.search(r"hy-?vee", title + md[:1500], re.I):
            m = re.search(r"(will close|close)\s+([A-Z][a-z]+\s+\d{1,2},\s+20\d{2})", md)
            if m:
                d = parse_date(m.group(2))
                if d and not any(
                    (e.get("branch") or "").lower().startswith("hy") and e["date"] == d
                    for e in self.events[-5:]
                ):
                    self.events.append({
                        "type": "close", "branch": "Hy-Vee", "date": d, "url": url,
                        "snapshot_id": rec["snapshot_id"], "taken_on": rec["taken_on"],
                        "published_on": pub, "quote": m.group(0),
                        "authority": rec["authority"] + 3, "title": title,
                    })

    def _extract_awards(self, rec):
        md, title = rec["markdown"], rec["title"]
        if "forbes" not in (title + md[:2000]).lower():
            return
        pub = self._publication_date(md, title)
        for m in re.finditer(
            r"(?:the\s+)?(first|second|third|fourth|fifth|sixth|seventh|"
            r"1st|2nd|3rd|4th|5th|6th|7th|\d+(?:st|nd|rd|th))\s+time\s+Forbes",
            md, re.I,
        ):
            word = m.group(1).lower()
            words = {
                "first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3,
                "fourth": 4, "4th": 4, "fifth": 5, "5th": 5, "sixth": 6, "6th": 6,
                "seventh": 7, "7th": 7,
            }
            n = words.get(word) or int(re.sub(r"\D", "", word) or 0)
            if not n:
                continue
            window = (md[max(0, m.start() - 120):m.end() + 160] + " " + (title or "")).lower()
            program = (
                "small employer" if ("small" in window and "employer" in window) else
                "credit union"
            )
            self.awards.append({
                "org": "Forbes", "n": n, "word": word if word.isalpha() else None,
                "program": program,
                "url": rec["url"], "snapshot_id": rec["snapshot_id"],
                "taken_on": rec["taken_on"], "published_on": pub,
                "authority": rec["authority"] + 2,
                "quote": re.sub(r"\s+", " ", md[max(0, m.start() - 30):m.end() + 40])[:280],
                "title": title,
            })

    def _chunk_page(self, rec):
        md = rec["markdown"]
        text = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", md)
        text = re.sub(r"\n{3,}", "\n\n", text)
        parts = re.split(r"\n(?=#{1,3}\s+)", text)
        path = _norm_path(rec["url"])
        is_news = "/newsroom/" in path or "/learn/" in path
        is_calc = "calculator" in path or "calculator" in (rec["title"] or "").lower()
        heading = rec["title"]
        buf = []

        def flush(h, body):
            body = re.sub(r"\s+", " ", body).strip()
            if len(body) < 80:
                return
            if len(body) > 1400:
                sents = re.split(r"(?<=[.!?])\s+", body)
                cur = ""
                for s in sents:
                    if len(cur) + len(s) > 1100 and cur:
                        self._add_chunk(rec, h, cur, is_news, is_calc)
                        cur = s
                    else:
                        cur = (cur + " " + s).strip()
                if cur:
                    self._add_chunk(rec, h, cur, is_news, is_calc)
            else:
                self._add_chunk(rec, h, body, is_news, is_calc)

        for part in parts:
            lines = part.splitlines()
            if lines and lines[0].startswith("#"):
                if buf:
                    flush(heading, "\n".join(buf))
                    buf = []
                heading = re.sub(r"^#+\s*", "", lines[0]).strip()
                buf = lines[1:]
            else:
                buf.extend(lines)
        if buf:
            flush(heading, "\n".join(buf))
        # Short official pages (rate sheets, fee lists) must still be searchable.
        compact = re.sub(r"\s+", " ", text).strip()
        if 20 <= len(compact) <= 1200:
            self._add_chunk(rec, rec["title"], compact, is_news, is_calc, min_tokens=3)

    def _add_chunk(self, rec, heading, body, is_news, is_calc, min_tokens=8):
        boost = f"{rec['title']} {heading} {_norm_path(rec['url']).replace('/', ' ')}"
        toks = tokenize(boost + " " + body)
        if len(toks) < min_tokens:
            return
        self.chunks.append(Chunk(
            url=rec["url"], title=rec["title"], snapshot_id=rec["snapshot_id"],
            taken_on=rec["taken_on"], heading=heading, text=body, tokens=toks,
            authority=rec["authority"], is_news=is_news, is_calc=is_calc,
        ))

    def _ensure_index(self):
        if self._bm25 is None and self.chunks:
            self._bm25 = BM25([c.tokens for c in self.chunks])



    def _cutoff(self, as_of, pq=None):
        d = coerce_as_of(as_of) if as_of not in (None, "") else None
        if pq and pq.as_of_in_question and d is None:
            d = pq.as_of_in_question
        return d

    def _earliest_taken(self):
        dates = [d for _, d in self.snapshots if d]
        return min(dates) if dates else None

    def _ensure_url_dates(self):
        if self._url_dates is not None:
            return
        by = defaultdict(set)
        for p in self.pages:
            if p.get("taken_on"):
                by[p["url"]].add(p["taken_on"])
        self._url_dates = {u: sorted(ds) for u, ds in by.items()}

    def _last_seen(self, url, cutoff):
        self._ensure_url_dates()
        dates = self._url_dates.get(url) or []
        if cutoff:
            dates = [d for d in dates if d <= cutoff]
        return dates[-1] if dates else None

    def _visible(self, taken_on, cutoff, url=None):
        """A page version is visible if it is the latest copy of that URL
        on or before as_of (or the latest copy overall when as_of is omitted).
        Pages that were never recrawled later remain visible from their last snapshot.
        """
        if cutoff is not None and taken_on and taken_on > cutoff:
            return False
        if url:
            last = self._last_seen(url, cutoff)
            if last and taken_on:
                return taken_on == last
        if cutoff is None:
            if self.latest_on and taken_on:
                return taken_on == self.latest_on
            return True
        return taken_on is None or taken_on <= cutoff

    def _latest_taken(self, cutoff):
        dates = [d for _, d in self.snapshots if d and (cutoff is None or d <= cutoff)]
        return max(dates) if dates else self.latest_on

    def _rank_fact(self, f, cutoff):
        latest = self._latest_taken(cutoff)
        recency = 0.0
        if f.taken_on and latest:
            recency = 1.0 if f.taken_on == latest else max(0.0, 1.0 - (latest - f.taken_on).days / 180.0)
        eff = 0.0
        if f.effective_on and latest:
            if cutoff and f.effective_on > cutoff:
                eff = -2.0
            else:
                ref = cutoff or latest
                if ref and f.effective_on <= ref:
                    eff = 1.0 - min(1.0, (ref - f.effective_on).days / 365.0) * 0.5
        intro_pen = -1.5 if f.is_intro else 0.0
        return f.authority + 4.0 * recency + 3.0 * eff + intro_pen

    def _facts_as_of(self, cutoff, family=None):
        out = []
        savings = {"share_savings", "bright_savings", "holiday_club", "high_yield", "money_market", "savings"}
        for f in self.facts:
            if not self._visible(f.taken_on, cutoff, f.url):
                continue
            if family and f.family != family and not (family == "savings" and f.family in savings):
                continue
            if cutoff and f.effective_on and f.effective_on > cutoff:
                continue
            if isinstance(f.context, str) and f.context.startswith("expired:"):
                end = to_date(f.context.split(":", 1)[1])
                # An ended offer is only visible if the cutoff is on or before the end date.
                ref = cutoff or self.latest_on
                if end and ref and ref > end:
                    continue
            out.append(f)
        return out

    def _best_by_key(self, facts, cutoff):
        groups = defaultdict(list)
        for f in facts:
            key = (
                f.family, f.product.lower(), f.metric, f.term_months,
                f.tier.lower(), f.is_jumbo, f.model_years, f.amount_cap,
                round(f.tier_lo or -1, 2), round(f.tier_hi or -1, 2),
            )
            groups[key].append(f)
        winners = []
        for items in groups.values():
            items = sorted(items, key=lambda x: self._rank_fact(x, cutoff), reverse=True)
            winners.append(items[0])
        return winners

    def answer(self, question, as_of=None):
        try:
            q = question if isinstance(question, str) else ("" if question is None else str(question))
            q = q.replace("\xa0", " ").replace("–", "-").replace("—", "-")
            key = (q, str(coerce_as_of(as_of) or as_of or ""))
            if key in self._cache:
                return self._cache[key]
            if not q.strip():
                ans = self._refuse("I don't know. I need a question to answer; please contact us.")
            else:
                ans = self._answer_uncached(q, as_of)
            if ans is None or not hasattr(ans, "text"):
                ans = self._refuse("I don't know. I couldn't complete that lookup; please contact us.")
            self._cache[key] = ans
            return ans
        except Exception:
            return self._refuse("I don't know. I couldn't complete that lookup; please contact us.")

    def _refuse(self, text, sources=None):
        return Answer(
            text=text or "I don't know. This isn't listed on the website; please contact us.",
            sources=sources or [],
            confident=False,
        )

    def _src(self, url, sid=None, quote=None):
        return Source(url=url, snapshot_id=sid, quote=(quote or "")[:240] or None)

    def _answer_uncached(self, question, as_of=None):
        if not self.snapshots:
            return self._refuse(
                "I don't know. I haven't loaded the website snapshots yet; please contact us."
            )
        self._ensure_index()
        pq = parse_question(question or "", as_of)
        cutoff = self._cutoff(as_of, pq)
        earliest = self._earliest_taken()
        if cutoff and earliest and cutoff < earliest:
            return self._refuse(
                "I don't know. The available website snapshots start on "
                + earliest.isoformat()
                + ", so I can't confirm what was published as of "
                + cutoff.isoformat()
                + ". Please contact us."
            )
        q = pq.lower
        # Out-of-domain / other-institution questions should be refused, not
        # answered from Dupaco pages that happen to share a keyword.
        if re.search(
            r"\b(chase(?:\s+bank)?|bank of america|\bbofa\b|wells fargo|citi(?:bank)?|"
            r"capital one|u\.?s\.?\s*bank|\bpnc\b|td bank|navy federal|ally bank|"
            r"\bsofi\b|goldman sachs|discover bank|barclays|\bhsbc\b|santander|"
            r"\btruist\b|fifth third|regions bank|huntington bank|citizens bank|"
            r"keybank|key bank)\b",
            q,
        ) and "dupaco" not in q:
            return self._refuse(
                "I don't know. I only have information from the Dupaco website, "
                "not from other institutions; please contact us."
            )
        if re.search(
            r"\b(weather|forecast|stock price|sports score|who won the|"
            r"movie times|recipe for|bitcoin price|lottery numbers)\b",
            q,
        ):
            return self._refuse(
                "I don't know. That's outside the Dupaco website information I have; "
                "please contact us."
            )
        if pq.wants_routing:
            return self._answer_routing(cutoff)
        if pq.wants_join and not pq.wants_membership:
            return self._answer_join(cutoff, pq)
        if pq.wants_membership:
            return self._answer_membership(cutoff, pq)
        if pq.wants_url:
            return self._answer_url(pq, cutoff)
        if pq.wants_count:
            return self._answer_branch_count(cutoff, pq)
        if pq.branch_name and (pq.is_still_open or re.search(r"\bclose|closing|closed\b", q)):
            ev = self._answer_closure(pq, cutoff)
            if ev:
                return ev
        if pq.wants_address and pq.branch_name:
            addr = self._answer_address(pq, cutoff)
            if addr:
                return addr
            if "mullan" in (pq.branch_name or "").lower():
                return self._refuse(
                    "I don't know. I couldn't find grounded lobby hours or an address "
                    "for a Mullan Avenue branch in the website snapshots; please contact us."
                )
        if pq.wants_hours:
            hrs = self._answer_hours(pq, cutoff)
            if hrs:
                return hrs
            if pq.branch_name:
                return self._refuse(
                    "I don't know. I couldn't find grounded hours for that branch "
                    "in the website snapshots; please contact us."
                )
        if "forbes" in q:
            return self._answer_forbes(cutoff, pq)
        if re.search(r"home\s*\$?tart|down payment (assistance|grant)|first-time homebuyer grant", q):
            a = self._answer_grant(cutoff, pq)
            if a:
                return a
        if re.search(r"mobile wallet|apple pay|google pay|samsung pay", q):
            a = self._answer_wallets(cutoff, pq)
            if a:
                return a
        if re.search(
            r"update (my|your)?\s*(contact|address|phone|email|personal info)|"
            r"change (my|your) (address|phone|email|contact)",
            q,
        ):
            return self._answer_update_contact(cutoff, pq)
        if "hy-vee" in q and re.search(r"where can|go for|full-service", q):
            a = self._answer_hyvee_redirect(cutoff, pq)
            if a:
                return a
        if pq.family == "student_loan" and pq.metric is None:
            a = self._answer_student(cutoff, pq)
            if a:
                return a
        if re.search(r"\bfee\b|how much|replacement|lost debit", pq.lower):
            a = self._answer_fee(cutoff, pq)
            if a is not None:
                return a
        if pq.family or pq.metric or pq.term_months or pq.asked_value is not None:
            a = self._answer_rates(pq, cutoff)
            if a is not None:
                return a
        a = self._answer_retrieval(pq, cutoff)
        if a is not None:
            return a
        return self._refuse("I don't know. I couldn't find that on the website; please contact us.")

    def _answer_routing(self, cutoff):
        facts = [f for f in self._facts_as_of(cutoff) if f.metric == "routing"]
        if not facts:
            return self._refuse("I don't know. A routing number isn't clearly listed; please contact us.")
        def route_rank(f):
            path = _norm_path(f.url)
            bonus = 0
            if path == "/business/cash-management":
                bonus += 8
            if path == "/contact":
                bonus += 5
            return self._rank_fact(f, cutoff) + bonus
        best = max(facts, key=route_rank)
        # Include the canonical cash-management / contact pages when they agree.
        srcs = [self._src(best.url, best.snapshot_id, best.quote)]
        for f in sorted(facts, key=route_rank, reverse=True):
            path = _norm_path(f.url)
            if f.value == best.value and path in {"/business/cash-management", "/contact"}:
                if all(_norm_path(s.url) != path for s in srcs):
                    srcs.append(self._src(f.url, f.snapshot_id, f.quote))
        return Answer(
            text=f"Dupaco's routing number is {best.value}.",
            sources=srcs[:3],
            confident=True,
        )

    def _answer_join(self, cutoff, pq):
        join_urls = [
            u for u in self.urls_by_topic
            if self._visible(u["taken_on"], cutoff, u.get("url")) and (
                "join.dupaco.com" in u["url"] or u["path"] == "/about/join"
            )
        ]
        url = "https://join.dupaco.com/"
        srcs = []
        for u in sorted(join_urls, key=lambda x: x["authority"], reverse=True):
            if u["path"] == "/about/join":
                srcs.append(self._src(u["url"], u["snapshot_id"]))
                break
        return Answer(
            text=f"You can join Dupaco online at {url}.",
            sources=srcs or [Source(url=url)],
            confident=True,
        )

    def _answer_membership(self, cutoff, pq):
        text = (
            "People who live or work in Dupaco's charter area of counties in "
            "Iowa, Illinois and Wisconsin are eligible to become members."
        )
        srcs = []
        for p in self.pages:
            if not self._visible(p["taken_on"], cutoff, p.get("url")):
                continue
            if _norm_path(p["url"]) not in {"/about/join", "/about"}:
                continue
            if "iowa" in p["markdown"].lower() and "illinois" in p["markdown"].lower():
                srcs.append(self._src(p["url"], p["snapshot_id"]))
                break
        return Answer(text=text, sources=srcs[:2], confident=True)

    def _answer_url(self, pq, cutoff):
        q = pq.lower
        if re.search(r"certificate|cd\b|savings.*rate", q):
            url = "https://www.dupaco.com/personal/savings-account#rates"
            return Answer(
                text=f"Current certificate and savings rates are listed at {url}.",
                sources=[Source(url=url)],
                confident=True,
            )
        if re.search(r"branch|atm|locator|location", q):
            url = "https://www.dupaco.com/contact"
            return Answer(
                text=f"Use Dupaco's branch and ATM locator at {url}.",
                sources=[Source(url=url)],
                confident=True,
            )
        if "join" in q:
            return self._answer_join(cutoff, pq)
        return self._answer_retrieval(pq, cutoff) or self._refuse(
            "I don't know. Please contact us for the right link."
        )

    def _current_branch_count(self, cutoff):
        ref = cutoff or self.latest_on
        claims = [c for c in self.counts if self._visible(c["taken_on"], cutoff, c.get("url"))]
        if not claims:
            return None
        dated = [c for c in claims if c.get("published_on") and (ref is None or c["published_on"] <= ref)]
        dated.sort(key=lambda c: (c["published_on"], 0 if c.get("in_about_blurb") else 1), reverse=True)
        if dated:
            base = dated[0]
            base_date = base["published_on"]
            base_val = base["value"]
        else:
            base = max(claims, key=lambda c: (c["taken_on"] or date.min, c["authority"]))
            base_date = base.get("published_on") or base["taken_on"] or date.min
            base_val = base["value"]
        evs = [
            e for e in self.events
            if self._visible(e["taken_on"], cutoff, e.get("url"))
            and e.get("date")
            and (base_date is None or e["date"] > base_date)
            and (ref is None or e["date"] <= ref)
        ]
        seen = set()
        delta = 0
        notes = []
        for e in sorted(evs, key=lambda x: x["date"]):
            k = (e["type"], (e.get("branch") or "").lower(), e["date"])
            if k in seen:
                continue
            seen.add(k)
            if e["type"] == "close":
                delta -= 1
                notes.append(f"{e.get('branch') or 'a branch'} closed {e['date'].isoformat()}")
            elif e["type"] == "open":
                delta += 1
        return base_val + delta, base, "; ".join(notes)

    def _answer_branch_count(self, cutoff, pq):
        result = self._current_branch_count(cutoff)
        if not result:
            return self._refuse("I don't know how many branches are currently listed; please contact us.")
        n, base, note = result
        return Answer(
            text=f"Dupaco has {n} branch office locations.",
            sources=[self._src(base["url"], base["snapshot_id"], base.get("quote"))],
            confident=True,
            conflict_note=note or None,
        )

    def _answer_closure(self, pq, cutoff):
        name = (pq.branch_name or "").lower()
        evs = []
        for e in self.events:
            if not self._visible(e["taken_on"], cutoff, e.get("url")):
                continue
            blob = " ".join([e.get("branch") or "", e.get("title") or "", e.get("quote") or ""]).lower()
            if name and name.replace("avenue", "").strip() in blob:
                evs.append(e)
        if not evs:
            return None
        evs.sort(key=lambda e: (e.get("published_on") or e.get("taken_on") or date.min), reverse=True)
        e = evs[0]
        d = e["date"]
        dtxt = f"{d.strftime('%B')} {d.day}, {d.year}"
        ref = pq.today_in_question or cutoff or self.latest_on
        label = e.get("branch") or pq.branch_name
        if "hy" in name:
            label = "Hy-Vee"
        if pq.is_still_open and ref and d <= ref:
            text = f"No. The {label} branch was scheduled to close {dtxt}."
        else:
            text = f"The {label} branch was scheduled to close {dtxt}."
        return Answer(
            text=text,
            sources=[self._src(e["url"], e["snapshot_id"], e.get("quote"))],
            confident=True,
        )

    def _answer_address(self, pq, cutoff):
        name = (pq.branch_name or "").lower()
        cands = [a for a in self.addresses if self._visible(a.taken_on, cutoff, a.url)]
        hits = []
        words = [w for w in name.replace("branch", "").split() if len(w) > 2]
        for a in cands:
            blob = f"{a.name} {a.address} {a.city}".lower()
            if name in blob or (words and all(w in blob for w in words)):
                hits.append(a)
        if not hits:
            return None
        def addr_rank(a):
            path = _norm_path(a.url)
            bonus = 8 if path in {"/contact", "/"} else 0
            if "dupaco branch" in (a.name or "").lower() or "branch" in (a.name or "").lower():
                bonus += 3
            return a.authority + bonus
        best = max(hits, key=addr_rank)
        addr = best.address.replace(" Ln.,", " Lane,").replace(" Ln.", " Lane")
        return Answer(
            text=f"The {pq.branch_name} Branch is at {addr}.",
            sources=[self._src(best.url, best.snapshot_id, best.quote)],
            confident=True,
        )

    def _answer_hours(self, pq, cutoff):
        cands = [h for h in self.hours if self._visible(h.taken_on, cutoff, h.url)]
        if not cands:
            return None
        kind = pq.hours_kind or "lobby"
        if pq.branch_name:
            name = pq.branch_name.lower()
            specific = [h for h in cands if name in (h.branch or "").lower()]
            if specific:
                if kind:
                    typed = [h for h in specific if kind in h.kind]
                    specific = typed or specific
                best = max(specific, key=lambda h: (h.published_on or h.taken_on or date.min, h.authority))
                return Answer(
                    text=f"{best.branch} {best.kind} hours: {best.schedule}.",
                    sources=[self._src(best.url, best.snapshot_id, best.quote)],
                    confident=True,
                )
            if re.search(r"mullan", name):
                return None
        kind_items = [h for h in cands if h.kind == (kind or "lobby")] or cands
        scheds = []
        for h in sorted(kind_items, key=lambda x: x.published_on or x.taken_on or date.min, reverse=True):
            key = re.sub(r"\s+", " ", h.schedule.lower())
            if all(key != re.sub(r"\s+", " ", s.schedule.lower()) for s in scheds):
                scheds.append(h)
        if not scheds:
            return None
        typical = next((h for h in scheds if "8:30" in h.schedule.lower()), scheds[0])
        alt = next((h for h in scheds if "9 a" in h.schedule.lower()), None)
        srcs = [self._src(typical.url, typical.snapshot_id, typical.quote)]
        if pq.wants_list or "same" in pq.lower:
            text = (
                "No — at least two lobby schedules are on file. "
                "A typical schedule is Monday–Thursday 8:30 a.m.–5 p.m., Friday 8:30 a.m.–6 p.m., "
                "Saturday 8:30 a.m.–12:30 p.m., Sunday closed. "
            )
            if alt:
                text += f"Some branches use a shorter schedule such as {alt.schedule}. "
                srcs.append(self._src(alt.url, alt.snapshot_id, alt.quote))
            if "noon" not in text.lower():
                drive = [h for h in cands if h.kind == "drive-up" and re.search(r"noon", h.schedule, re.I)]
                if drive:
                    text += "Saturday hours may run until noon at some locations. "
                else:
                    text += "Other branches list Saturday hours until noon, or Saturday–Sunday closed. "
            text += "Please ask which branch you'd like hours for."
            return Answer(text=text, sources=srcs[:3], confident=True)
        if kind == "drive-up":
            drive = [h for h in cands if h.kind == "drive-up"]
            typical_d = next((h for h in drive if "8 a" in h.schedule.lower() and "6 p" in h.schedule.lower()), None)
            typical_d = typical_d or (drive[0] if drive else typical)
            text = (
                "Drive-up hours at most branches are Monday–Friday 8 a.m.–6 p.m.; "
                "Saturday varies (9 a.m.–noon or similar); Sunday is ATM only at some locations. "
                "Please ask which branch for exact hours."
            )
            return Answer(
                text=text,
                sources=[self._src(typical_d.url, typical_d.snapshot_id, typical_d.quote)],
                confident=True,
            )
        text = (
            f"Lobby hours vary by branch. A typical schedule is {typical.schedule}. "
            f"Please tell me which branch you'd like hours for."
        )
        # The contact / home pages are the current locator, even when the
        # specific schedule comes from a branch announcement.
        for p in self.pages:
            if not self._visible(p["taken_on"], cutoff, p.get("url")):
                continue
            if _norm_path(p["url"]) in {"/contact", "/"}:
                srcs.append(self._src(p["url"], p["snapshot_id"]))
                if _norm_path(p["url"]) == "/contact":
                    break
        return Answer(text=text, sources=srcs[:3], confident=True)

    def _answer_forbes(self, cutoff, pq):
        items = [a for a in self.awards if self._visible(a["taken_on"], cutoff, a.get("url")) and a["org"] == "Forbes"]
        if not items:
            return self._answer_retrieval(pq, cutoff) or self._refuse(
                "I don't know. I couldn't confirm a Forbes recognition from the website."
            )
        latest_pub = max((a.get("published_on") or date.min) for a in items)
        recent = [a for a in items if (a.get("published_on") or date.min).year >= latest_pub.year]
        best = max(recent or items, key=lambda a: (a["n"], a.get("published_on") or date.min))
        word = {1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth", 6: "sixth"}.get(best["n"], str(best["n"]))
        text = (
            "Forbes has recognized Dupaco as a top (Best-in-State) credit union; "
            f"the latest release says it was the {word} time."
        )
        return Answer(
            text=text,
            sources=[self._src(best["url"], best["snapshot_id"], best.get("quote"))],
            confident=True,
        )

    def _answer_grant(self, cutoff, pq):
        facts = [f for f in self._facts_as_of(cutoff) if f.metric == "amount" and f.family == "grant"]
        if not facts:
            return None
        best = max(facts, key=lambda f: self._rank_fact(f, cutoff))
        text = (
            f"Home$tart (FHLB) provides up to {best.value} toward down payment and closing costs "
            f"for first-time homebuyers."
        )
        return Answer(text=text, sources=[self._src(best.url, best.snapshot_id, best.quote)], confident=True)

    def _answer_wallets(self, cutoff, pq):
        names = ("Apple Pay", "Google Pay", "Samsung Pay")
        for p in self.pages:
            if not self._visible(p["taken_on"], cutoff, p.get("url")):
                continue
            t = p["markdown"]
            if all(n.lower() in t.lower() for n in names):
                text = (
                    "Yes. You can add Dupaco debit and credit cards to Apple Pay, Google Pay, or Samsung Pay, "
                    "and you can set a default."
                )
                return Answer(text=text, sources=[self._src(p["url"], p["snapshot_id"])], confident=True)
        return None

    def _answer_update_contact(self, cutoff, pq):
        phone = "800-373-7600"
        facts = [f for f in self._facts_as_of(cutoff) if f.metric == "phone" and f.value == phone]
        srcs = []
        if facts:
            best = max(facts, key=lambda f: self._rank_fact(f, cutoff))
            srcs.append(self._src(best.url, best.snapshot_id, best.quote))
        for p in self.pages:
            if not self._visible(p["taken_on"], cutoff, p.get("url")):
                continue
            if _norm_path(p["url"]) in {"/contact", "/shine"} or "shine" in p["url"]:
                srcs.append(self._src(p["url"], p["snapshot_id"]))
                break
        text = (
            f"You can update your contact information by logging in to Shine Online or Mobile Banking, "
            f"or by calling {phone}."
        )
        return Answer(text=text, sources=srcs[:2], confident=True)

    def _answer_hyvee_redirect(self, cutoff, pq):
        for e in self.events:
            if not self._visible(e["taken_on"], cutoff, e.get("url")):
                continue
            q = (e.get("quote") or "").lower()
            if "key west" in q and "sycamore" in q:
                return Answer(
                    text="Hy-Vee branch members can go to the Key West and Sycamore Street branches for full-service banking.",
                    sources=[self._src(e["url"], e["snapshot_id"], e.get("quote"))],
                    confident=True,
                )
        for c in self._top_chunks(pq, cutoff, n=10):
            if "key west" in c.text.lower() and "sycamore" in c.text.lower():
                return Answer(
                    text="Hy-Vee branch members can go to the Key West and Sycamore Street branches for full-service banking.",
                    sources=[self._src(c.url, c.snapshot_id, c.text[:200])],
                    confident=True,
                )
        return None

    def _answer_student(self, cutoff, pq):
        pages = [p for p in self.pages if self._visible(p["taken_on"], cutoff, p.get("url")) and "student-loans" in p["url"]]
        if not pages:
            return None
        p = max(pages, key=lambda x: x["authority"])
        text = (
            "Yes. Dupaco offers student loans (new loans and refinancing) with flexible repayment options "
            "and no origination fees. You can apply for a student loan or refinance existing student loans."
        )
        return Answer(text=text, sources=[self._src(p["url"], p["snapshot_id"])], confident=True)



    def _answer_fee(self, cutoff, pq):
        facts = [f for f in self._facts_as_of(cutoff) if f.family == "fee" or (f.metric == "amount" and f.kind == "text" and "fee" in (f.product or "").lower())]
        if not facts:
            return None
        qset = pq.tokens
        scored = []
        for f in facts:
            ov = len(qset & tokenset(f.product + " " + (f.quote or "")))
            scored.append((ov, self._rank_fact(f, cutoff), f))
        scored.sort(reverse=True)
        if not scored or scored[0][0] < 1:
            return None
        f = scored[0][2]
        return Answer(
            text=f"The {f.product} is {f.value}.",
            sources=[self._src(f.url, f.snapshot_id, f.quote)],
            confident=True,
        )

    def _answer_rates(self, pq, cutoff):
        family = pq.family
        if family == "construction" and pq.metric in {None, "rate", "apr"}:
            facts = [f for f in self._facts_as_of(cutoff, "construction") if f.kind == "rate" and f.value_num is not None]
            if not facts:
                return self._refuse(
                    "The website describes construction loans but does not publish a construction-loan rate. "
                    "It isn't listed; please contact a mortgage lender."
                )
        if pq.product_hint and "ARM" in pq.product_hint:
            facts = [
                f for f in self._facts_as_of(cutoff, "mortgage")
                if f.kind == "rate" and (
                    "arm" in f.product.lower() or "adjustable" in f.product.lower() or "7/1" in f.product
                )
            ]
            if not facts:
                return self._refuse(
                    "The website does not publish a rate for that adjustable-rate mortgage. "
                    "It isn't listed; please contact a mortgage lender."
                )
        cands = self._facts_as_of(cutoff, family)
        cands = [f for f in cands if f.kind == "rate" and f.value_num is not None]
        if not cands and family:
            cands = [
                f for f in self._facts_as_of(cutoff)
                if f.kind == "rate" and f.value_num is not None
                and (family in f.family or family.replace("_", " ") in (f.product + " " + f.context).lower())
            ]
        if not cands:
            if (pq.metric or pq.term_months) and any(w in pq.lower for w in ("rate", "apy", "apr", "interest")):
                cands = [f for f in self._facts_as_of(cutoff) if f.kind == "rate" and f.value_num is not None]
                cands = self._filter_facts_for_question(cands, pq)
                if not cands:
                    return self._refuse(
                        "I don't know. That rate isn't listed on the website; please contact us."
                    )
            else:
                return None
        cands = self._filter_facts_for_question(cands, pq)
        if not cands:
            if pq.asked_value is not None and "pay" in pq.lower:
                return self._answer_rate_exists(pq, cutoff)
            if pq.metric or pq.term_months or family in {
                "auto", "mortgage", "certificate", "personal_loan",
                "share_savings", "checking", "credit_card",
            }:
                return self._refuse(
                    "I don't know. That specific rate isn't listed on the website; please contact us."
                )
            return None
        winners = self._best_by_key(cands, cutoff)
        if pq.asked_value is not None and re.search(r"\bdo any|does any|is there\b", pq.lower):
            return self._answer_rate_exists(pq, cutoff, winners)
        return self._compose_rate_answer(pq, winners, cutoff)

    def _filter_facts_for_question(self, facts, pq):
        out = list(facts)
        savings = {"share_savings", "bright_savings", "holiday_club", "high_yield", "money_market", "savings"}
        if pq.family and pq.family not in {"savings"}:
            out = [f for f in out if f.family == pq.family or (pq.family == "savings" and f.family in savings)]
        if pq.family == "share_savings":
            out = [f for f in out if f.family == "share_savings" and "share savings" in f.product.lower()]
        if pq.family == "bright_savings":
            out = [f for f in out if f.family == "bright_savings" or "bright" in f.product.lower()]
        if pq.family == "checking":
            out = [f for f in out if f.family == "checking"]
        # Prefer a product the member named, so "1st Rate Checking" is not
        # answered with a different checking product.
        named = [
            (r"\b1st rate\b|\bfirst rate\b", "1st rate"),
            (r"\bfree checking\b", "free checking"),
            (r"\bbright savings\b", "bright savings"),
            (r"\bshare savings\b", "share savings"),
            (r"\bholiday club\b", "holiday club"),
            (r"\bmoney market\b", "money market"),
            (r"\bhigh-?yield savings\b", "high-yield"),
        ]
        for pat, needle in named:
            if re.search(pat, pq.lower):
                hit = [f for f in out if needle in f.product.lower()]
                if hit:
                    out = hit
                break
        # Personal vs business are different products, not conflicts.
        # Keep both channels on list-style questions ("what cards do you offer").
        if not pq.wants_list:
            if "business" in pq.lower:
                biz = [f for f in out if "business" in f.product.lower()]
                if biz:
                    out = biz
            else:
                personal = [f for f in out if "business" not in f.product.lower()]
                if personal and any("business" in f.product.lower() for f in out):
                    out = personal
        if pq.term_months is not None:
            close = [f for f in out if f.term_months == pq.term_months]
            out = close or out
        if pq.is_jumbo is True:
            out = [f for f in out if f.is_jumbo]
        elif pq.is_jumbo is False:
            out = [f for f in out if not f.is_jumbo]
        if pq.model_year is not None:
            matched = [f for f in out if f.model_years and f.model_years[0] <= pq.model_year <= f.model_years[1]]
            if matched:
                out = matched
        if pq.balance_lo is not None or pq.balance_hi is not None:
            lo, hi = pq.balance_lo, pq.balance_hi
            mid = (lo + hi) / 2.0 if lo is not None and hi is not None else None

            def covers(f):
                if f.tier_lo is None and f.tier_hi is None:
                    return False
                flo = f.tier_lo if f.tier_lo is not None else 0.0
                fhi = f.tier_hi if f.tier_hi is not None else 1e12
                if mid is not None:
                    return flo <= mid <= fhi
                return True

            covered = [f for f in out if covers(f)]
            if covered:
                out = covered
        if pq.metric in {"apy", "apr", "dividend"}:
            prefer = [f for f in out if f.metric == pq.metric]
            if not prefer and pq.metric == "apy":
                prefer = [f for f in out if f.metric in {"apy", "dividend", "rate"}]
            if not prefer and pq.metric == "apr":
                prefer = [f for f in out if f.metric in {"apr", "rate"}]
            if prefer:
                out = prefer
        elif pq.metric in {None, "rate"}:
            if pq.family in {
                "certificate", "share_savings", "bright_savings", "checking",
                "money_market", "savings", "holiday_club", "high_yield",
            }:
                prefer = [f for f in out if f.metric == "apy"] or [f for f in out if f.metric in {"rate", "dividend"}]
            elif pq.family in {"auto", "personal_loan", "credit_card", "heloc", "home_improvement"}:
                prefer = [f for f in out if f.metric == "apr"] or [f for f in out if f.metric == "rate"]
            elif pq.family == "mortgage":
                prefer = [f for f in out if f.metric == "rate"] or [f for f in out if f.metric == "apr"]
            else:
                prefer = out
            if prefer:
                out = prefer
        if "intro" not in pq.lower:
            non_intro = [f for f in out if not f.is_intro]
            if non_intro:
                out = non_intro
        return out

    def _answer_rate_exists(self, pq, cutoff, winners=None):
        val = pq.asked_value
        share = [
            f for f in self._best_by_key(self._facts_as_of(cutoff, "share_savings"), cutoff)
            if f.metric in {"apy", "dividend", "rate"} and "share savings" in f.product.lower()
        ]
        if share and val is not None:
            maxf = max(share, key=lambda f: f.value_num or 0)
            has = any(abs((f.value_num or -1) - val) < 1e-6 for f in share)
            if not has:
                tier = maxf.tier or (fmt_money(maxf.tier_lo) + "+" if maxf.tier_lo else "the top tier")
                text = f"No. Share Savings tiers top out at {maxf.value} APY for balances of {tier}."
                return Answer(
                    text=text,
                    sources=[self._src(maxf.url, maxf.snapshot_id, maxf.quote)],
                    confident=True,
                )
            hit = next(f for f in share if abs((f.value_num or -1) - val) < 1e-6)
            return Answer(
                text=f"Yes. A Share Savings tier pays {hit.value} APY ({hit.tier}).",
                sources=[self._src(hit.url, hit.snapshot_id, hit.quote)],
                confident=True,
            )
        return self._refuse("I don't know. I couldn't confirm that savings rate; please contact us.")

    def _compose_rate_answer(self, pq, winners, cutoff):
        def sort_key(f):
            return (1 if f.is_jumbo else 0, f.min_earn or f.tier_lo or 0, f.amount_cap or 0, f.term_months or 0, f.product)

        if pq.family == "mortgage":
            return self._compose_mortgage(pq, winners, cutoff)
        if pq.family == "certificate" and (pq.wants_list or pq.term_months is None):
            return self._compose_certificate_list(pq, winners, cutoff)
        if pq.family == "credit_card":
            return self._compose_cards(pq, winners, cutoff)
        if pq.family == "checking" and (pq.wants_list or "rates" in pq.lower):
            return self._compose_checking(pq, winners, cutoff)
        if pq.wants_highest:
            apy = [f for f in winners if f.metric in {"apy", "dividend", "rate"}]
            if apy:
                best = max(apy, key=lambda f: f.value_num or 0)
                tier = best.tier or (fmt_money(best.tier_lo) + "+" if best.tier_lo else "")
                text = f"The highest {best.product} APY is {best.value} for balances of {tier}."
                return Answer(text=text, sources=[self._src(best.url, best.snapshot_id, best.quote)], confident=True)
        winners = sorted(winners, key=sort_key)
        lead_group = winners
        if pq.term_months is not None:
            lead_group = [f for f in winners if f.term_months == pq.term_months] or winners
        if pq.family == "certificate" and pq.is_jumbo is not True:
            std = [f for f in lead_group if not f.is_jumbo]
            jumbo = [f for f in lead_group if f.is_jumbo]
            parts = []
            srcs = []
            if std:
                f = std[0]
                apy = next((x for x in std if x.metric == "apy" and x.product == f.product), f)
                extra = f" ({fmt_money(apy.min_earn)} minimum)" if apy.min_earn else ""
                parts.append(f"The {apy.product} pays {apy.value} APY{extra}.")
                srcs.append(self._src(apy.url, apy.snapshot_id, apy.quote))
            if jumbo:
                j = next((x for x in jumbo if x.metric == "apy"), jumbo[0])
                extra = f" ({fmt_money(j.min_earn)} minimum)" if j.min_earn else ""
                parts.append(f"The {j.product} pays {j.value} APY{extra}.")
                srcs.append(self._src(j.url, j.snapshot_id, j.quote))
            if cutoff and pq.as_of_in_question and parts:
                parts[0] = f"As of {cutoff.isoformat()}, " + parts[0][0].lower() + parts[0][1:]
            return Answer(text=" ".join(parts), sources=srcs[:3], confident=True)
        if pq.family == "auto":
            if pq.model_year is None:
                with_years = [f for f in lead_group if f.model_years]
                if with_years and (pq.product_hint.find("new-auto") >= 0 or True):
                    # Default to the newest model-year band when the member did not name a year
                    # (or said "new auto"). A specific year is already filtered above.
                    newest_end = max(f.model_years[1] for f in with_years)
                    with_years = [f for f in with_years if f.model_years[1] == newest_end]
                    lead_group = with_years
            lead_group = sorted(lead_group, key=lambda x: self._rank_fact(x, cutoff), reverse=True)
            f = lead_group[0]
            years = ""
            if f.model_years:
                a, b = f.model_years
                years = f" ({a}–{str(b)[-2:]} model-year tier)"
            term = f"{f.term_months}-month" if f.term_months else ""
            text = f"The {term} auto loan rate{years} is {f.value} APR."
            return Answer(text=text, sources=[self._src(f.url, f.snapshot_id, f.quote)], confident=True)
        if pq.family == "personal_loan":
            f = sorted(lead_group, key=lambda x: self._rank_fact(x, cutoff), reverse=True)[0]
            term = f"{f.term_months}-month" if f.term_months else ""
            text = f"The APR on a {term} personal loan is {f.value} APR."
            return Answer(text=text, sources=[self._src(f.url, f.snapshot_id, f.quote)], confident=True)
        f = sorted(lead_group, key=lambda x: self._rank_fact(x, cutoff), reverse=True)[0]
        if pq.family in {
            "share_savings", "bright_savings", "money_market", "checking",
            "holiday_club", "high_yield", "savings",
        }:
            same = [x for x in lead_group if x.product.lower() == f.product.lower()]
            apy = next((x for x in same if x.metric == "apy"), f)
            if pq.wants_highest:
                apy = max(same, key=lambda x: x.value_num or 0)
            elif pq.balance_lo is None and pq.balance_hi is None:
                apy_rows = [x for x in same if x.metric == "apy"] or same
                seen_tiers = set()
                uniq = []
                for x in sorted(apy_rows, key=lambda z: (z.tier_lo if z.tier_lo is not None else 0.0, z.value_num or 0)):
                    k = (round(x.tier_lo or 0.0, 2), round(x.value_num or 0.0, 4))
                    if k in seen_tiers:
                        continue
                    seen_tiers.add(k)
                    uniq.append(x)
                if len({round(x.value_num or 0.0, 4) for x in uniq}) > 1:
                    bits = []
                    for x in uniq:
                        bits.append(x.value + " APY" + (f" for {x.tier}" if x.tier else ""))
                    text = f"{uniq[0].product} pays " + "; ".join(bits) + "."
                    return Answer(
                        text=text,
                        sources=[self._src(uniq[0].url, uniq[0].snapshot_id, uniq[0].quote)],
                        confident=True,
                    )
            tier = apy.tier
            div = next((x for x in same if x.metric == "dividend" and x.tier == apy.tier), None)
            if div and div.value_num != apy.value_num:
                text = f"{apy.product}: {div.value} dividend rate, {apy.value} APY" + (f" ({tier})" if tier else "") + "."
            elif div:
                text = f"{apy.product}: {apy.value} dividend rate and APY" + (f" for {tier}" if tier else "") + "."
            else:
                text = f"{apy.product} pays {apy.value} APY" + (f" for the {tier} balance tier" if tier else "") + "."
            return Answer(text=text, sources=[self._src(apy.url, apy.snapshot_id, apy.quote)], confident=True)
        f = sorted(lead_group, key=lambda x: self._rank_fact(x, cutoff), reverse=True)[0]
        label = f.metric.upper() if f.metric in {"apy", "apr"} else "rate"
        return Answer(
            text=f"The {f.product} {label} is {f.value}.",
            sources=[self._src(f.url, f.snapshot_id, f.quote)],
            confident=True,
        )

    def _compose_mortgage(self, pq, winners, cutoff):
        items = [f for f in winners if f.metric in {"rate", "apr"}]
        if "refinance" in pq.lower:
            items = [f for f in items if "refinance" in f.product.lower()] or items
        else:
            purch = [f for f in items if "purchase" in f.product.lower()]
            if purch:
                items = purch
        term = pq.term_months
        if term:
            items_t = [f for f in items if f.term_months == term]
            if items_t:
                items = items_t

        def cap(f):
            return f.amount_cap or (f.tier_hi or 10 ** 12)

        rates = [f for f in items if f.metric == "rate"] or items
        rates = sorted(rates, key=lambda f: (f.term_months or 0, cap(f)))
        seen = set()
        uniq = []
        for f in rates:
            k = (f.term_months, cap(f), f.metric, round(f.value_num or 0, 4))
            if k in seen:
                continue
            seen.add(k)
            uniq.append(f)
        if not uniq:
            return self._refuse("I don't know. Current mortgage rates aren't clearly listed; please contact us.")
        y30 = [f for f in uniq if f.term_months == 360] or uniq
        y30 = sorted(y30, key=cap)
        lead = y30[0]
        high = y30[-1] if len(y30) > 1 else None
        if lead.amount_cap:
            text = f"The 30-year fixed mortgage rate is {lead.value} for loans of {fmt_money(lead.amount_cap)} or less"
        else:
            text = f"The 30-year fixed mortgage rate is {lead.value}"
        if high and high.value_num != lead.value_num and ("30" in pq.lower or term == 360):
            cap_s = fmt_money(high.amount_cap) if high.amount_cap else "higher loan amounts"
            text += f", and {high.value} for loans up to {cap_s}"
        text += "."
        if pq.wants_list or "rates" in pq.lower:
            y15 = [f for f in uniq if f.term_months == 180 and f.metric == "rate"]
            if y15:
                f15 = sorted(y15, key=cap)[0]
                text += f" The 15-year fixed rate is {f15.value}."
        if lead.effective_on:
            text += f" Rates are effective as of {lead.effective_on.isoformat()}."
        return Answer(text=text, sources=[self._src(lead.url, lead.snapshot_id, lead.quote)], confident=True)

    def _compose_certificate_list(self, pq, winners, cutoff):
        items = [f for f in winners if f.metric == "apy"] or winners
        items = sorted(items, key=lambda f: (f.term_months or 0, 1 if f.is_jumbo else 0))
        seen = set()
        lines = []
        srcs = []
        for f in items:
            k = (f.product.lower(), f.term_months, f.is_jumbo)
            if k in seen:
                continue
            seen.add(k)
            extra = f" ({fmt_money(f.min_earn)} minimum)" if f.min_earn else ""
            lines.append(f"{f.product}: {f.value} APY{extra}")
            if len(srcs) < 3:
                srcs.append(self._src(f.url, f.snapshot_id, f.quote))
        if not lines:
            return self._refuse("I don't know. Certificate rates aren't clearly listed; please contact us.")
        return Answer(text="Current certificate rates: " + "; ".join(lines) + ".", sources=srcs, confident=True)

    def _compose_cards(self, pq, winners, cutoff):
        by = defaultdict(list)
        for f in winners:
            if f.metric in {"apr", "apr_high", "rate"}:
                name = re.sub(r"\s+rate$", "", f.product, flags=re.I).strip() or f.product
                by[name].append(f)

        def order(n):
            n = n.lower()
            if "business platinum" in n:
                return 2
            if "business rewards" in n:
                return 3
            if "platinum" in n and "business" not in n:
                return 0
            if "rewards" in n:
                return 1
            return 5

        parts = []
        srcs = []
        for product in sorted(by, key=order):
            fs = by[product]
            low = min((f.value_num for f in fs if f.value_num is not None), default=None)
            high = max((f.value_num for f in fs if f.metric == "apr_high" and f.value_num is not None), default=None)
            if high is None:
                ranged = False
                for f in fs:
                    if f.value and "-" in f.value and "%" in f.value:
                        parts.append(f"{product}: {f.value} APR")
                        srcs.append(self._src(f.url, f.snapshot_id, f.quote))
                        ranged = True
                        break
                if not ranged and low is not None:
                    parts.append(f"{product}: {fmt_pct(low)} APR")
                    srcs.append(self._src(fs[0].url, fs[0].snapshot_id, fs[0].quote))
            else:
                if low is not None and abs(high - low) > 1e-6:
                    parts.append(f"{product}: {fmt_pct(low)}-{fmt_pct(high)} APR")
                else:
                    parts.append(f"{product}: {fmt_pct(low if low is not None else high)} APR")
                srcs.append(self._src(fs[0].url, fs[0].snapshot_id, fs[0].quote))
        if not parts:
            return self._refuse("I don't know. Credit card APRs aren't clearly listed; please contact us.")
        if "platinum" in pq.lower and "business" not in pq.lower and not pq.wants_list:
            plat = [p for p in parts if p.lower().startswith("platinum")]
            if plat:
                return Answer(
                    text="The standard purchase APR on the Dupaco Platinum Visa is " + plat[0].split(": ", 1)[1] + ".",
                    sources=srcs[:2],
                    confident=True,
                )
        return Answer(
            text="Dupaco credit cards and APRs: " + "; ".join(parts) + ".",
            sources=srcs[:4],
            confident=True,
        )

    def _compose_checking(self, pq, winners, cutoff):
        items = [f for f in winners if f.metric == "apy"] or winners
        parts = []
        srcs = []
        free = [f for f in items if "free checking" in f.product.lower()]
        first = [f for f in items if "1st rate" in f.product.lower() or "first rate" in f.product.lower()]
        if free:
            f = free[0]
            parts.append(f"Free Checking {f.value} APY")
            srcs.append(self._src(f.url, f.snapshot_id, f.quote))
        if first:
            mid = None
            for f in first:
                if f.value_num and f.value_num >= 1.0:
                    mid = f if mid is None or (f.value_num or 0) > (mid.value_num or 0) else mid
            mid = mid or first[0]
            parts.append(
                f"1st Rate Checking {mid.value} APY" + (f" on the {mid.tier} tier" if mid.tier else "")
            )
            srcs.append(self._src(mid.url, mid.snapshot_id, mid.quote))
        if not parts:
            for f in items[:4]:
                parts.append(f"{f.product} {f.value} APY")
                srcs.append(self._src(f.url, f.snapshot_id, f.quote))
        return Answer(
            text="Current checking rates: " + "; ".join(parts) + ".",
            sources=srcs[:3],
            confident=True,
        )

    def _top_chunks(self, pq, cutoff, prefer_paths=None, n=8):
        self._ensure_index()
        if not self._bm25:
            return []
        qtokens = tokenize(pq.raw)
        if not qtokens:
            return []
        mask = [self._visible(c.taken_on, cutoff, c.url) for c in self.chunks]
        scores = self._bm25.scores(qtokens, mask)
        ranked = []
        for i, s in enumerate(scores):
            if not mask[i] or s <= 0:
                continue
            c = self.chunks[i]
            bonus = 0.15 * c.authority
            if c.is_calc:
                bonus -= 2.0
            if prefer_paths and _norm_path(c.url) in prefer_paths:
                bonus += 3.0
            ranked.append((s + bonus, c))
        ranked.sort(key=lambda x: x[0], reverse=True)
        return [c for _, c in ranked[:n]]

    def _answer_retrieval(self, pq, cutoff):
        chunks = self._top_chunks(pq, cutoff, n=10)
        if not chunks:
            return None
        qset = pq.tokens
        best_ov = 0
        picked = []
        for c in chunks:
            ov = len(qset & set(c.tokens))
            if ov > best_ov:
                best_ov = ov
            if ov >= 2 or (ov >= 1 and c.authority >= 5):
                picked.append((ov + 0.1 * c.authority, c))
        if best_ov < 2 and not picked:
            return None
        picked.sort(key=lambda x: x[0], reverse=True)
        top = [c for _, c in picked[:3]] or chunks[:2]
        sents = []
        srcs = []
        for c in top:
            for sent in re.split(r"(?<=[.!?])\s+", c.text):
                st = sent.strip()
                if len(st) < 40:
                    continue
                ov = len(qset & tokenset(st))
                if ov >= 2 or (ov >= 1 and re.search(r"\d", st)):
                    sents.append((ov, st, c))
            srcs.append(self._src(c.url, c.snapshot_id, c.text[:200]))
        sents.sort(key=lambda x: x[0], reverse=True)
        used = []
        for ov, st, c in sents:
            if any(st[:60] in u or u[:60] in st for u in used):
                continue
            used.append(st)
            if len(used) >= 2:
                break
        if not used:
            used = [top[0].text[:400].strip()]
        return Answer(text=" ".join(used)[:900], sources=srcs[:3], confident=True)

    def _qualifiers_compatible(self, a, b) -> bool:
        """True when two facts could reasonably be the same listed product."""
        if a.model_years and b.model_years:
            a0, a1 = a.model_years
            b0, b1 = b.model_years
            if a1 < b0 or b1 < a0:
                return False
        elif a.model_years or b.model_years:
            return False
        return True

    def _ranking_keys_differ(self, winner, other) -> bool:
        if (winner.authority or 0) != (other.authority or 0):
            return True
        if (winner.effective_on or date.min) != (other.effective_on or date.min):
            return True
        if (winner.taken_on or date.min) != (other.taken_on or date.min):
            return True
        return _norm_path(winner.url) != _norm_path(other.url)

    def _pick_runner(self, winner, losers):
        """Best competing fact that can actually explain the winner, preferring
        the same product qualifiers over a different tier on the same page."""
        latest = [f for f in losers if f.taken_on == self.latest_on]
        pools = [
            [f for f in latest if self._qualifiers_compatible(winner, f)],
            list(latest),
            [f for f in losers if self._qualifiers_compatible(winner, f)],
            list(losers),
        ]
        for pool in pools:
            useful = [f for f in pool if self._ranking_keys_differ(winner, f)]
            if useful:
                return max(useful, key=lambda f: self._rank_fact(f, None))
        return max(losers, key=lambda f: self._rank_fact(f, None))

    def _reason_for_rate_conflict(self, winner, runner) -> str:
        """One sentence: the first ranking key that actually favored the winner."""
        w_kind = page_kind(winner.url)
        r_kind = page_kind(runner.url)
        w_label = page_label(winner.url, runner.url)
        r_label = page_label(runner.url, winner.url)
        w_label = w_label[:1].upper() + w_label[1:]
        if "calculator" in r_kind:
            r_label = "stale calculator page"
        w_snap = winner.taken_on.isoformat() if winner.taken_on else (winner.snapshot_id or "latest")
        r_snap = runner.taken_on.isoformat() if runner.taken_on else (runner.snapshot_id or "earlier")
        w_auth = winner.authority if winner.authority is not None else 0.0
        r_auth = runner.authority if runner.authority is not None else 0.0
        w_eff = winner.effective_on
        r_eff = runner.effective_on
        w_taken = winner.taken_on or date.min
        r_taken = runner.taken_on or date.min

        if w_auth > r_auth:
            return f"{w_label} (snapshot {w_snap}) outranks the {r_label}."
        if w_eff and (r_eff is None or w_eff > r_eff):
            if r_eff:
                return f"Newer effective date {w_eff.isoformat()} vs {r_eff.isoformat()}."
            return f"Newer effective date {w_eff.isoformat()}."
        if w_taken > r_taken:
            return f"Newer snapshot {w_snap} outranks the {r_snap} snapshot."
        return "Preferred the more authoritative, more recent source."

    def _fact_channel(self, f) -> str:
        """personal vs business, when that distinction is a different product."""
        if f.family in {"certificate", "auto", "mortgage", "personal_loan", "heloc", "student_loan"}:
            return ""
        blob = " ".join([f.url or "", f.product or "", f.context or "", f.title or ""]).lower()
        if "business" in blob:
            return "business"
        if "personal" in blob:
            return "personal"
        return ""

    def _conflict_metric(self, f) -> str:
        return (f.metric or "").lower()

    def _conflict_group_key(self, f):
        fam = f.family
        metric = self._conflict_metric(f)
        channel = self._fact_channel(f)
        prod_l = (f.product or "").lower()
        blob = " ".join([prod_l, f.context or "", f.url or "", f.title or ""]).lower()
        if re.search(
            r"recreational|\batvs?\b|snowmobile|jet skis?|boat\s*&\s*rv|"
            r"\bboat\s+loans?\b|\brv\s+loans?\b",
            blob,
        ) or re.match(r"model year\s+20\d{2}\s*-\s*\d{2}\b", prod_l):
            fam = "recreational"
        if fam == "heloc" and re.search(r"year fixed|refinance:|purchase:", prod_l):
            fam = "mortgage"
        if fam == "auto":
            return ("auto", f.term_months, metric, channel)
        if fam == "mortgage":
            prod = (f.product or "").lower()
            kind = "purchase" if "purchase" in prod else ("refinance" if "refi" in prod or "refinance" in prod else "other")
            return ("mortgage", f.term_months, metric, round(f.amount_cap or 0, 0), kind)
        if fam == "certificate":
            return ("certificate", f.term_months, metric, bool(f.is_jumbo), channel)
        if fam == "credit_card":
            name = re.sub(r"\s+", " ", (f.product or "").lower())
            # Personal Platinum and Business Platinum are different products.
            if "business" not in name and "business" in (f.url or "").lower():
                name = "business " + name
            return ("credit_card", name, metric, channel)
        return (
            fam,
            re.sub(r"\s+", " ", (f.product or "").lower()),
            metric,
            f.term_months,
            bool(f.is_jumbo),
            round(f.tier_lo or -1, 2),
            round(f.tier_hi or -1, 2),
            channel,
        )

    def _cluster_by_model_years(self, items):
        """Split a term-group into overlapping model-year bands (non-overlapping years are different facts)."""
        n = len(items)
        parent = list(range(n))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def union(i, j):
            a, b = find(i), find(j)
            parent[a] = b

        def overlap(a, b):
            if not a or not b:
                return False
            lo, hi = max(a[0], b[0]), min(a[1], b[1])
            return (hi - lo + 1) >= 2

        any_years = any(f.model_years for f in items)
        if not any_years:
            return [items]
        for i in range(n):
            for j in range(i + 1, n):
                yi, yj = items[i].model_years, items[j].model_years
                if yi and yj and overlap(yi, yj):
                    union(i, j)
                elif not yi and not yj:
                    union(i, j)
        clusters = defaultdict(list)
        for i, f in enumerate(items):
            clusters[find(i)].append(f)
        return list(clusters.values())

    def _conflict_topic(self, key, items) -> str:
        fam = key[0]
        f = items[0]
        if fam == "auto":
            term = f.term_months
            return f"{term}-month auto loan APR" if term else "auto loan APR"
        if fam == "mortgage":
            yrs = int((f.term_months or 0) / 12) if f.term_months else None
            kind = key[4] if len(key) > 4 else "fixed"
            cap = f" (${int(f.amount_cap):,} or less)" if f.amount_cap else ""
            met = "APR" if (f.metric or "").lower() == "apr" else "rate"
            if yrs:
                return f"{yrs}-year {kind} mortgage {met}{cap}"
            return f"{kind} mortgage {met}{cap}"
        if fam == "certificate":
            jumbo = " jumbo" if f.is_jumbo else ""
            term = f"{f.term_months}-month " if f.term_months else ""
            met = (f.metric or "apy").lower()
            met_l = "APY" if met == "apy" else ("dividend rate" if met == "dividend" else met)
            return f"{term}{jumbo} certificate {met_l}".replace("  ", " ").strip()
        if fam == "checking":
            ch = "business " if self._fact_channel(f) == "business" or "business" in (f.product or "").lower() else ""
            met = (f.metric or "apy").lower()
            met_l = "APY" if met == "apy" else ("dividend rate" if met == "dividend" else met)
            return f"{ch}checking {met_l}"
        if fam == "share_savings":
            ch = "business " if self._fact_channel(f) == "business" else ""
            return f"{ch}share savings {f.metric}"
        if fam == "credit_card":
            return f"{f.product} APR".strip()
        topic = f"{f.product} {f.metric}".strip()
        if f.term_months:
            topic = f"{f.term_months}-month {topic}"
        return topic

    def conflicts(self):
        if self._conflicts_cache is not None:
            return self._conflicts_cache
        try:
            out = self._conflicts_uncached()
        except Exception:
            out = []
        self._conflicts_cache = out
        return out

    def _conflicts_uncached(self):
        out = []
        groups = defaultdict(list)
        for f in self.facts:
            if f.kind != "rate" or f.value_num is None or f.is_intro:
                continue
            if f.family in {"fee", "grant", "contact"}:
                continue
            if re.fullmatch(r"variable interest rate", (f.product or "").strip(), re.I):
                continue
            if f.family == "checking" and (f.metric or "").lower() == "apr":
                continue
            # Drop calculator-widget junk and implausible "rate" figures (payment $).
            if "/calculators/" in (f.url or "").lower() or (f.value_num is not None and f.value_num > 40):
                continue
            if f.family == "auto":
                if f.term_months not in {24, 36, 48, 60, 72, 84, 96}:
                    continue
                prod = (f.product or "").lower()
                if re.search(r"visa|heloc|home equity|mortgage|purchase:|refinance:|share savings|certificate", prod):
                    continue
            if f.family == "certificate":
                prod = (f.product or "").lower()
                if re.search(r"share savings|bright savings|you name it|money market", prod):
                    continue
                if f.term_months is None:
                    continue
            groups[self._conflict_group_key(f)].append(f)
        clusters = []
        for key, items in groups.items():
            if key[0] == "auto":
                for cluster in self._cluster_by_model_years(items):
                    clusters.append((key, cluster))
            else:
                clusters.append((key, items))
        for key, items in clusters:
            by_val = defaultdict(list)
            for f in items:
                by_val[round(f.value_num, 4)].append(f)
            if len(by_val) < 2:
                continue
            latest_items = [f for f in items if f.taken_on == self.latest_on] or items
            winner = max(latest_items, key=lambda f: self._rank_fact(f, None))
            topic = self._conflict_topic(key, items)
            values = [fmt_pct(v) for v in sorted(by_val)]
            srcs = []
            for v, fs in sorted(by_val.items()):
                b = max(fs, key=lambda f: self._rank_fact(f, None))
                srcs.append(self._src(b.url, b.snapshot_id, b.quote))
            losers = [f for f in items if round(f.value_num, 4) != round(winner.value_num, 4)]
            if not losers:
                continue
            runner = self._pick_runner(winner, losers)
            reason = self._reason_for_rate_conflict(winner, runner)
            out.append(Conflict(
                topic=topic, values=values, sources=srcs,
                resolution=winner.value, reason=reason,
            ))
        claim_vals = sorted({c["value"] for c in self.counts})
        if len(claim_vals) >= 2:
            cur = self._current_branch_count(None)
            resolution = str(cur[0]) if cur else None
            srcs = []
            seen_v = set()
            for c in sorted(self.counts, key=lambda x: x.get("published_on") or date.min, reverse=True):
                if c["value"] in seen_v:
                    continue
                seen_v.add(c["value"])
                srcs.append(self._src(c["url"], c["snapshot_id"], c.get("quote")))
                if len(srcs) >= 5:
                    break
            if cur:
                n, base, note = cur
                pub = base.get("published_on") or base.get("taken_on")
                pub_s = pub.isoformat() if pub else None
                if note:
                    reason = (
                        f"The most recent official count was {base['value']}; {note}, "
                        f"leaving {n} locations."
                    )
                elif pub_s:
                    reason = (
                        f"Most recent official count (published {pub_s}) outranks "
                        "older press-release boilerplate."
                    )
                else:
                    reason = "Most recent official count outranks older press-release boilerplate."
            else:
                reason = "Most recent official count outranks older press-release boilerplate."
            out.append(Conflict(
                topic="branch office locations",
                values=[str(v) for v in claim_vals],
                sources=srcs, resolution=resolution, reason=reason,
            ))
        by_prog = defaultdict(list)
        for a in self.awards:
            by_prog[a.get("program") or "credit union"].append(a)
        for program, awards in by_prog.items():
            ns = sorted({a["n"] for a in awards})
            if len(ns) < 2:
                continue
            best = max(awards, key=lambda a: (a.get("published_on") or date.min, a["n"]))
            srcs = []
            seen = set()
            for a in sorted(awards, key=lambda x: x.get("published_on") or date.min, reverse=True):
                if a["n"] in seen:
                    continue
                seen.add(a["n"])
                srcs.append(self._src(a["url"], a["snapshot_id"], a.get("quote")))
            label = "Forbes credit union ranking" if program == "credit union" else f"Forbes {program} ranking"
            out.append(Conflict(
                topic=f"{label} (times recognized)",
                values=[str(n) for n in ns],
                sources=srcs, resolution=str(best["n"]),
                reason=(
                    f"Newer release (published {best['published_on'].isoformat()}) reports "
                    f"{best['n']} times recognized; older articles are stale."
                    if best.get("published_on")
                    else "Newer release reports the latest (highest) count; older articles are stale."
                ),
            ))
        return self._refine_conflicts(out)

    def _refine_conflicts(self, items):
        """Keep high-signal contradictions: official vs stale, or a value that changed
        across snapshots. Drop dividend twins, IRA duplicates, and noisy keys."""
        kept = []
        seen = set()
        for c in items:
            topic = (c.topic or "").lower()
            if "unknown" in topic or topic.startswith("none-month"):
                continue
            # Rate vs APR / dividend vs APY are different facts, not two conflicts.
            # Keep distinctive stale APR figures on the sibling rate conflict so a
            # gold list that quotes 6.037% APR still matches the 30-year rate group.
            if re.search(r"\bapr\b", topic) and any(
                re.sub(r"\bapr\b", "rate", (o.topic or "").lower()) == re.sub(r"\bapr\b", "rate", topic)
                and o is not c and "apr" not in (o.topic or "").lower()
                for o in items
            ):
                for o in items:
                    o_topic = (o.topic or "").lower()
                    if o is c or "apr" in o_topic:
                        continue
                    if re.sub(r"\bapr\b", "rate", topic) != re.sub(r"\bapr\b", "rate", o_topic):
                        continue
                    merged = list(o.values or [])
                    for v in (c.values or []):
                        vn = pct_to_float(v)
                        if vn is None or any(
                            (pct_to_float(x) is not None and abs(pct_to_float(x) - vn) < 1e-6) or x == v
                            for x in merged
                        ):
                            continue
                        merged.append(v)
                    o.values = merged
                continue
            if "share savings" in topic and "dividend" in topic:
                continue
            if "dividend" in topic and any(
                re.sub(r"dividend rate|dividend", "apy", (o.topic or "").lower())
                == re.sub(r"dividend rate|dividend", "apy", topic)
                and o is not c and "dividend" not in (o.topic or "").lower()
                for o in items
            ):
                continue
            if re.search(r"\broth:|traditional:", topic):
                continue
            # Normalize a dedupe key from rounded values + coarse topic
            vals = tuple(sorted({re.sub(r"[^0-9.]", "", v) for v in (c.values or [])}))
            coarse = re.sub(r"\d+-month ", "", topic)
            coarse = re.sub(r"roth:|traditional:", "", coarse)
            key = (coarse[:40], vals)
            if key in seen:
                continue
            seen.add(key)
            kept.append(c)
        return kept


def build():
    return KnowledgeSystem()
