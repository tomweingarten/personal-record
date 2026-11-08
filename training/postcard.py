#!/usr/bin/env python3
"""Weekly postcard: one page per week of strength progress and PRs, shown at the
bottom of the dashboard to share anywhere. Strength & PRs ONLY: bodyweight, body
comp, nutrition and attendance stay private.

Rendered HTML -> PNG with a headless Chrome ([postcard] chrome in settings.toml);
skipped when none is configured. Run it nightly before build_dashboard.py so the
page shows the newest postcard.

    python3 training/postcard.py [YYYY-MM-DD week-start]
"""
from __future__ import annotations
import re
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

TRAIN = Path(__file__).resolve().parent
sys.path.insert(0, str(TRAIN))
import build_dashboard as B   # noqa: E402
import metrics                # noqa: E402
import house as H             # noqa: E402

import config  # noqa: E402

OUT_DIR = config.DASHBOARD / "postcards"
# Optional: a headless Chrome/Chromium binary (settings.toml [postcard] chrome).
CHROME = str(config.path_setting("postcard", "chrome") or "")
W, HT = 1080, 1350


EQUIP = {"Dumbbell": "DB", "Cable": "cable", "Machine": "mach", "Barbell": "BB",
         "Smith Machine": "smith", "Bodyweight": "BW"}


def short(title: str) -> str:
    t = re.sub(r"\s*\(.*?\)", "", title).strip()
    return {"Iso-Lateral Chest Press": "Chest Press", "Butterfly": "Pec Deck",
            "Overhead Cable Triceps Extension": "OH Triceps Ext",
            "Bayesian Bicep Curl": "Bayesian Curl", "Calf Extension": "Calf Raise",
            "Chest Supported Row": "Supported Row"}.get(t, t)


def name_lifts(lifts):
    """Short names, except where two lifts would collide ("Lateral Raise" is three
    different movements): those keep their equipment, abbreviated."""
    from collections import Counter
    # Up to two passes: equipment first, then the last qualifier ("Genesis") if
    # two variants share equipment too.
    for l in lifts:
        l["base"] = l["name"]
    for which in (0, -1):
        n = Counter(l["name"] for l in lifts)
        for l in lifts:
            if n[l["name"]] > 1:
                eq = re.findall(r"\(([^)]*)\)", l["title"])
                if eq and (which == 0 or len(eq) > 1):
                    q = eq[which]
                    l["name"] = f'{l["base"]} · {EQUIP.get(q, q.split()[0].lower())}'


def week_data(week_start: date):
    d = B.load_training()
    ws, we = week_start.isoformat(), (week_start + timedelta(days=6)).isoformat()
    idx = [r for r in B.composite_strength_index(d["trends"], d["last"]) if r[0] <= week_start]
    sb = metrics.session_best(d["enriched_sets"], d["last"], 3650)
    trend_n = {r["title"]: r for r in d["trends"]}
    lifts = []
    for t, bd in sb.items():
        pts = sorted((k, v["best_e1rm"] * metrics.KG_TO_LB) for k, v in bd.items())
        wk = [v for k, v in pts if ws <= k <= we]
        if not wk:
            continue
        prior = [v for k, v in pts if k < ws]
        best = max(wk)
        win = [v for k, v in pts if k >= (week_start - timedelta(days=90)).isoformat() and k <= we]
        lifts.append(dict(
            title=t, name=short(t), best=best, prev=prior[-1] if prior else None,
            prev_max=max(prior) if prior else None, lo=min(win), hi=max(win),
            pr=bool(prior) and best > max(prior) and len(prior) >= metrics.CALIBRATION_SESSIONS,
            n90=trend_n.get(t, {}).get("n", 0)))
    name_lifts(lifts)
    return idx, lifts


WORDS = "no one two three four five six seven eight nine ten eleven twelve".split()


def num_word(n):
    return WORDS[n] if n < len(WORDS) else str(n)


def caption(prs, lifts, delta_wk):
    if not lifts:
        return "A week of rest. The machine cools, and the numbers wait where they were left."
    up = sum(1 for l in lifts if l["prev"] and l["best"] > l["prev"])
    if prs:
        p = prs[0]["name"].lower()
        if len(prs) == 1:
            return (f"One new personal record this week. The {p} went past its old best, quietly, "
                    f"as if it had always known the way.")
        return (f"{num_word(len(prs)).capitalize()} new personal records this week. The {p} went first; "
                f"the others followed, the way lamps come on along a road at dusk.")
    if up:
        return (f"No records this week, but {num_word(up)} lift{'s' if up != 1 else ''} edged past "
                f"{'their' if up != 1 else 'its'} last session. The machine is warming, slow as morning.")
    return "A quiet week. The numbers held their ground, which is its own kind of work."


def render_html(week_start: date, fonts_prefix="../fonts/") -> str:
    idx, lifts = week_data(week_start)
    head = idx[-1]
    prev = idx[-2] if len(idx) > 1 else head
    # Two different things: did Tom train (any lift logged), and does the strength
    # index have a point for this week (needs lifts repeated from the week before).
    # A trained week can lack an index point, e.g. Aug 10 2026, two sessions of new lifts.
    trained = bool(lifts)
    measured = trained and head[0] == week_start
    delta_wk = (head[1] / prev[1] - 1) * 100 if measured else 0.0
    since = head[1] - 100
    base = idx[0][0]
    wnum = week_start.isocalendar()[1]
    we = week_start + timedelta(days=6)
    prs = sorted([l for l in lifts if l["pr"]], key=lambda l: -(l["best"] / l["prev_max"]))

    # --- fig.1 overall strength, as % gained since the baseline week
    pts = [(r[0].isoformat(), r[1] - 100, r[3]) for r in idx]
    xl = {pts[0][0]: f"{idx[0][0]:%b %-d}", pts[-1][0]: f"{idx[-1][0]:%b %-d}"}
    mid = len(pts) // 2
    xl[pts[mid][0]] = f"{idx[mid][0]:%b %-d}"
    fig1 = H.engraved_line(pts, 960, 150, min_span=10, baseline=0, baseline_label="start",
                           x_labels=xl, y_fmt="{:+.0f}%", last_label=f"{since:+.1f}%")

    # --- fig.2 each lift this week: one printed scale per lift (90-day low..high),
    # last session -> this week. Replaced the knobs + "what moved" bars on
    # 2026-10-05: knobs and switches read as controls you could turn, not data.
    MAX_ROWS = 12
    def pct(l):
        return (l["best"] / l["prev"] - 1) * 100 if l["prev"] else None
    ordered = sorted(lifts, key=lambda l: (not l["pr"], -(pct(l) if pct(l) is not None else -1e9)))
    shown = ordered[:MAX_ROWS]
    lift_rows = H.range_rows([dict(name=l["name"], lo=min(l["lo"], l["prev"] or l["lo"]),
                                   hi=max(l["hi"], l["best"]), last=l["prev"], now=l["best"],
                                   pr=l["pr"], pct=pct(l)) for l in shown])
    more = (f'<p class="more">+ {len(ordered) - MAX_ROWS} more lifts trained this week</p>'
            if len(ordered) > MAX_ROWS else "")

    ledger = "".join(
        f'<tr><td>{l["name"]}</td><td class="num old">{l["prev_max"]:.1f}</td>'
        f'<td class="arrow">→</td><td class="num new">{l["best"]:.1f}</td>'
        f'<td class="num pct">+{(l["best"]/l["prev_max"]-1)*100:.1f}%</td></tr>' for l in prs[:4]) \
        + (f'<tr><td colspan="5" class="none">and {len(prs) - 4} more</td></tr>' if len(prs) > 4 else "") \
        or '<tr><td colspan="5" class="none">No new records this week. The old bests hold.</td></tr>'

    frame = (f'<svg class="frame" viewBox="0 0 {W} {HT}" width="{W}" height="{HT}">'
             + H.stepped_corners(W, HT, inset=26) + H.reg_mark(W / 2, 13, 7)
             + H.reg_mark(13, HT / 2, 6) + H.reg_mark(W - 13, HT / 2, 6)
             + H.color_bar(W - 26 - 9 * 22, HT - 19, sw=22, h=9) + "</svg>")
    sun = (f'<svg class="sun" viewBox="0 0 640 300" width="640" height="300">'
           + H.sunburst(320, 270, 235, 150, length=105, n=44) + "</svg>")

    body = (f''' <div class="fig"><span class="n">fig. 2</span><span class="t">Each lift this week</span><span class="rule"></span><span class="note">est. one-rep max, lb</span></div>
 <div class="legend">{H.stamp_legend()}</div>
 {lift_rows}{more}
 <div class="fig"><span class="n">fig. 3</span><span class="t">New personal records</span><span class="rule"></span><span class="note">best ever logged for that lift</span></div>
 <table><tr><th>Lift</th><th>old best</th><th></th><th>new best</th><th>gain</th></tr>{ledger}</table>
''' if trained else
            '<div class="restweek"><p class="deco">A rest week</p><span>No sessions logged '
            f'{week_start:%b %-d} &ndash; {we:%b %-d}. The overall line above carries on from the last '
            'week that was trained.</span></div>')
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
{H.font_faces(fonts_prefix)}
*{{box-sizing:border-box;margin:0;padding:0}}
html,body{{width:{W}px;height:{HT}px;overflow:hidden}}
body{{background:{H.BONE};background-image:{H.GRAIN};color:{H.INK};font-family:'IBM Plex Mono',monospace;position:relative}}
.frame{{position:absolute;inset:0;pointer-events:none}}
.page{{position:absolute;inset:58px 64px 52px}}
.deco{{font-family:'Poiret One';letter-spacing:.14em;text-transform:uppercase;text-shadow:1.2px .8px 0 {H.VERMILION}55}}
header{{display:flex;justify-content:space-between;align-items:flex-end;border-bottom:2px solid {H.INK};padding-bottom:10px}}
header h1{{font-size:54px;line-height:1;font-weight:400}}
header h1 em{{font-style:normal;color:{H.VERMILION};font-size:30px;letter-spacing:.08em;margin-left:6px}}
.meta{{text-align:right;font-size:13px;line-height:1.5;letter-spacing:.08em;text-transform:uppercase;color:{H.INK_SOFT}}}
.meta b{{color:{H.INK};font-weight:600}}
.sub{{display:flex;justify-content:space-between;font-size:11.5px;letter-spacing:.18em;text-transform:uppercase;padding:5px 0;border-bottom:.8px solid {H.INK};color:{H.INK_SOFT}}}
.hero{{position:relative;height:214px;overflow:hidden;margin-top:6px;text-align:center}}
.sun{{position:absolute;left:50%;transform:translateX(-50%);top:-44px}}
.hero .big{{position:absolute;left:0;right:0;top:78px;font-size:92px;font-weight:600;letter-spacing:-.02em;line-height:1}}
.hero .lbl{{position:absolute;left:0;right:0;top:178px;font-size:21px}}
.side{{position:absolute;top:94px;width:200px;text-align:left;font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:{H.INK_SOFT}}}
.side.r{{right:0;text-align:right}}
.side b{{display:block;font-size:34px;letter-spacing:0;color:{H.INK};font-weight:500;margin-top:2px}}
.side b.v{{color:{H.VERMILION}}}
.side small{{display:block;font-size:10px;letter-spacing:.04em;text-transform:none;font-style:italic}}
.fig{{display:flex;align-items:baseline;gap:12px;margin:12px 0 4px;font-size:12px;letter-spacing:.14em;text-transform:uppercase}}
.fig .n{{font-family:'Poiret One';font-size:18px;letter-spacing:.1em;color:{H.VERMILION};white-space:nowrap}}
.fig .t{{font-family:'Poiret One';font-size:20px;letter-spacing:.12em;white-space:nowrap}}
.fig .rule{{flex:1;border-bottom:.8px dotted {H.INK}}}
.fig .note{{color:{H.INK_SOFT};font-style:italic;text-transform:none;letter-spacing:0;white-space:nowrap}}
svg text.ax{{font:11px 'IBM Plex Mono';fill:{H.INK_SOFT}}}
svg text.sage{{fill:{H.SAGE}}}
svg text.lastlbl{{font:600 15px 'IBM Plex Mono';fill:{H.VERMILION}}}
svg text.rh{{font:10.5px 'IBM Plex Mono';fill:{H.INK_SOFT};letter-spacing:.1em;text-transform:uppercase}}
svg text.rn{{font:13px 'IBM Plex Mono';fill:{H.INK}}}
svg text.rn.pr{{font-weight:600;fill:{H.VERMILION}}}
svg text.re{{font:10.5px 'IBM Plex Mono';fill:{H.INK_SOFT}}}
svg text.rv{{font:500 13.5px 'IBM Plex Mono';fill:{H.INK}}}
svg tspan.ru{{font-size:10px;fill:{H.INK_SOFT}}}
svg text.rc{{font:500 12.5px 'IBM Plex Mono';fill:{H.INK_SOFT}}}
svg text.rc.up{{fill:#3d7560}}
.legend{{margin:2px 0 6px}}
.more{{font-size:11px;color:{H.INK_SOFT};font-style:italic;margin-top:4px}}
svg text.lg{{font:11.5px 'IBM Plex Mono';fill:{H.INK_SOFT}}}
.stamp{{display:inline-block;font:600 10px 'IBM Plex Mono';color:{H.VERMILION};border:1.4px solid {H.VERMILION};padding:0 4px;margin-left:7px;transform:rotate(-6deg) translateY(-2px);letter-spacing:.1em}}
table{{width:100%;border-collapse:collapse;font-size:13.5px;margin-top:2px}}
th{{font-weight:400;font-size:10.5px;letter-spacing:.12em;text-transform:uppercase;color:{H.INK_SOFT};text-align:right;padding-bottom:2px}}
th:first-child{{text-align:left}}
td{{padding:6px 0;border-bottom:.8px dotted {H.INK}66}}
td:first-child{{white-space:nowrap;padding-right:8px}}
td.num{{text-align:right;font-variant-numeric:tabular-nums}}
td.old{{color:{H.INK_SOFT};text-decoration:line-through;text-decoration-color:{H.VERMILION}}}
td.arrow{{text-align:center;color:{H.INK_SOFT};width:24px}}
td.new{{font-weight:600}}
td.pct{{color:{H.VERMILION};width:66px}}
td.none{{font-style:italic;color:{H.INK_SOFT}}}
.foot{{font-size:11px;color:{H.INK_SOFT};margin-top:10px;line-height:1.5}}
.restweek{{margin:40px 0;text-align:center;border:1.4px dashed {H.INK}66;padding:40px 20px;border-radius:6px}}
.restweek p{{font-size:34px;margin-bottom:10px}}
.restweek span{{font-size:14px;color:{H.INK_SOFT};font-style:italic}}
.margin{{margin-top:16px;display:flex;gap:16px;align-items:flex-end}}
.margin p{{flex:1;font-style:italic;font-size:15px;line-height:1.5;border-left:2.5px solid {H.VERMILION};padding-left:14px}}
.colophon{{font-size:9.5px;letter-spacing:.14em;text-transform:uppercase;color:{H.INK_SOFT};text-align:right;line-height:1.6}}
</style></head><body>
{frame}
<div class="page">
 <header><h1 class="deco">Personal Record<em>Mk.I</em></h1>
  <div class="meta">Weekly strength report &middot; <b>Week {wnum}</b><br>{week_start:%b %-d} &ndash; {we:%b %-d, %Y}</div></header>
 <div class="sub"><span>Strength &amp; personal records</span><span>p. {wnum} / 52</span></div>
 <div class="hero">{sun}
  <div class="side"><span>This week</span><b class="v">{f"{delta_wk:+.1f}%" if measured else "—" if trained else "rest"}</b>{"" if measured or not trained else "<small>no repeat lifts to compare</small>"}</div>
  <div class="side r"><span>New records</span><b class="v">{len(prs)}</b></div>
  <div class="big">{since:+.1f}%</div><div class="lbl deco">Stronger since {base:%B %-d}</div></div>
 <div class="fig"><span class="n">fig. 1</span><span class="t">Overall strength, week by week</span><span class="rule"></span><span class="note">avg. change across every lift trained</span></div>
 {fig1}
{body}
 <div class="margin"><p>{caption(prs, lifts, delta_wk)}</p>
  <div class="colophon">printed {date.today():%b %-d %Y}{'<br>' + B.COLOPHON if B.COLOPHON else ''}</div></div>
</div></body></html>"""


def render_png(html_path: Path, png_path: Path, scale=2):
    subprocess.run([CHROME, "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
                    f"--force-device-scale-factor={scale}", f"--window-size={W},{HT}",
                    "--virtual-time-budget=3000", f"--screenshot={png_path}",
                    html_path.as_uri()], check=True, capture_output=True, timeout=90)


def card_name(week_start: date) -> str:
    y, w, _ = week_start.isocalendar()
    return f"postcard-{y}-w{w:02d}"


def main(argv):
    if not CHROME or not Path(CHROME).exists():
        print("postcard skipped: set [postcard] chrome in settings.toml to a headless Chrome")
        return 0
    """Render the last complete Mon-Sun week (or the week starting argv[1]).
    Re-rendered nightly: a late sync or a corrected log lands on the next run."""
    today = date.today()
    ws = date.fromisoformat(argv[1]) if len(argv) > 1 else today - timedelta(days=today.weekday() + 7)
    OUT_DIR.mkdir(exist_ok=True)
    html_path = OUT_DIR / f"{card_name(ws)}.html"
    html_path.write_text(render_html(ws))
    png = html_path.with_suffix(".png")
    render_png(html_path, png)
    print(f"{datetime.now():%Y-%m-%d %H:%M} wrote {png}")


if __name__ == "__main__":
    main(sys.argv)
