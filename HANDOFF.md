# Recon — Project Handoff (July 4, 2026)

## What Recon Is
Email open tracking tool — Mailsuite replacement. Chrome extension + FastAPI backend + Neon Postgres. Named "Recon".

## Tech Stack
| Component | Stack | Status |
|---|---|---|
| Pixel Server | FastAPI (Python) | ✅ Done |
| Database | Neon (Postgres) | ✅ Done |
| Chrome Extension | MV3 (JS) | ✅ Done (core) |
| Dev tunnel | Cloudflare Tunnel | ✅ Working |

## Project Structure
```
~/mailtrack/
├── server/
│   ├── app/
│   │   ├── main.py          # FastAPI app, routes, lifespan
│   │   ├── database.py      # AsyncPG + Neon connection
│   │   ├── models.py        # SQLAlchemy: emails, opens tables
│   │   ├── schemas.py       # Pydantic request/response
│   │   └── routes/
│   │       ├── track.py     # POST /track → create tracker
│   │       ├── pixel.py     # GET /t/:id/pixel.gif → log open
│   │       └── status.py    # GET /status → open status by thread_ids
│   ├── .env                 # DATABASE_URL=postgresql+asyncpg://...
│   ├── .venv/               # Python venv
│   ├── cert.pem / key.pem   # Self-signed SSL (for dev)
│   └── requirements.txt
├── extension/
│   ├── manifest.json        # MV3, name "Recon"
│   ├── content.js           # Gmail DOM injection, checkmarks
│   ├── service-worker.js    # Handles API calls (bypasses CORS)
│   ├── popup.html/js        # Config UI
│   └── icons/
├── PRD_Recon.md             # Full PRD with feature checklist
├── README.md
└── PRD_Email_Tracking_Tool.md (renamed to PRD_Recon.md)
```

## API Endpoints
| Endpoint | Method | Purpose |
|---|---|---|
| `/track` | POST | Create tracker (sender, recipient, subject, thread_id, recipient_field, optional `links[]`) → returns tracker_id + tracked link URLs |
| `/t/:id/pixel.gif` | GET | Log open event, return 1x1 gif. Fires Slack alert (fire-and-forget) if configured. |
| `/l/:id` | GET | Log link click, 302 redirect to original URL. Fires Slack alert (fire-and-forget) if configured. |
| `/status?thread_ids=[]` | GET | Open status by thread IDs. Auth-gated if `API_KEY` set. |
| `/status/sent?sender_email=X` | GET | All tracked emails for a sender incl. per-email `links[]` (clicks, last_clicked_at). Auth-gated if `API_KEY` set. |
| `/reports/weekly?sender_email=` | GET | HTML report, last 7 days. Auth-gated if `API_KEY` set. |
| `/reports/monthly?sender_email=` | GET | HTML report, last 30 days. Auth-gated if `API_KEY` set. |
| `/mute` | POST | body `{thread_id, seconds=30}` — suppress opens/clicks on a thread for N seconds (self-tracking). Never gated (called by extension pre-auth, same trust model as `/track`). |
| `/debug/emails` | GET | All emails with open counts (debug). Auth-gated if `API_KEY` set. |
| `/health` | GET | Health check |
| `/` | GET | Redirects to /docs |

Auth gate is a no-op unless `API_KEY` env var is set (backward compatible default). When set, gated endpoints require header `X-API-Key: <value>`. `/track`, `/t/:id/pixel.gif`, `/l/:id` are never gated (hit by extension pre-auth or by lead's own client/browser).

## DB Schema
**emails**: id(uuid), sender_email, recipient_email, recipient_field(to/cc/bcc), subject, thread_id, created_at
**opens**: id(int), email_id(fk), opened_at, user_agent, ip, verified(bool), internal(bool)
**links**: id(uuid), email_id(fk), original_url, link_type(link/pdf), created_at
**link_clicks**: id(int), link_id(fk), clicked_at, user_agent, ip, verified(bool), internal(bool)
**pixel_mutes**: thread_id(pk), muted_until — short-lived self-tracking suppression window

## Self-Tracking Suppression (Phase 5)
- Problem: a rep opening their own Sent copy of a tracked email fires the same pixel fetch(es) as a real recipient open — inflates counts, triggers false alerts. Worse: a single compose body embeds one pixel per recipient, so opening your own copy of a group send can fire ALL of those recipients' pixels at once.
- Fix: extension calls `POST /mute {thread_id, seconds:30}` right before it expects Gmail to render the sender's own copy of a tracked thread — either (a) on click into a matched Sent-list row (before navigation, so the mute lands ahead of the pixel fetch), or (b) as a fallback the moment `injectThreadCheckmark` detects an already-open tracked thread (covers direct nav/refresh, but can race the pixel fetch that already happened on render — known imperfection).
- Server: `/t/:id/pixel.gif` and `/l/:id` check `PixelMute` by the email's `thread_id` (not tracker_id, since mute must cover every recipient's pixel sharing that thread) and stamp `internal=true` on the `Open`/`LinkClick` row instead of dropping it — kept for audit, excluded from `total_opens`/`verified_opens`/`last_opened_at`/link click counts in `/status` and `/status/sent`, and skipped for Slack/desktop alerts.
- Known gap: brand-new sends have `thread_id=null` until Gmail assigns one (see Known Issues #4) — self-opens on a not-yet-threaded send can't be muted.

## Alerts & Reports
- Set `SLACK_WEBHOOK_URL` in `.env` to get instant Slack pings on open/link-click. Unset = logs to console only, no crash.
- `ALERT_ON_OPEN=true` (default) toggles the open-alert.
- APScheduler posts a weekly (Mon 9am) and monthly (1st, 9am) summary per sender to Slack if webhook configured.
- Desktop notifications (Chrome `notifications` API) fire client-side via a 1-minute alarm in the extension regardless of Slack config — toggle in extension popup ("Enable desktop alerts").

## Link & "PDF" Click Tracking
- Extension rewrites `<a href>` tags typed/pasted into the compose body to a tracked redirect URL (`/l/:id`) before send. Links ending in `.pdf` or with "pdf" in the link text are labeled `type: "pdf"` for reporting — same mechanism as regular links.
- **Cannot track real file attachments** — Chrome extensions can't intercept or instrument an actual attached file's bytes. Reps must paste a link to the PDF (e.g. Drive) instead of attaching directly, to get open/click tracking on it. Same limitation applies to Mailsuite/Yesware-style tools.
- Link click attribution is per-email, not per-recipient, when a send has multiple recipients — the compose body (and its links) is identical across all recipients in one Gmail send, so a click can only be attributed to "someone in this send," not a specific recipient.

## What's Working (Phases 1-4)
1. **Phase 1**: Pixel server + Neon DB. All endpoints verified. Apple MPP detection (CloudImageProxy → verified=false).
2. **Phase 2**: Chrome extension. Intercepts Send, calls API via service worker, injects pixel. MutationObserver + polling.
3. **Phase 3**: Sent view checkmarks. Double-tick (✓✓) style like Mailsuite. Green = opened, grey = sent. Hover popover with open details.
4. **Phase 4**: Apple MPP detection done in Phase 1.
5. **CC/BCC tracking**: Each recipient gets separate tracker with field label.
6. **Thread ID extraction**: From URL hash, dialog data attributes, draft IDs.

## How to Start
```bash
# Server
cd ~/mailtrack/server && source .venv/bin/activate
uvicorn app.main:app --host 127.0.0.1 --port 8000

# Tunnel (for HTTPS)
cloudflared tunnel --url http://127.0.0.1:8000 --protocol http2
```

## Extension Config
- Server URL: `https://<tunnel>.trycloudflare.com` (or `http://localhost:8000` for local)
- Sender email: user's Gmail address

## Known Issues
1. **Server dies when bash tool times out** — needs proper daemonization for production. Use `setsid` on Linux, or run in separate terminal.
2. **Cloudflare quick tunnels are temporary** — URL changes on restart. Production needs Vultr + real domain.
3. **Neon connection pool goes stale** — server needs restart if idle too long. Add pool recycling in production.
4. **thread_id is null for new emails** — Gmail doesn't expose thread_id until the email is sent and viewed. Works for replies. Extension falls back to recipient+subject fuzzy match when thread_id is unavailable.
5. **Checkmarks DOM selectors** — Gmail changes DOM frequently. Current selectors: `tr.zA, div[role="listitem"], tr[jscontroller]`. May need updates. List-row thread-id extraction (`getThreadIdFromRow`) is best-effort/unverified against live Gmail DOM — degrades to fuzzy fallback if it can't find one.
6. **Schedule-send button detection unverified live** — selector/text-matching for Gmail's "Schedule send" confirm dialog is best-effort, needs a real Gmail smoke test.
7. **Slack webhook / scheduler cron untested live** — verified via code review + console-log no-op path only, no `SLACK_WEBHOOK_URL` configured in dev. Configure it and trigger a pixel/link hit to confirm.
8. ~~No self-tracking suppression~~ — DONE, see "Self-Tracking Suppression" section above. Live-Gmail click-to-mute timing (row click vs. Gmail's own pixel fetch) still unverified against a real Gmail tab.

## Next Phases (PRD)
- ~~**Phase 5**: Self-tracking suppression~~ — DONE
- **Phase 6**: ~~Link click tracking + PDF open tracking~~ — DONE (link-based; true attachment tracking not feasible, see Link & "PDF" Click Tracking above)
- **Phase 7**: ~~Instant open alerts~~ — DONE (Slack webhook + desktop notifications)
- **Phase 8**: Team dashboard + analytics — partially done (weekly/monthly HTML reports); full dashboard still open
- **Phase 9**: Email sequences / cadences
- **Production**: Vultr + real domain + Let's Encrypt SSL

## Mailsuite Parity Features (from PRD)
Already done: ✓✓ checkmarks, hover tooltips, per-recipient tracking, open count, Apple MPP detection, no branding, reopen tracking, lives in Gmail.

Still TODO: Instant alerts, link/PDF click tracking, scheduled emails, follow-up reminders, reply detection, sequences, A/B testing, lead scoring, team dashboard, CRM sync, digests, Workspace admin deployment.

## Key Decisions
- FastAPI (Python) — matches Vaicore stack
- Vultr (production) — cheap, isolated
- Neon (Postgres) — serverless, no infra
- Cloudflare Tunnel (dev) — HTTPS without cert hassle
- Extension name: Recon

## Files to Touch for Each Phase
- **Content script changes**: `extension/content.js`
- **New API endpoints**: `server/app/routes/` + `server/app/main.py`
- **Schema changes**: `server/app/models.py` + `server/app/schemas.py`
- **Extension config**: `extension/manifest.json`
