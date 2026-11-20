"""Die Gesundheit EINES Mandanten (Jörg, 20.11.).

Wer im Kontext eines Mandanten auf „Gesundheit“ geht -- dessen tenant_admin,
oder der server_admin an der Adresse des Mandanten --, sieht nur, was diesen
Mandanten angeht: seine Instanzen, ihren Zustand, ihre Datengröße, ihren
Verkehr der letzten Stunde, die Datengröße des Mandanten im Verlauf und seine
letzte Sicherung. Keine fremde Instanz, keine knotenweite Kurve (RFC-0051:
Knotenwerte sind keine Mandantendaten).

Diese Datei misst nichts und kennt kein Flask. Sie bekommt, was der Host
ohnehin daneben schreibt -- Registry, Zustandsansicht, Größenansicht,
Verkehrsschnappschuss, das Tagesbuch des Berichts --, und rechnet daraus die
Ansicht. Was heute niemand misst (CPU und Speicher je Instanz im Verlauf),
behauptet sie nicht: es steht als „nicht gemessen“ da.
"""
import html
import time

W, H = 640, 150
PAD_L, PAD_R, PAD_T, PAD_B = 56, 10, 10, 22


def size(n):
    """„1,2 GB“; „—“, wenn unbekannt."""
    if n is None:
        return "—"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            s = f"{n:.0f}" if unit in ("B", "KB") or n >= 100 else f"{n:.1f}"
            return s.replace(".", ",") + " " + unit
        n /= 1024
    return "—"


def delta(now, then):
    """„+12 MB“ / „−3 MB“ / „±0“; „—“ ohne Vergleich."""
    if now is None or then is None:
        return "—"
    d = now - then
    if d == 0:
        return "±0"
    return ("+" if d > 0 else "−") + size(abs(d))


def tenant_bytes_series(ledger_lines):
    """[(day, bytes)] aus dem Tagesbuch des Berichts, nur gemessene Tage."""
    out = []
    for e in ledger_lines or []:
        b = e.get("bytes") if isinstance(e, dict) else None
        v = b.get("tenant") if isinstance(b, dict) else None
        if isinstance(v, (int, float)) and e.get("day"):
            out.append((str(e["day"]), int(v)))
    return out


def _value_at_or_before(series, day):
    best = None
    for d, v in series:
        if d <= day:
            best = v
    return best


def trend(series, today=None):
    """Heute (letzter Messtag) und die Veränderung gegen 7 und 30 Tage davor."""
    if not series:
        return {"now": None, "day": "", "d7": "—", "d30": "—"}
    day, now = series[-1]
    t = time.mktime(time.strptime(day, "%Y-%m-%d"))
    d7 = time.strftime("%Y-%m-%d", time.localtime(t - 7 * 86400))
    d30 = time.strftime("%Y-%m-%d", time.localtime(t - 30 * 86400))
    return {"now": now, "day": day,
            "d7": delta(now, _value_at_or_before(series[:-1], d7)),
            "d30": delta(now, _value_at_or_before(series[:-1], d30))}


def chart(series, days=90):
    """Die Datengröße des Mandanten als SVG, serverseitig, ohne Bibliothek
    (wie metrics_view). Eine Lücke im Tagesbuch ist eine Lücke im Bild."""
    pts = series[-days:]
    if len(pts) < 2:
        return ('<p class="muted">Noch zu wenige Messtage für einen Verlauf — '
                'der Knoten misst die Größe einmal am Tag.</p>')
    top = max(v for _, v in pts) * 1.1 or 1
    t0 = time.mktime(time.strptime(pts[0][0], "%Y-%m-%d"))
    t1 = time.mktime(time.strptime(pts[-1][0], "%Y-%m-%d"))
    span = max(t1 - t0, 86400)

    def x(day):
        t = time.mktime(time.strptime(day, "%Y-%m-%d"))
        return PAD_L + (t - t0) / span * (W - PAD_L - PAD_R)

    def y(v):
        return PAD_T + (1 - v / top) * (H - PAD_T - PAD_B)

    segs, cur, prev = [], [], None
    for d, v in pts:
        t = time.mktime(time.strptime(d, "%Y-%m-%d"))
        if prev is not None and t - prev > 1.5 * 86400 and cur:
            segs.append(cur)
            cur = []
        cur.append(f"{x(d):.1f},{y(v):.1f}")
        prev = t
    if cur:
        segs.append(cur)
    lines = "".join(f'<polyline points="{" ".join(s)}" fill="none" '
                    f'stroke="#2563eb" stroke-width="2"/>' for s in segs)
    grid = "".join(
        f'<line x1="{PAD_L}" x2="{W - PAD_R}" y1="{y(top * f):.1f}" y2="{y(top * f):.1f}" '
        f'stroke="currentColor" stroke-opacity=".12"/>'
        f'<text x="{PAD_L - 6}" y="{y(top * f) + 4:.1f}" text-anchor="end" '
        f'font-size="11" fill="currentColor" fill-opacity=".6">{html.escape(size(top * f))}</text>'
        for f in (0, 0.5, 1))
    labels = (f'<text x="{PAD_L}" y="{H - 4}" font-size="11" fill="currentColor" '
              f'fill-opacity=".6">{html.escape(pts[0][0])}</text>'
              f'<text x="{W - PAD_R}" y="{H - 4}" text-anchor="end" font-size="11" '
              f'fill="currentColor" fill-opacity=".6">{html.escape(pts[-1][0])}</text>')
    return (f'<svg viewBox="0 0 {W} {H}" width="100%" role="img" '
            f'aria-label="Datengröße des Mandanten, letzte {len(pts)} Messtage" '
            f'style="max-width:{W}px">{grid}{lines}{labels}</svg>')


def instance_rows(instances, probed, state_view, sizes, traffic, host_of):
    """Eine Zeile je Instanz des Mandanten.

    instances: {key: record} -- schon auf den Mandanten gefiltert
    probed:    {key: (state, label, detail)}
    state_view:{key: {"services": [{container, restarts, oom, ...}]}}
    sizes:     {key: {"kbytes": n}}   (Größenansicht des Hosts)
    traffic:   traffic.snapshot(): {"hosts": {host: [n, n101, bytes, durSum, durMax, 5xx]},
                                     "containers": [[name, cpu, memMB]]}
    host_of:   key, record -> [hosts, unter denen die Instanz antwortet]
    """
    cont_now = {}
    for c in (traffic or {}).get("containers") or []:
        if isinstance(c, (list, tuple)) and len(c) >= 3:
            cont_now[str(c[0])] = (c[1], c[2])
    hosts = (traffic or {}).get("hosts") or {}
    rows = []
    for key, inst in sorted(instances.items()):
        sv = (state_view or {}).get(key) or {}
        svcs = [s for s in sv.get("services") or [] if isinstance(s, dict)]
        restarts = sum(int(s.get("restarts") or 0) for s in svcs)
        oom = any(s.get("oom") for s in svcs)
        n = b = dur = n5 = 0
        seen = False
        for h in host_of(key, inst):
            r = hosts.get(h)
            if isinstance(r, list) and len(r) == 6:
                seen = True
                n += r[0]
                b += r[2]
                dur += r[3]
                n5 += r[5]
        cpu = mem = None
        for s in svcs:
            if s.get("container") in cont_now:
                c, m = cont_now[s["container"]]
                cpu = (cpu or 0) + float(c or 0)
                mem = (mem or 0) + float(m or 0)
        kb = ((sizes or {}).get(key) or {}).get("kbytes")
        st = probed.get(key) or ("unknown", "Unbekannt", "")
        rows.append({
            "key": key, "name": inst.get("name") or key,
            "app_name": inst.get("app_name", key), "version": inst.get("version", "?"),
            "channel": inst.get("channel", "production"),
            "state": st[0], "label": st[1], "detail": st[2],
            "restarts": restarts, "oom": oom,
            "data": size(kb * 1024) if isinstance(kb, (int, float)) else "—",
            "data_bytes": kb * 1024 if isinstance(kb, (int, float)) else None,
            "req": n if seen else None,
            "err": n5 if seen else None,
            "ms": round(dur / n) if seen and n else None,
            "out": size(b) if seen else "—",
            "cpu": None if cpu is None else round(cpu, 1),
            "mem": None if mem is None else round(mem),
        })
    return rows


def summary(rows):
    ok = sum(1 for r in rows if r["state"] == "ok")
    measured = [r for r in rows if r["req"] is not None]
    return {"total": len(rows), "ok": ok,
            "req": sum(r["req"] for r in measured) if measured else None,
            "err": sum(r["err"] for r in measured) if measured else None,
            "restarts": sum(r["restarts"] for r in rows),
            "oom": sum(1 for r in rows if r["oom"])}
