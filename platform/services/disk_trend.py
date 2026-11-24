"""When will the data disk be full? (RFC-0085 A4 §18.7) -- pure.

Computed on the NODE from its own metrics tiers (RFC-0051: `5m` for a day,
`30m` for a week), so it warns also when the hub is blind -- the lesson of
2026-11-20..24. Nothing here reads a file or a clock.

Two estimates of the time until 100 %:

  * **linear**: the least-squares slope of the last 24 hours;
  * **exponential**: only when the growth really accelerates -- the mean
    rate of the last 6 hours at least that of the last 24 hours, and that
    at least ACCEL times the rate of the last 7 days, the day's growth at
    least EXP_MIN_RATE. The rate is then taken to grow like e^(k t), k from
    the 24 h and 7 d windows (their midpoints lie 72 hours apart; at most
    a doubling a day), and integrated from the day's rate until the free
    space is used up. Measured against oaap-test's own week (§18.7): k from
    the 6 h and 24 h windows predicted "full in 27 hours" at 0.15 points a
    day, and flickered.

The EARLIER credible time counts. Noise guards: a growth below MIN_RATE
per day is no growth; a window needs MIN_SPAN of data and MIN_POINTS
points; the matter opens below WARN_H / CRIT_H hours and closes only above
WARN_OFF_H / CRIT_OFF_H (hysteresis). The fixed threshold of `disk.full`
stays beside it.

Measured background: the weekly reports of 2026-11-23 showed oaap-demo
+101.1 GB and oaap-test +95.4 GB in a week (oaap-test peaked at 96 %:
Docker images and build cache).
"""
import math

WARN_H = 48.0
CRIT_H = 12.0
WARN_OFF_H = 60.0          # an open warning closes only above this
CRIT_OFF_H = 18.0
MIN_RATE = 0.25            # percent points per day; below that: no growth
MIN_SPAN = 6 * 3600        # a window needs this much data
MIN_POINTS = 12
ACCEL = 1.5                # the day's rate this much above the week's
EXP_MIN_RATE = 1.0         # percent points per day before a curve is believed
K_MAX = math.log(2) / 86400  # the rate doubles at most once a day


def points(tiers):
    """[(t, percent)] oldest first from the tiers' entries, the finest tier
    winning where tiers overlap. `tiers`: [entries of 30m, entries of 5m]
    (coarse to fine), each entry {"t", "disk": value or [n, mean, min, max]}."""
    out = {}
    for entries in tiers:
        rows = []
        for e in entries or []:
            v = e.get("disk")
            if isinstance(v, list) and len(v) == 4:
                v = v[1]
            if isinstance(v, (int, float)) and not isinstance(v, bool) and isinstance(e.get("t"), int):
                rows.append((e["t"], float(v)))
        if rows:
            first = min(t for t, _ in rows)
            # a finer tier replaces the coarser one from its first point on
            out = {t: v for t, v in out.items() if t < first}
            out.update(dict(rows))
    return sorted(out.items())


def _window(pts, now, span):
    w = [(t, v) for t, v in pts if now - span <= t <= now]
    if len(w) < MIN_POINTS or w[-1][0] - w[0][0] < min(span, MIN_SPAN) * 0.9:
        return None
    return w


def slope_per_hour(w):
    """Least-squares slope in percent points per hour."""
    n = len(w)
    mt = sum(t for t, _ in w) / n
    mv = sum(v for _, v in w) / n
    den = sum((t - mt) ** 2 for t, _ in w)
    if den <= 0:
        return 0.0
    return sum((t - mt) * (v - mv) for t, v in w) / den * 3600


def mean_rate(w):
    """(last - first) / span, percent points per hour."""
    span = w[-1][0] - w[0][0]
    return (w[-1][1] - w[0][1]) / span * 3600 if span > 0 else 0.0


def forecast(pts, now, current=None):
    """{"pct", "linear_h", "exp_h", "hours", "how", "rate_day"} or None when
    there is too little data. `hours` is None when the disk does not fill."""
    if not pts:
        return None
    pct = current if current is not None else pts[-1][1]
    free = max(0.0, 100.0 - pct)
    w24 = _window(pts, now, 24 * 3600)
    if w24 is None:
        return None
    r_lin = slope_per_hour(w24)
    lin_h = free / r_lin if r_lin * 24 >= MIN_RATE else None
    exp_h = None
    w6, w7 = _window(pts, now, 6 * 3600), _window(pts, now, 7 * 86400)
    if w6 and w7 and w7[-1][0] - w7[0][0] >= 3 * 86400:
        r6, r24, r7 = mean_rate(w6), mean_rate(w24), mean_rate(w7)
        if (r7 * 24 >= MIN_RATE and r24 * 24 >= EXP_MIN_RATE
                and r24 >= ACCEL * r7 and r6 >= r24):
            k = min(K_MAX, math.log(r24 / r7) / (72 * 3600))  # midpoints 12 h / 84 h ago
            if k > 0:
                exp_h = math.log(1 + free * k / (r24 / 3600)) / k / 3600
    cands = [(h, how) for h, how in ((lin_h, "linear"), (exp_h, "exponentiell")) if h is not None]
    hours, how = min(cands) if cands else (None, "")
    return {"pct": round(pct, 2), "linear_h": lin_h, "exp_h": exp_h, "hours": hours,
            "how": how, "rate_day": round(r_lin * 24, 2)}


def words(hours):
    if hours < 2:
        return f"{int(hours * 60)} Minuten"
    if hours < 72:
        return f"{hours:.0f} Stunden"
    return f"{hours / 24:.0f} Tagen"


def conditions(fc, open_keys=(), total_bytes=None):
    """{key: condition} for `disk.trend` (warning) and `disk.soon`
    (critical). `open_keys`: the matters open now -- hysteresis."""
    if not fc or fc.get("hours") is None:
        return {}
    h = fc["hours"]
    out = {}
    gb = ""
    if total_bytes:
        gb = f" (etwa {fc['rate_day'] * total_bytes / 100 / 1e9:.1f} GB am Tag)"
    text = (f"Die Datenplatte ist zu {fc['pct']:.1f} % belegt und wächst um "
            f"{fc['rate_day']:.1f} Prozentpunkte am Tag{gb}. So wird sie in etwa "
            f"{words(h)} voll sein (Schätzung {fc['how']}"
            + (f"; linear: {words(fc['linear_h'])}" if fc.get("exp_h") is not None
               and fc.get("linear_h") is not None else "")
            + ").\nHäufig sind es Abbilder und Bau-Zwischenstände von Docker "
              "('docker system df' zeigt sie) oder Sicherungen.")
    if h < WARN_H or ("disk.trend" in open_keys and h < WARN_OFF_H):
        out["disk.trend"] = {"type": "disk.trend", "kurz": f"voll in {words(h)}", "text": text}
    if h < CRIT_H or ("disk.soon" in open_keys and h < CRIT_OFF_H):
        out["disk.soon"] = {"type": "disk.soon", "kurz": f"voll in {words(h)}", "text": text}
    return out
