#!/usr/bin/env python3
"""Ad-hoc analysis for coaching report. Stdlib only.

Metric logic lives in ``metrics.py`` (shared with build_dashboard.py) so the
text report and the dashboard never drift. This file is just the presentation
layer for the terminal.
"""
from __future__ import annotations
from collections import Counter, defaultdict
from datetime import datetime, timedelta

import metrics
from metrics import KG_TO_LB, classify, linreg, monday_of

data = metrics.load_all()
workouts = data["workouts"]
exercises = data["exercises"]
enriched_sets = data["enriched_sets"]
date_to_workout = data["date_to_workout"]
first = data["first"]
last = data["last"]

# --- basic range ---
print(f"\n=== BASIC ===")
print(f"First: {first.date()}  Last: {last.date()}  Span: {(last - first).days} days")
print(f"Total workouts: {len(workouts)}")

# --- workouts per week, last 20 weeks ---
print(f"\n=== WORKOUTS PER WEEK (last 20 weeks from {last.date()}) ===")
week_counts, week_types = metrics.weekly_workout_counts(workouts)
sorted_weeks = sorted(week_counts.keys(), reverse=True)[:20]
for wk in reversed(sorted_weeks):
    print(f"  {wk}: {week_counts[wk]}  — {', '.join(week_types[wk])}")

# --- workout title frequency ---
print(f"\n=== WORKOUT TYPES (last 90 days) ===")
cutoff_90 = last - timedelta(days=90)
titles_90 = Counter(w["title"] for w in workouts if w["start_dt"] >= cutoff_90)
for title, n in titles_90.most_common(15):
    print(f"  {n:3d}  {title}")

# --- exercise frequency, last 90 days ---
print(f"\n=== TOP EXERCISES (last 90 days) ===")
recent_workout_ids = {w["id"] for w in workouts if w["start_dt"] >= cutoff_90}
ex_counts = Counter()
for e in exercises:
    if e["workout_id"] in recent_workout_ids:
        ex_counts[e["title"]] += 1
for title, n in ex_counts.most_common(20):
    print(f"  {n:3d}  {title}")

# --- PR / top-set tracking for key compounds ---
print(f"\n=== PROGRESSIVE OVERLOAD: key compounds ===")
key_lifts = [
    "Bench Press (Dumbbell)",
    "Bench Press (Barbell)",
    "Squat (Barbell)",
    "Squat (Dumbbell)",
    "Deadlift (Barbell)",
    "Overhead Press (Barbell)",
    "Overhead Press (Dumbbell)",
    "Shoulder Press (Dumbbell)",
    "Bent Over Row (Barbell)",
    "Lat Pulldown (Cable)",
    "Pull Up",
    "Chin Up",
    "Incline Bench Press (Dumbbell)",
]

for lift in key_lifts:
    # bodyweight sets carry no e1RM — exclude them from this load-based section
    lift_sets = [s for s in enriched_sets
                 if s["title"] == lift and not s.get("bodyweight")]
    if len(lift_sets) < 3:
        continue
    all_time_best = max(lift_sets, key=lambda s: s["one_rm_est"])
    cutoff_8 = last - timedelta(days=56)
    recent = [s for s in lift_sets if s["dt"] >= cutoff_8]
    cutoff_16 = last - timedelta(days=112)
    prior = [s for s in lift_sets if cutoff_16 <= s["dt"] < cutoff_8]
    recent_best = max(recent, key=lambda s: s["one_rm_est"]) if recent else None
    prior_best = max(prior, key=lambda s: s["one_rm_est"]) if prior else None

    print(f"\n  {lift}  (n={len(lift_sets)} working sets over all time)")
    print(f"    all-time best e1RM: {all_time_best['one_rm_est']:.1f}kg "
          f"({all_time_best['weight_kg']}kg × {all_time_best['reps']}  on {all_time_best['date']})")
    if recent_best:
        print(f"    last 8 weeks best:  {recent_best['one_rm_est']:.1f}kg "
              f"({recent_best['weight_kg']}kg × {recent_best['reps']}  on {recent_best['date']})  "
              f"n={len(recent)} sessions")
    else:
        print(f"    last 8 weeks:  NOT TRAINED")
    if prior_best:
        print(f"    prior 8 weeks best: {prior_best['one_rm_est']:.1f}kg "
              f"({prior_best['weight_kg']}kg × {prior_best['reps']}  on {prior_best['date']})")
    if recent_best and prior_best:
        delta = recent_best["one_rm_est"] - prior_best["one_rm_est"]
        pct = delta / prior_best["one_rm_est"] * 100
        print(f"    Δ:  {delta:+.1f}kg  ({pct:+.1f}%)")

# --- strength trend: est-1RM regression per exercise (auto-detected) ---
# Fixes the two problems with eyeballing Hevy's per-session e1RM:
#   (1) it's noisy set-to-set — a linear regression over the window averages that out,
#       so it reads real progress at ANY rep range (not just heavy low-rep sets);
#   (2) no hardcoded lift list — picks up whatever Tom actually trains.
TREND_WINDOW_DAYS = 90
MIN_SESSIONS = 4

trend_rows = metrics.strength_trends(enriched_sets, last, TREND_WINDOW_DAYS, MIN_SESSIONS)

print(f"\n=== STRENGTH TREND — est-1RM regression (last {TREND_WINDOW_DAYS}d, ≥{MIN_SESSIONS} sessions) ===")
print("  Slope = lb/week from a linear fit of each session's best e1RM (Epley).")
print(f"  e1RM = the session's FIRST working set, on raw reps, only when that set is "
      f"≥{metrics.E1RM_REP_FLOOR} reps, reps capped at the lift's range top +{metrics.E1RM_CAP_MARGIN}.")
print("  + = strengthening. r² = fit quality (low r² = noisy/flat, read the trend loosely).")
print(f"  {'exercise':32} {'sess':>4} {'e1RM':>7} {'trend':>11} {'Δwin':>7} {'r²':>5}")
for r in trend_rows:
    print(f"  {r['title'][:32]:32} {r['n']:>4} {r['latest_e']:>5.0f}lb "
          f"{r['e_wk']:>+7.1f}/wk {r['pct']:>+6.1f}% {r['r2']:>5.2f}")

print(f"\n=== VOLUME-LOAD TREND (working sets, lb×reps, last {TREND_WINDOW_DAYS}d) ===")
print(f"  {'exercise':32} {'latest VL':>11} {'trend':>13}")
for r in trend_rows:
    print(f"  {r['title'][:32]:32} {r['latest_vol']:>8.0f}lb {r['v_wk']:>+9.0f}/wk")

# --- volume by body part (rough mapping) ---
print(f"\n=== WEEKLY VOLUME BY BODY PART (last 12 weeks) ===")
weekly_part_sets = metrics.weekly_part_sets(enriched_sets)

all_parts = ["chest", "back", "quads", "hamstrings", "glutes", "calves",
             "biceps", "triceps", "forearms",
             "side delt", "rear delt", "front delt", "core", "other"]
sorted_weeks_all = sorted({k[0] for k in weekly_part_sets.keys()})[-12:]
header = f"  {'week':12}  " + "  ".join(f"{p[:5]:>5}" for p in all_parts)
print(header)
for wk in sorted_weeks_all:
    row = f"  {wk}  " + "  ".join(f"{weekly_part_sets.get((wk, p), 0):5g}" for p in all_parts)
    print(row)

# Direct sets vs the per-muscle floor. The flat 10-set floor is gone: see
# metrics.PART_FLOORS for why each number differs.
#
# Denominator is CALENDAR weeks, not "weeks that contain a logged set". A fully
# untrained week is absent from weekly_part_sets entirely, so averaging over its
# keys silently drops it from the denominator and inflates every figure — the 12
# keyed weeks in current data actually span 13 calendar weeks (~8% high), and a
# 3-week layoff would inflate by 25% while looking like an unbroken run.
# The in-progress week is excluded too: it's a partial sum that drags the mean
# down, which is why the dashboard leaves it un-ringed.
_this_week = metrics.monday_of(last.date())
_complete = [wk for wk in sorted_weeks_all if wk < _this_week]
if _complete:
    _span = (datetime.fromisoformat(_complete[-1]).date()
             - datetime.fromisoformat(_complete[0]).date()).days // 7 + 1
    _cal_weeks = [(datetime.fromisoformat(_complete[0]).date()
                   + timedelta(days=7 * i)).isoformat() for i in range(_span)]
    _zero = _span - len(_complete)
    print(f"\n=== DIRECT SETS/WEEK vs FLOOR "
          f"(mean over {_span} calendar weeks ending {_complete[-1]}"
          f"{f', incl. {_zero} with zero sets' if _zero else ''}; "
          f"current partial week excluded) ===")
    knee = metrics.knee_flexion_sets(enriched_sets)
    for part, fl in sorted(metrics.PART_FLOORS.items(), key=lambda kv: kv[0]):
        avg = sum(weekly_part_sets.get((wk, part), 0) for wk in _cal_weeks) / _span
        flag = "OK " if avg >= fl.sets else "LOW"
        extra = ""
        if part == "hamstrings":
            kf = sum(knee.get(wk, 0) for wk in _cal_weeks) / _span
            extra = f"   (knee flexion {kf:.1f}/wk — hinges make up the rest)"
        print(f"  {flag} {part:12} {avg:5.1f} / {fl.sets:2d}  {'#' * int(round(avg))}{extra}")

# --- rolling windows vs floor ---
# Calendar weeks hide a block that was skipped four days ago, and one 7-day
# window can't tell a one-off from a pattern. The 19:00 plan check-in reads
# these two sections for its volume watch (Tom, 2026-11-09): 7 d is what the
# schedule can still fix, 14 d and 28 d say whether the schedule is the problem.
_today = datetime.now().astimezone().date()
_floors = sorted(metrics.PART_FLOORS.items(), key=lambda kv: kv[0])


def _local(s):
    return s["dt"].astimezone().date()


def _window(days):
    out: dict[str, float] = defaultdict(float)
    for s in enriched_sets:
        if 0 <= (_today - _local(s)).days < days:
            out[classify(s["title"])] += s["set_credit"]
    return out


_w7, _w14, _w28 = _window(7), _window(14), _window(28)
_last_hit: dict[str, int] = {}
for s in enriched_sets:
    part, age = classify(s["title"]), (_today - _local(s)).days
    if age >= 0:
        _last_hit[part] = min(age, _last_hit.get(part, 10 ** 6))
print(f"\n=== ROLLING VOLUME vs FLOOR (direct sets; today = {_today}) ===")
print(f"  {'':16} {'last 7d':>8} {'14d /wk':>8} {'28d /wk':>8} {'floor':>6}  last trained")
for part, fl in _floors:
    a, b, c = _w7.get(part, 0), _w14.get(part, 0) / 2, _w28.get(part, 0) / 4
    ago = _last_hit.get(part)
    ago = "never" if ago is None else "today" if ago == 0 else f"{ago} d ago"
    print(f"  {'OK ' if a >= fl.sets else 'LOW'} {part:12} {a:8.1f} {b:8.1f} {c:8.1f} "
          f"{fl.sets:6d}  {ago}")

# What one session of each routine delivers, so a reader can project the next
# 7 days: sets already inside the window + what the planned sessions will add.
# "usual" is the median of the last 4 sessions (blocks skipped for a busy
# machine pull it down); "full" is the best of the last 6.
_sessions: dict[str, dict[str, dict[str, float]]] = defaultdict(
    lambda: defaultdict(lambda: defaultdict(float)))
for s in enriched_sets:
    if (_today - _local(s)).days < 60:
        _sessions[s["workout_title"]][s["dt"].isoformat()][classify(s["title"])] += s["set_credit"]
print(f"\n=== TYPICAL DIRECT SETS PER SESSION (usual = median of last 4, full = best of last 6) ===")
for title, by_day in sorted(_sessions.items()):
    days = sorted(by_day)
    if len(days) < 3:
        continue
    cells = []
    for part, _fl in _floors:
        last4 = sorted(by_day[d].get(part, 0) for d in days[-4:])
        usual = (last4[(len(last4) - 1) // 2] + last4[len(last4) // 2]) / 2
        full = max(by_day[d].get(part, 0) for d in days[-6:])
        if full:
            cells.append(f"{part} {usual:g}" + (f" ({full:g})" if full != usual else ""))
    print(f"  {title} [{len(days)} sessions in 60 d]: " + ", ".join(cells))

# --- cardio ---
print(f"\n=== CARDIO: last 12 weeks ===")
cardio_workouts_by_week: dict[str, int] = defaultdict(int)
cardio_minutes_by_week: dict[str, float] = defaultdict(float)
for w in workouts:
    if classify(w["title"]) == "cardio" or "zone" in w["title"].lower() or "cardio" in w["title"].lower():
        wk = monday_of(w["start_dt"].date())
        cardio_workouts_by_week[wk] += 1
        if w["duration_min"]:
            cardio_minutes_by_week[wk] += w["duration_min"]

for wk in sorted_weeks_all:
    n = cardio_workouts_by_week.get(wk, 0)
    mins = cardio_minutes_by_week.get(wk, 0.0)
    print(f"  {wk}:  {n} sessions  {mins:.0f} min")

# --- missed-session detector: weeks with 0 workouts ---
print(f"\n=== ZERO-WORKOUT WEEKS (last 12 weeks) ===")
for wk in sorted_weeks_all:
    if week_counts.get(wk, 0) == 0:
        print(f"  {wk}  (zero workouts)")
