// Relays inventory snapshots from inventory.js to the local watcher server.
// The fetch has to happen here, not in the content script: a content script
// fetch runs as twitch.tv and gets blocked by CORS / Chrome's local network
// rules, while the service worker runs as the extension itself.
const SERVER_URL = "http://127.0.0.1:8787/api/twitch-status";
const TABS_URL = "http://127.0.0.1:8787/api/tabs";
const TABS_CLOSED_URL = "http://127.0.0.1:8787/api/tabs-closed";
const INVENTORY_URL = "https://www.twitch.tv/drops/inventory*";
const RELOAD_ALARM = "reload-inventory";
const RELOAD_MINUTES = 5;
const CLOSE_TABS_ALARM = "close-finished-tabs";
const CLOSE_TABS_MINUTES = 0.5;

chrome.runtime.onMessage.addListener((message) => {
    if (message.type !== "twitch-status") return;
    fetch(SERVER_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(message.payload),
    }).catch(() => {
        // Watcher isn't running - the next inventory reload will resend.
    });
    closeFinishedTabs();
});

function channelOf(url) {
    try {
        return new URL(url).pathname.split("/")[1].toLowerCase();
    } catch {
        return "";
    }
}

// The watcher opens a new stream tab every time it switches streamer, so
// without this they pile up overnight. Closes tabs for any stream the watcher
// opened that it's no longer watching, and reports them for the logs.
async function closeFinishedTabs() {
    let tabState;
    try {
        tabState = await (await fetch(TABS_URL)).json();
    } catch {
        return; // watcher isn't running
    }

    const keep = tabState.watching ? channelOf(tabState.watching) : null;
    const finished = new Set(tabState.opened.map(channelOf).filter((c) => c && c !== keep));
    if (!finished.size) return;

    const tabs = await chrome.tabs.query({ url: "https://www.twitch.tv/*" });
    const toClose = tabs.filter((tab) => finished.has(channelOf(tab.url)));
    if (!toClose.length) return;

    await chrome.tabs.remove(toClose.map((tab) => tab.id));
    fetch(TABS_CLOSED_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ channels: toClose.map((tab) => channelOf(tab.url)) }),
    }).catch(() => {});
}

// Twitch doesn't live-update drop progress on the inventory page, so the
// open inventory tab gets reloaded on a timer to pick up new percentages.
// Only created if missing, so each service worker wake-up doesn't reset it.
chrome.alarms.get(RELOAD_ALARM, (alarm) => {
    if (!alarm) chrome.alarms.create(RELOAD_ALARM, { periodInMinutes: RELOAD_MINUTES });
});
chrome.alarms.get(CLOSE_TABS_ALARM, (alarm) => {
    if (!alarm) chrome.alarms.create(CLOSE_TABS_ALARM, { periodInMinutes: CLOSE_TABS_MINUTES });
});

chrome.alarms.onAlarm.addListener(async (alarm) => {
    if (alarm.name === CLOSE_TABS_ALARM) return closeFinishedTabs();
    if (alarm.name !== RELOAD_ALARM) return;
    const tabs = await chrome.tabs.query({ url: INVENTORY_URL });
    for (const tab of tabs) chrome.tabs.reload(tab.id);
});
