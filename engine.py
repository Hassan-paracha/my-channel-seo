import os
import yt_dlp
from google import genai
import json

# --- CONFIGURATION ---
CHANNEL_HANDLE = "@HassanParacha-c3g"  # Change to your actual handle
PUBLIC_SITE_URL = "https://Hassan-paracha.github.io/my-seo-hub/"

def get_latest_content():
    base_url = f"https://www.youtube.com/@HassanParacha-c3g"
    tabs = ["/videos", "/shorts", "/streams"]
    latest_items = []
    
    ydl_opts = {
        'quiet': True,
        'extract_flat': True,
        'force_generic_extractor': True,
    }
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        for tab in tabs:
            try:
                result = ydl.extract_info(base_url + tab, download=False)
                if 'entries' in result and len(result['entries']) > 0:
                    item = result['entries'][0]
                    item['content_type'] = tab.replace("/", "").capitalize()
                    latest_items.append(item)
            except:
                continue
    return latest_items

def polish_metadata(item):
    # Initialize the new Google GenAI Client
    # It automatically picks up 'GEMINI_API_KEY' from environment variables
    client = genai.Client()
    
    raw_title = item.get('title', 'YouTube Content')
    c_type = item['content_type']
    
    prompt = f"""
    You are an SEO expert for a Pakistani YouTube channel.
    Type: {c_type}
    Hinglish Title: {raw_title}
    
    Task:
    1. Fix Roman Urdu spellings.
    2. Make it catchy for a Pakistani family audience.
    3. Add 2 English SEO keywords.
    4. Return ONLY the new title text.
    """
    
    try:
        # Using the modern Gemini 3 Flash model
        response = client.models.generate_content(
            model="gemini-3-flash-preview",
            contents=prompt
        )
        return response.text.strip().replace('"', '')
    except Exception as e:
        print(f"AI Error: {e}")
        return raw_title

def generate_hub(items):
    cards_html = ""
    for item in items:
        new_title = polish_metadata(item)
        v_id = item['id']
        # Fix for Shorts URL structure
        v_url = f"https://www.youtube.com/watch?v={v_id}" if "/shorts/" not in item.get('url', '') else item.get('url', f"https://www.youtube.com/shorts/{v_id}")
        
        # Schema for Google Search
        schema = {
            "@context": "https://schema.org",
            "@type": "VideoObject",
            "name": new_title,
            "description": f"Latest {item['content_type']} from our Pakistani Family Channel.",
            "thumbnailUrl": f"https://i.ytimg.com/vi/{v_id}/maxresdefault.jpg",
            "uploadDate": "2026-02-19",
            "embedUrl": f"https://www.youtube.com/embed/{v_id}"
        }

        cards_html += f"""
        <div class="card">
            <script type="application/ld+json">{json.dumps(schema)}</script>
            <div class="badge">{item['content_type']}</div>
            <h2>{new_title}</h2>
            <div class="video-container">
                <iframe src="https://www.youtube.com/embed/{v_id}" frameborder="0" allowfullscreen></iframe>
            </div>
            <a class="btn" href="{v_url}" target="_blank">Watch on YouTube</a>
        </div>
        """

    full_html = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Family Entertainment Hub</title>
        <style>
            body {{ font-family: 'Segoe UI', sans-serif; background: #fafafa; color: #333; margin: 0; padding: 20px; display: flex; flex-wrap: wrap; justify-content: center; }}
            .card {{ background: #fff; border: 1px solid #ddd; border-radius: 12px; width: 340px; margin: 15px; padding: 15px; box-shadow: 0 4px 6px rgba(0,0,0,0.05); }}
            h2 {{ font-size: 1.1rem; margin: 10px 0; height: 3em; overflow: hidden; }}
            .badge {{ display: inline-block; background: #ff0000; color: #fff; padding: 4px 10px; border-radius: 20px; font-size: 11px; font-weight: bold; }}
            .video-container {{ position: relative; padding-bottom: 56.25%; height: 0; border-radius: 8px; overflow: hidden; background: #000; }}
            .video-container iframe {{ position: absolute; top: 0; left: 0; width: 100%; height: 100%; }}
            .btn {{ display: block; margin-top: 15px; background: #065fd4; color: white; text-decoration: none; padding: 10px; border-radius: 6px; text-align: center; font-weight: bold; }}
        </style>
    </head>
    <body>
        {cards_html}
    </body>
    </html>
    """
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(full_html)

if __name__ == "__main__":
    content = get_latest_content()
    if content:
        generate_hub(content)
