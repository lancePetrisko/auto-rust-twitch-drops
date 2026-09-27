import requests
from bs4 import BeautifulSoup
import webbrowser
import time
import json
import os
import html
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
CLAIMED_PATH = os.path.join(BASE_DIR, "claimed.json")
DASHBOARD_PATH = os.path.join(BASE_DIR, "dashboard.html")
STYLE_PATH = os.path.join(BASE_DIR, "style.css")

PORT = 8787

DEFAULT_CONFIG = {
    "recheck_interval_minutes": 30,   # how long to wait before rechecking when no one is eligible/online
    "no_progress_wait_minutes": 15,   # how long to watch a streamer with no drop progress info yet
    "page_refresh_seconds": 15,       # how often the dashboard page reloads itself
}

# Shared state between the polling loop (main thread) and the HTTP handler
state_lock = threading.Lock()
state = {"claimed": set(), "watching": None, "watch_until": 0}

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

# Load the set of streamers the user has manually marked as claimed
def load_claimed():
    if os.path.exists(CLAIMED_PATH):
        with open(CLAIMED_PATH) as f:
            return set(json.load(f))
    return set()

def save_claimed(claimed):
    with open(CLAIMED_PATH, "w") as f:
        json.dump(sorted(claimed), f, indent=4)

# Grabs all streamer links
def get_streamer_links():
    url = 'https://twitch.facepunch.com/#get-started'
    response = requests.get(url)
    soup = BeautifulSoup(response.text, 'html.parser')

    streamer_cards = soup.select(".drop-box")

    streamers = []
    team_id = 0
    for card in streamer_cards:
        is_live_card = "is-live" in card.get("class", [])

        # Card can pair two streamers under one box (e.g. co-hosted drop).
        # Only the one matching drop-box-body's href is the actual live
        # stream; the other is just listed alongside, not hosting anyone.
        body_tag = card.select_one(".drop-box-body")
        target_link = body_tag['href'] if body_tag and body_tag.has_attr('href') else None

        drop_type_tag = card.select_one(".drop-box-footer .drop-type")
        drop_type = drop_type_tag.text.strip() if drop_type_tag else ""

        card_streamers = []
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

            card_streamers.append((name, link, status))

        if not card_streamers:
            continue

        # Each box's streamer(s) are grouped into the same team - they
        # share one drop reward, so they belong together on the dashboard.
        for name, link, status in card_streamers:
            streamers.append((name, link, status, team_id, drop_type))
        team_id += 1

    return streamers

# Check if some drops are completed
def get_completed_streamers():
    url = 'https://www.twitch.tv/drops/inventory'
    response = requests.get(url)
    soup = BeautifulSoup(response.text, 'html.parser')

    completed = set()      # 100% watched, still needs a manual claim confirm
    auto_claimed = set()   # Twitch's own page already shows this as claimed/collected
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

        if "claimed" in title.lower():
            auto_claimed.add(name)
        elif title == "Completed":
            completed.add(name)
        elif "%" in title:
            percent = int(title.split("%")[0])
            remaining = default_minutes * (100 - percent) / 100
            in_progress[name] = (f"{percent}%", int(remaining))

    return completed, in_progress, auto_claimed

# Renders the dashboard to dashboard.html
def render_dashboard(streamers, completed, in_progress, watching, claimed, config):
    # Claimed streamers drop off the main watch list entirely.
    visible_streamers = [s for s in streamers if s[0] not in claimed]
    online = [s for s in visible_streamers if s[2] == "ONLINE"]

    # Group streamers back into the teams they share a drop-box with.
    teams = {}
    for name, link, status, team_id, drop_type in visible_streamers:
        teams.setdefault(team_id, {"members": [], "drop_type": drop_type})
        teams[team_id]["members"].append((name, link, status))

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
    for team_id in sorted(teams):
        members = teams[team_id]["members"]
        drop_type = teams[team_id]["drop_type"] or "—"
        group_class = "team-group" if len(members) > 1 else ""
        for i, (name, link, status) in enumerate(members):
            badge_class, badge_text = badge_by_status[status]
            classes = " ".join(c for c in (group_class, "team-start" if i == 0 else "") if c)
            rows.append(
                f'<tr class="{classes}"><td>{html.escape(name)}</td>'
                f'<td><a href="{html.escape(link)}" target="_blank">{html.escape(link)}</a></td>'
                f'<td>{html.escape(drop_type)}</td>'
                f'<td><span class="badge {badge_class}">{badge_text}</span></td>'
                f'<td class="done-cell">'
                f'<form method="POST" action="/claim">'
                f'<input type="hidden" name="name" value="{html.escape(name)}">'
                f'<input type="checkbox" title="Mark done" onchange="this.form.submit()">'
                f'</form></td></tr>'
            )
    streamer_rows = "\n".join(rows) or '<tr><td colspan="5">No streamers found.</td></tr>'

    progress_rows = []
    for name, (percent, minutes) in in_progress.items():
        if name in claimed:
            continue
        pct = int(percent.rstrip("%"))
        progress_rows.append(f'''
        <div class="progress-item">
            <div class="progress-label"><span>{html.escape(name)}</span><span>{percent} — {minutes} min left</span></div>
            <div class="progress-bar"><div class="progress-fill" style="width:{pct}%;"></div></div>
        </div>''')

    for name in completed:
        if name in claimed:
            continue
        progress_rows.append(f'''
        <div class="progress-item claim-pending">
            <span>{html.escape(name)} — drop complete, ready to claim</span>
            <form method="POST" action="/claim">
                <input type="hidden" name="name" value="{html.escape(name)}">
                <button type="submit">Mark Claimed</button>
            </form>
        </div>''')

    progress_html = "\n".join(progress_rows) or '<p class="muted">No drop progress yet.</p>'

    claimed_rows = []
    for name in sorted(claimed):
        claimed_rows.append(f'''
        <div class="progress-item completed">
            <span>✓ {html.escape(name)}</span>
            <form method="POST" action="/unclaim">
                <input type="hidden" name="name" value="{html.escape(name)}">
                <button type="submit" class="undo">Undo</button>
            </form>
        </div>''')
    claimed_html = "\n".join(claimed_rows) or '<p class="muted">Nothing claimed yet.</p>'

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
        <h2>Claimed</h2>
        {claimed_html}
    </div>

    <div class="panel">
        <h2>Streamers</h2>
        <table>
            <tr><th>Name</th><th>Link</th><th>Drop</th><th>Status</th><th>Done</th></tr>
            {streamer_rows}
        </table>
    </div>
</div>
</body>
</html>'''

    with open(DASHBOARD_PATH, "w", encoding="utf-8") as f:
        f.write(html_doc)

# Serves the dashboard and handles Claim / Undo form submissions
class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # keep the console quiet

    def do_GET(self):
        if self.path in ("/", "/dashboard.html"):
            self._serve_file(DASHBOARD_PATH, "text/html")
        elif self.path == "/style.css":
            self._serve_file(STYLE_PATH, "text/css")
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path not in ("/claim", "/unclaim"):
            self.send_response(404)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8")
        name = parse_qs(body).get("name", [""])[0]

        with state_lock:
            if self.path == "/claim":
                state["claimed"].add(name)
            else:
                state["claimed"].discard(name)
            save_claimed(state["claimed"])

        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()

    def _serve_file(self, path, content_type):
        try:
            with open(path, "rb") as f:
                data = f.read()
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

# Scrapes, auto-opens eligible streams, and keeps the dashboard updated
def poll_loop():
    while True:
        config = load_config()
        streamers = get_streamer_links()
        completed, in_progress, auto_claimed = get_completed_streamers()

        with state_lock:
            if auto_claimed - state["claimed"]:
                state["claimed"] |= auto_claimed
                save_claimed(state["claimed"])
            claimed = set(state["claimed"])
            watching = state["watching"]
            watch_until = state["watch_until"]

        if watching and time.time() >= watch_until:
            watching = None

        if not watching:
            for name, link, status, _team_id, _drop_type in streamers:
                if status == "ONLINE" and name not in completed and name not in claimed:
                    print(f"\n Opening {name}'s stream: {link}")
                    webbrowser.open(link)
                    watching = name
                    minutes = in_progress[name][1] if name in in_progress else config["no_progress_wait_minutes"]
                    watch_until = time.time() + minutes * 60
                    break

        with state_lock:
            state["watching"] = watching
            state["watch_until"] = watch_until
            claimed = set(state["claimed"])

        render_dashboard(streamers, completed, in_progress, watching, claimed, config)

        time.sleep(60 if watching else config["recheck_interval_minutes"] * 60)


def main():
    with state_lock:
        state["claimed"] = load_claimed()
        claimed = set(state["claimed"])

    config = load_config()
    render_dashboard([], set(), {}, None, claimed, config)

    server = ThreadingHTTPServer(("127.0.0.1", PORT), DashboardHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    url = f"http://127.0.0.1:{PORT}/"
    print(f"Dashboard: {url}")
    webbrowser.open(url)
    webbrowser.open("https://www.twitch.tv/drops/inventory")

    poll_loop()


if __name__ == "__main__":
    main()
