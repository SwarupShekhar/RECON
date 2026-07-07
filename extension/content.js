(() => {
  "use strict";

  // Gmail's SPA can re-fire tab "complete" events without a real reload,
  // and the service worker re-injects content.js each time into the same
  // persistent isolated world. Without this guard, old setInterval/
  // MutationObserver instances never die and stack up — each poll cycle
  // multiplies DB load until the connection pool exhausts.
  if (window.__reconWatching) {
    console.log("[Recon] Already active in this page, skipping duplicate injection.");
    return;
  }
  window.__reconWatching = true;

  let config = { serverUrl: "", senderEmail: "", apiKey: "" };

  function defaultServerUrl() {
    return (typeof RECON_DEFAULT_SERVER_URL === "string" && RECON_DEFAULT_SERVER_URL) || "";
  }

  chrome.storage.sync.get(["serverUrl", "senderEmail", "apiKey"], (data) => {
    config.serverUrl = data.serverUrl || defaultServerUrl();
    config.senderEmail = data.senderEmail || "";
    config.apiKey = data.apiKey || "";
    console.log("[Recon] Loaded config:", config.serverUrl, config.senderEmail ? "(email set)" : "", config.apiKey ? "(api key set)" : "");
  });

  chrome.storage.onChanged.addListener((changes) => {
    if (changes.serverUrl) config.serverUrl = changes.serverUrl.newValue;
    if (changes.senderEmail) config.senderEmail = changes.senderEmail.newValue;
    if (changes.apiKey) config.apiKey = changes.apiKey.newValue;
  });

  // ---- Phase 5: Self-tracking suppression ----
  //
  // Fires right before the sender is about to render their own Sent-view
  // copy of a tracked thread, so the pixel fetch(es) that follow get
  // flagged internal server-side instead of counted as a real open. Muted
  // once per thread_id per page lifetime — server-side window (30s) covers
  // render + read time, re-muting on every poll isn't needed.
  const mutedThreadIds = new Set();

  // After the extension is reloaded/updated, content scripts already injected
  // into open Gmail tabs are orphaned: `chrome.runtime` still exists but has no
  // `id`, and any sendMessage throws "Extension context invalidated". Guard the
  // messaging entry points so an orphaned script degrades quietly (and reminds
  // the user to refresh the tab) instead of throwing mid-send.
  function runtimeAlive() {
    try {
      if (chrome.runtime && chrome.runtime.id) return true;
    } catch (err) {
      /* accessing chrome.runtime can itself throw once invalidated */
    }
    console.warn("[Recon] Extension context invalidated — reload this Gmail tab (Cmd+R).");
    return false;
  }

  function muteThread(threadId) {
    if (!threadId || !config.serverUrl || mutedThreadIds.has(threadId)) return;
    if (!runtimeAlive()) return;
    mutedThreadIds.add(threadId);
    chrome.runtime.sendMessage(
      { type: "mute", serverUrl: config.serverUrl, threadId, seconds: 30 },
      () => void chrome.runtime.lastError // fire-and-forget, ignore result
    );
  }

  // A brand-new compose has no thread_id yet (Gmail only assigns one after
  // send completes), so muteThread() can't cover it. Gmail frequently
  // re-renders the just-sent message in the sender's own tab within a few
  // seconds of hitting Send — that fires the embedded pixel from the
  // sender's own browser and gets misread as the recipient opening it.
  // Mute the exact tracker ids we just created, right after send, so this
  // covers new composes the same way muteThread covers Sent-list clicks.
  function muteEmails(emailIds) {
    if (!emailIds || emailIds.length === 0 || !config.serverUrl) return;
    if (!runtimeAlive()) return;
    chrome.runtime.sendMessage(
      { type: "mute", serverUrl: config.serverUrl, emailIds, seconds: 30 },
      () => void chrome.runtime.lastError
    );
  }

  // `links` (optional) is an array of {id, url, type} for link/PDF click
  // tracking (Phase 6) — only passed on ONE recipient's call per send since
  // the compose body/its links are shared across all recipients, not
  // per-recipient. `id`/`trackerId` are generated client-side (see
  // handleSend) so the pixel and link hrefs can be applied synchronously,
  // before this call is even made — this is a true fire-and-forget
  // registration, its timing no longer affects what gets sent.
  async function registerTracker(trackerId, recipientEmail, subject, threadId, field, links, allRecipients) {
    return new Promise((resolve, reject) => {
      if (!runtimeAlive()) {
        reject(new Error("Extension context invalidated — reload the Gmail tab."));
        return;
      }
      const payload = {
        id: trackerId,
        sender_email: config.senderEmail,
        recipient_email: recipientEmail,
        subject: subject,
        thread_id: threadId,
        recipient_field: field,
      };
      if (links && links.length > 0) payload.links = links;
      if (allRecipients && allRecipients.length > 0) payload.all_recipients = allRecipients;

      chrome.runtime.sendMessage(
        {
          type: "track",
          serverUrl: config.serverUrl,
          payload,
        },
        (response) => {
          if (chrome.runtime.lastError) {
            reject(new Error(chrome.runtime.lastError.message));
          } else if (response?.ok) {
            resolve({ trackerId: response.tracker_id, links: response.links || [] });
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

  function extractEmailsFromString(str) {
    if (!str) return [];
    const matches = String(str).match(/[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}/g);
    return matches || [];
  }

  // Dumps compose recipient DOM (inputs, chips, per-chip geometry) at send
  // time so To/Cc/Bcc parsing can be debugged against real Gmail markup.
  function debugDumpRecipients(container, force) {
    // Runs when window.__RECON_DEBUG_RECIPIENTS is set, OR when forced (e.g.
    // parsing found no recipients — auto-capture the DOM so it can be fixed
    // from a real failure without the user toggling a flag first).
    if (!force && !window.__RECON_DEBUG_RECIPIENTS) return;
    try {
      const info = { inputs: [], chipCount: 0, ariaLabels: [], chips: [] };
      // Every recipient-ish input by name OR aria-label, with its value — shows
      // whether Gmail populated to/cc/bcc at send-click and where each lives.
      container.querySelectorAll("input,textarea").forEach((el) => {
        const name = el.getAttribute("name") || "";
        const aria = el.getAttribute("aria-label") || "";
        if (/^(to|cc|bcc)$/i.test(name) || /recipient|\bto\b|\bcc\b|\bbcc\b/i.test(aria)) {
          const r = el.getBoundingClientRect();
          info.inputs.push({
            name, aria, value: el.value || "",
            top: Math.round(r.top), h: Math.round(r.height),
          });
        }
      });
      info.chipCount = container.querySelectorAll("span[email]").length;
      container.querySelectorAll("[aria-label]").forEach((el) => {
        const al = el.getAttribute("aria-label") || "";
        if (/recipient|cc|bcc|\bto\b/i.test(al) && info.ariaLabels.length < 25) {
          info.ariaLabels.push({
            tag: el.tagName,
            aria: al,
            chips: el.querySelectorAll("span[email]").length,
          });
        }
      });
      container.querySelectorAll("span[email]").forEach((chip) => {
        if (info.chips.length >= 12) return;
        let node = chip.parentElement;
        let hint = null;
        for (let i = 0; i < 10 && node && node !== container; i++) {
          const nm = node.getAttribute && (node.getAttribute("name") || node.getAttribute("aria-label"));
          if (nm) { hint = { depth: i, attr: nm, tag: node.tagName }; break; }
          node = node.parentElement;
        }
        const fieldRows = collectRecipientFieldRows(container);
        const rect = chip.getBoundingClientRect();
        info.chips.push({
          email: chip.getAttribute("email"),
          hint,
          geometry: {
            top: Math.round(rect.top),
            field: fieldForChipByGeometry(chip, fieldRows),
            rows: fieldRows.map((r) => ({ field: r.field, mid: Math.round(r.mid) })),
          },
          preceding: fieldForChipByPrecedingInput(chip, container),
        });
      });
      // Field-container view (Method 0): where the real fix reads from. Shows,
      // per aria-labeled field container, which chip addresses live under it.
      info.fieldContainers = [];
      [
        ["to", '[aria-label="To"]'],
        ["cc", '[aria-label="Cc"]'],
        ["bcc", '[aria-label="Bcc"]'],
      ].forEach(([field, sel]) => {
        container.querySelectorAll(sel).forEach((fc) => {
          const emails = [];
          fc.querySelectorAll(CHIP_SELECTOR).forEach((chipEl) => {
            const e = emailFromChipEl(chipEl);
            if (e && !emails.includes(e)) emails.push(e);
          });
          info.fieldContainers.push({ field, sel, emails });
        });
      });
      info.method0 = collectRecipientsByFieldContainer(container);
      console.log("[Recon][diag] recipient DOM ->", JSON.stringify(info, null, 2));
    } catch (e) {
      console.warn("[Recon][diag] dump failed:", e);
    }
  }

  function recipientInputSelector(field) {
    const labels = {
      to: ["To recipients", "to recipients"],
      cc: ["CC recipients", "Cc recipients", "cc recipients"],
      bcc: ["BCC recipients", "Bcc recipients", "bcc recipients"],
    };
    const names = labels[field] || [];
    const parts = [`input[name="${field}"]`];
    names.forEach((label) => {
      parts.push(`input[aria-label="${label}"]`);
    });
    return parts.join(", ");
  }

  function fieldFromRecipientLabel(label) {
    const l = (label || "").trim().toLowerCase();
    if (l === "to recipients" || l === "to") return "to";
    if (l === "cc recipients" || l === "cc") return "cc";
    if (l === "bcc recipients" || l === "bcc") return "bcc";
    return null;
  }

  // Classify a recipient <input> element to its field via name or aria-label.
  function inputFieldOf(input) {
    const name = input.getAttribute("name");
    if (name === "to" || name === "cc" || name === "bcc") return name;
    return fieldFromRecipientLabel(input.getAttribute("aria-label") || "");
  }

  function anyRecipientInputSelector() {
    return ["to", "cc", "bcc"].map(recipientInputSelector).join(", ");
  }

  // One row per To/Cc/Bcc field, using the best recipient input for geometry.
  function collectRecipientFieldRows(container) {
    const byField = new Map();
    container.querySelectorAll(anyRecipientInputSelector()).forEach((input) => {
      const field = inputFieldOf(input);
      if (!field) return;
      const aria = (input.getAttribute("aria-label") || "").toLowerCase();
      const score =
        input.getAttribute("name") === field ? 3 : aria.includes("recipients") ? 2 : 1;
      const rect = input.getBoundingClientRect();
      if (!rect.height && !rect.width) return;
      const existing = byField.get(field);
      if (!existing || score > existing.score) {
        byField.set(field, {
          field,
          top: rect.top,
          bottom: rect.bottom,
          mid: rect.top + rect.height / 2,
          score,
        });
      }
    });
    return [...byField.values()].sort((a, b) => a.top - b.top);
  }

  // Gmail often renders chips outside the input subtree. Match each chip to
  // the To/Cc/Bcc row whose vertical center is closest to the chip's center.
  function fieldForChipByGeometry(chip, fieldRows) {
    if (!fieldRows.length) return null;
    const rect = chip.getBoundingClientRect();
    if (!rect.height && !rect.width) return null;
    const chipMid = rect.top + rect.height / 2;
    let best = null;
    let bestDist = Infinity;
    fieldRows.forEach((row) => {
      const dist = Math.abs(chipMid - row.mid);
      if (dist < bestDist) {
        bestDist = dist;
        best = row.field;
      }
    });
    return best;
  }

  // For each chip, use the last recipient input seen before it in DOM order.
  function fieldForChipByPrecedingInput(chip, container) {
    let lastField = null;
    const walker = document.createTreeWalker(container, NodeFilter.SHOW_ELEMENT);
    let node = walker.currentNode;
    while (node) {
      if (node === chip) return lastField;
      if (node.tagName === "INPUT") {
        const f = inputFieldOf(node);
        if (f) lastField = f;
      }
      node = walker.nextNode();
    }
    return lastField;
  }

  // Gmail autocomplete / people-picker rows — not committed recipients.
  function isAutocompleteSuggestion(el) {
    if (!el) return false;
    return !!el.closest(
      '[role="listbox"], [role="menu"], [role="option"], [data-actas="people-chip-suggestion"]'
    );
  }

  // Only count chips the user actually added to To/Cc/Bcc (not hover suggestions).
  function isCommittedRecipientChip(el, container) {
    if (!el || isAutocompleteSuggestion(el)) return false;
    const rect = el.getBoundingClientRect();
    if (!rect.width && !rect.height) return false;
    if (el.closest('[aria-label="Press delete to remove this chip"]')) return true;
    if (el.getAttribute("aria-label") === "Press delete to remove this chip") return true;
    if (el.tagName === "SPAN" && el.hasAttribute("email")) {
      return !!el.closest(
        '[aria-label="To"], [aria-label="to"], [aria-label="Cc"], [aria-label="CC"], ' +
        '[aria-label="cc"], [aria-label="Bcc"], [aria-label="BCC"], [aria-label="bcc"]'
      );
    }
    return false;
  }

  // Gmail's hidden to/cc/bcc inputs reflect the addresses actually sent.
  function collectRecipientsFromInputs(container) {
    const results = [];
    const seen = new Set();
    ["to", "cc", "bcc"].forEach((field) => {
      container.querySelectorAll(recipientInputSelector(field)).forEach((input) => {
        extractEmailsFromString(input.value).forEach((e) => {
          const norm = e.trim().toLowerCase();
          if (!norm || seen.has(norm)) return;
          seen.add(norm);
          results.push({ email: norm, field });
        });
      });
    });
    return results;
  }

  // span[email]: some chips only carry data-hovercard-id, and the chip
  // wrapper is aria-label="Press delete to remove this chip". Matching only
  // span[email] silently dropped co-recipients (the diag showed 2 real chips
  // but chipCount:1). This catches all representations.
  const CHIP_SELECTOR =
    'span[email], [data-hovercard-id], [aria-label="Press delete to remove this chip"]';

  // Pull an address off a chip element from whichever attribute Gmail used.
  function emailFromChipEl(el) {
    const attrEmail = el.getAttribute && el.getAttribute("email");
    if (attrEmail && attrEmail.includes("@")) return attrEmail;
    const hover = el.getAttribute && el.getAttribute("data-hovercard-id");
    if (hover && hover.includes("@")) return hover;
    const fromText = extractEmailsFromString(el.textContent);
    if (fromText.length) return fromText[0];
    const dataName = el.getAttribute && el.getAttribute("data-name");
    if (dataName && dataName.includes("@")) return dataName;
    return null;
  }

  // Most reliable path: read chips from each field's own aria-labeled
  // container (div[aria-label="To"|"Cc"|"Bcc"]). These containers persist even
  // after Gmail collapses the recipient editor on send-click blur (when the
  // inputs report top:0/height:0 and geometry matching is impossible). Field
  // is defined by which container the chip lives under — no ordering guess.
  function collectRecipientsByFieldContainer(container) {
    const out = [];
    const seen = new Set();
    const fields = [
      ["to", ['[aria-label="To"]', '[aria-label="to"]']],
      ["cc", ['[aria-label="Cc"]', '[aria-label="CC"]', '[aria-label="cc"]']],
      ["bcc", ['[aria-label="Bcc"]', '[aria-label="BCC"]', '[aria-label="bcc"]']],
    ];
    fields.forEach(([field, selectors]) => {
      selectors.forEach((sel) => {
        container.querySelectorAll(sel).forEach((fc) => {
          fc.querySelectorAll(CHIP_SELECTOR).forEach((chipEl) => {
            // No visibility check and no suggestion filter here — both proven
            // wrong against live Gmail (diag 2026-07-07): at send-click the
            // editor is collapsed so chips report 0x0 rects, and committed
            // chips are role="option" inside a role="listbox" (the same roles
            // the suggestion dropdown uses, so isAutocompleteSuggestion eats
            // real recipients). Being inside the [aria-label="To"|"Cc"|"Bcc"]
            // container IS the commitment signal; the dropdown lives outside.
            const email = emailFromChipEl(chipEl);
            if (!email) return;
            const norm = email.trim().toLowerCase();
            const key = norm + "|" + field;
            if (seen.has(key)) return;
            seen.add(key);
            out.push({ email: norm, field });
          });
        });
      });
    });
    return out;
  }

  function getRecipientEmails(container) {
    const rank = { to: 0, cc: 1, bcc: 2 };
    const sortRecipients = (list) =>
      [...list].sort((a, b) => (rank[a.field] ?? 3) - (rank[b.field] ?? 3));

    // Gmail's hidden to/cc/bcc input values are the send payload — they exclude
    // autocomplete suggestions that never got added. Prefer them whenever set.
    const fromInputs = collectRecipientsFromInputs(container);
    if (fromInputs.length > 0) {
      return sortRecipients(fromInputs);
    }

    // email(lowercased) -> { email, field, authority }. Highest authority wins.
    const byEmail = new Map();
    const record = (email, field, authority) => {
      if (!email) return;
      const norm = String(email).trim().toLowerCase();
      if (!norm) return;
      const existing = byEmail.get(norm);
      if (!existing || authority > existing.authority) {
        byEmail.set(norm, { email: norm, field: field || "to", authority });
      }
    };

    const fieldRows = collectRecipientFieldRows(container);
    const selector = anyRecipientInputSelector();

    collectRecipientsByFieldContainer(container).forEach((r) => record(r.email, r.field, 5));

    const chips = container.querySelectorAll(CHIP_SELECTOR);
    chips.forEach((chip) => {
      if (!isCommittedRecipientChip(chip, container)) return;
      const email = emailFromChipEl(chip);
      if (!email) return;
      const geometric = fieldForChipByGeometry(chip, fieldRows);
      if (geometric) {
        record(email, geometric, 4);
        return;
      }
      const preceding = fieldForChipByPrecedingInput(chip, container);
      if (preceding) {
        record(email, preceding, 3);
        return;
      }
      record(email, "to", 0);
    });

    if (byEmail.size === 0) {
      container.querySelectorAll(selector).forEach((input) => {
        const field = inputFieldOf(input);
        if (!field) return;
        extractEmailsFromString(input.value).forEach((e) => record(e, field, 2));
      });
    }

    // Last-resort catch-all: the gated methods above all require a chip to sit
    // inside a [aria-label="To"|"Cc"|"Bcc"] container. Some Gmail layouts nest
    // chips differently, so every chip gets rejected and we track nothing.
    // Here we accept ANY non-suggestion recipient chip found in the compose or
    // its surrounding dialog, so a real send is never dropped. Field defaults
    // to "to" (attribution is best-effort in this fallback) — but the email
    // gets tracked, which is the priority.
    if (byEmail.size === 0) {
      const roots = [container];
      const dialog = container.closest('div[role="dialog"]');
      if (dialog && dialog !== container) roots.push(dialog);
      // Exclude the sender's own address(es): the configured one AND whatever
      // the compose From row shows — they can differ (send-as / aliases), and
      // the From chip matches CHIP_SELECTOR just like recipient chips do.
      const ownAddresses = new Set();
      const configured = (config.senderEmail || "").trim().toLowerCase();
      if (configured) ownAddresses.add(configured);
      roots.forEach((root) => {
        root.querySelectorAll('input[name="from"], [aria-label*="From"] [email], span.gD[email]').forEach((el) => {
          const v = el.value || el.getAttribute("email") || "";
          extractEmailsFromString(v).forEach((e) => ownAddresses.add(e.toLowerCase()));
        });
      });
      roots.forEach((root) => {
        root.querySelectorAll(CHIP_SELECTOR).forEach((chip) => {
          if (isAutocompleteSuggestion(chip)) return;
          const email = emailFromChipEl(chip);
          if (!email) return;
          const norm = email.trim().toLowerCase();
          if (ownAddresses.has(norm)) return;
          const geometric = fieldForChipByGeometry(chip, fieldRows);
          record(norm, geometric || "to", geometric ? 4 : 0);
        });
      });
      if (byEmail.size > 0) {
        console.log("[Recon] Recipients recovered via catch-all chip scan");
        // The precise parser failed — flag it so handleSend dumps the DOM and
        // we can fix field attribution from real markup instead of guessing.
        container._reconUsedCatchAll = true;
      }
    }

    return sortRecipients(
      [...byEmail.values()].map((r) => ({ email: r.email, field: r.field }))
    );
  }

  function getSubject(container) {
    const input = container.querySelector('input[name="subjectbox"]');
    if (input) return input.value;
    const h2 = container.querySelector('h2');
    return h2 ? h2.textContent : "";
  }

  // Shared by getThreadId() (compose-time) and injectThreadCheckmark()
  // (open-thread rendering) so both rely on the exact same hash pattern
  // instead of two regexes drifting apart.
  function extractThreadIdFromHash(hash) {
    const match = hash.match(/\/(\d+[a-f0-9]+)/);
    return match ? match[1] : null;
  }

  function getThreadId(container) {
    const hash = window.location.hash;
    const fromHash = extractThreadIdFromHash(hash);
    if (fromHash) return fromHash;

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

  // ---- Link / PDF click tracking (Phase 6, new) ----

  function isOwnTrackedUrl(href) {
    if (!config.serverUrl) return false;
    try {
      return href.indexOf(config.serverUrl) === 0;
    } catch (err) {
      return false;
    }
  }

  // Scans the compose body ONCE per send for <a href> the rep actually typed
  // or pasted into the message. Real file attachments are separate MIME
  // parts, not part of this DOM, so they can't be tracked this way — "PDF
  // tracking" here is limited to typed/pasted links whose href ends in
  // .pdf or whose visible text mentions "pdf".
  //
  // Returns [] (without re-scanning) if this body was already processed —
  // guards against re-collecting/re-sending the same links on a retry
  // within the same send.
  function collectTrackableLinks(body) {
    if (!body || body.dataset.reconLinksDone === "1") return [];

    const links = [];
    try {
      const anchors = body.querySelectorAll("a[href]");
      anchors.forEach((a) => {
        const href = (a.getAttribute("href") || "").trim();
        if (!href) return;
        if (href.toLowerCase().startsWith("mailto:")) return;
        if (isOwnTrackedUrl(href)) return;

        const text = (a.textContent || "").toLowerCase();
        const isPdf = href.toLowerCase().split(/[?#]/)[0].endsWith(".pdf") || text.includes("pdf");
        links.push({ url: href, type: isPdf ? "pdf" : "link", el: a });
      });
    } catch (err) {
      console.warn("[Recon] collectTrackableLinks failed:", err);
    }
    return links;
  }

  // Rewrites each collected <a>'s href to its pre-generated tracked_url.
  // Client-generated (see handleSend) so this runs synchronously, before any
  // network await — same reasoning as pixel injection below. Idempotent per
  // body via the reconLinksDone flag.
  function applyTrackedLinksSync(body, collected) {
    if (!body || body.dataset.reconLinksDone === "1") return;
    try {
      collected.forEach((item) => {
        if (!item.el || !item.trackedUrl) return;
        item.el.setAttribute("href", item.trackedUrl);
      });
      console.log("[Recon] Rewrote", collected.length, "link(s) to tracked URLs");
    } catch (err) {
      console.warn("[Recon] applyTrackedLinksSync failed:", err);
    } finally {
      body.dataset.reconLinksDone = "1";
    }
  }

  function handleSend(container) {
    if (!config.serverUrl || !config.senderEmail) {
      console.warn("[Recon] No config — click extension icon to set server URL and email");
      return;
    }

    if (container._reconSendHandled) {
      console.log("[Recon] Send already handled for this compose window, skipping duplicate");
      return;
    }
    container._reconSendHandled = true;
    setTimeout(() => { container._reconSendHandled = false; }, 4000);

    const body = getComposeBody(container);
    if (!body) {
      console.warn("[Recon] Could not find compose body");
      return;
    }

    debugDumpRecipients(container);
    container._reconUsedCatchAll = false;
    let recipients = getRecipientEmails(container);
    const snapshot = container._reconRecipientSnapshot;
    // The mousedown snapshot was taken while the editor was still expanded, so
    // it can be strictly better than the click-time read (which may have hit
    // the collapsed DOM and recovered fewer recipients via catch-all).
    if (snapshot && snapshot.length > recipients.length) {
      recipients = snapshot;
      console.log("[Recon] Using recipient snapshot from mousedown");
    }
    if (recipients.length === 0) {
      console.warn("[Recon] No recipients found");
      // Auto-capture the compose DOM so the parser can be fixed from a real
      // failure without the user needing to toggle a debug flag first.
      debugDumpRecipients(container, true);
      return;
    }
    if (container._reconUsedCatchAll) {
      // Tracking succeeded but only via the last-resort scan — the precise
      // To/Cc/Bcc parser failed on this DOM. Dump it so attribution can be
      // fixed from real markup.
      debugDumpRecipients(container, true);
    }

    const subject = getSubject(container);
    const threadId = getThreadId(container);

    console.log("[Recon] Sending to:", recipients.map(r => `${r.email} (${r.field})`).join(", "), "subject:", subject);

    // Gmail sends ONE identical HTML body to every recipient on a To/Cc
    // send — there is no way to embed a distinct pixel per co-recipient and
    // know which of them actually opened it (whoever opens their copy fires
    // every embedded image indiscriminately). We used to loop and call
    // createTracker+injectPixel per recipient anyway: injectPixel no-ops
    // past the first call (it bails if a pixel is already in the body), so
    // every recipient after the first got a real DB row that could never
    // possibly register an open — a permanently-stuck "Not opened" row that
    // misrepresented what we can actually observe. Only the primary
    // recipient (first "to", falling back to whoever's first) is
    // trackable; track only that one.
    const primary = recipients[0];

    // Everything the sent-out HTML needs (pixel src, rewritten link hrefs)
    // is generated CLIENT-SIDE and applied synchronously, before any
    // network `await`. Gmail's own send handler is a bubble-phase listener
    // on an ancestor of the button we capture on — it can only run after
    // this synchronous handler returns, so as long as the DOM mutation
    // happens in this call stack, it happens-before Gmail serializes and
    // transmits the compose body. Registering the tracker/links with the
    // server is genuinely fire-and-forget from here on — its timing no
    // longer affects what bytes get sent.
    const trackerId = crypto.randomUUID();

    const trackableLinks = collectTrackableLinks(body);
    trackableLinks.forEach((l) => {
      l.id = crypto.randomUUID();
      l.trackedUrl = `${config.serverUrl}/l/${l.id}`;
    });
    if (trackableLinks.length > 0) {
      applyTrackedLinksSync(body, trackableLinks);
    }

    injectPixel(body, trackerId);
    muteEmails([trackerId]);

    const linksForThisCall = trackableLinks.length > 0
      ? trackableLinks.map(l => ({ id: l.id, url: l.url, type: l.type }))
      : undefined;

    const allRecipients = recipients.map(r => ({ email: r.email, field: r.field }));

    registerTracker(trackerId, primary.email, subject, threadId, primary.field, linksForThisCall, allRecipients)
      .catch((err) => console.error("[Recon] Tracker registration failed:", err));

    container._reconRecipientSnapshot = null;
  }

  function isScheduleSendConfirmButton(el) {
    if (!el) return false;
    try {
      const label = (el.getAttribute?.('aria-label') || '').toLowerCase();
      if (label.includes('schedule send')) return true;
      const text = (el.textContent || '').trim().toLowerCase();
      // Keep this narrow (short text) so we don't accidentally bind every
      // button on the page that happens to contain these words somewhere
      // in a larger container's textContent.
      return text.length > 0 && text.length < 40 && text.includes('schedule send');
    } catch (err) {
      return false;
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
      'div[role="button"][aria-label*="Schedule send"]',
      'div[role="button"][aria-label*="schedule send"]',
    ];
    for (const sel of selectors) {
      const btn = el.querySelector(sel);
      if (btn) return btn;
    }
    return null;
  }

  function findComposeContainer(btn) {
    const direct = btn.closest('div[role="dialog"]') || btn.closest('div.nH');
    if (direct) return direct;

    // Gmail's "Schedule send" confirm button lives in a small popup dialog
    // that isn't always nested inside the compose window's own DOM subtree.
    // Fall back to locating an open compose window elsewhere on the page —
    // best-effort, since we can't verify this against a live Gmail DOM.
    try {
      const dialogs = document.querySelectorAll('div[role="dialog"]');
      for (let i = dialogs.length - 1; i >= 0; i--) {
        const d = dialogs[i];
        if (
          d.querySelector('input[name="subjectbox"]') ||
          d.querySelector('div[aria-label*="Message Body"]') ||
          d.querySelector('div[contenteditable="true"]')
        ) {
          return d;
        }
      }
    } catch (err) {
      console.warn('[Recon] findComposeContainer fallback failed:', err);
    }

    return btn.parentElement?.parentElement?.parentElement || null;
  }

  // Gmail collapses the recipient editor the instant Send is clicked, which
  // can zero out inputs/chips before the click handler reads them. mousedown
  // fires first, while the editor is still expanded — snapshot recipients then
  // (using the same getRecipientEmails) so handleSend always has a real list.
  function snapshotRecipients(container) {
    try {
      const recipients = getRecipientEmails(container);
      if (recipients.length > 0) {
        container._reconRecipientSnapshot = recipients;
      }
    } catch (err) {
      console.warn("[Recon] recipient snapshot failed:", err);
    }
  }

  function bindSendButton(btn) {
    if (btn._reconBound) return;
    const container = findComposeContainer(btn);
    if (!container) return;
    if (container._reconSendButtonBound) return;

    btn._reconBound = true;
    container._reconSendButtonBound = true;
    btn.addEventListener("mousedown", () => {
      snapshotRecipients(container);
    }, true);
    btn.addEventListener("click", () => {
      console.log("[Recon] Send clicked, container found");
      handleSend(container);
    }, true);
  }

  function scanForSendButtons(root) {
    if (!root || !root.querySelectorAll) return;
    try {
      const selectors = [
        'div[role="button"][aria-label*="Send"]',
        'div[role="button"][data-tooltip*="Send"]',
        'div[role="button"][aria-label*="send"]',
        'div.T-I.J-J5-Ji[aria-label*="Send"]',
        'div[role="button"][aria-label*="Schedule send"]',
        'div[role="button"][aria-label*="schedule send"]',
      ];
      for (const sel of selectors) {
        root.querySelectorAll(sel).forEach(bindSendButton);
      }

      // Fallback for Gmail's schedule-send confirm button, whose aria-label
      // doesn't reliably include "Schedule send" — match on visible text
      // too. Defensive: Gmail's DOM here is undocumented/unstable, mirrors
      // the existing fragile-selector tradeoff already accepted in this file.
      root.querySelectorAll('div[role="button"]').forEach((el) => {
        if (isScheduleSendConfirmButton(el)) bindSendButton(el);
      });
    } catch (err) {
      console.warn('[Recon] scanForSendButtons error:', err);
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
    if (!config.serverUrl || (!config.apiKey && !config.senderEmail)) return null;
    const cacheKey = config.apiKey || config.senderEmail;
    const now = Date.now();
    if (sentCache[cacheKey] && now - sentCache[cacheKey].time < 4000) {
      return sentCache[cacheKey].data;
    }
    try {
      const path = config.apiKey
        ? "/status/sent"
        : `/status/sent?sender_email=${encodeURIComponent(config.senderEmail)}`;
      const headers = config.apiKey ? { "X-API-Key": config.apiKey } : {};
      const data = await new Promise((resolve, reject) => {
        chrome.runtime.sendMessage(
          {
            type: "fetch",
            serverUrl: config.serverUrl,
            path,
            headers,
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

  // Best-effort thread-id extraction for a Gmail list row. Gmail row markup
  // is undocumented and changes frequently; this tries a few plausible
  // spots (anchor hrefs encoding a thread id, common data-* attributes)
  // and returns null if none pan out, in which case callers fall back to
  // the existing recipient+subject fuzzy heuristic. NOTE: unverified
  // against a live Gmail DOM — flagged for manual smoke-testing.
  function getThreadIdFromRow(row) {
    try {
      const links = row.querySelectorAll('a[href*="#"]');
      for (const link of links) {
        const href = link.getAttribute('href') || '';
        const id = extractThreadIdFromHash(href);
        if (id) return id;
      }

      const dataThreadEl = row.matches?.('[data-thread-id]') ? row : row.querySelector('[data-thread-id]');
      if (dataThreadEl) {
        const id = dataThreadEl.getAttribute('data-thread-id');
        if (id) return id;
      }

      const legacyEl = row.matches?.('[data-legacy-thread-id]') ? row : row.querySelector('[data-legacy-thread-id]');
      if (legacyEl) {
        const id = legacyEl.getAttribute('data-legacy-thread-id');
        if (id) return id;
      }
    } catch (err) {
      console.warn('[Recon] getThreadIdFromRow failed:', err);
    }
    return null;
  }

  // ---- Shared matching + popover helpers (thread_id-first, fuzzy fallback) ----

  function matchSentDataByThreadId(sentData, threadId) {
    if (!threadId) return null;
    const matches = sentData.filter((e) => e.thread_id != null && String(e.thread_id) === String(threadId));
    return matches.length > 0 ? matches : null;
  }

  // excludeIds: sentData entries already claimed by another row in this same
  // pass. Fuzzy matching (recipient+subject only, no thread_id) is ambiguous
  // by nature — without this, two different Sent-list rows with the same
  // recipient+subject (e.g. repeated test sends) can both latch onto the
  // same underlying entry, bleeding an older email's open status onto a
  // brand-new, actually-unopened row.
  function matchSentDataFuzzy(sentData, recipient, subject, excludeIds) {
    const match = sentData.find((e) => {
      if (excludeIds && excludeIds.has(e.id)) return false;
      const recipientMatch = !recipient || e.recipient === recipient;
      const subjectMatch = !subject || !e.subject ||
        e.subject.toLowerCase().includes(subject.toLowerCase()) ||
        subject.toLowerCase().includes(e.subject?.toLowerCase() || '');
      return recipientMatch && subjectMatch;
    });
    return match ? [match] : null;
  }

  // Thread-id match takes priority (exact, unambiguous); only fall back to
  // the recipient+subject heuristic when no thread_id is available (known
  // limitation: thread_id is null for brand-new sends until Gmail assigns
  // one — see HANDOFF.md known issues) or nothing matched by id.
  function getMatchesForContext(sentData, threadId, recipient, subject, excludeIds) {
    return matchSentDataByThreadId(sentData, threadId) ||
      matchSentDataFuzzy(sentData, recipient, subject, excludeIds) ||
      [];
  }

  function recipientFieldSortKey(field) {
    const order = { to: 0, cc: 1, bcc: 2 };
    return order[(field || '').toLowerCase()] ?? 3;
  }

  // Green if any recipient has a verified open, orange if only
  // unverified/MPP opens exist, grey if nobody has opened yet.
  function computeOverallStatus(matches) {
    const anyVerified = matches.some((m) => (m.verified_opens || 0) > 0);
    if (anyVerified) return 'green';
    const anyOpened = matches.some((m) => (m.total_opens || 0) > 0);
    if (anyOpened) return 'orange';
    return 'grey';
  }

  function statusColor(status) {
    if (status === 'green') return '#0d9e3f';
    if (status === 'orange') return '#e67e22';
    return '#bbb';
  }

  // Real SVG instead of a unicode "✓✓" + negative letter-spacing hack — the
  // text-glyph version rendered inconsistently across OS/font stacks
  // (sometimes drawing as two visibly separated pairs instead of one tight
  // double-check), which read as broken/duplicated to users.
  function checkmarkSvg(color, size = 16) {
    return `<svg width="${size}" height="${size}" viewBox="0 0 16 16" fill="none" style="display:block;">
      <path d="M1.5 8.3 4.2 11 8 6.6" stroke="${color}" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
      <path d="M6.3 8.3 9 11 14 5" stroke="${color}" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
    </svg>`;
  }

  // Builds popover HTML for one or many sentData entries on the same
  // thread (per-recipient breakdown for group/CC/BCC emails — each
  // recipient gets its own line with its own open count / status / last
  // opened time, sorted To -> Cc -> Bcc).
  function buildPopoverHtml(matches) {
    const sorted = [...matches].sort((a, b) => recipientFieldSortKey(a.recipient_field) - recipientFieldSortKey(b.recipient_field));

    const rows = sorted.map((m) => {
      const isVerified = (m.verified_opens || 0) > 0;
      const opened = (m.total_opens || 0) > 0;
      const dotColor = isVerified ? '#0d9e3f' : opened ? '#e67e22' : '#bbb';
      const fieldLabel = m.recipient_field ? m.recipient_field.toUpperCase() : '';
      const statusText = opened
        ? `${m.total_opens > 1 ? `Opened ${m.total_opens}x` : 'Opened'}${m.last_opened_at ? ` · ${timeAgo(m.last_opened_at)}` : ''}${!isVerified ? ' (unverified)' : ''}`
        : 'Not opened yet';

      return `
        <div style="display:flex;align-items:center;gap:6px;padding:3px 0;">
          <span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${dotColor};flex-shrink:0;"></span>
          <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${m.recipient || ''}${fieldLabel ? ` <span style="color:#999;font-size:11px;">(${fieldLabel})</span>` : ''}</span>
          <span style="color:#666;font-size:11px;white-space:nowrap;">${statusText}</span>
        </div>`;
    }).join('');

    const title = sorted.length > 1
      ? `${sorted.length} recipients`
      : (sorted[0]?.recipient ? `${sorted[0].recipient}` : 'Recipient');

    return `<div style="font-weight:600;margin-bottom:6px;">${title}</div><div style="color:#555;">${rows}</div>`;
  }

  function injectSentCheckmarks() {
    return injectSentCheckmarksAsync().catch((err) => console.warn('[Recon] injectSentCheckmarks failed:', err));
  }

  async function injectSentCheckmarksAsync() {
    const hash = window.location.hash;
    // Restricted to Sent only (bug fix): Inbox list rows show the SENDER of
    // an incoming message as their span[email] chip, not a tracked
    // recipient — matching that against outbound sentData mistags unrelated
    // inbound mail. injectThreadCheckmark (below) is scoped to one specific
    // opened thread instead of ambiguous row-level chips, so it's safe to
    // run on both #sent/ and #inbox/.
    if (!hash.includes('#sent')) return;

    const sentData = await fetchSentStatus();
    if (!sentData || sentData.length === 0) return;

    const rows = document.querySelectorAll(
      'tr.zA, tr[class*="zA"], div[role="listitem"], tr[jscontroller]'
    );

    const now = Date.now();
    const GIVE_UP_MS = 5 * 60 * 1000;
    // Rows are processed top-to-bottom, matching sentData's newest-first
    // order. Once a fuzzy match claims a sentData entry, no other row in
    // this pass may reuse it — prevents two Sent-list rows with the same
    // recipient+subject (repeated test sends, reused subject lines) from
    // both showing the same (possibly stale/opened) status.
    const usedEmailIds = new Set();

    function applyMatch(row, matches) {
      matches.forEach((m) => usedEmailIds.add(m.id));
      row._reconMatchedIds = matches.map((m) => m.id);
      renderRowIndicator(row, matches);

      // Self-tracking suppression: clicking into this row is what triggers
      // Gmail to render the Sent-view copy (and fetch its pixel(s)). Mute
      // the thread(s) here, before navigation, so the mute is in place
      // ahead of the pixel fetch rather than racing it.
      if (!row._reconClickBound) {
        row._reconClickBound = true;
        const rowThreadIds = [...new Set(matches.map((m) => m.thread_id).filter(Boolean))];
        row.addEventListener("click", () => rowThreadIds.forEach(muteThread), true);
      }
    }

    for (const row of rows) {
      if (row._reconGaveUp) continue;

      // Already matched on an earlier poll — re-fetch by id instead of
      // re-running fuzzy matching, so an unopened -> opened transition
      // (or a growing open count) keeps showing up instead of the
      // checkmark freezing at whatever it first rendered.
      if (row._reconMatchedIds) {
        const refreshed = sentData.filter((e) => row._reconMatchedIds.includes(e.id));
        if (refreshed.length > 0) applyMatch(row, refreshed);
        continue;
      }

      const recipient = getRecipientFromRow(row);
      const subject = getSubjectFromRow(row);
      if (!recipient && !subject) continue;

      // Track first-attempt time so we can bound retries, but do NOT mark
      // the row done just because we scanned it — async tracker creation +
      // the 10s sentData cache mean the match may simply not exist yet.
      if (!row._reconFirstSeenAt) row._reconFirstSeenAt = now;

      const threadId = getThreadIdFromRow(row);
      const matches = getMatchesForContext(sentData, threadId, recipient, subject, usedEmailIds);

      if (matches.length === 0) {
        if (now - row._reconFirstSeenAt > GIVE_UP_MS) {
          row._reconGaveUp = true; // give up — bound polling cost
          console.log('[Recon] Row never matched, giving up:', { recipient, subject, threadId });
        }
        continue; // keep retrying on future polls
      }

      applyMatch(row, matches);
    }
  }

  function renderRowIndicator(row, matches) {
    const status = computeOverallStatus(matches);
    const totalOpens = matches.reduce((sum, m) => sum + (m.total_opens || 0), 0);
    const signature = `${status}:${totalOpens}`;

    // Rows are re-scanned every poll (see setInterval below), so a row
    // that was "not opened" on first render must still pick up a later
    // open — previously this bailed out permanently once any indicator
    // existed, freezing the checkmark at its first-seen state forever.
    const existing = row.querySelector('.recon-checkmark');
    if (existing) {
      if (existing.dataset.signature === signature) return;
      existing.remove();
    }

    const indicator = document.createElement('span');
    indicator.className = 'recon-checkmark';
    indicator.dataset.signature = signature;
    indicator.style.cssText = 'display:inline-flex;align-items:center;justify-content:center;width:22px;height:20px;cursor:default;user-select:none;position:relative;flex-shrink:0;';
    indicator.innerHTML = checkmarkSvg(statusColor(status));

    if (status !== 'grey') {
      const popover = document.createElement('div');
      popover.className = 'recon-popover';
      popover.style.cssText = 'display:none;position:absolute;z-index:99999;background:#fff;border:1px solid #e0e0e0;border-radius:8px;padding:12px 16px;box-shadow:0 4px 12px rgba(0,0,0,0.15);font-size:13px;line-height:1.5;max-width:320px;pointer-events:none;';
      popover.innerHTML = buildPopoverHtml(matches);
      indicator.appendChild(popover);

      indicator.addEventListener('mouseenter', () => { popover.style.display = 'block'; });
      indicator.addEventListener('mouseleave', () => { popover.style.display = 'none'; });
    } else {
      indicator.title = 'Sent — not opened yet';
    }

    // aria-label is case-varying across Gmail UI versions ("Not starred" vs
    // "Not Starred") — match case-insensitively (CSS attr-selector "i" flag)
    // so this doesn't silently fail to the prepend fallback below, which
    // dumps the indicator at the very left edge of the row (before the
    // checkbox) instead of alongside the star.
    const starEl = row.querySelector('div[role="checkbox"][aria-label*="star" i]');
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
    // Widened (bug fix) to also run on #inbox/ — a thread you sent into can
    // end up sitting in the Inbox view once there's a reply. Safe here
    // (unlike the list-row version) because this function is scoped to one
    // specific opened thread rather than ambiguous row-level chips.
    if (!/#(sent|inbox)\//.test(hash)) return;

    const sentData = await fetchSentStatus();
    if (!sentData || sentData.length === 0) return;

    // h2.hP (the subject heading) has been one of Gmail's more stable
    // selectors for years — anchor here first. span.xW.xY (per-message
    // timestamp) is far more brittle and was observed to silently stop
    // matching in testing, which made the checkmark vanish entirely with
    // no console signal. Falling back to it only if the subject anchor is
    // somehow missing, and logging either way so a future break is
    // diagnosable from the console instead of requiring a live DOM dump.
    const subjectEl = document.querySelector('h2.hP');
    const dateEl = document.querySelector('span.xW.xY span[title]');
    const headerRow = subjectEl || (dateEl && (dateEl.closest('div') || dateEl.parentElement));
    if (!headerRow) {
      console.warn('[Recon] injectThreadCheckmark: no subject or date anchor found, skipping.');
      return;
    }

    const emailEl = document.querySelector('span.go span[email]');
    const recipientEmail = emailEl?.getAttribute('email');
    const subject = subjectEl?.textContent;

    // For an OPEN thread, the thread_id is trivially available from the URL
    // hash — use the same regex as getThreadId() for consistency.
    const threadId = extractThreadIdFromHash(hash);
    const matches = getMatchesForContext(sentData, threadId, recipientEmail, subject);
    if (matches.length === 0) {
      console.log('[Recon] injectThreadCheckmark: no tracked match for', { threadId, recipientEmail, subject });
      return;
    }

    // Fallback self-tracking suppression for direct navigation/refresh into
    // an already-open tracked thread (the click-based mute in
    // injectSentCheckmarks won't have fired in that case). Late relative to
    // the pixel fetch that already happened on this render, but still
    // suppresses any repeat fetch within the window — known imperfection.
    if (threadId) muteThread(threadId);

    const status = computeOverallStatus(matches);
    const totalOpens = matches.reduce((sum, m) => sum + (m.total_opens || 0), 0);
    const signature = `${threadId || subject}:${status}:${totalOpens}`;

    // Re-run every poll tick (this function used to bail out permanently
    // once any '.recon-thread-check' existed — meaning if you opened the
    // thread while it was still unopened, the checkmark froze on "not
    // opened" forever, even after a real open landed seconds later). Now
    // we only touch the DOM when the underlying status actually changed,
    // both to keep it live and to avoid flicker/duplicate nodes on ticks
    // where nothing changed.
    const existing = document.querySelector('.recon-thread-check');
    if (existing && existing.dataset.signature === signature) return;
    if (existing) existing.remove();

    const indicator = document.createElement('span');
    indicator.className = 'recon-thread-check';
    indicator.dataset.signature = signature;
    indicator.style.cssText = 'display:inline-flex;align-items:center;vertical-align:middle;margin-left:8px;cursor:default;user-select:none;position:relative;';
    indicator.innerHTML = checkmarkSvg(statusColor(status), 18);

    if (status !== 'grey') {
      const popover = document.createElement('div');
      popover.style.cssText = 'display:none;position:absolute;z-index:99999;background:#fff;border:1px solid #e0e0e0;border-radius:8px;padding:12px 16px;box-shadow:0 4px 12px rgba(0,0,0,0.15);font-size:13px;line-height:1.5;max-width:320px;bottom:100%;left:0;margin-bottom:8px;';
      popover.innerHTML = buildPopoverHtml(matches);
      indicator.appendChild(popover);

      indicator.addEventListener('mouseenter', () => { popover.style.display = 'block'; });
      indicator.addEventListener('mouseleave', () => { popover.style.display = 'none'; });
    } else {
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
