# Recon

Email intelligence — open tracking for Gmail.

## Stack

- **API**: FastAPI (Python)
- **Database**: Neon (Postgres)
- **Hosting**: Vultr

## Quick Start

```bash
cd server
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # edit with your Neon URL
uvicorn app.main:app --reload
```

Server runs at `http://localhost:8000`. Docs at `/docs`.

## API

| Endpoint | Method | Purpose |
|---|---|---|
| `/track` | POST | Create tracker for an email |
| `/t/{id}/pixel.gif` | GET | Pixel endpoint — logs open, returns 1x1 gif |
| `/status?thread_ids=[]` | GET | Get open status for threads |
| `/health` | GET | Health check |

## Manual Test (Phase 1)

1. Create a tracker:
   ```bash
   curl -X POST http://localhost:8000/track \
     -H "Content-Type: application/json" \
     -d '{"sender_email":"you@company.com","recipient_email":"test@example.com","subject":"Test"}'
   ```

2. Simulate open (paste pixel URL in browser):
   ```
   http://localhost:8000/t/{tracker_id}/pixel.gif
   ```

3. Check status:
   ```bash
   curl "http://localhost:8000/status?thread_ids=1"
   ```

## Chrome Extension (Phase 2)

### Load in Chrome

1. Go to `chrome://extensions`
2. Enable **Developer mode** (top right)
3. Click **Load unpacked** → select the `extension/` folder
4. Click the Recon icon in toolbar → set:
   - **API Server URL**: `http://localhost:8000` (or your Vultr URL)
   - **Your Email**: your Gmail address

### How it works

- Content script runs on `mail.google.com`
- Watches for compose windows via MutationObserver
- Intercepts Send button click → calls `POST /track` → injects invisible pixel into email body
- Pixel fires when recipient opens the email → logged to Neon DB

### Test flow

1. Open Gmail → compose a new email
2. Add recipient, subject, body
3. Click Send
4. Check logs: `curl "http://localhost:8000/status?thread_ids=<thread_id>"`
5. Have the recipient open the email → pixel fires → open logged
