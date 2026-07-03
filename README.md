# KartSync XRK Service

Stateless FastAPI service that parses AiM `.xrk`/`.xrz` files using
[libxrk](https://github.com/m3rlin45/libxrk) and returns JSON shaped exactly
like the `telemStore` object KartSync's CSV parser already produces, so the
existing frontend telemetry pipeline works unchanged regardless of source
format.

## Deploy to Railway

1. Push this folder to its own GitHub repo (e.g. `KartSync/xrk-service`), or
   a subfolder of an existing repo with Railway's root directory setting
   pointed at it.
2. In Railway: **New Project → Deploy from GitHub repo** → select the repo.
3. Railway auto-detects Python and installs `requirements.txt`. The
   `Procfile` tells it how to start the service — no extra config needed.
4. Set an environment variable **`KARTSYNC_SHARED_SECRET`** to a random
   string (e.g. generate one with `openssl rand -hex 32`). This is checked
   against the `X-KartSync-Secret` header on every request.
5. Once deployed, Railway gives you a public URL like
   `https://kartsync-xrk-production.up.railway.app`.
6. In `KartSync_v13.html`, set:
   ```js
   const XRK_BACKEND_URL = 'https://kartsync-xrk-production.up.railway.app';
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
and for confirming the service is reachable before wiring up the frontend.

## Known things to verify against a real file

This was built from libxrk's documented API without a real `.xrk` file to
test against, so a few things need confirming once you run an actual file
from JP's MyChron through it:

- **Metadata key names** (`Logger ID`, `Log Date/Time` etc.) — `get_meta()`
  does a case-insensitive lookup across a few likely variants, but if the
  actual field name differs, `logger_id` will come back empty. If that
  happens, print `log.metadata` from a real file and I'll adjust the key
  list.
- **Channel names** for RPM/speed/EGT/GPS/gear — matched fuzzily via
  `find_channel()`. If your MyChron's channel names don't match any hint
  (e.g. `console.log(list(log.channels.keys()))` reveals something
  unexpected), the relevant `hasX` flag will come back `false` even though
  the data exists. Easy to extend the hint lists once we see real names.
- **Speed units** — converts km/h → mph based on the channel's declared
  units metadata. Worth double-checking a known lap time / speed reading
  against Race Studio to confirm the conversion direction is right.
- **`danger` field** is hardcoded to `0` — the EGT alarm thresholds are a
  user preference that lives client-side (`getAlarms()` in the frontend),
  not something the backend has access to. This field is cosmetic (used in
  the initial parse-time UI note) and isn't relied on anywhere else, so it
  was left as a known simplification for now.

None of this affects the *security-critical* part (device ID verification
and Pro-gating are enforced entirely in the frontend against Supabase,
independent of anything this service returns) — worst case here is
telemetry displaying with a missing channel, not a security gap.
