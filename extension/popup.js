const serverUrlEl = document.getElementById("serverUrl");
const senderEmailEl = document.getElementById("senderEmail");
const alertsEnabledEl = document.getElementById("alertsEnabled");
const saveBtn = document.getElementById("save");
const statusEl = document.getElementById("status");

chrome.storage.sync.get(["serverUrl", "senderEmail", "alertsEnabled"], (data) => {
  if (data.serverUrl) serverUrlEl.value = data.serverUrl;
  if (data.senderEmail) senderEmailEl.value = data.senderEmail;
  // Default to enabled (true) unless the user has explicitly turned it off.
  alertsEnabledEl.checked = data.alertsEnabled !== false;
});

saveBtn.addEventListener("click", () => {
  const serverUrl = serverUrlEl.value.replace(/\/+$/, "");
  const senderEmail = senderEmailEl.value.trim();
  const alertsEnabled = alertsEnabledEl.checked;

  if (!serverUrl || !senderEmail) {
    statusEl.textContent = "Fill in both fields";
    statusEl.className = "error";
    return;
  }

  chrome.storage.sync.set({ serverUrl, senderEmail, alertsEnabled }, () => {
    statusEl.textContent = "Saved";
    statusEl.className = "status";
  });
});
