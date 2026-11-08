#!/usr/bin/env python3
"""Personal Record dashboard — self-contained HTML with inline SVG. Stdlib only.

Reads Hevy training CSVs (via metrics.py), the latest MacroFactor daily export,
and body_composition.csv (may be empty), and writes one file under dashboard/:

  * recomp.html — full standalone doc, served by ../serve_dashboard.py

There used to be a second output, recomp.artifact.html, a headless fragment for
publishing to claude.ai as an Artifact. Retired 2026-08-07: it was a static
snapshot that only refreshed when someone manually re-published it (a cron can't
call the publish tool), so it was always staler than the live page served off the
box — and it put health data on a third-party host to do it.

Why this exists — the metric problem (2026-07-18):
  Hevy's headline e1RM is biased toward heavy low-rep sets, so a hypertrophy
  (8-15 rep) program's real progress is invisible to it. This dashboard reads
  progress the right way: a composite STRENGTH INDEX (each lift's session-best
  e1RM normalized to 100, trend-fit, averaged) plus VOLUME-LOAD, both of which
  register progress made by adding reps or load at any rep range — then frames
  it against flat bodyweight to tell the recomp story.

Regenerate:  python3 training/build_dashboard.py
"""
from __future__ import annotations
import csv
import glob
import html
import json
import math
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple

import cardio_panel
import house as H
import metrics
import routine_roles
from metrics import KG_TO_LB, monday_of

import config

HEALTH_ROOT = config.DATA
TRAIN_ROOT = config.TRAINING
NUTRITION_ROOT = config.NUTRITION
BODYCOMP_CSV = config.BODY_CSV
OUT_DIR = config.DASHBOARD

WINDOW_DAYS = 90
WEIGHT_WINDOW_DAYS = 180           # bodyweight trend horizon
NUTRITION_WINDOW_DAYS = 30         # adherence reflects *current* behavior
RPE_WINDOW_DAYS = 28               # RPE-logging habit indicator
TARGETS_WINDOW_DAYS = 42           # "recently trained" — the routine writer's window
# The working-targets TABLE reads a wider window than the routine writer does, so the
# "active in the last 60 days" filter button can actually show 60 days of lifts. Kept
# separate from TARGETS_WINDOW_DAYS on purpose: push_targets.py uses that constant to
# decide which lifts it will WRITE loads for in Hevy, and widening it there would put
# stale lifts back into the routines. Displaying a lift and prescribing one are
# different decisions.
DASH_TARGETS_DAYS = 60
MIN_SESSIONS = 4
# Personal targets come from settings.toml (see settings.example.toml).
TARGET_WEIGHT_LB = float(config.setting("body", "target_weight_lb", 170.0))
PROTEIN_FLOOR_G = float(config.setting("nutrition", "protein_floor_g", 150.0))
MAINTENANCE_KCAL = float(config.setting("nutrition", "maintenance_kcal", 2400.0))
HEIGHT_IN = float(config.setting("body", "height_in", 70.0))   # for the US Navy body-fat estimate
COLOPHON = config.setting("dashboard", "colophon", "")   # optional footer signature

GENERATED = datetime.now().strftime("%Y-%m-%d %H:%M")

# ---------------------------------------------------------------- palette
# Reference dataviz palette (validated). Roles are CSS custom props so the
# light/dark values swap in one place; see <style> block below.
C_STRENGTH = "series-1"   # blue
C_WEIGHT = "series-2"     # aqua


# ================================================================ data prep

def load_training():
    d = metrics.load_all(TRAIN_ROOT)
    d["trends"] = metrics.strength_trends(
        d["enriched_sets"], d["last"], WINDOW_DAYS, MIN_SESSIONS)
    return d


MIN_COMMON_LIFTS = 4      # overlap needed to move the chained index


def composite_strength_index(trends, last):
    """CHAIN-LINKED weekly strength index. Returns
    [(week_date, index, n_common, reliable)].

    Each week's change is measured only across lifts trained in BOTH that week and
    the anchor week, then chained — the method price/stock indices use. A plain
    average over whatever was trained that week is dominated by *composition*:
    Chest Press sits near 186 (it nearly doubled), so any week including it was
    yanked up, and weeks that happened to skip the weak lifts (RDL, Bayesian curl)
    read high. Measured like-for-like, 2026-07-20→07-27 was +2.5% while the naive
    average showed −3.8 points — opposite conclusions from the same data.

    Weeks with thin overlap get a *provisional* value (shown, marked unreliable)
    and do NOT advance the chain, so their noise can't compound into the level.
    """
    # A lift's calibration sessions (its first metrics.CALIBRATION_SESSIONS
    # ever) are left out: a feel-out set at a placeholder load reads as a huge
    # "gain" the week after, and the chain carries it forward forever. That is
    # how the Hip Thrust moved the headline +31.6% -> +53.9% on 2026-09-19.
    by_week: dict[str, dict[str, float]] = defaultdict(dict)
    for r in trends:
        skip = r.get("calib_dates", set())
        for val, dt in zip(r["e1_series"], r["dates"]):
            if dt.date().isoformat() in skip:
                continue
            by_week[monday_of(dt.date())][r["title"]] = val      # raw e1RM (lb)

    weeks = sorted(by_week)

    def overlap(a, b):
        return len(set(by_week[a]) & set(by_week[b]))

    # Chaining needs week-to-week overlap. Tom's May/early-June weeks were a
    # ramp-back-up with barely 2 lifts repeated between them, so a chain rooted
    # there compounds noise (it inflated the level to ~150). Root the index at the
    # first week whose *next* transition has real overlap: a short trustworthy
    # index beats a long fabricated one. The baseline date is shown in the UI.
    start = next((i for i in range(len(weeks) - 1)
                  if overlap(weeks[i], weeks[i + 1]) >= MIN_COMMON_LIFTS), None)
    if start is None:
        return []
    weeks = weeks[start:]
    rows = [(datetime.fromisoformat(weeks[0]).date(), 100.0, len(by_week[weeks[0]]), True)]
    level = 100.0
    for prev, wk in zip(weeks, weeks[1:]):
        cur, anchor = by_week[wk], by_week[prev]   # anchor is always the prior week
        common = set(cur) & set(anchor)
        ratio = (sum(cur[t] / anchor[t] for t in common) / len(common)) if common else 1.0
        level *= ratio
        rows.append((datetime.fromisoformat(wk).date(), level, len(common),
                     len(common) >= MIN_COMMON_LIFTS))
    return rows


class Headline(NamedTuple):
    """The one strength number the dashboard shows, plus what it was derived from.

    Extracted from build_html() on 2026-08-01 so it can be shared. A separate
    display project was reimplementing this selection rule inline against
    composite_strength_index() output, which meant the Hub and the dashboard could
    silently disagree if either changed. One implementation, two consumers.
    """
    week: date | None       # the headline week ("through") — complete + reliable
    index: float            # index at that week
    delta_pct: float        # index - 100
    partial: bool           # a provisional / in-progress week exists beyond `week`
    baseline: date | None   # week the chain is rooted at (index == 100)
    rows: list              # the rows the headline was chosen from, ascending


def strength_headline(idx_rows, today=None) -> Headline:
    """Pick the headline week from composite_strength_index() rows.

    The in-progress week is a thin sample (often 1-3 sessions) and swings the
    average for no real reason, so the headline reads the last week that is BOTH
    complete and chain-reliable. Falls back to complete-only, then to everything,
    so early data still produces a number rather than nothing.

    ``rows`` is the list the headline came from, not the raw input — so
    ``rows[-1]`` is always the headline week. Consumers plotting a sparkline
    beside the number get a series that ends where the number does.
    """
    cur_wk = datetime.fromisoformat(
        metrics.monday_of(today or datetime.now().date())).date()
    complete_reliable = [r for r in idx_rows if r[0] < cur_wk and r[3]]
    good = complete_reliable or [r for r in idx_rows if r[0] < cur_wk] or list(idx_rows)
    if not good:
        return Headline(None, 100.0, 0.0, False, None, [])
    week, index = good[-1][0], good[-1][1]
    partial = bool(idx_rows and (idx_rows[-1][0] >= cur_wk or not idx_rows[-1][3]))
    return Headline(week, index, index - 100.0, partial,
                    idx_rows[0][0] if idx_rows else None, good)


# Optional world-readable snapshot for other local apps (e.g. a wall display that
# shows the strength index), so they never need read access to the data directory.
# Point [dashboard] strength_export at a dedicated directory, e.g. one created by root:
#     sudo install -d -o youruser -g youruser -m 755 /var/lib/personal-record-public
STRENGTH_EXPORT = config.path_setting("dashboard", "strength_export")   # optional; None = off
STRENGTH_EXPORT_SCHEMA = 1


def write_strength_export(idx_rows, last, path: Path | None = STRENGTH_EXPORT) -> bool:
    """Write the strength-index snapshot atomically. Returns True on success.

    Everything in the file is final — the headline rule is applied here, so no
    consumer re-derives it. Written tmp-then-os.replace() because the reader is a
    once-a-minute cron and must never see a half-written file; os.replace is
    atomic within a filesystem.

    Never raises: the export is a side channel, and a missing directory or a
    permissions problem must not fail the dashboard build.
    """
    h = strength_headline(idx_rows)
    payload = {
        "schema": STRENGTH_EXPORT_SCHEMA,
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "asof": last.date().isoformat() if hasattr(last, "date") else str(last),
        "baseline": h.baseline.isoformat() if h.baseline else None,
        "through": h.week.isoformat() if h.week else None,
        "latest": round(h.index, 3),
        "delta_pct": round(h.delta_pct, 2),
        "partial": h.partial,
        # Headline-eligible weeks only, ascending. The in-progress week is
        # excluded by construction (strength_headline filters on < current week),
        # so a sparkline drawn from this ends exactly at `latest` rather than
        # spiking to a provisional value the headline doesn't account for.
        "weeks": [{"week": r[0].isoformat(), "index": round(r[1], 3),
                   "n_common": r[2], "reliable": bool(r[3])} for r in h.rows],
    }
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    try:
        tmp.write_text(json.dumps(payload, indent=2) + "\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
        return True
    except Exception as e:
        print(f"strength export skipped ({path}): {e}", file=sys.stderr)
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        return False


def export_strength_snapshot(path: Path | None = STRENGTH_EXPORT) -> bool:
    if path is None:
        return False
    """Load training data, compute the index, write the export. For callers that
    don't already have the data in hand (sync_hevy.py after a successful sync)."""
    d = load_training()
    return write_strength_export(composite_strength_index(d["trends"], d["last"]),
                                 d["last"], path)


NUTRITION_GLOB = "macrofactor_*_daily*.csv"   # serve_dashboard.py watches the same pattern
APPLE_HEALTH_CSV = NUTRITION_ROOT / "apple_health_daily.csv"   # nutrition_hook.py (iPhone push)
TREND_ALPHA = 0.1   # per-day EWMA weight for the scale-derived trend (MacroFactor-like smoothing)


def _mf_date(s):
    """MacroFactor dates: ISO (xlsx-converted exports) or M/D/YYYY (native CSV)."""
    s = (s or "").strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).date()
    except ValueError:
        try:
            return datetime.strptime(s, "%m/%d/%Y").date()
        except ValueError:
            return None


def load_nutrition():
    """Every MacroFactor daily export, merged by date -> weight trend + adherence.

    Changed 2026-09-18. This used to read only the newest file matching
    ``macrofactor_*_daily.csv``. The native-CSV exports from 2026-09-18 are one
    month per file (``..._daily_2026-08.csv``), so they matched neither the name
    nor the one-file assumption, and the dashboard sat on the Jul 25 export for
    two months while the health agent had already filed and analysed the new
    ones. Formats also drifted (see nutrition/README.md): ``(lb)`` vs ``(lbs)``,
    M/D/YYYY vs ISO dates, columns reordered. So: read every file by header
    name, normalise, and merge by date with the NEWEST export winning, because a
    later export restates earlier days with MacroFactor's revised model outputs.

    Trend weight on a day with no weigh-in AND no food logged is dropped. That is
    MacroFactor carrying the last value forward, not a measurement: across the
    Portugal trip it sat at 142.54-142.55 for 14 days, which plotted as "weight
    held flat" (README: "Frozen columns != flat data").

    2026-10-21: day-to-day calories/protein now arrive from Apple Health
    (APPLE_HEALTH_CSV, pushed by an iPhone Shortcut via nutrition_hook.py) and
    override the exports for any day they cover, since MacroFactor syncs edits to
    Health live. Health carries no protein target, so the last exported target is
    carried forward. Health carries no trend weight either: past the last
    exported trend day, the trend is extended with an EWMA over the HA scale's
    daily weigh-ins (body_composition.csv), seeded from MacroFactor's last trend.
    Today is excluded from adherence because it is a partial day.
    """
    files = sorted(glob.glob(str(NUTRITION_ROOT / NUTRITION_GLOB)))
    if not files and not APPLE_HEALTH_CSV.exists():
        return None

    def fnum(row, *keys):
        for key in keys:
            v = (row.get(key) or "").strip()
            if v:
                try:
                    return float(v)
                except ValueError:
                    return None
        return None

    by_day = {}
    for path in files:                 # ascending export date -> newest wins
        with open(path) as f:
            for row in csv.DictReader(f):
                d = _mf_date(row.get("Date"))
                if d is None:
                    continue
                by_day[d] = {
                    "trend": fnum(row, "Trend Weight (lbs)", "Trend Weight (lb)"),
                    "scale": fnum(row, "Weight (lbs)", "Weight (lb)"),
                    "protein": fnum(row, "Protein (g)"),
                    "protein_target": fnum(row, "Target Protein (g)"),
                    "kcal": fnum(row, "Calories (kcal)"),
                }

    blank = {"trend": None, "scale": None, "protein": None, "protein_target": None, "kcal": None}
    ah_days = set()
    if APPLE_HEALTH_CSV.exists():
        with APPLE_HEALTH_CSV.open() as f:
            for row in csv.DictReader(f):
                d = _mf_date(row.get("date"))
                if d is None:
                    continue
                kcal, prot = fnum(row, "kcal"), fnum(row, "protein_g")
                if kcal is not None or prot is not None:
                    r = by_day.setdefault(d, dict(blank))
                    r["kcal"], r["protein"] = kcal, prot
                    ah_days.add(d)

    today = datetime.now().date()
    weight_trend, protein_days, kcal_days, frozen = [], [], [], 0
    last_target = None
    for d in sorted(by_day):
        r = by_day[d]
        if r["protein_target"] is not None:
            last_target = r["protein_target"]
        logged = r["kcal"] is not None and r["kcal"] > 0
        if r["trend"] is not None:
            if r["scale"] is None and not logged:
                frozen += 1
            else:
                weight_trend.append((d, r["trend"]))
        if d >= today:
            continue                      # partial day
        target = r["protein_target"] if r["protein_target"] is not None else last_target
        if r["protein"] is not None and target is not None:
            protein_days.append((d.isoformat(), r["protein"], target))
        if logged:
            kcal_days.append((d.isoformat(), r["kcal"]))

    # Extend the trend past MacroFactor's last trend day from the HA scale.
    scale_trend_from = None
    mf_last = weight_trend[-1] if weight_trend else None
    trend = mf_last[1] if mf_last else None
    prev = mf_last[0] if mf_last else None
    for row in load_bodycomp():
        d = _mf_date(row.get("date"))
        w = fnum(row, "weight_lb")
        if d is None or w is None or (prev is not None and d <= prev):
            continue
        a = 1 - (1 - TREND_ALPHA) ** ((d - prev).days if prev else 1)
        trend = w if trend is None else trend + a * (w - trend)
        prev = d
        weight_trend.append((d, round(trend, 2)))
        scale_trend_from = scale_trend_from or d
    path = files[-1] if files else str(APPLE_HEALTH_CSV)

    # The CSV holds full history (back to 2023). Window each view to its own range:
    # weight trend to WEIGHT_WINDOW_DAYS, adherence (which must reflect *current*
    # behavior, and keep the per-day bars legible) to NUTRITION_WINDOW_DAYS.
    if weight_trend:
        wcut = weight_trend[-1][0] - timedelta(days=WEIGHT_WINDOW_DAYS)
        weight_trend = [(dt, v) for dt, v in weight_trend if dt >= wcut]
    if protein_days:
        cutoff = (datetime.fromisoformat(protein_days[-1][0]).date()
                  - timedelta(days=NUTRITION_WINDOW_DAYS))
        protein_days = [r for r in protein_days
                        if datetime.fromisoformat(r[0]).date() >= cutoff]
        kcal_days = [r for r in kcal_days
                     if datetime.fromisoformat(r[0]).date() >= cutoff]

    hits_target = sum(1 for _, p, pt in protein_days if p >= pt)
    hits_floor = sum(1 for _, p, _ in protein_days if p >= PROTEIN_FLOOR_G)
    n = len(protein_days)
    return {
        "file": (os.path.basename(path) if len(files) == 1
                 else f"{len(files)} MacroFactor exports, newest {os.path.basename(path)}"),
        "apple_health_days": len(ah_days),
        "nutrition_through": (datetime.fromisoformat(protein_days[-1][0]).date()
                              if protein_days else None),
        "scale_trend_from": scale_trend_from,
        "frozen_days_dropped": frozen,
        "weight_trend": weight_trend,
        "through": weight_trend[-1][0] if weight_trend else None,
        "protein_days": protein_days,
        "protein_n": n,
        "protein_hit_target_pct": (100 * hits_target / n) if n else None,
        "protein_hit_floor_pct": (100 * hits_floor / n) if n else None,
        "avg_protein": (sum(p for _, p, _ in protein_days) / n) if n else None,
        "avg_kcal": (sum(k for _, k in kcal_days) / len(kcal_days)) if kcal_days else None,
    }


def load_bodycomp():
    if not BODYCOMP_CSV.exists():
        return []
    with BODYCOMP_CSV.open() as f:
        return [r for r in csv.DictReader(f) if (r.get("date") or "").strip()]


# ================================================================ SVG helpers
# All charts are inline SVG. Marks: 2px lines, recessive grid, direct labels,
# native <title> tooltips (robust, no JS, CSP-safe).

def esc(s):
    return html.escape(str(s), quote=True)


def _scale(v, lo, hi, a, b):
    if hi == lo:
        return (a + b) / 2
    return a + (v - lo) / (hi - lo) * (b - a)


def nice_ticks(lo, hi, n=4):
    """Round-number gridlines (…, 140, 141, 142 / 0, 50, 100) inside [lo, hi]."""
    raw = (hi - lo) / n or 1
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    t = math.ceil(lo / step) * step
    out = []
    while t <= hi + 1e-9:
        out.append(round(t, 10))
        t += step
    return out


def line_chart(series, *, width=680, height=260, y_label="", baseline_100=False,
               ref=None, ref_label="", fmt=lambda v: f"{v:.0f}", pad_l=44, pad_r=64,
               end_fmt=None):
    """series: list of dicts {name, color(role), points:[(date, value)]}.
    Single shared y-axis (never dual). Direct end-labels + legend live outside."""
    pad_t, pad_b = 16, 26
    all_pts = [p for s in series for p in s["points"]]
    if not all_pts:
        return '<div class="empty">no data</div>'
    xs = [p[0] for p in all_pts]
    ys = [p[1] for p in all_pts]
    x0, x1 = min(xs), max(xs)
    ylo, yhi = min(ys), max(ys)
    if baseline_100:
        ylo, yhi = min(ylo, 100), max(yhi, 100)
    if ref is not None:
        ylo, yhi = min(ylo, ref), max(yhi, ref)
    span = (yhi - ylo) or 1
    ylo -= span * 0.08
    yhi += span * 0.08
    xr = ((x1 - x0).days) or 1

    def px(d):
        return _scale((d - x0).days, 0, xr, pad_l, width - pad_r)

    def py(v):
        return _scale(v, ylo, yhi, height - pad_b, pad_t)

    out = [f'<svg viewBox="0 0 {width} {height}" class="chart" '
           f'role="img" preserveAspectRatio="xMidYMid meet">',
           '<defs><pattern id="hatch" width="6" height="6" patternUnits="userSpaceOnUse" '
           'patternTransform="rotate(45)"><line x1="0" y1="0" x2="0" y2="6" class="hatchline"/>'
           '</pattern></defs>']
    # y gridlines at round numbers
    for v in nice_ticks(ylo, yhi):
        y = py(v)
        out.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width-pad_r}" y2="{y:.1f}" class="grid"/>')
        out.append(f'<text x="{pad_l-6}" y="{y+3:.1f}" class="axis" text-anchor="end">{esc(fmt(v))}</text>')
    if baseline_100:
        y = py(100)
        out.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width-pad_r}" y2="{y:.1f}" class="baseline-ref"/>')
    if ref is not None:
        y = py(ref)
        out.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width-pad_r}" y2="{y:.1f}" class="baseline-ref"/>')
        if ref_label:
            out.append(f'<text x="{width-pad_r}" y="{y+14:.1f}" class="axis reflabel" '
                       f'text-anchor="end">{esc(ref_label)}</text>')
    # x ticks (start, mid, end)
    for d in (x0, x0 + (x1 - x0) / 2, x1):
        x = px(d)
        out.append(f'<text x="{x:.1f}" y="{height-8}" class="axis" text-anchor="middle">{d.strftime("%b %-d")}</text>')
    # series
    for s in series:
        pts = sorted(s["points"], key=lambda p: p[0])
        color = f'var(--{s["color"]})'
        dpath = " ".join(f'{"M" if i==0 else "L"}{px(p[0]):.1f},{py(p[1]):.1f}' for i, p in enumerate(pts))
        # engraving: hatch the area under a lone series (two hatched areas would muddle)
        if len(series) == 1:
            out.append(f'<path d="{dpath} L{px(pts[-1][0]):.1f},{height-pad_b} '
                       f'L{px(pts[0][0]):.1f},{height-pad_b} Z" fill="url(#hatch)"/>')
        out.append(f'<path d="{dpath}" fill="none" stroke="{color}" stroke-width="2.2" '
                   f'stroke-linejoin="round" stroke-linecap="round"/>')
        stamps = len(pts) <= 40     # stamped points read on a weekly series, not a daily one
        for d, v, *rest in pts:
            note = f' ({rest[0]})' if rest and rest[0] else ''   # e.g. sample size
            out.append(f'<circle cx="{px(d):.1f}" cy="{py(v):.1f}" r="7" fill="transparent">'
                       f'<title>{esc(s["name"])} — {d.strftime("%b %-d")}: {esc(fmt(v))}'
                       f'{esc(note)}</title></circle>')
            if stamps:
                out.append(f'<circle cx="{px(d):.1f}" cy="{py(v):.1f}" r="3.2" class="stamp-pt" '
                           f'style="stroke:{color}"/>')
        # "now": the newest point in vermilion, with its value
        dl, vl = pts[-1][0], pts[-1][1]
        out.append(f'<circle cx="{px(dl):.1f}" cy="{py(vl):.1f}" r="5" class="now-pt"/>')
        out.append(f'<text x="{px(dl)+9:.1f}" y="{py(vl)+4:.1f}" class="endlabel">{esc((end_fmt or fmt)(vl))}</text>')
    out.append('</svg>')
    return "".join(out)


def sparkline(series, *, width=150, height=40):
    """Overlay one or more shape-only trend lines, each normalized to its OWN
    min-max (no shared y-axis — the dataviz no-dual-axis rule; a legend states
    each is independently scaled). series: [(values, color_role, name)]."""
    if not any(len(vals) >= 2 for vals, *_ in series):
        return '<span class="muted">—</span>'
    out = [f'<svg viewBox="0 0 {width} {height}" class="spark" preserveAspectRatio="none">']
    for vals, role, name in series:
        if len(vals) < 2:
            continue
        ylo, yhi = min(vals), max(vals)
        color = f"var(--{role})"

        def px(i, n=len(vals)):
            return _scale(i, 0, n - 1, 2, width - 2)

        def py(v, lo=ylo, hi=yhi):
            return _scale(v, lo, hi, height - 4, 4) if hi > lo else height / 2

        d = " ".join(f'{"M" if i==0 else "L"}{px(i):.1f},{py(v):.1f}' for i, v in enumerate(vals))
        out.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="2" '
                   f'stroke-linecap="round" stroke-linejoin="round" opacity="0.9"/>')
        out.append(f'<circle cx="{px(len(vals)-1):.1f}" cy="{py(vals[-1]):.1f}" r="2.5" '
                   f'fill="{color}"><title>{esc(name)}</title></circle>')
    out.append('</svg>')
    return "".join(out)


def date_sparkline(points, role, name, *, width=180, height=56, fmt=lambda v: f"{v:.1f}"):
    """Trend line for irregularly-spaced measurements: x is the DATE, not the row
    index, with the range labelled underneath.

    The body-comp series are tape measurements taken whenever Tom happens to
    measure — five in a week then nothing for a fortnight. Plotting them by index
    (what sparkline() does, correctly, for session-by-session lift data where the
    spacing is the training schedule) drew that gap as one even step and made the
    rate of change unreadable. points: [(date, value)].
    """
    pts = sorted(points, key=lambda p: p[0])
    if len(pts) < 2:
        return '<span class="muted">—</span>'
    pad_b = 13                                  # room for the two date labels
    x0, x1 = pts[0][0], pts[-1][0]
    xr = (x1 - x0).days or 1
    vals = [v for _, v in pts]
    ylo, yhi = min(vals), max(vals)
    color = f"var(--{role})"

    def px(d):
        return _scale((d - x0).days, 0, xr, 3, width - 3)

    def py(v):
        return _scale(v, ylo, yhi, height - pad_b - 4, 5) if yhi > ylo else (height - pad_b) / 2

    d = " ".join(f'{"M" if i==0 else "L"}{px(dt):.1f},{py(v):.1f}'
                 for i, (dt, v) in enumerate(pts))
    # Not preserveAspectRatio="none" like sparkline(): stretching the viewBox to the
    # card width would stretch the date labels with it.
    out = [f'<svg viewBox="0 0 {width} {height}" class="dspark" role="img" '
           f'preserveAspectRatio="xMidYMid meet">',
           f'<path d="{d}" fill="none" stroke="{color}" stroke-width="2" '
           f'stroke-linecap="round" stroke-linejoin="round" opacity="0.9"/>']
    for dt, v in pts:
        out.append(f'<circle cx="{px(dt):.1f}" cy="{py(v):.1f}" r="2" fill="{color}" '
                   f'opacity="0.75"><title>{esc(name)} — {dt:%b %-d}: '
                   f'{esc(fmt(v))}</title></circle>')
    out.append(f'<circle cx="{px(x1):.1f}" cy="{py(vals[-1]):.1f}" r="3" fill="{color}"/>')
    out.append(f'<text x="0" y="{height-2}" class="sparx">{x0:%b %-d}</text>')
    out.append(f'<text x="{width}" y="{height-2}" class="sparx" text-anchor="end">'
               f'{x1:%b %-d}</text>')
    out.append('</svg>')
    return "".join(out)


def heatmap(weeks, parts, cell, maxval, *, floors=None, ring_cell=None,
            partial_week=None):
    """weeks: list of week strings; parts: list of part names;
    cell(week, part)->int, the number displayed (effective sets).

    ``floors`` is metrics.PART_FLOORS — a per-muscle ``Floor(sets, why)``, not one
    number, and muscles absent from it carry no floor. ``ring_cell(week, part)``
    supplies the value the floor is *judged* on. The two differ on purpose: the
    cell shows effective sets (direct + indirect) but the floor is a DIRECT-set
    target, because the discount for indirect work is already baked into the floor
    (biceps 4, not 10, precisely because rows and pulldowns feed it). Scoring a
    discounted floor against an inflated count would forgive the same indirect
    volume twice — so passing ``floors`` without ``ring_cell`` is rejected rather
    than silently defaulting to ``cell``, which would BE that double-count.
    """
    if floors and ring_cell is None:
        raise ValueError("heatmap(floors=...) requires ring_cell — floors are "
                         "direct-set targets and must not be judged against the "
                         "effective-set values shown in the cells")
    judge = ring_cell or cell
    # ink tints on bone paper, light to full ink
    ramp = ["#e3dccb", "#cdc8bd", "#aeaeaf", "#86899a", "#5d6580", "#3a4562", H.INK]
    short = {"side delt": "side", "rear delt": "rear", "front delt": "front",
             "biceps": "bis", "triceps": "tris", "forearms": "fore", "shoulders": "shldr",
             "hamstrings": "hams", "quads": "quad", "glutes": "glute", "calves": "calf"}
    out = ['<table class="heat"><thead><tr><th></th>']
    for p in parts:
        out.append(f'<th>{esc(short.get(p, p[:5]))}</th>')
    out.append('</tr></thead><tbody>')
    for wk in weeks:
        # The in-progress week is a partial sum — always reads low. Mark it so it
        # isn't misread as a volume drop. It can only grow, so a floor already met
        # is final and gets its ring now; a not-yet-met one stays unmarked.
        part_wk = (wk == partial_week)
        lbl = f'{esc(wk[5:])} <span class="tnote">wip</span>' if part_wk else esc(wk[5:])
        out.append(f'<tr class="{"partial" if part_wk else ""}"><th class="wk">{lbl}</th>')
        for p in parts:
            v = cell(wk, p)
            if not v:
                out.append('<td class="z"><span>·</span></td>')
                continue
            txt = f"{v:g}" if float(v).is_integer() else f"{v:.1f}"
            idx = min(len(ramp) - 1, int(v / max(maxval, 1) * (len(ramp) - 1) + 0.5))
            pf = (floors or {}).get(p)
            direct = judge(wk, p) if pf is not None else None
            hit = pf is not None and direct >= pf.sets
            ink = H.BONE if idx >= 3 else H.INK
            ring = ' class="hit"' if hit else ""
            tip = (f"{p} — week of {wk}: {txt} effective sets"
                   f"{f' ({direct:g} direct, floor {pf.sets})' if pf is not None else ''}"
                   f"{(' (week in progress — floor already met)' if hit else ' (week in progress)') if part_wk else ''}")
            out.append(f'<td{ring}><span class="cell" style="background:{ramp[idx]};color:{ink}" '
                       f'title="{esc(tip)}">{txt}</span></td>')
        out.append('</tr>')
    out.append('</tbody></table>')
    return "".join(out)


def bars(day_vals, target, floor, *, width=680, height=150):
    """Daily protein bars vs target/floor lines. day_vals:[(date, val, tgt)]."""
    if not day_vals:
        return '<div class="empty">no data</div>'
    pad_l, pad_r, pad_t, pad_b = 40, 12, 12, 22
    ys = [v for _, v, _ in day_vals] + [floor]
    yhi = max(ys) * 1.1
    n = len(day_vals)
    bw = (width - pad_l - pad_r) / n

    def py(v):
        return _scale(v, 0, yhi, height - pad_b, pad_t)
    out = [f'<svg viewBox="0 0 {width} {height}" class="chart" preserveAspectRatio="xMidYMid meet">',
           '<defs><pattern id="hatch" width="5" height="5" patternUnits="userSpaceOnUse" '
           'patternTransform="rotate(45)"><line x1="0" y1="0" x2="0" y2="5" class="hatchline"/>'
           '</pattern></defs>']
    for v in nice_ticks(0, yhi, 3):
        y = py(v)
        out.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width-pad_r}" y2="{y:.1f}" class="grid"/>')
        out.append(f'<text x="{pad_l-6}" y="{y+3:.1f}" class="axis" text-anchor="end">{v:.0f}</text>')
    for i, (d, v, tgt) in enumerate(day_vals):
        x = pad_l + i * bw
        y = py(v)
        h = (height - pad_b) - y
        met = v >= (tgt or floor)
        col = "var(--series-2)" if met else "url(#hatch)"
        out.append(f'<rect x="{x+1:.1f}" y="{y:.1f}" width="{max(bw-2,1):.1f}" height="{max(h,0):.1f}" '
                   f'fill="{col}" class="{"" if met else "miss"}"><title>{esc(d)}: {v:.0f} g (target {tgt:.0f})</title></rect>')
    # floor line
    yf = py(floor)
    out.append(f'<line x1="{pad_l}" y1="{yf:.1f}" x2="{width-pad_r}" y2="{yf:.1f}" class="floor-line"/>')
    out.append(f'<text x="{width-pad_r}" y="{yf-4:.1f}" class="axis" text-anchor="end">floor {floor:.0f}g</text>')
    out.append('</svg>')
    return "".join(out)


# ================================================================ panels

def recent_lifts(enriched_sets, last, days=TARGETS_WINDOW_DAYS):
    """Every lift trained in the window, with its latest session's best e1RM (lb).

    Deliberately has NO min-session gate (unlike metrics.strength_trends, which
    needs ≥4 points for a meaningful regression): a load recommendation only reads
    your last session, so gating it on trend-fit quality wrongly hid lifts trained
    infrequently — e.g. RDL at 3 sessions/90d.
    """
    cutoff = last - timedelta(days=days)
    titles = {s["title"] for s in enriched_sets if s["dt"] >= cutoff}
    sess = metrics.session_best(enriched_sets, last, days)
    out = []
    for t in titles:
        bydate = sess.get(t) or {}
        latest_e = (bydate[max(bydate)]["best_e1rm"] * KG_TO_LB) if bydate else None
        out.append({"title": t, "n": len(bydate), "latest_e": latest_e})
    return out


def recent_sets_by_lift(enriched_sets):
    """{title: [(date, weight_lb, reps)]} sorted by date."""
    out = defaultdict(list)
    for s in enriched_sets:
        # Myo-rep mini-sets and drop-set continuations are logged as `dropset`
        # rows (metrics counts them at half a set for volume). They are 3-5 reps
        # at the working load by design, so feeding them to backoff_load would
        # read every myo-rep session as a rep collapse and fire the lighten
        # branch. The progression is judged on the straight sets only.
        if s["type"] == "dropset":
            continue
        out[s["title"]].append((s["date"], s["weight_kg"] * KG_TO_LB, s["reps"]))
    for t in out:
        out[t].sort(key=lambda x: x[0])   # by date only — keep within-session set order
    return out


# The shoulder / prehab title keywords moved to routine_roles (2026-10-29) so
# metrics.py can derive each lift's rep range for the strength measurement
# without importing this module. Same tuples, see the comments there.
_SHOULDER_RE = routine_roles.SHOULDER_RE
_PREHAB_RE = routine_roles.PREHAB_RE


# Largest single-session load increase the progression will ever propose. Shared
# with push_targets.MAX_UP_PCT so the dashboard and the routine writer cannot
# disagree about what counts as a runaway step.
MAX_STEP_PCT = 0.12

# Largest ABSOLUTE step up that may be taken automatically when the only option
# is the next pin on a coarse stack (see step_ok). Sized so the cable-cross pins
# (6-8 lb apart) progress on their own, while a big jump on a heavy machine still
# has to be a deliberate --set-load.
COARSE_STEP_LB = 10.0

# Loads that can actually be selected (lb), keyed by an exercise-title substring.
#
# A cable crossover pin does not move in 2.5 lb steps. Proposing 28.5 lb there
# prescribes a weight that cannot be dialled in, and an unreachable prescription
# is worse than none: it gets rounded UP to the next real pin in the gym, and the
# overshoot is then read back as "what was performed" and adopted as the standing
# target. That is exactly how the lateral raise went 26 -> 28.5 prescribed ->
# 34 performed -> 34 prescribed between 2026-08-01 and 2026-08-06.
#
# Keyed per MACHINE. An earlier version of this table keyed per exercise, on the
# belief that lifts on the Genesis cross ran different ladders (the lateral raise
# at 20/26/34/40, the overhead triceps at 23/27/30/34/40) for some unknown reason
# of "pulley setup, single vs double stack". That was wrong, and the evidence for
# it was contaminated: 23/27/30 were logged ONLY on 2026-06-03 and 06-05, which
# are the London hotel sessions -- a different gym's machine recorded against
# these template names. Tom pulls both stacks on everything here (confirmed
# 2026-08-29), and every load ever logged at his own gym is 2x a pin label.
#
# The pin labels are read off the machine. Steps run 4/3/3 repeating because it
# is a 1.5 kg stack rounded to whole pounds on the label -- which is why this
# cannot be generated as an arithmetic series. Tom logs the TOTAL across both
# stacks, so the selectable ladder is 2x each label.
GENESIS_PINS = [3, 7, 10, 13, 17, 20, 23, 27, 30, 33, 37, 40,
                43, 47, 50, 53, 57, 60, 63, 67, 70]
#
# Only list values verified from logged sets. A lift with no entry falls back to
# the smooth 2.5/5 lb ladder, which is right for plate-loaded and fine-stack
# machines and is the pre-existing behaviour.
SELECTABLE_LOADS = {
    # Matches every exercise on this machine, including ones not yet programmed.
    "(genesis dual cable cross)": [2 * p for p in GENESIS_PINS],
}


def stack_for(title):
    """Selectable loads (lb) for this exercise, or None if it uses a fine stack."""
    t = (title or "").lower()
    for name, ladder in SELECTABLE_LOADS.items():
        if name in t:
            return sorted(ladder)
    return None


def snap_up(load, ladder):
    """Lowest selectable load strictly above ``load``, or None at the top pin."""
    return next((v for v in ladder if v > load + 0.01), None)


def snap_down(load, ladder):
    """Highest selectable load <= ``load``, or the lightest pin if below the stack."""
    below = [v for v in ladder if v <= load + 0.01]
    return below[-1] if below else ladder[0]


def _round5(x):
    return int(round(x / 5.0) * 5)


def _fmt_load(x):
    """Loads to the nearest 2.5 lb, without a trailing .0 on whole numbers."""
    v = round(x * 2) / 2
    return f"{v:g}"


def min_increment(med):
    """Smallest load change that can actually be made at this weight."""
    return 2.5 if med < 50 else 5.0


def step_ok(base, proposed, title=""):
    """Is ``base`` -> ``proposed`` a sane single-session step UP?

    Three ways to pass, because a percentage cap alone is wrong at both ends of
    the load range:

    1. It is the smallest increment the gym can physically make. 2.5 lb on a
       20 lb lift is +12.5%, over the cap, but it is also the only way to move at
       all -- a bare percentage test froze every lift under ~20.8 lb permanently.
    2. It is within MAX_STEP_PCT. The ordinary case.
    3. It is the next pin on a coarse stack (MACHINE_STACKS) and the ABSOLUTE
       jump is small. A prescription between two pins has to round UP -- rounding
       down returns the load you were already using, so the lift never moves --
       but on a coarse stack that round-up routinely blows past a percentage cap:
       20 -> 26 lb is +30% and unavoidable. Percentage is the wrong lens there,
       because it cannot tell 26 -> 34 (+31%, 8 lb, fine on a lateral raise) from
       200 -> 260 (+30%, 60 lb, not fine on anything). Judge those on the jump.
    """
    if proposed <= base:
        return True
    step = proposed - base
    if step <= min_increment(base) + 0.01:
        return True
    if proposed <= base * (1 + MAX_STEP_PCT):
        return True
    return bool(stack_for(title)) and step <= COARSE_STEP_LB + 0.01


def next_load(med, title=""):
    """The load to move up to. ~5% more, snapped to a realistic gym increment,
    with a floor so light lifts actually move: at 20 lb a naive 5% rounded to the
    nearest 5 returned 20 again (no increase at all).

    On a machine with a known stack (MACHINE_STACKS) the answer is the next pin
    up instead -- on a coarse stack that IS the only available step, whatever the
    ~5% rule would prefer. Returns ``med`` unchanged at the top of the stack.
    """
    ladder = stack_for(title)
    if ladder:
        # Above the heaviest pin on record, return ``med`` unchanged. Falling
        # through to the smooth ladder here would emit 42.5 lb for a lift whose
        # stack is known to be coarse -- i.e. exactly the unreachable
        # prescription this table exists to prevent. Better to say we have run
        # out of known pins than to invent one; the caller reports it.
        return snap_up(med, ladder) or med
    inc = min_increment(med)
    step = max(inc, round(med * 0.05 / inc) * inc)
    return med + step


# Lifts whose rep_lo is a HARD safety floor: the training/programming.md *Pressing
# rule* (never below 8 reps, machine only, no deep bottom) plus RDL. This used to be
# every _SHOULDER_RE lift -- rows, pulldowns, laterals, the overhead triceps -- which
# is wider than the rule. It mattered: the lat pulldown logged 8/7/6 on 2026-08-30,
# averaged 7 against a strict floor of 8, and was cut 15% to 95 lb, then logged
# 15/13/11 and spent a month climbing back. Under the soft floor (rep_lo - 1 = 7)
# that session is a hold. "press" also catches leg press; harmless, its rep_lo is 6.
_STRICT_RE = ("press", "bench", "rdl", "romanian", "deadlift")


def is_strict_floor_lift(title):
    return any(k in title.lower() for k in _STRICT_RE)


def prev_load(med, title=""):
    """One step DOWN: the mirror of next_load. The pin below on a coarse stack,
    otherwise ~5% snapped to the gym increment. Replaces the old flat -15%, which
    over-corrected every time it fired (chest press 114 -> 95 on 2026-08-08, then
    15/12/9 at 95 and three sessions to climb back)."""
    ladder = stack_for(title)
    if ladder:
        below = [v for v in ladder if v < med - 0.01]
        return below[-1] if below else med
    inc = min_increment(med)
    step = max(inc, round(med * 0.05 / inc) * inc)
    return max(med - step, inc)


def is_shoulder_lift(title):
    """Pressing / overhead / row / pulldown / lateral / fly — kept RIR 2, no heavy
    top set, for shoulder safety."""
    return any(k in title.lower() for k in _SHOULDER_RE)


def _typical_set_count(recent, lookback=4):
    """Median count of sets AT THE WORKING LOAD per session, over the recent
    sessions for a lift.

    Counted through _session_sets, not over every row: a lighter back-off set
    (the press's 4th set since 2026-09-22) is real volume, but it is not a
    working set. Counting it made the typical count 4 while only 3 sets sit at the
    working load, so every session read as "short" and the lift could never
    graduate."""
    days = sorted({d for d, _, _ in recent})[-lookback:]
    counts = sorted(len(_session_sets(recent, d)[0]) for d in days)
    return counts[len(counts) // 2] if counts else 1


def _session_sets(recent, day):
    """(working sets on ``day`` at the session's working load, in performed order,
    that load). Sets more than 2.5 lb off the median load are a top set or a
    back-off and are not compared."""
    sets = [(w, r) for d, w, r in recent if d == day]
    if not sets:
        return [], 0
    loads = sorted(w for w, _ in sets)
    med = loads[len(loads) // 2]
    work = [(w, r) for w, r in sets if abs(w - med) < 2.5] or sets
    return work, med


def backoff_load(recent, rep_lo, rep_hi, *, strict_floor=False, title="",
                 floor=None):
    """Back-off working load (lb) via *first-set* double progression, from the
    most recent session. Returns (load|None, note, action, prev_load).

    GATE (changed 2026-09-18): graduate when the FIRST counted set reaches
    ``rep_hi`` and no counted set is under ``rep_lo``. The sets counted are the
    lift's typical count (median of the last 4 sessions), in performed order.

    Why not the old total-rep gate (total >= sets x rep_hi): it was argued as
    self-correcting against set 1 drifting past 12, and it is the opposite. Tom
    ramps RIR 2 -> 1 -> 0 and drops ~2-3 reps per set, so 3 x 12 = 36 needs set 1
    at ~14-15 -- above the 12 cap the gate was meant to protect. Replayed over
    2026-07-31 -> 09-16, every 3-set graduation had set 1 >= 13 except one, and
    the chest press (114 since 08-01), seated leg curl (100 since 08-03) and
    overhead triceps (40 since 08-05) sat in "hold" for seven weeks with the leg
    curl logging 12/10/13 = 35/36 on 08-06. Set 1 is also the standardized
    strength-index measurement (metrics.session_best), so gating on it keeps
    prescription and measurement on the same set. The all-sets >= rep_lo clause
    is what stops set 1 drifting alone: 14/7/6 does not graduate.

    SHORT SESSIONS cannot graduate. The old code computed the target from the
    sets actually performed after truncating to the typical count, so cutting a
    set lowered the bar -- the docstring claimed the reverse. On 2026-08-31 one
    set of 12 on the hack squat cleared "12/12" and wrote 283 -> 303; the next
    two sessions logged 10/10 and 8/9. Fewer sets than typical now holds.

    LIGHTEN needs two consecutive sub-floor sessions at (about) the same load,
    and steps down one increment (prev_load), not 15%. One bad session is noise
    -- the chest press logged 9/6/6 on 08-08 after 10/9/8 and 10/10/8 -- and a
    15% cut then costs three sessions of climbing back through loads that are
    far too light (95 lb: 15/12/9).

    Drop-set / myo-rep mini-set rows never reach here: recent_sets_by_lift drops
    them, so a myo-rep session is judged on its activation set alone.

    ``strict_floor`` makes ``rep_lo`` the lighten floor instead of rep_lo - 1
    (pressing rule lifts, see is_strict_floor_lift). ``floor`` overrides both --
    a high-rep role sets it well under rep_lo because Tom's third-set drop-off
    otherwise reads a good 17/11/8 light session as "too heavy".
    """
    if not recent:
        return None, "no recent data", "none", None
    days = sorted({d for d, _, _ in recent})
    last_date = days[-1]
    work, med = _session_sets(recent, last_date)
    if not work:
        return None, "no recent data", "none", None
    typical = _typical_set_count(recent)
    counted = work[:typical]
    reps = [r for _, r in counted]
    first = reps[0]
    avg = sum(reps) / len(reps)
    shape = "/".join(str(r) for r in reps)
    if med == 0:   # bodyweight — nothing to load; progress is reps only
        return 0, f"set 1 {first}/{rep_hi} ({shape})", "bodyweight", None
    floor = floor if floor is not None else (rep_lo if strict_floor else rep_lo - 1)

    if avg < floor:
        why = (f"under the {rep_lo}-rep floor" if strict_floor
               else f"too heavy for {rep_lo}–{rep_hi}")
        prev_reps, prev_med = [], 0
        if len(days) > 1:
            prev_work, prev_med = _session_sets(recent, days[-2])
            prev_reps = [r for _, r in prev_work[:typical]]
        repeat = (bool(prev_reps) and abs(prev_med - med) < 2.5
                  and sum(prev_reps) / len(prev_reps) < floor)
        if not repeat:
            return med, (f"{shape} at {_fmt_load(med)} — {why} once; holding, "
                         f"lightens if it repeats"), "hold", med
        lighter = prev_load(med, title)
        if lighter >= med:
            return med, f"{shape} at {_fmt_load(med)} — {why}, no lighter pin", "hold", med
        return lighter, f"was {_fmt_load(med)} — {why} two sessions running ({shape})", "lighten", med

    if len(work) < typical:
        return med, (f"{shape} at {_fmt_load(med)} — {len(work)} of the usual {typical} "
                     f"sets; a short session can't graduate"), "hold", med

    if first >= rep_hi and all(r >= rep_lo for r in reps):
        up = next_load(med, title)
        if up <= med:      # coarse stack, nothing heavier on record
            return med, (f"set 1 hit {first}/{rep_hi} at {_fmt_load(med)} — no "
                         f"selectable load on record above this; add the next pin "
                         f"to SELECTABLE_LOADS"), "hold", med
        # On a coarse stack the only step up can be far more than the ~5% the
        # smooth ladder assumes (26 -> 34 lb on the cable cross is +31%). Never
        # write that jump automatically; surface it for --set-load.
        if not step_ok(med, up, title):
            return med, (f"earned the next pin: {_fmt_load(up)} lb "
                         f"(+{up / med - 1:.0%}) — apply with --set-load when "
                         f"ready [{shape} at {_fmt_load(med)}]"), "hold", med
        # COARSE-STACK READINESS GATE: a jump larger than the ordinary cap needs
        # rep_hi on EVERY counted set, not just set 1 (26 x 15, 9 is not ready
        # for +31% -- that is how the lateral raise sat at 34 lb under the 8-rep
        # floor for a month). Exempt the smallest gym increment, exactly as
        # step_ok rule 1 does, or every light lift on the smooth ladder re-freezes.
        # Scoped to jumps over TWICE the cap (2026-09-18): the 40 -> 46 lb pin on
        # the overhead triceps is +15%, barely over the 12% cap, and demanding 12
        # on every set for it kept that lift at 40 lb for seven weeks on 12/8/9.
        # The gate is for 26 -> 34 (+31%), where the surplus really has to exist.
        low = min(reps)
        if (up - med > min_increment(med) + 0.01
                and up > med * (1 + 2 * MAX_STEP_PCT) and low < rep_hi):
            return med, (f"set 1 hit {first}/{rep_hi} at {_fmt_load(med)}, but the "
                         f"next pin is +{up / med - 1:.0%} — needs {rep_hi} on every "
                         f"set first (lowest was {low})"), "hold", med
        return up, f"set 1 hit {first}/{rep_hi} ({shape}) at {_fmt_load(med)}", "add_load", med

    if first < rep_hi:
        note = f"set 1 {first}/{rep_hi} ({shape}) — {rep_hi - first} to go"
    else:
        note = (f"set 1 {first}/{rep_hi} but a set under {rep_lo} ({shape}) — "
                f"bring the tail up")
    return med, note, "hold", med


def workout_targets(title, recent, latest_e1rm_lb, rep_range=None, strict=None,
                    floor=None):
    """(heavy, back, note, no_heavy, action, prev_load, reps) — action is one of
    hold / add_load / lighten / bodyweight / none, so the UI can shout when it's
    time to move the load up rather than burying it in text.

    ``rep_range``/``strict`` override the title-derived rep target for ONE slot.
    Needed because the rep target is otherwise a property of the lift's title,
    and a lift can sit in two routines at deliberately different intensities --
    the cable lateral raise runs 8-12 on the heavy day and 15-20 on the light
    one. Without an override the light slot is judged against 8-12, clears it on
    the first set, and gets promoted straight out of being light. See
    push_targets.ROUTINE_ROLES, which is the only caller that passes them.

    ``strict`` is separate from the range on purpose: the hard rep floor exists
    for shoulder safety and tracks the SAFETY floor, not whatever the day's
    target happens to be. A 15-20 light day is already well clear of it, so it
    takes the soft grace band like prehab -- making 15 a hard floor would read a
    perfectly good 14-rep set as "too heavy" and strip the load.
    """
    prehab = any(k in title.lower() for k in _PREHAB_RE)
    shoulder = is_shoulder_lift(title) or prehab
    lo, hi = rep_range or routine_roles.default_reps(title)
    # Shoulder/pressing lifts carry a hard rep floor (the pressing rule), not a
    # soft target — so no grace band before the "lighten" call. Prehab is a soft
    # 15-20 preference, so it keeps the grace band.
    # Hard floor only where the doc puts one: presses and RDL. Rows, pulldowns
    # and laterals keep the 8-12 target but take the ordinary grace band.
    hard = is_strict_floor_lift(title) if strict is None else strict
    back, note, action, prev = backoff_load(
        recent, lo, hi, strict_floor=hard, title=title, floor=floor)
    # heavy top set: perform 5-6 reps stopping at RIR ~1-2, i.e. load ≈ a 7-8RM
    # (e1RM/1.25). Not a 5-6RM to failure. Skipped on shoulder lifts (shoulder safety).
    heavy = None if (shoulder or not latest_e1rm_lb) else _round5(latest_e1rm_lb / 1.25)
    return heavy, back, note, shoulder, action, prev, (lo, hi)


def navy_bf_pct(waist_in, neck_in, height_in=HEIGHT_IN):
    """US Navy male body-fat estimate from circumferences (all inches).
    Waist term should be navel-level (use abdomen if available). Population
    regression — good for *trend* at fixed height, ~±3-4% absolute vs DEXA."""
    if not (waist_in and neck_in and waist_in > neck_in and height_in):
        return None
    return (86.010 * math.log10(waist_in - neck_in)
            - 70.041 * math.log10(height_in) + 36.76)


# ================================================================ filtering
# The by-lift panels list every lift trained in the window, which on a phone is a
# lot of scrolling to find the one you're standing in front of. Two filters cut it
# down, applied client-side so there is still exactly one build and one HTML file.

# Push / pull / legs, derived from metrics.classify() rather than a second keyword
# table — one classifier, so a lift can never be "chest" in the volume heatmap and
# "pull" here. Side delts sit on push (that's where laterals are programmed);
# forearms on pull (grip/wrist work rides with pulling days). Core, cardio and
# "other" deliberately map to none of the three: they'd be arbitrary in any bucket,
# so they show under "all" only.
PPL_BY_PART = {
    "chest": "push", "triceps": "push", "front delt": "push", "side delt": "push",
    "back": "pull", "biceps": "pull", "rear delt": "pull", "forearms": "pull",
    "quads": "legs", "hamstrings": "legs", "glutes": "legs", "calves": "legs",
}


def ppl_group(title):
    """'push' | 'pull' | 'legs' | 'other' for an exercise title."""
    return PPL_BY_PART.get(metrics.classify(title), "other")


# (value, label). The recency values are read by the inline script: "last" means
# the most recent session, "all" means no filter, anything else is a day count.
RECENCY_FILTERS = [("last", "last session"), ("14", "2 wks"), ("60", "60 d"), ("all", "all")]
PPL_FILTERS = [("push", "push"), ("pull", "pull"), ("legs", "legs"), ("all", "all")]
DEFAULT_RECENCY = "14"   # the readable default; "all" is one tap away


def filter_bar(default_recency=DEFAULT_RECENCY):
    """Two button groups + a live "N of M" count. The enclosing section carries
    data-filterable; the script matches buttons to [data-item] elements inside it."""
    out = ['<div class="filters">']
    for name, opts, default in (("recency", RECENCY_FILTERS, default_recency),
                                ("ppl", PPL_FILTERS, "all")):
        out.append(f'<div class="fgroup" data-filter="{name}" role="group">')
        for val, label in opts:
            on = "true" if val == default else "false"
            out.append(f'<button type="button" class="fbtn" data-val="{val}" '
                       f'aria-pressed="{on}">{esc(label)}</button>')
        out.append('</div>')
    out.append('<span class="fcount"></span></div>')
    return "".join(out)


def item_attrs(title, last_trained, last_session_titles, today=None):
    """data-* attributes the filter script reads off one lift's row/card.

    ``days`` is measured from TODAY, not from the last training date, because
    "active in the last 2 weeks" is a claim about the calendar — after a week off,
    windowing from the last session would still call everything current.
    """
    today = today or date.today()
    if isinstance(last_trained, str):
        last_trained = datetime.fromisoformat(last_trained).date()
    days = (today - last_trained).days
    return (f' data-item data-days="{days}" data-ppl="{ppl_group(title)}"'
            f' data-last="{1 if title in last_session_titles else 0}"')


# ================================================================ disclosure
# Every panel carries a paragraph explaining how to read it. They're the reason the
# page is long, and they're re-read maybe once. Collapse them behind a tap target.
#
# <details>/<summary> rather than a JS toggle or a hover reveal: it's a click (not
# hover, which does not exist on a phone), it survives with JS disabled, and it is
# focusable/announced by screen readers for free.

def explain(body_html, label="How to read this"):
    return (f'<details class="ex"><summary><span class="exi" aria-hidden="true">i</span>'
            f'<span>{esc(label)}</span></summary>'
            f'<div class="exb">{body_html}</div></details>')


def h2(n, title, sub=""):
    """Section heading in the manual style: a numbered section, a plain-language
    question as the title (name the information, not the picture), dotted leader."""
    sub_html = f'<span class="h2sub">{sub}</span>' if sub else ""
    return (f'<h2><span class="sn">§{n}</span><span class="ht">{title}</span>'
            f'<span class="lead"></span>{sub_html}</h2>')


def training_calendar(workouts, weeks):
    """Did I show up: a printed calendar, one row per week (Mon-Sun), a solid stamp
    on each day with a workout. Was DIP switches until 2026-10-05; Tom: switches
    read as controls to flip, not as data. Days are LOCAL dates (Hevy stores UTC;
    an evening session would otherwise land on the next day)."""
    trained = {w["start_dt"].astimezone().date() for w in workouts}
    today = date.today()
    out = ['<table class="cal"><thead><tr><th class="wk">week of</th>'
           + "".join(f'<th>{d}</th>' for d in "MTWTFSS") + '<th class="n">days</th></tr></thead><tbody>']
    for wk in weeks:
        mon = date.fromisoformat(wk)
        days = [mon + timedelta(days=i) for i in range(7)]
        n = sum(d in trained for d in days)
        cells = []
        for d in days:
            state = "fut" if d > today else ("on" if d in trained else "off")
            tip = {"on": "trained", "off": "rest", "fut": ""}[state]
            cells.append(f'<td class="{state}{" today" if d == today else ""}" title="{d:%a %b %-d}'
                         f'{": " + tip if tip else ""}"><i></i></td>')
        cur = mon <= today < mon + timedelta(days=7)
        out.append(f'<tr class="{"cur" if cur else ""}"><th class="wk">{mon:%b %-d}</th>{"".join(cells)}'
                   f'<td class="n">{f"{n} <em>so far</em>" if cur else n if n else "<span>0 · rest week</span>"}'
                   '</td></tr>')
    out.append('</tbody></table><div class="calkey"><span><i class="k on"></i>trained</span>'
               '<span><i class="k off"></i>rest</span><span><i class="k today"></i>today</span></div>')
    return "".join(out)


def card_section():
    """The weekly postcard (training/postcard.py renders it nightly), placed last
    so it's one copy away. Clipboard image writes need a secure context, so the copy
    button only appears over HTTPS; over plain HTTP the hint says right-click."""
    wk_dir = OUT_DIR / "postcards"
    pngs = sorted(wk_dir.glob("postcard-*.png")) if wk_dir.exists() else []
    if not pngs:
        return ('<section class="card">' + h2(10, "This week's postcard", "strength &amp; PRs only, safe to share")
                + '<div class="empty">No postcard yet. Run <code>python3 training/postcard.py</code>.</div></section>')
    latest = pngs[-1]
    rel = f"postcards/{latest.name}"
    past = "".join(f'<a href="postcards/{p.name}">{p.stem.replace("postcard-", "").replace("-w", " · wk ")}</a>'
                   for p in reversed(pngs[:-1][-8:]))
    return ('<section class="card share">' + h2(10, "This week's postcard", "strength &amp; PRs only, safe to share")
            + f'<div class="sharewrap"><img src="{rel}" alt="Weekly strength postcard" class="shareimg" id="shareimg">'
            '<div class="shareside">'
            '<button type="button" class="fbtn copybtn" id="copycard" hidden>Copy image</button>'
            f'<a class="fbtn" href="{rel}" download>Download PNG</a>'
            '<p class="copyhint" id="copyhint">Right-click it → <b>Copy image</b>. On a phone, long-press it.</p>'
            '<p class="copyhint">Shows strength and personal records only: no bodyweight, body comp, '
            'food or attendance.</p>'
            + (f'<div class="past"><span>Earlier weeks</span>{past}</div>' if past else "")
            + '</div></div></section>')


def stat_tile(value, label, sub="", tone=""):
    cls = f"tile {tone}".strip()
    subhtml = f'<div class="tile-sub">{sub}</div>' if sub else ""
    return (f'<div class="{cls}"><div class="tile-val">{value}</div>'
            f'<div class="tile-label">{esc(label)}</div>{subhtml}</div>')


def build_html():
    tr = load_training()
    trends = tr["trends"]
    last = tr["last"]
    first = tr["first"]
    idx_rows = composite_strength_index(trends, last)
    recent_sets = recent_sets_by_lift(tr["enriched_sets"])
    nut = load_nutrition()
    bodycomp = load_bodycomp()

    # Filter inputs (see item_attrs). "Last session" is every lift logged on the
    # most recent training DATE — Hevy can split one gym trip across two workout
    # records, and a date is the unit a person means by "my last session".
    last_day = max(s["date"] for s in tr["enriched_sets"])
    last_session_titles = {s["title"] for s in tr["enriched_sets"] if s["date"] == last_day}
    last_trained = {t: max(d for d, _, _ in v) for t, v in recent_sets.items()}

    # ---- hero stats
    # Headline selection lives in strength_headline() so the JSON export and any
    # display reading it can't drift from what's rendered here.
    _hl = strength_headline(idx_rows)
    latest_idx, idx_delta, idx_partial = _hl.index, _hl.delta_pct, _hl.partial
    n_lifts = len({r["title"] for r in trends})
    rising = sum(1 for r in trends if r["e_wk"] > 0)
    total_vol_wk = sum(r["v_wk"] for r in trends)

    # RPE coverage. NOT an e1RM input — e1RM is raw-reps only (see metrics.py).
    # What RPE buys is auditing the RIR system in training/programming.md: tier ceilings,
    # effort ramp, and the junk-volume rule are all stated in RIR and were
    # unverifiable while only 2.4% of sets carried one. Recent window — it's a
    # habit indicator, not a lifetime stat.
    _rpe_cut = last - timedelta(days=RPE_WINDOW_DAYS)
    _rec = [s for s in tr["enriched_sets"] if s["dt"] >= _rpe_cut]
    _rpe = [s for s in _rec
            if str(s.get("rpe") or "").strip() not in ("", "None", "0")]
    rpe_pct = (100 * len(_rpe) / len(_rec)) if _rec else None

    parts_order = ["chest", "back", "quads", "hamstrings", "glutes", "calves",
                   "biceps", "triceps", "forearms",
                   "side delt", "rear delt", "front delt", "core"]
    wps = metrics.weekly_effective_sets(tr["enriched_sets"])
    wps_direct = metrics.weekly_part_sets(tr["enriched_sets"])
    weeks12 = sorted({k[0] for k in wps})[-12:]
    maxcell = max((wps.get((w, p), 0) for w in weeks12 for p in parts_order), default=1)

    wk_counts, _ = metrics.weekly_workout_counts(tr["workouts"])

    P = []  # page fragments
    _wk = date.today().isocalendar()[1]
    P.append('<header class="head">'
             '<div class="mast"><h1>Personal Record<em>Mk.I</em></h1>'
             f'<div class="meta">Operating manual &middot; <b>p. {_wk} / 52</b><br>'
             f'data through {last.date():%b %-d, %Y}</div></div>'
             f'<p class="sub"><span>Fat&nbsp;↓ / muscle&nbsp;↑ at a flat <b>{TARGET_WEIGHT_LB:.0f} lb</b></span>'
             f'<span>generated {GENERATED}</span></p></header>')

    # ---- method note (answers the e1RM complaint)
    P.append(
        '<section class="card note">' + explain(
        '<p>Hevy surfaces your <b>record e1RM</b> — the single best estimate you\'ve ever hit, '
        'almost always from a heavy low-rep day. It only ticks up when you beat that PR, so on a '
        'hypertrophy program (8–15 reps) it sits still even while you\'re adding reps and load. '
        'Same Epley math per set here — the fix is the <b>aggregation</b>: a <b>strength index</b> '
        'that trend-fits <i>every</i> session\'s best set (each lift normalized to 100, then '
        'averaged), plus <b>volume-load</b> (weight × reps). A 10×100→12×100 or 12×100→12×110 jump '
        'lifts both. Strength trending up while bodyweight holds flat is the recomp signal — tape, '
        'photos, and periodic DEXA fill in the rest.</p>'
        '<p>Each lift is shown as an <b>e<i>N</i>RM</b> — the load you could do for <i>N</i> reps, '
        '<i>N</i> = the middle of that lift\'s rep range — so a 15–20 rep lateral reads as a weight '
        'you\'d recognise, not a 1RM extrapolated from 18 reps. Epley is linear, so the choice of '
        '<i>N</i> changes the number shown but never a trend or the index. '
        f'Two accuracy guards: reps past the top of the lift\'s range + {metrics.E1RM_CAP_MARGIN} '
        'count as range top + 3 — a <b>floor on capacity, not an estimate</b> — because Epley gets '
        'unreliable far from where you train and you\'re past the point of adding load anyway; and '
        'reps are always <b>raw</b>, never RPE-adjusted. Feeding RPE into the estimate was tried and removed: logging '
        'began Jul 24, so adjusting only the logged sets put a fake +2.5% step in the series at that '
        'boundary rather than measuring a strength change. <b>RPE still matters</b> — it\'s what makes '
        'the RIR targets in training/programming.md auditable — it just doesn\'t feed this number.</p>',
        label="How progress is measured here") + '</section>')

    # ---- HERO: strength index
    hero_series = [{"name": "Strength index", "color": C_STRENGTH,
                    "points": [(d, v, f"{n} common lift{'' if n == 1 else 's'}"
                                + ("" if ok else " · provisional"))
                               for d, v, n, ok in idx_rows]}]
    _base = _hl.baseline
    P.append('<section class="card hero-card">')
    P.append(h2(1, "Am I getting stronger?", "all lifts, like-for-like"))
    P.append('<div class="hero"><svg class="sun" viewBox="0 0 640 260" aria-hidden="true">'
             + H.sunburst(320, 250, 235, 150, length=100, n=44) + '</svg>'
             f'<div class="big">{idx_delta:+.1f}%</div>'
             f'<div class="biglbl">stronger since {_base:%B %-d}</div>'
             f'<div class="bigsub">{rising} of {len(trends)} lifts trending up'
             + (' · through the last full week' if idx_partial else '') + '</div></div>')
    P.append('<div class="tiles">')
    P.append(stat_tile(f'{n_lifts}', 'lifts tracked', 'auto-detected, ≥4 sessions'))
    P.append(stat_tile(f'{total_vol_wk:+,.0f}', 'volume-load lb/wk', 'net across all lifts',
                       'good' if total_vol_wk > 0 else 'crit'))
    if rpe_pct is not None:
        rtone = "good" if rpe_pct >= 70 else ("warn" if rpe_pct >= 25 else "muted")
        rsub = ('RIR targets auditable' if rpe_pct >= 70 else
                ('log more → audit RIR' if rpe_pct >= 25 else 'log RPE to audit RIR'))
        P.append(stat_tile(f'{rpe_pct:.0f}%', f'sets w/ RPE · {RPE_WINDOW_DAYS}d', rsub, rtone))
    P.append('</div>')
    # Plotted as % gained (index - 100): "+29.5%" is a number a person can feel,
    # "index 129.5" is not. Same series, same chain-linking.
    pct_series = [{**hero_series[0], "points": [(d, v - 100, *rest)
                                                for d, v, *rest in hero_series[0]["points"]]}]
    P.append(f'<div class="legend"><span class="lg"><i style="background:var(--{C_STRENGTH})"></i>'
             f'strength gained, week by week (0% = week of {_base:%b %-d})</span></div>')
    P.append(line_chart(pct_series, ref=0, ref_label="starting point", fmt=lambda v: f'{v:+.0f}%',
                         end_fmt=lambda v: f'{v:+.1f}%'))
    P.append(explain('<p><b>Chain-linked</b>: each week\'s change is measured only across lifts '
             'trained in <i>both</i> that week and the previous one, then multiplied through — the way '
             'price indices handle a changing basket. A plain average was dominated by <i>which</i> '
             'lifts you happened to train: your Chest Press sits near 186 (it nearly doubled), so any '
             'week including it got yanked up, and weeks that skipped the weak lifts read high. '
             'Like-for-like, Jul 20→27 was <b>+2.7</b>; the old naive average showed −3.8. The index is '
             'rooted at the first week with real week-to-week overlap (earlier weeks were a ramp-back-up '
             'with ~2 lifts repeated, too thin to chain). Hover any point for the overlap count; weeks '
             'marked <i>provisional</i> have thin overlap. '
             'Each lift\'s <b>first two sessions ever</b> are left out: a new exercise runs at RIR 2–3 '
             'with a feel-out load, so its second week reads as a huge fake gain (the Hip Thrust\'s 66 lb '
             'opener once moved this headline from +32% to +54% overnight). '
             'Built from the <b>first loaded working set</b> of each session — a fixed position, so '
             'sessions stay comparable. (Fixed <i>position within the exercise</i>, note: where that '
             'exercise sat in the session is unrecoverable from Hevy, so this is a consistency '
             'guarantee, not a "least fatigued" one.) Sets are counted at '
             f'<b>{metrics.E1RM_REP_FLOOR}+ reps</b>, capped at each lift\'s range top + '
             f'{metrics.E1RM_CAP_MARGIN}. A first set <b>over</b> the cap is recorded '
             'at the cap rather than skipped: it used to fall '
             'through to set 2, a fatigued back-off set, which fired on 38% of sessions after the '
             'Jul 21 move to 8–12 rep work and read climbing reps-at-fixed-load as <i>decline</i>. '
             'Skipping those sessions instead would censor on the outcome — they occur exactly when '
             'progress is showing — so they\'re kept as lower bounds. The final point is the '
             'in-progress week (thin sample); the headline reads the last complete week. '
             'Pair with the weight panel below: rising here + flat there = muscle gained.</p>',
             label="What the strength index is"))
    P.append('</section>')

    # ---- Working targets (glanceable during a workout), grouped by muscle
    P.append('<section class="card target-card" data-filterable>'
             + h2(2, "What to lift next session", "patch sheet · glance while lifting · lb"))
    P.append(filter_bar())
    P.append(explain(
        '<div class="keyrow">'
        '<span><span class="up-badge">↑ ADD LOAD</span> you earned the range — go up</span>'
        '<span><b>hold</b> same weight, add reps</span>'
        '<span><span class="dn-badge">↓ LIGHTEN</span> too heavy for the rep range</span>'
        '<span><b>BW</b> bodyweight — add reps</span></div>', label="Key"))
    P.append('<div style="overflow-x:auto"><table class="targets"><thead><tr>'
             '<th>lift</th><th>next load <span>working sets</span></th>'
             '</tr></thead><tbody>')
    cur = None
    title_tids = defaultdict(set)
    for e in tr["exercises"]:
        title_tids[e["title"]].add(e["exercise_template_id"])
    for r in sorted(recent_lifts(tr["enriched_sets"], last, DASH_TARGETS_DAYS),
                    key=lambda r: (metrics.classify(r["title"]), r["title"])):
        part = metrics.classify(r["title"])
        if part != cur:
            cur = part
            P.append(f'<tr class="grp" data-grp><td colspan="2">{esc(part)}</td></tr>')
        # Apply a routine role's rep range where it is unambiguous (the home DB
        # lifts). Without it the dashboard judged the home lateral raise against
        # the title default 8-12 and said ADD LOAD while push_targets, using the
        # role's 12-20, held -- the two disagreed on the first home session.
        role = next((rr for rr in (routine_roles.single_role(tid)
                                   for tid in title_tids.get(r["title"], ()))
                     if rr), None)
        # The heavy 5-6 top-set column was removed 2026-09-22: the program has no
        # top sets (2-3 working sets in the lift's range, programming.md), and it
        # was suggesting ~7-8RM singles on lifts like the home skull crusher.
        _heavy, back, note, _shoulder, action, prev, (lo, hi) = workout_targets(
            r["title"], recent_sets[r["title"]], r["latest_e"],
            rep_range=role["reps"] if role else None,
            strict=role["strict"] if role else None,
            floor=role.get("floor") if role else None)
        # The load cell shouts the ACTION: a load increase reads as "prev → new"
        # with an ↑ badge, so it can't be mistaken for "same weight again".
        if action == "add_load":
            b = (f'<span class="up-badge">↑ ADD LOAD</span><br>'
                 f'<span class="was">{_fmt_load(prev)}</span> → <b class="up">{_fmt_load(back)}</b>'
                 f'<span class="tnote"> × {lo} · {esc(note)}</span>')
        elif action == "lighten":
            b = (f'<span class="dn-badge">↓ LIGHTEN</span><br>'
                 f'<span class="was">{_fmt_load(prev)}</span> → <b class="warn">{_fmt_load(back)}</b>'
                 f'<span class="tnote"> × {lo}–{hi} · {esc(note)}</span>')
        elif action == "bodyweight":
            b = f'<b>BW</b> <span class="tnote">{esc(note)}</span>'
        elif action == "hold":
            b = (f'<b>{_fmt_load(back)}</b> <span class="tnote">hold · {esc(note)}</span>')
        else:
            b = '<span class="muted">—</span>'
        P.append(f'<tr class="{"levelup" if action == "add_load" else ""}"'
                 f'{item_attrs(r["title"], last_trained[r["title"]], last_session_titles)}>'
                 f'<td class="tlift">{esc(r["title"])}</td>'
                 f'<td class="w">{b}</td></tr>')
    P.append('</tbody></table>')
    P.append('<div class="fnone" hidden>No lifts match these filters — widen the '
             'window or tap <b>all</b>.</div></div>')
    P.append(explain('<p><b>Next load</b> = the weight for your working sets next time, judged in each lift\'s own rep range: 8–12 for most lifts, 15–20 for corrective work, 12–20 / 10–15 for the home dumbbell lifts. There are no heavy top sets in this program. Pressing and RDL also carry a hard 8-rep floor (training/programming.md\'s pressing rule: machine only, no deep bottom position; terminal RIR 0–1 is fine there, dropping below 8 reps is not). '
             '<b>Progression = first set:</b> <i>set 1 11/12 (11/9/8)</i> = your first working set hit 11 '
             'of the 12 that graduates you, with the whole session in brackets. Load moves when set 1 '
             'reaches the top of the range <i>and</i> no counted set is under the bottom of it — a green '
             '<b>↑ ADD LOAD</b> row means you cleared it, and shows the <s>old</s> → <b>new</b> weight to '
             'put on (~5% up, snapped to a real gym increment: 2.5 lb under 50, 5 lb above). Adding reps '
             '<i>or</i> load both build muscle '
             '(<a href="https://pubmed.ncbi.nlm.nih.gov/36199287/">Plotkin 2022</a> — similar hypertrophy '
             'either way). Set 1 is the gate because it is also the strength-index measurement, and '
             'because with a RIR 2→1→0 ramp the old sets×12 total needed set 1 at 14–15 to clear. A short '
             'session (fewer sets than usual) holds; two sub-floor sessions in a row step the load down one '
             'increment. Myo-rep mini-sets (drop-set rows) don\'t count here — only the activation set. '
             'Corrective work (face pulls etc.) targets 15–20 reps light — never heavier.</p>',
             label="How these loads are picked"))
    P.append('</section>')

    # ---- Strength by lift (small multiples)
    P.append('<section class="card" data-filterable>'
             + h2(3, "How is each lift trending?", f"last {WINDOW_DAYS} days"))
    P.append(filter_bar())
    P.append('<div class="legend">'
             f'<span class="lg"><i style="background:var(--{C_STRENGTH})"></i>est. N-rep max</span> '
             f'<span class="lg"><i style="background:var(--{C_WEIGHT})"></i>volume-load</span> '
             '<span class="muted">— each line normalized to its own range; shape = direction, '
             'not shared scale</span></div>')
    P.append(explain('<p class="key">Two lines per lift, session by session: <b>e<i>N</i>RM</b> '
             '(first working set\'s estimated max for <i>N</i> reps, <i>N</i> = middle of the lift\'s '
             'rep range — the overload/strength proxy) and <b>volume-load</b> (session '
             'weight×reps across working sets — the total-work / stimulus metric). A rep or load '
             'PR at <i>any</i> rep range lifts them, so progress shows without a heavy single. When '
             'they diverge — e<i>N</i>RM flat but volume-load rising, or vice-versa — that\'s the '
             'interesting case. Under each:</p>'
             '<div class="keyrow">'
             '<span><b class="good">▲ lb/wk</b> trend (regression slope)</span>'
             f'<span><b>Δ</b> change over the {WINDOW_DAYS}d</span>'
             '<span><b>e<i>N</i>RM</b> latest, for <i>N</i> reps</span>'
             '<span><b>vol</b> volume-load trend (wt×reps)</span>'
             '<span><b>r²</b> fit quality — low = noisy, read loosely</span></div>',
             label="What the two lines are"))
    P.append('<div class="grid-lifts">')
    for r in trends:
        # e1RM -> e{N}RM is a constant factor (Epley is linear), so only the
        # shown lb values scale; pct, r² and the arrow's sign are unchanged.
        n_ref = r["ref_reps"]
        to_n = 1 / (1 + n_ref / 30)
        e_wk_n = r["e_wk"] * to_n
        arrow = "▲" if e_wk_n > 0.1 else ("▼" if e_wk_n < -0.1 else "—")
        atone = "good" if e_wk_n > 0.1 else ("crit" if e_wk_n < -0.1 else "muted")
        pct_cls = "good" if r["pct"] > 0 else "crit" if r["pct"] < 0 else "muted"
        # r² < 0.3 = noisy fit; flag so the trend isn't over-read
        r2_cls = "muted" if r["r2"] >= 0.3 else "faded"
        r2_tip = "clean trend" if r["r2"] >= 0.5 else ("moderate" if r["r2"] >= 0.3 else "noisy — read loosely")
        P.append(
            f'<div class="lift"'
            f'{item_attrs(r["base_title"], max(r["dates"]).date(), last_session_titles)}>'
            f'<div class="lift-top"><span class="lift-name">{esc(r["title"])}</span>'
            f'<span class="lift-n">{r["n"]} sess</span></div>'
            f'{sparkline([([v * to_n for v in r["e1_series"]], C_STRENGTH, f"e{n_ref}RM"), (r["vol_series"], C_WEIGHT, "volume-load")])}'
            f'<div class="lift-stats">'
            f'<span class="{atone}" title="trend: regression slope of first-set e{n_ref}RM">{arrow} {e_wk_n:+.1f} lb/wk</span>'
            f'<span class="{pct_cls}" title="change from first to last session in window">Δ {r["pct"]:+.0f}%</span>'
            f'<span class="muted" title="most recent session: estimated max for {n_ref} reps">e{n_ref}RM {r["latest_e"] * to_n:.0f} lb</span>'
            f'<span class="muted" title="volume-load (weight×reps) trend per week">vol {r["v_wk"]:+.0f}/wk</span>'
            f'<span class="{r2_cls}" title="fit quality (r²): {r2_tip}">r² {r["r2"]:.2f}</span>'
            f'</div></div>')
    P.append('</div>')
    P.append('<div class="fnone" hidden>No lifts match these filters — widen the window or tap '
             '<b>all</b>. (This panel needs ≥4 sessions in the last '
             f'{WINDOW_DAYS} days to trend a lift.)</div>')
    P.append(explain('<p>(Working weights are in the <b>Working targets</b> panel above — this is '
             'the progress view.) <b>How to read the lines:</b> ▲ = stronger at your rep ranges — keep '
             'going; e<i>N</i>RM flat/▼ while volume-load rises = you trained lighter/higher-rep, not weaker. '
             'Low-r² (faded) lines are noisy — read loosely. This is <i>not</i> Hevy\'s heaviest-ever '
             '"1RM PR"; it\'s the trend across every session, so it moves with real hypertrophy work.</p>',
             label="How to read the lines"))
    P.append('</section>')

    # ---- Volume by muscle vs floor
    P.append('<section class="card">'
             + h2(4, "Is each muscle getting enough work?", "effective sets per week · last 12 weeks"))
    P.append('<div class="legend"><span class="lg"><i class="ringkey"></i>ringed = met that '
             'muscle\'s weekly floor of direct sets (wip week: rings as soon as it\'s met)</span> <span class="muted">· darker = more sets · '
             'hover a cell for its floor</span></div>')
    P.append('<div class="scrollx">')
    P.append(heatmap(weeks12, parts_order, lambda w, p: wps.get((w, p), 0), maxcell,
                     floors=metrics.PART_FLOORS,
                     ring_cell=lambda w, p: wps_direct.get((w, p), 0),
                     partial_week=metrics.monday_of(datetime.now().date())))
    P.append('</div>')
    P.append(explain('<p><b>Effective sets = direct + indirect.</b> A press adds 0.5 to arms '
             '(triceps) and 0.5 to front delt; a row/pulldown adds 0.5 biceps '
             '+ 0.25 rear delt; <b>hinges add 0.5 core</b> (RDL, back extension — spinal '
             'erectors) and free squats/lunges 0.25, while <b>supported leg work adds none</b> '
             '(hack squat, leg press/curl/extension brace your trunk for you); isolation work '
             '(laterals, pec deck) adds nothing. <b>Arms are split bis / tris / forearms and delts '
             'by head</b> — they\'re separate muscles with separate floors, and the split is what '
             'makes indirect credit coherent (a press works triceps, not biceps; a row works biceps, '
             'not triceps — one merged "arms" bucket implied both). Wrist work is broken out because '
             'it was inflating arms. '
             'Counting direct sets only understated your arms badly — they read ~7/wk direct but '
             '~14 effective. The 0.5 is a <b>convention, not a measured value</b> (evidence supports '
             'the direction: compound-only programs grow arms less than compound + direct work), so '
             'read these as directional — and the <b>core</b> credit is weaker still, since the trunk '
             'works isometrically in a hinge rather than as a prime mover, and this bucket lumps abs '
             '(your cable crunches) with erectors (RDL / back extension). '
             '<b>Legs are split quads / hams / glutes / calves</b> — as one bucket they read ~5 sets/wk '
             '(mildly low) while hiding quads 2.7, hamstrings 2.8, glutes 0.2 and <b>calves 0.0</b>; a '
             'floor on the sum can\'t surface that. Hip hinges (RDL, back extension) count as '
             'hamstrings — hamstring is the prime mover in loaded hip extension — but they aren\'t '
             'knee flexion, and only ~0.8 sets/wk of the hamstring total is leg-curl work. '
             '<b>The green ring is judged on DIRECT sets against a per-muscle floor</b> '
             '(chest/back 10, quads 8, side delt 8, hams/calves/tris/rear delt 6, glutes 4, bis 4) — '
             'not on the effective number shown in the cell. The floors already discount for indirect '
             'work (biceps is 4 <i>because</i> rows feed it), so scoring them against effective sets '
             'would forgive that volume twice. Forearms and core carry no floor: with Versa Gripps on '
             'pulls, direct wrist work is the only forearm stimulus there is, but forearm size isn\'t '
             'a stated goal. The current week is marked '
             '<span class="tnote">wip</span> and left un-ringed: it\'s a partial sum, not a drop.</p>',
             label="What an effective set is") + '</section>')

    # ---- Weight panel
    P.append('<section class="card">'
             + h2(5, "Is my weight holding steady?", f"trend weight · last {WEIGHT_WINDOW_DAYS} days"))
    if nut and nut["weight_trend"]:
        wseries = [{"name": "Trend weight", "color": C_WEIGHT, "points": nut["weight_trend"]}]
        latest_w = nut["weight_trend"][-1][1]
        stale_days = (datetime.now().date() - nut["through"]).days
        if stale_days > 10:
            P.append(f'<div class="banner warn">⚠ Weight data is stale — the last weigh-in '
                     f'was <b>{nut["through"]:%b %-d, %Y}</b> ({stale_days} days ago). Check the '
                     f'Etekcity scale in Home Assistant (sync_scale.py).</div>')
        P.append('<div class="tiles">')
        P.append(stat_tile(f'{latest_w:.1f}', 'lb · last trend', f'target {TARGET_WEIGHT_LB:.0f} lb'))
        P.append('</div>')
        P.append(line_chart(wseries, ref=TARGET_WEIGHT_LB,
                             ref_label=f'target {TARGET_WEIGHT_LB:.0f}', fmt=lambda v: f'{v:.1f}'))
        _src = (f' From {nut["scale_trend_from"]:%b %-d} on, the trend is a smoothed average of the '
                f'smart-scale weigh-ins (MacroFactor exports don\'t reach past that).'
                if nut.get("scale_trend_from") else '')
        P.append(explain(f'<p>Target band is {TARGET_WEIGHT_LB:.0f} lb (settings.toml). Recomp = hold this flat while strength climbs.{_src}</p>',
                 label="What the target line is"))
    else:
        P.append('<div class="empty">No weight data — add a MacroFactor daily export to '
                 '<code>nutrition/</code>.</div>')
    P.append('</section>')

    # ---- Nutrition adherence
    P.append('<section class="card">'
             + h2(6, "Am I eating enough protein?", f"last {NUTRITION_WINDOW_DAYS} days"))
    if nut and nut["protein_n"]:
        pf = nut["protein_hit_floor_pct"]
        tone = "good" if pf >= 80 else ("warn" if pf >= 50 else "crit")
        nthru = nut["nutrition_through"]
        nstale = (datetime.now().date() - nthru).days
        if nstale > 3:
            P.append(f'<div class="banner warn">⚠ Nutrition data stops at <b>{nthru:%b %-d}</b> '
                     f'({nstale} days ago). Run the “Health → Mini” Shortcut on the iPhone.</div>')
        P.append(f'<div class="banner">Through {nthru:%b %-d, %Y} · '
                 f'{nut["protein_n"]} logged days (last {NUTRITION_WINDOW_DAYS}d)</div>')
        P.append('<div class="tiles">')
        P.append(stat_tile(f'{pf:.0f}%', f'days ≥ {PROTEIN_FLOOR_G:.0f}g protein',
                           'the binding constraint (training/programming.md)', tone))
        P.append(stat_tile(f'{nut["avg_protein"]:.0f}g', 'avg protein', 'average hides low days'))
        if nut["avg_kcal"]:
            P.append(stat_tile(f'{nut["avg_kcal"]:.0f}', 'avg kcal',
                               f'~{MAINTENANCE_KCAL:.0f} maintenance'))
        P.append('</div>')
        days = [(d, p, pt) for d, p, pt in nut["protein_days"]]
        P.append('<div class="legend"><span class="lg"><i style="background:var(--series-2)"></i>'
                 'floor met</span> <span class="lg"><i class="hatchkey"></i>under the floor</span></div>')
        P.append(bars(days, None, PROTEIN_FLOOR_G))
        P.append(explain('<p>Protein is an <b>every-day</b> floor, not an average — missing it '
                 'in a recomp costs muscle directly (Helms 2014). Bars ≥ floor are green.</p>',
                 label="Why a floor, not an average"))
    else:
        P.append('<div class="empty">No nutrition data.</div>')
    P.append('</section>')

    # ---- Body composition (Renpho tape + scale, synced via sync_scale.py + sync_tape.py from HA)
    P.append('<section class="card">'
             + h2(7, "Is my body changing?", "smart tape · scale · DEXA"))
    # (col, short label). Only columns with data are shown.
    bc_cols = [("weight_lb", "weight"), ("bf_pct", "bf%"), ("waist_in", "waist"),
               ("abdomen_in", "abdomen"), ("chest_in", "chest"), ("arm_l_in", "L arm"),
               ("arm_r_in", "R arm"), ("thigh_l_in", "L thigh"), ("thigh_r_in", "R thigh"),
               ("calf_l_in", "L calf"), ("calf_r_in", "R calf"), ("neck_in", "neck"),
               ("shoulder_in", "shoulder")]
    if bodycomp:
        present = [(c, lbl) for c, lbl in bc_cols if any((r.get(c) or "").strip() for r in bodycomp)]
        # headline recomp site: navel/abdomen is the most fat-responsive spot for
        # men + the Navy waist term, so prefer it; fall back to natural-waist.
        _has = lambda col: any((r.get(col) or "").strip() for r in bodycomp)
        recomp_col = "abdomen_in" if _has("abdomen_in") else "waist_in"
        recomp_lbl = "waist (navel)" if recomp_col == "abdomen_in" else "waist"
        waist = [(r["date"], float(r[recomp_col])) for r in bodycomp if (r.get(recomp_col) or "").strip()]
        # Navy BF% est from a navel-level waist (prefer abdomen) + a FIXED neck: the median of
        # every neck reading. Tom stopped taping the neck on 2026-10-31: it's hard to measure
        # well, and its tape noise (14.4-15.2 in) moved the estimate more than real change did
        # (~2 pt per inch). With the neck fixed, BF% simply tracks the abdomen.
        necks = sorted(float(r["neck_in"]) for r in bodycomp if (r.get("neck_in") or "").strip())
        neck_fixed = necks[len(necks) // 2] if necks else None
        navy = []
        for r in bodycomp:
            w = (r.get("abdomen_in") or "").strip() or (r.get("waist_in") or "").strip()
            if neck_fixed and w:
                bf = navy_bf_pct(float(w), neck_fixed)
                if bf:
                    navy.append((r["date"], bf))
        P.append('<div class="tiles">')
        if waist:
            latest = waist[-1][1]
            sub = "in · waist"
            tone = ""
            if len(waist) > 1:
                dw = latest - waist[0][1]
                sub = f'in · {recomp_lbl} ({dw:+.1f} since {waist[0][0][5:]})'
                tone = "good" if dw < 0 else ("crit" if dw > 0 else "")
            P.append(stat_tile(f'{latest:.1f}', recomp_lbl, sub, tone))
        if navy:
            lb = navy[-1][1]
            sub = f"Navy est (neck fixed {neck_fixed:.1f} in)"
            tone = ""
            if len(navy) > 1:
                db = lb - navy[0][1]
                sub = f'Navy est · {db:+.1f} pt since {navy[0][0][5:]} · neck fixed {neck_fixed:.1f}'
                tone = "good" if db < 0 else ("crit" if db > 0 else "")
            P.append(stat_tile(f'{lb:.1f}%', 'body fat', sub, tone))
        P.append(stat_tile(f'{len(bodycomp)}', 'measurement days', 'auto-synced from tape + scale'))
        P.append('</div>')
        # ---- tracked-metric trend small-multiples (different scales -> small multiples,
        # not one overlaid axis). Direction-aware: abdomen/BF ↓ = good, limbs ↑ = good.
        tracked = [("abdomen_in", "Abdomen", "in", "lower"), ("neck_in", "Neck", "in", "neutral"),
                   ("chest_in", "Chest", "in", "higher"), ("arm_l_in", "Bicep L", "in", "higher"),
                   ("thigh_l_in", "Quad L", "in", "higher"), ("calf_l_in", "Calf L", "in", "higher"),
                   ("__navy__", "Body fat", "%", "lower")]
        P.append('<div class="legend"><span class="muted">trends — abdomen &amp; body-fat ↓ good, '
                 'limbs ↑ good (green); x-axis is the measurement date, so gaps between '
                 'measurements show as gaps</span></div>')
        P.append('<div class="grid-lifts">')
        for col, label, unit, dirn in tracked:
            # Measurements are irregular, so these plot on a DATE axis (date_sparkline)
            # rather than by row index like the per-lift sparklines above.
            series = [(datetime.fromisoformat(d).date(), v)
                      for d, v in (navy if col == "__navy__"
                                   else [(r["date"], float(r[col])) for r in bodycomp
                                         if (r.get(col) or "").strip()])]
            series.sort()
            vals = [v for _, v in series]
            u = "%" if unit == "%" else '"'
            if not vals:
                inner = ('<div class="dspark"></div><div class="lift-stats">'
                         '<span class="muted">no data — measure to start</span></div>')
            elif len(vals) < 2:
                inner = (f'<div class="dspark"></div><div class="lift-stats">'
                         f'<span class="muted">now {vals[-1]:.1f}{u}</span>'
                         f'<span class="muted">{series[-1][0]:%b %-d} · need ≥2 pts</span></div>')
            else:
                d = vals[-1] - vals[0]
                tone = ("muted" if dirn == "neutral" or abs(d) < 1e-9
                        else ("good" if ((d < 0) == (dirn == "lower")) else "crit"))
                arrow = "▲" if d > 0 else ("▼" if d < 0 else "—")
                inner = (f'{date_sparkline(series, C_STRENGTH, label)}'
                         f'<div class="lift-stats">'
                         f'<span class="muted">now {vals[-1]:.1f}{u}</span>'
                         f'<span class="{tone}">{arrow} {d:+.1f}{u}</span></div>')
            P.append(f'<div class="lift"><div class="lift-top"><span class="lift-name">{esc(label)}</span>'
                     f'<span class="lift-n">{len(vals)} pt</span></div>{inner}</div>')
        P.append('</div>')
        # table: newest first, only populated columns (all values in inches unless weight/bf%)
        P.append('<div style="overflow-x:auto"><table class="mini"><thead><tr><th>date</th>'
                 + "".join(f'<th>{esc(lbl)}</th>' for _, lbl in present) + '<th>src</th></tr></thead><tbody>')
        for r in reversed(bodycomp[-12:]):
            P.append('<tr><td>' + esc(r["date"]) + '</td>'
                     + "".join(f'<td>{esc((r.get(c) or "").strip() or "—")}</td>' for c, _ in present)
                     + f'<td>{esc((r.get("source") or "").replace("renpho-",""))}</td></tr>')
        P.append('</tbody></table></div>')
        P.append(explain('<p>Circumferences in inches (Renpho tape, cm→in); weight in lb. '
                 '<b>Abdomen (at the navel) at flat bodyweight is the cleanest recomp signal</b> — it\'s '
                 'the most fat-responsive spot for men, so ↓ at flat weight = fat down. '
                 '<b>Body fat %</b> is the <b>US Navy estimate</b> from that navel measurement + a '
                 f'<b>fixed neck</b> (median of past readings; neck is no longer taped) at {HEIGHT_IN:.0f}" height — a validated regression, best for <i>trend</i> (~±3–4% '
                 'absolute vs DEXA); cross-check its direction against your BIA scale. '
                 '<b>Where to measure:</b> <i>abdomen</i> = around the navel; <i>waist</i> = the '
                 'narrowest point (≈midway between lowest rib and hip bone, often ~1" above the navel). '
                 'Same landmarks each time, relaxed, normal exhale, tape level, don\'t suck in. '
                 'Re-sync: <code>.venv/bin/python sync_renpho.py</code>.</p>',
                 label="Where to measure & what BF% means"))
    else:
        P.append('<div class="empty">No body-comp data yet. Run '
                 '<code>.venv/bin/python sync_renpho.py</code> to pull from Renpho.</div>')
    P.append('</section>')

    # ---- Consistency
    # ---- Cardio (training/cardio_panel.py; data from training/sync_cardio.py)
    P.append(cardio_panel.section(8, h2=h2, explain=explain, esc=esc,
                                  date_sparkline=date_sparkline, stat_tile=stat_tile))

    P.append('<section class="card">' + h2(9, "Did I show up?", "training days · last 12 weeks"))
    # 12 CONSECUTIVE weeks ending this one: weeks12 above only holds weeks that had
    # sets, which silently dropped the zero weeks this panel exists to show.
    _mon = date.today() - timedelta(days=date.today().weekday())
    cal_weeks = [(_mon - timedelta(weeks=i)).isoformat() for i in range(11, -1, -1)]
    P.append(training_calendar(tr["workouts"], cal_weeks))
    zero = [w for w in weeks12 if wk_counts.get(w, 0) == 0]
    if zero:
        P.append(f'<p class="cap">Zero-workout weeks: {", ".join(w[5:] for w in zero)}.</p>')
    P.append('</section>')

    P.append(card_section())
    P.append(f'<footer class="foot"><span>Regenerate: <code>python3 training/build_dashboard.py</code> · '
             f'metrics from Hevy sync + MacroFactor export · no data fabricated.</span>'
             + (f'<span class="press">{COLOPHON}</span>' if COLOPHON else '') + '</footer>')

    return "".join(P)


# ================================================================ assemble

STYLE = """
<style>
/* House style, RECOMP Mk.I: a printed field manual with deco bones. Light only:
   "sunlit paper" is the identity (2026-10-05). Tokens mirror training/house.py.
   Vermilion means "now" or "record" and nothing else; declines are oxide, and
   warnings are brass. */
.viz-root{--bone:#f3ecdc;--deep:#e8dec8;--ink:#1f2a44;--ink-soft:#4a5470;--vermilion:#e2683c;
--sage:#5f8f7a;--brass:#b8913f;
--surface-1:#f6f0e3;--plane:var(--deep);--text-primary:var(--ink);--text-secondary:var(--ink-soft);
--muted:#6f7486;--grid:rgba(31,42,68,.13);--baseline:rgba(31,42,68,.45);--border:rgba(31,42,68,.2);
--series-1:var(--ink);--series-2:var(--sage);--good:#3d7560;--warn:#8f6b1f;--crit:#a3402a;
--deco:'Poiret One',Georgia,serif;--mono:'IBM Plex Mono',ui-monospace,monospace;
color-scheme:light;color:var(--ink);font-family:var(--mono);font-size:15px;line-height:1.55;
max-width:960px;margin:0 auto;padding:28px 16px 40px}
body{margin:0;background:#f3ecdc;background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='240' height='240'><filter id='n'><feTurbulence type='fractalNoise' baseFrequency='.85' numOctaves='2' stitchTiles='stitch'/><feColorMatrix values='0 0 0 0 .12  0 0 0 0 .16  0 0 0 0 .26  0 0 0 .09 0'/></filter><rect width='100%' height='100%' filter='url(%23n)'/></svg>")}
.viz-root *{box-sizing:border-box}
.viz-root [hidden]{display:none!important}
.viz-root a{color:var(--ink)}
.deco-shadow{text-shadow:1.2px .8px 0 rgba(226,104,60,.35)}

/* masthead */
.head{border:2.2px solid var(--ink);outline:.8px solid var(--ink);outline-offset:-8px;padding:22px 24px 14px;
margin-bottom:8px}
.mast{display:flex;justify-content:space-between;align-items:flex-end;gap:12px;flex-wrap:wrap;
border-bottom:2px solid var(--ink);padding-bottom:10px}
.head h1{margin:0;font:400 clamp(38px,8vw,60px)/1 var(--deco);letter-spacing:.14em;text-transform:uppercase;
text-shadow:1.2px .8px 0 rgba(226,104,60,.35)}
.head h1 em{font-style:normal;color:var(--vermilion);font-size:.5em;letter-spacing:.08em;margin-left:6px;text-shadow:none}
.meta{text-align:right;font-size:.78rem;letter-spacing:.1em;text-transform:uppercase;color:var(--ink-soft);line-height:1.6}
.meta b{color:var(--ink);font-weight:600}
.sub{margin:0;padding-top:6px;display:flex;justify-content:space-between;flex-wrap:wrap;gap:4px 12px;
font-size:.74rem;letter-spacing:.14em;text-transform:uppercase;color:var(--ink-soft)}
.sub b{color:var(--ink)}

/* sections: ruled pages, not floating cards */
.card{background:none;border:0;border-top:2px solid var(--ink);padding:14px 0 6px;margin:38px 0 0;position:relative}
.card:before{content:"";position:absolute;left:0;right:0;top:3px;border-top:.8px solid var(--ink)}
.card h2{display:flex;align-items:baseline;flex-wrap:wrap;gap:4px 12px;margin:6px 0 14px;font-weight:400}
.card h2 .sn{font:400 1.25rem var(--deco);color:var(--vermilion);letter-spacing:.06em}
.card h2 .ht{font:400 clamp(1.25rem,3.4vw,1.6rem)/1.15 var(--deco);letter-spacing:.11em;text-transform:uppercase;
text-shadow:1.2px .8px 0 rgba(226,104,60,.3)}
.card h2 .lead{flex:1 1 40px;border-bottom:.8px dotted var(--ink);min-width:30px;transform:translateY(-4px)}
.h2sub{font:italic 400 .8rem var(--mono);color:var(--ink-soft);letter-spacing:0;text-transform:none}
.note{border-top:0;margin-top:14px;padding-top:0}
.note:before{display:none}
.note p{margin:0;color:var(--text-secondary);font-size:.86rem}
.key{margin:0 0 8px;color:var(--text-secondary);font-size:.82rem}
.keyrow{display:flex;flex-wrap:wrap;gap:4px 16px;margin:0 0 12px;font-size:.76rem;color:var(--muted)}
.keyrow b{color:var(--text-secondary);font-variant-numeric:tabular-nums}
.keyrow b.good{color:var(--good)}

/* hero */
.hero{position:relative;height:250px;text-align:center;overflow:hidden;margin:-6px 0 6px}
.hero .sun{position:absolute;left:50%;top:-10px;width:640px;max-width:none;transform:translateX(-50%)}
.hero .big{position:absolute;left:0;right:0;top:96px;font:600 clamp(56px,13vw,92px)/1 var(--mono);letter-spacing:-.02em}
.hero .biglbl{position:absolute;left:0;right:0;top:196px;font:400 1.3rem var(--deco);letter-spacing:.14em;
text-transform:uppercase;text-shadow:1.2px .8px 0 rgba(226,104,60,.35)}
.hero .bigsub{position:absolute;left:0;right:0;top:226px;font-size:.78rem;color:var(--ink-soft);font-style:italic}
@media (max-width:560px){.hero{height:210px}.hero .sun{top:-40px}.hero .big{top:70px}
.hero .biglbl{top:150px;font-size:1.05rem}.hero .bigsub{top:180px}}

/* readout tiles */
.tiles{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:12px}
.tile{flex:1 1 140px;background:rgba(232,222,200,.6);border:1.2px solid var(--ink);border-radius:5px;padding:9px 12px 8px}
.tile-val{font-size:1.6rem;font-weight:600;letter-spacing:-.01em;font-variant-numeric:tabular-nums;line-height:1.2}
.tile-label{font:400 .9rem var(--deco);letter-spacing:.12em;text-transform:uppercase;color:var(--ink)}
.tile-sub{font-size:.7rem;color:var(--muted);margin-top:2px}
.tile.good .tile-val{color:var(--good)} .tile.crit .tile-val{color:var(--crit)}
.tile.warn .tile-val{color:var(--warn)}

/* charts: engraved */
.chart{width:100%;height:auto;display:block;overflow:visible}
.grid{stroke:var(--grid);stroke-width:1;stroke-dasharray:1 3}
.baseline-ref{stroke:var(--sage);stroke-width:1.3;stroke-dasharray:6 4}
.reflabel{fill:var(--sage)!important}
.floor-line{stroke:var(--ink);stroke-width:1.3;stroke-dasharray:6 4}
.hatchline{stroke:var(--ink);stroke-opacity:.22;stroke-width:1.2}
.stamp-pt{fill:var(--bone);stroke-width:1.6}
.now-pt{fill:var(--vermilion)}
.miss{stroke:var(--ink);stroke-opacity:.5;stroke-width:.8}
.axis{paint-order:stroke;stroke:#f3ecdc;stroke-width:3px;stroke-linejoin:round;fill:var(--muted);font-size:10.5px;font-family:var(--mono);font-variant-numeric:tabular-nums}
.endlabel{font:600 12px var(--mono);fill:var(--vermilion);font-variant-numeric:tabular-nums}
.legend{margin:4px 0 6px;font-size:.78rem;color:var(--text-secondary)}
.lg{margin-right:10px;white-space:nowrap}
.lg i{display:inline-block;width:12px;height:12px;border-radius:2px;vertical-align:-1px;margin-right:5px}
.lg i.ringkey{background:var(--deep);box-shadow:inset 0 0 0 2px var(--sage)}
.lg i.hatchkey{background:repeating-linear-gradient(45deg,rgba(31,42,68,.45) 0 1.2px,transparent 1.2px 4px);
outline:.8px solid rgba(31,42,68,.5)}
.cap{font-size:.78rem;color:var(--muted);margin:8px 0 0}

/* working targets: a pedal-manual patch sheet */
.targets{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
.targets th{text-align:left;font-size:.66rem;color:var(--muted);font-weight:500;padding:2px 10px 6px;
text-transform:uppercase;letter-spacing:.12em;border-bottom:1.4px solid var(--ink)}
.targets th span{font-weight:400;text-transform:none;letter-spacing:0;font-style:italic}
.targets td{padding:9px 10px;border-top:.8px dotted rgba(31,42,68,.35)}
.targets .tlift{font-weight:500;font-size:.88rem}
.targets .w{font-size:1.15rem;font-weight:600;white-space:nowrap}
.targets .w b{color:var(--text-primary)}
.targets .w .muted{font-size:.85rem;font-weight:400}
.targets .tnote{font-size:.66rem;color:var(--muted);font-weight:400}
.targets tr.grp td{border-top:0;padding:16px 10px 3px;font:400 .95rem var(--deco);text-transform:uppercase;
letter-spacing:.16em;color:var(--ink)}
.targets tr.levelup td{background:rgba(95,143,122,.1)}
.targets tr.levelup td:first-child{box-shadow:inset 3px 0 0 var(--sage)}
.up-badge,.dn-badge{display:inline-block;font:600 .62rem var(--mono);letter-spacing:.1em;padding:0 5px;
margin-bottom:3px;white-space:nowrap;background:none;border:1.4px solid;transform:rotate(-3deg)}
.up-badge{color:var(--good);border-color:var(--sage)}
.dn-badge{color:var(--warn);border-color:var(--brass)}
.targets .up{color:var(--good)}
.targets .was{color:var(--muted);text-decoration:line-through;text-decoration-color:var(--vermilion);font-weight:400;font-size:.85rem}

/* small multiples */
.grid-lifts{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:0;
border-top:.8px solid var(--ink);border-left:.8px solid var(--ink)}
.lift{background:none;border-right:.8px solid var(--ink);border-bottom:.8px solid var(--ink);padding:10px 12px}
.lift-top{display:flex;justify-content:space-between;align-items:baseline;gap:6px}
.lift-name{font:400 .95rem var(--deco);letter-spacing:.1em;text-transform:uppercase;overflow:hidden;
text-overflow:ellipsis;white-space:nowrap}
.lift-n{font-size:.68rem;color:var(--muted);flex-shrink:0}
.spark{width:100%;height:40px;display:block;margin:4px 0}
.lift-stats{display:flex;flex-wrap:wrap;gap:2px 10px;font-size:.7rem;font-variant-numeric:tabular-nums}
.good{color:var(--good)} .crit{color:var(--crit)} .warn{color:var(--warn)} .muted{color:var(--muted)}
.faded{color:var(--muted);opacity:.55}
.rec{margin-top:6px;padding-top:6px;border-top:.8px dotted var(--border);font-size:.72rem;color:var(--text-secondary)}
.rec.good{color:var(--text-secondary)} .rec.good b{color:var(--good)}
.rec.warn b{color:var(--warn)}

/* muscle grid */
.heat{border-collapse:separate;border-spacing:3px;width:100%;font-variant-numeric:tabular-nums}
.heat th{font-size:.7rem;color:var(--muted);font-weight:500;text-transform:uppercase;letter-spacing:.06em}
.heat th.wk{text-align:right;padding-right:4px;white-space:nowrap;text-transform:none;letter-spacing:0}
.heat td{text-align:center;border-radius:3px}
.heat td.hit .cell{outline:2.6px solid var(--sage);outline-offset:-2.6px}
.heat .cell{display:block;border-radius:2px;padding:5px 0;font-size:.74rem;font-weight:600}
.heat td.z span{color:var(--muted)}
.heat tr.partial .cell{opacity:.55}
.heat tr.partial td.hit .cell{opacity:1}
.heat tr.partial .wk{opacity:.7}

.banner{background:rgba(232,222,200,.6);border:.8px solid var(--border);border-radius:4px;padding:8px 10px;
font-size:.78rem;color:var(--text-secondary);margin-bottom:10px}
.banner.warn{border:1.4px solid var(--brass);color:var(--text-primary)}
.empty{border:1px dashed var(--baseline);border-radius:4px;padding:20px;text-align:center;color:var(--muted);font-size:.85rem}
.empty.awaiting{text-align:left;line-height:1.6}

/* did I show up: a printed calendar, stamped */
.cal{border-collapse:collapse;font-variant-numeric:tabular-nums}
.cal td:not(.n),.cal thead th:not(.wk):not(.n){width:34px}
.cal th{font-size:.68rem;color:var(--muted);font-weight:500;padding:0 0 6px;text-align:center}
.cal th.wk{text-align:left;font-weight:400;padding-right:14px;white-space:nowrap;color:var(--ink-soft)}
.cal thead th.wk{text-transform:uppercase;letter-spacing:.08em;font-size:.62rem}
.cal td{text-align:center;padding:4px 0;border-top:.8px dotted rgba(31,42,68,.25)}
.cal tbody th{border-top:.8px dotted rgba(31,42,68,.25);padding:4px 14px 4px 0;font-size:.74rem}
.cal td i{display:inline-block;border-radius:50%;vertical-align:middle}
.cal td.on i{width:15px;height:15px;background:var(--ink)}
.cal td.off i{width:4px;height:4px;background:rgba(31,42,68,.3)}
.cal td.today i{box-shadow:0 0 0 2px var(--bone),0 0 0 3.6px var(--vermilion)}
.cal td.fut.today i,.cal td.off.today i{width:9px;height:9px;background:none;border:1.5px solid var(--vermilion);box-shadow:none}
.cal td.n{text-align:right;font-weight:600;font-size:.82rem;padding-left:10px;white-space:nowrap}
.cal td.n span{font-weight:400;color:var(--crit);font-style:italic;font-size:.74rem}
.cal td.n em{font-weight:400;color:var(--muted);font-size:.7rem}
.cal tr.cur th,.cal tr.cur td{background:rgba(232,222,200,.7)}
.calkey{display:flex;gap:16px;font-size:.72rem;color:var(--muted);margin-top:10px}
.calkey .k{display:inline-block;border-radius:50%;vertical-align:middle;margin-right:6px}
.calkey .k.on{width:12px;height:12px;background:var(--ink)}
.calkey .k.off{width:4px;height:4px;background:rgba(31,42,68,.3)}
.calkey .k.today{width:9px;height:9px;border:1.5px solid var(--vermilion)}

.mini{width:100%;border-collapse:collapse;font-size:.76rem;font-variant-numeric:tabular-nums}
.mini th,.mini td{text-align:left;padding:4px 8px;border-bottom:.8px dotted var(--border)}
.mini th{font-weight:500;text-transform:uppercase;letter-spacing:.08em;font-size:.66rem;color:var(--muted);
border-bottom:1.4px solid var(--ink)}
code{background:rgba(232,222,200,.8);padding:1px 5px;border-radius:2px;font-size:.85em;font-family:var(--mono)}
.foot{display:flex;justify-content:space-between;flex-wrap:wrap;gap:6px 16px;color:var(--muted);font-size:.72rem;
margin:34px 0 4px;padding-top:10px;border-top:.8px solid var(--ink)}
.foot .press{letter-spacing:.16em;text-transform:uppercase}

/* filters: toggles with an LED */
.filters{display:flex;flex-wrap:wrap;align-items:center;gap:6px 10px;margin:0 0 12px}
.fgroup{display:flex;flex-wrap:wrap;gap:2px;background:var(--ink);border-radius:5px;padding:3px}
.fbtn{appearance:none;-webkit-appearance:none;border:0;background:transparent;font:500 .74rem var(--mono);
letter-spacing:.04em;color:var(--deep);padding:7px 11px;min-height:32px;border-radius:3px;cursor:pointer;
white-space:nowrap;-webkit-tap-highlight-color:transparent;display:inline-flex;align-items:center;gap:6px;
text-decoration:none}
.fbtn:before{content:"";width:6px;height:6px;border-radius:50%;background:rgba(243,236,220,.25);flex:none}
.fbtn[aria-pressed="true"]{background:var(--bone);color:var(--ink)}
.fbtn[aria-pressed="true"]:before{background:var(--vermilion);box-shadow:0 0 5px var(--vermilion)}
.fcount{font-size:.72rem;color:var(--muted);font-variant-numeric:tabular-nums;margin-left:auto}
.fnone{color:var(--muted);font-size:.82rem;text-align:center;padding:18px 6px}

/* explainers: fold-out manual pages */
.ex{margin:12px 0 0}
.ex>summary{list-style:none;display:inline-flex;align-items:center;gap:8px;cursor:pointer;
font:400 .9rem var(--deco);letter-spacing:.1em;text-transform:uppercase;color:var(--ink);
border-bottom:.8px dotted var(--ink);padding:4px 2px;min-height:32px;-webkit-tap-highlight-color:transparent}
.ex>summary::-webkit-details-marker{display:none}
.ex>summary::marker{content:""}
.ex>summary:after{content:"+";font-family:var(--mono);color:var(--vermilion);font-weight:600}
.ex[open]>summary:after{content:"−"}
.ex[open]>summary{margin-bottom:8px}
.exi{display:inline-flex;align-items:center;justify-content:center;width:20px;height:20px;flex-shrink:0;
border:1.2px solid var(--ink);border-radius:50%;color:var(--ink);font:italic 600 .72rem var(--mono);line-height:1}
.exb{font-size:.8rem;color:var(--ink-soft);background:rgba(232,222,200,.55);border-left:2.5px solid var(--vermilion);
padding:10px 14px}
.exb>p{margin:0 0 8px}
.exb>p:last-child{margin-bottom:0}
.exb .keyrow{margin-bottom:0}
.note .ex{margin:0}
.dspark{width:100%;display:block;margin:4px 0}
svg.dspark{height:auto}
div.dspark{height:52px}
.sparx{fill:var(--muted);font-size:9px;font-family:var(--mono);font-variant-numeric:tabular-nums}

/* this week's card */
.sharewrap{display:grid;grid-template-columns:minmax(0,1.25fr) minmax(0,1fr);gap:24px;align-items:start}
.shareimg{width:100%;height:auto;border:1px solid var(--border);box-shadow:0 10px 30px rgba(31,42,68,.18);display:block}
.shareside{display:flex;flex-direction:column;gap:10px;align-items:flex-start}
.shareside .fbtn{background:var(--ink);color:var(--bone);border-radius:4px;padding:9px 14px}
.copybtn:before{background:var(--vermilion)!important;box-shadow:0 0 5px var(--vermilion)}
.copyhint{margin:0;font-size:.8rem;color:var(--ink-soft)}
.past{display:flex;flex-wrap:wrap;gap:4px 12px;font-size:.76rem;padding-top:8px;border-top:.8px dotted var(--border);width:100%}
.past span{width:100%;font:400 .85rem var(--deco);letter-spacing:.12em;text-transform:uppercase}
@media (max-width:720px){.sharewrap{grid-template-columns:1fr}}
.scrollx{overflow-x:auto;-webkit-overflow-scrolling:touch}
.scrollx .heat{min-width:560px}
/* phone, mid-workout: the load is what matters, so let its note wrap under it */
@media (max-width:600px){.targets .w{white-space:normal}.targets .tnote{display:block;line-height:1.4}
.targets .tlift{width:42%;font-size:.82rem}.targets td{padding:8px 6px}}
</style>
"""

# The page is otherwise pure HTML+SVG (no JS by design — it renders the same served
# over HTTP or opened as a plain file). Filtering is the one thing that has to be
# interactive: it's driven by data-* attributes emitted at build time, so with JS off
# every row simply stays visible and nothing else changes.
SCRIPT = """
<script>
(function () {
  var scopes = document.querySelectorAll('[data-filterable]');
  function apply(scope) {
    var f = {};
    scope.querySelectorAll('.fgroup').forEach(function (g) {
      var on = g.querySelector('.fbtn[aria-pressed="true"]');
      f[g.getAttribute('data-filter')] = on ? on.getAttribute('data-val') : 'all';
    });
    var items = scope.querySelectorAll('[data-item]'), shown = 0;
    items.forEach(function (el) {
      var ok = true, r = f.recency || 'all';
      if (r === 'last') ok = el.getAttribute('data-last') === '1';
      else if (r !== 'all') ok = parseInt(el.getAttribute('data-days'), 10) <= parseInt(r, 10);
      if (ok && f.ppl && f.ppl !== 'all') ok = el.getAttribute('data-ppl') === f.ppl;
      el.hidden = !ok;
      if (ok) shown++;
    });
    // A muscle heading with nothing left under it is noise — hide it too.
    var grp = null, any = false;
    scope.querySelectorAll('tr[data-grp], tr[data-item]').forEach(function (tr) {
      if (tr.hasAttribute('data-grp')) {
        if (grp) grp.hidden = !any;
        grp = tr; any = false;
      } else if (!tr.hidden) any = true;
    });
    if (grp) grp.hidden = !any;
    var c = scope.querySelector('.fcount');
    if (c) c.textContent = shown + ' of ' + items.length;
    var none = scope.querySelector('.fnone');
    if (none) none.hidden = shown !== 0;
  }
  // Postcard: an image copy needs a secure context (HTTPS). Over plain HTTP the
  // button stays hidden and the right-click hint is the instruction.
  var cb = document.getElementById('copycard'), img = document.getElementById('shareimg');
  if (cb && img && window.isSecureContext && navigator.clipboard && window.ClipboardItem) {
    cb.hidden = false;
    document.getElementById('copyhint').hidden = true;
    cb.addEventListener('click', function () {
      var blob = fetch(img.src).then(function (r) { return r.blob(); });
      navigator.clipboard.write([new ClipboardItem({'image/png': blob})]).then(function () {
        cb.textContent = 'Copied ✓';
        setTimeout(function () { cb.textContent = 'Copy image'; }, 1800);
      }, function () { cb.textContent = 'Copy failed: right-click instead'; });
    });
  }
  scopes.forEach(function (scope) {
    scope.querySelectorAll('.fbtn').forEach(function (b) {
      b.addEventListener('click', function () {
        b.parentNode.querySelectorAll('.fbtn').forEach(function (o) {
          o.setAttribute('aria-pressed', o === b ? 'true' : 'false');
        });
        apply(scope);
      });
    });
    apply(scope);
  });
})();
</script>
"""


def main():
    # Script inside .viz-root, after the markup it queries — so no DOMContentLoaded
    # wrapper is needed and the whole dashboard stays one self-contained div.
    inner = f'<div class="viz-root">{build_html()}{SCRIPT}</div>'
    # Republish the snapshot from the same data that just rendered the page, so
    # the export can never be staler than the dashboard. Best-effort by design.
    if export_strength_snapshot():
        print(f"wrote {STRENGTH_EXPORT}")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # The page loads its fonts from ./fonts next to it; ship them with the output.
    fonts = OUT_DIR / "fonts"
    if not fonts.exists():
        import shutil
        shutil.copytree(config.ASSETS / "fonts", fonts)
    full = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>Personal Record</title><style>' + H.font_faces("fonts/") + '</style>' + STYLE
            + '<style>' + cardio_panel.STYLE + '</style>' +
            '</head><body>' + inner + '</body></html>')
    (OUT_DIR / "recomp.html").write_text(full)
    print(f"wrote {OUT_DIR/'recomp.html'}")


if __name__ == "__main__":
    main()
