"""Cardio section of the recomp dashboard: "Am I getting my cardio in?"

Reads health/cardio/*.csv written by training/sync_cardio.py. Three layers,
matching what Tom asked for on 2026-10-27:
  1. this week — two session badges, a Mon–Sun strip and a "what's left" line
  2. over time — weekly minutes with session marks, and 4x4 interval peaks
  3. is it working — resting HR, HRV, deep-sleep breathing rate (objective only;
     Tom declined a stress log)
Targets come from programming.md, *Cardio — for stress first* (adopted 2026-10-23).
"""
from __future__ import annotations

import csv
from datetime import date, timedelta
from pathlib import Path
from statistics import mean

import config

ROOT = config.DATA
CARDIO = config.CARDIO
PLAN_START = date(2026, 10, 23)    # cardio-for-stress plan adopted
INTERVAL_HIT = 152
WEEKS = 12


def _rows(name):
    p = CARDIO / name
    if not p.exists():
        return []
    with p.open() as f:
        return list(csv.DictReader(f))


def _monday(d):
    return d - timedelta(days=d.weekday())


def load():
    daily = {date.fromisoformat(r["date"]): r for r in _rows("daily.csv")}
    sessions = _rows("sessions.csv")
    for s in sessions:
        s["d"] = date.fromisoformat(s["date"])
        s["minutes"] = int(s["minutes"] or 0)
        s["peaks_l"] = [int(p) for p in (s.get("peaks") or "").split()]
    rec = []
    for r in _rows("recovery.csv"):
        rec.append({"d": date.fromisoformat(r["date"]),
                    **{k: (float(r[k]) if r[k] else None)
                       for k in ("resting_hr", "hrv_rmssd", "resp_rate")}})
    return daily, sessions, rec


def week_status(sessions, mon):
    """Best moderate session and best 4x4 in the Mon–Sun week starting `mon`."""
    wk = [s for s in sessions if mon <= s["d"] < mon + timedelta(days=7)]
    mods = sorted((s for s in wk if s["kind"] == "moderate"), key=lambda s: -s["minutes"])
    fours = [s for s in wk if s["kind"] == "4x4"]
    fours.sort(key=lambda s: (s["grade"] != "hit", -max(s["peaks_l"] or [0])))
    boosts = [s for s in wk if s["kind"] == "boost"]
    return (mods[0] if mods else None), (fours[0] if fours else None), boosts


# ---------------------------------------------------------------- 1. this week

def _badge(label, s, kind, esc):
    if s is None:
        return (f'<div class="cbadge open"><div class="cb-k">{label}</div>'
                f'<div class="cb-v">open</div><div class="cb-s">'
                + ("30–40 min at 110–130 bpm" if kind == "moderate"
                   else "4 × 4 min reaching 152+, 3 min easy between")
                + '</div></div>')
    day = f'{s["d"]:%a}'
    if kind == "moderate":
        return (f'<div class="cbadge done"><div class="cb-k">{label}</div>'
                f'<div class="cb-v">✓ {s["minutes"]} min</div>'
                f'<div class="cb-s">{day} · avg {s["avg_hr"]} bpm'
                + (" · override" if s["source"] == "override" else "") + '</div></div>')
    peaks = " · ".join(map(str, s["peaks_l"])) or "—"
    if s["grade"] == "hit":
        hits = sum(p >= INTERVAL_HIT for p in s["peaks_l"])
        sub = (f'{day} · marked hard by you' if s["source"] == "override"
               else f'{day} · {hits} of {len(s["peaks_l"])} intervals ≥{INTERVAL_HIT}')
        return (f'<div class="cbadge done"><div class="cb-k">{label}</div>'
                f'<div class="cb-v">✓ hit</div><div class="cb-s">{sub}<br>peaks {esc(peaks)}</div></div>')
    return (f'<div class="cbadge easy"><div class="cb-k">{label}</div>'
            f'<div class="cb-v">too easy</div><div class="cb-s">{day} · peaks {esc(peaks)}'
            f'<br>need {INTERVAL_HIT}+ by minute 2 · felt hard? override it</div></div>')


def whats_left(mod, four, today):
    days_left = 6 - today.weekday()          # after today
    weekend_left = [d for d in ("Sat", "Sun")
                    if (5 if d == "Sat" else 6) >= today.weekday()]
    four_ok = four is not None and four["grade"] == "hit"
    if mod and four_ok:
        return "good", "Both sessions done this week. Anything more is a bonus."
    todo = []
    if not four_ok:
        todo.append("the <b>4x4</b> (after a Pull, morning or midday)" if four is None
                    else "a proper <b>4x4</b> (the last one stayed under 152)")
    if not mod:
        todo.append("the <b>moderate session</b> (30–40 min, or after the weekend lift)")
    when = (f'{" or ".join(weekend_left)} still free' if weekend_left else "")
    if days_left == 0:
        if mod or four_ok:
            return "warn", f"Last day: still open, {' and '.join(todo)}. One session is the floor, and you have it."
        return "crit", f"Last day and no session yet: get one in, {' or '.join(todo)}."
    if today.weekday() >= 3 and not mod and not four_ok:
        return "warn", (f"Squeezed week: {days_left} day{'s' * (days_left != 1)} left and nothing in yet. "
                        f"Keep at least one: {' or '.join(todo)}. {when}.")
    return "", f"Still open: {' and '.join(todo)}. {days_left} day{'s' * (days_left != 1)} left{', ' + when if when else ''}."


def strip(daily, sessions, mon, today):
    cells = []
    for i in range(7):
        d = mon + timedelta(days=i)
        r = daily.get(d)
        ss = [s for s in sessions if s["d"] == d]
        fut = d > today
        lifts = (r or {}).get("lifts") or ""
        marks = []
        for s in ss:
            if s["kind"] == "moderate":
                marks.append(f'<i class="mk mod" title="moderate {s["minutes"]} min, avg {s["avg_hr"]}"></i>')
            elif s["kind"] == "4x4":
                cls = "four" if s["grade"] == "hit" else "four easy"
                marks.append(f'<i class="mk {cls}" title="4x4 {s["grade"]}: peaks {s["peaks"]}"></i>')
            elif s["kind"] == "boost":
                marks.append(f'<i class="mk boost" title="walk boost {s["minutes"]} min ({s["start"]})"></i>')
        mod = int(r["mod_min"]) + int(r["hard_min"]) if r else 0
        h = min(mod, 90) / 90 * 34
        cls = "fut" if fut else ("today" if d == today else "")
        cells.append(
            f'<div class="sd {cls}"><div class="sd-h">{d:%a}<span>{d:%-d}</span></div>'
            f'<div class="sd-l">{lifts.replace("+", " + ") if lifts else ("·" if not fut else "")}</div>'
            f'<div class="sd-m">{"".join(marks)}</div>'
            f'<div class="sd-b"><i style="height:{h:.1f}px"></i></div>'
            f'<div class="sd-n">{"" if fut else (str(mod) if r else "–")}</div></div>')
    return ('<div class="cstrip">' + "".join(cells) + '</div>'
            '<div class="calkey ckey"><span><i class="mk mod"></i>moderate session</span>'
            '<span><i class="mk four"></i>4x4 hit</span><span><i class="mk four easy"></i>4x4 too easy</span>'
            '<span><i class="mk boost"></i>walk boost 10–19 min</span>'
            '<span><i class="kbar"></i>minutes ≥105 bpm outside lifting</span></div>')


# ---------------------------------------------------------------- 2. over time

def weekly_chart(daily, sessions, today, esc, *, width=680, height=170):
    mons = [_monday(today) - timedelta(weeks=i) for i in range(WEEKS - 1, -1, -1)]
    have = [d for d in daily]
    first = min(have) if have else today
    rows = []
    for m in mons:
        days = [daily.get(m + timedelta(days=i)) for i in range(7)]
        mod = sum(int(r["mod_min"]) for r in days if r)
        hard = sum(int(r["hard_min"]) for r in days if r)
        ms, fs, _ = week_status(sessions, m)
        rows.append((m, mod, hard, ms, fs, m + timedelta(days=6) >= first))
    pad_l, pad_r, pad_t, pad_b = 36, 8, 26, 20
    yhi = max([mo + ha for _, mo, ha, *_ in rows] + [60]) * 1.1
    bw = (width - pad_l - pad_r) / len(rows)

    def py(v):
        return (height - pad_b) - v / yhi * (height - pad_b - pad_t)
    out = [f'<svg viewBox="0 0 {width} {height}" class="chart" preserveAspectRatio="xMidYMid meet">']
    step = 60 if yhi > 150 else 30
    v = 0
    while v <= yhi:
        y = py(v)
        out.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width-pad_r}" y2="{y:.1f}" class="grid"/>'
                   f'<text x="{pad_l-6}" y="{y+3:.1f}" class="axis" text-anchor="end">{v}</text>')
        v += step
    for i, (m, mod, hard, ms, fs, has) in enumerate(rows):
        x = pad_l + i * bw
        cur = m == _monday(today)
        if not has:
            out.append(f'<text x="{x+bw/2:.1f}" y="{height-pad_b-4}" class="axis" text-anchor="middle">·</text>')
        else:
            ym, yh = py(mod), py(mod + hard)
            out.append(f'<rect x="{x+3:.1f}" y="{ym:.1f}" width="{bw-6:.1f}" height="{height-pad_b-ym:.1f}" '
                       f'fill="var(--sage)"{" opacity=.6" if cur else ""}><title>week of {m:%b %-d}: '
                       f'{mod} min moderate, {hard} min hard</title></rect>')
            if hard:
                out.append(f'<rect x="{x+3:.1f}" y="{yh:.1f}" width="{bw-6:.1f}" height="{ym-yh:.1f}" '
                           f'fill="var(--ink)"{" opacity=.6" if cur else ""}/>')
        # session marks above the bar: M = moderate session, 4 = 4x4 hit (hollow = too easy)
        cx = x + bw / 2
        if ms:
            out.append(f'<circle cx="{cx-6:.1f}" cy="10" r="4.5" fill="var(--sage)"><title>moderate session '
                       f'{ms["minutes"]} min</title></circle>')
        if fs:
            hit = fs["grade"] == "hit"
            out.append(f'<rect x="{cx+1.5:.1f}" y="5.5" width="9" height="9" transform="rotate(45 {cx+6:.1f} 10)" '
                       f'fill="{"var(--ink)" if hit else "none"}" stroke="var(--ink)" stroke-width="1.3">'
                       f'<title>4x4 {fs["grade"]}: peaks {esc(fs["peaks"])}</title></rect>')
        lab = f"{m:%b %-d}" if i % 2 == 0 or cur else ""
        out.append(f'<text x="{cx:.1f}" y="{height-5}" class="axis{" now" if cur else ""}" '
                   f'text-anchor="middle">{lab}</text>')
    out.append('</svg>')
    return "".join(out)


def interval_chart(sessions, esc, *, width=680, height=150):
    fours = [s for s in sessions if s["kind"] == "4x4" and s["peaks_l"]][-12:]
    if not fours:
        return ('<div class="empty">No 4x4 detected yet. It shows up here as one column of dots '
                'per session, one dot per interval.</div>')
    pad_l, pad_r, pad_t, pad_b = 36, 8, 10, 20
    lo, hi = 110, 180
    cw = (width - pad_l - pad_r) / max(len(fours), 4)

    def py(v):
        v = max(lo, min(hi, v))
        return (height - pad_b) - (v - lo) / (hi - lo) * (height - pad_b - pad_t)
    out = [f'<svg viewBox="0 0 {width} {height}" class="chart" preserveAspectRatio="xMidYMid meet">',
           f'<rect x="{pad_l}" y="{py(170):.1f}" width="{width-pad_l-pad_r}" height="{py(152)-py(170):.1f}" '
           f'fill="var(--sage)" opacity=".16"/>',
           f'<line x1="{pad_l}" y1="{py(152):.1f}" x2="{width-pad_r}" y2="{py(152):.1f}" class="floor-line"/>',
           f'<text x="{width-pad_r}" y="{py(170)+11:.1f}" class="axis" text-anchor="end">target 152–170</text>']
    for v in (120, 140, 160, 180):
        out.append(f'<text x="{pad_l-6}" y="{py(v)+3:.1f}" class="axis" text-anchor="end">{v}</text>')
    for i, s in enumerate(fours):
        cx = pad_l + cw * (i + .5)
        for j, p in enumerate(s["peaks_l"]):
            hit = p >= INTERVAL_HIT
            out.append(f'<circle cx="{cx + (j - (len(s["peaks_l"]) - 1) / 2) * 7:.1f}" cy="{py(p):.1f}" r="3.6" '
                       f'fill="{"var(--ink)" if hit else "var(--bone)"}" stroke="var(--ink)" stroke-width="1.3">'
                       f'<title>{s["d"]:%b %-d} interval {j+1}: {p} bpm</title></circle>')
        out.append(f'<text x="{cx:.1f}" y="{height-5}" class="axis" text-anchor="middle">{s["d"]:%b %-d}</text>')
    out.append('</svg>')
    return "".join(out)


# ---------------------------------------------------------------- 3. is it working

def _roll7(rec, key):
    pts = [(r["d"], r[key]) for r in rec if r[key] is not None]
    out = []
    for i, (d, _) in enumerate(pts):
        win = [v for dd, v in pts if d - timedelta(days=6) <= dd <= d]
        if len(win) >= 4:
            out.append((d, mean(win)))
    return out


def outcome_tiles(rec, today, date_sparkline, stat_tile):
    if not rec:
        return '<div class="empty">No recovery data yet (sleep_data API).</div>'
    specs = [("resting_hr", "resting HR", "bpm", -1, "series-1", "{:.1f}"),
             ("hrv_rmssd", "HRV (RMSSD)", "ms", +1, "series-2", "{:.1f}"),
             ("resp_rate", "breathing rate, deep sleep", "/min", -1, "series-1", "{:.2f}")]
    out = ['<div class="ctiles">']
    for key, label, unit, better, role, fmt in specs:
        s7 = _roll7(rec, key)
        if not s7:
            continue
        base = [v for d, v in s7 if PLAN_START - timedelta(days=28) <= d < PLAN_START]
        now = s7[-1][1]
        sub = "7-day average"
        tone = ""
        if base and s7[-1][0] >= PLAN_START + timedelta(days=7):
            b = mean(base)
            delta = now - b
            good = delta * better > 0
            sub = (f'{"+" if delta >= 0 else "−"}{fmt.format(abs(delta))} vs the 4 weeks before Oct 23 '
                   + ("(better)" if good else "(not better yet)"))
            tone = "good" if good else ""
        elif base:
            sub = f'baseline {fmt.format(mean(base))} (4 wk before Oct 23) · too early to compare'
        recent = [(d, v) for d, v in s7 if d >= today - timedelta(days=120)]
        out.append('<div class="ctile">' + stat_tile(f'{fmt.format(now)}<small> {unit}</small>', label, sub, tone)
                   + date_sparkline(recent, role, label, width=320, height=62,
                                    fmt=lambda v, f=fmt: f.format(v)) + '</div>')
    out.append('</div>')
    return "".join(out)


# ---------------------------------------------------------------- section

def section(n, *, h2, explain, esc, date_sparkline, stat_tile, today=None):
    today = today or date.today()
    daily, sessions, rec = load()
    P = ['<section class="card">' + h2(n, "Am I getting my cardio in?", "for stress first · Mon–Sun · wrist HR")]
    if not daily:
        P.append('<div class="empty">No cardio data yet. Run <code>training/sync_cardio.py</code>.</div></section>')
        return "".join(P)
    last = max(daily)
    if (today - last).days > 2:
        P.append(f'<div class="banner warn">⚠ Heart-rate data stops at <b>{last:%b %-d}</b>. '
                 'Check <code>logs/sync_cardio.log</code> and the Google Health token.</div>')
    mon = _monday(today)
    mod, four, boosts = week_status(sessions, mon)
    P.append('<div class="cbadges">' + _badge("Moderate session", mod, "moderate", esc)
             + _badge("4x4 intervals", four, "4x4", esc)
             + f'<div class="cbadge mini"><div class="cb-k">Walk boosts</div>'
               f'<div class="cb-v">{len(boosts)}</div><div class="cb-s">free extra · 10–15 min brisk</div></div>'
             + '</div>')
    tone, msg = whats_left(mod, four, today)
    P.append(f'<p class="cleft {tone}">{msg}</p>')
    P.append(strip(daily, sessions, mon, today))

    P.append('<h3 class="ch3">Week by week</h3>')
    P.append('<div class="legend"><span class="lg"><i style="background:var(--sage)"></i>moderate min (105–131)</span>'
             '<span class="lg"><i style="background:var(--ink)"></i>hard min (132+)</span>'
             '<span class="lg">● moderate session done</span><span class="lg">◆ 4x4 hit · ◇ too easy</span></div>')
    P.append(weekly_chart(daily, sessions, today, esc))
    P.append('<h3 class="ch3">Are the 4x4s hard enough?</h3>')
    P.append(interval_chart(sessions, esc))
    P.append('<p class="cap">One column per 4x4, one dot per interval peak. Filled = reached 152. '
             'A trend up means you are getting the intensity; later, the same effort at a lower peak means fitter.</p>')

    P.append('<h3 class="ch3">Is it lowering stress?</h3>')
    P.append(outcome_tiles(rec, today, date_sparkline, stat_tile))
    P.append(explain(
        '<p><b>Why these targets.</b> Cardio is in the plan to calm a stressed nervous system '
        '(the Oct 23 sleep deep dive), not for fat loss. Your walking already gives you plenty of '
        'easy minutes below 105 bpm, so they are not counted here. What was missing was moderate '
        'effort and real intervals.</p>'
        '<p><b>How it is measured.</b> Minute heart rate from the watch, with lifting cut out using the '
        'Hevy session times. A bike or treadmill logged in Hevy is kept. A moderate session is 20+ '
        'minutes held at 105+; a 4x4 is 3+ distinct interval peaks, graded on 152.</p>'
        '<p><b>The watch reads low on a bike.</b> If a 4x4 felt hard but shows "too easy", add a '
        'line to <code>health/cardio/overrides.csv</code> (or ask the health bot) and it counts.</p>'
        '<p><b>Is it working?</b> The three tiles compare the latest 7-day average with the 4 weeks '
        'before the plan started. Day-to-day and week-to-week wobble is noise; the real check is the '
        're-audit around Jan 2027 (programming.md). Lower resting HR and breathing rate and higher HRV '
        'are the good directions.</p>',
        label="How to read this"))
    P.append('</section>')
    return "".join(P)


STYLE = """
.cbadges{display:flex;flex-wrap:wrap;gap:10px;margin:4px 0 10px}
.cbadge{flex:1 1 200px;border:1.2px solid var(--ink);border-radius:5px;padding:9px 12px 8px;background:rgba(232,222,200,.6)}
.cbadge.mini{flex:0 1 150px}
.cbadge.open{background:none;border-style:dashed}
.cbadge.done{background:rgba(95,143,122,.16);border-color:var(--good)}
.cbadge.easy{border-color:var(--brass);background:rgba(184,145,63,.1)}
.cb-k{font:400 .9rem var(--deco);letter-spacing:.12em;text-transform:uppercase}
.cb-v{font-size:1.35rem;font-weight:600;line-height:1.3}
.cbadge.done .cb-v{color:var(--good)} .cbadge.easy .cb-v{color:var(--warn)} .cbadge.open .cb-v{color:var(--muted)}
.cb-s{font-size:.7rem;color:var(--muted)}
.cleft{margin:4px 0 12px;font-size:.84rem}
.cleft.good{color:var(--good)} .cleft.warn{color:var(--warn)} .cleft.crit{color:var(--crit)}
.cstrip{display:grid;grid-template-columns:repeat(7,1fr);border-top:.8px solid var(--ink);border-left:.8px solid var(--ink)}
.sd{border-right:.8px solid var(--ink);border-bottom:.8px solid var(--ink);padding:6px 4px 4px;text-align:center;min-width:0}
.sd.today{background:rgba(232,222,200,.8);box-shadow:inset 0 0 0 1.6px var(--vermilion)}
.sd.fut{opacity:.45}
.sd-h{font-size:.66rem;text-transform:uppercase;letter-spacing:.1em;color:var(--ink-soft)}
.sd-h span{display:block;font-size:.9rem;letter-spacing:0;color:var(--ink);font-weight:600}
.sd-l{font-size:.68rem;color:var(--ink-soft);min-height:1.1em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sd-m{min-height:16px;margin:2px 0}
.sd-b{height:36px;display:flex;align-items:flex-end;justify-content:center;border-bottom:.8px dotted rgba(31,42,68,.3)}
.sd-b i{display:block;width:12px;background:var(--sage);border-radius:1px 1px 0 0}
.sd-n{font-size:.68rem;color:var(--muted);font-variant-numeric:tabular-nums}
.mk{display:inline-block;width:11px;height:11px;margin:0 1.5px;vertical-align:middle}
.mk.mod{border-radius:50%;background:var(--sage)}
.mk.four{transform:rotate(45deg) scale(.85);background:var(--ink)}
.mk.four.easy{background:none;border:1.4px solid var(--ink)}
.mk.boost{width:9px;height:4px;background:var(--ink-soft);border-radius:2px}
.ckey{flex-wrap:wrap;gap:6px 16px}
.ckey .kbar{display:inline-block;width:8px;height:12px;background:var(--sage);vertical-align:middle;margin-right:6px}
.ckey .mk{margin-right:6px}
.ch3{font:400 1rem var(--deco);letter-spacing:.12em;text-transform:uppercase;margin:22px 0 4px}
.axis.now{fill:var(--vermilion)}
.ctiles{display:flex;flex-wrap:wrap;gap:10px}
.ctile{flex:1 1 200px}
.ctile .tile{margin-bottom:2px}
.ctile .tile-val small{font-size:.7rem;font-weight:400;color:var(--muted)}
@media (max-width:520px){.sd-l{font-size:.58rem}.sd-h span{font-size:.8rem}.mk{width:9px;height:9px}}
"""
