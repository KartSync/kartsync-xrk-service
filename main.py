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
