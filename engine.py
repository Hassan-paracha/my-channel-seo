import os
import yt_dlp
from google import genai
import json
import time
from datetime import datetime

# --- CONFIGURATION ---
CHANNEL_HANDLE = "@HassanParacha-c3g" 
DB_FILE = "video_db.json"
DAILY_INDEX_LIMIT = 180  # Your safe limit
BATCH_SIZE = 5          # Your chunk size
COOLDOWN = 65           # Seconds to wait to reset AI limit

def load_db():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r") as f:
                return json.load(f)
        except: return {}
    return {}

def save_db(db):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=4)

def get_channel_videos():
    """DEEP SCRAPE: Specifically targets Videos, Shorts, and Live tabs."""
    all_entries = []
    # We check all three main content tabs
    urls = [
        f"https://www.youtube.com/{CHANNEL_HANDLE}/videos",
        f"https://www.youtube.com/{CHANNEL_HANDLE}/shorts",
        f"https://www.youtube.com/{CHANNEL_HANDLE}/streams"
    ]
    
    ydl_opts = {
        'quiet': True, 
        'extract_flat': True,
        'playlist_items': '1:200' # Pulls up to 200 from EACH tab
    }
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        for url in urls:
            try:
                print(f"Scraping tab: {url}")
                result = ydl.extract_info(url, download=False)
                if 'entries' in result:
                    all_entries.extend(result['entries'])
            except Exception as e:
                print(f"Tab skip: {url} (May be empty)")
                continue
    return all_entries

def ai_polish(title):
    client = genai.Client()
    # SEO Prompt optimized for 2026 Pakistani Search Trends
    prompt = f"Optimize this title for Pakistani YouTube SEO. Use Hinglish + English keywords. Return ONLY the title: {title}"
    try:
        response = client.models.generate_content(
            model="gemini-3-flash-preview",
            contents=prompt
        )
        return response.text.strip().replace('"', '')
    except:
        return title

def run_rolling_cycle():
    db = load_db()
    all_videos = get_channel_videos()
    today_str = datetime.now().strftime("%Y-%m-%d")

    # 1. Sync & Catalog
    for v in all_videos:
        v_id = v.get('id')
        if v_id and v_id not in db:
            db[v_id] = {
                "title": v.get('title', 'YouTube Video'),
                "last_refreshed": "2000-01-01",
                "url": f"https://www.youtube.com/watch?v={v_id}"
            }

    # 2. Sort by Stale Date
    sorted_ids = sorted(db.keys(), key=lambda x: db[x].get('last_refreshed', "2000-01-01"))
    to_process = sorted_ids[:DAILY_INDEX_LIMIT]

    print(f"Sync complete. Total library: {len(db)}. Processing {len(to_process)} stale videos today.")

    # 3. Rolling Loop with 180 limit and Chunk of 5
    for i in range(0, len(to_process), BATCH_SIZE):
        batch = to_process[i:i+BATCH_SIZE]
        for v_id in batch:
            print(f"AI Polishing: {db[v_id]['title']}")
            db[v_id]['title'] = ai_polish(db[v_id]['title'])
            db[v_id]['last_refreshed'] = today_str
        
        save_db(db)
        if i + BATCH_SIZE < len(to_process):
            print(f"Chunk finished. Resting {COOLDOWN}s to keep Gemini happy...")
            time.sleep(COOLDOWN)

    # 4. Generate the Hub (Displaying all sorted by newest refresh)
    # This keeps the most recently optimized videos at the top
    sorted_for_display = sorted(db.items(), key=lambda x: x[1]['last_refreshed'], reverse=True)
    
    cards_html = "".join([f"""
        <div class="card">
            <script type="application/ld+json">
            {{"@context":"https://schema.org","@type":"VideoObject","name":"{v['title']}","thumbnailUrl":"https://i.ytimg.com/vi/{vid}/hqdefault.jpg","uploadDate":"{v['last_refreshed']}"}}
            </script>
            <h3>{v['title']}</h3>
            <div class="video-container">
                <iframe src="https://www.youtube.com/embed/{vid}" loading="lazy" frameborder="0"></iframe>
            </div>
            <a class="btn" href="{v['url']}" target="_blank">Watch Video</a>
        </div>""" for vid, v in sorted_for_display])

    # Template (Simple Grid)
    full_html = f"<!DOCTYPE html><html><head><meta charset='UTF-8'><title>Hassan SEO Hub</title><style>body{{font-family:sans-serif;background:#f9f9f9;display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:20px;padding:25px;}}.card{{background:#fff;padding:15px;border-radius:15px;box-shadow:0 5px 15px rgba(0,0,0,0.05);}}.video-container{{position:relative;padding-bottom:56.25%;height:0;}}.video-container iframe{{position:absolute;top:0;left:0;width:100%;height:100%;border-radius:10px;}}.btn{{display:block;text-align:center;background:#ff0000;color:#fff;text-decoration:none;padding:12px;margin-top:10px;border-radius:8px;font-weight:bold;}}h3{{font-size:15px;height:40px;overflow:hidden;}}</style></head><body>{cards_html}</body></html>"
    
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(full_html)

if __name__ == "__main__":
    run_rolling_cycle()
