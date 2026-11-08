"""House style for Tom's sites — "a field manual for a quiet machine".

Shared tokens + SVG motifs. Each site is a product-line "model" with its own
spot color; RECOMP Mk.I is vermilion.
"""
from __future__ import annotations
import math

BONE = "#f3ecdc"
BONE_DEEP = "#e8dec8"     # card wells, knob faces
INK = "#1f2a44"
INK_SOFT = "#4a5470"      # secondary text
INK_FAINT = "#1f2a4426"   # construction lines
VERMILION = "#e2683c"
SAGE = "#5f8f7a"
BRASS = "#b8913f"         # used sparingly: rules, PR stamps' outline

MODELS = {  # the product line
    "RECOMP": ("Mk.I", VERMILION, "strength & body"),
    "SLEEP": ("Mk.I", "#3e5c8a", "nights & airway"),
    "LEDGER": ("Mk.I", BRASS, "money"),
    "GRIMOIRE": ("Mk.I", SAGE, "guitar"),
}

# riso paper grain — an SVG turbulence tile, multiplied over the bone
GRAIN = ("url(\"data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='240' height='240'>"
         "<filter id='n'><feTurbulence type='fractalNoise' baseFrequency='.85' numOctaves='2' stitchTiles='stitch'/>"
         "<feColorMatrix values='0 0 0 0 .12  0 0 0 0 .16  0 0 0 0 .26  0 0 0 .09 0'/></filter>"
         "<rect width='100%' height='100%' filter='url(%23n)'/></svg>\")")


def font_faces(prefix: str = "fonts/") -> str:
    faces = [("Poiret One", "normal", 400, "poiret-one"),
             ("IBM Plex Mono", "normal", 400, "plex-mono-400"),
             ("IBM Plex Mono", "italic", 400, "plex-mono-400i"),
             ("IBM Plex Mono", "normal", 500, "plex-mono-500"),
             ("IBM Plex Mono", "normal", 600, "plex-mono-600")]
    return "".join(
        f"@font-face{{font-family:'{f}';font-style:{s};font-weight:{w};"
        f"src:url({prefix}{fn}.woff2) format('woff2');font-display:block}}"
        for f, s, w, fn in faces)


def _pt(cx, cy, r, deg):
    """deg clockwise from 12 o'clock"""
    a = math.radians(deg)
    return cx + r * math.sin(a), cy - r * math.cos(a)


def sunburst(cx, cy, rx, ry, length=110, n=48, sweep=(-90, 90), stroke=INK, op=.2):
    """Deco rays radiating from an elliptical clear zone (so a number can sit inside)."""
    out = []
    lo, hi = sweep
    for i in range(n + 1):
        a = math.radians(lo + (hi - lo) * i / n)
        ux, uy = math.sin(a), -math.cos(a)
        x0, y0 = cx + rx * ux, cy + ry * uy
        L = length if i % 2 == 0 else length * .55
        x1, y1 = x0 + L * ux, y0 + L * uy
        out.append(f'<line x1="{x0:.1f}" y1="{y0:.1f}" x2="{x1:.1f}" y2="{y1:.1f}" '
                   f'stroke="{stroke}" stroke-opacity="{op}" stroke-width="{1.5 if i % 4 == 0 else .8}"/>')
    return "".join(out)


def arc(cx, cy, r, d0, d1, stroke=INK, op=1.0, w=1.5, extra=""):
    x0, y0 = _pt(cx, cy, r, d0)
    x1, y1 = _pt(cx, cy, r, d1)
    large = 1 if (d1 - d0) > 180 else 0
    return (f'<path d="M{x0:.1f},{y0:.1f} A{r},{r} 0 {large} 1 {x1:.1f},{y1:.1f}" fill="none" '
            f'stroke="{stroke}" stroke-opacity="{op}" stroke-width="{w}" {extra}/>')




def stepped_corners(W, H, inset=14, step=10, stroke=INK):
    """Deco stepped corners + double rule frame, as an absolutely-positioned SVG."""
    def corner(x, y, sx, sy):
        p = [(x, y + sy * step * 3), (x + sx * step, y + sy * step * 3), (x + sx * step, y + sy * step * 2),
             (x + sx * step * 2, y + sy * step * 2), (x + sx * step * 2, y + sy * step),
             (x + sx * step * 3, y + sy * step), (x + sx * step * 3, y)]
        return '<polyline fill="none" stroke="%s" stroke-width="1.5" points="%s"/>' % (
            stroke, " ".join(f"{a:.1f},{b:.1f}" for a, b in p))
    i2 = inset + 6
    o = [f'<rect x="{inset}" y="{inset}" width="{W-2*inset}" height="{H-2*inset}" fill="none" stroke="{stroke}" stroke-width="2.2"/>',
         f'<rect x="{i2}" y="{i2}" width="{W-2*i2}" height="{H-2*i2}" fill="none" stroke="{stroke}" stroke-width=".8"/>']
    j = i2 + 5
    o += [corner(j, j, 1, 1), corner(W - j, j, -1, 1), corner(j, H - j, 1, -1), corner(W - j, H - j, -1, -1)]
    return "".join(o)


def reg_mark(cx, cy, r=9, stroke=INK):
    return (f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{stroke}" stroke-width=".9"/>'
            f'<circle cx="{cx}" cy="{cy}" r="{r*.45:.1f}" fill="none" stroke="{stroke}" stroke-width=".9"/>'
            f'<line x1="{cx-r-5}" y1="{cy}" x2="{cx+r+5}" y2="{cy}" stroke="{stroke}" stroke-width=".9"/>'
            f'<line x1="{cx}" y1="{cy-r-5}" x2="{cx}" y2="{cy+r+5}" stroke="{stroke}" stroke-width=".9"/>')


def color_bar(x, y, sw=22, h=12):
    inks = [INK, VERMILION, SAGE, BRASS, INK_SOFT, BONE_DEEP]
    o = []
    for i, c in enumerate(inks):
        o.append(f'<rect x="{x+i*sw}" y="{y}" width="{sw}" height="{h}" fill="{c}"/>')
    for i, op in enumerate((.75, .5, .25)):
        o.append(f'<rect x="{x+(len(inks)+i)*sw}" y="{y}" width="{sw}" height="{h}" fill="{INK}" fill-opacity="{op}"/>')
    return "".join(o)


def engraved_line(points, W, H, *, pad=(46, 14, 28, 26), y_fmt="{:.0f}", baseline=None,
                  baseline_label="", x_labels=None, hatch_id="hatch", last_label="", min_span=0):
    """Engraving-style line chart: hatched area, ruled grid, stamped points.
    points: [(label, value, reliable)], equally spaced."""
    l, t, r, b = pad
    vals = [p[1] for p in points]
    lo, hi = min(vals + ([baseline] if baseline else [])), max(vals)
    if hi - lo < min_span:            # a near-flat series shouldn't magnify noise
        hi = lo + min_span
    span = hi - lo or 1
    lo -= span * .08
    hi += span * .1
    n = len(points)
    px = lambda i: l + (W - l - r) * (i / (n - 1 if n > 1 else 1))
    py = lambda v: t + (H - t - b) * (1 - (v - lo) / (hi - lo))
    o = [f'<svg viewBox="0 0 {W} {H}" width="100%" style="display:block">',
         f'<defs><pattern id="{hatch_id}" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">'
         f'<line x1="0" y1="0" x2="0" y2="6" stroke="{INK}" stroke-opacity=".22" stroke-width="1.2"/></pattern></defs>']
    # ruled grid: 4 lines, nice-ish steps
    step = 10 ** math.floor(math.log10((hi - lo) / 4))
    for m in (1, 2, 2.5, 5, 10):
        if (hi - lo) / (step * m) <= 5:
            step *= m
            break
    g = math.ceil(lo / step) * step
    while g <= hi:
        y = py(g)
        o.append(f'<line x1="{l}" y1="{y:.1f}" x2="{W-r}" y2="{y:.1f}" stroke="{INK}" stroke-opacity=".14" stroke-dasharray="1 3"/>')
        is_base = baseline is not None and baseline_label and abs(g - baseline) < 1e-9
        o.append(f'<text x="{l-8}" y="{y+4:.1f}" class="ax{" sage" if is_base else ""}" text-anchor="end">'
                 f'{baseline_label if is_base else y_fmt.format(g)}</text>')
        g += step
    if baseline is not None:
        y = py(baseline)
        o.append(f'<line x1="{l}" y1="{y:.1f}" x2="{W-r}" y2="{y:.1f}" stroke="{SAGE}" stroke-width="1.2" stroke-dasharray="6 4"/>')
    pts = [(px(i), py(v)) for i, (_, v, _) in enumerate(points)]
    area = f"M{pts[0][0]:.1f},{H-b} " + " ".join(f"L{x:.1f},{y:.1f}" for x, y in pts) + f" L{pts[-1][0]:.1f},{H-b} Z"
    o.append(f'<path d="{area}" fill="url(#{hatch_id})"/>')
    o.append(f'<line x1="{l}" y1="{H-b}" x2="{W-r}" y2="{H-b}" stroke="{INK}" stroke-width="1.4"/>')
    o.append('<polyline fill="none" stroke="%s" stroke-width="2.4" stroke-linejoin="round" points="%s"/>'
             % (INK, " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)))
    for i, ((x, y), (lbl, v, ok)) in enumerate(zip(pts, points)):
        last = i == n - 1
        if last:
            o.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="7" fill="{VERMILION}"/>')
            o.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="11" fill="none" stroke="{VERMILION}" stroke-width="1"/>')
            if last_label:
                ly = y + 30 if y < t + (H - t - b) * .6 else y - 16    # above the point when it hugs the axis
                o.append(f'<text x="{x-4:.1f}" y="{ly:.1f}" class="lastlbl" text-anchor="end">{last_label}</text>')
        else:
            o.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{BONE if ok else BONE_DEEP}" stroke="{INK}" '
                     f'stroke-width="1.6" {"" if ok else "stroke-dasharray=\"2 2\""}/>')
        if x_labels and lbl in x_labels:
            o.append(f'<text x="{x:.1f}" y="{H-b+17}" class="ax" text-anchor="middle">{x_labels[lbl]}</text>')
    o.append("</svg>")
    return "".join(o)



def range_rows(rows, W=952, row_h=27, name_w=230, val_w=96, chg_w=78):
    """Each lift this week, as a printed scale per row: the lift's 90-day low..high
    ruled out, a hollow dot at last session, a solid dot at this week, joined by a
    line. A static figure, deliberately NOT a knob or a fader: hardware imagery
    promises interactivity (Tom, 2026-10-05).
    rows: [dict(name, lo, hi, last|None, now, pr, pct|None)]"""
    lbl_w = 40                                   # room for the low / high numbers
    x0 = name_w + lbl_w
    x1 = W - val_w - chg_w - lbl_w
    head = 24
    Ht = head + len(rows) * row_h + 4
    o = [f'<svg viewBox="0 0 {W} {Ht}" width="100%" style="display:block">',
         f'<text x="0" y="14" class="rh">lift</text>',
         f'<text x="{(x0+x1)/2:.0f}" y="14" class="rh" text-anchor="middle">90-day low ··· its range ··· 90-day high</text>',
         f'<text x="{W-chg_w-8}" y="14" class="rh" text-anchor="end">this week</text>',
         f'<text x="{W}" y="14" class="rh" text-anchor="end">vs last</text>',
         f'<line x1="0" y1="{head-4}" x2="{W}" y2="{head-4}" stroke="{INK}" stroke-width="1.2"/>']
    for i, r in enumerate(rows):
        y = head + i * row_h
        cy = y + row_h / 2
        span = r["hi"] - r["lo"]
        # one data point = no range yet: sit mid-scale rather than read as "at its low"
        fx = (lambda v: (x0 + x1) / 2) if span <= 0 else \
             (lambda v: x0 + (x1 - x0) * max(0.0, min(1.0, (v - r["lo"]) / span)))
        nm = r["name"] if len(r["name"]) <= 25 else r["name"][:24] + "…"
        o.append(f'<text x="0" y="{cy+4.5:.1f}" class="rn{" pr" if r["pr"] else ""}">{nm}</text>')
        # the printed scale: a hairline with ruler ticks, ends labelled
        o.append(f'<line x1="{x0}" y1="{cy:.1f}" x2="{x1}" y2="{cy:.1f}" stroke="{INK}" stroke-opacity=".35" stroke-width="1"/>')
        for k in range(11):
            tx = x0 + (x1 - x0) * k / 10
            th = 4 if k in (0, 5, 10) else 2
            o.append(f'<line x1="{tx:.1f}" y1="{cy-th:.1f}" x2="{tx:.1f}" y2="{cy+th:.1f}" stroke="{INK}" stroke-opacity=".35"/>')
        o.append(f'<text x="{x0-8}" y="{cy+4:.1f}" class="re" text-anchor="end">{r["lo"]:.0f}</text>')
        o.append(f'<text x="{x1+8}" y="{cy+4:.1f}" class="re">{r["hi"]:.0f}</text>')
        xn = fx(r["now"])
        now_fill = VERMILION if r["pr"] else INK
        if r["last"] is not None:
            xl = fx(r["last"])
            up = r["now"] > r["last"]
            if abs(xn - xl) > 1:
                o.append(f'<line x1="{xl:.1f}" y1="{cy:.1f}" x2="{xn:.1f}" y2="{cy:.1f}" '
                         f'stroke="{SAGE if up else INK_SOFT}" stroke-width="{3.2 if up else 2}" '
                         f'{"" if up else "stroke-dasharray=\"3 2\""}/>')
            o.append(f'<circle cx="{xl:.1f}" cy="{cy:.1f}" r="5" fill="{BONE}" stroke="{INK}" stroke-width="1.6"/>')
        o.append(f'<circle cx="{xn:.1f}" cy="{cy:.1f}" r="{6 if r["pr"] else 4.6}" fill="{now_fill}"/>')
        o.append(f'<text x="{W-chg_w-8}" y="{cy+4.5:.1f}" class="rv" text-anchor="end">{r["now"]:.1f}'
                 f'<tspan class="ru"> lb</tspan></text>')
        if r["pct"] is None:
            chg, cls = "first time", "rc"
        elif abs(r["pct"]) < 0.05:
            chg, cls = "held", "rc"
        else:
            chg, cls = f'{r["pct"]:+.1f}%', "rc up" if r["pct"] > 0 else "rc dn"
        o.append(f'<text x="{W}" y="{cy+4.5:.1f}" class="{cls}" text-anchor="end">{chg}</text>')
        if i < len(rows) - 1:
            o.append(f'<line x1="0" y1="{y+row_h}" x2="{W}" y2="{y+row_h}" stroke="{INK}" stroke-opacity=".12" stroke-dasharray="1 3"/>')
    o.append("</svg>")
    return "".join(o)


def stamp_legend():
    """Key for range_rows, drawn with the same marks."""
    return (f'<svg viewBox="0 0 680 18" width="680" height="18">'
            f'<circle cx="8" cy="9" r="5" fill="{BONE}" stroke="{INK}" stroke-width="1.6"/>'
            f'<text x="20" y="13" class="lg">last session</text>'
            f'<circle cx="130" cy="9" r="4.6" fill="{INK}"/><text x="142" y="13" class="lg">this week</text>'
            f'<line x1="232" y1="9" x2="256" y2="9" stroke="{SAGE}" stroke-width="3.2"/>'
            f'<text x="264" y="13" class="lg">gained</text>'
            f'<line x1="332" y1="9" x2="356" y2="9" stroke="{INK_SOFT}" stroke-width="2" stroke-dasharray="3 2"/>'
            f'<text x="364" y="13" class="lg">dropped</text>'
            f'<circle cx="448" cy="9" r="6" fill="{VERMILION}"/><text x="460" y="13" class="lg">all-time record</text>'
            '</svg>')
