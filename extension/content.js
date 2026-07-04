(() => {
  "use strict";

  let config = { serverUrl: "", senderEmail: "" };

  chrome.storage.sync.get(["serverUrl", "senderEmail"], (data) => {
    config.serverUrl = data.serverUrl || "";
    config.senderEmail = data.senderEmail || "";
    console.log("[Recon] Loaded config:", config.serverUrl, config.senderEmail);
  });

  chrome.storage.onChanged.addListener((changes) => {
    if (changes.serverUrl) config.serverUrl = changes.serverUrl.newValue;
    if (changes.senderEmail) config.senderEmail = changes.senderEmail.newValue;
  });

  async function createTracker(recipientEmail, subject, threadId, field) {
    return new Promise((resolve, reject) => {
      chrome.runtime.sendMessage(
        {
          type: "track",
          serverUrl: config.serverUrl,
          payload: {
            sender_email: config.senderEmail,
            recipient_email: recipientEmail,
            subject: subject,
            thread_id: threadId,
            recipient_field: field,
          },
        },
        (response) => {
          if (chrome.runtime.lastError) {
            reject(new Error(chrome.runtime.lastError.message));
          } else if (response?.ok) {
            resolve(response.tracker_id);
          } else {
            reject(new Error(response?.error || "Unknown error"));
          }
        }
      );
    });
  }

  function buildPixelHtml(trackerId) {
    const url = `${config.serverUrl}/t/${trackerId}/pixel.gif`;
    return `<img src="${url}" width="1" height="1" style="display:none" alt="">`;
  }

  function getComposeBody(container) {
    const selectors = [
      'div[role="textbox"][aria-label*="Body"]',
      'div[role="textbox"][aria-label*="body"]',
      'div[aria-label*="Message Body"]',
      'div[aria-label*="message body"]',
      'div.nH .no .editable',
      'div[contenteditable="true"]',
    ];
    for (const sel of selectors) {
      const el = container.querySelector(sel);
      if (el) return el;
    }
    return null;
  }

  function getRecipientEmails(container) {
    const results = [];
    const seen = new Set();

    const toInput = container.querySelector('input[aria-label="To recipients"]') ||
      container.querySelector('input[name="to"]');
    const ccInput = container.querySelector('input[aria-label="Cc recipients"]') ||
      container.querySelector('input[name="cc"]');
    const bccInput = container.querySelector('input[aria-label="Bcc recipients"]') ||
      container.querySelector('input[name="bcc"]');

    const addChips = (input, field) => {
      if (!input) return;
      const parent = input.closest('div[role="list"]') || input.parentElement;
      if (!parent) return;
      const chips = parent.querySelectorAll('span[email]');
      chips.forEach((chip) => {
        const email = chip.getAttribute("email");
        if (email && !seen.has(email)) {
          seen.add(email);
          results.push({ email, field });
        }
      });
    };

    addChips(toInput, "to");
    addChips(ccInput, "cc");
    addChips(bccInput, "bcc");

    if (results.length === 0) {
      const allChips = container.querySelectorAll('span[email]');
      allChips.forEach((chip) => {
        const email = chip.getAttribute("email");
        if (email && !seen.has(email)) {
          seen.add(email);
          results.push({ email, field: "to" });
        }
      });
    }

    return results;
  }

  function getSubject(container) {
    const input = container.querySelector('input[name="subjectbox"]');
    if (input) return input.value;
    const h2 = container.querySelector('h2');
    return h2 ? h2.textContent : "";
  }

  function getThreadId(container) {
    const hash = window.location.hash;
    const match = hash.match(/\/(\d+[a-f0-9]+)/);
    if (match) return match[1];

    const dialog = container.closest('div[role="dialog"]');
    if (dialog) {
      const threadAttr = dialog.getAttribute('data-thread-id') ||
        dialog.querySelector('[data-thread-id]')?.getAttribute('data-thread-id');
      if (threadAttr) return threadAttr;
    }

    const draftMatch = hash.match(/#drafts?\/([^&]+)/);
    if (draftMatch) return draftMatch[1];

    return null;
  }

  function injectPixel(body, trackerId) {
    if (body.innerHTML.includes("pixel.gif")) return;
    body.insertAdjacentHTML("beforeend", buildPixelHtml(trackerId));
    console.log("[Recon] Injected pixel for tracker:", trackerId);
  }

  async function handleSend(container) {
    if (!config.serverUrl || !config.senderEmail) {
      console.warn("[Recon] No config — click extension icon to set server URL and email");
      return;
    }

    const body = getComposeBody(container);
    if (!body) {
      console.warn("[Recon] Could not find compose body");
      return;
    }

    const recipients = getRecipientEmails(container);
    if (recipients.length === 0) {
      console.warn("[Recon] No recipients found");
      return;
    }

    const subject = getSubject(container);
    const threadId = getThreadId(container);

    console.log("[Recon] Sending to:", recipients.map(r => `${r.email} (${r.field})`).join(", "), "subject:", subject);

    for (const { email, field } of recipients) {
      try {
        const trackerId = await createTracker(email, subject, threadId, field);
        injectPixel(body, trackerId);
      } catch (err) {
        console.error("[Recon] Tracker failed:", err);
      }
    }
  }

  function findSendButton(el) {
    if (!el || !el.querySelectorAll) return null;
    const selectors = [
      'div[role="button"][aria-label*="Send"]',
      'div[role="button"][data-tooltip*="Send"]',
      'div[role="button"][aria-label*="send"]',
      'div.T-I.J-J5-Ji[aria-label*="Send"]',
      'div[role="button"][gh="cm"]',
    ];
    for (const sel of selectors) {
      const btn = el.querySelector(sel);
      if (btn) return btn;
    }
    return null;
  }

  function findComposeContainer(btn) {
    return btn.closest('div[role="dialog"]') || btn.closest('div.nH') || btn.parentElement?.parentElement?.parentElement;
  }

  function bindSendButton(btn) {
    if (btn._reconBound) return;

    btn._reconBound = true;
    btn.addEventListener("click", (e) => {
      const container = findComposeContainer(btn);
      if (container) {
        console.log("[Recon] Send clicked, container found");
        handleSend(container);
      }
    }, true);
  }

  function scanForSendButtons(root) {
    const selectors = [
      'div[role="button"][aria-label*="Send"]',
      'div[role="button"][data-tooltip*="Send"]',
      'div[role="button"][aria-label*="send"]',
      'div.T-I.J-J5-Ji[aria-label*="Send"]',
    ];
    for (const sel of selectors) {
      root.querySelectorAll(sel).forEach(bindSendButton);
    }
  }

  function watchCompose() {
    console.log("[Recon] Content script loaded, watching Gmail...");

    scanForSendButtons(document.body);

    const observer = new MutationObserver((mutations) => {
      for (const mutation of mutations) {
        for (const node of mutation.addedNodes) {
          if (node.nodeType !== 1) continue;
          scanForSendButtons(node);
        }
        if (mutation.type === "attributes" && mutation.attributeName === "aria-label") {
          scanForSendButtons(mutation.target);
        }
      }
    });

    observer.observe(document.body, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: ["aria-label"],
    });

    setInterval(() => {
      scanForSendButtons(document.body);
      injectSentCheckmarks();
      injectThreadCheckmark();
    }, 2000);
  }

  const sentCache = {};

  async function fetchSentStatus() {
    if (!config.serverUrl || !config.senderEmail) return null;
    const cacheKey = config.senderEmail;
    const now = Date.now();
    if (sentCache[cacheKey] && now - sentCache[cacheKey].time < 10000) {
      return sentCache[cacheKey].data;
    }
    try {
      const data = await new Promise((resolve, reject) => {
        chrome.runtime.sendMessage(
          {
            type: "fetch",
            serverUrl: config.serverUrl,
            path: `/status/sent?sender_email=${encodeURIComponent(config.senderEmail)}`,
          },
          (response) => {
            if (chrome.runtime.lastError) {
              reject(new Error(chrome.runtime.lastError.message));
            } else if (response?.ok) {
              resolve(response.data);
            } else {
              reject(new Error(response?.error || "Fetch failed"));
            }
          }
        );
      });
      sentCache[cacheKey] = { data, time: now };
      return data;
    } catch (err) {
      console.warn("[Recon] Sent status fetch failed:", err.message);
      return null;
    }
  }

  function getSubjectFromRow(row) {
    const heading = row.querySelector('div[role="heading"]');
    if (heading) return heading.textContent?.trim();

    const span = row.querySelector('span.bog') || row.querySelector('span[title]');
    if (span) return span.textContent?.trim();

    const spans = row.querySelectorAll('span');
    for (const s of spans) {
      const text = s.textContent?.trim();
      if (text && text.length > 3 && !text.includes('@') && !text.match(/\d{1,2}:\d{2}/) && !text.match(/(PM|AM)/i)) {
        return text;
      }
    }
    return null;
  }

  function getRecipientFromRow(row) {
    const emailSpans = row.querySelectorAll('span[email]');
    if (emailSpans.length > 0) {
      return emailSpans[0].getAttribute('email');
    }

    const fromSpan = row.querySelector('td.yX.xY span[email]');
    if (fromSpan) return fromSpan.getAttribute('email');

    return null;
  }

  async function injectSentCheckmarks() {
    const hash = window.location.hash;
    if (!hash.includes('#sent') && !hash.includes('#inbox')) return;

    const sentData = await fetchSentStatus();
    if (!sentData || sentData.length === 0) return;

    const rows = document.querySelectorAll(
      'tr.zA, tr[class*="zA"], div[role="listitem"], tr[jscontroller]'
    );

    for (const row of rows) {
      if (row._reconChecked) continue;

      const recipient = getRecipientFromRow(row);
      const subject = getSubjectFromRow(row);
      if (!recipient && !subject) continue;

      row._reconChecked = true;

      const match = sentData.find(e => {
        const recipientMatch = e.recipient === recipient;
        const subjectMatch = !subject || !e.subject ||
          e.subject.toLowerCase().includes(subject.toLowerCase()) ||
          subject.toLowerCase().includes(e.subject?.toLowerCase() || '');
        return recipientMatch && subjectMatch;
      });

      if (!match) continue;

      const indicator = document.createElement('span');
      indicator.className = 'recon-checkmark';
      indicator.style.cssText = 'font-size:14px;cursor:default;vertical-align:middle;margin-right:4px;letter-spacing:-2px;user-select:none;';

      if (match.total_opens > 0) {
        const isVerified = match.verified_opens > 0;
        indicator.textContent = '✓✓';
        indicator.style.color = isVerified ? '#0d9e3f' : '#e67e22';
        indicator.title = '';

        const popover = document.createElement('div');
        popover.className = 'recon-popover';
        popover.style.cssText = 'display:none;position:absolute;z-index:99999;background:#fff;border:1px solid #e0e0e0;border-radius:8px;padding:12px 16px;box-shadow:0 4px 12px rgba(0,0,0,0.15);font-size:13px;line-height:1.5;max-width:320px;pointer-events:none;';
        popover.innerHTML = `
          <div style="font-weight:600;margin-bottom:6px;">${match.recipient} opened your email${match.total_opens > 1 ? ` ${match.total_opens} times` : ''}</div>
          <div style="color:#555;">
            <span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${isVerified ? '#0d9e3f' : '#e67e22'};margin-right:6px;"></span>
            ${match.total_opens > 1 ? `First opened ${timeAgo(match.last_opened_at)}` : `Opened ${timeAgo(match.last_opened_at)}`}
          </div>
          ${match.last_opened_at ? `<div style="color:#888;font-size:11px;margin-top:4px;">Last: ${new Date(match.last_opened_at).toLocaleString()}</div>` : ''}
        `;
        indicator.appendChild(popover);

        indicator.addEventListener('mouseenter', () => { popover.style.display = 'block'; });
        indicator.addEventListener('mouseleave', () => { popover.style.display = 'none'; });
      } else {
        indicator.textContent = '✓✓';
        indicator.style.color = '#bbb';
        indicator.title = 'Sent — not opened yet';
      }

      const starEl = row.querySelector('div[role="checkbox"][aria-label*="Star"]');
      if (starEl && starEl.parentElement) {
        starEl.parentElement.insertBefore(indicator, starEl.nextSibling);
      } else {
        const firstCell = row.querySelector('td:first-child');
        if (firstCell) {
          firstCell.appendChild(indicator);
        } else {
          row.prepend(indicator);
        }
      }
    }
  }

  function timeAgo(dateStr) {
    if (!dateStr) return '';
    const now = Date.now();
    const then = new Date(dateStr).getTime();
    const diff = Math.floor((now - then) / 1000);
    if (diff < 60) return 'less than a minute ago';
    if (diff < 3600) return `${Math.floor(diff / 60)} minutes ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)} hours ago`;
    return `${Math.floor(diff / 86400)} days ago`;
  }

  async function injectThreadCheckmark() {
    const hash = window.location.hash;
    if (!hash.includes('#sent/')) return;

    if (document.querySelector('.recon-thread-check')) return;

    const sentData = await fetchSentStatus();
    if (!sentData || sentData.length === 0) return;

    const dateEl = document.querySelector('span.xW.xY span[title]');
    if (!dateEl) return;

    const headerRow = dateEl.closest('div') || dateEl.parentElement;
    if (!headerRow) return;

    const emailEl = document.querySelector('span.go span[email]');
    const recipientEmail = emailEl?.getAttribute('email');
    const subjectEl = document.querySelector('h2.hP');
    const subject = subjectEl?.textContent;

    const match = sentData.find(e => {
      const recipientMatch = !recipientEmail || e.recipient === recipientEmail;
      const subjectMatch = !subject || e.subject?.toLowerCase().includes(subject.toLowerCase()) || subject.toLowerCase().includes(e.subject?.toLowerCase() || '');
      return recipientMatch && subjectMatch;
    });

    if (!match) return;

    const indicator = document.createElement('span');
    indicator.className = 'recon-thread-check';
    indicator.style.cssText = 'font-size:14px;cursor:default;vertical-align:middle;margin-left:8px;letter-spacing:-2px;color:#0d9e3f;user-select:none;position:relative;';

    if (match.total_opens > 0) {
      const isVerified = match.verified_opens > 0;
      indicator.textContent = '✓✓';
      indicator.style.color = isVerified ? '#0d9e3f' : '#e67e22';

      const popover = document.createElement('div');
      popover.style.cssText = 'display:none;position:absolute;z-index:99999;background:#fff;border:1px solid #e0e0e0;border-radius:8px;padding:12px 16px;box-shadow:0 4px 12px rgba(0,0,0,0.15);font-size:13px;line-height:1.5;max-width:320px;bottom:100%;left:0;margin-bottom:8px;white-space:nowrap;';
      popover.innerHTML = `
        <div style="font-weight:600;margin-bottom:4px;">${match.recipient} opened your email${match.total_opens > 1 ? ` ${match.total_opens} times` : ''}</div>
        <div style="color:#555;">First opened ${timeAgo(match.last_opened_at)}</div>
      `;
      indicator.appendChild(popover);

      indicator.addEventListener('mouseenter', () => { popover.style.display = 'block'; });
      indicator.addEventListener('mouseleave', () => { popover.style.display = 'none'; });
    } else {
      indicator.textContent = '✓✓';
      indicator.style.color = '#bbb';
      indicator.title = 'Sent — not opened yet';
    }

    headerRow.appendChild(indicator);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", watchCompose);
  } else {
    watchCompose();
  }
})();
