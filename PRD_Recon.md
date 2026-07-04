# PRD: In-House Email Tracking Tool (Mailsuite Replacement)

## 1. Problem
Team uses Mailsuite (paid, per-seat) to track lead email opens on business domain. Goal: replace with owned tool. Track: opened y/n, timestamp, who (which sales rep's thread), open count.

## 2. Goals
- Track opens on outbound Gmail emails sent by team (business domain, Google Workspace).
- Show open status + timestamp inline in Gmail Sent/thread view (checkmark UI like Mailsuite).
- Own the data (Postgres), no per-seat SaaS cost.
- Flag unreliable opens (Apple MPP prefetch) separately from real opens.

## 3. Non-Goals (v1)
- Link click tracking (v2).
- Attachment tracking.
- Outlook/other clients — Gmail only.
- Mobile Gmail app extension (Chrome extension = desktop only; mobile opens still tracked via pixel, just no UI overlay on mobile).

## 4. Architecture (3 parts)

| Component | Stack | Job |
|---|---|---|
| Chrome Extension (MV3) | JS, Gmail DOM injection | Inject pixel on send, render checkmarks in Sent view |
| Pixel Server | FastAPI (or Node) | Serve 1x1 gif, log open event |
| DB | PostgreSQL (Neon) | tracker_id → sender, lead email, subject, timestamps, open_count, verified/unverified |

## 5. Data Model (v1)

**emails**
- id (uuid, pk)
- sender_email
- recipient_email
- subject
- thread_id (gmail)
- created_at

**opens**
- id (pk)
- email_id (fk)
- opened_at
- user_agent
- ip
- verified (bool) — false if Apple MPP / self-open detected

## 6. Lifecycle
1. **Send:** Extension intercepts Gmail compose submit → calls `POST /track` → gets tracker_id → injects `<img src="https://track.yourdomain.com/t/{id}/pixel.gif" width=1 height=1 style="display:none">` before Gmail sends.
2. **Open:** Lead's client fetches pixel → `GET /t/:id/pixel.gif` → log row in `opens` → return cached 1x1 gif.
3. **Render:** Extension polls/queries `GET /status?thread_ids=...` on Sent view render → injects checkmarks (grey = sent, green = opened, with tooltip showing time).

## 7. Edge Cases — Must Handle
1. **Gmail image proxy:** requests come from Google IP, not lead's IP. Don't rely on IP for anything; timestamp + ID is the source of truth.
2. **Apple Mail Privacy Protection:** Apple prefetches images → false "instant open." Detect via `User-Agent` containing `CloudImageProxy` → mark `verified=false`. Optional v2: link tracking as backup signal (Apple doesn't prefetch link clicks).
3. **Self-tracking:** Rep opening own Sent folder shouldn't count as open. Extension should not fire/should signal internal session so backend drops it — needs an internal-user allowlist by IP or a session token passed with the extension's own background checks.
4. **Caching:** Some clients cache images — set `Cache-Control: no-store` on pixel response or opens under-count on repeat views.

## 8. API Endpoints (v1)
- `POST /track` — body: sender, recipient, subject, thread_id → returns tracker_id
- `GET /t/:id/pixel.gif` — logs open, returns gif
- `GET /status?thread_ids=[]` — returns open status/timestamps for extension to render
- `POST /auth` — extension login (map to sender identity, business domain only)

## 9. Auth / Access Control
- Restrict to company Google Workspace domain (OAuth, domain-restricted).
- Each rep only sees their own sent threads' tracking data (unless admin role).

## 10. Phased Build

### DONE
- **Phase 1 (MVP):** Pixel server (FastAPI) + Neon Postgres + all API endpoints. Verified end-to-end via automated tests. Apple MPP detection working (`CloudImageProxy` → `verified=false`).
- **Phase 2:** Chrome extension (MV3) — service worker + content script. Intercepts Gmail Send button, calls `POST /track` via service worker, injects invisible pixel into email body. MutationObserver + polling for reliable DOM detection. Cloudflare Tunnel for HTTPS dev testing.
- **Phase 4:** Apple MPP detection + verified/unverified split — done in Phase 1.
- **Phase 3:** Sent view checkmarks — extension detects Sent/inbox view, queries `/status/sent` endpoint, injects checkmarks (✓ sent, ✅ opened, ⚠️ MPP prefetch) with tooltips showing open count and last opened time.
- **CC/BCC tracking:** Each recipient (To/CC/BCC) gets separate tracker, field labeled in DB.
- **Thread ID capture:** Extracts from URL hash, dialog data attributes, and draft IDs.

### IN PROGRESS
- **Deployment:** Cloudflare Tunnel (`*.trycloudflare.com`) for HTTPS during dev. Production deploy to Vultr + real domain pending.

### TODO
- **Phase 5:** Self-tracking suppression (don't count own opens). — TODO
- **Phase 6:** ~~Link click tracking + PDF open tracking.~~ — DONE
- **Phase 7:** ~~Instant open alerts (desktop push / Slack).~~ — DONE
- **Phase 8:** Team dashboard + analytics. — PARTIAL (weekly/monthly reports done, full dashboard TODO)
- **Phase 9:** Email sequences / cadences. — TODO
- **Production deploy:** Vultr + real domain + Let's Encrypt SSL (replace Cloudflare Tunnel).

## 11. Success Metrics
- Open detection parity with Mailsuite on test batch (send 20 emails, compare open logs).
- False-positive rate (Apple MPP flagged correctly) > 90%.
- Zero missed opens due to caching/self-tracking bugs.

## 12. Risks
- Google may flag/reject extension in Chrome Web Store if scraping Gmail DOM aggressively — keep permissions minimal (activeTab + your domain only), avoid broad `<all_urls>`.
- Gmail UI changes break DOM injection selectors — needs maintenance.
- Domain reputation: pixel server on subdomain, watch for spam-filter flags if volume high.

## 13. Recon vs Mailsuite — Feature Parity & Advantages

Mailsuite charges per-seat for basic open tracking. Recon is self-owned, per-team, and built to surpass Mailsuite in every dimension.

### Mailsuite Feature Parity (must have)
- [ ] **Double-tick checkmarks** in Sent view (grey ✓✓ = sent, green ✓✓ = opened) — WhatsApp-style
- [ ] **Hover tooltip** — per-recipient details: "keshav.mishra@vaidik.ai opened your email several times" + "First opened less than a minute after you sent it"
- [ ] **Per-recipient open tracking** — individual tracking for group/CC emails (To, CC, BCC)
- [ ] **Open count per email** — number of times opened per recipient
- [ ] **Reopen tracking per email** — tracks every open, not just first
- [x] **Apple MPP detection** — flag prefetches as unverified (CloudImageProxy in User-Agent)
- [x] **Lives entirely inside Gmail** — no separate dashboard needed
- [x] **No branding** — clean, white-label experience, no "Sent with Mailsuite" footer
- [x] **Instant open alerts** — desktop notification (extension, chrome.notifications via 1-min alarm) + Slack webhook (server, fire-and-forget on open/click)
- [x] **Link click tracking** — compose-body `<a href>` rewritten to tracked redirect URL, per-link click count + last-clicked in `/status/sent`
- [x] **PDF open tracking** — link-based only (`.pdf`-labeled tracked links); true attachment-open detection isn't feasible via Chrome extension (can't instrument attached file bytes) — reps must link to the PDF, not attach it

### Recon Premium Features (what Mailsuite charges extra for or doesn't offer)
- [ ] **Advanced alerts & custom notifications** — configurable rules (e.g., "alert if opened 3+ times", "alert if opened from new device")
- [ ] **Weekly & monthly reports** — automated summary of tracking activity per rep
- [ ] **Scheduled tracked emails** — compose now, send later, still tracked
- [ ] **Follow-up reminders** — auto-nudge if no open after N days
- [ ] **Reply detection** — mark thread as responded, stop tracking
- [ ] **Email sequences / cadences** — multi-touch drip with per-step tracking
- [ ] **Lead scoring from engagement** — opens + clicks + replies → priority score
- [ ] **A/B subject line testing** — send variants, track which opens better
- [ ] **Best-time-to-send** — aggregate data suggests optimal send windows
- [ ] **Team dashboard** — manager view: rep-wise open rates, response times
- [ ] **CRM/lead sync** — push events to internal tools (Vaicore, sheets)
- [ ] **Daily/weekly digest** — passive visibility without checking manually
- [ ] **Domain-wide deployment** — push extension to all reps via Workspace admin (Mailsuite charges per seat)
- [ ] **Zero per-seat cost** — own the stack, unlimited users

### Current Implementation Status
- [x] Phase 1: Pixel server + DB — DONE
- [x] Phase 2: Chrome extension send interception + pixel injection — DONE
- [x] Phase 4: Apple MPP detection — DONE
- [x] Phase 3: Sent view checkmarks — DONE (thread_id-based matching + per-recipient breakdown; list-row DOM selectors still best-effort, see Known Issues in HANDOFF.md)
- [ ] Phase 5: Self-tracking suppression
- [x] Phase 6: Link click tracking — DONE (link-based, see PDF caveat above)
- [x] Phase 7: Instant open alerts — DONE (Slack + desktop)
- [~] Phase 8: Analytics dashboard + reports — PARTIAL (weekly/monthly HTML reports done; full team dashboard still open)

## 14. Resolved Decisions
- **Pixel server:** FastAPI (Python) — consistent with Vaicore stack.
- **Hosting:** Vultr (production), Cloudflare Tunnel (dev).
- **Database:** Neon (Postgres) — serverless, no infra overhead.
- **Extension name:** Recon