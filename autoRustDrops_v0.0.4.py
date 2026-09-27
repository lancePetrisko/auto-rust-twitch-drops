import requests
from bs4 import BeautifulSoup
import webbrowser
import time
import json
import os
import html
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
DASHBOARD_PATH = os.path.join(BASE_DIR, "dashboard.html")

DEFAULT_CONFIG = {
    "recheck_interval_minutes": 30,   # how long to wait before rechecking when no one is eligible/online
    "no_progress_wait_minutes": 15,   # how long to watch a streamer with no drop progress info yet
    "page_refresh_seconds": 15,       # how often the dashboard page reloads itself
}

# Load user config, filling in any missing keys with defaults
def load_config():
    config = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            config.update(json.load(f))
    else:
        with open(CONFIG_PATH, "w") as f:
            json.dump(config, f, indent=4)
    return config

# Grabs all streamer links
def get_streamer_links():
    url = 'https://twitch.facepunch.com/#get-started'
    response = requests.get(url)
    soup = BeautifulSoup(response.text, 'html.parser')

    streamer_cards = soup.select(".drop-box")

    streamers = []
    for card in streamer_cards:
        is_live_card = "is-live" in card.get("class", [])

        # Card can pair two streamers under one box (e.g. co-hosted drop).
        # Only the one matching drop-box-body's href is the actual live
        # stream; the other is just listed alongside, not hosting anyone.
        body_tag = card.select_one(".drop-box-body")
        target_link = body_tag['href'] if body_tag and body_tag.has_attr('href') else None

        for info_tag in card.select(".drop-box-header a.streamer-info"):
            name_tag = info_tag.select_one(".streamer-name")
            if not name_tag:
                continue

            name = name_tag.text.strip()
            link = info_tag['href']

            if not is_live_card:
                status = "OFFLINE"
            elif target_link is None or link == target_link:
                status = "ONLINE"
            else:
                status = "PAIRED"  # listed alongside the live streamer, not live itself

            streamers.append((name, link, status))

    return streamers

# Check if some drops are completed
def get_completed_streamers():
    url = 'https://www.twitch.tv/drops/inventory'
    response = requests.get(url)
    soup = BeautifulSoup(response.text, 'html.parser')

    completed = set()
    in_progress = {}  # {streamer: ("45%", minutes_remaining)}

    default_minutes = 120  # 2 hours total

    status_tags = soup.select(".ScCardDropStatusText")
    for tag in status_tags:
        parent = tag.find_parent("div", class_="ScCardDropCard")
        if not parent:
            continue
        streamer_tag = parent.select_one(".ScCardDropCampaignTitleText")
        if not streamer_tag:
            continue

        name = streamer_tag.text.strip()
        title = tag.get("title", "").strip()

        if title == "Completed":
            completed.add(name)
        elif "%" in title:
            percent = int(title.split("%")[0])
            remaining = default_minutes * (100 - percent) / 100
            in_progress[name] = (f"{percent}%", int(remaining))

    return completed, in_progress

# Renders the dashboard to dashboard.html
def render_dashboard(streamers, completed, in_progress, watching, config):
    online = [s for s in streamers if s[2] == "ONLINE"]

    if watching:
        status_html = f'<div class="status status-online">● WATCHING {html.escape(watching)}</div>'
    elif online:
        status_html = '<div class="status status-online">● STREAMERS ONLINE</div>'
    else:
        status_html = '<div class="status status-offline">● NO ELIGIBLE STREAMERS</div>'

    badge_by_status = {
        "ONLINE": ("badge-online", "ONLINE"),
        "PAIRED": ("badge-hosted", "HOSTED"),
        "OFFLINE": ("badge-offline", "OFFLINE"),
    }

    rows = []
    for name, link, status in streamers:
        badge_class, badge_text = badge_by_status[status]
        rows.append(
            f'<tr><td>{html.escape(name)}</td>'
            f'<td><a href="{html.escape(link)}" target="_blank">{html.escape(link)}</a></td>'
            f'<td><span class="badge {badge_class}">{badge_text}</span></td></tr>'
        )
    streamer_rows = "\n".join(rows) or '<tr><td colspan="3">No streamers found.</td></tr>'

    progress_rows = []
    for name, (percent, minutes) in in_progress.items():
        pct = int(percent.rstrip("%"))
        progress_rows.append(f'''
        <div class="progress-item">
            <div class="progress-label"><span>{html.escape(name)}</span><span>{percent} — {minutes} min left</span></div>
            <div class="progress-bar"><div class="progress-fill" style="width:{pct}%;"></div></div>
        </div>''')
    for name in completed:
        progress_rows.append(
            f'<div class="progress-item completed"><span>✓ {html.escape(name)}</span><span>Completed</span></div>'
        )
    progress_html = "\n".join(progress_rows) or '<p class="muted">No drop progress yet.</p>'

    refresh_seconds = config["page_refresh_seconds"]
    updated = datetime.now().strftime("%H:%M:%S")

    html_doc = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="refresh" content="{refresh_seconds}">
<title>Rust Twitch Drops Watcher</title>
<link rel="stylesheet" href="style.css">
</head>
<body>
<div class="container">
    <h1>Rust Twitch Drops Watcher</h1>
    <div class="py-status"><span class="py-dot"></span>Python watcher running</div>
    {status_html}
    <p class="muted">Last updated {updated} &middot; refreshes every {refresh_seconds}s &middot; edit config.json to change timing</p>

    <div class="panel">
        <h2>Drop Progress</h2>
        {progress_html}
    </div>

    <div class="panel">
        <h2>Streamers</h2>
        <table>
            <tr><th>Name</th><th>Link</th><th>Status</th></tr>
            {streamer_rows}
        </table>
    </div>
</div>
</body>
</html>'''

    with open(DASHBOARD_PATH, "w", encoding="utf-8") as f:
        f.write(html_doc)

# Main loop: scrape, auto-open eligible streams, keep the dashboard updated
def main():
    config = load_config()
    print(f"Dashboard: {DASHBOARD_PATH}")
    webbrowser.open(f"file://{DASHBOARD_PATH}")
    webbrowser.open("https://www.twitch.tv/drops/inventory")

    watching = None
    watch_until = 0

    while True:
        config = load_config()  # re-read so edits to config.json apply without restarting
        streamers = get_streamer_links()
        completed, in_progress = get_completed_streamers()

        if watching and time.time() >= watch_until:
            watching = None

        if not watching:
            for name, link, status in streamers:
                if status == "ONLINE" and name not in completed:
                    print(f"\n Opening {name}'s stream: {link}")
                    webbrowser.open(link)
                    watching = name
                    minutes = in_progress[name][1] if name in in_progress else config["no_progress_wait_minutes"]
                    watch_until = time.time() + minutes * 60
                    break

        render_dashboard(streamers, completed, in_progress, watching, config)

        if watching:
            time.sleep(60)
        else:
            print("\n No eligible streamers online. Check the dashboard for live status.")
            time.sleep(config["recheck_interval_minutes"] * 60)


if __name__ == "__main__":
    main()
