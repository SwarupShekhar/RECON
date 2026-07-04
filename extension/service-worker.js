chrome.runtime.onInstalled.addListener(() => {
  console.log("[Recon] Extension installed");
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
      .then((data) => sendResponse({ ok: true, tracker_id: data.tracker_id }))
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
