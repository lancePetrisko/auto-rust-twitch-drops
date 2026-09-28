// Reads the rendered Twitch drops inventory in your logged-in tab and hands
// it to background.js. Twitch's class names are hashed and change between
// deploys, so this keys off ARIA roles, alt text, data-test-selectors and
// the Rust box art instead. Item -> streamer matching happens in Python.
const RUST_GAME_ID = "263490";

function isRustCampaign(info) {
    const boxart = info.querySelector("img.inventory-boxart");
    const about = info.querySelector('a[data-test-selector$="about-button"]');
    return (boxart && boxart.src.includes(`/${RUST_GAME_ID}`)) || (about && about.href.includes("facepunch"));
}

function findClaimButton(el) {
    return [...el.querySelectorAll("button")].find((b) => /claim/i.test(b.textContent));
}

// Walks up from a reward image to the smallest box holding its progress bar
// or claim button, stopping before it swallows a neighbouring reward.
function rewardUnit(img) {
    for (let el = img.parentElement; el; el = el.parentElement) {
        if (el.querySelectorAll("img.inventory-drop-image").length > 1) return null;
        if (el.querySelector('[role="progressbar"]') || findClaimButton(el)) return el;
    }
    return null;
}

function readInProgress() {
    const drops = [];
    for (const info of document.querySelectorAll(".inventory-campaign-info")) {
        if (!isRustCampaign(info)) continue;
        const campaign = info.parentElement;
        const channelLink = info.querySelector('a[data-test-selector$="channel-hint-text"]');
        const channel = channelLink ? new URL(channelLink.href).pathname.replace(/\//g, "") : "";

        for (const img of campaign.querySelectorAll("img.inventory-drop-image")) {
            const unit = rewardUnit(img);
            if (!unit) continue;
            const nameTag = unit.querySelector("p");
            const bar = unit.querySelector('[role="progressbar"]');
            const total = unit.textContent.match(/of\s+(\d+(?:\.\d+)?)\s*(hour|minute)/i);
            drops.push({
                item: nameTag ? nameTag.textContent.trim() : "",
                channel,
                percent: bar ? Math.round(parseFloat(bar.getAttribute("aria-valuenow")) || 0) : 100,
                total_minutes: total ? Math.round(parseFloat(total[1]) * (/hour/i.test(total[2]) ? 60 : 1)) : null,
                claimable: Boolean(findClaimButton(unit)),
            });
        }
    }
    return drops;
}

// Claimed rewards only carry the item name (in the image alt) and a
// relative "N days ago" time, which Python uses to skip old campaigns.
function readClaimed() {
    const items = [];
    for (const img of document.querySelectorAll('img[alt^="Drop image for "]')) {
        let age = "";
        for (let el = img.parentElement; el && !age; el = el.parentElement) {
            const ageTag = [...el.querySelectorAll("p")].find((p) => /ago|yesterday|today/i.test(p.textContent));
            if (ageTag) age = ageTag.textContent.trim();
        }
        items.push({ item: img.alt.slice("Drop image for ".length).trim(), age });
    }
    return items;
}

// The page renders in stages - only report once the Claimed section exists,
// otherwise a half-rendered page would briefly look like "nothing claimed".
function pageReady() {
    return [...document.querySelectorAll(".inventory-page h5")].some((h) => h.textContent.trim() === "Claimed");
}

let lastSent = "";

function scan() {
    if (!pageReady()) return;
    const payload = { in_progress: readInProgress(), claimed: readClaimed() };
    const json = JSON.stringify(payload);
    if (json === lastSent) return;
    lastSent = json;
    chrome.runtime.sendMessage({ type: "twitch-status", payload });
}

let debounce;
new MutationObserver(() => {
    clearTimeout(debounce);
    debounce = setTimeout(scan, 1000);
}).observe(document.body, { childList: true, subtree: true, characterData: true });
scan();
