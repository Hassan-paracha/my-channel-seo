"""
engine.py  —  Hassan Paracha SEO Rolling Cycle  (optimised)
============================================================
Key improvements over previous version:
  1. Duplicate-title deduplication — Gemini sees already-used titles and
     is forced to generate a genuinely different angle for each video.
  2. yt-dlp cookie auth — reads YT_COOKIES_B64 secret → writes a temp
     cookies.txt so YouTube bot-check is bypassed.
  3. Richer per-video pages — description, tags, related links.
  4. 15 topic categories instead of 3.
  5. Internal cross-linking on every /v/ page.
  6. Rate-limit safety: exponential back-off on Gemini 429.
  7. Autocomplete via YouTube search suggest (no API key needed).
  8. Sitemap includes all pages.
  9. robots.txt allows all bots, points to sitemap.
 10. index.html rebuilt every run with correct pagination.
"""

import os, json, time, base64, tempfile, random, re, math
from datetime import datetime, date
from pathlib import Path
from urllib.parse import quote_plus
import urllib.request

import yt_dlp
from google import genai

# ── CONFIGURATION ────────────────────────────────────────────────────────────
CHANNEL_HANDLE      = "@hassanparachaoffical"
DB_FILE             = "video_db.json"
SITE_DIR            = "site"
SITE_BASE_URL       = os.getenv("SITE_BASE_URL",
                        "https://hassan-paracha.github.io/my-channel-seo/")
DAILY_POLISH_LIMIT  = int(os.getenv("REFRESH_PER_DAY", "150"))
SYNC_SLICE          = int(os.getenv("SYNC_SLICE_SIZE", "1000"))
BATCH_SIZE          = 5          # videos per Gemini call
COOLDOWN            = 60         # seconds between batches
DRY_RUN             = os.getenv("DRY_RUN", "0") == "1"
VIDEOS_PER_PAGE     = 48

# ── TOPIC TAXONOMY ────────────────────────────────────────────────────────────
TOPICS = {
    "bike-start":       ["motorcycle", "bike start", "khrab", "start nahi"],
    "celebrations":     ["celebration", "mubarak", "independence", "eid", "birthday"],
    "family-funny":     ["family", "bacho", "kids", "ghar", "bachon"],
    "park-vlogs":       ["park", "mela", "outing", "bahar"],
    "jungle-adventure": ["jungle", "forest", "bhoot", "adventure"],
    "desi-comedy":      ["funny", "comedy", "masti", "mazak", "prank"],
    "street-food":      ["food", "khana", "chai", "desi food"],
    "travel-vlogs":     ["travel", "safar", "road trip", "tour"],
    "emotional":        ["emotional", "dil", "yaad", "motivational"],
    "reaction":         ["reaction", "dekhiye", "shocking", "amazing"],
    "daily-vlog":       ["vlog", "routine", "aaj ka din", "daily"],
    "challenges":       ["challenge", "try", "attempt", "koshish"],
    "unboxing":         ["unbox", "new", "product", "review"],
    "shorts-compilation": ["shorts", "viral", "trending", "reels"],
    "special-events":   ["wedding", "shadi", "party", "event"],
}

LIVE_TRENDING = [
    "viral shorts", "trending pakistan", "funny video", "vlog",
    "hindi", "urdu", "desi", "reels", "youtube shorts 2026",
]

# ── HELPERS ───────────────────────────────────────────────────────────────────

def load_db() -> dict:
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_db(db: dict):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2, ensure_ascii=False)

def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:50]

def site_path(*parts) -> Path:
    p = Path(SITE_DIR, *parts)
    p.mkdir(parents=True, exist_ok=True)
    return p

def write_file(path: Path, content: str):
    path.write_text(content, encoding="utf-8")

# ── COOKIE AUTH ───────────────────────────────────────────────────────────────

def write_cookies_file() -> str | None:
    """Decode YT_COOKIES_B64 secret → temp file, return path or None."""
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

def get_channel_videos(cookie_file: str | None) -> list:
    opts = {
        "quiet": True,
        "extract_flat": True,
        "playlist_items": f"1:{SYNC_SLICE}",
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
                uploads_url = f"https://www.youtube.com/playlist?list=UU{ch_id[2:]}"
                full = ydl.extract_info(uploads_url, download=False)
                return full.get("entries", []) or []
            return info.get("entries", []) or []
    except Exception as e:
        print(f"Scrape error: {e}")
        return []

def get_video_detail(vid_id: str, cookie_file: str | None) -> dict:
    """Fetch real description + tags for a single video."""
    opts = {"quiet": True, "ignoreerrors": True}
    if cookie_file:
        opts["cookiefile"] = cookie_file
    url = f"https://www.youtube.com/watch?v={vid_id}"
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return {
                "description": (info.get("description") or "")[:300].strip(),
                "tags":        (info.get("tags") or [])[:10],
                "view_count":  info.get("view_count", 0),
                "upload_date": info.get("upload_date", ""),
            }
    except Exception:
        return {}

def youtube_suggest(query: str) -> list[str]:
    """Zero-API YouTube autocomplete."""
    try:
        url = (
            "https://suggestqueries.google.com/complete/search?"
            f"client=youtube&ds=yt&q={quote_plus(query)}"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as r:
            raw = r.read().decode("utf-8")
        # response is JSONP: window.google.ac.h([...])
        inner = re.search(r'\[.*\]', raw)
        if inner:
            data = json.loads(inner.group())
            if isinstance(data, list) and len(data) > 1:
                items = data[1]
                return [i[0] if isinstance(i, list) else str(i) for i in items[:6]]
    except Exception:
        pass
    return []

# ── GEMINI AI ─────────────────────────────────────────────────────────────────

_client = None

def _gemini():
    global _client
    if _client is None:
        _client = genai.Client()
    return _client

def ai_polish_batch(batch: list[dict], used_titles: set[str]) -> list[str]:
    """
    batch: list of {"vid": id, "title": original_title}
    used_titles: titles already in DB so Gemini avoids duplicates.
    Returns list of polished titles (same length as batch).
    """
    if DRY_RUN:
        return [b["title"] for b in batch]

    used_sample = "; ".join(list(used_titles)[-40:])  # last 40 used
    items = "\n".join(
        f'{i+1}. VID={b["vid"]} TITLE={b["title"]}'
        for i, b in enumerate(batch)
    )
    prompt = (
        "You are a Pakistani YouTube SEO expert. "
        "Optimise each title below for Hinglish (Hindi+English) search. "
        "Rules:\n"
        "- Each title MUST be unique; do NOT reuse any phrase from the USED list.\n"
        "- Include 2-3 trending Urdu/Hindi keywords naturally.\n"
        "- Keep under 70 characters.\n"
        "- Return ONLY a JSON array of strings, one per title, no extra text.\n\n"
        f"USED (avoid repeating these themes): {used_sample}\n\n"
        f"TITLES TO OPTIMISE:\n{items}"
    )

    for attempt in range(4):
        try:
            resp = _gemini().models.generate_content(
                model="gemini-2.0-flash", contents=prompt
            )
            text = resp.text.strip()
            # strip markdown fences if present
            text = re.sub(r"^```[a-z]*\n?", "", text)
            text = re.sub(r"\n?```$", "", text)
            parsed = json.loads(text)
            if isinstance(parsed, list) and len(parsed) == len(batch):
                return [str(t).strip().replace('"', '') for t in parsed]
        except json.JSONDecodeError:
            pass
        except Exception as e:
            msg = str(e)
            if "429" in msg or "quota" in msg.lower():
                wait = (2 ** attempt) * 30 + random.randint(0, 10)
                print(f"  Gemini rate-limit, waiting {wait}s…")
                time.sleep(wait)
                continue
            print(f"  Gemini error: {e}")
            break
        time.sleep(5)

    # fallback: return originals
    return [b["title"] for b in batch]

# ── HTML BUILDERS ─────────────────────────────────────────────────────────────

_CSS = """
:root{
  --bg:#f0f2f5;--card:#fff;--accent:#ff0000;--text:#222;
  --sub:#555;--border:#e0e0e0;--radius:14px;--shadow:0 4px 18px rgba(0,0,0,.07);
}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',sans-serif;background:var(--bg);color:var(--text)}
header{background:#fff;border-bottom:1px solid var(--border);
       padding:14px 20px;display:flex;align-items:center;gap:14px;
       position:sticky;top:0;z-index:10}
header h1{font-size:1.1rem;font-weight:700}
header a{color:var(--accent);text-decoration:none;font-size:.85rem;font-weight:600}
.topic-bar{display:flex;gap:8px;flex-wrap:wrap;padding:12px 20px;
           background:#fff;border-bottom:1px solid var(--border)}
.topic-bar a{padding:4px 12px;border-radius:20px;border:1px solid var(--border);
             font-size:.8rem;color:var(--sub);text-decoration:none;
             transition:background .2s}
.topic-bar a:hover{background:var(--accent);color:#fff;border-color:var(--accent)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));
      gap:20px;padding:20px;max-width:1400px;margin:0 auto}
.card{background:var(--card);border-radius:var(--radius);
      box-shadow:var(--shadow);overflow:hidden;display:flex;flex-direction:column}
.thumb{position:relative;padding-bottom:56.25%;background:#000}
.thumb img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
.play-btn{position:absolute;inset:0;display:flex;align-items:center;
          justify-content:center;background:rgba(0,0,0,.3);
          font-size:2.5rem;color:#fff;text-decoration:none;opacity:0;
          transition:opacity .2s}
.thumb:hover .play-btn{opacity:1}
.info{padding:12px 14px;flex:1;display:flex;flex-direction:column;gap:8px}
.info h3{font-size:.9rem;line-height:1.4;color:var(--text);
         display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;
         overflow:hidden}
.info p{font-size:.78rem;color:var(--sub);flex:1;
        display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;
        overflow:hidden}
.btn{display:block;text-align:center;background:var(--accent);color:#fff;
     text-decoration:none;padding:9px;margin:0 14px 14px;
     border-radius:8px;font-size:.82rem;font-weight:700}
.btn:hover{background:#cc0000}
.pagination{display:flex;justify-content:center;gap:8px;
            padding:28px 20px;flex-wrap:wrap}
.pagination a,.pagination span{padding:8px 16px;border-radius:8px;
  border:1px solid var(--border);font-size:.85rem;text-decoration:none;
  color:var(--text)}
.pagination a:hover,.pagination .cur{background:var(--accent);
  color:#fff;border-color:var(--accent)}
footer{text-align:center;padding:20px;font-size:.78rem;color:var(--sub);
       border-top:1px solid var(--border)}
/* single video page */
.single{max-width:860px;margin:30px auto;padding:0 16px}
.single .embed{position:relative;padding-bottom:56.25%;border-radius:var(--radius);
               overflow:hidden;background:#000}
.single .embed iframe{position:absolute;inset:0;width:100%;height:100%;border:0}
.single h1{font-size:1.3rem;margin:16px 0 8px}
.single .desc{font-size:.88rem;color:var(--sub);line-height:1.6;margin-bottom:16px}
.related-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));
              gap:14px;margin-top:14px}
.related-grid .card{font-size:.8rem}
.tags{display:flex;flex-wrap:wrap;gap:6px;margin:10px 0}
.tags span{background:var(--border);border-radius:12px;
           padding:3px 10px;font-size:.75rem;color:var(--sub)}
@media(max-width:480px){
  header h1{font-size:.95rem}
  .grid{grid-template-columns:1fr;padding:12px}
}
"""

def _header(active_topic: str = "") -> str:
    def _topic_link(t: str) -> str:
        style = ' style="background:var(--accent);color:#fff"' if t == active_topic else ""
        label = t.replace("-", " ").title()
        return f'<a href="{SITE_BASE_URL}topics/{t}/"{style}>{label}</a>'

    topic_links = "\n".join(_topic_link(t) for t in TOPICS)
    return f"""
<header>
  <h1>Hassan Paracha</h1>
  <a href="{SITE_BASE_URL}" title="Home">🏠 All Videos</a>
  <a href="https://www.youtube.com/@hassanparachaoffical?sub_confirmation=1"
     target="_blank" rel="noopener">▶ Subscribe</a>
</header>
<nav class="topic-bar" aria-label="Topics">
{topic_links}
</nav>"""

def _card_html(vid: str, data: dict) -> str:
    title   = data.get("title", "Video")
    desc    = data.get("description", "")
    thumb   = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
    yt_url  = f"https://www.youtube.com/watch?v={vid}"
    page_url= f"{SITE_BASE_URL}v/{vid}/"
    return f"""
<article class="card" itemscope itemtype="https://schema.org/VideoObject">
  <meta itemprop="name" content="{title}">
  <meta itemprop="uploadDate" content="{data.get('last_refreshed','2000-01-01')}">
  <meta itemprop="thumbnailUrl" content="{thumb}">
  <meta itemprop="url" content="{yt_url}">
  <a href="{page_url}" class="thumb" aria-label="Watch {title}">
    <img src="{thumb}" alt="{title}" loading="lazy" width="320" height="180">
    <span class="play-btn" aria-hidden="true">▶</span>
  </a>
  <div class="info">
    <h3 itemprop="description"><a href="{page_url}" style="color:inherit;text-decoration:none">{title}</a></h3>
    {"<p>" + desc + "</p>" if desc else ""}
  </div>
  <a class="btn" href="{yt_url}" target="_blank" rel="noopener">Watch on YouTube</a>
</article>"""

def _html_shell(title: str, desc: str, url: str, body: str,
                og_image: str = "") -> str:
    if not og_image:
        og_image = f"{SITE_BASE_URL}og.jpg"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<meta name="description" content="{desc}">
<meta name="robots" content="index,follow">
<link rel="canonical" href="{url}">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{desc}">
<meta property="og:url" content="{url}">
<meta property="og:image" content="{og_image}">
<meta property="og:type" content="website">
<meta name="google-site-verification" content="QyDSUhm4hfRGneKuDBC1rA-p19E7Y2lI_Eh9krE5nVY">
<style>{_CSS}</style>
</head>
<body>
{body}
<footer>
  <p>© {date.today().year} Hassan Paracha ·
     <a href="{SITE_BASE_URL}sitemap.xml">Sitemap</a> ·
     <a href="https://www.youtube.com/@hassanparachaoffical" target="_blank" rel="noopener">YouTube Channel</a>
  </p>
</footer>
</body>
</html>"""

def _pagination(current: int, total_pages: int, base: str) -> str:
    if total_pages <= 1:
        return ""
    links = []
    if current > 1:
        prev = base if current == 2 else f"{base}page/{current-1}/"
        links.append(f'<a href="{prev}">« Prev</a>')
    for p in range(1, total_pages + 1):
        if p == current:
            links.append(f'<span class="cur">{p}</span>')
        elif abs(p - current) <= 2 or p == 1 or p == total_pages:
            href = base if p == 1 else f"{base}page/{p}/"
            links.append(f'<a href="{href}">{p}</a>')
        elif abs(p - current) == 3:
            links.append('<span>…</span>')
    if current < total_pages:
        links.append(f'<a href="{base}page/{current+1}/">Next »</a>')
    return f'<nav class="pagination" aria-label="Pages">{"".join(links)}</nav>'

# ── PAGE BUILDERS ─────────────────────────────────────────────────────────────

def build_index(polished: list[tuple], base: str = SITE_BASE_URL):
    """Build paginated index pages at site/ root."""
    total = len(polished)
    total_pages = max(1, math.ceil(total / VIDEOS_PER_PAGE))

    for page_num in range(1, total_pages + 1):
        chunk = polished[(page_num-1)*VIDEOS_PER_PAGE : page_num*VIDEOS_PER_PAGE]
        cards = "".join(_card_html(vid, data) for vid, data in chunk)
        pager = _pagination(page_num, total_pages, base)
        body = f"""
{_header()}
<main class="grid" aria-label="Videos">
{cards}
</main>
{pager}"""
        url = base if page_num == 1 else f"{base}page/{page_num}/"
        html = _html_shell(
            "Hassan Paracha Videos | Funny Shorts & Viral Vlogs",
            "Watch Hassan Paracha's funny Pakistani YouTube videos, bike start fails, "
            "family vlogs, and desi comedy shorts.",
            url, body,
            og_image=f"https://i.ytimg.com/vi/{polished[0][0]}/hqdefault.jpg" if polished else "",
        )
        if page_num == 1:
            write_file(site_path() / "index.html", html)
        else:
            write_file(site_path("page", str(page_num)) / "index.html", html)

    return total_pages

def build_video_pages(polished: list[tuple]):
    """Build individual /v/{id}/ pages with related videos."""
    index_map = {vid: i for i, (vid, _) in enumerate(polished)}

    for vid, data in polished:
        title   = data.get("title", "Video")
        desc    = data.get("description", "Watch this entertaining video by Hassan Paracha.")
        tags    = data.get("tags", [])
        yt_url  = f"https://www.youtube.com/watch?v={vid}"
        page_url= f"{SITE_BASE_URL}v/{vid}/"
        thumb   = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
        upload  = data.get("last_refreshed", date.today().isoformat())

        # pick 4 related (prev/next 2 in polished list)
        idx = index_map[vid]
        neighbours = []
        for offset in [-2, -1, 1, 2, 3, -3]:
            ni = idx + offset
            if 0 <= ni < len(polished) and ni != idx:
                neighbours.append(polished[ni])
            if len(neighbours) == 4:
                break
        related_cards = "".join(
            _card_html(rv, rd) for rv, rd in neighbours
        )

        tags_html = "".join(f"<span>{t}</span>" for t in tags[:8]) if tags else ""

        schema = json.dumps({
            "@context": "https://schema.org",
            "@type": "VideoObject",
            "name": title,
            "description": desc,
            "thumbnailUrl": thumb,
            "uploadDate": upload,
            "url": yt_url,
            "embedUrl": f"https://www.youtube.com/embed/{vid}",
            "author": {
                "@type": "Person",
                "name": "Hassan Paracha",
                "url": f"https://www.youtube.com/{CHANNEL_HANDLE}"
            }
        }, ensure_ascii=False)

        body = f"""
{_header()}
<div class="single">
  <script type="application/ld+json">{schema}</script>
  <div class="embed">
    <iframe src="https://www.youtube.com/embed/{vid}"
            title="{title}" loading="lazy" allowfullscreen></iframe>
  </div>
  <h1>{title}</h1>
  <p class="desc">{desc}</p>
  {"<div class='tags'>" + tags_html + "</div>" if tags_html else ""}
  <a class="btn" href="{yt_url}" target="_blank" rel="noopener">
    ▶ Watch on YouTube &amp; Subscribe to Hassan Paracha
  </a>
  <h2 style="margin:24px 0 12px;font-size:1rem">Related Videos</h2>
  <div class="related-grid">
    {related_cards}
  </div>
  <p style="margin-top:16px;font-size:.8rem;color:var(--sub)">
    <a href="{SITE_BASE_URL}">← Back to all videos</a>
  </p>
</div>"""

        html = _html_shell(
            f"{title} | Hassan Paracha",
            desc[:155],
            page_url, body,
            og_image=thumb,
        )
        write_file(site_path("v", vid) / "index.html", html)

def build_topic_pages(polished: list[tuple]):
    """Build /topics/{name}/ pages."""
    for topic, keywords in TOPICS.items():
        matched = [
            (vid, data) for vid, data in polished
            if any(kw in (data.get("title", "") + data.get("description", "")).lower()
                   for kw in keywords)
        ]
        if not matched:
            matched = polished[:12]  # fallback: show recent

        cards = "".join(_card_html(vid, data) for vid, data in matched[:60])
        topic_title = topic.replace("-", " ").title()
        url = f"{SITE_BASE_URL}topics/{topic}/"
        body = f"""
{_header(topic)}
<div style="padding:16px 20px">
  <h1 style="font-size:1.2rem">{topic_title} Videos</h1>
  <p style="font-size:.85rem;color:var(--sub);margin-top:4px">
    {len(matched)} videos · Hassan Paracha
  </p>
</div>
<main class="grid">{cards}</main>"""
        html = _html_shell(
            f"{topic_title} Videos | Hassan Paracha",
            f"Watch the best {topic_title.lower()} videos by Hassan Paracha — "
            "Pakistani funny shorts, desi vlogs and more.",
            url, body,
        )
        write_file(site_path("topics", topic) / "index.html", html)

def build_sitemap(polished: list[tuple], total_pages: int):
    today = date.today().isoformat()
    urls = [f"<url><loc>{SITE_BASE_URL}</loc><changefreq>daily</changefreq><priority>1.0</priority></url>"]

    for p in range(2, total_pages + 1):
        urls.append(f"<url><loc>{SITE_BASE_URL}page/{p}/</loc><changefreq>daily</changefreq><priority>0.8</priority></url>")

    for topic in TOPICS:
        urls.append(f"<url><loc>{SITE_BASE_URL}topics/{topic}/</loc><changefreq>weekly</changefreq><priority>0.7</priority></url>")

    for vid, _ in polished:
        urls.append(f"<url><loc>{SITE_BASE_URL}v/{vid}/</loc><lastmod>{today}</lastmod><changefreq>monthly</changefreq><priority>0.6</priority></url>")

    sitemap = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    sitemap += "\n".join(urls)
    sitemap += "\n</urlset>"
    write_file(site_path() / "sitemap.xml", sitemap)

def build_robots():
    write_file(site_path() / "robots.txt",
               f"User-agent: *\nAllow: /\nSitemap: {SITE_BASE_URL}sitemap.xml\n")

# ── MAIN CYCLE ────────────────────────────────────────────────────────────────

def run_rolling_cycle():
    # ── 1. Cookie auth
    cookie_file = write_cookies_file()
    if cookie_file:
        print("✓ YouTube cookies loaded.")
    else:
        print("⚠ No YT_COOKIES_B64 — running without auth (bot-check risk).")

    # ── 2. Fetch live trending autocomplete
    suggestions = []
    for seed in ["viral shorts pakistan", "funny desi video", "motorcycle fail"]:
        suggestions += youtube_suggest(seed)
    trending_keywords = list(dict.fromkeys(LIVE_TRENDING + suggestions))[:20]
    print(f"Live trending keywords: {', '.join(trending_keywords[:10])}")

    # ── 3. Load DB + scrape channel
    db = load_db()
    all_videos = get_channel_videos(cookie_file)

    today_str = date.today().isoformat()
    new_count = 0
    for v in all_videos:
        v_id = v.get("id")
        if not v_id:
            continue
        if v_id not in db:
            db[v_id] = {
                "title":          v.get("title", "YouTube Video"),
                "description":    "",
                "tags":           [],
                "last_refreshed": "2000-01-01",
                "url":            f"https://www.youtube.com/watch?v={v_id}",
            }
            new_count += 1

    print(f"Library: {len(db)} total | {new_count} new today")

    # ── 4. Pick oldest for polishing
    sorted_ids = sorted(db, key=lambda x: db[x].get("last_refreshed", "2000-01-01"))
    to_process = sorted_ids[:DAILY_POLISH_LIMIT]

    # ── 5. yt-dlp detail sync for unpolished videos (best-effort)
    detail_budget = 30  # max detail fetches per run to avoid bot-check
    fetched = 0
    for v_id in to_process:
        if fetched >= detail_budget:
            break
        if not db[v_id].get("description") and not DRY_RUN:
            detail = get_video_detail(v_id, cookie_file)
            if detail:
                db[v_id].update(detail)
                fetched += 1

    if fetched:
        print(f"yt-dlp: fetched details for {fetched} videos.")

    # ── 6. Gemini polish in batches with deduplication
    used_titles: set[str] = {
        db[vid]["title"]
        for vid in db
        if db[vid].get("last_refreshed", "2000-01-01") != "2000-01-01"
    }

    refreshed = 0
    for i in range(0, len(to_process), BATCH_SIZE):
        batch_ids = to_process[i:i + BATCH_SIZE]
        batch = [{"vid": v, "title": db[v]["title"]} for v in batch_ids]

        new_titles = ai_polish_batch(batch, used_titles)

        for v_id, new_title in zip(batch_ids, new_titles):
            db[v_id]["title"] = new_title
            db[v_id]["last_refreshed"] = today_str
            used_titles.add(new_title)
            refreshed += 1

        save_db(db)

        if i + BATCH_SIZE < len(to_process):
            print(f"  Batch {i // BATCH_SIZE + 1} done. Sleeping {COOLDOWN}s…")
            time.sleep(COOLDOWN)

    print(f"SEO refreshed: {refreshed}")

    # ── 7. Build site
    polished = [
        (vid, data) for vid, data in db.items()
        if data.get("last_refreshed", "2000-01-01") != "2000-01-01"
    ]
    polished.sort(key=lambda x: x[1]["last_refreshed"], reverse=True)

    total_pages = build_index(polished)
    build_video_pages(polished)
    build_topic_pages(polished)
    build_sitemap(polished, total_pages)
    build_robots()

    seo_pages = len(polished)
    total_site_pages = seo_pages + total_pages + len(TOPICS)
    print(f"Built {SITE_DIR}/ with {seo_pages} SEO pages across {total_site_pages} total pages.")

    # clean up temp cookie file
    if cookie_file:
        try:
            os.unlink(cookie_file)
        except Exception:
            pass


if __name__ == "__main__":
    run_rolling_cycle()
