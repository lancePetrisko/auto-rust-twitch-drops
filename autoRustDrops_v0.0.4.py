import requests
from bs4 import BeautifulSoup
import webbrowser
import time
import json
import os
import re
import html
import threading
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
CLAIMED_PATH = os.path.join(BASE_DIR, "claimed.json")
DASHBOARD_PATH = os.path.join(BASE_DIR, "dashboard.html")
STYLE_PATH = os.path.join(BASE_DIR, "style.css")
LOGS_PATH = os.path.join(BASE_DIR, "logs.html")

PORT = 8787
LOG_HISTORY_LIMIT = 300
REQUEST_TIMEOUT = 10

# Reused across scrape calls so they share pooled connections instead of
# opening a fresh TCP+TLS handshake to facepunch/twitch on every poll.
HTTP_SESSION = requests.Session()

DEFAULT_CONFIG = {
    "recheck_interval_minutes": 30,   # how long to wait before rechecking when no one is eligible/online
    "no_progress_wait_minutes": 15,   # how long to watch a streamer with no drop progress info yet
    "page_refresh_seconds": 15,       # how often the dashboard page reloads itself
    "claimed_max_age_days": 21,       # Twitch claims older than this belong to past campaigns and are ignored
    "twitch_item_map": {},            # manual fixes: {"Twitch item name": "facepunch drop name", or "" to ignore}
}

# Twitch shortens some item names on the inventory page ("Rust Isles DB")
# compared to facepunch ("Double Barrel Shotgun") - these expand the short
# forms so both sides tokenize to the same words before being compared.
ITEM_TOKEN_ALIASES = {
    "db": ["double", "barrel", "shotgun"],
    "sar": ["semi", "automatic", "rifle"],
    "ar": ["assault", "rifle"],
    "sm": ["small"],
    "lg": ["large"],
    "wooden": ["wood"],
}

# Words too generic to identify a drop on their own ("Rustoria Med Box"
# shouldn't match "Small Box" just because both are boxes)
GENERIC_ITEM_TOKENS = {"box", "door", "rifle", "hat"}

# Stale extension data gets flagged in the log after this long
TWITCH_STALE_SECONDS = 15 * 60

# Shared state between the polling loop (main thread) and the HTTP handler
state_lock = threading.Lock()
state = {
    "claimed": set(),
    "watching": None,
    "watch_until": 0,
    "completed": set(),
    "in_progress": {},
    "streamers": [],
    "team_by_name": {},
    "members_by_team": {},
    "drop_types": set(),
    "twitch_payload": None,   # last raw inventory snapshot from the browser extension
    "twitch_synced_at": 0,
    "twitch_stale_logged": False,
    "unmatched_logged": set(),
    "config": dict(DEFAULT_CONFIG),
    "session_claimed_count": 0,
    "log": deque(maxlen=LOG_HISTORY_LIMIT),
    "log_seq": 0,
    "run_state": "running",  # running | paused | stopped
    "dashboard_fragments": {},
}

# Wakes poll_loop immediately when the run state changes, instead of it
# only noticing after its current sleep (which can be up to 30 min)
control_event = threading.Event()

# Records a line to both the console and the in-memory log the Logs page reads
def log_event(message):
    timestamp = datetime.now().strftime("%I:%M:%S %p").lstrip("0")
    with state_lock:
        state["log_seq"] += 1
        state["log"].append({"seq": state["log_seq"], "time": timestamp, "message": message})
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

# Load user config, filling in any missing keys with defaults - cached
# between calls so a poll cycle doesn't re-read+re-parse an unchanged file
_config_cache = {"mtime": None, "value": None}

def load_config():
    if not os.path.exists(CONFIG_PATH):
        config = dict(DEFAULT_CONFIG)
        with open(CONFIG_PATH, "w") as f:
            json.dump(config, f, indent=4)
        _config_cache["mtime"] = os.path.getmtime(CONFIG_PATH)
        _config_cache["value"] = config
        return dict(config)

    mtime = os.path.getmtime(CONFIG_PATH)
    if _config_cache["value"] is not None and _config_cache["mtime"] == mtime:
        return dict(_config_cache["value"])

    config = dict(DEFAULT_CONFIG)
    with open(CONFIG_PATH) as f:
        config.update(json.load(f))
    _config_cache["mtime"] = mtime
    _config_cache["value"] = config
    return dict(config)

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
    response = HTTP_SESSION.get(url, timeout=REQUEST_TIMEOUT)
    soup = BeautifulSoup(response.text, 'html.parser')

    streamer_cards = soup.select(".drop-box")

    streamers = []
    drop_types = set()  # every drop on the page, including general ones with no streamer
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
        if drop_type:
            drop_types.add(drop_type)

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

    return streamers, drop_types

def _item_tokens(name):
    tokens = set()
    for word in re.findall(r"[a-z0-9]+", name.lower()):
        tokens.update(ITEM_TOKEN_ALIASES.get(word, [word]))
    return tokens

# Picks the facepunch drop a Twitch item name refers to by shared words,
# preferring the drop with the fewest extra words. Returns None when there's
# no confident match (nothing specific shared, or a tie).
def match_drop_type(item, drop_types, item_map):
    if item in item_map:
        return item_map[item] or None

    item_tokens = _item_tokens(item)
    best, best_score, tied = None, None, False
    for drop_type in drop_types:
        drop_tokens = _item_tokens(drop_type)
        shared = item_tokens & drop_tokens
        if not shared - GENERIC_ITEM_TOKENS:
            continue
        score = (len(shared), -len(drop_tokens - item_tokens))
        if best_score is None or score > best_score:
            best, best_score, tied = drop_type, score, False
        elif score == best_score:
            tied = True
    return None if tied else best

# Turns a relative Twitch time ("11 hours ago", "yesterday", "2 months ago")
# into days. Returns None if it can't be read, so the item is kept.
def _claimed_age_days(text):
    text = text.lower()
    if "yesterday" in text:
        return 1
    if re.search(r"\b(second|minute|hour)s?\b", text) or "today" in text:
        return 0
    m = re.search(r"\b(\d+|an?)\s+(day|week|month|year)s?\b", text)
    if not m:
        return None
    count = 1 if m.group(1) in ("a", "an") else int(m.group(1))
    return count * {"day": 1, "week": 7, "month": 30, "year": 365}[m.group(2)]

# Maps the extension's raw inventory snapshot onto facepunch streamers.
# In-progress drops name the channel directly; claimed ones only name the
# item, so they're matched to a drop and mark that whole drop's streamers.
def match_twitch_payload(payload, streamers, drop_types, config):
    completed = set()      # 100% watched, still needs a claim on Twitch
    auto_claimed = set()   # Twitch's inventory lists the reward as claimed
    in_progress = {}       # {streamer: ("45%", minutes_remaining)}
    unmatched = set()

    members_by_drop = {}
    name_by_login = {}
    for name, link, _status, _team_id, drop_type, _image in streamers:
        members_by_drop.setdefault(drop_type, set()).add(name)
        name_by_login[link.rstrip("/").rsplit("/", 1)[-1].lower()] = name
        name_by_login.setdefault(name.lower(), name)

    def streamers_for(item):
        drop_type = match_drop_type(item, drop_types, config["twitch_item_map"])
        if drop_type is None:
            unmatched.add(item)
            return set()
        return members_by_drop.get(drop_type, set())

    for drop in payload.get("in_progress", []):
        channel = drop.get("channel", "").lower()
        names = {name_by_login[channel]} if channel in name_by_login else streamers_for(drop.get("item", ""))
        percent = int(drop.get("percent", 0))
        if percent >= 100 or drop.get("claimable"):
            completed |= names
        else:
            total_minutes = drop.get("total_minutes") or 120
            remaining = int(total_minutes * (100 - percent) / 100)
            for name in names:
                in_progress[name] = (f"{percent}%", remaining)

    max_age = config["claimed_max_age_days"]
    for drop in payload.get("claimed", []):
        age = _claimed_age_days(drop.get("age", ""))
        if age is not None and age > max_age:
            continue
        auto_claimed |= streamers_for(drop.get("item", ""))

    return completed, in_progress, auto_claimed, unmatched

# Folds one matched Twitch snapshot into state and logs what changed.
# Returns True if a drop newly completed or got claimed.
def merge_twitch_status(completed, in_progress, auto_claimed, unmatched):
    with state_lock:
        newly_claimed = auto_claimed - state["claimed"]
        if newly_claimed:
            state["claimed"] |= newly_claimed
            state["session_claimed_count"] += len(newly_claimed)
            save_claimed(state["claimed"])
        newly_completed = (completed - state["claimed"]) - state["completed"]
        state["completed"] = set(completed)
        state["in_progress"] = dict(in_progress)
        # Non-Rust rewards (other games' drops) are expected not to match
        new_unmatched = {i for i in unmatched if "rust" in i.lower()} - state["unmatched_logged"]
        state["unmatched_logged"] |= new_unmatched

    for name in sorted(newly_claimed):
        log_event(f"Twitch inventory shows {name} as claimed")
    for name in sorted(newly_completed):
        log_event(f"{name}'s drop hit 100% — ready to claim")
    for item in sorted(new_unmatched):
        log_event(f"Couldn't match Twitch item '{item}' to a facepunch drop — add it to twitch_item_map in config.json")

    return bool(newly_claimed or newly_completed)

# Re-matches the last extension snapshot against current facepunch data and
# re-renders. Used when a new snapshot arrives and by the Re-check button.
def apply_twitch_payload():
    with state_lock:
        payload = state["twitch_payload"]
        streamers = list(state["streamers"])
        drop_types = set(state["drop_types"])
        config = dict(state["config"])

    if payload is None:
        return False

    completed, in_progress, auto_claimed, unmatched = match_twitch_payload(payload, streamers, drop_types, config)
    changed = merge_twitch_status(completed, in_progress, auto_claimed, unmatched)

    with state_lock:
        claimed = set(state["claimed"])
        watching = state["watching"]
    render_dashboard(streamers, completed, in_progress, watching, claimed, config)
    return changed

# Tracks the last content actually written to dashboard.html, so unchanged
# renders (e.g. nothing moved while paused) skip the disk write entirely
_last_render_signature = None

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
                    f'<tr class="{classes}" data-status="{status}" data-team-id="{team_id}"><td>{html.escape(name)}</td>'
                    f'<td><a href="{html.escape(link)}" target="_blank">{html.escape(link)}</a></td>'
                    f'<td><span class="badge {badge_class}">{badge_text}</span></td>'
                    f'<td class="done-cell">'
                    f'<form method="POST" action="/claim">'
                    f'<input type="hidden" name="name" value="{html.escape(name)}">'
                    f'<input type="checkbox" title="Mark done" onchange="submitClaimForm(this.form)">'
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

    # Group claimed streamers the same way as the main table - by the team/
    # drop-box they came from - so the claimed list also shows what skin
    # each one was for, picture included.
    all_teams = {}
    for name, link, status, team_id, drop_type, drop_image in streamers:
        all_teams.setdefault(team_id, {"members": [], "drop_type": drop_type, "drop_image": drop_image})
        all_teams[team_id]["members"].append(name)

    claimed_groups = {}
    known_claimed_names = set()
    for team_id in sorted(all_teams):
        team_members = all_teams[team_id]["members"]
        known_claimed_names.update(team_members)
        claimed_members = [n for n in team_members if n in claimed]
        if not claimed_members:
            continue
        drop_type = all_teams[team_id]["drop_type"] or "Unknown Drop"
        group = claimed_groups.setdefault(drop_type, {"image": all_teams[team_id]["drop_image"], "members": []})
        group["members"].extend(claimed_members)

    # Claimed streamers no longer on the live page (drop rotated out) still
    # need to show up somewhere, just without a known skin/picture.
    orphan_claimed = sorted(n for n in claimed if n not in known_claimed_names)
    if orphan_claimed:
        group = claimed_groups.setdefault("Unknown Drop", {"image": "", "members": []})
        group["members"].extend(orphan_claimed)

    claimed_rows = []
    for drop_type, group in claimed_groups.items():
        image_html = (
            f'<img src="{html.escape(group["image"])}" alt="{html.escape(drop_type)}" class="drop-thumb">'
            if group["image"] else ""
        )
        members_html = "".join(f'''
        <div class="progress-item completed">
            <span>✓ {html.escape(name)}</span>
            <form method="POST" action="/unclaim">
                <input type="hidden" name="name" value="{html.escape(name)}">
                <button type="submit" class="undo">Undo</button>
            </form>
        </div>''' for name in group["members"])
        claimed_rows.append(f'''
        <div class="claimed-group">
            <div class="claimed-group-header">{image_html}<span class="drop-group-title">{html.escape(drop_type)}</span></div>
            {members_html}
        </div>''')
    claimed_html = "\n".join(claimed_rows) or '<p class="muted">Nothing claimed yet.</p>'

    refresh_seconds = config["page_refresh_seconds"]
    updated = datetime.now().strftime("%I:%M:%S %p").lstrip("0")

    with state_lock:
        synced_at = state["twitch_synced_at"]
    if synced_at:
        synced = datetime.fromtimestamp(synced_at).strftime("%I:%M %p").lstrip("0")
        sync_html = f'<span class="py-dot"></span>Twitch synced {synced}'
    else:
        sync_html = '<span class="py-dot py-dot-off"></span>Extension not connected'

    with state_lock:
        state["dashboard_fragments"] = {
            "status_html": status_html,
            "progress_html": progress_html,
            "claimed_html": claimed_html,
            "streamer_rows": streamer_rows,
            "sync_html": sync_html,
            "updated": updated,
        }

    # The full page is only rewritten to disk when the actual content
    # differs from last time - live tabs get updates via /api/dashboard
    # instead, so a static (e.g. paused) state doesn't churn the disk.
    global _last_render_signature
    signature = (status_html, progress_html, claimed_html, streamer_rows, sync_html, refresh_seconds)
    if signature == _last_render_signature:
        return
    _last_render_signature = signature

    html_doc = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Rust Twitch Drops Watcher</title>
<link rel="stylesheet" href="style.css">
</head>
<body>
<div class="container">
    <h1>Rust Twitch Drops Watcher</h1>
    <div class="nav"><a href="/" class="active">Dashboard</a><a href="/logs.html">Logs</a></div>
    <div class="header-row">
        <div class="py-status"><span class="py-dot"></span>Python watcher running</div>
        <div class="py-status" id="twitch-sync">{sync_html}</div>
        <a class="btn-open-twitch" href="https://www.twitch.tv/drops/inventory" target="_blank">Open Twitch Drops Inventory</a>
    </div>
    <div id="dashboard-status">{status_html}</div>
    <p class="muted">Last updated <span id="last-updated">{updated}</span> &middot; refreshes every {refresh_seconds}s &middot; edit config.json to change timing</p>

    <div class="panel">
        <h2>Drop Progress</h2>
        <div id="progress-content">{progress_html}</div>
    </div>

    <div class="panel">
        <div class="panel-header">
            <h2>Claimed</h2>
            <form method="POST" action="/refresh-claimed"><button type="submit" class="btn-sort">Re-check Twitch</button></form>
        </div>
        <div id="claimed-content">{claimed_html}</div>
    </div>

    <div class="panel">
        <div class="panel-header">
            <h2>Streamers</h2>
            <button id="sort-online-btn" class="btn-sort" onclick="toggleSortOnline()">Sort: Online First</button>
        </div>
        <table class="streamer-table">
            <thead><tr><th>Name</th><th>Link</th><th>Status</th><th>Done</th></tr></thead>
            <tbody id="streamer-rows">{streamer_rows}</tbody>
        </table>
    </div>
</div>
<script>
function statusPriority(s) {{
    return s === "ONLINE" ? 0 : s === "PAIRED" ? 1 : 2;
}}

function collectBlocks(tbody) {{
    const rows = Array.from(tbody.querySelectorAll("tr"));
    const blocks = [];
    let current = null;
    rows.forEach((row) => {{
        if (row.classList.contains("drop-group-header")) {{
            current = {{ header: row, members: [] }};
            blocks.push(current);
        }} else if (current) {{
            current.members.push(row);
        }}
    }});
    return blocks;
}}

function applySort() {{
    const tbody = document.getElementById("streamer-rows");
    if (!tbody) return;
    const sortOnline = localStorage.getItem("rustDropsSortOnline") === "1";
    const blocks = collectBlocks(tbody);
    blocks.forEach((b, i) => {{ b.originalIndex = i; }});
    const ordered = sortOnline
        ? blocks.slice().sort((a, b) => {{
              const pa = Math.min(...a.members.map((m) => statusPriority(m.dataset.status)));
              const pb = Math.min(...b.members.map((m) => statusPriority(m.dataset.status)));
              return pa - pb || a.originalIndex - b.originalIndex;
          }})
        : blocks;
    ordered.forEach((b) => {{
        tbody.appendChild(b.header);
        b.members.forEach((m) => tbody.appendChild(m));
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

// Claim/unclaim happen against a background fetch so the row disappears
// instantly instead of waiting on a full page reload (which used to also
// re-fetch every drop image and felt like a big lag spike).
function showToast(message) {{
    const toast = document.createElement("div");
    toast.className = "toast";
    toast.textContent = message;
    document.body.appendChild(toast);
    requestAnimationFrame(() => toast.classList.add("show"));
    setTimeout(() => {{
        toast.classList.remove("show");
        setTimeout(() => toast.remove(), 300);
    }}, 2500);
}}

function fadeAndRemove(el) {{
    el.style.transition = "opacity 0.15s ease";
    el.style.opacity = "0.3";
    el.style.pointerEvents = "none";
    setTimeout(() => el.remove(), 200);
}}

function submitClaimForm(form) {{
    const action = form.getAttribute("action");
    const nameInput = form.querySelector('input[name="name"]');
    const name = nameInput ? nameInput.value : "";
    const row = form.closest("tr");
    const container = row || form.closest(".progress-item");

    if (container) {{
        fadeAndRemove(container);
    }}

    // Claiming one member auto-claims the whole team server-side, so fade
    // out the rest of their team's rows too instead of leaving them stale.
    if (action === "/claim" && row && row.dataset.teamId !== undefined) {{
        document.querySelectorAll('tr[data-team-id="' + row.dataset.teamId + '"]').forEach((teamRow) => {{
            if (teamRow !== row) fadeAndRemove(teamRow);
        }});
    }}

    if (action === "/claim") {{
        showToast("Claimed! The Claimed section will update shortly.");
    }}

    fetch(action, {{
        method: "POST",
        headers: {{ "Content-Type": "application/x-www-form-urlencoded" }},
        body: "name=" + encodeURIComponent(name),
    }}).catch(() => {{}});
}}

function bindClaimForms() {{
    document.querySelectorAll('form[action="/claim"], form[action="/unclaim"]').forEach((form) => {{
        form.addEventListener("submit", (e) => {{
            e.preventDefault();
            submitClaimForm(form);
        }});
    }});
}}

// Polls the pre-rendered fragments instead of the old <meta refresh>, which
// used to force a full page navigation (and a full style.css/image re-fetch)
// every {refresh_seconds}s just to show a percentage tick over.
async function refreshDashboard() {{
    let data;
    try {{
        const res = await fetch("/api/dashboard");
        data = await res.json();
    }} catch (e) {{
        return;
    }}

    const patch = (id, html) => {{
        if (html === undefined) return;
        const el = document.getElementById(id);
        if (el) el.innerHTML = html;
    }};

    patch("dashboard-status", data.status_html);
    patch("progress-content", data.progress_html);
    patch("claimed-content", data.claimed_html);
    patch("streamer-rows", data.streamer_rows);
    patch("twitch-sync", data.sync_html);

    const updatedEl = document.getElementById("last-updated");
    if (updatedEl && data.updated !== undefined) updatedEl.textContent = data.updated;

    applySort();
    bindClaimForms();
}}

bindClaimForms();
applySort();
refreshDashboard();
setInterval(refreshDashboard, {refresh_seconds * 1000});
</script>
</body>
</html>'''

    with open(DASHBOARD_PATH, "w", encoding="utf-8") as f:
        f.write(html_doc)

# Builds the JSON payload the Logs page polls for live stats + console lines
def build_state_snapshot(since_seq=None):
    with state_lock:
        watching = state["watching"]
        watch_until = state["watch_until"]
        claimed = set(state["claimed"])
        completed = set(state["completed"])
        in_progress_count = len(state["in_progress"])
        session_claimed_count = state["session_claimed_count"]
        run_state = state["run_state"]
        log_seq = state["log_seq"]
        if since_seq is None:
            logs = list(state["log"])
        else:
            logs = [entry for entry in state["log"] if entry["seq"] > since_seq]

    remaining = max(0, int(watch_until - time.time())) if watching else 0

    return {
        "logs": logs,
        "log_seq": log_seq,
        "watching": watching,
        "watch_remaining_seconds": remaining,
        "ready_to_claim": len(completed - claimed),
        "in_progress_count": in_progress_count,
        "session_claimed_count": session_claimed_count,
        "run_state": run_state,
    }

# Returns the last-rendered dashboard fragments the dashboard page polls,
# so /api/dashboard can serve them without re-running any render logic.
def build_dashboard_fragments():
    with state_lock:
        return dict(state["dashboard_fragments"])

# In-memory cache for static files (style.css, logs.html) keyed by path,
# holding (mtime, bytes) so unchanged files aren't re-read from disk per request
_static_file_cache = {}

# Serves the dashboard and handles Claim / Undo form submissions
class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # keep the console quiet

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/", "/dashboard.html"):
            self._serve_file(DASHBOARD_PATH, "text/html")
        elif path == "/logs.html":
            self._serve_file(LOGS_PATH, "text/html", cache_seconds=30)
        elif path == "/style.css":
            self._serve_file(STYLE_PATH, "text/css", cache_seconds=30)
        elif path == "/api/state":
            since_raw = parse_qs(parsed.query).get("since", [None])[0]
            since_seq = int(since_raw) if since_raw not in (None, "") else None
            self._serve_json(build_state_snapshot(since_seq))
        elif path == "/api/dashboard":
            self._serve_json(build_dashboard_fragments())
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path in ("/claim", "/unclaim"):
            self._handle_claim()
        elif self.path in ("/control/start", "/control/pause", "/control/stop"):
            self._handle_control()
        elif self.path == "/refresh-claimed":
            self._handle_refresh_claimed()
        elif self.path == "/api/twitch-status":
            self._handle_twitch_status()
        else:
            self.send_response(404)
            self.end_headers()

    def _handle_claim(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8")
        name = parse_qs(body).get("name", [""])[0]

        newly_claimed = []
        removed = False
        with state_lock:
            if self.path == "/claim":
                if name not in state["claimed"]:
                    team_id = state["team_by_name"].get(name)
                    teammates = state["members_by_team"].get(team_id, {name}) if team_id is not None else {name}
                    for teammate in teammates:
                        if teammate not in state["claimed"]:
                            state["claimed"].add(teammate)
                            newly_claimed.append(teammate)
                    state["session_claimed_count"] += len(newly_claimed)
            else:
                if name in state["claimed"]:
                    state["claimed"].discard(name)
                    state["session_claimed_count"] = max(0, state["session_claimed_count"] - 1)
                    removed = True
            save_claimed(state["claimed"])

        if newly_claimed:
            teammates = [n for n in newly_claimed if n != name]
            if teammates:
                log_event(f"Marked {name} as claimed (manual) — auto-claimed teammate(s): {', '.join(sorted(teammates))}")
            else:
                log_event(f"Marked {name} as claimed (manual)")
        elif removed:
            log_event(f"Unmarked {name} as claimed")

        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()

    def _handle_refresh_claimed(self):
        if apply_twitch_payload():
            control_event.set()
        else:
            with state_lock:
                has_payload = state["twitch_payload"] is not None
            if has_payload:
                log_event("Re-checked Twitch inventory data — no changes")
            else:
                log_event("No data from the browser extension yet — open the Twitch Drops Inventory tab")

        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()

    # Receives inventory snapshots from the browser extension. Browsers
    # always attach an Origin header websites can't fake, so requiring a
    # chrome-extension:// origin stops any open webpage from posting here.
    def _handle_twitch_status(self):
        if not self.headers.get("Origin", "").startswith("chrome-extension://"):
            self.send_response(403)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            self.send_response(400)
            self.end_headers()
            return

        with state_lock:
            first = state["twitch_payload"] is None
            state["twitch_payload"] = payload
            state["twitch_synced_at"] = time.time()
            state["twitch_stale_logged"] = False

        if first:
            log_event("Browser extension connected — receiving Twitch drop inventory")

        # Wake the poll loop so it can switch streams right away instead of
        # waiting out its sleep (up to recheck_interval_minutes)
        if apply_twitch_payload() or first:
            control_event.set()

        self.send_response(204)
        self.end_headers()

    def _handle_control(self):
        action = self.path.rsplit("/", 1)[-1]
        request_control(action)

        self.send_response(303)
        self.send_header("Location", self.headers.get("Referer", "/logs.html"))
        self.end_headers()

    def _serve_file(self, path, content_type, cache_seconds=None):
        try:
            mtime = os.path.getmtime(path)
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()
            return

        cached = _static_file_cache.get(path)
        if cached is not None and cached[0] == mtime:
            data = cached[1]
        else:
            with open(path, "rb") as f:
                data = f.read()
            _static_file_cache[path] = (mtime, data)

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if cache_seconds:
            self.send_header("Cache-Control", f"max-age={cache_seconds}")
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
# Runs one scrape+render cycle and returns how long to sleep before the next
def run_poll_cycle():
        config = load_config()

        try:
            streamers, drop_types = get_streamer_links()
        except Exception as e:
            log_event(f"ERROR fetching streamer data from facepunch: {e}")
            streamers, drop_types = [], set()

        # Precomputed once per cycle so claim requests can do O(1) lookups
        # instead of scanning the full streamer list while holding the lock.
        team_by_name = {}
        members_by_team = {}
        for s in streamers:
            name_, team_id_ = s[0], s[3]
            team_by_name[name_] = team_id_
            members_by_team.setdefault(team_id_, set()).add(name_)

        with state_lock:
            state["streamers"] = list(streamers)
            state["drop_types"] = set(drop_types)
            state["team_by_name"] = team_by_name
            state["members_by_team"] = members_by_team
            state["config"] = dict(config)
            payload = state["twitch_payload"]
            sync_age = time.time() - state["twitch_synced_at"]
            stale_logged = state["twitch_stale_logged"]
            if payload is not None and sync_age > TWITCH_STALE_SECONDS:
                state["twitch_stale_logged"] = True

        if payload is None:
            log_event("No Twitch inventory data yet — open the Twitch Drops Inventory tab with the browser extension installed")
        elif sync_age > TWITCH_STALE_SECONDS and not stale_logged:
            log_event(f"Twitch inventory data is {int(sync_age // 60)} min old — is the inventory tab still open?")

        completed, in_progress, auto_claimed, unmatched = match_twitch_payload(payload or {}, streamers, drop_types, config)
        merge_twitch_status(completed, in_progress, auto_claimed, unmatched)

        with state_lock:
            claimed = set(state["claimed"])
            watching = state["watching"]
            watch_until = state["watch_until"]

        if watching:
            # They may have gone offline (or gotten claimed elsewhere) well
            # before the watch timer runs out - drop the "watching" state
            # immediately instead of showing a stale status until it expires.
            current_status = next((s[2] for s in streamers if s[0] == watching), None)
            if current_status != "ONLINE" or watching in completed or watching in claimed:
                log_event(f"{watching} is no longer eligible (offline or claimed) — not watching anyone")
                watching = None
            elif time.time() >= watch_until:
                log_event(f"Done watching {watching} — watch window elapsed")
                watching = None

        # Without inventory data every drop looks unclaimed, so hold off
        # opening streams until the extension's first snapshot arrives.
        if not watching and payload is not None:
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

        return 60 if watching else config["recheck_interval_minutes"] * 60

# Repeatedly runs poll cycles, respecting the Start/Pause/Stop controls
def poll_loop():
    while True:
        with state_lock:
            run_state = state["run_state"]

        if run_state != "running":
            # No timeout - request_control() already wakes us via
            # control_event.set() on any real Start/Pause/Stop transition.
            control_event.wait()
            control_event.clear()
            continue

        sleep_seconds = run_poll_cycle()
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

    log_event("Running initial refresh before opening dashboard...")
    run_poll_cycle()

    webbrowser.open(url)
    log_event("Watcher started")

    poll_loop()


if __name__ == "__main__":
    main()
