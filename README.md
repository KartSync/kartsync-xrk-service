# KartSync XRK Service

Stateless FastAPI service that parses AiM `.xrk`/`.xrz` files using
[libxrk](https://github.com/m3rlin45/libxrk) and returns JSON shaped exactly
like the `telemStore` object KartSync's CSV parser already produces, so the
existing frontend telemetry pipeline works unchanged regardless of source
format.

Validated end-to-end against a real MyChron file from Shenington (July
2026) — lap times, speeds, RPM, EGT, and G-forces all confirmed correct.

## Deploy to Railway

1. Push this folder to its own GitHub repo (e.g. `KartSync/xrk-service`), or
   a subfolder of an existing repo with Railway's root directory setting
   pointed at it.
2. In Railway: **New Project → Deploy from GitHub repo** → select the repo.
3. Railway auto-detects Python and installs `requirements.txt`. The
   `Procfile` tells it how to start the service — no extra config needed.
4. Set an environment variable **`KARTSYNC_SHARED_SECRET`** to a random
   string. This is checked against the `X-KartSync-Secret` header on every
   request.
5. Once deployed, Railway gives you a public URL, e.g.
   `https://web-production-xxxx.up.railway.app`.
6. In `KartSync_v13.html`, set:
   ```js
   const XRK_BACKEND_URL = 'https://your-service.up.railway.app';
   const XRK_SHARED_SECRET = '<the same random string from step 4>';
   ```
   Re-deploy the app (Cloudflare Pages) after this change.

## Test it directly

```bash
curl -X POST https://your-service.up.railway.app/parse \
  -H "X-KartSync-Secret: <your secret>" \
  -F "file=@/path/to/session.xrk"
```

Should return JSON with `logger_id`, `telemStore`, `flyingLaps`, etc.

`GET /health` returns `{"status":"ok"}` — useful for a Railway health check
and for confirming the service is reachable.

## Security notes

- **CORS** is restricted to `kartsync.app` and `kartsync.co.uk` — only
  those origins can call this service from a browser.
- **Device ID verification and Pro-account gating happen entirely in the
  frontend**, against Supabase — this service only reports what's in the
  file. It doesn't authenticate users or know about accounts.
- The shared secret prevents arbitrary internet traffic from hitting the
  parsing endpoint, but isn't a substitute for the frontend's own checks.

## Channel detection notes

Channel names vary by logger configuration, so RPM/speed/EGT/GPS/gear
channels are matched fuzzily (normalised, case-insensitive) rather than by
exact name — see `find_channel()` and `resolve_egt_channel()`. If a future
file from a differently-configured logger doesn't populate a channel that
should exist, the fix is almost always extending the relevant `_HINTS` list
in `main.py` once we see the actual channel name. There's no `/debug` route
anymore (removed once real-file testing was done) — if we need to inspect
a new device's raw channel names again later, it's easy to add one back
temporarily.

`danger` in the response counts EGT samples at or above 620°C (KartSync's
own default alarm threshold from `getAlarms()`) across all flying laps —
this is a fixed default, not the individual user's customised threshold,
since that preference lives client-side only.
