#!/usr/bin/env python3
"""Push updated target loads back into the Hevy routine you just trained.

Runs nightly after sync_hevy.py. For each routine trained in the last week (its
latest session, if not already processed -- see main()), computes the
next target load per exercise with the SAME rule the dashboard uses
(build_dashboard.workout_targets -> backoff_load, i.e. first-set double
progression per training/programming.md), and writes it into the matching Hevy
routine so the next session opens at the right weight.

  python3 training/push_targets.py            # apply (nightly default)
  python3 training/push_targets.py --dry-run  # show the diff, write nothing
  python3 training/push_targets.py --routine "Don't Push It v2 (2026-07)" --force
                                              # re-plan one routine from its own
                                              # last session (repairs a bad write
                                              # without waiting to train it again)
  python3 training/push_targets.py --restore training/routine_snapshots/<f>.json

WHY THIS IS A SEPARATE SCRIPT: a bug in progression logic must not be able to
break data ingest. sync_hevy.py stays read-only.

--- Safety model (every claim below was verified against the live API on
    2026-08-02 with a throwaway routine, not read off the spec) ---

1. PUT /routines/{id} is a FULL REPLACE. There is no PATCH. Anything not sent
   back is destroyed, so we rebuild the whole routine from a fresh GET and touch
   only weight_kg on `normal` sets.

2. rep_range MUST be forwarded explicitly. The published spec omits it from the
   GET schema; it is in fact returned, and dropping it silently deletes the
   routine's 8-12 rep target. This is the one field that actually broke in
   testing, and it is the automated version of a known Hevy bug
   (2026-08-01: "Hevy clears the rep target when you swap an exercise in-app").
   Verified preserved: superset_id, rest_seconds, per-exercise notes, folder_id,
   set types (warmup rows included), reps.

3. KNOWN UNFIXABLE LOSS: routine-LEVEL notes. GET never returns the field, so it
   cannot be round-tripped and a PUT clears it. Per-exercise notes -- where all
   of Tom's programming cues live -- are safe. Keep routine cues on exercises.

4. Match on exercise_template_id, never title. Tom runs gym-specific duplicate
   exercises; title matching would write one machine's load onto another.

5. Snapshot before every write, git-committed, restorable with --restore. This
   is what makes a bad write recoverable rather than a data-loss event. The
   commit is commit_snapshots() at the end of every run (local only; the health
   agent's push-every-turn rule carries it to GitHub). Until 2026-10-09 nothing
   actually committed them and ten snapshots sat untracked.

6. Bounded deltas + an all-time-max ceiling: a logic bug cannot write an absurd
   load, it aborts that exercise and says so.

7. Idempotent on workout id. A re-run cannot bump a load that was already
   bumped -- that is what would turn this into a ratchet.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_dashboard as bd  # noqa: E402
import metrics  # noqa: E402
from sync_hevy import API_BASE, API_KEY  # noqa: E402

import config

ROOT = config.TRAINING
SNAP_DIR = ROOT / "routine_snapshots"
STATE_PATH = ROOT / "routine_state.json"

LB_PER_KG = 2.20462262185

# Bounds on a single night's change. The "lighten" branch of backoff_load is a
# deliberate -15% (med * 0.85), so the down bound has to clear that; anything
# past these is a bug, not a decision. The up bound is shared with the dashboard
# so the two cannot disagree about what a runaway step is; it is raised per-lift
# to one minimum gym increment where that is larger (see plan()).
MAX_UP_PCT = bd.MAX_STEP_PCT
MAX_DOWN_PCT = 0.20

# A normal set lighter than BACKOFF_BELOW x the exercise's heaviest normal set is a
# deliberate back-off set: to_put_body keeps it proportionally lighter on a load
# change instead of promoting it to the working load.
BACKOFF_BELOW = 0.97

# Nightly scope: every routine trained within this many days of the newest
# workout gets its latest session processed (see main()).
RECENT_DAYS = 7

# Separate cap on adopting a PERFORMED load that differs from the prescribed one
# (the "hold" re-sync path, which the up bound above structurally cannot see).
MAX_RESYNC_UP_PCT = 0.12
# A session that follows a layoff at least this long is not a fair read of
# capacity, so it may raise a prescription but never lower one. See _layoff_gate.
LAYOFF_DAYS = 7
# Never write a load more than this above the heaviest working set ever logged
# for the lift. Catches a runaway even if it somehow lands inside MAX_UP_PCT.
MAX_VS_ALLTIME = 1.10


# ---------------------------------------------------------------- http

def api(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        API_BASE + path, data=data, method=method,
        headers={"api-key": API_KEY, "accept": "application/json",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def _unwrap(payload: dict) -> dict:
    """GET /routines/{id} returns {"routine": [ {...} ]} on some responses and
    {"routine": {...}} on others. Normalise."""
    r = payload["routine"]
    return r[0] if isinstance(r, list) else r


def get_routine(rid: str) -> dict:
    return _unwrap(api("GET", f"/routines/{rid}"))


def all_routines() -> list[dict]:
    out, page = [], 1
    while True:
        try:
            d = api("GET", f"/routines?page={page}&pageSize=10")
        except urllib.error.HTTPError as e:
            if e.code == 404:      # past the last page
                break
            raise
        rs = d.get("routines") or []
        if not rs:
            break
        out += rs
        page += 1
    return out


# ---------------------------------------------------------------- state

def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n")


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40]


def snapshot(routine: dict) -> Path:
    SNAP_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M")
    p = SNAP_DIR / f"{stamp}-{slug(routine['title'])}.json"
    p.write_text(json.dumps(routine, indent=1) + "\n")
    return p


# ---------------------------------------------------------------- rebuild

def resync_ok(cur_lb: float, back: float, title: str = "") -> bool:
    """Is adopting a PERFORMED load that exceeds the prescribed one sane?

    Same three-way shape as bd.step_ok, but on the tighter MAX_RESYNC_UP_PCT: a
    re-sync is not a decision this script made, so it gets less rope than a
    deliberate progression step.

    The stack clause is why this cannot stay a bare percentage. On the Genesis
    cross the only move available from 26 lb is 34 (+31%), so a flat 12% cap
    blocked the Don't Push It lateral raise every night from 2026-08-26 and it
    could never progress at all -- the same failure the percentage-only bound in
    bd.step_ok already had, fixed there and missed here.
    """
    if back <= cur_lb:
        return True
    step = back - cur_lb
    if step <= bd.min_increment(cur_lb) + 0.01:
        return True
    if back <= cur_lb * (1 + MAX_RESYNC_UP_PCT):
        return True
    return bool(bd.stack_for(title)) and step <= bd.COARSE_STEP_LB + 0.01


def to_put_body(routine: dict, new_kg: dict[int, float | None],
                new_notes: dict[int, str] | None = None,
                warmups: dict[int, list[tuple[float, int]]] | None = None) -> dict:
    """Rebuild the full routine for PUT, overriding weight_kg on `normal` sets
    of the exercises in ``new_kg`` (keyed by exercise index), per-exercise
    notes for the exercises in ``new_notes``, and the warmup rows of the
    exercises in ``warmups`` (all existing warmup rows replaced, new ones first).

    Every field here is load-bearing: dropping any one of them deletes it from
    the routine. rep_range especially -- see module docstring note 2.
    """
    warmups = warmups or {}
    exercises = []
    for i, e in enumerate(routine["exercises"]):
        override = new_kg.get(i)
        top = max((s.get("weight_kg") or 0 for s in e["sets"]
                   if s["type"] == "normal"), default=0)
        sets = []
        src = e["sets"]
        if i in warmups:
            sets = [{"type": "warmup", "weight_kg": kg, "reps": reps,
                     "distance_meters": None, "duration_seconds": None,
                     "custom_metric": None} for kg, reps in warmups[i]]
            src = [s for s in src if s["type"] != "warmup"]
        for s in src:
            w = s.get("weight_kg")
            if override is None or s["type"] not in ("normal", "dropset"):
                kg = w
            elif s["type"] == "normal" and w and top and w < top * BACKOFF_BELOW:
                # A deliberately lighter BACK-OFF set (the press's 4th set, ~10%
                # under the working load). Keep it proportionally lighter, snapped
                # to 2.5 lb, instead of promoting it to a 4th working set.
                kg = round(override * (w / top) * LB_PER_KG / 2.5) * 2.5 / LB_PER_KG
            else:
                # `dropset` rows are myo-rep mini-sets at the SAME working load
                # (programming.md, intensity techniques, 2026-09-18), so a load
                # change applies to them too -- otherwise the activation set moves
                # and the mini-sets stay at last month's weight.
                kg = override
            out = {
                "type": s["type"],
                "weight_kg": kg,
                "reps": s.get("reps"),
                "distance_meters": s.get("distance_meters"),
                "duration_seconds": s.get("duration_seconds"),
                "custom_metric": s.get("custom_metric"),
            }
            if s.get("rep_range") is not None:
                out["rep_range"] = s["rep_range"]
            sets.append(out)
        exercises.append({
            "exercise_template_id": e["exercise_template_id"],
            "superset_id": e.get("superset_id"),
            "rest_seconds": e.get("rest_seconds"),
            "notes": (new_notes or {}).get(i, e.get("notes")),
            "sets": sets,
        })
    return {"routine": {"title": routine["title"], "exercises": exercises}}


def diff_structure(before: dict, after: dict,
                   expect_notes: dict[int, str] | None = None,
                   expect_warmups: dict[int, list[tuple[float, int]]] | None = None
                   ) -> list[str]:
    """Everything that must be identical after the write. Weights are checked by
    the caller against what it intended; this catches collateral damage.

    ``expect_notes`` lists exercises whose note we MEANT to change; those are
    checked against the intended text instead of against the previous note, so a
    deliberate role cue is not reported as damage. Every other note must still be
    byte-identical -- that is the guard on Tom's programming cues.

    ``expect_warmups`` lists exercises whose warmup rows we MEANT to replace:
    their warmup rows are checked against the intended ramp, and every
    non-warmup row must still match the old routine exactly.
    """
    problems = []
    expect_notes = expect_notes or {}
    expect_warmups = expect_warmups or {}
    if expect_warmups:
        before = json.loads(json.dumps(before))
        after = json.loads(json.dumps(after))
        for idx, want in expect_warmups.items():
            if idx >= len(after["exercises"]):
                continue
            got = [(s.get("weight_kg") or 0, s.get("reps"))
                   for s in after["exercises"][idx]["sets"] if s["type"] == "warmup"]
            if (len(got) != len(want)
                    or any(abs(g[0] - w[0]) > 0.05 or g[1] != w[1]
                           for g, w in zip(got, want))):
                problems.append(f"exercise {idx}: warmups {got} != intended {want}")
            for side in (before, after):
                side["exercises"][idx]["sets"] = [
                    s for s in side["exercises"][idx]["sets"] if s["type"] != "warmup"]
    if len(before["exercises"]) != len(after["exercises"]):
        return [f"exercise count {len(before['exercises'])} -> {len(after['exercises'])}"]
    if before.get("folder_id") != after.get("folder_id"):
        problems.append(f"folder_id {before.get('folder_id')} -> {after.get('folder_id')}")
    for idx, (b, a) in enumerate(zip(before["exercises"], after["exercises"])):
        tag = b.get("title", b["exercise_template_id"])[:30]
        for f in ("exercise_template_id", "superset_id", "rest_seconds", "notes"):
            want = expect_notes[idx] if (f == "notes" and idx in expect_notes) else b.get(f)
            if want != a.get(f):
                problems.append(f"{tag}: {f} {want!r} -> {a.get(f)!r}")
        if len(b["sets"]) != len(a["sets"]):
            problems.append(f"{tag}: set count {len(b['sets'])} -> {len(a['sets'])}")
            continue
        for j, (bs, as_) in enumerate(zip(b["sets"], a["sets"])):
            for f in ("type", "reps", "rep_range", "distance_meters",
                      "duration_seconds"):
                if bs.get(f) != as_.get(f):
                    problems.append(f"{tag} set{j}: {f} {bs.get(f)!r} -> {as_.get(f)!r}")
    return problems


# ---------------------------------------------------------------- planning

def template_titles(exercises_csv: list[dict],
                    workouts: list[dict]) -> dict[str, str]:
    """{exercise_template_id: title}, chronologically latest occurrence wins
    (templates get renamed -- e.g. Chest Press (Machine) Vertical -> Horizontal,
    or a gym qualifier is added: Bayesian Bicep Curl -> ... (Genesis Dual Cable
    Cross)).

    Sort EXPLICITLY. exercises.csv is written newest-first, so a plain overwrite
    loop keeps the OLDEST title, not the newest -- the exact opposite of what
    this is for. That is not cosmetic: the resolved title is the key into
    recent_sets_by_lift, so an out-of-date title silently sources the target from
    a different gym's history for the same template. It did, on 2026-08-04: the
    Bayesian curl resolved to the 2025-02-02 title, read a 2026-07-24 session on
    other equipment, and wrote 45 lb while the actual 2026-08-03 session was
    40 lb x 12, x 12 (a hold at 40).
    """
    when = {w["id"]: w["date"] for w in workouts}
    out = {}
    for e in sorted(exercises_csv, key=lambda r: when.get(r["workout_id"], "")):
        out[e["exercise_template_id"]] = e["title"]
    return out


# Per-routine roles (rep ranges, cues) live in routine_roles.py since 2026-09-22,
# shared with the dashboard. Re-exported here under their old names.
from routine_roles import (  # noqa: E402,F401
    BICEP_CURL_DB, HOME_ROUTINE, LATERAL_RAISE_DB, LATERAL_RAISE_GENESIS,
    ROLE_TEMPLATES, ROUTINE_ROLES, SKULLCRUSHER_DB, role_for)


# Marker for the one line in a per-exercise note that this script owns. Tom's
# programming cues live in these notes (module docstring note 3), so the note is
# never overwritten -- only the marked line is replaced, and everything else is
# preserved verbatim.
NOTE_MARK = "[auto]"


HEVY_NOTE_MAX = 1000   # Hevy silently truncates longer notes (2026-10-03)


def merged_note(existing: str | None, cue: str | list[str]) -> str:
    """``existing`` with its [auto] line(s) replaced by ``cue``, others untouched."""
    cues = [cue] if isinstance(cue, str) else cue
    kept = [ln for ln in (existing or "").splitlines()
            if not ln.strip().startswith(NOTE_MARK)]
    while kept and not kept[-1].strip():
        kept.pop()
    return "\n".join(kept + [f"{NOTE_MARK} {c}" for c in cues]).strip()


# Best single-set reps at the load the routine will prescribe next, over the
# last year (Tom, 2026-10-19). Shown so one off day does not anchor what he
# thinks he can do -- it is a reference point, NOT the progression gate (that is
# first-set double progression, bd.backoff_load). Drop-set / myo-rep mini-sets
# are excluded: they start pre-fatigued, so their reps are not comparable.
PR_WINDOW_DAYS = 365
PR_MATCH_LB = 1.0      # kg<->lb rounding; the finest real step is 2.5 lb


def pr_line(enriched: list[dict], title: str, lb: float | None) -> str | None:
    if not lb:
        return None     # bodyweight / unloaded slot
    cutoff = date.today() - timedelta(days=PR_WINDOW_DAYS)
    best = max((s["reps"] for s in enriched
                if s["title"] == title and s["type"] != "dropset"
                and s["dt"].date() >= cutoff
                and abs(s["weight_kg"] * LB_PER_KG - lb) <= PR_MATCH_LB),
               default=None)
    return f"PR @{round(lb * 2) / 2:g}: {best if best is not None else 'none yet'}"


# ---------------------------------------------------------------- warm-ups

# Warm-up ramp written into the routine (2026-10-08, Tom asked for it). Shape is
# programming.md -> Warm-ups item 3: ~40% x 5 -> ~60% x 3-5 -> ~80% x 2-3, two
# ramp sets on lighter lifts. Before this the routines carried NO warmup rows and
# the ramp lived in Tom's head, so it varied session to session (hack squat:
# 88/178/268 into 303, chest press: none logged on some days).
WARMUP_RAMP = ((0.40, 5), (0.60, 4), (0.80, 2))
WARMUP_RAMP_LIGHT = ((0.50, 5), (0.75, 3))
WARMUP_HEAVY_LB = 100      # working load at/above which the 3-set ramp is used

# Which slots get a ramp: the FIRST lower-body and the FIRST upper-body exercise
# of the routine, by metrics.classify, in routine order -- so a reordered routine
# moves the ramp with it. Small muscles never qualify, which is why the home
# arms/delts routine gets none (programming.md item 2: no ramp on laterals,
# triceps, curls -- and that session has nothing else). Today that resolves to
# hack squat + chest press on Push, chest-supported row + seated leg curl on
# Pull, matching the table in programming.md.
WARMUP_LOWER = {"quads", "hamstrings", "glutes"}
WARMUP_UPPER = {"chest", "back", "front delt"}


def _snap_warmup(lb: float, title: str) -> float:
    ladder = bd.stack_for(title)
    if ladder:
        return bd.snap_down(lb, ladder)
    step = bd.min_increment(lb)
    return max(step, round(lb / step) * step)


def warmup_slots(routine: dict, titles: dict[str, str]) -> list[int]:
    """Exercise indices that should carry a warm-up ramp (see WARMUP_LOWER)."""
    out, seen = [], set()
    for i, e in enumerate(routine["exercises"]):
        title = titles.get(e["exercise_template_id"]) or e.get("title") or ""
        part = metrics.classify(title)
        group = ("lower" if part in WARMUP_LOWER
                 else "upper" if part in WARMUP_UPPER else None)
        if group and group not in seen:
            seen.add(group)
            out.append(i)
    return out


def warmup_plan(routine: dict, titles: dict[str, str],
                working_kg: dict[int, float]) -> dict[int, list[tuple[float, int]]]:
    """{exercise index: [(kg, reps), ...]} -- the ramp each slot SHOULD carry,
    computed from its working load (the new load if one is being written
    tonight, else the stored one). Only slots whose current warmup rows differ
    are returned, so an unchanged routine produces no write."""
    out = {}
    for i in warmup_slots(routine, titles):
        e = routine["exercises"][i]
        title = titles.get(e["exercise_template_id"]) or e.get("title") or ""
        kg = working_kg.get(i)
        if kg is None:
            kg = next((s.get("weight_kg") for s in e["sets"]
                       if s["type"] == "normal"), None)
        if not kg:
            continue
        work_lb = kg * LB_PER_KG
        ramp = WARMUP_RAMP if work_lb >= WARMUP_HEAVY_LB else WARMUP_RAMP_LIGHT
        want, last = [], 0.0
        for pct, reps in ramp:
            lb = _snap_warmup(work_lb * pct, title)
            # A coarse stack can snap two steps onto the same pin; a repeated or
            # at-working-load warm-up is pointless, so drop it.
            if lb <= last + 0.01 or lb >= work_lb - 0.01:
                continue
            want.append((lb / LB_PER_KG, reps))
            last = lb
        have = [(s.get("weight_kg") or 0, s.get("reps"))
                for s in e["sets"] if s["type"] == "warmup"]
        same = (len(have) == len(want)
                and all(abs(a[0] - b[0]) < 0.05 and a[1] == b[1]
                        for a, b in zip(have, want)))
        if not same:
            out[i] = want
    return out


def down_step_ok(base: float, proposed: float, title: str) -> bool:
    """Is ``base`` -> ``proposed`` a sane single-session step DOWN?

    The mirror of bd.step_ok, and it exists because that mirror was missing.
    step_ok has carried a coarse-stack escape since the pin table went in: on the
    cable cross the adjacent pin is 26 -> 34 lb, +31%, and its own docstring says
    a percentage is the wrong lens for that ("fine on a lateral raise"). The down
    direction was left as a bare -MAX_DOWN_PCT test, so the SAME 8 lb gap on the
    SAME lift was legal upward and blocked downward: 34 -> 26 is -23.5% against a
    20% bound.

    That asymmetry pinned the cable lateral raise at 34 lb from 2026-08-05 to
    09-02. The 34 was itself a fossil of the unreachable-prescription bug (28.5
    prescribed, rounded up to 34, adopted). Every correction back to 26 was then
    discarded -- including sessions Tom had actually performed at 20 -- so each
    session re-issued 34 and he ground out 8,5 / 10,6 / 8,7 under the 8-rep floor.

    One pin down is never a violation. Anything further still has to clear the
    percentage bound, so a runaway ``lighten`` is still caught.
    """
    if proposed >= base:
        return True
    ladder = bd.stack_for(title)
    if not ladder:
        return False
    below = [v for v in ladder if v < base - 0.01]
    return bool(below) and proposed >= below[-1] - 0.01


def plan(routine: dict, data: dict, session_date: str) -> list[dict]:
    """One row per routine exercise: what we'd write and why (or why not).

    ``session_date`` is the date of the workout being processed. A lift is only
    updated from THAT session -- see the "not trained in this session" gate below.
    """
    enriched = data["enriched_sets"]
    by_lift = bd.recent_sets_by_lift(enriched)
    titles = template_titles(data["exercises"], data["workouts"])
    last = max(s["dt"] for s in enriched)
    cutoff = (last - timedelta(days=bd.TARGETS_WINDOW_DAYS)).date().isoformat()

    alltime_max = {}
    for s in enriched:
        lb = s["weight_kg"] * LB_PER_KG
        alltime_max[s["title"]] = max(alltime_max.get(s["title"], 0.0), lb)

    # What was actually performed in this session, and what the routine asks for.
    # Used to tell a skipped exercise apart from a substituted one, below.
    performed_today = {s["title"] for s in enriched
                       if s["dt"].date().isoformat() == session_date}
    routine_titles = {titles.get(e["exercise_template_id"]) or e.get("title")
                      for e in routine["exercises"]}

    rows = []
    for i, e in enumerate(routine["exercises"]):
        title = titles.get(e["exercise_template_id"]) or e.get("title")
        cur = next((s.get("weight_kg") for s in e["sets"]
                    if s["type"] == "normal"), None)
        cur_lb = cur * LB_PER_KG if cur else None
        row = {"i": i, "title": title, "cur_lb": cur_lb, "prev_lb": None,
               "new_lb": None, "action": None, "note": "", "skip": None,
               "role": role_for(routine["title"], e["exercise_template_id"])}
        rows.append(row)

        recent = [r for r in by_lift.get(title, []) if r[0] >= cutoff]
        if not recent:
            row["skip"] = "no history in window"
            continue
        # backoff_load reads the lift's MOST RECENT session, and by_lift is keyed
        # by title alone -- so a lift that sits in two routines pools both. The
        # cable lateral raise is in Push It (heavy) and Don't Push It (light);
        # without this gate, processing one routine on a day that lift was not
        # trained would hand it the other routine's load (34 lb into a 20 lb
        # slot). Only ever update a lift from the session being processed.
        lift_last = max(d for d, _, _ in recent)
        if lift_last != session_date:
            # Distinguish "skipped" from "substituted". Tom's gym has one of each
            # machine, so an occupied or broken station forces an alternate --
            # Glute Thrust for Hip Thrust on 2026-08-26, Triceps Pushdown for the
            # overhead cable extension on 08-24. The load cannot carry across
            # (different machine, different leverage) so the prescription still
            # correctly holds; but reporting it as a bare "not trained" makes a
            # completed session read as a hole in the log. Volume floors already
            # handle this on their own -- metrics.classify() keys off title
            # keywords, so both names land in the same muscle bucket.
            subs = sorted(t for t in performed_today
                          if t not in routine_titles
                          and metrics.classify(t) == metrics.classify(title))
            row["skip"] = f"not trained in this session (last {lift_last})"
            if subs:
                row["skip"] += f" — substituted: {', '.join(subs)}"
            continue
        # How long since this lift was last trained, for the layoff gate below.
        prior = [d for d, _, _ in recent if d < session_date]
        gap_days = ((date.fromisoformat(session_date)
                     - date.fromisoformat(max(prior))).days if prior else None)
        e1 = None
        role = row["role"]
        _heavy, back, note, _sh, action, prev, _rr = bd.workout_targets(
            title, recent, e1,
            rep_range=role["reps"] if role else None,
            strict=role["strict"] if role else None,
            floor=role.get("floor") if role else None)
        row["action"], row["note"] = action, note
        if action in ("none", "bodyweight") or back in (None, 0):
            row["skip"] = f"action={action}"
            continue
        if cur_lb is None:
            row["skip"] = "routine has no loaded working set"
            continue

        # --- guardrails -------------------------------------------------
        # Bound the PROGRESSION STEP (performed load -> next target), not the
        # gap between the routine's stored default and the target. Those are
        # different quantities and only the first one is a decision this script
        # makes. Stored defaults go stale on their own -- after the 2026-07-31
        # machine swaps the routine still held 60 lb for overhead triceps while
        # the logged working load was 34 lb, so a stored-value bound flagged a
        # correct +3 lb step as a 39% cliff. Re-syncing a stale default to
        # reality is the feature, not the hazard.
        base = prev if prev else cur_lb
        row["prev_lb"] = prev
        delta = (back - base) / base if base else 0.0
        # bd.step_ok, not a bare percentage: the smallest change the gym can
        # actually make is never a violation. A flat 12% cap blocked every
        # possible step below ~20.8 lb (2.5 lb on 20 lb is +12.5%), so the light
        # lateral raise in Don't Push It was BLOCKED every night from 2026-07-26
        # onward and could never progress at all.
        if base and not bd.step_ok(base, back, title):
            row["skip"] = (f"BLOCKED +{delta:.1%} > {MAX_UP_PCT:.1%} "
                           f"(performed {base:.0f} -> {back:.0f} lb)")
            continue
        if delta < -MAX_DOWN_PCT and not down_step_ok(base, back, title):
            row["skip"] = (f"BLOCKED {delta:.1%} < -{MAX_DOWN_PCT:.1%} "
                           f"(performed {base:.0f} -> {back:.0f} lb)")
            continue
        # A "hold" hands back the load that was PERFORMED, so `delta` above is 0
        # by construction and the up-bound cannot fire on it -- every hold was
        # unguarded. That is the hole the 2026-08-05 lateral raise went through:
        # the routine prescribed 28.5 lb (unreachable), the session was logged at
        # 34 (the next real pin), and 34 became the standing target with nothing
        # checking it. Bound the UPWARD re-sync against what was prescribed.
        # Downward re-syncs stay unbounded on purpose -- that is the stale-default
        # case in the note above (60 -> 34 lb after the 2026-07-31 machine swap).
        if cur_lb and not resync_ok(cur_lb, back, title):
            row["skip"] = (f"BLOCKED re-sync {cur_lb:.0f} -> {back:.0f} lb "
                           f"(+{back / cur_lb - 1:.0%} over the prescribed load)")
            continue
        # LAYOFF GATE. The rule above lets a session lower the prescription
        # without bound, which is right for a stale default but wrong for a
        # session that only looks weak because of time off. That is how the
        # iso-lateral chest press went 114 -> 99 lb: the 2026-08-18 session was
        # the first after a 10-day trip, its lighter load was adopted as the
        # standing target, and the lift then had to climb back through a 36-rep
        # gate it had already cleared. A post-layoff session may still RAISE a
        # target -- that is unambiguous good news -- but it may not lower one.
        # Real losses re-assert themselves next session, which is no longer
        # post-layoff and passes straight through.
        if (gap_days is not None and gap_days >= LAYOFF_DAYS
                and cur_lb and back < cur_lb - 0.5):
            row["skip"] = (f"post-layoff ({gap_days}d gap) — holding "
                           f"{cur_lb:.0f} lb, not adopting {back:.0f}")
            continue
        ceiling = alltime_max.get(title, 0.0) * MAX_VS_ALLTIME
        # One pin above the all-time best on a coarse stack is never a runaway
        # (2026-09-18). Without this escape the ceiling is unpassable there: the
        # best ever lifted IS the current pin, the next pin is +15-17%, and the
        # best only rises once Tom lifts it -- so the overhead triceps (40 -> 46)
        # and the Bayesian curl (46 -> 54) were blocked on every graduation.
        # Same shape as step_ok rule 3 and down_step_ok.
        one_pin_up = (bool(bd.stack_for(title))
                      and back - alltime_max.get(title, 0.0) <= bd.COARSE_STEP_LB + 0.01)
        if ceiling and back > ceiling and not one_pin_up:
            row["skip"] = (f"BLOCKED {back:.0f} lb > {MAX_VS_ALLTIME:.0%} of "
                           f"all-time best {alltime_max[title]:.0f} lb")
            continue
        if abs(back - cur_lb) < 0.5:
            row["skip"] = "already at target"
            continue
        row["new_lb"] = back
    return rows


def fmt(rows: list[dict]) -> str:
    out = []
    for r in rows:
        cur = f"{r['cur_lb']:.0f}" if r["cur_lb"] else "-"
        if r["new_lb"] is not None:
            arrow = f"{cur} -> {r['new_lb']:.0f} lb"
            verdict = {"add_load": "UP", "lighten": "DOWN", "hold": "hold"}.get(
                r["action"], r["action"] or "")
            # Flag when the stored default was out of step with what was
            # actually lifted -- that is a re-sync, worth seeing.
            if r["prev_lb"] and abs(r["prev_lb"] - (r["cur_lb"] or 0)) >= 2.5:
                verdict += f" (was performed at {r['prev_lb']:.0f})"
        else:
            arrow = f"{cur} lb (unchanged)"
            verdict = r["skip"] or ""
        out.append(f"  {r['title'][:44]:46} {arrow:24} {verdict}  {r['note']}")
    return "\n".join(out)


# ---------------------------------------------------------------- main

def propagate(routine: dict, writes: dict[int, float], routines: list[dict],
              state: dict, data: dict) -> None:
    """Mirror loads just written into the OTHER routines this script manages.

    Tom runs the cable lateral raise in both Push It v2 and Don't Push It v2, on
    the same station at the same weight. Before this, each routine progressed its
    own copy from whichever session happened to touch it, and the plan() gate
    that (correctly) refuses to update a lift from a session it was not trained
    in meant neither copy could ever fix the other. They drifted to 34 vs 26 lb.

    SCOPE: only routines already in routine_state.json -- i.e. ones this script
    actively manages. Folder is too broad a rule (11 routines share the Push/Pull
    folder, most of them retired programs), and "every routine" would write into
    a year of dead templates. A new routine joins the set the first time it is
    processed normally.

    Matches on exercise_template_id, never title (safety note 4). Snapshots and
    verifies each write exactly like the primary path.
    """
    shared = {routine["exercises"][i]["exercise_template_id"]: kg
              for i, kg in writes.items()}
    for meta in routines:
        if meta["id"] == routine["id"] or meta["id"] not in state:
            continue
        other = get_routine(meta["id"])
        upd = {}
        for j, e in enumerate(other["exercises"]):
            kg = shared.get(e["exercise_template_id"])
            if kg is None:
                continue
            # A lift with a per-routine role is deliberately held at different
            # loads in different routines (ROUTINE_ROLES). Mirroring it is the
            # bug this exemption exists to stop: on 2026-09-02 propagate pushed
            # the light day's 26 lb straight into the heavy day and flattened the
            # split within one run.
            if e["exercise_template_id"] in ROLE_TEMPLATES:
                continue
            cur = next((s.get("weight_kg") for s in e["sets"]
                        if s["type"] == "normal"), None)
            if cur is None or abs(cur - kg) < 0.05:
                continue
            upd[j] = kg
        if not upd:
            continue
        names = ", ".join(f"{other['exercises'][j].get('title')} "
                          f"-> {kg * LB_PER_KG:.0f} lb" for j, kg in upd.items())
        # The mirrored load needs its own PR line, or the slot shows the PR at
        # the load it no longer prescribes until that routine is next trained.
        # Role slots never reach here, so the PR is the only [auto] line.
        titles = template_titles(data["exercises"], data["workouts"])
        pnotes = {}
        for j, kg in upd.items():
            e = other["exercises"][j]
            pr = pr_line(data["enriched_sets"],
                         titles.get(e["exercise_template_id"]) or e.get("title"),
                         kg * LB_PER_KG)
            want = merged_note(e.get("notes"), pr) if pr else None
            if want and want != (e.get("notes") or "") and len(want) <= HEVY_NOTE_MAX:
                pnotes[j] = want
        snap = snapshot(other)
        api("PUT", f"/routines/{other['id']}", to_put_body(other, upd, pnotes))
        after = get_routine(other["id"])
        bad = diff_structure(other, after, pnotes)
        for j, kg in upd.items():
            got = next((s.get("weight_kg") for s in after["exercises"][j]["sets"]
                        if s["type"] == "normal"), None)
            if got is None or abs(got - kg) > 0.05:
                bad.append(f"{after['exercises'][j].get('title')}: wrote "
                           f"{kg:.2f} kg, routine now reads {got}")
        print(f"  propagated to {other['title']}: {names}")
        if bad:
            print("  VERIFY FAILED on the propagated write:")
            for b in bad:
                print("   ", b)
            print(f"  restore with:\n    python3 training/push_targets.py "
                  f"--restore {snap}")


def restore(path: Path) -> None:
    snap = json.loads(path.read_text())
    api("PUT", f"/routines/{snap['id']}", to_put_body(snap, {}))
    after = get_routine(snap["id"])
    bad = diff_structure(snap, after)
    print(f"restored {snap['title']} from {path.name}")
    if bad:
        print("  WARNING, structure still differs:")
        for b in bad:
            print("   ", b)


def set_load(routine_title: str, assignments: list[str]) -> int:
    """Manual override: force a load, bypassing the progression guardrails.

    Exists because the guardrails are correctly shaped for *incremental*
    progression and correctly refuse a large jump -- but a lift can be so far
    out of its rep range that +5%/session would take a couple of months to
    converge (calf extension, 2026-08-02: 24 reps against a 6-12 target, needing
    ~+28%). The answer to that is a deliberate human step, not looser bounds on
    the automatic one. Snapshots and verifies like any other write; deliberately
    does NOT touch the idempotency state, so the next real session still runs.
    """
    routines = all_routines()
    match = next((r for r in routines if r["title"] == routine_title), None)
    if not match:
        print(f"no routine titled {routine_title!r}")
        return 1
    routine = get_routine(match["id"])

    writes, labels = {}, []
    for a in assignments:
        name, _, lb = a.rpartition("=")
        try:
            lb = float(lb)
        except ValueError:
            print(f"bad --set-load {a!r}, expected 'Exercise name=LB'")
            return 1
        hits = [i for i, e in enumerate(routine["exercises"])
                if name.lower() in (e.get("title") or "").lower()]
        if len(hits) != 1:
            print(f"{name!r} matched {len(hits)} exercises in {routine_title!r}"
                  f" — need exactly 1")
            return 1
        i = hits[0]
        cur = next((s.get("weight_kg") for s in routine["exercises"][i]["sets"]
                    if s["type"] == "normal"), None)
        writes[i] = lb / LB_PER_KG
        labels.append(f"  {routine['exercises'][i]['title'][:44]:46} "
                      f"{(cur or 0) * LB_PER_KG:.0f} -> {lb:.0f} lb")

    print(f"{routine_title}  (manual override)")
    print("\n".join(labels))
    snap = snapshot(routine)
    print(f"\nsnapshot: {snap.relative_to(ROOT.parent)}")
    api("PUT", f"/routines/{routine['id']}", to_put_body(routine, writes))
    after = get_routine(routine["id"])
    bad = diff_structure(routine, after)
    for i, kg in writes.items():
        got = next((s.get("weight_kg") for s in after["exercises"][i]["sets"]
                    if s["type"] == "normal"), None)
        if got is None or abs(got - kg) > 0.05:
            bad.append(f"{after['exercises'][i].get('title')}: wrote {kg:.2f} kg, "
                       f"routine now reads {got}")
    if bad:
        print("VERIFY FAILED:")
        for b in bad:
            print("   ", b)
        print(f"\nrestore with:\n  python3 training/push_targets.py "
              f"--restore {snap}")
        return 1
    print(f"wrote {len(writes)} load(s), verified clean")
    return 0


def commit_snapshots() -> None:
    """Commit new snapshots + routine_state.json, and nothing else.

    `git commit -- <paths>` commits only those paths even if a health-agent turn
    has other files staged, so this can never sweep someone's half-done edit into
    a 2 am commit. Local only: a push from cron would fail on any divergence with
    the desktop clone, and the agent pushes on its next turn anyway. Failures
    print and move on -- the snapshot is on disk either way.
    """
    # Optional (settings.toml [hevy] commit_snapshots = true): only when the data
    # directory is itself a git repository you want the history in.
    repo = config.DATA
    if not config.setting("hevy", "commit_snapshots", False) or not (repo / ".git").exists():
        return
    paths = [str(SNAP_DIR.relative_to(repo)), str(STATE_PATH.relative_to(repo))]
    git = ["git", "-C", str(repo), "-c", f"safe.directory={repo}"]
    try:
        subprocess.run(git + ["add", "--", *paths], check=True,
                       capture_output=True, text=True)
        staged = subprocess.run(git + ["diff", "--cached", "--name-only", "--", *paths],
                                check=True, capture_output=True, text=True).stdout.split()
        if not staged:
            return
        snaps = sum(1 for f in staged if f.endswith(".json") and "routine_snapshots" in f)
        subprocess.run(git + ["commit", "-q", "-m",
                              f"push_targets: {snaps} routine snapshot(s) + state "
                              f"({datetime.now():%Y-%m-%d %H:%M})", "--", *paths],
                       check=True, capture_output=True, text=True)
        print(f"committed {len(staged)} file(s)")
    except (subprocess.CalledProcessError, OSError) as e:
        err = getattr(e, "stderr", "") or e
        print(f"WARNING: snapshot commit failed: {str(err).strip()[:300]}")


def main() -> int:
    try:
        return _main()
    finally:
        commit_snapshots()


def _main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="print the plan, write nothing")
    ap.add_argument("--restore", metavar="SNAPSHOT",
                    help="replay a snapshot back into Hevy")
    ap.add_argument("--force", action="store_true",
                    help="ignore the idempotency check (re-process a workout)")
    ap.add_argument("--routine", metavar="TITLE",
                    help="routine to target: with --set-load, the one to force a "
                         "load on; otherwise re-plan this routine from its own "
                         "most recent session instead of the latest workout "
                         "(pair with --force to bypass the idempotency check)")
    ap.add_argument("--set-load", metavar="NAME=LB", action="append", default=[],
                    help="force a load, bypassing guardrails (repeatable)")
    args = ap.parse_args()

    if args.restore:
        restore(Path(args.restore))
        return 0

    if args.set_load:
        if not args.routine:
            print("--set-load requires --routine")
            return 1
        return set_load(args.routine, args.set_load)

    data = metrics.load_all(ROOT)
    workouts = data["workouts"]
    if not workouts:
        print("no workouts")
        return 0
    # With --routine: that routine's own most recent session, so a routine can be
    # re-planned without waiting to train it again (pair with --force to bypass
    # the idempotency check).
    #
    # Nightly: EVERY routine's latest session within RECENT_DAYS of the newest
    # workout, oldest first, each skipped if already processed. This used to be
    # workouts[-1] only, which silently dropped a routine whenever a second
    # workout followed it before the 02:00 run -- a real risk once the home
    # arms/delts session (2026-09-22) floats between days and can land on a gym
    # day. Oldest first so a later session's propagate() wins. The window keeps
    # retired routines (a year of dead templates in the same folder) out.
    if args.routine:
        latest = next((w for w in reversed(workouts)
                       if w["title"] == args.routine), None)
        if not latest:
            print(f"no logged workout titled {args.routine!r}")
            return 1
        todo = [latest]
    else:
        newest = workouts[-1]["start_dt"]
        by_title = {}
        for w in workouts:
            if (newest - w["start_dt"]).days <= RECENT_DAYS:
                by_title[w["title"]] = w
        todo = sorted(by_title.values(), key=lambda w: w["start_dt"])

    routines = all_routines()
    rc = 0
    for latest in todo:
        rc = max(rc, process(latest, routines, data, args))
    return rc


def process(latest: dict, routines: list[dict], data: dict, args) -> int:
    """Plan and write one routine's targets from one logged workout."""
    match = next((r for r in routines if r["title"] == latest["title"]), None)
    if not match:
        print(f"no routine titled {latest['title']!r} — nothing to update "
              f"(workout {latest['date']})")
        return 0

    state = load_state()
    if state.get(match["id"]) == latest["id"] and not args.force:
        print(f"already processed {latest['title']} {latest['date']} "
              f"({latest['id'][:8]}) — nothing to do")
        return 0

    routine = get_routine(match["id"])
    rows = plan(routine, data, latest["date"])
    print(f"{latest['title']}  (workout {latest['date']}, "
          f"routine {match['id'][:8]})")
    print(fmt(rows))

    writes = {r["i"]: r["new_lb"] / LB_PER_KG for r in rows
              if r["new_lb"] is not None}
    # Role cues and the PR line are synced whether or not any load moved, so the
    # note in the app always states which day this is and the PR at the load it
    # now prescribes. Only slots whose [auto] lines are actually out of date are
    # sent.
    notes = {}
    for r in rows:
        cues = [r["role"]["cue"]] if r["role"] else []
        pr = pr_line(data["enriched_sets"], r["title"],
                     r["new_lb"] if r["new_lb"] is not None else r["cur_lb"])
        if pr:
            cues.append(pr)
        if not cues:
            continue
        cur = routine["exercises"][r["i"]].get("notes")
        want = merged_note(cur, cues)
        if len(want) > HEVY_NOTE_MAX:
            print(f"  note TOO LONG ({len(want)} > {HEVY_NOTE_MAX}), not sent: "
                  f"{r['title'][:44]}")
            continue
        if want != (cur or ""):
            notes[r["i"]] = want
    # Warm-up ramps follow the working load being written tonight, so a load
    # change re-derives its ramp in the same PUT.
    warmups = warmup_plan(routine, template_titles(data["exercises"],
                                                   data["workouts"]), writes)
    blocked = [r for r in rows if r["skip"] and r["skip"].startswith("BLOCKED")]
    if blocked:
        print(f"\n{len(blocked)} exercise(s) BLOCKED by guardrails — "
              f"left untouched, review by hand")
    for i in notes:
        print(f"  note: {str(routine['exercises'][i].get('title'))[:44]} -> "
              + " | ".join(ln for ln in notes[i].splitlines()
                           if ln.startswith(NOTE_MARK)))
    for i, ramp in warmups.items():
        print(f"  warm-up: {str(routine['exercises'][i].get('title'))[:44]} -> "
              + ", ".join(f"{kg * LB_PER_KG:g}x{r}" for kg, r in ramp))
    if not writes and not notes and not warmups:
        print("\nno load changes to write")
        if not args.dry_run:
            state[match["id"]] = latest["id"]
            save_state(state)
        return 0
    if args.dry_run:
        print(f"\ndry run — would update {len(writes)} load(s), "
              f"{len(notes)} note(s), {len(warmups)} warm-up ramp(s)")
        return 0

    snap = snapshot(routine)
    print(f"\nsnapshot: {snap.relative_to(ROOT.parent)}")
    api("PUT", f"/routines/{routine['id']}",
        to_put_body(routine, writes, notes, warmups))
    after = get_routine(routine["id"])

    bad = diff_structure(routine, after, notes, warmups)
    for i, kg in writes.items():
        got = next((s.get("weight_kg") for s in after["exercises"][i]["sets"]
                    if s["type"] == "normal"), None)
        if got is None or abs(got - kg) > 0.05:
            bad.append(f"{after['exercises'][i].get('title')}: wrote {kg:.2f} kg, "
                       f"routine now reads {got}")
    if bad:
        print("VERIFY FAILED — the routine is not what we intended:")
        for b in bad:
            print("   ", b)
        print(f"\nrestore with:\n  python3 training/push_targets.py "
              f"--restore {snap}")
        return 1

    print(f"wrote {len(writes)} load(s), {len(notes)} note(s), "
          f"{len(warmups)} warm-up ramp(s), verified clean")
    # Only after the primary write has verified clean -- never mirror a load we
    # are not sure landed.
    propagate(routine, writes, routines, state, data)
    state[match["id"]] = latest["id"]
    save_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
