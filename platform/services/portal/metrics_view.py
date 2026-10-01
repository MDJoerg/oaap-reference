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

import metrics

LABELS = {"4h": "4 h", "24h": "24 h", "1w": "1 Woche", "1m": "1 Monat"}
DEFAULT = "24h"
TITLES = (("cpu", "CPU"), ("mem", "Arbeitsspeicher"), ("disk", "Platte"))

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


def _y(v):
    return PAD_T + (100.0 - max(0.0, min(100.0, v))) / 100.0 * (H - PAD_T - PAD_B)


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


def chart(title, points, now, span, step):
    """Ein Diagramm als SVG-Text."""
    t0 = now - span
    parts = [f'<svg viewBox="0 0 {W} {H}" class="vl-svg" role="img" '
             f'aria-label="{html.escape(title)}">']
    for v in (0, 50, 100):
        y = _y(v)
        parts.append(f'<line x1="{PAD_L}" x2="{W - PAD_R}" y1="{y:.1f}" '
                     f'y2="{y:.1f}" class="vl-grid"/>'
                     f'<text x="{PAD_L - 4}" y="{y + 3:.1f}" text-anchor="end" '
                     f'class="vl-axis">{v}</text>')
    parts.append(f'<text x="{PAD_L}" y="{H - 4}" class="vl-axis">'
                 f'-{html.escape(_ago(span))}</text>'
                 f'<text x="{W - PAD_R}" y="{H - 4}" text-anchor="end" '
                 f'class="vl-axis">jetzt</text>')
    for seg in _segments(points, step):
        if len(seg) == 1:                       # ein einzelner Punkt: ein Punkt
            p = seg[0]
            parts.append(f'<circle cx="{_x(p[0], t0, span):.1f}" '
                         f'cy="{_y(p[1]):.1f}" r="1.8" class="vl-dot"/>')
            continue
        top = " ".join(f"{_x(p[0], t0, span):.1f},{_y(p[3]):.1f}" for p in seg)
        bottom = " ".join(f"{_x(p[0], t0, span):.1f},{_y(p[2]):.1f}"
                          for p in reversed(seg))
        parts.append(f'<polygon points="{top} {bottom}" class="vl-band"/>')
        line = " ".join(f"{_x(p[0], t0, span):.1f},{_y(p[1]):.1f}" for p in seg)
        parts.append(f'<polyline points="{line}" class="vl-line"/>')
    parts.append("</svg>")
    return "".join(parts)


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
    note = (f"{w['step'] // 60} Min. je Punkt; Linie = Mittel, Band = Minimum "
            "bis Maximum.")
    if w["since"] > now - span + 2 * w["step"]:
        note = f"Daten seit {_ago(now - w['since'])}. " + note
    out.append(f'<p class="muted">{html.escape(note)}</p></div>')
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
