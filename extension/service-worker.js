const RECON_ALARM_NAME = "recon-open-check";

// Idempotent: safe to call every time the service worker wakes up. Chrome
// enforces a floor around 1 minute for non-persistent-alarm periods.
chrome.alarms.create(RECON_ALARM_NAME, { periodInMinutes: 1 });

chrome.runtime.onInstalled.addListener(() => {
  console.log("[Recon] Extension installed");
  chrome.alarms.create(RECON_ALARM_NAME, { periodInMinutes: 1 });
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status === "complete" && tab.url?.includes("mail.google.com")) {
    chrome.scripting.executeScript({
      target: { tabId },
      files: ["content.js"],
    }).catch((err) => console.error("[Recon] Inject failed:", err));
  }
});

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === "track") {
    const { serverUrl, payload } = msg;
    fetch(`${serverUrl}/track`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })
      .then((r) => r.json())
      .then((data) => sendResponse({ ok: true, tracker_id: data.tracker_id, links: data.links || [] }))
      .catch((err) => {
        console.error("[Recon] Track error:", err);
        sendResponse({ ok: false, error: err.message });
      });
    return true;
  }

  if (msg.type === "fetch") {
    const { serverUrl, path } = msg;
    fetch(`${serverUrl}${path}`)
      .then((r) => r.json())
      .then((data) => sendResponse({ ok: true, data }))
      .catch((err) => {
        console.error("[Recon] Fetch error:", err);
        sendResponse({ ok: false, error: err.message });
      });
    return true;
  }
});

// ---- Phase 7: Desktop alerts on new opens ----
//
// Periodic alarm (no content-script trigger needed) that polls
// /status/sent?sender_email=X, diffs each email's total_opens against a
// snapshot in chrome.storage.local, and fires a desktop notification for
// any email whose open count increased since last check.
//
// Assumption about the backend contract: each entry returned by
// /status/sent has a stable identifier field to key the snapshot on. We
// prefer `id` (the emails table PK per HANDOFF.md's DB schema); if that's
// absent we fall back to a composite key of recipient+subject+thread_id,
// which is weaker (could collide across repeated identical sends) — note
// this as a known limitation to reconcile with the backend agent.

function getStorageSync(keys) {
  return new Promise((resolve) => chrome.storage.sync.get(keys, resolve));
}

function getStorageLocal(keys) {
  return new Promise((resolve) => chrome.storage.local.get(keys, resolve));
}

function setStorageLocal(items) {
  return new Promise((resolve) => chrome.storage.local.set(items, resolve));
}

function snapshotKeyFor(entry) {
  return entry.id || entry.tracker_id || `${entry.recipient || ""}|${entry.subject || ""}|${entry.thread_id || ""}`;
}

async function checkForNewOpens() {
  const { serverUrl, senderEmail, alertsEnabled } = await getStorageSync(["serverUrl", "senderEmail", "alertsEnabled"]);
  if (!serverUrl || !senderEmail) return;
  if (alertsEnabled === false) return; // default true unless explicitly disabled in popup

  let data;
  try {
    const res = await fetch(`${serverUrl}/status/sent?sender_email=${encodeURIComponent(senderEmail)}`);
    data = await res.json();
  } catch (err) {
    console.warn("[Recon] Alarm fetch failed:", err.message);
    return;
  }
  if (!Array.isArray(data)) return;

  const { reconOpenSnapshot } = await getStorageLocal(["reconOpenSnapshot"]);
  const snapshot = reconOpenSnapshot || {};
  const nextSnapshot = {};

  for (const entry of data) {
    const key = snapshotKeyFor(entry);
    const prevOpens = snapshot[key]?.total_opens ?? 0;
    const currentOpens = entry.total_opens || 0;

    nextSnapshot[key] = { total_opens: currentOpens, last_opened_at: entry.last_opened_at || null };

    if (currentOpens > prevOpens) {
      try {
        chrome.notifications.create(`recon-${key}-${currentOpens}`, {
          type: "basic",
          iconUrl: "icons/icon48.png",
          title: "Recon",
          message: `${entry.recipient || "Someone"} opened "${entry.subject || "(no subject)"}" (${currentOpens}x)`,
        });
      } catch (err) {
        console.warn("[Recon] notifications.create failed:", err);
      }
    }
  }

  await setStorageLocal({ reconOpenSnapshot: nextSnapshot });
}

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === RECON_ALARM_NAME) {
    checkForNewOpens().catch((err) => console.error("[Recon] checkForNewOpens failed:", err));
  }
});
