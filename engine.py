import os, yt_dlp, google.generativeai as genai

# 1. Scraper that understands Videos, Shorts, and Streams
def get_all_content():
    base_url = "https://www.youtube.com/@HassanParacha-c3g"
    # We check all three potential tabs
    tabs = ["/videos", "/shorts", "/streams"]
    all_entries = []
    
    with yt_dlp.YoutubeDL({'quiet': True, 'extract_flat': True}) as ydl:
        for tab in tabs:
            try:
                result = ydl.extract_info(base_url + tab, download=False)
                if 'entries' in result and result['entries']:
                    # Take the latest item from each tab
                    all_entries.append(result['entries'][0])
            except:
                continue # Skip if a tab is empty (e.g., no live streams)
    return all_entries

# 2. AI Polisher with Content-Type awareness
def polish(title, url):
    is_short = "/shorts/" in url
    genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
    model = genai.GenerativeModel('gemini-pro')
    
    context = "Shorts" if is_short else "Video/Live"
    prompt = f"Targeting a Pakistani family audience, rewrite this {context} title for SEO. Use clean Roman Urdu and add viral keywords: {title}"
    
    return model.generate_content(prompt).text

# 3. Generating the Hub
entries = get_all_content()
html_list = ""

for entry in entries:
    clean_title = polish(entry['title'], entry['url'])
    html_list += f"""
    <div class='content-card'>
        <h2>{clean_title}</h2>
        <a href='{entry['url']}'>Watch on YouTube</a>
    </div>
    """

with open("index.html", "w") as f:
    f.write(f"<html><body>{html_list}</body></html>")
