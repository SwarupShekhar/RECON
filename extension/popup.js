const serverUrlEl = document.getElementById("serverUrl");
const senderEmailEl = document.getElementById("senderEmail");
const saveBtn = document.getElementById("save");
const statusEl = document.getElementById("status");

chrome.storage.sync.get(["serverUrl", "senderEmail"], (data) => {
  if (data.serverUrl) serverUrlEl.value = data.serverUrl;
  if (data.senderEmail) senderEmailEl.value = data.senderEmail;
});

saveBtn.addEventListener("click", () => {
  const serverUrl = serverUrlEl.value.replace(/\/+$/, "");
  const senderEmail = senderEmailEl.value.trim();

  if (!serverUrl || !senderEmail) {
    statusEl.textContent = "Fill in both fields";
    statusEl.className = "error";
    return;
  }

  chrome.storage.sync.set({ serverUrl, senderEmail }, () => {
    statusEl.textContent = "Saved";
    statusEl.className = "status";
  });
});
