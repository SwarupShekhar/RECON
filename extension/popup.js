const serverUrlEl = document.getElementById("serverUrl");
const apiKeyEl = document.getElementById("apiKey");
const senderEmailEl = document.getElementById("senderEmail");
const alertsEnabledEl = document.getElementById("alertsEnabled");
const saveBtn = document.getElementById("save");
const statusEl = document.getElementById("status");
const loggedInAsEl = document.getElementById("loggedInAs");
const loggedInEmailEl = document.getElementById("loggedInEmail");
const dashboardLinkEl = document.getElementById("dashboardLink");

function defaultServerUrl() {
  return (typeof RECON_DEFAULT_SERVER_URL === "string" && RECON_DEFAULT_SERVER_URL) || "";
}

chrome.storage.sync.get(["serverUrl", "apiKey", "senderEmail", "alertsEnabled"], (data) => {
  const serverUrl = data.serverUrl || defaultServerUrl();
  serverUrlEl.value = serverUrl;
  if (data.apiKey) apiKeyEl.value = data.apiKey;
  if (data.senderEmail) senderEmailEl.value = data.senderEmail;
  alertsEnabledEl.checked = data.alertsEnabled !== false;
  updateDashboardLink(serverUrl);
  refreshAuthState(serverUrl, data.apiKey, data.senderEmail);
  if (!data.apiKey) {
    chrome.storage.local.get(["reconBackupApiKey"], (localData) => {
      if (localData.reconBackupApiKey) {
        apiKeyEl.value = localData.reconBackupApiKey;
        statusEl.textContent = "Restored API key backup — click Save";
        statusEl.className = "status";
      }
    });
  }
});

function updateDashboardLink(serverUrl) {
  if (serverUrl) {
    dashboardLinkEl.href = serverUrl.replace(/\/+$/, "") + "/dashboard";
    dashboardLinkEl.style.display = "block";
  } else {
    dashboardLinkEl.style.display = "none";
  }
}

function buildFetchHeaders(apiKey) {
  const headers = {};
  if (apiKey) headers["X-API-Key"] = apiKey;
  return headers;
}

function buildSentPath(apiKey, senderEmail) {
  if (apiKey) return "/status/sent";
  if (senderEmail) return `/status/sent?sender_email=${encodeURIComponent(senderEmail)}`;
  return null;
}

function refreshAuthState(serverUrl, apiKey, senderEmail) {
  if (!serverUrl || (!apiKey && !senderEmail)) {
    loggedInAsEl.style.display = "none";
    return;
  }
  if (!apiKey) {
    loggedInAsEl.style.display = "block";
    loggedInEmailEl.textContent = senderEmail;
    return;
  }
  chrome.runtime.sendMessage(
    { type: "fetch", serverUrl, path: "/me", headers: buildFetchHeaders(apiKey) },
    (response) => {
      if (response && response.ok && response.data && response.data.email) {
        loggedInAsEl.style.display = "block";
        loggedInEmailEl.textContent = response.data.email;
        senderEmailEl.value = response.data.email;
      } else {
        loggedInAsEl.style.display = "none";
      }
    }
  );
}

saveBtn.addEventListener("click", () => {
  const serverUrl = serverUrlEl.value.replace(/\/+$/, "");
  const apiKey = apiKeyEl.value.trim();
  const senderEmail = senderEmailEl.value.trim();
  const alertsEnabled = alertsEnabledEl.checked;

  if (!serverUrl || !apiKey) {
    statusEl.textContent = "API key required";
    statusEl.className = "error";
    return;
  }

  const persist = (resolvedEmail) => {
    const emailToStore = resolvedEmail || senderEmail;
    chrome.storage.sync.set({ serverUrl, apiKey, senderEmail: emailToStore, alertsEnabled }, () => {
      chrome.storage.local.set({ reconBackupApiKey: apiKey });
      statusEl.textContent = "Saved";
      statusEl.className = "status";
      updateDashboardLink(serverUrl);
      refreshAuthState(serverUrl, apiKey, emailToStore);
      loadRecentActivity(serverUrl, apiKey, emailToStore);
    });
  };

  if (apiKey) {
    chrome.runtime.sendMessage(
      { type: "fetch", serverUrl, path: "/me", headers: buildFetchHeaders(apiKey) },
      (response) => {
        if (response?.ok && response.data?.email) {
          senderEmailEl.value = response.data.email;
          persist(response.data.email);
        } else {
          persist(senderEmail);
        }
      }
    );
  } else {
    persist(senderEmail);
  }
});

const reconListEl = document.getElementById("reconList");

function timeAgo(dateStr) {
  if (!dateStr) return "";
  const diff = Math.floor((Date.now() - new Date(dateStr).getTime()) / 1000);
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

function statusDotColor(entry) {
  if ((entry.verified_opens || 0) > 0) return "var(--accent)";
  if ((entry.total_opens || 0) > 0) return "var(--warn)";
  return "var(--border)";
}

function statusLabel(entry) {
  if (!entry.total_opens) return "Not opened";
  const times = entry.total_opens > 1 ? `${entry.total_opens}x` : "Opened";
  const ago = entry.last_opened_at ? timeAgo(entry.last_opened_at) : "";
  return ago ? `${times} · ${ago}` : times;
}

function totalClicks(entry) {
  if (!entry.links || !entry.links.length) return 0;
  return entry.links.reduce((sum, l) => sum + (l.clicks || 0), 0);
}

function renderRecentActivity(entries) {
  if (!entries || entries.length === 0) {
    reconListEl.innerHTML = '<div class="recon-empty">No tracked emails yet — send one from Gmail.</div>';
    return;
  }

  reconListEl.innerHTML = entries.slice(0, 10).map((e) => {
    const clicks = totalClicks(e);
    const clickBadge = clicks > 0 ? `<span class="recon-badge">${clicks} click${clicks > 1 ? "s" : ""}</span>` : "";
    return `
    <div class="recon-item">
      <span class="recon-dot" style="background:${statusDotColor(e)}"></span>
      <div class="recon-item-text">
        <div class="recon-item-subject">${escapeHtml(e.subject || "(no subject)")}${clickBadge}</div>
        <div class="recon-item-meta">${escapeHtml(e.recipient || "")}</div>
      </div>
      <div class="recon-item-status">${escapeHtml(statusLabel(e))}</div>
    </div>`;
  }).join("");
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

function loadRecentActivity(serverUrl, apiKey, senderEmail) {
  const path = buildSentPath(apiKey, senderEmail);
  if (!serverUrl || !path) {
    reconListEl.innerHTML = '<div class="recon-empty">Configure server URL and API key to see activity.</div>';
    return;
  }

  reconListEl.innerHTML = '<div class="recon-empty">Loading…</div>';

  chrome.runtime.sendMessage(
    { type: "fetch", serverUrl, path, headers: buildFetchHeaders(apiKey) },
    (response) => {
      if (chrome.runtime.lastError) {
        reconListEl.innerHTML = '<div class="recon-empty">Couldn\'t reach the server.</div>';
        return;
      }
      if (!response || !response.ok) {
        const detail = response?.error || response?.data?.detail || "unknown error";
        reconListEl.innerHTML = `<div class="recon-empty">Couldn't load activity — ${escapeHtml(String(detail))}</div>`;
        return;
      }
      if (!Array.isArray(response.data)) {
        reconListEl.innerHTML = '<div class="recon-empty">Unexpected server response — check URL and API key.</div>';
        return;
      }
      renderRecentActivity(response.data);
    }
  );
}

chrome.storage.sync.get(["serverUrl", "apiKey", "senderEmail"], (data) => {
  loadRecentActivity(data.serverUrl || defaultServerUrl(), data.apiKey, data.senderEmail);
});
