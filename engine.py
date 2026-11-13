"""
engine.py  —  Hassan Paracha SEO Rolling Cycle  (optimised, test-compatible)
=============================================================================
All public names expected by tests/test_engine.py are preserved exactly.
Improvements over original:
  - Duplicate-title deduplication in Gemini prompts
  - yt-dlp cookie auth via YT_COOKIES_B64 secret
  - Richer per-video pages with related links, tags, embedded player
  - 15 topic categories
  - Internal cross-linking on every /v/ page
  - Exponential back-off on Gemini 429
  - HTML escaping on all untrusted content
  - Proper sitemap + robots.txt
"""

import html as _html_mod
import json
import math
import os
import random
import re
import tempfile
import base64
import time
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote_plus
import urllib.request

import yt_dlp
from google import genai

# ── CONFIGURATION ─────────────────────────────────────────────────────────────
CHANNEL_HANDLE     = "@hassanparachaoffical"
DB_FILE            = "video_db.json"
SITE_DIR           = "site"
SITE_BASE_URL      = os.getenv("SITE_BASE_URL",
                       "https://hassan-paracha.github.io/my-channel-seo/")
REFRESH_PER_DAY    = int(os.getenv("REFRESH_PER_DAY", "150"))
SYNC_SLICE_SIZE    = int(os.getenv("SYNC_SLICE_SIZE", "1000"))
SYNC_TIME_BUDGET_MIN = int(os.getenv("SYNC_TIME_BUDGET_MIN", "5"))
BATCH_SIZE         = int(os.getenv("BATCH_SIZE", "40"))
DRY_RUN            = os.getenv("DRY_RUN", "0") == "1"
VIDEOS_PER_PAGE    = 48
PROMPT_VERSION     = 3   # bump when prompt logic changes to force re-polish

TRENDING_KEYWORDS_FALLBACK = [
    "viral shorts", "trending pakistan", "funny video", "vlog",
    "hindi", "urdu", "desi", "reels", "youtube shorts 2026",
]

TRENDING_TOPICS = ["bike start", "funny", "family", "vlog", "comedy"]

# ── TOPIC TAXONOMY ─────────────────────────────────────────────────────────────
TOPICS = {
    "bike-start":        ["motorcycle", "bike start", "khrab", "start nahi"],
    "celebrations":      ["celebration", "mubarak", "independence", "eid", "birthday"],
    "family-funny":      ["family", "bacho", "kids", "ghar", "bachon"],
    "park-vlogs":        ["park", "mela", "outing", "bahar"],
    "jungle-adventure":  ["jungle", "forest", "bhoot", "adventure"],
    "desi-comedy":       ["funny", "comedy", "masti", "mazak", "prank"],
    "street-food":       ["food", "khana", "chai", "desi food"],
    "travel-vlogs":      ["travel", "safar", "road trip", "tour"],
    "emotional":         ["emotional", "dil", "yaad", "motivational"],
    "reaction":          ["reaction", "dekhiye", "shocking", "amazing"],
    "daily-vlog":        ["vlog", "routine", "aaj ka din", "daily"],
    "challenges":        ["challenge", "try", "attempt", "koshish"],
    "unboxing":          ["unbox", "new", "product", "review"],
    "shorts-compilation":["shorts", "viral", "trending", "reels"],
    "special-events":    ["wedding", "shadi", "party", "event"],
}

# ── HELPERS ───────────────────────────────────────────────────────────────────

def load_db() -> dict:
    if os.path.exists(DB_FILE):
        with open(DB_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_db(db: dict):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2, ensure_ascii=False)


def migrate_database(db: dict) -> dict:
    """Migrate legacy DB records to current schema. Returns updated db."""
    for vid, rec in db.items():
        # ensure status field
        if "status" not in rec:
            rec["status"] = "active"
        # ensure missing_count
        if "missing_count" not in rec:
            rec["missing_count"] = 0
        # ensure original_title
        if "original_title" not in rec:
            rec["original_title"] = rec.get("title", "")
        # ensure seo sub-object
        if "seo" not in rec and rec.get("polished"):
            rec["seo"] = {
                "title":          rec.get("title", ""),
                "description":    rec.get("description", ""),
                "keywords":       rec.get("tags", []),
                "prompt_version": 0,
                "updated_at":     rec.get("polished_at") or rec.get("last_refreshed", ""),
            }
    return db


def _sync_database(db: dict, listings: list) -> bool:
    """
    Merge channel listing into db.
    Returns True if any record changed.
    """
    changed = False
    for entry in listings:
        vid = entry.get("id")
        if not vid:
            continue
        if vid not in db:
            db[vid] = {
                "original_title": entry.get("title", ""),
                "status":         "active",
                "missing_count":  0,
                "last_synced":    None,
                "published_at":   entry.get("published_at"),
                "description":    entry.get("description", ""),
                "tags":           entry.get("tags", []),
                "duration_seconds": entry.get("duration_seconds", 0),
            }
            changed = True
        else:
            # if YouTube title changed, reset polish
            new_title = entry.get("title", "")
            if new_title and new_title != db[vid].get("original_title", ""):
                db[vid]["original_title"] = new_title
                db[vid].pop("seo", None)
                changed = True
    return changed


def _apply_listing_sync(db: dict, listings: list) -> bool:
    """
    Track videos missing from listing.
    Mark removed after 3 consecutive misses when listing >= 90% of db.
    Returns True if any db record changed.
    """
    listed_ids = {e["id"] for e in listings if e.get("id")}
    active_total = sum(1 for r in db.values() if r.get("status") == "active")

    if active_total == 0 or len(listed_ids) < 0.9 * active_total:
        return False

    changed = False
    for vid, rec in db.items():
        if rec.get("status") != "active":
            continue
        if vid not in listed_ids:
            rec["missing_count"] = rec.get("missing_count", 0) + 1
            if rec["missing_count"] >= 3:
                rec["status"] = "removed"
            changed = True
        else:
            if rec.get("missing_count", 0) > 0:
                rec["missing_count"] = 0
                changed = True
    return changed


def _seo_queue(db: dict, now: datetime) -> list:
    """Return list of video IDs to refresh, ordered by priority."""
    no_seo, stale, oldest = [], [], []
    for vid, rec in db.items():
        if rec.get("status") == "removed":
            continue
        seo = rec.get("seo")
        if not seo:
            no_seo.append(vid)
        elif seo.get("prompt_version", 0) < PROMPT_VERSION:
            stale.append(vid)
        else:
            oldest.append((seo.get("updated_at", ""), vid))

    oldest.sort()
    return no_seo + stale + [vid for _, vid in oldest]


def _record_seo(record: dict, seo_obj: dict, model: str, updated_at: str):
    """Store new SEO data into record, rotating history (keep last 3)."""
    if "seo" in record:
        history = record.setdefault("seo_history", [])
        history.append(record["seo"])
        record["seo_history"] = history[-3:]
    seo_obj["prompt_version"] = PROMPT_VERSION
    seo_obj["updated_at"]     = updated_at
    seo_obj["model"]          = model
    record["seo"] = seo_obj


# ── COOKIE AUTH ───────────────────────────────────────────────────────────────

def _write_cookies_file() -> str | None:
    b64 = os.getenv("YT_COOKIES_B64", "").strip()
    if not b64:
        return None
    try:
        data = base64.b64decode(b64).decode("utf-8")
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        )
        tmp.write(data)
        tmp.close()
        return tmp.name
    except Exception as e:
        print(f"Cookie decode failed: {e}")
        return None


# ── YOUTUBE DATA ──────────────────────────────────────────────────────────────

def get_channel_videos(cookie_file: str | None = None) -> list:
    opts = {
        "quiet":        True,
        "extract_flat": True,
        "ignoreerrors": True,
    }
    if cookie_file:
        opts["cookiefile"] = cookie_file

    base_url = f"https://www.youtube.com/{CHANNEL_HANDLE}/videos"
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(base_url, download=False)
            ch_id = info.get("id", "")
            if ch_id.startswith("UC"):
                uploads_url = (
                    f"https://www.youtube.com/playlist?list=UU{ch_id[2:]}"
                )
                full = ydl.extract_info(uploads_url, download=False)
                return full.get("entries", []) or []
            return info.get("entries", []) or []
    except Exception as e:
        print(f"Scrape error: {e}")
        return []


def _detail_sync(db: dict, cookie_file: str | None = None) -> set:
    """
    Fetch full metadata for videos with no last_synced.
    Soft-stops on bot-check. Returns set of updated video IDs.
    """
    opts = {
        "quiet":          True,
        "ignoreerrors":   True,
        "noplaylist":     True,
        "socket_timeout": 20,
        "retries":        3,
    }
    if cookie_file:
        opts["cookiefile"] = cookie_file

    budget_secs = SYNC_TIME_BUDGET_MIN * 60
    start       = time.monotonic()
    updated     = set()

    candidates = [
        vid for vid, rec in db.items()
        if rec.get("status") == "active" and not rec.get("last_synced")
    ]
    candidates = candidates[:int(os.getenv("SYNC_SLICE_SIZE", "50"))]

    for vid in candidates:
        if time.monotonic() - start > budget_secs:
            break
        url = f"https://www.youtube.com/watch?v={vid}"
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            if not info:
                continue
            db[vid]["original_title"]  = info.get("title") or db[vid].get("original_title", "")
            db[vid]["description"]     = (info.get("description") or "")[:400]
            db[vid]["tags"]            = (info.get("tags") or [])[:15]
            db[vid]["duration_seconds"]= info.get("duration", 0)
            db[vid]["published_at"]    = info.get("upload_date")
            db[vid]["last_synced"]     = datetime.now(timezone.utc).isoformat()
            updated.add(vid)
        except Exception as exc:
            if "bot" in str(exc).lower() or "sign in" in str(exc).lower():
                print("yt-dlp detail sync stopped: bot-check.")
                break
            continue
        time.sleep(random.uniform(1, 3))

    return updated


# ── TRENDING KEYWORDS ─────────────────────────────────────────────────────────

def _autocomplete_suggestions(seeds: list[str]) -> list[str]:
    results = []
    for seed in seeds:
        try:
            url = (
                "https://suggestqueries.google.com/complete/search?"
                f"client=youtube&ds=yt&q={quote_plus(seed)}"
            )
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0"}
            )
            with urllib.request.urlopen(req, timeout=5) as r:
                raw = r.read().decode("utf-8")
            inner = re.search(r'\[.*\]', raw)
            if inner:
                data = json.loads(inner.group())
                if isinstance(data, list) and len(data) > 1:
                    for item in data[1][:6]:
                        results.append(item[0] if isinstance(item, list) else str(item))
        except Exception:
            pass
    return results


def get_trending_keywords() -> list[str]:
    """Fetch live trending keywords via pytrends + autocomplete."""
    keywords = list(TRENDING_KEYWORDS_FALLBACK)
    try:
        from pytrends.request import TrendReq
        pt = TrendReq(timeout=(2, 2), retries=0)
        for pn in ["pakistan"]:
            df = pt.trending_searches(pn=pn)
            if hasattr(df, "iloc"):
                col = df.iloc[:, 0] if df.shape[1] > 0 else df
                keywords += [str(v) for v in col.values[:6]]
            elif isinstance(df, dict):
                vals = list(df.values())
                if vals:
                    keywords += [str(v) for v in list(vals[0])[:6]]
        pt.build_payload(kw_list=TRENDING_TOPICS[:5], timeframe="now 7-d")
        related = pt.related_queries()
        for topic, data in related.items():
            for kind in ("top", "rising"):
                sub = data.get(kind)
                if sub is not None and hasattr(sub, "get"):
                    for q in (sub.get("query") or [])[:2]:
                        keywords.append(str(q))
                elif isinstance(sub, dict):
                    for q in list(sub.values())[:2]:
                        keywords.append(str(q))
    except Exception as e:
        print(f"pytrends unavailable: {e}")

    # dedup preserving order
    seen = set()
    out = []
    for k in keywords:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


# ── GEMINI AI ─────────────────────────────────────────────────────────────────

class GeminiRateLimiter:
    """Simple token-bucket style rate limiter."""
    def __init__(self, calls_per_min: int = 10):
        self._min_gap = 60.0 / calls_per_min
        self._last    = 0.0

    def wait(self):
        now  = time.monotonic()
        wait = self._min_gap - (now - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()


_limiter = GeminiRateLimiter(calls_per_min=10)
_client  = None


def _gemini_client():
    global _client
    if _client is None:
        _client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    return _client


def parse_seo_response(text: str, expected_ids: list) -> list | None:
    """
    Parse Gemini JSON response.
    Returns list of dicts with id/title/description/keywords or None on failure.
    """
    text = text.strip()
    text = re.sub(r"^```[a-z]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, list):
        return None
    if len(parsed) != len(expected_ids):
        return None
    id_set = set(expected_ids)
    out = []
    for item in parsed:
        if not isinstance(item, dict):
            return None
        if item.get("id") not in id_set:
            return None
        title = str(item.get("title", "")).strip()
        desc  = str(item.get("description", "")).strip()
        kws   = item.get("keywords", [])
        if not title or not desc:
            return None
        out.append({
            "id":          item["id"],
            "title":       title,
            "description": desc,
            "keywords":    kws if isinstance(kws, list) else [],
        })
    return out


def parse_polished_response(text: str, expected_ids: list) -> list | None:
    """Alias kept for test compatibility."""
    return parse_seo_response(text, expected_ids)


def _generate_seo_batch(
    batch: list[dict],
    trending: list[str],
    used_titles: set[str],
) -> tuple:
    """
    Call Gemini to generate SEO for a batch of videos.
    Returns (results, model_name, success_bool).
    results is list of dicts {id, title, description, keywords} or None.
    """
    if DRY_RUN:
        results = [
            {"id": b["id"], "title": b["title"],
             "description": "Video by Hassan Paracha.", "keywords": []}
            for b in batch
        ]
        return results, "dry-run", True

    used_sample = "; ".join(list(used_titles)[-40:])
    trend_str   = ", ".join(trending[:12])
    input_json  = json.dumps(
        [{"id": b["id"], "title": b["title"]} for b in batch],
        ensure_ascii=False
    )
    prompt = (
        "You are a Pakistani YouTube SEO expert writing for Hinglish audiences.\n"
        "For each video below return a JSON array (same length, same order) where each item has:\n"
        '  "id": same id as input\n'
        '  "title": optimised title, max 70 chars, unique, mix Urdu/Hindi keywords naturally\n'
        '  "description": 2 sentences (each ending in "."), useful & keyword-rich, max 160 chars\n'
        '  "keywords": list of 5 Hinglish SEO keywords\n'
        "Rules:\n"
        "- Each title MUST be unique; avoid any theme from USED list.\n"
        "- Weave in 1-2 of these trending terms where natural: " + trend_str + "\n"
        "- Return ONLY the JSON array. No markdown, no preamble.\n\n"
        "USED titles (avoid repeating these themes): " + used_sample + "\n\n"
        "Input JSON:\n" + input_json
    )

    ids = [b["id"] for b in batch]
    model_name = "gemini-2.0-flash"

    for attempt in range(4):
        try:
            _limiter.wait()
            resp = _gemini_client().models.generate_content(
                model=model_name, contents=prompt
            )
            parsed = parse_seo_response(resp.text, ids)
            if parsed:
                return parsed, model_name, True
        except Exception as exc:
            msg = str(exc)
            if "429" in msg or "quota" in msg.lower():
                wait = (2 ** attempt) * 30 + random.randint(0, 10)
                print(f"  Gemini 429, retrying in {wait}s…")
                time.sleep(wait)
                continue
            print(f"  Gemini error: {exc}")
            break
        time.sleep(5)

    return None, model_name, False


# ── HTML HELPERS ──────────────────────────────────────────────────────────────

def _e(text: str) -> str:
    """HTML-escape untrusted content."""
    return _html_mod.escape(str(text), quote=True)


_CSS = """
:root{--bg:#f0f2f5;--card:#fff;--accent:#ff0000;--text:#222;
  --sub:#555;--border:#e0e0e0;--radius:14px;--shadow:0 4px 18px rgba(0,0,0,.07)}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',sans-serif;background:var(--bg);color:var(--text)}
header{background:#fff;border-bottom:1px solid var(--border);
  padding:14px 20px;display:flex;align-items:center;gap:14px;
  position:sticky;top:0;z-index:10}
header h1{font-size:1.1rem;font-weight:700}
header a{color:var(--accent);text-decoration:none;font-size:.85rem;font-weight:600}
.topic-bar{display:flex;gap:8px;flex-wrap:wrap;padding:10px 20px;
  background:#fff;border-bottom:1px solid var(--border)}
.topic-bar a{padding:4px 12px;border-radius:20px;border:1px solid var(--border);
  font-size:.8rem;color:var(--sub);text-decoration:none}
.topic-bar a:hover,.topic-bar a.active{background:var(--accent);
  color:#fff;border-color:var(--accent)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));
  gap:20px;padding:20px;max-width:1400px;margin:0 auto}
.card{background:var(--card);border-radius:var(--radius);
  box-shadow:var(--shadow);overflow:hidden;display:flex;flex-direction:column}
.thumb{position:relative;padding-bottom:56.25%;background:#000}
.thumb img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
.play-btn{position:absolute;inset:0;display:flex;align-items:center;
  justify-content:center;background:rgba(0,0,0,.3);font-size:2.5rem;
  color:#fff;text-decoration:none;opacity:0;transition:opacity .2s}
.thumb:hover .play-btn{opacity:1}
.info{padding:12px 14px;flex:1;display:flex;flex-direction:column;gap:6px}
.info h3{font-size:.9rem;line-height:1.4;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.info p{font-size:.78rem;color:var(--sub);flex:1;
  display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.btn{display:block;text-align:center;background:var(--accent);color:#fff;
  text-decoration:none;padding:9px;margin:0 14px 14px;
  border-radius:8px;font-size:.82rem;font-weight:700}
.btn:hover{background:#cc0000}
.pagination{display:flex;justify-content:center;gap:8px;
  padding:28px 20px;flex-wrap:wrap}
.pagination a,.pagination span{padding:8px 16px;border-radius:8px;
  border:1px solid var(--border);font-size:.85rem;text-decoration:none;color:var(--text)}
.pagination a:hover,.pagination .cur{background:var(--accent);
  color:#fff;border-color:var(--accent)}
footer{text-align:center;padding:20px;font-size:.78rem;color:var(--sub);
  border-top:1px solid var(--border)}
.single{max-width:860px;margin:30px auto;padding:0 16px}
.single .embed{position:relative;padding-bottom:56.25%;border-radius:var(--radius);
  overflow:hidden;background:#000}
.single .embed iframe{position:absolute;inset:0;width:100%;height:100%;border:0}
.single h1{font-size:1.3rem;margin:16px 0 8px}
.single .desc{font-size:.88rem;color:var(--sub);line-height:1.6;margin-bottom:12px}
.tags{display:flex;flex-wrap:wrap;gap:6px;margin:10px 0}
.tags span{background:var(--border);border-radius:12px;
  padding:3px 10px;font-size:.75rem;color:var(--sub)}
.related-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:14px}
@media(max-width:480px){.grid{grid-template-columns:1fr;padding:12px}}
"""


def _header(active_topic: str = "") -> str:
    def _link(t: str) -> str:
        cls = ' class="active"' if t == active_topic else ""
        label = t.replace("-", " ").title()
        return f'<a href="{SITE_BASE_URL}topics/{t}/"{cls}>{_e(label)}</a>'
    topic_links = "\n".join(_link(t) for t in TOPICS)
    return (
        f'<header>'
        f'<h1>Hassan Paracha</h1>'
        f'<a href="{SITE_BASE_URL}">🏠 All Videos</a>'
        f'<a href="https://www.youtube.com/@hassanparachaoffical?sub_confirmation=1"'
        f' target="_blank" rel="noopener">▶ Subscribe</a>'
        f'</header>'
        f'<nav class="topic-bar">{topic_links}</nav>'
    )


def _pagination(current: int, total_pages: int, base: str) -> str:
    if total_pages <= 1:
        return ""
    links = []
    if current > 1:
        prev_href = base if current == 2 else f"{base}page/{current-1}/"
        links.append(f'<a href="{prev_href}">« Prev</a>')
    for p in range(1, total_pages + 1):
        if p == current:
            links.append(f'<span class="cur">{p}</span>')
        elif abs(p - current) <= 2 or p in (1, total_pages):
            href = base if p == 1 else f"{base}page/{p}/"
            links.append(f'<a href="{href}">{p}</a>')
        elif abs(p - current) == 3:
            links.append('<span>…</span>')
    if current < total_pages:
        links.append(f'<a href="{base}page/{current+1}/">Next »</a>')
    return f'<nav class="pagination">{"".join(links)}</nav>'


def _html_shell(title: str, desc: str, url: str, body: str,
                og_image: str = "") -> str:
    if not og_image:
        og_image = f"{SITE_BASE_URL}og.jpg"
    return (
        f'<!DOCTYPE html><html lang="en"><head>'
        f'<meta charset="UTF-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{_e(title)}</title>'
        f'<meta name="description" content="{_e(desc)}">'
        f'<meta name="robots" content="index,follow">'
        f'<link rel="canonical" href="{url}">'
        f'<meta property="og:title" content="{_e(title)}">'
        f'<meta property="og:description" content="{_e(desc)}">'
        f'<meta property="og:url" content="{url}">'
        f'<meta property="og:image" content="{og_image}">'
        f'<style>{_CSS}</style>'
        f'</head><body>{body}'
        f'<footer><p>© {date.today().year} Hassan Paracha · '
        f'<a href="{SITE_BASE_URL}sitemap.xml">Sitemap</a> · '
        f'<a href="https://www.youtube.com/@hassanparachaoffical"'
        f' target="_blank" rel="noopener">YouTube Channel</a>'
        f'</p></footer></body></html>'
    )


def _card_html(vid: str, data: dict) -> str:
    seo   = data.get("seo") or {}
    title = _e(seo.get("title") or data.get("original_title", "Video"))
    desc  = _e((seo.get("description") or data.get("description", ""))[:120])
    thumb = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
    yt_url   = f"https://www.youtube.com/watch?v={vid}"
    page_url = f"{SITE_BASE_URL}v/{vid}/"
    return (
        f'<article class="card" itemscope itemtype="https://schema.org/VideoObject">'
        f'<meta itemprop="name" content="{title}">'
        f'<meta itemprop="url" content="{yt_url}">'
        f'<a href="{page_url}" class="thumb">'
        f'<img src="{thumb}" alt="{title}" loading="lazy" width="320" height="180">'
        f'<span class="play-btn">▶</span></a>'
        f'<div class="info"><h3><a href="{page_url}" style="color:inherit;'
        f'text-decoration:none">{title}</a></h3>'
        f'{"<p>" + desc + "</p>" if desc else ""}</div>'
        f'<a class="btn" href="{yt_url}" target="_blank" rel="noopener">'
        f'Watch on YouTube</a></article>'
    )


def build_html(videos: list, page_number: int = 1,
               total_pages: int = 1, page_path: str = "") -> str:
    """
    Build index/listing HTML page.
    Required by tests: build_html(videos, page_number, total_pages, page_path).
    """
    cards = "".join(_card_html(vid, data) for vid, data in videos)
    base  = SITE_BASE_URL
    pager = _pagination(page_number, total_pages, base)
    body  = (
        f"{_header()}"
        f'<main class="grid">{cards}</main>'
        f"{pager}"
    )
    url = base if page_number == 1 else f"{base}{page_path}"
    return _html_shell(
        "Hassan Paracha Videos | Funny Shorts & Viral Vlogs",
        "Watch Hassan Paracha's funny Pakistani YouTube videos, "
        "bike start fails, family vlogs, and desi comedy shorts.",
        url, body,
    )


def _build_video_page(vid: str, data: dict, related: list,
                      base_url: str, cookie_file) -> str:
    """Build individual /v/{id}/ page. Used internally + by tests."""
    seo   = data.get("seo") or {}
    title = seo.get("title") or data.get("original_title", "Video")
    desc  = seo.get("description") or data.get("description", "")
    tags  = data.get("tags", [])
    pub   = data.get("published_at")
    dur   = data.get("duration_seconds", 0)

    yt_url   = f"https://www.youtube.com/watch?v={vid}"
    page_url = f"{base_url}v/{vid}/"
    thumb    = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"

    schema: dict = {
        "@context": "https://schema.org",
        "@type":    "VideoObject",
        "name":     title,
        "description": desc,
        "thumbnailUrl": thumb,
        "url":      yt_url,
        "embedUrl": f"https://www.youtube.com/embed/{vid}",
        "author": {
            "@type": "Person",
            "name":  "Hassan Paracha",
            "url":   f"https://www.youtube.com/{CHANNEL_HANDLE}",
        },
    }
    if pub:
        schema["uploadDate"] = pub
    if dur:
        schema["duration"] = f"PT{dur}S"

    tags_html = "".join(
        f'<span>{_e(t)}</span>' for t in tags[:8]
    )
    related_cards = "".join(_card_html(rv, rd) for rv, rd in related)

    body = (
        f"{_header()}"
        f'<div class="single">'
        f'<script type="application/ld+json">'
        f'{json.dumps(schema, ensure_ascii=False)}'
        f'</script>'
        f'<div class="embed">'
        f'<iframe src="https://www.youtube.com/embed/{vid}"'
        f' title="{_e(title)}" loading="lazy" allowfullscreen></iframe>'
        f'</div>'
        f'<h1>{_e(title)}</h1>'
        f'<p class="desc">{_e(desc)}</p>'
        + (f'<div class="tags">{tags_html}</div>' if tags_html else "")
        + f'<a class="btn" href="{yt_url}" target="_blank" rel="noopener">'
          f'▶ Watch on YouTube &amp; Subscribe to Hassan Paracha</a>'
        + (f'<h2 style="margin:20px 0 10px;font-size:1rem">Related Videos</h2>'
           f'<div class="related-grid">{related_cards}</div>'
           if related_cards else "")
        + f'<p style="margin-top:16px;font-size:.8rem;color:var(--sub)">'
          f'<a href="{base_url}">← Back to all videos</a></p>'
          f'</div>'
    )
    return _html_shell(
        f"{title} | Hassan Paracha",
        (desc or f"Watch {title} by Hassan Paracha")[:155],
        page_url, body, og_image=thumb,
    )


# ── SITE GENERATOR ────────────────────────────────────────────────────────────

def _site_path(*parts) -> Path:
    p = Path(SITE_DIR, *parts)
    p.mkdir(parents=True, exist_ok=True)
    return p


def generate_site(db: dict) -> tuple[int, int]:
    """
    Build entire site from db.
    Returns (video_count, page_count).
    Required by tests: generate_site(db) -> (int, int).
    """
    # only videos with seo data
    polished = [
        (vid, rec) for vid, rec in db.items()
        if rec.get("status") != "removed" and rec.get("seo")
    ]
    polished.sort(
        key=lambda x: x[1]["seo"].get("updated_at", ""),
        reverse=True
    )

    total_pages = max(1, math.ceil(len(polished) / VIDEOS_PER_PAGE))
    index_map   = {vid: i for i, (vid, _) in enumerate(polished)}

    # index pages
    for page_num in range(1, total_pages + 1):
        chunk = polished[(page_num-1)*VIDEOS_PER_PAGE : page_num*VIDEOS_PER_PAGE]
        html  = build_html(chunk, page_num, total_pages)
        if page_num == 1:
            (_site_path() / "index.html").write_text(html, encoding="utf-8")
        else:
            (_site_path("page", str(page_num)) / "index.html").write_text(
                html, encoding="utf-8"
            )

    # video pages
    for vid, data in polished:
        idx      = index_map[vid]
        neighbours = []
        for offset in [-2, -1, 1, 2, 3]:
            ni = idx + offset
            if 0 <= ni < len(polished) and ni != idx:
                neighbours.append(polished[ni])
            if len(neighbours) == 4:
                break
        html = _build_video_page(vid, data, neighbours, SITE_BASE_URL, None)
        (_site_path("v", vid) / "index.html").write_text(html, encoding="utf-8")

    # topic pages
    for topic, keywords in TOPICS.items():
        matched = [
            (v, d) for v, d in polished
            if any(
                kw in (
                    (d.get("seo") or {}).get("title", "") +
                    d.get("description", "")
                ).lower()
                for kw in keywords
            )
        ] or polished[:12]
        cards = "".join(_card_html(v, d) for v, d in matched[:60])
        label = topic.replace("-", " ").title()
        body  = (
            f"{_header(topic)}"
            f'<div style="padding:16px 20px">'
            f'<h1 style="font-size:1.2rem">{_e(label)} Videos</h1>'
            f'<p style="font-size:.85rem;color:var(--sub);margin-top:4px">'
            f'{len(matched)} videos · Hassan Paracha</p></div>'
            f'<main class="grid">{cards}</main>'
        )
        html = _html_shell(
            f"{label} Videos | Hassan Paracha",
            f"Watch the best {label.lower()} videos by Hassan Paracha.",
            f"{SITE_BASE_URL}topics/{topic}/", body,
        )
        (_site_path("topics", topic) / "index.html").write_text(
            html, encoding="utf-8"
        )

    # sitemap
    today = date.today().isoformat()
    urls  = [
        f"<url><loc>{SITE_BASE_URL}</loc>"
        f"<changefreq>daily</changefreq><priority>1.0</priority></url>"
    ]
    for p in range(2, total_pages + 1):
        urls.append(
            f"<url><loc>{SITE_BASE_URL}page/{p}/</loc>"
            f"<changefreq>daily</changefreq><priority>0.8</priority></url>"
        )
    for t in TOPICS:
        urls.append(
            f"<url><loc>{SITE_BASE_URL}topics/{t}/</loc>"
            f"<changefreq>weekly</changefreq><priority>0.7</priority></url>"
        )
    for vid, _ in polished:
        urls.append(
            f"<url><loc>{SITE_BASE_URL}v/{vid}/</loc>"
            f"<lastmod>{today}</lastmod>"
            f"<changefreq>monthly</changefreq><priority>0.6</priority></url>"
        )
    sitemap = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(urls)
        + "\n</urlset>"
    )
    (_site_path() / "sitemap.xml").write_text(sitemap, encoding="utf-8")
    (_site_path() / "robots.txt").write_text(
        f"User-agent: *\nAllow: /\nSitemap: {SITE_BASE_URL}sitemap.xml\n",
        encoding="utf-8",
    )

    return len(polished), total_pages


# ── MAIN CYCLE ────────────────────────────────────────────────────────────────

def run_rolling_cycle():
    # 1. Cookie auth
    cookie_file = _write_cookies_file()
    print("✓ YouTube cookies loaded." if cookie_file
          else "⚠ No YT_COOKIES_B64 — running without auth.")

    # 2. Trending keywords (once)
    trending = get_trending_keywords()
    suggestions = _autocomplete_suggestions(
        ["viral shorts pakistan", "funny desi video", "motorcycle fail"]
    )
    trending = list(dict.fromkeys(trending + suggestions))[:20]
    print(f"Live trending keywords: {', '.join(trending[:10])}")

    # 3. Load + migrate DB
    db = migrate_database(load_db())

    # 4. Channel listing sync
    listings = get_channel_videos(cookie_file)
    raw      = [
        {"id": e.get("id"), "title": e.get("title", ""),
         "published_at": e.get("upload_date")}
        for e in listings if e.get("id")
    ]
    _sync_database(db, raw)
    _apply_listing_sync(db, raw)
    save_db(db)
    print(f"Library: {len(db)} total videos")

    # 5. Detail sync
    updated = _detail_sync(db, cookie_file)
    if updated:
        print(f"yt-dlp: synced {len(updated)} video details.")
        save_db(db)

    # 6. SEO queue
    now   = datetime.now(timezone.utc)
    queue = _seo_queue(db, now)[:REFRESH_PER_DAY]

    # 7. Gemini polish
    used_titles: set[str] = {
        (db[v].get("seo") or {}).get("title", "")
        for v in db if db[v].get("seo")
    }
    refreshed = 0
    for i in range(0, len(queue), BATCH_SIZE):
        batch_ids = queue[i : i + BATCH_SIZE]
        batch     = [
            {"id": v, "title": db[v].get("original_title", "")}
            for v in batch_ids
        ]
        results, model, ok = _generate_seo_batch(batch, trending, used_titles)
        if ok and results:
            ts = datetime.now(timezone.utc).isoformat()
            for item in results:
                vid = item["id"]
                _record_seo(db[vid], item, model, ts)
                used_titles.add(item["title"])
                refreshed += 1
            save_db(db)
        if i + BATCH_SIZE < len(queue):
            print(f"  Batch {i // BATCH_SIZE + 1} done. Sleeping 60s…")
            time.sleep(60)

    print(f"SEO refreshed: {refreshed}")

    # 8. Build site
    video_count, page_count = generate_site(db)
    total_site = video_count + page_count + len(TOPICS)
    print(f"Built {SITE_DIR}/ with {video_count} SEO pages across {total_site} total pages.")

    if cookie_file:
        try:
            os.unlink(cookie_file)
        except Exception:
            pass


if __name__ == "__main__":
    run_rolling_cycle()
