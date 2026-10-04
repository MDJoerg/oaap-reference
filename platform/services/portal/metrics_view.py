"""Der Verlauf auf der Gesundheitsseite (RFC-0051, Stufe 2).

Drei kleine Diagramme -- CPU, Arbeitsspeicher, Platte -- und vier Knoepfe
fuer das Fenster. Gezeichnet wird als SVG, serverseitig, ohne Bibliothek
und ohne etwas von aussen zu laden: die Seite muss auf einem Knoten ohne
Internet genauso aussehen. Wie `relay_view.py` und `store_view.py` traegt
diese Datei kein Flask; was sie braucht, ist ein Verzeichnis und eine Uhr.

Die Skala ist immer 0-100 Prozent, damit zwei Diagramme (und zwei Knoten)
vergleichbar bleiben. Das Mittel ist die Linie, zwischen Minimum und
Maximum liegt ein blasses Band -- die Spitze, die ein Mittel verschluckt,
bleibt sichtbar. Eine Luecke in den Daten ist eine Luecke im Bild: die
Linie reisst ab, sie wird nicht ueberbrueckt.
"""
import html
import json
import os

import metrics
import traffic

LABELS = {"4h": "4 h", "24h": "24 h", "1w": "1 Woche", "1m": "1 Monat"}
DEFAULT = "24h"
TITLES = (("cpu", "CPU"), ("mem", "Arbeitsspeicher"), ("disk", "Platte"))
# RFC-0051 stage 3: no natural scale, so each chart picks its own.
TRAFFIC_TITLES = (("req", "Anfragen je Minute"),
                  ("lat", "Dauer je Anfrage (ms)"),
                  ("ctop", "Staerkster Container (% eines Kerns)"),
                  ("tx", "Netz raus (KB/s)"), ("rx", "Netz rein (KB/s)"),
                  ("conn", "TCP-Verbindungen"))
AXIS_DIV = {"tx": 1000.0, "rx": 1000.0}

W, H = 300, 120
PAD_L, PAD_R, PAD_T, PAD_B = 30, 8, 8, 18


def clean_window(key):
    return key if key in metrics.WINDOWS else DEFAULT


def _ago(seconds):
    """„2 Std. 10 Min." -- relative, damit keine Zeitzone geraten wird."""
    m = max(0, int(seconds // 60))
    d, m = divmod(m, 1440)
    h, m = divmod(m, 60)
    if d:
        return f"{d} Tg. {h} Std."
    if h:
        return f"{h} Std. {m} Min."
    return f"{m} Min."


def _x(t, t0, span):
    return PAD_L + (t - t0) / span * (W - PAD_L - PAD_R)


def _y(v, top=100.0):
    return PAD_T + (top - max(0.0, min(top, v))) / top * (H - PAD_T - PAD_B)


def nice_top(peak):
    """The scale's upper end for a series without a natural one: 1, 2, 5
    times a power of ten above the peak. 0 or nothing gives 1."""
    if not peak or peak <= 0:
        return 1.0
    p = 1.0
    while p * 10 <= peak:
        p *= 10
    while p > peak:
        p /= 10
    for f in (1, 2, 5, 10):
        if peak <= p * f:
            return p * f
    return p * 10


def fmt(series, v):
    """A value with its unit, for the head of a chart and the axis."""
    if series in ("tx", "rx"):
        for limit, unit in ((1e6, "MB/s"), (1e3, "KB/s")):
            if v >= limit:
                return f"{v / limit:.1f} {unit}".replace(".", ",")
        return f"{v:.0f} B/s"
    if series == "lat":
        return (f"{v / 1000:.1f} s" if v >= 1000 else f"{v:.0f} ms").replace(".", ",")
    if series == "req":
        return f"{v:.0f}/Min."
    if series == "ctop":
        return f"{v:.0f} % Kern"
    if series == "conn":
        return f"{v:.0f}"
    return f"{v:.0f} %".replace(".", ",")


def _segments(points, step):
    """Zusammenhaengende Stuecke: ein Abstand ueber zwei Schritte ist eine
    Luecke, und eine Luecke wird nicht ueberbrueckt."""
    seg, out = [], []
    for p in points:
        if seg and p[0] - seg[-1][0] > 2 * step:
            out.append(seg)
            seg = []
        seg.append(p)
    if seg:
        out.append(seg)
    return out


def chart(title, points, now, span, step, top=100.0, series=""):
    """Ein Diagramm als SVG-Text. `top` ist das obere Ende der Skala:
    100 fuer Prozent, bei den Verkehrsreihen nach dem Gipfel gewaehlt."""
    t0 = now - span
    parts = [f'<svg viewBox="0 0 {W} {H}" class="vl-svg" role="img" '
             f'aria-label="{html.escape(title)}">']
    for v in (0, top / 2, top):
        y = _y(v, top)
        # The axis carries bare numbers; the unit is in the chart's head.
        label = f"{v / AXIS_DIV.get(series, 1):g}"
        parts.append(f'<line x1="{PAD_L}" x2="{W - PAD_R}" y1="{y:.1f}" '
                     f'y2="{y:.1f}" class="vl-grid"/>'
                     f'<text x="{PAD_L - 4}" y="{y + 3:.1f}" text-anchor="end" '
                     f'class="vl-axis">{html.escape(label)}</text>')
    parts.append(f'<text x="{PAD_L}" y="{H - 4}" class="vl-axis">'
                 f'-{html.escape(_ago(span))}</text>'
                 f'<text x="{W - PAD_R}" y="{H - 4}" text-anchor="end" '
                 f'class="vl-axis">jetzt</text>')
    for seg in _segments(points, step):
        if len(seg) == 1:                       # ein einzelner Punkt: ein Punkt
            p = seg[0]
            parts.append(f'<circle cx="{_x(p[0], t0, span):.1f}" '
                         f'cy="{_y(p[1], top):.1f}" r="1.8" class="vl-dot"/>')
            continue
        upper = " ".join(f"{_x(p[0], t0, span):.1f},{_y(p[3], top):.1f}"
                         for p in seg)
        bottom = " ".join(f"{_x(p[0], t0, span):.1f},{_y(p[2], top):.1f}"
                          for p in reversed(seg))
        parts.append(f'<polygon points="{upper} {bottom}" class="vl-band"/>')
        line = " ".join(f"{_x(p[0], t0, span):.1f},{_y(p[1], top):.1f}"
                        for p in seg)
        parts.append(f'<polyline points="{line}" class="vl-line"/>')
    parts.append("</svg>")
    return "".join(parts)


def _json(directory, name):
    try:
        with open(os.path.join(directory, name), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def sender_line(directory, now):
    """One line on how the sender (RFC-0052) is doing, or "" when none is
    configured. A loss is said in words, not only as a number: it is the
    one thing on this line somebody has to act on."""
    info = _json(directory, metrics.SENDER_INFO_FILE)
    if not info:
        return ""
    st = _json(directory, metrics.SENDER_STATE_FILE) or {}
    q = metrics.queue_status(directory)
    where = html.escape(str(info.get("host", "?")))
    waiting = f"{q['pending']} wartend"
    ok_at = st.get("ok_at")
    if st.get("err"):
        since = st.get("err_at")
        head = (f'<span class="err">nicht erreichbar oder abgelehnt</span>'
                + (f" seit {_ago(now - since)}" if isinstance(since, int) else ""))
        tail = [waiting, html.escape(str(st["err"])[:200])]
        if isinstance(ok_at, int):
            tail.insert(1, f"letzter Erfolg vor {_ago(now - ok_at)}")
    elif isinstance(ok_at, int):
        head = '<span class="ok">ok</span>, ' + f"zuletzt vor {_ago(now - ok_at)}"
        tail = [waiting]
    else:
        head = "noch nichts gesendet"
        tail = [waiting]
    if q["lost"]:
        tail.append(f'<span class="err">{q["lost"]} Messwerte gingen verloren '
                    "(die Warteschlange war zu voll oder zu alt)</span>")
    return (f'<p class="vl-send">Senden an <strong>{where}</strong>: {head} · '
            + " · ".join(tail) + "</p>")


def traffic_tables(directory, now):
    """Die letzte Stunde je Host und die staerksten Container. Alles darin
    kommt aus dem Zugriffsprotokoll bzw. von Docker -- ein Hostname ist
    die Wahl des Absenders, darum laeuft jeder durch html.escape."""
    snap = traffic.snapshot(directory)
    out = []
    hosts = sorted(snap["hosts"].items(), key=lambda x: -x[1][0])[:12]
    if hosts:
        out.append('<h3 class="vl-sub">Letzte Stunde je Host</h3>'
                   '<table class="vl-tab"><tr><th>Host</th><th>Anfragen</th>'
                   '<th>Dauerverb.</th><th>MB raus</th><th>Dauer &#216;</th>'
                   '<th>laengste</th><th>5xx</th></tr>')
        for h, r in hosts:
            timed = r[0] - r[1]
            mean = r[3] / timed if timed else 0.0
            slow = ' class="err"' if mean > 2 or r[4] > 10 else ""
            out.append(f"<tr><td>{html.escape(h)}</td><td>{r[0]}</td>"
                       f"<td>{r[1]}</td><td>{r[2] / 1e6:.1f}</td>"
                       f"<td{slow}>{mean:.2f} s</td>"
                       f"<td{slow}>{r[4]:.1f} s</td><td>{r[5]}</td></tr>")
        out.append("</table>")
    cs = snap["containers"]
    if cs:
        out.append('<h3 class="vl-sub">Container jetzt</h3>'
                   '<table class="vl-tab"><tr><th>Container</th>'
                   '<th>CPU (% eines Kerns)</th><th>Speicher MB</th></tr>')
        for n, c, mb in cs:
            hot = ' class="err"' if c >= 80 else ""
            out.append(f"<tr><td>{html.escape(str(n))}</td>"
                       f"<td{hot}>{c:.0f}</td><td>{mb:.0f}</td></tr>")
        out.append("</table>")
    return "".join(out)


def block(directory, key=DEFAULT, now=None):
    """Der ganze Block „Verlauf" als HTML-Text (alles Eingesetzte ist
    entweder eine Zahl oder durch html.escape gelaufen)."""
    import time
    key = clean_window(key)
    now = int(time.time() if now is None else now)
    w = metrics.window(directory, key, now=now)
    span = metrics.WINDOWS[key][1]
    out = ['<div class="card" id="verlauf">', "<h2>Verlauf</h2>",
           '<p class="vl-tabs">']
    for k, label in LABELS.items():
        cls = "vl-btn active" if k == key else "vl-btn"
        out.append(f'<a class="{cls}" data-w="{k}" '
                   f'href="/health?w={k}#verlauf">{html.escape(label)}</a>')
    out.append("</p>")
    if not w["since"]:
        out.append('<p class="muted">Noch keine Messwerte. Der Knoten misst '
                   "jede Minute; nach ein paar Minuten erscheint hier der "
                   "Verlauf.</p></div>")
        return "".join(out)
    out.append('<div class="vl-row">')
    for series, title in TITLES:
        pts = w["series"][series]
        now_v = f"{pts[-1][1]:.0f} %".replace(".", ",") if pts else "–"
        out.append('<div class="vl-cell">'
                   f'<div class="vl-head"><span>{html.escape(title)}</span>'
                   f'<strong>{now_v}</strong></div>'
                   + chart(title, pts, now, span, w["step"]) + "</div>")
    out.append("</div>")
    shown = [(s, t) for s, t in TRAFFIC_TITLES if w["series"].get(s)]
    if shown:
        out.append('<h3 class="vl-sub">Verkehr</h3><div class="vl-row">')
        for series, title in shown:
            pts = w["series"][series]
            top = nice_top(max(p[3] for p in pts))
            out.append('<div class="vl-cell">'
                       f'<div class="vl-head"><span>{html.escape(title)}</span>'
                       f'<strong>{html.escape(fmt(series, pts[-1][1]))}'
                       '</strong></div>'
                       + chart(title, pts, now, span, w["step"], top, series)
                       + "</div>")
        out.append("</div>")
    out.append(traffic_tables(directory, now))
    note = (f"{w['step'] // 60} Min. je Punkt; Linie = Mittel, Band = Minimum "
            "bis Maximum.")
    if w["since"] > now - span + 2 * w["step"]:
        note = f"Daten seit {_ago(now - w['since'])}. " + note
    out.append(f'<p class="muted">{html.escape(note)}</p>')
    out.append(sender_line(directory, now) + "</div>")
    return "".join(out)


STYLE = """
<style>
  .vl-tabs{display:flex;gap:.4rem;flex-wrap:wrap;margin:.2rem 0 .8rem}
  .vl-btn{padding:.25rem .7rem;border:1px solid var(--oaap-border);
          border-radius:999px;text-decoration:none;color:var(--oaap-text);
          font-size:.9rem;background:var(--oaap-surface)}
  .vl-btn.active{background:var(--oaap-blue-600);border-color:var(--oaap-blue-600);
          color:#fff}
  .vl-row{display:grid;grid-template-columns:repeat(3,1fr);gap:1rem}
  @media (max-width:820px){.vl-row{grid-template-columns:1fr}}
  .vl-head{display:flex;justify-content:space-between;align-items:baseline;
           font-size:.95rem}
  .vl-svg{width:100%;height:auto;display:block}
  .vl-grid{stroke:var(--oaap-border);stroke-width:1}
  .vl-axis{fill:var(--oaap-muted);font-size:9px}
  .vl-line{fill:none;stroke:var(--oaap-blue-600);stroke-width:1.6;
           stroke-linejoin:round}
  .vl-band{fill:var(--oaap-blue-600);opacity:.18;stroke:none}
  .vl-dot{fill:var(--oaap-blue-600)}
  .vl-send{margin:.4rem 0 0;font-size:.9rem}
  .vl-sub{margin:1rem 0 .4rem;font-size:1rem}
  .vl-tab{border-collapse:collapse;width:100%;font-size:.9rem}
  .vl-tab th,.vl-tab td{text-align:right;padding:.2rem .5rem;
          border-bottom:1px solid var(--oaap-border)}
  .vl-tab th:first-child,.vl-tab td:first-child{text-align:left;
          word-break:break-all}
  .vl-tab .err{color:#b00020;font-weight:600}
</style>
<script>
  // Ohne Skript sind die Knoepfe gewoehnliche Links. Mit Skript wird nur
  // der Block ausgetauscht, nicht die Seite.
  document.addEventListener("click", function (e) {
    var a = e.target.closest && e.target.closest("a.vl-btn");
    if (!a) return;
    e.preventDefault();
    fetch("/health/verlauf?w=" + encodeURIComponent(a.dataset.w),
          {credentials: "same-origin"})
      .then(function (r) { return r.ok ? r.text() : Promise.reject(); })
      .then(function (h) {
        var old = document.getElementById("verlauf");
        var t = document.createElement("div");
        t.innerHTML = h;
        old.replaceWith(t.firstElementChild);
      })
      .catch(function () { location.href = a.href; });
  });
</script>
"""
