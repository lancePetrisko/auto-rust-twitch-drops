import requests
from bs4 import BeautifulSoup
import webbrowser
import time
import json
import os
import html
import threading
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
CLAIMED_PATH = os.path.join(BASE_DIR, "claimed.json")
DASHBOARD_PATH = os.path.join(BASE_DIR, "dashboard.html")
STYLE_PATH = os.path.join(BASE_DIR, "style.css")
LOGS_PATH = os.path.join(BASE_DIR, "logs.html")

PORT = 8787
LOG_HISTORY_LIMIT = 300

DEFAULT_CONFIG = {
    "recheck_interval_minutes": 30,   # how long to wait before rechecking when no one is eligible/online
    "no_progress_wait_minutes": 15,   # how long to watch a streamer with no drop progress info yet
    "page_refresh_seconds": 15,       # how often the dashboard page reloads itself
}

# Shared state between the polling loop (main thread) and the HTTP handler
state_lock = threading.Lock()
state = {
    "claimed": set(),
    "watching": None,
    "watch_until": 0,
    "completed": set(),
    "in_progress": {},
    "session_claimed_count": 0,
    "log": deque(maxlen=LOG_HISTORY_LIMIT),
    "run_state": "running",  # running | paused | stopped
}

# Wakes poll_loop immediately when the run state changes, instead of it
# only noticing after its current sleep (which can be up to 30 min)
control_event = threading.Event()

# Records a line to both the console and the in-memory log the Logs page reads
def log_event(message):
    timestamp = datetime.now().strftime("%I:%M:%S %p").lstrip("0")
    with state_lock:
        state["log"].append({"time": timestamp, "message": message})
    print(f"[{timestamp}] {message}")

# Applies a Start / Pause / Stop request from the Logs page
def request_control(action):
    state_by_action = {
        "start": "running",
        "pause": "paused",
        "stop": "stopped",
    }
    verb_by_action = {
        "start": "started",
        "pause": "paused",
        "stop": "stopped",
    }
    new_state = state_by_action.get(action)
    if new_state is None:
        return

    with state_lock:
        if state["run_state"] == new_state:
            return
        state["run_state"] = new_state
        if new_state == "stopped":
            state["watching"] = None
            state["watch_until"] = 0

    log_event(f"Watcher {verb_by_action[action]} by user")
    control_event.set()

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

        image_tag = card.select_one(".drop-box-body img")
        drop_image = image_tag['src'] if image_tag and image_tag.has_attr('src') else ""

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
            streamers.append((name, link, status, team_id, drop_type, drop_image))
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
    for name, link, status, team_id, drop_type, drop_image in visible_streamers:
        teams.setdefault(team_id, {"members": [], "drop_type": drop_type, "drop_image": drop_image})
        teams[team_id]["members"].append((name, link, status))

    # Then group those teams by the item they're giving drops for, so every
    # streamer handing out the same item lands together on the dashboard.
    groups = {}
    for team_id in sorted(teams):
        drop_type = teams[team_id]["drop_type"] or "Unknown Drop"
        group = groups.setdefault(drop_type, {"image": teams[team_id]["drop_image"], "team_ids": []})
        group["team_ids"].append(team_id)

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
    for drop_type, group in groups.items():
        image_html = (
            f'<img src="{html.escape(group["image"])}" alt="{html.escape(drop_type)}" class="drop-thumb">'
            if group["image"] else ""
        )
        rows.append(
            f'<tr class="drop-group-header"><td colspan="4">'
            f'{image_html}<span class="drop-group-title">{html.escape(drop_type)}</span>'
            f'</td></tr>'
        )
        for team_id in group["team_ids"]:
            members = teams[team_id]["members"]
            group_class = "team-group" if len(members) > 1 else ""
            for i, (name, link, status) in enumerate(members):
                badge_class, badge_text = badge_by_status[status]
                classes = " ".join(c for c in (group_class, "team-start" if i == 0 else "") if c)
                rows.append(
                    f'<tr class="{classes}" data-status="{status}"><td>{html.escape(name)}</td>'
                    f'<td><a href="{html.escape(link)}" target="_blank">{html.escape(link)}</a></td>'
                    f'<td><span class="badge {badge_class}">{badge_text}</span></td>'
                    f'<td class="done-cell">'
                    f'<form method="POST" action="/claim">'
                    f'<input type="hidden" name="name" value="{html.escape(name)}">'
                    f'<input type="checkbox" title="Mark done" onchange="this.form.submit()">'
                    f'</form></td></tr>'
                )
    streamer_rows = "\n".join(rows) or '<tr><td colspan="4">No streamers found.</td></tr>'

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
    updated = datetime.now().strftime("%I:%M:%S %p").lstrip("0")

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
    <div class="nav"><a href="/" class="active">Dashboard</a><a href="/logs.html">Logs</a></div>
    <div class="py-status"><span class="py-dot"></span>Python watcher running</div>
    <a class="btn-open-twitch" href="https://www.twitch.tv/drops/inventory" target="_blank">Open Twitch Drops Inventory</a>
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
        <div class="panel-header">
            <h2>Streamers</h2>
            <button id="sort-online-btn" class="btn-sort" onclick="toggleSortOnline()">Sort: Online First</button>
        </div>
        <table class="streamer-table">
            <tr><th>Name</th><th>Link</th><th>Status</th><th>Done</th></tr>
            {streamer_rows}
        </table>
    </div>
</div>
<script>
function statusPriority(s) {{
    return s === "ONLINE" ? 0 : s === "PAIRED" ? 1 : 2;
}}

function collectBlocks(table) {{
    const rows = Array.from(table.querySelectorAll("tr"));
    const headerRow = rows[0];
    const blocks = [];
    let current = null;
    rows.forEach((row) => {{
        if (row === headerRow) return;
        if (row.classList.contains("drop-group-header")) {{
            current = {{ header: row, members: [] }};
            blocks.push(current);
        }} else if (current) {{
            current.members.push(row);
        }}
    }});
    return {{ headerRow, blocks }};
}}

function applySort() {{
    const table = document.querySelector(".streamer-table");
    if (!table) return;
    const sortOnline = localStorage.getItem("rustDropsSortOnline") === "1";
    const {{ headerRow, blocks }} = collectBlocks(table);
    blocks.forEach((b, i) => {{ b.originalIndex = i; }});
    const ordered = sortOnline
        ? blocks.slice().sort((a, b) => {{
              const pa = Math.min(...a.members.map((m) => statusPriority(m.dataset.status)));
              const pb = Math.min(...b.members.map((m) => statusPriority(m.dataset.status)));
              return pa - pb || a.originalIndex - b.originalIndex;
          }})
        : blocks;
    ordered.forEach((b) => {{
        table.appendChild(b.header);
        b.members.forEach((m) => table.appendChild(m));
    }});
    const btn = document.getElementById("sort-online-btn");
    if (btn) {{
        btn.textContent = sortOnline ? "Sort: Drop Order" : "Sort: Online First";
        btn.classList.toggle("active", sortOnline);
    }}
}}

function toggleSortOnline() {{
    const sortOnline = localStorage.getItem("rustDropsSortOnline") === "1";
    localStorage.setItem("rustDropsSortOnline", sortOnline ? "0" : "1");
    applySort();
}}

applySort();
</script>
</body>
</html>'''

    with open(DASHBOARD_PATH, "w", encoding="utf-8") as f:
        f.write(html_doc)

# Builds the JSON payload the Logs page polls for live stats + console lines
def build_state_snapshot():
    with state_lock:
        watching = state["watching"]
        watch_until = state["watch_until"]
        claimed = set(state["claimed"])
        completed = set(state["completed"])
        in_progress_count = len(state["in_progress"])
        session_claimed_count = state["session_claimed_count"]
        run_state = state["run_state"]
        logs = list(state["log"])

    remaining = max(0, int(watch_until - time.time())) if watching else 0

    return {
        "logs": logs,
        "watching": watching,
        "watch_remaining_seconds": remaining,
        "ready_to_claim": len(completed - claimed),
        "in_progress_count": in_progress_count,
        "session_claimed_count": session_claimed_count,
        "run_state": run_state,
    }

# Serves the dashboard and handles Claim / Undo form submissions
class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # keep the console quiet

    def do_GET(self):
        if self.path in ("/", "/dashboard.html"):
            self._serve_file(DASHBOARD_PATH, "text/html")
        elif self.path == "/logs.html":
            self._serve_file(LOGS_PATH, "text/html")
        elif self.path == "/style.css":
            self._serve_file(STYLE_PATH, "text/css")
        elif self.path == "/api/state":
            self._serve_json(build_state_snapshot())
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path in ("/claim", "/unclaim"):
            self._handle_claim()
        elif self.path in ("/control/start", "/control/pause", "/control/stop"):
            self._handle_control()
        else:
            self.send_response(404)
            self.end_headers()

    def _handle_claim(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8")
        name = parse_qs(body).get("name", [""])[0]

        changed = False
        with state_lock:
            if self.path == "/claim":
                if name not in state["claimed"]:
                    state["claimed"].add(name)
                    state["session_claimed_count"] += 1
                    changed = True
            else:
                if name in state["claimed"]:
                    state["claimed"].discard(name)
                    state["session_claimed_count"] = max(0, state["session_claimed_count"] - 1)
                    changed = True
            save_claimed(state["claimed"])

        if changed:
            action = "Marked" if self.path == "/claim" else "Unmarked"
            log_event(f"{action} {name} as claimed (manual)")

        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()

    def _handle_control(self):
        action = self.path.rsplit("/", 1)[-1]
        request_control(action)

        self.send_response(303)
        self.send_header("Location", self.headers.get("Referer", "/logs.html"))
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

    def _serve_json(self, data):
        body = json.dumps(data).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

# Scrapes, auto-opens eligible streams, and keeps the dashboard updated
def poll_loop():
    while True:
        with state_lock:
            run_state = state["run_state"]

        if run_state != "running":
            control_event.wait(timeout=1)
            control_event.clear()
            continue

        config = load_config()

        try:
            streamers = get_streamer_links()
        except Exception as e:
            log_event(f"ERROR fetching streamer data from facepunch: {e}")
            streamers = []

        try:
            completed, in_progress, auto_claimed = get_completed_streamers()
        except Exception as e:
            log_event(f"ERROR fetching drop inventory from twitch: {e}")
            completed, in_progress, auto_claimed = set(), {}, set()

        with state_lock:
            newly_auto_claimed = auto_claimed - state["claimed"]
            if newly_auto_claimed:
                state["claimed"] |= newly_auto_claimed
                state["session_claimed_count"] += len(newly_auto_claimed)
                save_claimed(state["claimed"])
            claimed = set(state["claimed"])
            watching = state["watching"]
            watch_until = state["watch_until"]
            prev_completed = set(state["completed"])
            state["completed"] = set(completed)
            state["in_progress"] = dict(in_progress)

        for name in sorted(newly_auto_claimed):
            log_event(f"Twitch auto-claimed the drop for {name}")

        newly_completed = (completed - claimed) - prev_completed
        for name in sorted(newly_completed):
            log_event(f"{name}'s drop hit 100% — ready to claim")

        if watching and time.time() >= watch_until:
            log_event(f"Done watching {watching} — watch window elapsed")
            watching = None

        if not watching:
            switched = False
            for name, link, status, _team_id, _drop_type, _drop_image in streamers:
                if status == "ONLINE" and name not in completed and name not in claimed:
                    minutes = in_progress[name][1] if name in in_progress else config["no_progress_wait_minutes"]
                    log_event(f"Switching to {name} — watching for {minutes} min ({link})")
                    webbrowser.open(link)
                    watching = name
                    watch_until = time.time() + minutes * 60
                    switched = True
                    break
            if not switched:
                log_event("No eligible streamers online — waiting to recheck")

        with state_lock:
            state["watching"] = watching
            state["watch_until"] = watch_until
            claimed = set(state["claimed"])

        render_dashboard(streamers, completed, in_progress, watching, claimed, config)

        sleep_seconds = 60 if watching else config["recheck_interval_minutes"] * 60
        control_event.wait(timeout=sleep_seconds)
        control_event.clear()


def main():
    log_event("Starting Rust Twitch Drops Watcher...")

    try:
        config = load_config()
        log_event(
            f"Loaded config.json (recheck every {config['recheck_interval_minutes']}m, "
            f"no-progress wait {config['no_progress_wait_minutes']}m)"
        )
    except Exception as e:
        log_event(f"ERROR loading config.json, using defaults: {e}")
        config = dict(DEFAULT_CONFIG)

    try:
        loaded_claimed = load_claimed()
        claimed_error = None
    except Exception as e:
        loaded_claimed = set()
        claimed_error = e

    with state_lock:
        state["claimed"] = loaded_claimed
        claimed = set(state["claimed"])

    if claimed_error is not None:
        log_event(f"ERROR loading claimed.json, starting empty: {claimed_error}")
    else:
        log_event(f"Loaded {len(claimed)} previously claimed streamer(s)")

    log_event("Rendering initial dashboard...")
    render_dashboard([], set(), {}, None, claimed, config)

    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), DashboardHandler)
    except OSError as e:
        log_event(f"ERROR starting HTTP server on port {PORT}: {e}")
        raise
    threading.Thread(target=server.serve_forever, daemon=True).start()

    url = f"http://127.0.0.1:{PORT}/"
    log_event(f"Dashboard server listening at {url}")
    webbrowser.open(url)
    log_event("Watcher started")

    poll_loop()


if __name__ == "__main__":
    main()
