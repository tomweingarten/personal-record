#!/usr/bin/env python3
"""Shared training-metrics core (stdlib only).

Single source of truth for the numbers used by both the text report
(``analyze.py``) and the recomp dashboard (``build_dashboard.py``), so the two
never drift. No printing — pure loading + computation.

Key metric choices (2026-07-18):
  * Epley e1RM is used only as a *trend* input, never as a headline — it is
    biased toward heavy low-rep sets, which is exactly the wrong lens for a
    hypertrophy (8-15 rep) program. A linear fit over each session's best
    e1RM (``linreg``) averages set-to-set noise and reads progress at any
    rep range.
  * Volume-load (lb x reps) rewards the higher-rep work e1RM ignores.
  * Lifts are SHOWN as an e{N}RM, N = the middle of the lift's rep range
    (``ref_reps``). Epley is linear, so eNRM = e1RM / (1 + N/30): every ratio,
    %-slope and the strength index are identical for any N. N only makes the
    displayed number a load you could lift for those reps.
"""
from __future__ import annotations
import csv
import re
import routine_roles
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import NamedTuple

import config

ROOT = config.TRAINING
KG_TO_LB = 2.2046226218


# --- loading -------------------------------------------------------------

def load_csv(name: str, root: Path = ROOT) -> list[dict]:
    with (root / name).open() as f:
        return list(csv.DictReader(f))


def epley(weight_kg: float, reps: float) -> float:
    """Estimated 1RM (Epley). Trend input only — see module docstring."""
    return weight_kg * (1 + reps / 30.0)


def effective_reps(reps: int, rpe) -> float:
    """Reps-to-failure implied by a logged RPE (reps + reps-in-reserve).

    RPE 10 = 0 in reserve; each point below 10 = ~1 more rep in reserve. So a
    12 @ RPE 8 behaves like a ~14-rep max effort.

    NOT used for e1RM — see ``build_enriched_sets``. Kept for RIR-compliance
    analytics (is the terminal set actually landing in its tier's band?), which
    is what the RPE log is for."""
    try:
        r = float(rpe)
    except (TypeError, ValueError):
        return reps
    if not (1 <= r <= 10):
        return reps
    return reps + max(0.0, 10.0 - r)


def parse_workouts(workouts: list[dict]) -> list[dict]:
    """Attach start_dt / date / duration_min and return sorted by start_dt."""
    for w in workouts:
        w["start_dt"] = datetime.fromisoformat(w["start_time"].replace("Z", "+00:00"))
        w["date"] = w["start_dt"].date().isoformat()
        try:
            end = datetime.fromisoformat(w["end_time"].replace("Z", "+00:00"))
            w["duration_min"] = round((end - w["start_dt"]).total_seconds() / 60, 1)
        except Exception:
            w["duration_min"] = None
    workouts.sort(key=lambda w: w["start_dt"])
    return workouts


DROPSET_CREDIT = 0.5


def build_enriched_sets(workouts: list[dict], exercises: list[dict],
                        sets_: list[dict]) -> list[dict]:
    """Working sets with title/date/e1RM attached, sorted by workout time.

    Mirrors analyze.py exactly: warmups and zero weight/rep sets are dropped.
    """
    date_to_workout = {w["id"]: w for w in workouts}
    ex_by_wid_order = {(e["workout_id"], e["order"]): e for e in exercises}

    # Merge titles that are the SAME lift recorded under two names. Hevy stores
    # the title on each logged exercise, so renaming a template renames it going
    # forward only -- history stays under the old name. Downstream code keys on
    # title alone (recent_sets_by_lift, alltime_max, classify), so a rename
    # silently SPLITS one lift in two: push_targets.template_titles resolves the
    # template to its newest title and then sees only the sessions since.
    #
    # This is an EXPLICIT allowlist, not "newest title wins", because a rename
    # carries two meanings that are indistinguishable from the rename itself:
    #
    #   correction      -- same machine, mislabelled. Glute Thrust -> Glute Drive
    #                      (2026-08-26 -> 08-30). History MUST merge; it is one
    #                      lift and splitting it hides sessions.
    #   equipment change-- the rename records a move to different hardware, and
    #                      the loads are not comparable. "Bayesian Bicep Curl" ->
    #                      "... (Genesis Dual Cable Cross)" is the old gym at up
    #                      to 70 lb vs the Genesis cross at 46. Merging those
    #                      pools two gyms' loads: it lifts alltime_max from 46 to
    #                      70, so the MAX_VS_ALLTIME ceiling stops binding and a
    #                      46 -> 54 lb step passes that should be blocked. That
    #                      is the 2026-08-04 bug in push_targets.plan()'s docstring,
    #                      re-entered from the other side.
    #
    # So: default to LEAVING TITLES SPLIT, and merge only what is known to be one
    # lift. Add an entry here only after confirming the machine did not change.
    title_aliases = {
        "Glute Thrust (Machine)": "Glute Drive (Machine)",
    }

    def get_title(s):
        e = ex_by_wid_order.get((s["workout_id"], s["exercise_order"]))
        if not e:
            return None
        return title_aliases.get(e["title"], e["title"])

    enriched = []
    for s in sets_:
        title = get_title(s)
        if not title:
            continue
        w = date_to_workout.get(s["workout_id"])
        if not w:
            continue
        try:
            wt = float(s["weight_kg"]) if s["weight_kg"] else 0.0
            reps = int(s["reps"]) if s["reps"] else 0
        except (ValueError, TypeError):
            wt, reps = 0.0, 0
        # Keep BODYWEIGHT sets (no load logged, e.g. "Leg Raise Parallel Bars",
        # pull-ups, dragon flags). Dropping them on wt == 0 hid 127 real working
        # sets — 98 of them leg raises — so core volume read far too low. They
        # count as sets; they just can't carry an e1RM or a tonnage figure.
        if s["type"] == "warmup" or reps == 0:
            continue
        rpe = s.get("rpe")
        bw = wt == 0
        enriched.append({
            "date": w["date"],
            "dt": w["start_dt"],
            "title": title,
            "weight_kg": wt,
            "reps": reps,
            # e1RM uses RAW reps, never the RPE-adjusted count. RPE logging began
            # 2026-07-24; feeding effective_reps() here shifted every logged set
            # up +2.5% on average (max +8.3%) against 21 months of unlogged
            # history — a step change in the trend at the logging boundary, not a
            # strength change. Same artifact class as the chain-link fix (dd83115).
            # It also leaked past the (then flat) 12-rep cap: 12 raw reps @ RPE 8 was scoring
            # Epley at 14 effective reps, well outside where the formula holds.
            # The adjustment's value is level accuracy; the regression reads
            # *slope*, and a near-constant offset barely moves slope — so dropping
            # it costs little and removes a real discontinuity. Revisit with an
            # RPE-native metric once there's a full block of logged data.
            # 0 for bodyweight — no load, so no meaningful estimate; that also
            # keeps these lifts out of the strength-trend regressions.
            "one_rm_est": 0.0 if bw else epley(wt, reps),
            "bodyweight": bw,
            "type": s["type"],
            "rpe": rpe,
            # Routine + template: the set's rep range (and so its e1RM cap) and
            # its strength series depend on which routine it was done in.
            "workout_title": w.get("title", ""),
            "template_id": ex_by_wid_order[(s["workout_id"], s["exercise_order"])]["exercise_template_id"],
            # How much of a "set" this row is worth for volume counting. Hevy logs
            # each drop of a drop set as its own row, but a drop is a continuation
            # of one extended set, not an independent set — counting each as 1.0
            # inflates the weekly volume floors. Tom ran 610 dropset rows (16% of
            # all-time working sets) before pausing the technique during the cut;
            # resuming it would have silently inflated every floor.
            # 0.5 is a CONVENTION, not a measurement — same 0.5 used for synergist
            # credit below. A drop is real stimulating work (near failure, minimal
            # rest) but at reduced load and without a fresh start, so it's worth
            # more than nothing and less than a full set.
            "set_credit": DROPSET_CREDIT if s["type"] == "dropset" else 1.0,
        })
    enriched.sort(key=lambda s: s["dt"])
    return enriched


def load_all(root: Path = ROOT) -> dict:
    """Load and enrich everything the report + dashboard need."""
    workouts = parse_workouts(load_csv("workouts.csv", root))
    exercises = load_csv("exercises.csv", root)
    sets_ = load_csv("sets.csv", root)
    enriched = build_enriched_sets(workouts, exercises, sets_)
    return {
        "workouts": workouts,
        "exercises": exercises,
        "sets": sets_,
        "enriched_sets": enriched,
        "date_to_workout": {w["id"]: w for w in workouts},
        "first": workouts[0]["start_dt"],
        "last": workouts[-1]["start_dt"],
    }


# --- stats ---------------------------------------------------------------

def linreg(xs, ys):
    """Ordinary least squares. Returns (slope, intercept, r2)."""
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    if sxx == 0:
        return 0.0, my, 0.0
    slope = sxy / sxx
    intercept = my - slope * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return slope, intercept, r2


# --- body-part classification -------------------------------------------

# The two hamstring movement patterns, named once and reused. They train
# different regions and the distinction has to survive independently of the
# muscle label: the hamstrings floor can otherwise be satisfied entirely by
# hinges (erector-heavy work wearing a hamstring label), which is the hole this
# split exists to expose. The hamstrings entry in PART_KEYWORDS is built from
# these, and knee_flexion_sets() reads KNEE_FLEXION_KEYS, so adding a curl
# variant updates both at once instead of requiring two hand-synced edits.
KNEE_FLEXION_KEYS = ["leg curl", "ham curl", "nordic", "glute ham", "ghr"]
HIP_HINGE_KEYS = ["rdl", "romanian", "good morning", "back extension", "hyperext"]

# Order matters — more specific patterns first. Shoulders before back so
# "lateral raise" doesn't match "lat". Legs before back so "romanian deadlift"
# matches "romanian" before "deadlift".
PART_KEYWORDS = [
    # Delts split by head: they have separate volume needs, and rear-delt volume is
    # posture-relevant (face pulls are prehab). Rear before chest
    # so "reverse FLY" isn't caught by the chest "fly" keyword.
    ("rear delt",  ["rear delt", "face pull", "reverse fly", "reverse pec", "rear fly",
                    "pull apart", "pullapart"]),
    ("side delt",  ["lateral raise", "lat raise", "side raise", "upright row"]),
    ("front delt", ["shoulder press", "overhead press", "ohp", "front raise", "arnold",
                    "push press"]),
    # Ab work that *names a leg* must beat the legs rules below — "leg raise",
    # "hanging leg raise", "captain's chair leg raise" all contain "leg " and were
    # being counted as leg volume (same trap as "chin" ⊂ "maCHINe").
    ("core",      ["leg raise", "hanging leg", "knee raise", "toes to bar", "knee tuck", "reverse crunch"]),
    # "legs" was one bucket for four muscles with independent volume needs, and the
    # aggregate hid a real hole: it read ~5 sets/wk (mildly under floor) while the
    # decomposition over the same 12 weeks was quads 2.3/wk, hip-hinge 2.0/wk, leg
    # curl 0.5/wk, glutes 0.2/wk and calves 0.0 — 2 calf sets in 21 months of
    # history. A single floor on the sum can't surface that. Order: calves and
    # hamstrings before quads so "calf raise" and "leg curl" don't fall into the
    # broad quad patterns; hamstrings before glutes so "glute ham raise" is a
    # hamstring lift; all of them before "back" so "romanian deadlift" doesn't
    # match the back keyword "deadlift".
    ("calves",     ["calf", "calve", "heel raise", "toe press"]),
    # Hip hinges (RDL, back extension, good morning) count as hamstrings: hamstring
    # is the prime mover in loaded hip extension. They are NOT knee flexion though,
    # and the two train different regions of the muscle — knee_flexion_sets() below
    # breaks that out, because "hamstrings: 2.5/wk" satisfied entirely by back
    # extensions is the exact failure mode this split exists to expose.
    ("hamstrings", KNEE_FLEXION_KEYS + HIP_HINGE_KEYS + ["hamstring"]),
    # No bare "kickback" here — triceps is classified later, so it would swallow
    # "Triceps Kickback". Same trap as "chin" ⊂ "maCHINe" below.
    ("glutes",     ["hip thrust", "glute"]),
    # "leg " (with the trailing space) subsumes "leg press"/"leg extension", so
    # those are not listed separately — matching is unordered `any(k in t)`, and a
    # key that a shorter key in the same list already covers is dead weight that
    # reads as load-bearing.
    ("quads",      ["squat", "leg ", "lunge", "hack", "bulgarian", "step up",
                    "goblet", "sissy"]),
    # Back before chest so "chest-supported ROW" (a back lift; "chest" = the pad)
    # tags as back. Chest lifts carry no back keyword, so they still fall to chest.
    # "chin up" not bare "chin" — "chin" ⊂ "maCHINe" mis-tagged every machine lift as back.
    # "vertical traction" / "lat prayer" / "pullover" are lat-dominant machines and
    # cable work that carried no back keyword and were silently landing in "other".
    ("back",      ["row", "pulldown", "pullup", "pull up", "chin up", "chinup", "chin-up", "lat pulldown", "cable row", "deadlift", "rack pull", "shrug", "pull-up", "vertical traction", "lat prayer", "pullover"]),
    # "floor press" carries no other chest keyword and was landing in "other". It's
    # also the travel-safe press under the pressing rule: the floor caps the ROM
    # mechanically, so the deep loaded bottom position can't happen.
    ("chest",     ["bench", "chest", "fly", "push up", "pushup", "dip", "cable cross", "pec deck", "incline press", "floor press"]),
    # Arms split three ways: biceps and triceps are separate muscles with separate
    # floors, and forearm/wrist work is a third thing that was inflating "arms".
    # Forearms first ("wrist curl" would otherwise match the biceps "curl").
    ("forearms",  ["wrist", "forearm", "grip"]),
    ("triceps",   ["tricep", "skull crusher", "skullcrusher", "push down", "pushdown", "kickback"]),
    ("biceps",    ["curl", "preacher", "hammer", "bayesian"]),
    ("core",      ["crunch", "plank", "sit up", "ab ", "russian twist", "dragon flag"]),
    ("cardio",    ["run", "bike", "spinning", "cycling", "row machine", "stair", "cardio", "walk", "zone 2", "zone 3", "elliptical", "treadmill"]),
]

class Floor(NamedTuple):
    """A muscle's weekly direct-set floor, and the reason it is that number.

    The rationale lives here rather than in prose so there is exactly one owner:
    ``training/sync_docs.py`` renders this table into the programming doc, so the
    numbers can't drift between code and docs the way the old flat-10 floor did
    (it ended up restated in three files, two of which went stale).
    """
    sets: int
    why: str


# Weekly DIRECT-set floors, per muscle. A flat 10 across the board was wrong in
# both directions: it over-demanded muscles that arrive pre-loaded from compounds
# and under-demanded nothing, while the "legs" aggregate hid a 4-way hole.
#
# The differentiator is *indirect volume received*, not muscle-specific biology —
# there's no good evidence for per-muscle volume optima, but the dose-response is
# well mapped (Schoenfeld/Krieger 2017, PMID 27433992) and flattens around 10+
# direct sets. So: prime movers that get little or no indirect work carry the full
# floor; muscles that eat 0.5/set from every press or pull carry less.
#
# Judged on DIRECT sets (weekly_part_sets), not effective sets — the discount for
# indirect work is already priced into the floor, so scoring it against a number
# that also includes indirect work would double-count it.
PART_FLOORS: dict[str, Floor] = {
    "chest":      Floor(10, "Prime mover; essentially no indirect volume reaches it."),
    "back":       Floor(10, "Prime mover; essentially no indirect volume reaches it."),
    "quads":      Floor(8,  "Prime mover with ~zero indirect volume."),
    "side delt":  Floor(8,  "Little indirect volume, and reliably volume-responsive."),
    "hamstrings": Floor(6,  "Keep **≥4 of it knee flexion** — hinges train a different region "
                            "and are erector-heavy."),
    "calves":     Floor(6,  "No indirect volume whatsoever."),
    "triceps":    Floor(6,  "Plus ~0.5 per set from every press."),
    "rear delt":  Floor(4,  "Cheap shoulder insurance, not a prescription. 2 sets on the Genesis "
                            "cable cross, in the curl + lateral block. Rows/pulldowns add ~0.25/set on top."),
    "glutes":     Floor(4,  "Plus ~0.5 per set from every squat and hinge."),
    "biceps":     Floor(4,  "Plus ~0.5 per set from every row and pulldown; ~50 pull sets/wk already."),
}

# Muscles deliberately carrying NO floor, and why — rendered into the doc alongside
# the table so "absent from PART_FLOORS" never reads as an oversight.
UNFLOORED = {
    "forearms": "Tom uses Versa Gripps on RDLs, pulldowns and rows, so those lifts give ~no grip "
                "stimulus and direct wrist work is the *only* forearm work he gets — but forearm "
                "size isn't a stated goal, so there's nothing to hold it to. Don't cut it either.",
    "core":     "Adequately covered by direct ab work plus hinge/squat bracing.",
}


def classify(title: str) -> str:
    # Strip parenthetical equipment/gym qualifiers first: they name the machine,
    # not the muscle. "Overhead Cable Triceps Extension (Genesis Dual Cable Cross)"
    # and "Triceps Pushdown (Copy) (Genesis Dual Cable Cross)" were both matching
    # the chest keyword "cable cross" and counting as chest volume.
    t = re.sub(r"\([^)]*\)", " ", title.lower())
    for part, keys in PART_KEYWORDS:
        if any(k in t for k in keys):
            return part
    return "other"


# --- aggregators ---------------------------------------------------------

def monday_of(d) -> str:
    """Monday-start ISO week key for a date or ISO date string."""
    if isinstance(d, str):
        d = datetime.fromisoformat(d).date()
    return (d - timedelta(days=d.weekday())).isoformat()


# Reps feeding the estimate are capped PER LIFT at the top of its rep range +
# E1RM_CAP_MARGIN (2026-10-29). The old flat cap of 12 sat at or below the range
# of every 10-15 / 12-20 / 15-20 lift, so reps-at-fixed-load progress there was
# invisible: hammer curl 35x12 -> 35x15 and skullcrusher 40x13 -> 40x15 read
# dead flat. Epley is unreliable far from where you train, but the trend only
# reads ratios between nearby rep counts, so the cap only needs to catch a
# runaway set -- and a set past the range top is one where load was already due
# to rise. Replayed Aug-Oct: 8 of 239 first sets exceed range top + 3; the 8-12
# lifts' slopes move <0.2 %/wk; the headline index reads ~2 points lower,
# because the flat cap underrated sessions that opened above 12 reps and then
# credited the next load jump from that low base.
#
# Epley was kept over Brzycki/Lombardi/Mayhew/Wathan/O'Conner after testing on
# Tom's own load changes (2026-10-29): Brzycki over-credits reps, Lombardi
# over-credits load, and the middle four were within noise of each other.
E1RM_CAP_MARGIN = 3
# ...and a floor. Sets of 1-4 reps come from the pre-2026-07 heavy/low-rep style;
# mixing them with 8-12 work makes a rep-range *change* read as a strength change.
# 5 is deliberate: it keeps the current protocol's heavy 5-6 top sets (the
# standardized measurement) while dropping the very-low-rep outliers. Empirically
# free — all 17 tracked lifts are retained at this floor (a floor of 8 would drop 7).
E1RM_REP_FLOOR = 5


def set_rep_range(s: dict) -> tuple[int, int]:
    return routine_roles.rep_range(s["title"], s["template_id"], s["workout_title"])


def rep_cap(s: dict) -> int:
    """Max reps of this set that feed its e1RM (see E1RM_CAP_MARGIN)."""
    return set_rep_range(s)[1] + E1RM_CAP_MARGIN


def ref_reps(lo: int, hi: int) -> int:
    """N for the displayed e{N}RM: the middle of the rep range."""
    return round((lo + hi) / 2)


def trend_key(s: dict) -> str:
    """Strength-series name for a set. A template run at two deliberate
    intensities in two routines (the cable lateral raise: HEAVY 12-16 in Push,
    LIGHT 15-20 in Don't Push) is split per routine. Pooled, its week-to-week
    ratios swung with whichever day came last (16% residual noise vs ~4% for
    everything else), and the strength index chained those swings."""
    if s["template_id"] in routine_roles.ROLE_TEMPLATES \
            and not routine_roles.single_role(s["template_id"]):
        role = routine_roles.role_for(s["workout_title"], s["template_id"])
        if role:
            return f'{s["title"]} · {role["label"]}'
    return s["title"]


def session_best(enriched_sets: list[dict], last: datetime,
                 window_days: int, rep_floor: int = E1RM_REP_FLOOR,
                 key=lambda s: s["title"]) -> dict[str, dict[str, dict]]:
    """{title: {date: {best_e1rm, vol, dt}}} within the window (best set/day).

    The measurement is the session's **first loaded working set** — never a
    later one. vol still counts every working set regardless.

    Why first-set-only rather than the old first-*qualifying*-set: the old rule
    fell through to set 2 whenever set 1 exceeded the rep cap, and set 2 is a
    fatigued set at the same or lower load. That fired on 33% of
    exercise-sessions historically and 38% after the 2026-07-21 shift to 8-12
    rep work. The bias ran one way — precisely when reps-at-fixed-load were
    climbing (i.e. when progress was happening), the metric swapped in a
    lower-valued set and read the trend down.

    Handling of a first set outside the band, by side — the two are NOT
    symmetric and an earlier version of this fix wrongly dropped both:

    * **Above ``rep_cap(s)``** → recorded at the cap, a **censored lower
      bound** on capacity. Dropping these would be censoring on the outcome: a
      set goes over the cap exactly when reps-at-fixed-load have been climbing.
      The cap sits above the lift's own range (see E1RM_CAP_MARGIN), so this
      only fires on runaway sets, not on ordinary in-range progress.
    * **Below ``rep_floor``** → keep scanning for the first in-band set, i.e.
      the old fall-through, retained *only* on this side. It is not the
      pathological case: a sub-5-rep opener is a heavy top set or the top of a
      warm-up ramp, and the next set is at the same or lower load with *more*
      reps, so falling through does not systematically hide progress the way
      the high side did. Dropping instead costs real data — it removed Hack
      Squat and Romanian Deadlift from the trend basket entirely, leaving zero
      lower-body lifts in a dashboard whose main finding is that lower body is
      undertrained. Sub-5-rep openers are legacy anyway (17% of sets before
      2026-07-21, 1% after), so this path is shrinking on its own.

    ``key`` names the series a set belongs to (default its title;
    strength_trends passes ``trend_key``). Each session also records the
    measurement set's rep ``range``.

    Bodyweight sets are skipped when looking for the measurement set rather
    than ending the search: they carry no load, so they can't be a measurement
    under any protocol. This is not the fall-through described above — that was
    about substituting a *fatigued, lower-load* set for a valid one; here there
    is no candidate value to reject."""
    cutoff = last - timedelta(days=window_days)
    sess: dict[str, dict[str, dict]] = defaultdict(dict)
    measured: set[tuple[str, str]] = set()
    for s in enriched_sets:
        if s["dt"] < cutoff:
            continue
        d = sess[key(s)].setdefault(
            s["date"], {"best_e1rm": 0.0, "vol": 0.0, "dt": s["dt"],
                        "censored": False, "title": s["title"]})
        k = (key(s), s["date"])
        # Within an exercise, sets are in set_index order (sync_hevy writes them
        # that way and the sort below is stable), so the first loaded one here is
        # the first loaded working set performed *for that exercise*. NOTE this
        # says nothing about where the exercise sat in the session — execution
        # order across exercises is unrecoverable from Hevy (see dashboard/README).
        # A dropset row can never be the measurement: it's a continuation at
        # reduced load, so it would read as a strength drop.
        if k not in measured and not s["bodyweight"] and s["type"] != "dropset":
            cap = rep_cap(s)
            if s["reps"] > cap:
                # Stop here: looking further would substitute a fatigued,
                # lower-value back-off set — the exact bias this rewrite removes.
                measured.add(k)
                d["best_e1rm"] = epley(s["weight_kg"], cap)
                d["censored"] = True
                d["range"] = set_rep_range(s)
            elif s["reps"] >= rep_floor:
                measured.add(k)
                d["best_e1rm"] = s["one_rm_est"]
                d["range"] = set_rep_range(s)
            # below the floor: don't mark measured — keep scanning for the first
            # in-band set (benign fall-through, see docstring).
        d["vol"] += s["weight_kg"] * s["reps"]
    # drop sessions with no measurement set (best_e1rm never set)
    for title in list(sess):
        sess[title] = {dt: v for dt, v in sess[title].items() if v["best_e1rm"] > 0}
        if not sess[title]:
            del sess[title]
    return sess


# A lift's first sessions EVER are calibration, not measurement: programming.md
# runs new exercises at RIR 2-3 for their first 2-3 sessions, and the opening set
# is often a feel-out at a placeholder load. The 2026-08-06 Hip Thrust opened at
# 66 lb x 10 against a real working load near 156, so its first week-to-week ratio
# in the strength index was 2.25. When the lift reached 4 in-window sessions on
# 2026-09-18 it joined the basket, and that one ratio moved the headline from
# +31.6% to +53.9% overnight. Counted all-time, not in-window, so a lift sliding
# out of the window never gets re-calibrated.
CALIBRATION_SESSIONS = 2


def calibration_dates(enriched_sets: list[dict],
                      n: int = CALIBRATION_SESSIONS) -> dict[str, set[str]]:
    """{title: the dates of that lift's first ``n`` sessions ever}."""
    seen: dict[str, list[str]] = defaultdict(list)
    for s in enriched_sets:           # sorted by dt
        ds = seen[s["title"]]
        if len(ds) < n and s["date"] not in ds:
            ds.append(s["date"])
    return {t: set(ds) for t, ds in seen.items()}


def strength_trends(enriched_sets: list[dict], last: datetime,
                    window_days: int = 90, min_sessions: int = 4) -> list[dict]:
    """Per-lift e1RM + volume-load regression (lb, lb/week). Sorted by n desc.

    Each row carries ``calib_dates`` (see CALIBRATION_SESSIONS). The composite
    strength index skips them; the per-lift regressions still see every session,
    because a lift's own sparkline should show where it started.

    Rows are per ``trend_key`` series: ``title`` is the series name, ``base_title``
    the Hevy lift, ``ref_reps`` the N for display (from the latest session's
    range, since ranges are re-programmed over time)."""
    calib = calibration_dates(enriched_sets)
    sess = session_best(enriched_sets, last, window_days, key=trend_key)
    rows = []
    for title, bydate in sess.items():
        pts = sorted(bydate.values(), key=lambda p: p["dt"])
        if len(pts) < min_sessions:
            continue
        t0 = pts[0]["dt"]
        xs = [(p["dt"] - t0).total_seconds() / 86400.0 for p in pts]
        e1 = [p["best_e1rm"] * KG_TO_LB for p in pts]
        vol = [p["vol"] * KG_TO_LB for p in pts]
        e_slope, _, e_r2 = linreg(xs, e1)
        v_slope, _, _ = linreg(xs, vol)
        pct = (e1[-1] - e1[0]) / e1[0] * 100 if e1[0] else 0.0
        rows.append({
            "title": title, "base_title": pts[-1]["title"], "n": len(pts),
            "ref_reps": ref_reps(*pts[-1]["range"]),
            "latest_e": e1[-1], "e_wk": e_slope * 7, "pct": pct, "r2": e_r2,
            "latest_vol": vol[-1], "v_wk": v_slope * 7,
            "e1_series": e1, "vol_series": vol,
            "dates": [p["dt"] for p in pts],
            "calib_dates": calib.get(pts[-1]["title"], set()),
        })
    rows.sort(key=lambda r: -r["n"])
    return rows


def weekly_part_sets(enriched_sets: list[dict]) -> dict[tuple[str, str], float]:
    """{(week_monday, part): set_count} over all working sets (direct only).

    Counts ``set_credit``, not rows, so drop-set continuations don't each read as
    a full set. Returns floats for that reason — formatting is the caller's job.
    """
    out: dict[tuple[str, str], float] = defaultdict(float)
    for s in enriched_sets:
        out[(monday_of(s["date"]), classify(s["title"]))] += s["set_credit"]
    return out


def knee_flexion_sets(enriched_sets: list[dict]) -> dict[str, int]:
    """{week_monday: leg-curl-pattern sets} — the knee-flexion slice of hamstrings.

    Hip hinges and knee flexion both classify as hamstrings but train different
    regions, and hinges dominate Tom's log (24 hinge sets vs 6 leg curl over 12
    weeks). Without this breakout a hamstring floor can be satisfied entirely by
    back extensions, which is erector-heavy work wearing a hamstring label.
    """
    out: dict[str, float] = defaultdict(float)
    for s in enriched_sets:
        if any(k in s["title"].lower() for k in KNEE_FLEXION_KEYS):
            out[monday_of(s["date"])] += s["set_credit"]
    return out


def synergist_sets(title: str) -> dict[str, float]:
    """Indirect (synergist) volume a compound lift contributes, as fractions of a set.

    Heuristic, NOT a measured value: adding direct arm work to compound-only
    programs grows arms more, so indirect work counts for something but less than
    a full set — 0.5 is the common convention. Treat effective totals as
    directional. Keyed off the exercise name rather than the classified part so
    isolation work (lateral raise, pec deck) correctly contributes nothing.

    Each credit is decided by its OWN predicate and merged, rather than by a
    first-match-wins if/elif chain. The chain was a live bug: the
    "supported/machine work braces the trunk" early-return was written to deny
    *core* credit, but sat ahead of every other branch, so it also silently
    denied glute credit to Hack Squat and Leg Press (Tom's primary squat
    patterns) while Goblet Squat got it — and denied triceps/front-delt credit
    to every Smith-machine press, purely because "smith" was in a lower-body
    guard list. One predicate per muscle keeps an argument about the core from
    deciding a question about the glutes.
    """
    t = title.lower()
    if any(k in t for k in ("fly", "butterfly", "pec deck", "raise", "rear delt")):
        return {}                                    # isolation — no synergists

    out: dict[str, float] = {}
    # Hinges load the spinal erectors hard (back extension trains them directly);
    # free squats/lunges demand real trunk bracing.
    hinge = any(k in t for k in ("rdl", "romanian", "deadlift", "good morning",
                                 "back extension", "hyperext"))
    squat = any(k in t for k in ("squat", "lunge", "step up", "split squat",
                                 "leg press"))
    # Supported/machine lower-body work braces the trunk FOR you — this is a
    # statement about the core only. It does not make the hip extension stop
    # happening, so it must not gate the glute credit below.
    # Deliberately NOT a bare "machine" test: a machine back extension still has
    # the erectors as prime mover, so it should keep its core credit. Only these
    # patterns actually take the trunk out of the lift.
    braced = any(k in t for k in ("hack", "leg press", "smith"))
    # NOTE weaker evidence than the arm case: the trunk works largely
    # isometrically here rather than as a prime mover through a loaded ROM, and
    # "core" lumps abs (flexion) with erectors (extension) — this credit is
    # mostly erector work, not abs.
    if not braced:
        if hinge:
            out["core"] = 0.5
        elif squat:
            out["core"] = 0.25
    # Glutes: any loaded hip extension, braced or not. Knee-isolation machines
    # (leg extension / leg curl) match neither pattern and correctly get nothing.
    if hinge or squat:
        out["glutes"] = 0.5
    # Pressing works triceps + front delt; pulling works biceps + rear delt. These
    # are disjoint — lumping both into one "arms" bucket implied presses trained
    # biceps and rows trained triceps, which is why the split matters.
    if any(k in t for k in ("bench", "chest press", "overhead press", "shoulder press",
                            "dip", "push up", "pushup", "ohp", "incline press",
                            "floor press")):
        out["triceps"] = 0.5
        out["front delt"] = 0.5
    if any(k in t for k in ("row", "pulldown", "pull up", "pullup", "chin up", "chinup")):
        out["biceps"] = 0.5
        out["rear delt"] = 0.25
    return out


def weekly_effective_sets(enriched_sets: list[dict]) -> dict[tuple[str, str], float]:
    """{(week_monday, part): effective_sets} = direct + fractional synergist volume."""
    out: dict[tuple[str, str], float] = defaultdict(float)
    for s in enriched_sets:
        wk = monday_of(s["date"])
        credit = s["set_credit"]
        out[(wk, classify(s["title"]))] += credit
        for part, frac in synergist_sets(s["title"]).items():
            out[(wk, part)] += frac * credit
    return out


def weekly_workout_counts(workouts: list[dict]) -> tuple[dict, dict]:
    """(week_counts, week_titles) keyed by Monday-start ISO week."""
    counts: dict[str, int] = defaultdict(int)
    titles: dict[str, list[str]] = defaultdict(list)
    for w in workouts:
        wk = monday_of(w["start_dt"].date())
        counts[wk] += 1
        titles[wk].append(w["title"])
    return counts, titles
