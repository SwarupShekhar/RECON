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
| `/track` | POST | Create tracker (sender, recipient, subject, thread_id, recipient_field) |
| `/t/:id/pixel.gif` | GET | Log open event, return 1x1 gif |
| `/status?thread_ids=[]` | GET | Open status by thread IDs |
| `/status/sent?sender_email=X` | GET | All tracked emails for a sender (for checkmarks) |
| `/debug/emails` | GET | All emails with open counts (debug) |
| `/health` | GET | Health check |
| `/` | GET | Redirects to /docs |

## DB Schema
**emails**: id(uuid), sender_email, recipient_email, recipient_field(to/cc/bcc), subject, thread_id, created_at
**opens**: id(int), email_id(fk), opened_at, user_agent, ip, verified(bool)

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
4. **thread_id is null for new emails** — Gmail doesn't expose thread_id until the email is sent and viewed. Works for replies.
5. **Checkmarks DOM selectors** — Gmail changes DOM frequently. Current selectors: `tr.zA, div[role="listitem"], tr[jscontroller]`. May need updates.

## Next Phases (PRD)
- **Phase 5**: Self-tracking suppression (don't count own opens)
- **Phase 6**: Link click tracking + PDF open tracking
- **Phase 7**: Instant open alerts (desktop push / Slack)
- **Phase 8**: Team dashboard + analytics
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
