// Relays inventory snapshots from inventory.js to the local watcher server.
// The fetch has to happen here, not in the content script: a content script
// fetch runs as twitch.tv and gets blocked by CORS / Chrome's local network
// rules, while the service worker runs as the extension itself.
const SERVER_URL = "http://127.0.0.1:8787/api/twitch-status";
const INVENTORY_URL = "https://www.twitch.tv/drops/inventory*";
const RELOAD_ALARM = "reload-inventory";
const RELOAD_MINUTES = 5;

chrome.runtime.onMessage.addListener((message) => {
    if (message.type !== "twitch-status") return;
    fetch(SERVER_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(message.payload),
    }).catch(() => {
        // Watcher isn't running - the next inventory reload will resend.
    });
});

// Twitch doesn't live-update drop progress on the inventory page, so the
// open inventory tab gets reloaded on a timer to pick up new percentages.
// Only created if missing, so each service worker wake-up doesn't reset it.
chrome.alarms.get(RELOAD_ALARM, (alarm) => {
    if (!alarm) chrome.alarms.create(RELOAD_ALARM, { periodInMinutes: RELOAD_MINUTES });
});

chrome.alarms.onAlarm.addListener(async (alarm) => {
    if (alarm.name !== RELOAD_ALARM) return;
    const tabs = await chrome.tabs.query({ url: INVENTORY_URL });
    for (const tab of tabs) chrome.tabs.reload(tab.id);
});
