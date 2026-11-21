"""Bon Temps event collector.

Checks every source in collector/sources.json (plus manual.json and approved
"Submit an event" issues), cleans and merges the events, and writes
docs/events.json, which the website loads.

Uses only the Python standard library, so there is nothing to install.

    python collector/collect.py            # check everything
    python collector/collect.py "Blue Moon" # check only sources whose name contains this

Optional environment variables:
    GITHUB_TOKEN          lets the AI read venue pages that have no data feed
                          (GitHub Actions provides this automatically)
    TICKETMASTER_API_KEY  free key from developer.ticketmaster.com
    GITHUB_REPOSITORY     owner/repo, used to read approved event submissions
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCES_FILE = ROOT / "collector" / "sources.json"
MANUAL_FILE = ROOT / "collector" / "manual.json"
OUT_FILE = ROOT / "docs" / "events.json"

DAYS_AHEAD = 60
USER_AGENT = "BonTemps/1.0 (community events guide for Lafayette, LA)"
AI_URL = "https://models.github.ai/inference/chat/completions"
AI_MODEL = os.environ.get("BONTEMPS_MODEL", "openai/gpt-4o-mini")
AI_PAGE_CHARS = 18000  # keeps each request inside the free tier's size limit

# Lafayette is at the center; events elsewhere are dropped when a city is given.
AREA = {
    "lafayette", "broussard", "youngsville", "scott", "carencro", "duson", "milton",
    "maurice", "breaux bridge", "st. martinville", "st martinville", "saint martinville",
    "cecilia", "arnaudville", "rayne", "sunset", "grand coteau", "opelousas", "abbeville",
    "new iberia", "church point", "erath", "kaplan", "crowley",
}

CATEGORIES = {
    "music": r"music(?!al)|concert|\bband\b|jazz|blues|zydeco|cajun jam|\bdj\b|symphony|orchestra|choir|singer|songwriter|fais do-do|dance ?hall|two-step|karaoke|open mic|bluegrass|country night|hip hop|\brap\b|rock\b|\bjam\b|acoustic|live on|, tba\b|\bw/ |\bft\b.|\bfeat\b.|album release",
    "food": r"\bfood|dinner|brunch|lunch|breakfast|crawfish|boudin|gumbo|tasting|\bwine|\bbeer|brew|cocktail|\bchef|cooking|farmers market|\bfeast|supper|bbq|barbecue|pop-up|happy hour|cook-off|cookoff",
    "arts": r"\bart\b|\barts\b|museum|gallery|exhibit|\bfilm|movie|poetry|\bbook|author|artwalk|art walk|\bcraft|painting|\bscreen(?:ing)?s?\b|film festival|selection of works|artist talk",
    "theater": r"theat(?:er|re)|musical|broadway|\ba play\b|stage play|new play|\bplays\b|shakespeare|playwright|ballet|nutcracker|opera\b|\bimprov\b|comedy|comedian|stand-up|drag show|cabaret|readers.? theat|on stage|\bthe wiz\b|saturday night live|\bsnl\b|late night with|netflix special|comedy central",
    "wellness": r"yoga|\brun\b|\b5k\b|\b10k\b|marathon|half marathon|\bwalk\b|fitness|workout|meditation|sound bath|wellness|\bhealth|pilates|\bbike|cycling|\bhike|tai chi|blood drive|zumba|mental health|run club",
    "family": r"\bkids?\b|children|family|storytime|story time|toddler|\bteens?\b|sensory|playtime|\bcamp\b|santa|easter egg|trick or treat|\bbaby|preschool",
    "civic": r"town ?hall|public (?:forum|meeting|hearing|comment|input)|community (?:meeting|forum|conversation|input)|listening session|speak ?out|\brally\b|protest|\bmarch (?:for|against|on)\b|\bcandidates?\b|\bdebate|\belections?\b|\bvoters?\b|\bvoting\b|\bcity council|parish council|school board|legislat|lawmakers?|elected officials?|advocacy|\bforum\b|state of (?:the|homelessness)|\bcivic|ballot|petition|\bvigil\b|know your rights",
    "sports": r"basketball|football|baseball|softball|volleyball|soccer|ragin.? cajuns|\btennis\b|track (?:and|&) field|\bgolf (?:tournament|classic)|\brodeo\b|\bboxing\b|wrestling|tailgat|\bgame day\b|championships?\b",
    "community": r"volunteer|clean ?up|fundraiser|benefit|charity|workshop|lecture|\btalk\b|seminar|networking|parade|\bfair\b|market|festival|\bclass\b",
}
CATEGORY_ORDER = list(CATEGORIES)
THEATER_HINT = r"best musical|musical theat|broadway|theat(?:er|re) company|stage play|\bplaywright|\ba play\b|\bplay by\b|by william shakespeare|ballet company|touring production"


class Skip(Exception):
    """A source that can't run right now (for example, no API key)."""


# ---------- time (Lafayette is US Central, with daylight saving) ----------

def _central_offset_hours(utc_naive: datetime) -> int:
    y = utc_naive.year

    def nth_sunday(month: int, n: int) -> datetime:
        d = datetime(y, month, 1)
        d += timedelta(days=(6 - d.weekday()) % 7)
        return d + timedelta(weeks=n - 1)

    dst_start = nth_sunday(3, 2) + timedelta(hours=8)  # 2 AM CST
    dst_end = nth_sunday(11, 1) + timedelta(hours=7)   # 2 AM CDT
    return -5 if dst_start <= utc_naive < dst_end else -6


def to_central(d: datetime) -> datetime:
    """Aware datetime -> naive Lafayette local time."""
    u = d.astimezone(timezone.utc).replace(tzinfo=None)
    return u + timedelta(hours=_central_offset_hours(u))


def central_to_utc(d: datetime) -> datetime:
    guess = d + timedelta(hours=6)
    return d - timedelta(hours=_central_offset_hours(guess))


def now_central() -> datetime:
    return to_central(datetime.now(timezone.utc))


def parse_when(value) -> tuple[datetime | None, bool]:
    """Returns (local naive datetime, is_date_only)."""
    if not value:
        return None, False
    s = str(value).strip()
    date_only = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", s))
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
        if not m:
            return None, False
        d, date_only = datetime.fromisoformat(m.group(1)), True
    if d.tzinfo:
        d = to_central(d)
    return d, date_only


def iso(d: datetime | None) -> str | None:
    return d.strftime("%Y-%m-%dT%H:%M") if d else None


# ---------- helpers ----------

try:  # use an up-to-date certificate list when available (fixes some Windows setups)
    import certifi
    import ssl
    _SSL = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL = None


def fetch(url: str, *, headers: dict | None = None, data: bytes | None = None, timeout: int = 40) -> str:
    h = {"User-Agent": USER_AGENT, "Accept": "text/html,application/json,text/calendar,*/*"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as r:
        charset = r.headers.get_content_charset() or "utf-8"
        return r.read().decode(charset, "replace")


# WordPress page-builder "shortcodes" like [et_pb_text admin_label="..."] or [/et_pb_section].
# Quoted attribute values may themselves contain brackets, so they're matched as whole strings.
SHORTCODE = re.compile(r"""\[/?[a-z][a-z0-9_-]*(?:\s+[\w-]+(?:=(?:"[^"]*"|'[^']*'|[^\s\]]+))?)*\s*/?\]""")
MD_LINK = re.compile(r"\[([^\[\]]+)\]\((https?://[^)\s]+)\)")


def strip_code(s: str) -> str:
    for _ in range(3):                         # nested shortcodes
        s = SHORTCODE.sub(" ", s)
    s = MD_LINK.sub(r"\1", s)                  # [text](https://…) -> text
    s = re.sub(r"\{\{.*?\}\}|\{%.*?%\}", " ", s)  # template tags
    s = re.sub(r"\*\*|__|^#{1,6}\s+", "", s, flags=re.M)  # markdown bold and headings
    return s


def looks_like_code(s: str) -> bool:
    """True when text still reads like markup instead of a description."""
    return bool(re.search(r'\[/?[a-z_]+[\s\]]|[\w-]+="[^"]*"|</?[a-z]+[\s>]|\{"|":\s*"|\bfunction\s*\(|;\s*\}', s))


def clean(text, limit: int = 420, breaks: bool = False) -> str:
    s = strip_code(str(text or "")).replace("%26", "&")
    if breaks:  # keep paragraph breaks for event descriptions
        s = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h\d>|</div>", "\n", s)
    s = html.unescape(re.sub(r"<[^>]+>", " ", s))
    if breaks:
        s = re.sub(r"[ \t\r\f\v]+", " ", s)
        s = re.sub(r" *\n *", "\n", s)
        # drop leftover button text like "BUY TICKETS HERE" or "Read more"
        s = re.sub(r"(?im)^(buy|get) tickets( here| now)?!?$|^(click here|read more|learn more|more info)\.?$", "", s)
        s = re.sub(r"\n{2,}", "\n\n", s).strip()
    else:
        s = re.sub(r"\s+", " ", s).strip()
    if len(s) > limit:
        s = s[:limit].rsplit(" ", 1)[0].rstrip(",.;:") + "…"
    return s


def in_area(city: str) -> bool:
    c = city.strip().lower()
    return not c or c in AREA


# Words in venue names that say nothing about the event ("Acadiana Center for the Arts", "Moncus Theater")
VENUE_WORDS = r"(?i)theat(?:er|re)|performing|" + chr(92) + "barts?" + chr(92) + "b"


def categorize(main_text: str, extra_text: str = "", forced: list[str] | None = None,
               fallback: list[str] | None = None) -> tuple[list[str], bool]:
    """Categories come from the title, venue and the source's own tags. The description is only
    used when those say nothing (so a storytime that mentions "songs" doesn't become music).
    Returns (categories, guessed): guessed is True when nothing matched and the source's
    fallback (or "community") was used, so a better-informed duplicate can override it."""
    cats = [c for c in (forced or []) if c in CATEGORIES]
    for text in (main_text, extra_text):
        t = text.lower()
        for name, pattern in CATEGORIES.items():
            if name == "civic" and text is extra_text:  # only a title makes something civic
                continue
            if name not in cats and re.search(pattern, t):
                cats.append(name)
        if cats:
            break
    # A show's description often says what it is even when the title doesn't ("The Wiz" -> "Best Musical")
    # (skipped for music-only sources like Blue Moon, whose band bios talk about "the musical heritage")
    if "theater" not in cats and "music" not in (forced or []) and re.search(THEATER_HINT, extra_text.lower()):
        cats.append("theater")
    # "Nutcracker Painting Class" or "The Nutcracker Tea" are themed outings, not stage shows
    main = main_text.lower()
    if ("theater" in cats and re.search(r"\b(?:class|classes|painting|paint|workshop|camp|tea|cake|cookies?)\b", main)
            and not re.search(r"theat(?:er|re)|musical|\bplay\b|comedy|improv|ballet\b(?! cake)", main)):
        cats.remove("theater")
    guessed = not cats
    if guessed:
        cats = list(fallback or ["community"])
    return sorted(cats, key=CATEGORY_ORDER.index)[:3], guessed


def price_info(raw) -> tuple[str, bool]:
    p = clean(raw, 60)
    free = bool(re.search(r"\bfree\b", p, re.I)) or p in {"0", "$0", "0.00", "$0.00"}
    return ("Free" if free else p), free


def make_event(src: dict, *, title, start, end=None, all_day=False, venue="", address="",
               price="", url="", desc="", image="", hint="") -> dict | None:
    title = clean(title, 140)
    s, date_only = parse_when(start)
    if not title or not s:
        return None
    e, _ = parse_when(end)
    if e and e <= s:
        e = None
    all_day = bool(all_day) or date_only
    price_text, free = price_info(price)
    desc = clean(desc, 4000, breaks=True)
    if looks_like_code(desc):  # couldn't clean it: show the "find more details" link instead
        desc = ""
    venue = clean(venue, 120)
    if re.fullmatch(r"(?i)(?:location|venue|place|event)(?: name)?|tb[adc]|to be (?:announced|determined)|n/?a|none|-+", venue):
        venue = ""  # calendar placeholders like Moncus Park's "Location Name"
    venue = venue or src.get("venue", "")
    cats, guessed = categorize(f"{title} {hint} {re.sub(VENUE_WORDS, '', venue)}", desc,
                               src.get("cats"), src.get("fallback"))
    return {
        "title": title,
        "start": iso(s),
        "end": iso(e),
        "allDay": all_day,
        "ongoing": bool(e and e - s >= timedelta(days=3)),  # exhibits, seasons, multi-day runs
        "venue": venue,
        "address": clean(address, 160),
        "price": price_text,
        "free": free,
        "cats": cats,
        "_guess": guessed,
        "desc": desc,
        "url": url or src.get("url", ""),
        "image": image if isinstance(image, str) and image.startswith("http") else "",
        "source": src["name"],
    }


# ---------- source: WordPress "The Events Calendar" ----------

def src_tribe(src, start, end):
    base = src["url"].rstrip("/") + "/wp-json/tribe/events/v1/events"
    url = base + "?" + urllib.parse.urlencode({
        "per_page": 50,
        "start_date": start.strftime("%Y-%m-%d %H:%M:%S"),
        "end_date": end.strftime("%Y-%m-%d 23:59:59"),
    })
    out = []
    for _ in range(40):
        data = json.loads(fetch(url, timeout=90))
        for e in data.get("events", []):
            v = e.get("venue") if isinstance(e.get("venue"), dict) else {}
            city = (v.get("city") or "").strip()
            if not in_area(city):
                continue
            img = e.get("image") if isinstance(e.get("image"), dict) else {}
            out.append(make_event(
                src, title=e.get("title"), start=e.get("start_date"), end=e.get("end_date"),
                all_day=e.get("all_day"), venue=v.get("venue") or "",
                address=", ".join(x for x in [v.get("address"), city] if x),
                price=e.get("cost"), url=e.get("url"), desc=e.get("description"),
                image=img.get("url", ""),
                hint=" ".join(c.get("name", "") for c in e.get("categories", [])),
            ))
        url = data.get("next_rest_url")
        if not url:
            break
    return out


# ---------- source: structured event data built into a page (schema.org JSON-LD) ----------

def jsonld_events(page: str) -> list[dict]:
    found = []
    for m in re.finditer(r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", page, re.S | re.I):
        try:
            data = json.loads(m.group(1).strip())
        except ValueError:
            continue
        stack = [data]
        while stack:
            x = stack.pop()
            if isinstance(x, list):
                stack.extend(x)
            elif isinstance(x, dict):
                t = x.get("@type")
                types = t if isinstance(t, list) else [t]
                if any(isinstance(y, str) and y.endswith("Event") for y in types):
                    found.append(x)
                for k in ("@graph", "itemListElement", "item", "event", "subEvent"):
                    if k in x:
                        stack.append(x[k])
    return found


def _undo_bad_offset(value):
    try:
        d = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return value
    if not d.tzinfo:
        return value
    return (d.replace(tzinfo=None) - d.utcoffset()).isoformat()


def from_jsonld(src, x):
    loc = x.get("location") or {}
    if isinstance(loc, list):
        loc = loc[0] if loc else {}
    venue, address = "", ""
    if isinstance(loc, dict):
        venue = loc.get("name", "")
        a = loc.get("address")
        if isinstance(a, dict):
            if not in_area(a.get("addressLocality") or ""):
                return None
            address = ", ".join(v for v in [a.get("streetAddress"), a.get("addressLocality")] if v)
        elif isinstance(a, str):
            address = a
    elif isinstance(loc, str):
        venue = loc
    offers = x.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    price = ""
    if isinstance(offers, dict) and offers.get("price") not in (None, ""):
        p = str(offers["price"])
        price = "Free" if p in {"0", "0.0", "0.00"} else (p if p.startswith("$") else f"${p}")
    image = x.get("image")
    if isinstance(image, list):
        image = image[0] if image else ""
    if isinstance(image, dict):
        image = image.get("url", "")
    s, e = x.get("startDate"), x.get("endDate")
    if src.get("fixTimezone"):  # site labels local times as if they were UTC
        s, e = _undo_bad_offset(s), _undo_bad_offset(e)
    return make_event(src, title=x.get("name"), start=s, end=e,
                      venue=venue, address=address, price=price, url=x.get("url", ""),
                      desc=x.get("description", ""), image=image or "")


def src_jsonld(src, start, end):
    page = fetch(src["url"])
    items = jsonld_events(page)
    if src.get("details"):  # list pages only give dates: open each event's page for times and details
        items = [_with_details(x, src["details"] == "description") for x in items]
    events = [from_jsonld(src, x) for x in items]
    if not any(events):
        raise Skip("no event data found on the page")
    return events


# Event pages already read are remembered between runs (collector/detail-cache.json), so each
# update only opens pages for events it hasn't seen before.
DETAIL_FILE = ROOT / "collector" / "detail-cache.json"
DETAILS: dict = {}
DETAILS_USED: set = set()


DETAILS_VERSION = 2  # bump to re-read every cached page (2: full write-ups)


def _full_text(page: str) -> str:
    """The complete write-up shown on an event page. Some sites (Happening Next) cut the
    description in their event data at 300 characters but print all of it on the page."""
    m = re.search(r'<div[^>]+class="[^"]*\bep-desc\b[^"]*"[^>]*>', page)
    if not m:
        return ""
    depth, pos = 1, m.end()
    for t in re.finditer(r"<(/?)div\b[^>]*>", page[pos:]):
        depth += -1 if t.group(1) else 1
        if depth == 0:
            return page[pos:pos + t.start()]
    return ""


def _with_details(x, desc_only: bool = False):
    url = x.get("url")
    if not url:
        return x
    DETAILS_USED.add(url)
    if url not in DETAILS or DETAILS[url].get("_v") != DETAILS_VERSION:
        try:
            page = fetch(url)
            found = jsonld_events(page)
            DETAILS[url] = {**(found[0] if found else {}), "_full": _full_text(page), "_v": DETAILS_VERSION}
        except (urllib.error.URLError, TimeoutError, ValueError):
            return x  # try again next run
        time.sleep(0.3)
    d = {k: v for k, v in DETAILS[url].items() if not k.startswith("_")}
    full = DETAILS[url].get("_full", "")
    if desc_only:  # recurring shows: the page lists every date, so keep this listing's date
        d = {"description": d.get("description", "")}
    if len(clean(full, 99999)) > len(clean(d.get("description") or x.get("description") or "", 99999)):
        d["description"] = full
    merged = {**x, **{k: v for k, v in d.items() if v not in (None, "", [], {})}}
    merged["url"] = url
    return merged


# ---------- source: iCal calendar feed ----------

def _ics_time(v: str, params: str = "") -> tuple[datetime | None, bool]:
    """iCal date/time -> (Lafayette local naive datetime, is_date_only)."""
    v = v.strip()
    if "VALUE=DATE" in params.upper() and "DATE-TIME" not in params.upper() or re.fullmatch(r"\d{8}", v):
        return datetime.strptime(v[:8], "%Y%m%d"), True
    m = re.match(r"(\d{8})T(\d{4})", v)
    if not m:
        return None, False
    d = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M")
    if v.endswith("Z"):
        d = to_central(d.replace(tzinfo=timezone.utc))
    return d, False  # times with a TZID are treated as local (Central) time


_WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


def _occurrences(first: datetime, rule: str, window_end: datetime) -> list[datetime]:
    """Expand a simple RRULE (daily, weekly, monthly) up to window_end."""
    r = dict(p.split("=", 1) for p in rule.strip().split(";") if "=" in p)
    freq, interval = r.get("FREQ", ""), int(r.get("INTERVAL", "1"))
    count = int(r["COUNT"]) if "COUNT" in r else None
    until = _ics_time(r["UNTIL"])[0] if "UNTIL" in r else None
    stop = min(window_end, until) if until else window_end
    days = [(re.match(r"(-?\d*)([A-Z]{2})", b).groups()) for b in r.get("BYDAY", "").split(",") if b]
    out, d = [], first
    if freq == "DAILY":
        while d <= stop and (count is None or len(out) < count):
            out.append(d)
            d += timedelta(days=interval)
    elif freq == "WEEKLY":
        wanted = sorted({_WEEKDAYS[w] for _, w in days} or {first.weekday()})
        week = first - timedelta(days=first.weekday())
        while week <= stop and (count is None or len(out) < count):
            for wd in wanted:
                x = week + timedelta(days=wd)
                if first <= x <= stop and (count is None or len(out) < count):
                    out.append(x)
            week += timedelta(weeks=interval)
    elif freq == "MONTHLY":
        y, mo = first.year, first.month
        while datetime(y, mo, 1) <= stop and (count is None or len(out) < count):
            if days:  # e.g. BYDAY=2SA (second Saturday) or -1FR (last Friday)
                for n, w in days:
                    hits = [datetime(y, mo, dd, first.hour, first.minute) for dd in range(1, 32)
                            if _valid(y, mo, dd) and datetime(y, mo, dd).weekday() == _WEEKDAYS[w]]
                    pick = hits[int(n) - 1 if int(n or 1) > 0 else int(n)] if hits and abs(int(n or 1)) <= len(hits) else None
                    if pick and first <= pick <= stop:
                        out.append(pick)
            elif _valid(y, mo, first.day):
                x = first.replace(year=y, month=mo)
                if x <= stop:
                    out.append(x)
            mo += interval
            y, mo = y + (mo - 1) // 12, (mo - 1) % 12 + 1
    else:
        out = [first]
    return out


def _valid(y, m, d):
    try:
        datetime(y, m, d)
        return True
    except ValueError:
        return False


def src_ical(src, start, end):
    text = fetch(src["url"], timeout=90)
    text = re.sub(r"\r?\n[ \t]", "", text)  # unfold long lines
    out = []
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", text, re.S):
        fields: dict[str, list[tuple[str, str]]] = {}
        for line in block.strip().splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            name, _, params = key.partition(";")
            value = value.strip().replace("\\n", "\n").replace("\\,", ",").replace("\\;", ";")
            fields.setdefault(name.upper(), []).append((value, params))
        get = lambda k: fields.get(k, [("", "")])[0][0]
        if get("STATUS").upper() == "CANCELLED" or "DTSTART" not in fields:
            continue
        first, date_only = _ics_time(*fields["DTSTART"][0])
        if not first:
            continue
        last = _ics_time(*fields["DTEND"][0])[0] if "DTEND" in fields else None
        length = (last - first) if last and last > first else None

        starts = _occurrences(first, get("RRULE"), end) if "RRULE" in fields else [first]
        for v, p in fields.get("RDATE", []):
            starts += [d for d in (_ics_time(x, p)[0] for x in v.split(",")) if d]
        skip = {d for v, p in fields.get("EXDATE", []) for d in (_ics_time(x, p)[0] for x in v.split(","))}

        desc_html = get("DESCRIPTION")
        img = get("X-WP-IMAGES-URL").split(";")[0] or (re.findall(r"<img[^>]+src='([^']+)'", desc_html) or [""])[0]
        location = get("LOCATION")
        title = get("SUMMARY")
        # keep: only events whose "title @ location" matches (UL: home games, "vs ... @ Lafayette, ...")
        if src.get("keep") and not re.search(src["keep"], f"{title} @ {location}", re.I):
            continue
        for pat, rep in src.get("rename", []):  # tidy titles: "University of Louisiana at Lafayette" -> "Ragin' Cajuns"
            title = re.sub(pat, rep, title).strip()
        for pat, rep in src.get("renameLocation", []):  # "Lafayette, Cajundome" -> "Cajundome"
            location = re.sub(pat, rep, location).strip()
        link = get("URL") or src.get("link", "")
        for pat, rep in src.get("renameLink", []):
            link = re.sub(pat, rep, link)
        if desc_html.strip().lower().startswith(get("SUMMARY").strip().lower()):
            desc_html = ""  # the description only repeats the title
        cost = get("X-COST")  # X-COST-TYPE isn't used: some calendars mark every event "free" by default
        venue, _, addr = location.partition(" @ ")  # "Blue Moon Saloon @ 215 East Convent Street"
        for s in sorted(set(starts) - skip):
            e = s + length if length else None
            if e and e.strftime("%H:%M") == "23:59":  # placeholder "until midnight" end time
                e = None
            if (e or s) < start or s > end:
                continue
            fmt = "%Y-%m-%d" if date_only else "%Y-%m-%dT%H:%M"
            out.append(make_event(
                src, title=title, start=s.strftime(fmt), end=e.strftime(fmt) if e and not date_only else None,
                venue=(venue if addr else location.split(",")[0]) if location else "",
                address=addr or (location if "," in location else ""),
                price=cost, url=link, desc=desc_html, image=img,
            ))
    return out


# ---------- source: Ticketmaster (Cajundome, Heymann and other ticketed shows) ----------

def src_ticketmaster(src, start, end):
    key = os.environ.get("TICKETMASTER_API_KEY")
    if not key:
        raise Skip("add a free TICKETMASTER_API_KEY to turn this on")
    out = []
    for page in range(5):
        url = "https://app.ticketmaster.com/discovery/v2/events.json?" + urllib.parse.urlencode({
            "apikey": key, "latlong": "30.2241,-92.0198", "radius": "30", "unit": "miles",
            "size": "200", "page": page, "sort": "date,asc",
            "startDateTime": central_to_utc(start).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endDateTime": central_to_utc(end).strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
        data = json.loads(fetch(url))
        for e in data.get("_embedded", {}).get("events", []):
            d = e.get("dates", {}).get("start", {})
            when = d.get("localDate", "")
            if d.get("localTime") and not d.get("timeTBA"):
                when += "T" + d["localTime"][:5]
            venues = e.get("_embedded", {}).get("venues", [{}])
            v = venues[0] if venues else {}
            city = (v.get("city") or {}).get("name", "")
            if not in_area(city):
                continue
            pr = (e.get("priceRanges") or [{}])[0]
            price = ""
            if pr.get("min") is not None:
                lo, hi = round(pr["min"]), round(pr.get("max") or pr["min"])
                price = f"${lo}" if lo == hi else f"${lo}–${hi}"
            imgs = sorted(e.get("images", []), key=lambda i: -(i.get("width") or 0))
            genre = " ".join(
                (c.get(k) or {}).get("name", "") for c in e.get("classifications", [])
                for k in ("segment", "genre", "subGenre"))
            out.append(make_event(
                src, title=e.get("name"), start=when, venue=v.get("name", ""),
                address=", ".join(x for x in [(v.get("address") or {}).get("line1"), city] if x),
                price=price, url=e.get("url", ""), desc=e.get("info") or e.get("pleaseNote") or "",
                image=imgs[0]["url"] if imgs else "",
                hint=genre.replace("Music", "music").replace("Arts & Theatre", "theater").replace("Sports", "basketball"),
            ))
        if page + 1 >= data.get("page", {}).get("totalPages", 1):
            break
        time.sleep(0.3)
    return out


# ---------- source: Squarespace event pages (Hideaway on Lee) ----------

def src_squarespace(src, start, end):
    data = json.loads(fetch(src["url"].split("?")[0] + "?format=json"))
    base = src["url"].split("/", 3)
    origin = "/".join(base[:3])
    out = []
    for e in data.get("upcoming", []) + data.get("items", []):
        title = e.get("title", "")
        if not e.get("startDate") or re.search(r"\bclosed\b", title, re.I):
            continue
        utc = lambda ms: datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        loc = e.get("location") or {}
        out.append(make_event(
            src, title=title, start=to_central(utc(e["startDate"])).isoformat(),
            end=to_central(utc(e["endDate"])).isoformat() if e.get("endDate") else None,
            venue=loc.get("addressTitle") or "", address=loc.get("addressLine1") or "",
            url=origin + e["fullUrl"] if e.get("fullUrl", "").startswith("/") else src["url"],
            desc=e.get("excerpt") or e.get("body") or "", image=e.get("assetUrl") or ""))
    return out


# ---------- source: a listing page that links to one page per event (Wix Events: Cité des Arts) ----------

def _read_event_pages(src, urls, limit=45):
    """Open each event page and read the event data built into it."""
    out = []
    for u in urls[:limit]:
        try:
            page = fetch(u)
        except urllib.error.URLError:
            continue
        for x in jsonld_events(page):
            x.setdefault("url", u)
            out.append(from_jsonld(src, x))
        time.sleep(0.4)
    return out


def src_eventlinks(src, start, end):
    """Collect event links from the listing page(s), then read each event page."""
    pattern = re.compile(src["links"])
    urls = []
    for listing in [src["url"], *src.get("more", [])]:
        for u in pattern.findall(fetch(listing)):
            if u not in urls:
                urls.append(u)
    return _read_event_pages(src, urls)


def src_wixsitemap(src, start, end):
    """Wix sites list every event page in a sitemap, even when the calendar itself loads with
    JavaScript (Wonderland, Cité des Arts). Event addresses usually end with their date
    ("...-2026-11-20-19-30"), so only upcoming ones are opened."""
    xml = fetch(src["url"])
    entries = re.findall(r"<url>\s*<loc>([^<]+)</loc>(?:\s*<lastmod>([^<]+)</lastmod>)?", xml)
    dated, undated = [], []
    recent = (start - timedelta(days=120)).strftime("%Y-%m-%d")
    for loc, lastmod in entries:
        m = re.search(r"-(\d{4})-(\d{2})-(\d{2})-\d{2}-\d{2}(?:-\d+)?/?$", loc)
        if m:
            day = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
            if start.strftime("%Y-%m-%d") <= day <= end.strftime("%Y-%m-%d"):
                dated.append((day, loc))
        elif (lastmod or "")[:10] >= recent:
            undated.append(loc)  # one-off pages without a date in the address: check recent ones
    urls = [u for _, u in sorted(dated)] + undated[:15]
    return _read_event_pages(src, urls, limit=src.get("limit", 45))


def src_mecrest(src, start, end):
    """WordPress 'Modern Events Calendar' sites (Acadiana Center for the Arts) list every event
    page through WordPress's public API; each page has the event's details built in."""
    api = src["url"].rstrip("/") + "/wp-json/wp/v2/mec-events?per_page=50&orderby=modified&_fields=link"
    urls = [x["link"] for x in json.loads(fetch(api)) if x.get("link")]
    return _read_event_pages(src, urls, limit=src.get("limit", 45))


# ---------- source: Saffire venue sites (Heymann Center) ----------

def src_saffire(src, start, end):
    base = src["url"].rstrip("/") + "/services/eventsservice.asmx/"
    h = {"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"}
    flags = {"categoryID": 0, "tagID": 0, "keywords": "", "isFeatured": "false", "fanPicks": "false",
             "pastEvents": "false", "allEvents": "false", "memberEvents": "false", "memberOnly": "false",
             "showCategoryExceptionID": 0, "isolatedSchedule": 0, "customFieldFilters": []}
    us = lambda d: f"{d.month}/{d.day}/{d.year}"
    days = json.loads(fetch(base + "GetEventDays", headers=h, data=json.dumps({
        **flags, "day": "", "startDate": us(start), "endDate": us(end), "currentUserItems": "false",
        "myPicks": "false", "searchInDescription": "false"}).encode()))["d"]
    out = []
    for i in range(0, len(days), 10):
        data = json.loads(fetch(base + "GetEventDaysByList", headers=h, data=json.dumps({
            **flags, "dates": ",".join(days[i:i + 10]), "day": "", "searchInDescription": False}).encode()))["d"]
        for day in data.get("Days", []):
            for slot in day.get("Times", []):
                for it in slot.get("Items", []):
                    if it.get("IsPrivate") or it.get("IsMemberOnly"):
                        continue
                    when = f"{day['Year']:04d}-{day['Month']:02d}-{day['Day']:02d}"
                    t = it.get("Time") if it.get("TimeIsSpecified") else None
                    if t is not None:
                        when += f"T{t // 100:02d}:{t % 100:02d}"
                    out.append(make_event(
                        src, title=it.get("Name"), start=when, url=it.get("DetailURL") or src["url"],
                        desc=it.get("ShortDescription") or it.get("LongDescription") or "",
                        image=it.get("ImageOrVideoThumbnailWithPath") or ""))
    return out


# ---------- source: Downtown Lafayette (Webflow event pages) ----------

def src_downtown(src, start, end):
    listing = fetch(src["url"])
    links = sorted(set(re.findall(r'href="(/event/[^"#?]+)"', listing)))[:40]
    out = []
    for path in links:
        url = urllib.parse.urljoin(src["url"], path)
        try:
            page = fetch(url)
        except urllib.error.URLError:
            continue
        title = re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S)
        lines = [l.strip() for l in page_text(page, url).split("\n")]
        lines = [re.sub(r"\s*\[https?://[^\]]+\]\s*", " ", l).strip() for l in lines if l.strip()]
        # the page lists machine-readable values: 2026-11-21 / 16:30 / December 31, 2026 / 23:00
        idx = next((i for i, l in enumerate(lines) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", l)), None)
        if idx is None or not title:
            continue
        start_s, end_s = lines[idx], None
        rest = lines[idx + 1: idx + 4]
        if rest and re.fullmatch(r"\d{1,2}:\d{2}", rest[0]):
            start_s += "T" + rest.pop(0).zfill(5)
        if rest and re.fullmatch(r"[A-Z][a-z]+ \d{1,2}, \d{4}", rest[0]):
            end_d = datetime.strptime(rest.pop(0), "%B %d, %Y").strftime("%Y-%m-%d")
            end_s = end_d + ("T" + rest[0].zfill(5) if rest and re.fullmatch(r"\d{1,2}:\d{2}", rest[0]) else "")
        # Under the title: date and times, then "Venue | Address", the price, the write-up, "Attend"
        name = clean(title.group(1))
        top = max((i for i, l in enumerate(lines[:idx]) if clean(l) == name), default=-1)
        attend = next((i for i in range(top + 1, idx) if lines[i] == "Attend"), idx)
        body = lines[top + 1:attend]
        bar = next((i for i, l in enumerate(body[:14]) if l == "|"), None)
        venue = address = ""
        if bar is not None:
            venue = body[bar - 1] if bar > 0 else ""
            address = body[bar + 1] if bar + 1 < len(body) else ""
            body = body[bar + 2:]
            if re.search(r"\d{1,2}:\d{2}|^\W*$", venue):  # no venue given, only the time
                venue = ""
            if len(address) > 70 or not re.search(r"\d", address):  # that's the write-up, not an address
                body.insert(0, address)
                address = ""
        else:  # no address given: skip the date and time lines
            when = re.compile(r"(?i)^(-|–|[A-Z][a-z]+day, .*\d{4}|[A-Z][a-z]{2}|[A-Z][a-z]{2} \d{1,2}|\d{1,2}:\d{2} ?[ap]m)$")
            while body and when.match(body[0]):
                body.pop(0)
        venue = re.sub(r"\s+[-–]\s+Lafayette$", "", venue)  # "Cavalier House Books - Lafayette"
        price = ""
        if body and re.fullmatch(r"(?i)free|\$[\d.,]+(\s*[-–]\s*\$?[\d.,]+)?|\$?[\d.,]+\s*(\+|and up)?", body[0]):
            price = body.pop(0)
        desc = "\n".join(l for l in body if l and l != "|")
        out.append(make_event(src, title=name, start=start_s, end=end_s, price=price,
                              venue=venue, address=address, url=url, desc=desc))
        time.sleep(0.5)
    return out


# ---------- source: Lafayette Travel's full calendar (event pages listed in its sitemap) ----------

LT_FILE = ROOT / "collector" / "lafayette-travel-cache.json"
LT_CACHE: dict = {}


def _lt_read(url: str) -> dict:
    """One Lafayette Travel event page -> what we need from it. The page's own event data gives
    the name, place, picture and write-up; a script on the page lists every date ("12/7/2026,
    12/8/2026, ...") and the start and end times."""
    page = fetch(url)
    found = jsonld_events(page)
    x = found[0] if found else {}
    var = lambda name: (re.search(r'var %s = "([^"]*)";' % name, page) or [None, ""])[1]
    city = (re.search(r'location\.city = "([^"]*)"', page) or [None, ""])[1]
    dates = []
    for d in var("dates").split(","):
        m = re.fullmatch(r"\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*", d)
        if m:
            dates.append(f"{m[3]}-{int(m[1]):02d}-{int(m[2]):02d}")
    return {"item": x, "dates": dates, "start": var("startDate"), "end": var("endDate"), "city": city}


def src_lafayette_travel(src, start, end):
    """Lafayette Travel lists Lafayette-area events months ahead (festivals, parades, concerts).
    Its sitemap names every event page with the date it last changed, so each run only opens
    pages that are new or changed (2 seconds apart, as the site asks). Weekly regulars (more
    than maxDates dates) are skipped: the venues' own calendars cover those better."""
    if LT_FILE.exists() and not LT_CACHE:
        try:
            LT_CACHE.update(json.loads(LT_FILE.read_text(encoding="utf-8")))
        except ValueError:
            pass
    sitemap = fetch(src["url"])
    pages = re.findall(r"<loc>([^<]*/event/[^<]+)</loc>\s*<lastmod>([^<]*)</lastmod>", sitemap)
    recent = (now_central() - timedelta(days=400)).strftime("%Y-%m-%d")
    pages = sorted(((u, m) for u, m in pages if m[:10] >= recent), key=lambda p: p[1], reverse=True)
    budget = int(os.environ.get("LT_PAGE_BUDGET") or src.get("pagesPerRun", 120))
    out, first, last = [], start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
    for url, mod in pages:
        hit = LT_CACHE.get(url)
        if not hit or hit.get("lastmod") != mod:
            if budget <= 0:
                continue  # read on a later run
            budget -= 1
            try:
                LT_CACHE[url] = {"lastmod": mod, **_lt_read(url)}
            except (urllib.error.URLError, TimeoutError, ValueError):
                continue
            time.sleep(2)
        info = LT_CACHE[url]
        days = [d for d in info["dates"] if first <= d <= last]
        if not days or len(info["dates"]) > src.get("maxDates", 8):
            continue
        if info.get("city") and not in_area(info["city"]):
            continue
        x = info["item"]
        if not x:
            continue
        clock = lambda s: _time_24h(m.group(1)) if (m := re.search(r"(\d{1,2}:\d{2}\s*[AaPp]\.?[Mm]\.?)", s or "")) else None
        t0, t1 = clock(info["start"]), clock(info["end"])  # "Monday, December 7, 2026 5:30 PM" -> "17:30"
        for d in days:
            e = from_jsonld(src, {**x, "url": url, "startDate": f"{d}T{t0}" if t0 else d,
                                  "endDate": f"{d}T{t1}" if t0 and t1 else None})
            if e:
                out.append(e)
    listed = {u for u, _ in pages}
    LT_FILE.write_text(json.dumps({u: v for u, v in LT_CACHE.items() if u in listed},
                                  ensure_ascii=False), encoding="utf-8")
    return out


# ---------- source: Cajundome (one page per event) ----------

_MONTHS = "January February March April May June July August September October November December".split()


def src_cajundome(src, start, end):
    """The events page links to one page per event. Each page prints the dates as text: 'Date
    November 20 - 22, 2026', then one line per show day ('Nov 22Sunday' / '/ 10:00AM'), or a
    single 'Event Starts 6:00PM'."""
    listing = fetch(src["url"])
    links = sorted(set(re.findall(r'href="(https://www\.cajundome\.com/events/detail/[^"#?]+)"', listing)))
    if not links:  # the page didn't load properly (counts as a failure, so last run's events stay)
        raise RuntimeError(f"no event links on the events page ({len(listing)} bytes)")
    skip = re.compile(src.get("skipSlugs") or r"$^", re.I)
    out = []
    for url in links:
        if skip.search(url.rsplit("/", 1)[-1]):
            continue
        try:
            page = fetch(url)
        except urllib.error.URLError:
            continue
        lines = [re.sub(r"\s*\[https?://[^\]]+\]\s*", " ", l).strip() for l in page_text(page, url).split("\n")]
        lines = [l for l in lines if l]
        dl = next((i for i, l in enumerate(lines) if l.startswith("Date ")), None)
        if dl is None:
            continue
        m = re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S) or re.search(r'<meta property="og:title" content="([^"]+)"', page)
        title = clean(m.group(1)) if m else ""
        title = re.sub(r"\s*[|·-]\s*CAJUNDOME.*$", "", title, flags=re.I).strip()
        if not title or title.lower() == "skip to content":
            continue
        year = (re.search(r"(\d{4})", lines[dl]) or [None, str(now_central().year)])[1]
        shows = []
        for i, l in enumerate(lines):
            m = re.fullmatch(r"([A-Z][a-z]{2}) ?(\d{1,2})(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)", l)
            if m and i + 1 < len(lines):
                t = _time_24h(lines[i + 1].lstrip("/ "))
                mon = next(k for k, n in enumerate(_MONTHS, 1) if n.startswith(m[1]))
                shows.append((f"{year}-{mon:02d}-{int(m[2]):02d}", t))
        if not shows:  # single date: "Date November 28, 2026" + "Event Starts" / "6:00PM"
            m = re.search(r"Date ([A-Z][a-z]+) (\d{1,2})(?:\s*-\s*\d{1,2})?, (\d{4})", lines[dl])
            if not m:
                continue
            mon = next(k for k, n in enumerate(_MONTHS, 1) if n == m[1])
            ts = next((lines[i + 1] for i, l in enumerate(lines) if l in ("Event Starts", "Doors Open") and i + 1 < len(lines)), "")
            shows.append((f"{m[3]}-{mon:02d}-{int(m[2]):02d}", _time_24h(ts)))
        try:
            k = lines.index("Event Details")
            desc = "\n".join(lines[k + 1:k + 9])
        except ValueError:
            desc = ""
        for day, t in shows:
            out.append(make_event(src, title=title, start=f"{day}T{t}" if t else day, url=url, desc=desc,
                                  venue=src.get("venue", ""), address=src.get("address", "")))
        time.sleep(0.5)
    return out


# ---------- source: Lafayette Consolidated Government events calendar ----------

def src_lcg(src, start, end):
    """The city-parish calendar lists each event as a link whose address holds its date and
    time: events.lafayettela.gov/default/detail/2026-11-24-1630-City-Parish-Council-Meeting.
    Routine business (src["skip"]) is left out."""
    page = fetch(src["url"])
    skip = re.compile(src.get("skip") or r"$^", re.I)
    out, seen = [], set()
    for m in re.finditer(r'<a[^>]+href="(https://events\.lafayettela\.gov/[^"]*/detail/'
                         r'(\d{4}-\d{2}-\d{2})-(\d{2})(\d{2})-[^"]*)"[^>]*>(.*?)</a>', page, re.S):
        url, day, hh, mm, label = m.groups()
        title = clean(label) or clean(url.rsplit("/", 1)[-1][16:].replace("-", " "))
        if not title or url in seen or skip.search(title):
            continue
        seen.add(url)
        out.append(make_event(src, title=title, start=f"{day}T{hh}:{mm}", url=url))
    if not out and "events.lafayettela.gov" not in page:
        raise Skip("no events found on the page")
    return out


# ---------- source: any web page, read by AI (free GitHub Models) ----------

class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head", "iframe", "template"}
    BLOCK = {"p", "div", "li", "br", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "article", "time"}

    def __init__(self, base):
        super().__init__()
        self.base, self.parts, self.skip = base, [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")
        if tag == "a" and not self.skip:
            href = dict(attrs).get("href") or ""
            if href and not href.startswith(("#", "mailto:", "tel:", "javascript:")):
                self.parts.append(f" [{urllib.parse.urljoin(self.base, href)}] ")
        if tag == "time":
            dt = dict(attrs).get("datetime")
            if dt:
                self.parts.append(f" ({dt}) ")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def page_text(page: str, base: str) -> str:
    p = _TextExtractor(base)
    p.feed(page)
    text = html.unescape("".join(p.parts))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


AI_PROMPT = """You extract upcoming events from a venue's web page for a Lafayette, Louisiana events guide.
Today is {today}. Return JSON: {{"events": [...]}}. Each event:
{{"title": str, "start": "YYYY-MM-DDTHH:MM" or "YYYY-MM-DD" if no time, "end": same format or null,
 "venue": str or null, "price": str or null, "url": str or null, "description": short str or null}}
Rules: only real dated events from {today} through {until}. If a date has no year, use the next
occurrence after today. Times are local. Don't invent anything that isn't on the page. Skip
"hours of operation", menus and closed notices. For repeating events (like "every Wednesday"),
list each date in range. Use the page's link for the event's url when there is one.
The page is from: {name} ({url})"""


def ai_extract(src, text, start, end):
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_MODELS_TOKEN")
    if not token:
        raise Skip("this page needs AI reading, which runs on GitHub (no GITHUB_TOKEN here)")
    body = {
        "model": AI_MODEL,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": AI_PROMPT.format(
                today=start.strftime("%A %Y-%m-%d"), until=end.strftime("%Y-%m-%d"),
                name=src["name"], url=src["url"])},
            {"role": "user", "content": text[:AI_PAGE_CHARS]},
        ],
    }
    for attempt in range(3):
        try:
            raw = fetch(AI_URL, data=json.dumps(body).encode(), timeout=120, headers={
                "Authorization": f"Bearer {token}", "Content-Type": "application/json",
                "Accept": "application/json"})
            break
        except urllib.error.HTTPError as err:
            if err.code == 429 and attempt < 2:  # free tier rate limit: wait and retry
                time.sleep(30 * (attempt + 1))
                continue
            raise
    try:
        content = json.loads(raw)["choices"][0]["message"]["content"] or ""
    except (ValueError, KeyError, IndexError):
        raise RuntimeError(f"unexpected AI response: {raw[:200]!r}")
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip())  # tolerate ```json fences
    try:
        items = json.loads(content).get("events", [])
    except ValueError:
        raise RuntimeError(f"AI reply wasn't JSON: {content[:200]!r}")
    return [make_event(src, title=i.get("title"), start=i.get("start"), end=i.get("end"),
                       venue=i.get("venue") or "", price=i.get("price") or "",
                       url=i.get("url") or "", desc=i.get("description") or "")
            for i in items if isinstance(i, dict)]


def src_page(src, start, end):
    page = fetch(src["url"])
    structured = [from_jsonld(src, x) for x in jsonld_events(page)]
    if any(structured):
        return structured
    text = page_text(page, src["url"])
    if len(text) < 200:
        raise Skip("page has almost no text (it may load events with JavaScript)")
    # Only spend an AI request when the page changed since last time (saves the free daily quota).
    digest = hashlib.sha1(text[:AI_PAGE_CHARS].encode()).hexdigest()[:12]
    PAGE_HASHES[src["name"]] = digest
    if LAST_RUN["hashes"].get(src["name"]) == digest and src["name"] in LAST_RUN["events"]:
        return [e for e in LAST_RUN["events"][src["name"]]]
    events = ai_extract(src, text, start, end)
    time.sleep(5)  # stay under the free tier's requests-per-minute limit
    return events


PAGE_HASHES: dict[str, str] = {}
LAST_RUN: dict = {"hashes": {}, "events": {}}


# ---------- your own events: manual.json and approved submissions ----------

DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def manual_events(start, end):
    if not MANUAL_FILE.exists():
        return []
    out = []
    for m in json.loads(MANUAL_FILE.read_text(encoding="utf-8")).get("events", []):
        s = {"name": m.get("source") or "Bon Temps", "cats": m.get("cats")}
        common = dict(venue=m.get("venue", ""), address=m.get("address", ""), price=m.get("price", ""),
                      url=m.get("url", ""), desc=m.get("desc", ""), image=m.get("image", ""))
        if m.get("from") and m.get("to"):  # every night of a run: "from": "2026-12-01", "to": "2026-12-23"
            d = max(datetime.strptime(m["from"], "%Y-%m-%d"), start.replace(hour=0, minute=0, second=0, microsecond=0))
            last = min(datetime.strptime(m["to"], "%Y-%m-%d"), end)
            days = [DAY_NAMES.index(x[:3].lower()) for x in m.get("days", [])]  # optional: only these weekdays
            while d <= last:
                if not days or d.weekday() in days:
                    ds = d.strftime("%Y-%m-%d")
                    out.append(make_event(s, title=m["title"],
                                          start=f"{ds}T{m['time']}" if m.get("time") else ds,
                                          end=f"{ds}T{m['endTime']}" if m.get("endTime") else None, **common))
                d += timedelta(days=1)
        elif m.get("weekly"):  # e.g. "weekly": "Wed", "time": "19:00", "endTime": "22:00"
            day = DAY_NAMES.index(m["weekly"][:3].lower())
            d = start.replace(hour=0, minute=0, second=0, microsecond=0)
            d += timedelta(days=(day - d.weekday()) % 7)
            while d <= end:
                ds = d.strftime("%Y-%m-%d")
                out.append(make_event(s, title=m["title"],
                                      start=f"{ds}T{m['time']}" if m.get("time") else ds,
                                      end=f"{ds}T{m['endTime']}" if m.get("endTime") else None, **common))
                d += timedelta(days=7)
        else:
            out.append(make_event(s, title=m.get("title"), start=m.get("start"), end=m.get("end"),
                                  all_day=m.get("allDay"), **common))
    return out


def _time_24h(t: str) -> str | None:
    """'8 PM', '8:30pm', '8:00:00 PM', '20:00' or '20:00:00' -> '20:00'."""
    m = re.match(r"\s*(\d{1,2})(?::(\d{2}))?(?::\d{2})?\s*([ap])\.?m?\.?", t or "", re.I)
    if m:
        h = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "p" else 0)
        return f"{h:02d}:{m.group(2) or '00'}"
    m = re.match(r"\s*(\d{1,2}):(\d{2})", t or "")
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else None


def _sheet_date(d: str) -> str | None:
    """'2026-11-21', '11/21/2026' or '11/21' (Google Sheets may reformat dates) -> '2026-11-21'."""
    d = (d or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
        return d
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?", d)
    if not m:
        return None
    year = int(m.group(3)) if m.group(3) else now_central().year
    year += 2000 if year < 100 else 0
    return f"{year:04d}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"


APPROVED = {"yes", "y", "x", "approved", "approve", "ok", "true", "✓", "✔"}


def mark_trending(events: list[dict], config: dict) -> int:
    """Marks events that local listing sites are featuring right now ("hot": true), which the site
    uses to pick its Top picks. Nobody publishes how many people plan to go, so this is the best
    outside sign of what's popular: Happening Next's "Trending Events in Lafayette" and the events
    on Lafayette Travel's home page."""
    hot: list[tuple[set, str | None]] = []  # (title words, date if known)
    for src in config.get("trending", []):
        try:
            page = fetch(src["url"])
        except (urllib.error.URLError, TimeoutError, ValueError) as err:
            print(f"   (trending: couldn't read {src['url']}: {err})")
            continue
        if src["type"] == "jsonld":
            for x in jsonld_events(page):
                hot.append((_words(html.unescape(x.get("name", ""))), (x.get("startDate") or "")[:10] or None))
        else:  # links: "/event/football%3a-ragin-cajuns-v-coastal-carolina/47021/" -> words of the slug
            for slug in set(re.findall(src["pattern"], page)):
                hot.append((_words(urllib.parse.unquote(slug).replace("-", " ")), None))
    for hw, hd in hot:
        if len(hw) < 2:
            continue
        matches = []
        for e in events:
            words = _words(e["title"])
            common = len(words & hw)
            if hd:  # dated (Happening Next): same name, same day or the next
                if abs((datetime.fromisoformat(hd) - datetime.fromisoformat(e["start"][:10])).days) <= 1 \
                        and common >= 2 and common >= 0.6 * min(len(words), len(hw)):
                    matches.append(e)
            elif common >= 2 and common >= 0.75 * len(hw):  # no date: most of its name, best match only
                matches.append((common / len(hw), e["start"], e))
        if hd:
            for e in matches:
                e["hot"] = True
        elif matches:  # the closest name, then the soonest date (the featured one is the next)
            best = max(m[0] for m in matches)
            min((m for m in matches if m[0] == best), key=lambda m: m[1])[2]["hot"] = True
    return sum(1 for e in events if e.get("hot"))


def apply_photos(events: list[dict], config: dict) -> None:
    """sources.json "photos": pictures you add yourself (a flyer someone texted you), saved in
    docs/img/ and attached to every listing whose title matches. They replace any other picture."""
    for p in config.get("topPicks", {}).get("items", []):  # always in Top picks
        pat = re.compile(p["match"], re.I)
        for e in events:
            if pat.search(e["title"]):
                e["pick"] = True
    for p in config.get("photos", {}).get("items", []):
        pat = re.compile(p["match"], re.I)
        url = p["image"] if p["image"].startswith("http") else p["image"].lstrip("/")  # relative: works live and in a local preview
        for e in events:
            if pat.search(e["title"]):
                e["image"] = url
                if p.get("credit"):
                    e["imageCredit"] = p["credit"]
                    e["imageCreditUrl"] = p.get("creditUrl", "")


_PHOTOS: dict[str, str] = {}


def _photo_url(link: str) -> str:
    """A submitted "Photo or flyer" link -> an image address. A direct image link is used as is;
    for a page (a venue's website, an Eventbrite or Facebook event), its preview picture
    (og:image) is used when the page shares one. Instagram hides it, so those come back empty."""
    link = (link or "").strip()
    if not link.startswith(("http://", "https://")):
        return ""
    if re.search(r"\.(?:jpe?g|png|webp|gif|avif)(?:\?|$)", link, re.I) or "googleusercontent.com" in link:
        return link
    if link not in _PHOTOS:
        try:
            page = fetch(link, timeout=20)
            m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', page) or \
                re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', page)
            _PHOTOS[link] = html.unescape(m.group(1)) if m else ""
        except (urllib.error.URLError, TimeoutError, ValueError):
            _PHOTOS[link] = ""
    return _PHOTOS[link]


def src_sheet(src, start, end):
    """Events people submitted through the site's form. Only rows with Approved = yes are used."""
    import csv
    import io
    rows = list(csv.DictReader(io.StringIO(fetch(src["url"]))))
    out = []
    for raw in rows:
        r = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        r = {k: "" if v in ("-", "—", "–") else v for k, v in r.items()}  # the site sends "-" for blanks
        if r.get("approved", "").lower() not in APPROVED:
            continue
        date = _sheet_date(r.get("date", ""))
        if not date:
            continue
        st, et = _time_24h(r.get("start time", "")), _time_24h(r.get("end time", ""))
        cat = r.get("category", "").lower()
        s = {"name": src["name"], "cats": [c for c in CATEGORIES if c in cat]}
        link = r.get("link", "")
        if link and not link.startswith("http"):
            link = "https://" + link
        photo = next((v for k, v in r.items() if "photo" in k or "flyer" in k or "image" in k), "")
        out.append(make_event(
            s, title=r.get("event name"), start=f"{date}T{st}" if st else date,
            end=f"{date}T{et}" if et else None, venue=r.get("venue", ""), address=r.get("address", ""),
            price=r.get("price", ""), url=link, desc=r.get("description", ""), image=_photo_url(photo)))
    return out


# ---------- merge, dedupe, save ----------

def _key(e):
    t = re.sub(r"[^a-z0-9]", "", e["title"].lower())
    if e.get("ongoing") and e.get("end"):  # exhibits: sources disagree on the start, not the closing date
        return f"{t[:32]}|until {e['end'][:10]}"
    return f"{t[:32]}|{e['start'][:10]}"


def _richness(e):
    return sum(bool(e.get(k)) for k in ("end", "venue", "address", "price", "desc", "image")) + \
        (0 if e["allDay"] else 2)


def _same_event(a, b):
    """Same day, one title contains the other ("Sound Bath" / "Sound Bath with Ann Kergan",
    "Supercollider" / "Supercollider! an improv comedy showcase"), and the same start time
    unless one listing only gives the date."""
    if a["start"][:10] != b["start"][:10]:
        return False
    if not (a["allDay"] or b["allDay"]) and a["start"] != b["start"]:
        return False
    ta, tb = (re.sub(r"[^a-z0-9]", "", x["title"].lower()) for x in (a, b))
    short, long_ = sorted((ta, tb), key=len)
    return len(short) >= 6 and short in long_


_FILLER = {"the", "a", "an", "and", "of", "at", "in", "on", "with", "w", "ft", "feat", "featuring", "live",
           "presents", "night", "show", "for", "to", "de", "la", "le", "les", "du", "et", "lafayette"}


def _words(title: str) -> set:
    import unicodedata
    plain = unicodedata.normalize("NFD", title or "").encode("ascii", "ignore").decode().lower()
    return {w for w in re.findall(r"[a-z0-9]+", plain) if w not in _FILLER and len(w) > 1}


def _likely_same(a, b) -> bool:
    """Second look for duplicates the first merge misses, because listing sites word titles
    differently: 'Spirit In The Room + Grave Promise' and 'Spirit in The Room at the Freetown Boom
    Boom Room'. Same day, starts within 30 minutes, and either nearly the same name, or the same
    venue and at least half the name's words in common."""
    if a["start"][:10] != b["start"][:10]:
        return False
    if not (a["allDay"] or b["allDay"]):
        sa, sb = parse_when(a["start"])[0], parse_when(b["start"])[0]
        if abs((sa - sb).total_seconds()) > 1800:
            return False
    import difflib
    ka, kb = title_key(a["title"]), title_key(b["title"])
    if len(ka) >= 8 and difflib.SequenceMatcher(None, ka, kb).ratio() >= 0.85:
        return True
    wa, wb = _words(a["title"]), _words(b["title"])
    if not wa or not wb:
        return False
    overlap = len(wa & wb) / min(len(wa), len(wb))
    same_place = venue_key(a["venue"]) and venue_key(a["venue"]) == venue_key(b["venue"])
    return bool(same_place and overlap >= 0.5 and len(wa & wb) >= 1)


def dedupe_pass(events: list[dict]) -> tuple[list[dict], list[str]]:
    """Merge the pairs _likely_same finds. The venue's or organizer's own listing is kept (a
    listing site's copy only fills in what's missing), and the other site is noted in alsoOn."""
    out, notes = [], []
    by_day: dict[str, list[dict]] = {}
    for e in sorted(events, key=lambda x: (x.get("_yield", False), -_richness(x))):
        day = by_day.setdefault(e["start"][:10], [])
        twin = next((k for k in day if not k.get("ongoing") and not e.get("ongoing") and _likely_same(k, e)), None)
        if not twin:
            day.append(e)
            out.append(e)
            continue
        for f in ("end", "venue", "address", "price", "desc", "image"):
            if not twin.get(f) and e.get(f):
                twin[f] = e[f]
        if len(e.get("desc", "")) > len(twin.get("desc", "")) * 2:
            twin["desc"] = e["desc"]
        also = set(twin.get("alsoOn", [])) | {e["source"]} | set(e.get("alsoOn", []))
        also.discard(twin["source"])
        twin["alsoOn"] = sorted(also)
        notes.append(f"{e['start'][:10]}  “{twin['title'][:45]}” ({twin['source'][:22]})  =  “{e['title'][:45]}” ({e['source'][:22]})")
    return out, notes


def title_key(title: str) -> str:
    """'Noël Acadien au Village' -> 'noelacadienauvillage' (first 30 letters, no accents)."""
    import unicodedata
    t = unicodedata.normalize("NFD", title or "").encode("ascii", "ignore").decode().lower()
    t = re.sub(r"^(?:the|\d+(?:st|nd|rd|th)? annual)\s+", "", t)
    return re.sub(r"[^a-z0-9]", "", t)[:30]


def _close_title(key: str, keys: set) -> bool:
    """Same event name, allowing small spelling differences ('Acadian' vs 'Acadien') and extra
    words on the official listing ('The Nutcracker' / 'The Nutcracker - Matinee')."""
    import difflib
    if len(key) < 8:
        return key in keys
    if key in keys or (len(key) >= 10 and any(k.startswith(key) for k in keys)):
        return True
    return any(abs(len(k) - len(key)) <= 3 and difflib.SequenceMatcher(None, key, k).ratio() >= 0.9
               for k in keys if k[:3] == key[:3])


def merge(events):
    best = {}     # slot key -> the best copy so far
    by_day = {}   # day -> slot keys, for matching near-identical titles
    for e in events:
        k = _key(e)
        day = e["start"][:10]
        if k not in best:
            twin = next((s for s in by_day.get(day, []) if _same_event(best[s], e)), None)
            if twin is None:
                best[k] = e
                by_day.setdefault(day, []).append(k)
                continue
            k = twin  # same event under a slightly different title: merge into its slot
        a = best[k]
        keep, other = (e, a) if _richness(e) > _richness(a) else (a, e)
        for field in ("end", "venue", "address", "price", "desc", "image", "url"):
            if field == "address" and keep.get("venue") and venue_key(keep["venue"]) != venue_key(other.get("venue", "")):
                continue  # don't pin another listing's address on a different venue
            if not keep.get(field) and other.get(field):
                keep[field] = other[field]
        # A guessed category (from a source's fallback) gives way to one that actually matched
        kg, og = keep.get("_guess", False), other.get("_guess", False)
        if kg and not og:
            cats = set(other["cats"])
        elif og and not kg:
            cats = set(keep["cats"])
        elif kg and og:  # both guesses: trust the more specific source (listed earlier in sources.json)
            first = min((keep, other), key=lambda x: x.get("_order", 999))
            cats = set(first["cats"])
        else:
            cats = set(keep["cats"]) | set(other["cats"])
        keep["cats"] = sorted(cats, key=CATEGORY_ORDER.index)[:3]
        keep["_guess"] = kg and og
        keep["_order"] = min(keep.get("_order", 999), other.get("_order", 999))
        also = set(keep.get("alsoOn", [])) | {other["source"]} | set(other.get("alsoOn", []))
        also.discard(keep["source"])
        keep["alsoOn"] = sorted(also)
        best[k] = keep
    return list(best.values())


# ---------- venue names for listings that only give an address ----------

ADDRESS_LIKE = re.compile(r"^\s*\d+[A-Za-z]?\s+\S")
_DIRECTIONS = {"e", "w", "n", "s", "east", "west", "north", "south", "e.", "w.", "n.", "s."}


def street_key(text: str) -> str:
    """'215 E Convent St, Lafayette' -> '215 convent'"""
    words = re.sub(r"[^a-z0-9 ]", " ", (text or "").lower()).split()
    if len(words) < 2 or not words[0].isdigit():
        return ""
    rest = [w for w in words[1:] if w not in _DIRECTIONS and not w.isdigit()]  # "2801-2851 Johnston"
    return f"{words[0]} {rest[0]}" if rest else ""


def resolve_venues(events, known: dict):
    """Swap 'venue = street address' for the venue's name, using (1) your list in sources.json,
    (2) what other listings say lives at that address, (3) a '📍 Name' line in the description."""
    names = {street_key(k): v for k, v in known.items() if street_key(k)}
    for e in events:  # learn from listings that give both a name and an address
        if e["venue"] and not ADDRESS_LIKE.match(e["venue"]):
            k = street_key(e.get("address", ""))
            if k and k not in names:
                names[k] = e["venue"]
    fixed = 0
    for e in events:
        if not ADDRESS_LIKE.match(e["venue"] or ""):
            continue
        k = street_key(e["venue"]) or street_key(e.get("address", ""))
        name = names.get(k)
        if not name:
            pin = re.search(r"📍\s*([^\n|]{3,60})", e.get("desc", ""))
            if pin and not ADDRESS_LIKE.match(pin.group(1)):
                name = pin.group(1).strip(" -–,.")
        if name:
            if not e.get("address"):
                e["address"] = e["venue"]
            e["venue"] = name
            fixed += 1
    return fixed


# ---------- quality check for events only an aggregator lists ----------

def keep_listing(e, cfg, known_venues: set) -> bool:
    """Events that only an aggregator (Happening Next) lists must be at a known venue, be in a
    'going out' category, or be a recognizable public event. Medicare seminars and realty
    parties at an office address don't make it."""
    agg = cfg.get("aggregators", [])
    sources = [e["source"], *e.get("alsoOn", [])]
    if not all(any(s.startswith(a) for a in agg) for s in sources):
        return True  # a venue's own calendar lists it
    if set(e["cats"]) & set(cfg.get("keepCats", [])):
        return True
    if re.search(cfg.get("keepWords", "$^"), e["title"], re.I):
        return True
    return venue_key(e["venue"]) in known_venues


def venue_key(v: str) -> str:
    v = re.split(r" - | \| |,| @ |\[", (v or "").lower())[0]
    v = re.sub(r"\b(?:the|and|guest|house|de|la|lafayette|center|centre|for|arts|performing|of|at)\b", " ", v)
    return re.sub(r"[^a-z0-9]", "", v)


RUNNERS = {"tribe": src_tribe, "jsonld": src_jsonld, "ical": src_ical, "saffire": src_saffire,
           "squarespace": src_squarespace, "downtown": src_downtown, "lcg": src_lcg,
           "lafayettetravel": src_lafayette_travel, "cajundome": src_cajundome, "sheet": src_sheet, "eventlinks": src_eventlinks,
           "wixsitemap": src_wixsitemap, "mecrest": src_mecrest,
           "ticketmaster": src_ticketmaster, "page": src_page}


def quick_submissions() -> None:
    """`collect.py --submissions`: only re-read the "Submit an event" sheet and update the approved
    events on the site, without checking any other source. Runs every 10 minutes, so an event you
    approve shows up within about 15 minutes instead of waiting for the next full update."""
    config = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))
    src = next(s for s in config["sources"] if s["type"] == "sheet" and s.get("enabled", True))
    data = json.loads(OUT_FILE.read_text(encoding="utf-8"))
    start = now_central().replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=DAYS_AHEAD)
    fresh = [e for e in src_sheet(src, start, end) if e and (e["end"] or e["start"]) >= iso(start) and e["start"] <= iso(end)]
    for e in fresh:
        e["address"] = short_address(e.get("address", ""))
    apply_photos(fresh, config)

    sig = lambda e: (e["title"], e["start"], e.get("end"), e["venue"], e.get("address"), e.get("price"), e.get("desc"), e.get("url"), e.get("image"), tuple(e.get("cats") or []))
    had = sorted(sig(e) for e in data["events"] if e["source"] == src["name"])
    if had == sorted(sig(e) for e in fresh) and not any(src["name"] in (e.get("alsoOn") or []) for e in data["events"]):
        print("submissions: no changes")
        return

    # Take out the old submitted listings, then add the approved ones (or note them on a matching event)
    events = [e for e in data["events"] if e["source"] != src["name"]]
    for e in events:
        if src["name"] in (e.get("alsoOn") or []):
            e["alsoOn"] = [x for x in e["alsoOn"] if x != src["name"]]
    added = 0
    for e in fresh:
        twin = next((x for x in events if _same_event(x, e) or _likely_same(x, e)), None)
        if twin:
            twin["alsoOn"] = sorted(set(twin.get("alsoOn") or []) | {src["name"]})
            continue
        for k in ("_guess", "_order", "_yield"):
            e.pop(k, None)
        e["id"] = hashlib.sha1(_key(e).encode()).hexdigest()[:10]
        events.append(e)
        added += 1
    events.sort(key=lambda e: (e["start"], e["title"]))
    data["events"] = events
    OUT_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=0), encoding="utf-8")
    write_calendar_files(events)
    write_event_pages(events)
    write_day_pages(events)
    print(f"submissions: {len(fresh)} approved ({added} new on the site, {len(fresh) - added} matched an event already listed)")


def main(argv):
    if len(argv) > 1 and argv[1] == "--submissions":
        return quick_submissions()
    only = argv[1].lower() if len(argv) > 1 else ""
    # keep everything from today (since midnight), so today's earlier events stay listed
    start = now_central().replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=DAYS_AHEAD)
    cutoff = iso(start)

    previous = {}
    if OUT_FILE.exists():
        try:
            prev = json.loads(OUT_FILE.read_text(encoding="utf-8"))
            for e in prev.get("events", []):
                for name in [e["source"], *e.get("alsoOn", [])]:
                    previous.setdefault(name, []).append({**e, "source": name, "alsoOn": []})
            LAST_RUN["hashes"] = {s["name"]: s["hash"] for s in prev.get("sources", []) if s.get("hash")}
            LAST_RUN["events"] = previous
        except ValueError:
            pass

    if DETAIL_FILE.exists():
        try:
            DETAILS.update(json.loads(DETAIL_FILE.read_text(encoding="utf-8")))
        except ValueError:
            pass

    config = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))
    sources = [s for s in config["sources"] if s.get("enabled", True) and only in s["name"].lower()]
    if not only:
        sources += [{"name": "Bon Temps", "type": "_manual"}]

    all_events, report = [], []
    for order, src in enumerate(sources):
        t0 = time.time()
        runner = {"_manual": lambda s, a, b: manual_events(a, b)}.get(src["type"]) or RUNNERS[src["type"]]
        try:
            found = [e for e in runner(src, start, end) if e]
            if src.get("skip"):  # titles this source gets wrong or that are routine business
                skip = re.compile(src["skip"], re.I)
                found = [e for e in found if not skip.search(e["title"])]
            for pat, rep in src.get("renameTitle", []):  # "Shotgun Lillie | 10-30-26" -> "Shotgun Lillie"
                for e in found:
                    e["title"] = re.sub(pat, rep, e["title"]).strip()
            if src.get("require"):  # only these kinds of events from this source (big ones)
                need = re.compile(src["require"], re.I)
                found = [e for e in found if need.search(e["title"])]
            if src.get("requireAddress"):  # sites with more than one location (Rock'n'Bowl New Orleans)
                here = re.compile(src["requireAddress"], re.I)
                found = [e for e in found if here.search(f"{e['venue']} {e['address']}")]
            if src.get("maxRepeats"):  # programs that repeat all month (storytime at four branches)
                count: dict[str, int] = {}
                for e in found:
                    count[title_key(e["title"])] = count.get(title_key(e["title"]), 0) + 1
                found = [e for e in found if count[title_key(e["title"])] <= src["maxRepeats"]]
            for e in found:
                e["_order"] = order
                e["_yield"] = bool(src.get("yield"))
            found = [e for e in found if (e["end"] or e["start"]) >= cutoff and e["start"] <= iso(end)]
            if src.get("maxDays"):  # open-ended "weekly trivia since 2022" style listings
                limit = timedelta(days=src["maxDays"])
                found = [e for e in found if not e["end"] or parse_when(e["end"])[0] - parse_when(e["start"])[0] <= limit]
            status = {"name": src["name"], "ok": True, "count": len(found)}
        except Skip as why:
            found, status = [], {"name": src["name"], "ok": None, "count": 0, "note": str(why)}
        except Exception as err:  # keep last run's events if a site is down
            kept = [e for e in previous.get(src["name"], []) if (e.get("end") or e["start"]) >= cutoff]
            found = kept
            status = {"name": src["name"], "ok": False, "count": len(kept),
                      "note": f"{type(err).__name__}: {str(err)[:140]}" + (" (showing last good results)" if kept else "")}
        if status["ok"] and src["name"] in PAGE_HASHES:
            status["hash"] = PAGE_HASHES[src["name"]]
        all_events += found
        report.append(status)
        mark = {True: "ok  ", None: "skip", False: "FAIL"}[status["ok"]]
        print(f"{mark} {src['name']:<42} {status['count']:>4} events  {time.time() - t0:5.1f}s  {status.get('note', '')}")

    # Remember event pages read this run; a full run also forgets pages no longer listed
    keep = DETAILS if only else {u: v for u, v in DETAILS.items() if u in DETAILS_USED}
    DETAIL_FILE.write_text(json.dumps(keep, ensure_ascii=False), encoding="utf-8")

    if only:  # partial runs don't overwrite the site's data
        print(f"\n(test run for '{only}': docs/events.json not changed)")
        return

    fixed = resolve_venues(all_events, config.get("venueNames", {}))
    print(f"named {fixed} venues that were only an address")

    # Leave out clutter ("Moncus Mondays-Free Parking", listings that are just a venue's name)
    hide = config.get("hide", {}).get("patterns", [])
    if hide:
        hidden = re.compile("|".join(f"(?:{p})" for p in hide), re.I)
        before = len(all_events)
        all_events = [e for e in all_events if not hidden.search(e["title"].strip())]
        print(f"hid {before - len(all_events)} clutter listings")

    # Listing sites marked "yield" step aside when the venue or organizer lists the same event,
    # even on different dates (Lafayette Travel had Noël Acadien a week off)
    official = {title_key(e["title"]) for e in all_events if not e.get("_yield")}
    yielded = [e for e in all_events if e.get("_yield") and _close_title(title_key(e["title"]), official)]
    if yielded:
        drop = {id(e) for e in yielded}
        all_events = [e for e in all_events if id(e) not in drop]
        print(f"{len(yielded)} listing-site copies gave way to the official listing: "
              + ", ".join(sorted({e['title'][:40] for e in yielded})[:12]))

    events = merge(all_events)
    events, twins = dedupe_pass(events)
    print(f"merged {len(twins)} more duplicates that listing sites worded differently")
    for t in twins:
        print("   = " + t)

    # Venues on your address list always show that address (listing sites sometimes get it wrong)
    canon = {venue_key(name): f"{street}, Lafayette, LA" for street, name in config.get("venueNames", {}).items()}
    for e in events:
        right = canon.get(venue_key(e["venue"]))
        if right and street_key(e.get("address", "")) != street_key(right):
            e["address"] = right

    quality = config.get("quality")
    if quality:
        agg = tuple(quality.get("aggregators", []))
        known = {venue_key(e["venue"]) for e in events if not e["source"].startswith(agg)}
        known |= {venue_key(v) for v in config.get("venueNames", {}).values()}
        known |= {venue_key(v) for v in quality.get("knownVenues", [])}
        known.discard("")
        dropped = [e for e in events if not keep_listing(e, quality, known)]
        events = [e for e in events if keep_listing(e, quality, known)]
        print(f"left out {len(dropped)} low-quality aggregator listings")
        for e in dropped:  # shown in the Actions log, to spot anything worth adding back
            print(f"   - {e['start'][:10]} {e['title'][:60]} @ {e['venue'][:40]}")

    # Clutter check: what the listing sites marked "yield" add that nobody else lists
    for src in config["sources"]:
        if src.get("yield") and src.get("enabled", True):
            only_here = [e for e in events if e["source"] == src["name"] and not e.get("alsoOn")]
            shared = [e for e in events if src["name"] in (e.get("alsoOn") or []) or (e["source"] == src["name"] and e.get("alsoOn"))]
            print(f"{src['name']}: {len(only_here)} events only it lists, {len(shared)} also listed elsewhere")
            for e in only_here:
                print(f"   + {e['start'][:16]} {e['title'][:55]} @ {e['venue'][:35]} {e['cats']}")
            if len(only_here) > src.get("warnAbove", 40):
                print(f"   ! that's more than {src.get('warnAbove', 40)}: check for clutter")
    # Short, readable addresses: "301 W Congress St, Lafayette" (no state, ZIP or country)
    for e in events:
        e["address"] = short_address(e.get("address", ""))
        if re.match(r"\d", e["venue"] or ""):  # venue that is only an address
            e["venue"] = short_address(e["venue"])
            if street_key(e["venue"]) and street_key(e["venue"]) == street_key(e["address"]):
                e["address"] = ""  # don't show the same address twice

    if not only and config.get("bigEvents"):
        missing = check_big_events(events, config["bigEvents"])
        print(f"big yearly events check: {len(missing)} missing")
        report_big_events(missing)
    if not only:
        check_sources(report)
    apply_photos(events, config)
    print(f"trending: {mark_trending(events, config)} events featured by local listing sites")

    events.sort(key=lambda e: (e["start"], e["title"]))
    for e in events:
        e["id"] = hashlib.sha1(_key(e).encode()).hexdigest()[:10]
        e.pop("_guess", None)
        e.pop("_order", None)
        e.pop("_yield", None)
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps({
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sources": report,
        "events": events,
    }, ensure_ascii=False, indent=0), encoding="utf-8")
    write_calendar_files(events)
    write_event_pages(events)
    write_day_pages(events)
    print(f"\nwrote {len(events)} events to {OUT_FILE.relative_to(ROOT)} (and a calendar file for each)")


def short_address(a: str) -> str:
    """'301 W Congress St, Lafayette, LA 70501-6823, United States, Lafayette' -> '301 W Congress St, Lafayette'."""
    a = re.sub(r",?\s*(United States|USA|US)\b\.?", "", a or "")
    a = re.sub(r"(?:\b(?:LA|Louisiana)\b\.?|\bLa\.(?=\W|$))\s*(?:\d{5}(?:-\d{4})?)?", "", a)
    a = re.sub(r"\b\d{5}(?:-\d{4})?\b", "", a)
    parts = [p.strip(" .") for p in a.split(",")]
    out = []
    for p in parts:
        if p and p.lower() not in (x.lower() for x in out):  # "Lafayette, Lafayette"
            out.append(p)
    return re.sub(r"\s{2,}", " ", ", ".join(out)).strip(" ,")


def easter(year: int):
    """Western Easter (anonymous Gregorian algorithm); Mardi Gras is 47 days before."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return datetime(year, month, day + 1)


def check_big_events(events: list[dict], big: list[dict]) -> list[str]:
    """Big yearly events that should be on the site by now but aren't. Each entry in
    sources.json 'bigEvents' says roughly when it usually starts ("04-22", or "mardigras-14" for
    14 days before Mardi Gras); within 'leadDays' of that date, at least one listing should match."""
    today = now_central().replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    titles = [title_key(e["title"]) + " " + e["title"].lower() for e in events]
    missing = []
    for b in big:
        for year in (today.year, today.year + 1):
            when = b["usually"]
            if when.startswith("mardigras"):
                usual = easter(year) - timedelta(days=47 + int(when.split("-")[1] if "-" in when else 0))
            else:
                usual = datetime(year, *map(int, when.split("-")))
            days = (usual - today).days
            if 0 <= days <= b.get("leadDays", 45):
                if not any(re.search(b["match"], t, re.I) for t in titles):
                    missing.append(f"**{b['name']}** usually starts around {usual:%b} {usual.day} ({days} days away), "
                                   f"but nothing on the site matches it yet. {b.get('tip', '')}".rstrip())
                break
    return missing


def report_big_events(missing: list[str]) -> None:
    keep_issue("Big events missing dates", missing,
               "These big yearly events usually start soon, but no source lists them yet. Add this year's "
               "dates to `collector/manual.json` (see the Noël Acadien entry for a nightly run), or close "
               "this if it isn't happening.")


# ---------- alert when a source stops working ----------

HEALTH_FILE = ROOT / "collector" / "health.json"
BROKEN_AFTER = timedelta(hours=24)  # a day of failures, not one bad run


def check_sources(report: list[dict]) -> None:
    """Remembers since when each source has been failing (an error, or no events when it usually
    has some) in collector/health.json. Sources down for more than a day go into one GitHub issue,
    "Event sources not working" (GitHub emails you), which closes itself once they all work again."""
    try:
        health = json.loads(HEALTH_FILE.read_text(encoding="utf-8")) if HEALTH_FILE.exists() else {}
    except ValueError:
        health = {}
    now = datetime.now(timezone.utc).replace(microsecond=0)
    broken = []
    for s in report:
        if s["ok"] is None:  # turned off on purpose (no Ticketmaster key, nothing to read)
            continue
        h = health.setdefault(s["name"], {})
        failing = s["ok"] is False or (s["count"] == 0 and h.get("usual", 0) > 0)
        if not failing:
            if s["count"]:
                h["usual"] = s["count"]
            h.pop("since", None)
            h.pop("why", None)
            continue
        h.setdefault("since", now.isoformat())
        h["why"] = s.get("note") or "found no events (it usually has some)"
        down = now - datetime.fromisoformat(h["since"])
        if down >= BROKEN_AFTER:
            broken.append(f"**{s['name']}**: not working for {down.days or 1} day{'s' if down.days > 1 else ''}. {h['why']}")
    names = {s["name"] for s in report}
    health = {k: v for k, v in health.items() if k in names}
    HEALTH_FILE.write_text(json.dumps(health, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"source check: {len(broken)} not working for over a day")
    keep_issue("Event sources not working", broken,
               "These sources have failed for more than a day, so their events aren't updating (the site "
               "keeps showing their last good events for now). Usually the site changed its layout or "
               "moved its calendar. Ask Claude to take a look, or turn the source off in "
               "`collector/sources.json`. This closes by itself once they work again.")


def keep_issue(title: str, items: list[str], intro: str) -> None:
    """Prints the list, adds it to the Actions run summary, and keeps one GitHub issue with this
    title up to date (GitHub emails you when it's opened). It closes itself when the list is empty."""
    for m in items:
        print("   ! " + re.sub(r"\*\*", "", m))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary and items:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(f"### {title}\n\n" + "\n".join(f"- {m}" for m in items) + "\n")
    missing = items
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not (token and repo):
        return

    def api(method, path, body=None):
        req = urllib.request.Request(f"https://api.github.com/repos/{repo}{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT,
                                              "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or b"null")

    try:
        issue = next((i for i in api("GET", "/issues?state=open&per_page=100")
                      if i["title"] == title and "pull_request" not in i), None)
        body = intro + "\n\n" + "\n".join(f"- {m}" for m in missing)
        if missing and not issue:
            api("POST", "/issues", {"title": title, "body": body})
        elif missing and issue["body"] != body:
            api("PATCH", f"/issues/{issue['number']}", {"body": body})
        elif not missing and issue:
            api("PATCH", f"/issues/{issue['number']}", {"state": "closed"})
    except (urllib.error.URLError, TimeoutError, ValueError) as err:
        print(f"   (couldn't update the GitHub issue: {err})")


PAGE_DIR = ROOT / "docs" / "e"
SITE_URL = "https://brody-robicheaux.github.io/bon-temps/"


def _when_text(e: dict) -> str:
    """'Sat, Nov 21 · 8 PM', 'Sat, Nov 21 · All day' or 'Thru Dec 23' for exhibits."""
    s = parse_when(e["start"])[0]
    en = parse_when(e["end"])[0] if e.get("end") else None
    day = f"{s:%a}, {s:%b} {s.day}"
    if e.get("ongoing") and en:
        return f"{day} – {en:%b} {en.day}"
    if e.get("allDay"):
        return f"{day} · All day"
    t = f"{s.hour % 12 or 12}" + (f":{s:%M}" if s.minute else "") + (" AM" if s.hour < 12 else " PM")
    return f"{day} · {t}"


def write_event_pages(events: list[dict]) -> None:
    """docs/e/<id>.html for every event, so a shared event link shows that event in the preview
    (Messages, Facebook...): those apps read a page's preview tags but ignore the #part of a link,
    so '/bon-temps/#id' always previewed as the home page. Each page keeps the regular poster
    picture, puts what/when/where in the title, and sends visitors straight to the event."""
    PAGE_DIR.mkdir(parents=True, exist_ok=True)
    keep = {f"{e['id']}.html" for e in events}
    for old in PAGE_DIR.glob("*.html"):
        if old.name not in keep:
            old.unlink()
    for e in events:
        title = " · ".join(x for x in (e["title"], _when_text(e), e.get("venue")) if x)
        blurb = re.sub(r"\s+", " ", e.get("desc") or "").strip()
        if len(blurb) > 180:
            blurb = blurb[:180].rsplit(" ", 1)[0].rstrip(",.;:") + "…"
        blurb = blurb or "Live music, theater, food, arts, sports and festivals around Lafayette, LA."
        url, target = f"{SITE_URL}e/{e['id']}", f"../#{e['id']}"
        h = lambda x: html.escape(x, quote=True)
        text = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{h(title)}</title>
<meta name="description" content="{h(blurb)}">
<meta property="og:type" content="website">
<meta property="og:site_name" content="Bon Temps">
<meta property="og:title" content="{h(title)}">
<meta property="og:description" content="{h(blurb)}">
<meta property="og:url" content="{h(url)}">
<meta property="og:image" content="{SITE_URL}og-poster.png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta name="twitter:card" content="summary_large_image">
<link rel="canonical" href="{h(url)}">
<meta http-equiv="refresh" content="0; url={h(target)}">
<script>location.replace({json.dumps(target)});</script>
</head><body><p><a href="{h(target)}">{h(title)}</a> on Bon Temps</p></body></html>
"""
        path = PAGE_DIR / f"{e['id']}.html"
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8", newline="\n")


DAY_DIR = ROOT / "docs" / "d"
DAY_PAGES = 14  # the date strip's two weeks


def write_day_pages(events: list[dict]) -> None:
    """docs/d/<date>.html for each day on the date strip, so "Share this list" on a day previews as
    'Saturday, Nov 21 in Lafayette · 42 events' (one summary line, never a list of names), then
    opens that day with the same filters."""
    DAY_DIR.mkdir(parents=True, exist_ok=True)
    today = now_central().date()
    days = [today + timedelta(days=i) for i in range(DAY_PAGES)]
    keep = {f"{d:%Y-%m-%d}.html" for d in days}
    for old in DAY_DIR.glob("*.html"):
        if old.name not in keep:
            old.unlink()
    for d in days:
        iso_day = f"{d:%Y-%m-%d}"
        n = sum(1 for e in events if not e.get("ongoing") and e["start"][:10] <= iso_day <= (e.get("end") or e["start"])[:10])
        when = "today" if d == today else "tomorrow" if d == today + timedelta(days=1) else f"on {d:%A}"
        title = f"{d:%A}, {d:%b} {d.day} in Lafayette · {n} event{'s' if n != 1 else ''}"
        blurb = f"Live music, theater, food, arts, sports and more around Lafayette {when}."
        url, target = f"{SITE_URL}d/{iso_day}", f"../?when={iso_day}"
        h = lambda x: html.escape(x, quote=True)
        text = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{h(title)}</title>
<meta name="description" content="{h(blurb)}">
<meta property="og:type" content="website">
<meta property="og:site_name" content="Bon Temps">
<meta property="og:title" content="{h(title)}">
<meta property="og:description" content="{h(blurb)}">
<meta property="og:url" content="{h(url)}">
<meta property="og:image" content="{SITE_URL}og-poster.png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta name="twitter:card" content="summary_large_image">
<meta http-equiv="refresh" content="0; url={h(target)}">
<script>location.replace({json.dumps(target)} + location.search.replace(/^\\?/, "&"));</script>
</head><body><p><a href="{h(target)}">{h(title)}</a> on Bon Temps</p></body></html>
"""
        path = DAY_DIR / f"{iso_day}.html"
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8", newline="\n")


CAL_DIR = ROOT / "docs" / "cal"
VTIMEZONE = """BEGIN:VTIMEZONE
TZID:America/Chicago
BEGIN:DAYLIGHT
TZOFFSETFROM:-0600
TZOFFSETTO:-0500
TZNAME:CDT
DTSTART:19700308T020000
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU
END:DAYLIGHT
BEGIN:STANDARD
TZOFFSETFROM:-0500
TZOFFSETTO:-0600
TZNAME:CST
DTSTART:19701101T020000
RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU
END:STANDARD
END:VTIMEZONE"""


def _ics_text(s) -> str:
    s = str(s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
    return s.replace("\r", "").replace("\n", "\\n")


def _ics_fold(line: str) -> str:
    """iCal lines are at most 75 bytes; longer ones continue on the next line after a space."""
    out, cur = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(cur) + len(b) > (75 if not out else 74):
            out.append(cur.decode("utf-8"))
            cur = b""
        cur += b
    out.append(cur.decode("utf-8"))
    return "\r\n ".join(out)


def write_calendar_files(events: list[dict]) -> None:
    """docs/cal/<id>.ics for every event: the site's "Apple Calendar" button opens these.
    Phones (iPhones especially) only offer "Add to Calendar" for a real .ics web address."""
    CAL_DIR.mkdir(parents=True, exist_ok=True)
    keep = {f"{e['id']}.ics" for e in events}
    for old in CAL_DIR.glob("*.ics"):
        if old.name not in keep:
            old.unlink()
    stamp = "20260101T000000Z"  # fixed, so unchanged events don't produce new files every run
    for e in events:
        s = parse_when(e["start"])[0]
        en = parse_when(e["end"])[0] if e.get("end") else None
        if e.get("allDay"):
            last = en.date() if en else s.date()
            when = [f"DTSTART;VALUE=DATE:{s:%Y%m%d}",
                    f"DTEND;VALUE=DATE:{max(last, s.date()) + timedelta(days=1):%Y%m%d}"]
            alarm = []
        else:
            en = en if en and en > s else s + timedelta(hours=2)
            when = [f"DTSTART;TZID=America/Chicago:{s:%Y%m%dT%H%M%S}",
                    f"DTEND;TZID=America/Chicago:{en:%Y%m%dT%H%M%S}"]
            alarm = ["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{_ics_text(e['title'])}",
                     "TRIGGER:-PT1H", "END:VALARM"]  # reminder an hour before
        place = ", ".join(x for x in (e.get("venue"), e.get("address")) if x)
        about = "\n\n".join(x for x in (e.get("desc"), e.get("url")) if x)
        lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Bon Temps//Lafayette events//EN",
                 "CALSCALE:GREGORIAN", "METHOD:PUBLISH", *VTIMEZONE.split("\n"),
                 "BEGIN:VEVENT", f"UID:{e['id']}@bontemps", f"DTSTAMP:{stamp}", *when,
                 f"SUMMARY:{_ics_text(e['title'])}", f"LOCATION:{_ics_text(place)}",
                 f"DESCRIPTION:{_ics_text(about)}",
                 *([f"URL:{e['url']}"] if e.get("url") else []), *alarm, "END:VEVENT", "END:VCALENDAR"]
        text = "\r\n".join(_ics_fold(l) for l in lines) + "\r\n"
        path = CAL_DIR / f"{e['id']}.ics"
        if not path.exists() or path.read_bytes() != text.encode("utf-8"):
            path.write_bytes(text.encode("utf-8"))


if __name__ == "__main__":
    main(sys.argv)
