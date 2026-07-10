"""
KartSync XRK processing service
─────────────────────────────────────────────────────────────────────────
Stateless FastAPI service that accepts an AiM .xrk file upload, parses it
with libxrk, and returns JSON shaped exactly like the `telemStore` object
KartSync's CSV parser (parseAIM) already produces — so the existing
frontend telemetry pipeline (applyTelemToSession, renderTelemCharts,
renderHeatMaps etc.) works unchanged regardless of source format.

No database, no auth beyond a shared-secret header — auth/authorisation
for *linking* telemetry to a user's account happens in the frontend,
against Supabase. This service only proves what's IN the file.

Deploy: Railway (see README.md in this folder).
"""

import os
import math
import tempfile
from datetime import datetime, timezone

from fastapi import FastAPI, File, UploadFile, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from libxrk import aim_xrk, ChannelMetadata

app = FastAPI(title="KartSync XRK Service")

# Restrict to your deployed domains once live — using "*" during development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://kartsync.app", "https://kartsync.co.uk"],
    allow_methods=["POST"],
    allow_headers=["*"],
)

SHARED_SECRET = os.environ.get("KARTSYNC_SHARED_SECRET", "")

# ── Channel name matching ────────────────────────────────────────────────
# XRK channel names depend on how the logger was configured, so match
# fuzzily rather than expecting exact names — mirrors the approach already
# used in parseAIM() for CSV headers. Matching is done on a normalised form
# (lowercased, spaces/underscores stripped) since real AiM channel names mix
# both styles inconsistently (e.g. "GPS Speed" vs "GPS_LateralAcc").
RPM_HINTS = ["enginerpm", "rpm"]
SPEED_HINTS = ["gpsspeed", "speed"]
EGT_HINTS = ["exhaust", "egt"]
WATER_HINTS = ["watertemp", "coolant", "water"]
LAT_HINTS = ["gpslatitude", "latitude"]
LON_HINTS = ["gpslongitude", "longitude"]
# Confirmed against real hardware: AiM's GPS-derived G-force channels are
# named "GPS_LateralAcc" (lateral) and "GPS_InlineAcc" (longitudinal) — not
# "LonAcc" as originally assumed.
LATG_HINTS = ["gpslateralacc", "lateralacc", "acclat", "latacc"]
LONG_HINTS = ["gpsinlineacc", "inlineacc", "gpslonacc", "longitudinalacc", "acclon", "lonacc"]
GEAR_HINTS = ["calculatedgear", "gear"]

# Channels containing any of these are never a valid match for the hints
# above, regardless of what else they contain — e.g. "EGT Alarm_1" contains
# "egt" but is a 0/1 threshold flag, not a temperature reading.
GLOBAL_EXCLUDE = ["alarm"]

# Matches the default in KartSync's own getAlarms() (frontend) — user can
# customise this client-side, but this is the same starting point, so the
# backend's "danger" count is meaningful rather than a placeholder.
DEFAULT_EGT_DANGER_THRESHOLD = 620


def _norm(s):
    return s.lower().replace(" ", "").replace("_", "").replace("-", "")


def find_channel(channel_names, hints, exclude=()):
    normed = {name: _norm(name) for name in channel_names}
    all_exclude = list(exclude) + GLOBAL_EXCLUDE
    for hint in hints:
        hint_n = _norm(hint)
        for name, nname in normed.items():
            if hint_n in nname and not any(_norm(ex) in nname for ex in all_exclude):
                return name
    return None


def get_meta(metadata, *keys, default=""):
    """Case/format-insensitive metadata lookup — libxrk key naming can vary
    by device firmware version, so check a few likely variants."""
    if not metadata:
        return default
    lower_map = {str(k).strip().lower(): v for k, v in dict(metadata).items()}
    for key in keys:
        v = lower_map.get(key.lower())
        if v not in (None, ""):
            return v
    return default


def haversine_cum_dist(lats, lons):
    """Cumulative distance in metres, matching the frontend's GPS-based
    distance calculation (parseAIM uses the same formula)."""
    R = 6371000
    dist = [0.0]
    cum = 0.0
    for i in range(1, len(lats)):
        lat1, lon1 = lats[i - 1], lons[i - 1]
        lat2, lon2 = lats[i], lons[i]
        if lat1 and abs(lat1) > 1 and lat2 and abs(lat2) > 1:
            dlat = math.radians(lat2 - lat1)
            dlon = math.radians(lon2 - lon1)
            mlat = math.radians((lat1 + lat2) / 2)
            cum += math.sqrt((dlat * R) ** 2 + (dlon * R * math.cos(mlat)) ** 2)
        dist.append(round(cum, 1))
    return dist


def downsample(seq, target_points):
    if not seq:
        return []
    every = max(1, len(seq) // target_points)
    return [seq[i] for i in range(0, len(seq), every)]


def _channel_value_column(table):
    """PyArrow channel tables have 'timecodes' plus one value column — return
    the value column's name."""
    for name in table.column_names:
        if name.lower() != "timecodes":
            return name
    return None


def _channel_peak(log, channel_name):
    try:
        table = log.channels[channel_name]
        col = _channel_value_column(table)
        if col is None:
            return None
        return max(table.column(col).to_pylist())
    except Exception:
        return None


def resolve_egt_channel(log, channel_names):
    """Find the EGT channel, and the water/coolant temperature channel where
    identifiable. Prefers explicitly-named channels; falls back to
    disambiguating generic "Temperature N" channels by peak value — EGT on
    a 2-stroke kart runs 500-800C, coolant/water never approaches that, so
    whichever generic temperature channel peaks far higher is EGT and the
    other is water.

    Some loggers report water with an abbreviated name (e.g. "Water Temp")
    while leaving EGT fully generic ("Temperature 1") — in that case only
    ONE channel is left once the named water channel and the logger's own
    internal channel are excluded, so there's nothing to "compare" against.
    That single remaining channel is EGT by elimination; the peak sanity
    check still applies so a clearly-non-EGT reading isn't guessed anyway.

    Returns (egt_channel_name_or_None, water_channel_name_or_None, detection_note).
    """
    named_egt = find_channel(channel_names, EGT_HINTS)
    named_water = find_channel(channel_names, WATER_HINTS)
    if named_egt:
        return named_egt, named_water, "EGT matched by name" + (", water matched by name" if named_water else "")

    # Generic temperature channels: match on "temp" (not the full word
    # "temperature") so abbreviated names like "Water Temp" are caught here
    # too — otherwise they fall through neither the name-hint match nor this
    # pool and silently vanish. Exclude the logger's own internal channel and
    # whichever channel was already claimed as water by name above.
    temp_channels = [
        c for c in channel_names
        if "temp" in _norm(c)
        and "alarm" not in _norm(c)
        and "logger" not in _norm(c)
        and c != named_water
    ]

    if len(temp_channels) == 0:
        return None, named_water, "no named EGT channel and no unclaimed temperature channels to check"

    if len(temp_channels) == 1:
        # Only one unclaimed candidate left — nothing to compare against,
        # so no peak-vs-peak disambiguation is possible or needed. Still
        # apply the same sanity floor before trusting it as EGT.
        only_ch = temp_channels[0]
        peak = _channel_peak(log, only_ch)
        if peak is None:
            return None, named_water, f"only unclaimed temperature channel ({only_ch}) had unreadable values"
        if peak < 150:
            return None, named_water, f"only unclaimed temperature channel ({only_ch}) peaks at only {peak:.0f} — too low to confidently be EGT"
        note = f"only unclaimed temperature channel — {only_ch} peaks {peak:.0f}, assumed EGT" + (", water matched by name" if named_water else "")
        return only_ch, named_water, note

    # 2+ unclaimed candidates: fall back to peak-based comparison as before.
    peaks = {c: _channel_peak(log, c) for c in temp_channels}
    peaks = {c: v for c, v in peaks.items() if v is not None}
    if len(peaks) < 2:
        return None, named_water, "could not read values from generic temperature channels"
    egt_ch = max(peaks, key=peaks.get)
    water_ch = min(peaks, key=peaks.get)
    # Sanity check: EGT should be well above a plausible max water temp (~120C
    # even under extreme conditions). If the gap isn't there, don't guess.
    if peaks[egt_ch] < 150:
        return None, named_water, f"highest generic temperature channel ({egt_ch}) peaks at only {peaks[egt_ch]:.0f} — too low to confidently be EGT"
    note = f"inferred from value range — {egt_ch} peaks {peaks[egt_ch]:.0f} (EGT), {water_ch} peaks {peaks[water_ch]:.0f} (water)"
    # Don't let a generically-inferred water channel override one already
    # matched by name above.
    return egt_ch, (named_water or water_ch), note

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/parse")
async def parse_xrk(
    file: UploadFile = File(...),
    x_kartsync_secret: str = Header(default=""),
):
    if SHARED_SECRET and x_kartsync_secret != SHARED_SECRET:
        raise HTTPException(status_code=401, detail="Invalid or missing shared secret")

    if not file.filename.lower().endswith((".xrk", ".xrz")):
        raise HTTPException(status_code=400, detail="Only .xrk / .xrz files are supported")

    suffix = ".xrk" if file.filename.lower().endswith(".xrk") else ".xrz"
    contents = await file.read()

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(contents)
        tmp_path = tmp.name

    try:
        log = aim_xrk(tmp_path)
        print("XRK log object attributes:", [a for a in dir(log) if not a.startswith('_')])
        print("log.metadata =", log.metadata)
        print("log.metadata type =", type(log.metadata))
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Could not parse XRK file: {e}")
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    metadata = getattr(log, "metadata", {}) or {}
    logger_id = str(get_meta(metadata, "logger id", "loggerid", "logger serial")).strip()
    logger_model = str(get_meta(metadata, "logger model", "loggermodel")).strip()
    device_name = str(get_meta(metadata, "device name", "devicename")).strip()

    channel_names = list(log.channels.keys())
    rpm_ch = find_channel(channel_names, RPM_HINTS)
    spd_ch = find_channel(channel_names, SPEED_HINTS, exclude=["accuracy", "acc"])
    egt_ch, water_ch, egt_detection_note = resolve_egt_channel(log, channel_names)
    lat_ch = find_channel(channel_names, LAT_HINTS)
    lon_ch = find_channel(channel_names, LON_HINTS)
    latg_ch = find_channel(channel_names, LATG_HINTS)
    long_ch = find_channel(channel_names, LONG_HINTS)
    gear_ch = find_channel(channel_names, GEAR_HINTS)

    has_egt = egt_ch is not None
    has_water = water_ch is not None
    has_gps = lat_ch is not None and lon_ch is not None
    has_imu = latg_ch is not None and long_ch is not None
    has_gear = gear_ch is not None

    if spd_ch is None:
        raise HTTPException(
            status_code=422,
            detail="No GPS speed channel found in this file — cannot detect laps.",
        )

    # Speed unit normalisation — KartSync stores/displays speed in mph
    # throughout. Confirmed via real hardware: AiM's raw "GPS Speed" channel
    # is reported in m/s (not km/h) — handle that plus km/h and mph so this
    # is robust across different logger configs.
    spd_meta = ChannelMetadata.from_channel_table(log.channels[spd_ch])
    spd_units = (spd_meta.units or "").strip().lower()
    MS_TO_MPH = 2.236936
    KMH_TO_MPH = 0.621371

    def to_mph(v):
        if v is None:
            return 0.0
        if spd_units in ("m/s", "mps", "meters/sec", "metres/sec") or "m/s" in spd_units:
            return v * MS_TO_MPH
        if "km" in spd_units:
            return v * KMH_TO_MPH
        return v  # already mph, or units unknown — assume no conversion needed

    laps_table = log.laps
    lap_nums = laps_table.column("num").to_pylist()
    lap_starts = laps_table.column("start_time").to_pylist()
    lap_ends = laps_table.column("end_time").to_pylist()

    flying_laps = []
    for lap_num, start_ms, end_ms in zip(lap_nums, lap_starts, lap_ends):
        try:
            lap_log = log.filter_by_lap(lap_num)
            aligned = lap_log.resample_to_channel(spd_ch)
            df = aligned.get_channels_as_table().to_pandas()
        except Exception:
            continue
        if df.empty:
            continue

        lap_time = round((end_ms - start_ms) / 1000.0, 3)
        if lap_time < 33 or lap_time > 120:
            continue  # outside plausible KZ2 lap range — mirrors parseAIM

        spds_mph = [to_mph(v) for v in df.get(spd_ch, [])]
        if not spds_mph or min(spds_mph) < 15:
            continue  # in/out/formation lap — never drops below 15mph on a flyer

        rpms = df.get(rpm_ch, []).tolist() if rpm_ch else []
        peak_rpm = round(max(rpms)) if rpms else 0
        peak_spd = round(max(spds_mph), 1)

        egts = df.get(egt_ch, []).tolist() if has_egt else []
        peak_egt = round(max(egts), 1) if egts else None
        top_egts = [e for r, e in zip(rpms, egts) if r > 13000] if (rpms and egts) else []
        mid_egts = [e for r, e in zip(rpms, egts) if 10000 < r < 12500] if (rpms and egts) else []
        mean_egt_top = round(sum(top_egts) / len(top_egts), 1) if top_egts else None
        mean_egt_mid = round(sum(mid_egts) / len(mid_egts), 1) if mid_egts else None

        waters = df.get(water_ch, []).tolist() if has_water else []
        peak_water = round(max(waters), 1) if waters else None

        lats = df.get(lat_ch, []).tolist() if has_gps else []
        lons = df.get(lon_ch, []).tolist() if has_gps else []
        dist_arr = haversine_cum_dist(lats, lons) if has_gps else []

        latgs = df.get(latg_ch, []).tolist() if has_imu else []
        longs = df.get(long_ch, []).tolist() if has_imu else []
        peak_lat_g = round(max(abs(v) for v in latgs), 3) if latgs else None
        peak_lon_g_accel = round(max(longs), 3) if longs else None
        peak_lon_g_brake = round(min(longs), 3) if longs else None

        gears = df.get(gear_ch, []).tolist() if has_gear else []
        peak_gear = round(max(gears)) if gears else 0

        n = len(df)
        every = max(1, n // 90)
        trace_every = max(1, n // 400)
        idxs = list(range(0, n, every))

        time_col = [(t - start_ms) / 1000.0 for t in df.get("timecodes", range(n))]
        trace = {
            "time": [round(time_col[i], 1) for i in idxs],
            "dist": [dist_arr[i] if i < len(dist_arr) else 0 for i in idxs],
            "rpm": [round(rpms[i]) if rpms else 0 for i in idxs],
            "spd": [round(spds_mph[i], 1) for i in idxs],
            "gear": [round(gears[i]) if gears else 0 for i in idxs] if has_gear else [],
        }
        if has_egt:
            trace["egt"] = [round(egts[i], 1) if egts else 0 for i in idxs]
        if has_water:
            trace["water"] = [round(waters[i], 1) if waters else 0 for i in idxs]
        if has_gps:
            gps_idxs = list(range(0, n, trace_every))
            trace["lat"] = [round(lats[i], 7) for i in gps_idxs if abs(lats[i]) > 0.01]
            trace["lon"] = [round(lons[i], 7) for i in gps_idxs if abs(lons[i]) > 0.01]
        if has_imu:
            trace["latG"] = [round(latgs[i], 3) if latgs else 0 for i in idxs]
            trace["lonG"] = [round(longs[i], 3) if longs else 0 for i in idxs]

        flying_laps.append({
            "lap": int(lap_num),
            "lap_time": lap_time,
            "peak_rpm": peak_rpm,
            "peak_spd": peak_spd,
            "peak_egt": peak_egt,
            "mean_egt_top": mean_egt_top,
            "mean_egt_mid": mean_egt_mid,
            "peak_water": peak_water,
            "peak_lat_g": peak_lat_g,
            "peak_lon_g_accel": peak_lon_g_accel,
            "peak_lon_g_brake": peak_lon_g_brake,
            "peak_gear": peak_gear,
            "trace": trace,
        })

    if not flying_laps:
        raise HTTPException(
            status_code=422,
            detail="No flying laps found (all laps outside 33-120s or below 15mph).",
        )

    max_egt = max((l["peak_egt"] for l in flying_laps if l["peak_egt"]), default=0)
    max_water = max((l["peak_water"] for l in flying_laps if l["peak_water"]), default=0)
    peak_rpm_all = max((l["peak_rpm"] for l in flying_laps), default=0)
    peak_spd_all = max((l["peak_spd"] for l in flying_laps), default=0)
    best_lap = min(flying_laps, key=lambda l: l["lap_time"])

    danger_count = sum(
        1
        for l in flying_laps
        for e in l["trace"].get("egt", [])
        if e >= DEFAULT_EGT_DANGER_THRESHOLD
    ) if has_egt else 0


    # Log date/time from metadata, formatted to match parseAIM's csvDate/csvTime
    log_dt_raw = get_meta(metadata, "log date/time", "log date", "date/time", default="")
    csv_date, csv_time = "", ""
    if log_dt_raw:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%m/%d/%Y %H:%M:%S"):
            try:
                dt = datetime.strptime(str(log_dt_raw), fmt)
                csv_date = dt.strftime("%Y-%m-%d")
                csv_time = dt.strftime("%H:%M")
                break
            except ValueError:
                continue

    duration_sec = round((max(lap_ends) - min(lap_starts)) / 1000.0, 1) if lap_starts else 0

    telem_store = {
        "fname": file.filename,
        "hasEGT": has_egt,
        "hasWater": has_water,
        "hasGPS": has_gps,
        "hasIMU": has_imu,
        "hasGear": has_gear,
        "flyingLaps": flying_laps,
        "bestLapTime": best_lap["lap_time"],
        "peakEGT": max_egt,
        "peakWater": max_water,
        "peakRPM": peak_rpm_all,
        "peakSpd": peak_spd_all,
        "danger": danger_count,  # count of EGT samples >= 620C (KartSync's
                                  # default alarm threshold), across all
                                  # flying laps' downsampled traces
        "csvDate": csv_date,
        "csvTime": csv_time,
        "durationSec": duration_sec,
    }

    return {
        "logger_id": logger_id,
        "logger_model": logger_model,
        "device_name": device_name,
        "telemStore": telem_store,
        "flyingLaps": flying_laps,
        "hasEGT": has_egt,
        "hasWater": has_water,
        "maxEGT": max_egt,
        "maxWater": max_water,
        "egt_channel_used": egt_ch,
        "water_channel_used": water_ch,
       "egt_detection_note": egt_detection_note,
        "debug_all_channel_names": list(channel_names),
    }
