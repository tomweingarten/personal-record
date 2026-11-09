"""Per-routine intensity roles, shared by push_targets.py and build_dashboard.py.

Split out of push_targets.py on 2026-09-22 so the dashboard can apply the same
rep ranges. push_targets imports build_dashboard, so the dashboard could not
import push_targets back (and must not need the Hevy API key just to render).
"""
from __future__ import annotations


# Deliberate per-routine intensity roles for a lift that sits in more than one
# routine. Keyed routine title -> exercise_template_id (never exercise title;
# safety note 4), because the rep target is otherwise derived from the lift's
# TITLE and both routines share it.
#
# Why this exists: the Genesis cable stack jumps 20 -> 26 -> 34 lb, so there is no
# pin that gives 8-12 hard reps on the lateral raise -- 26 is too light, 34 is
# under the 8-rep shoulder-safety floor (logged 8,5 / 10,6 / 8,7 through August). Rather
# than oscillate between them, the gap is used: 26 for 12-16 on the heavy day
# (8-12 until 2026-10-08, see the role), 20 for 15-20 on the light one. This is NOT a hypertrophy claim -- load is
# broadly interchangeable for growth when sets are taken near failure (the
# Plotkin 2022 note in build_dashboard.backoff_load applies here too). It is a
# way to program around a coarse stack and a medical rep floor.
#
# Cues are TARGETS only (2026-10-08, Tom): form tips live in the free-text part
# of each Hevy note, rationale lives here in comments, never in the app.
#
# A lift listed here is also exempt from propagate() -- the whole point is that
# the two routines hold different loads, so mirroring them is wrong.
LATERAL_RAISE_GENESIS = "36a475a7-9268-4d23-9c58-2c297720f01c"
HOME_ROUTINE = "Home Arms & Delts (2026-09)"
LATERAL_RAISE_DB = "422B08F1"
SKULLCRUSHER_DB = "68F8A292"
BICEP_CURL_DB = "37FCC2BB"      # left the home routine 2026-10-12, kept for imports
HAMMER_CURL_DB = "7E3BC8B6"
WRIST_CURL_DB = "1006DF48"
WRIST_EXTENSION_DB = "d9282e9e-3779-4e2f-b070-2ef5db5f9d94"

OVERHEAD_TRICEPS_GENESIS = "22a02d04-e2ca-4900-af05-e98c96c5b071"

# Myo-rep lifts on the push day (roles added 2026-11-09, see ROUTINE_ROLES).
LEG_EXTENSION = "75A4F6C4"
PEC_DECK = "9DCE2D64"
CALF_EXTENSION = "47B9DF13"

ROUTINE_ROLES = {
    "Push It v2 (2026-07)": {
        # 12-16 since 2026-10-08 (was 8-12 strict). 8-12 graduated 26 -> 34 as soon
        # as 26 gave 12/13, and 34 then logged 8/8/5-ish for eight sessions until
        # it was lightened back. The coarse-stack readiness gate in backoff_load
        # already demands rep_hi on EVERY set for a +31% pin, so rep_hi=16 means
        # ~16 on set 3 (set 1 near 20) at 26 before 34 is offered -- enough
        # surplus that 34 should land at ~10-12. Floor 8 keeps the old shoulder-safety
        # floor as the lighten trigger; without it rep_lo-1 = 11 would read a
        # normal 26 x 14/12/10 session as too heavy.
        LATERAL_RAISE_GENESIS: {
            "label": "HEAVY",
            "reps": (12, 16),
            "strict": False,
            "floor": 8,
            "cue": "HEAVY - 12-16 reps, strict, shoulders down.",
        },
        # Same coarse-stack problem, smaller gap (2026-10-08). 40 -> 46 lb is +15%,
        # under the 2x-cap readiness gate, so 8-12 graduated on set 1 alone
        # (12/8/9 on 09-16) and 46 then logged 8/6/6, 8/8/5, 7/6/6 before
        # dropping back. 10-15 asks for ~15 on set 1 at 40 first. Floor 7 because
        # Tom's current 40 lb sessions run 8/8/9: the default rep_lo-1 = 9 would
        # call that too heavy and strip the load to 34.
        OVERHEAD_TRICEPS_GENESIS: {
            "label": "PUSH",
            "reps": (10, 15),
            "strict": False,
            "floor": 7,
            "cue": "10-15 reps, RIR 2 -> 1 -> 0.",
        },
        # Myo-rep lifts (2026-11-09). The Hevy notes and programming.md both say
        # the activation set is 12-15 and the load moves at the top of that
        # range, but with no role these fell to the title default (8-12 pec deck,
        # 6-12 the other two) and graduated at 12 -- the BOTTOM of the written
        # range. Pec deck went 95 -> 110 in four weeks and then logged 6-9; calf
        # at 180 and leg extension at 120 sat at 8-11, with mini-sets of 1-3.
        # The 2026-09-18 myo-rep change only kept the mini-sets (dropset rows)
        # out of the gate; it never set the range. "reps" is the activation set.
        # Default floor (11): two sessions running under it step the load down.
        **{tid: {
            "label": "MYO",
            "reps": (12, 15),
            "strict": False,
            "cue": "MYO - load moves when set 1 hits 15.",
        } for tid in (LEG_EXTENSION, PEC_DECK, CALF_EXTENSION)},
    },
    "Don't Push It v2 (2026-07)": {
        LATERAL_RAISE_GENESIS: {
            "label": "LIGHT",
            "reps": (15, 20),
            "strict": False,
            # Well under rep_lo on purpose -- the lighten trigger is an average
            # and Tom's third set drops hard. See backoff_load's floor note.
            "floor": 10,
            "cue": "LIGHT - 15-20 reps, RIR 1-2, shoulders down.",
        },
    },
    # Home dumbbell session (2026-09-22): shoulders + arms volume without the
    # commute. The roles exist for the REP RANGES -- the title-derived default is
    # 8-12, which would push the lateral raise heavy -- and they also exempt these
    # templates from propagate(), so home loads never leak into the Travel
    # routine's hotel dumbbells (Lateral Raise (Dumbbell) is in both).
    # Loads are logged as the TOTAL of both dumbbells (Tom's convention), so one
    # 1.25 lb magnet per dumbbell = 2.5 lb, which is min_increment below 50 lb.
    HOME_ROUTINE: {
        LATERAL_RAISE_DB: {
            "label": "HOME",
            "reps": (12, 20),
            "strict": False,
            "floor": 10,   # same 3rd-set drop-off reasoning as the LIGHT lateral
            "cue": "HOME - 12-20 reps, RIR 2 -> 1 -> 0. "
                   "Load = total of both dumbbells.",
        },
        SKULLCRUSHER_DB: {
            "label": "HOME",
            "reps": (10, 15),
            "strict": False,
            # Form cues (lying, not overhead) live
            # in the free-text note above the [auto] line since 2026-10-08.
            "cue": "HOME - 10-15 reps. Load = total of both dumbbells.",
        },
        # Replaced the supinated DB curl on 2026-10-12. Neutral grip puts the brachioradialis -- the visible top
        # of the forearm -- to work, and loads the long head less than a supinated
        # curl. Supinated work stays at the gym (Bayesian, 3 sets x2/wk).
        HAMMER_CURL_DB: {
            "label": "HOME",
            "reps": (10, 15),
            "strict": False,
            "cue": "HOME - 10-15 reps, RIR 2 -> 1 -> 0. "
                   "Load = total of both dumbbells.",
        },
        # Wrist curl + extension superset (2026-10-12, Tom). Versa Gripps on every
        # pull means this is the only forearm stimulus in the program; no floor.
        # 15-20 because the wrist flexors/extensors are small and the joint
        # tolerates high reps better than heavy ones. The hammer curl above covers
        # the top of the forearm.
        WRIST_CURL_DB: {
            "label": "HOME",
            "reps": (15, 20),
            "strict": False,
            "floor": 12,
            "cue": "HOME - 15-20 reps. Load = total of both dumbbells.",
        },
        WRIST_EXTENSION_DB: {
            "label": "HOME",
            "reps": (15, 20),
            "strict": False,
            "floor": 12,
            "cue": "HOME - 15-20 reps. Load = total of both dumbbells.",
        },
    },
}

# Every template that carries a role anywhere -- these must never be mirrored
# between routines by propagate().
ROLE_TEMPLATES = {tid for r in ROUTINE_ROLES.values() for tid in r}


# Lifts kept in the 8-12 rep range with NO heavy low-rep top set. Two sources:
# scapular-retraction / shrug-loading lifts (rows, pulldowns, laterals, presses)
# for shoulder safety, AND pressing per the *Pressing rule*, plus RDL (loaded
# scapular depression).
#
# The constraint is REPS, not RIR (changed 2026-07-31). The risk is heavy load at
# end-range ROM, not proximity to failure. So suppressing the heavy 5-6 rep top set is exactly right
# and stays; the terminal set may go to RIR 0-1 as long as it's >= 8 reps.
SHOULDER_RE = ("press", "overhead", "lateral", "row", "pulldown", "pull-down", "fly",
               "butterfly", "bench", "rdl", "romanian", "deadlift")

# Prehab/corrective work (2026-07-21: embedded in the pull routine rather
# than standalone homework). Prescribed LIGHT at 15-20 reps, RIR 1-2, shoulders
# down — so never a heavy set, and progress reps in that higher range rather than
# chasing load, which would defeat the corrective intent.
PREHAB_RE = ("face pull", "y-raise", "y raise", "external rotation", "band pull")


def default_reps(title: str) -> tuple[int, int]:
    """The title-derived rep target for a lift with no routine role."""
    t = title.lower()
    if any(k in t for k in PREHAB_RE):
        return 15, 20                  # corrective work: light, high-rep
    return (8, 12) if any(k in t for k in SHOULDER_RE) else (6, 12)


def rep_range(title: str, template_id: str, routine_title: str = "") -> tuple[int, int]:
    """The rep range a logged set was programmed for: the role of the routine
    it was done in, else the template's only role, else the title default."""
    role = role_for(routine_title, template_id) or single_role(template_id)
    return role["reps"] if role else default_reps(title)


def role_for(routine_title: str, template_id: str) -> dict | None:
    return ROUTINE_ROLES.get(routine_title, {}).get(template_id)


def single_role(template_id: str) -> dict | None:
    """The role for a template that carries exactly ONE role across all
    routines, else None. The dashboard is keyed by lift, not by routine, so it can
    only apply a role that is unambiguous: the home dumbbell lifts, yes; the cable
    lateral raise (HEAVY in Push, LIGHT in Pull), no."""
    found = [r[template_id] for r in ROUTINE_ROLES.values() if template_id in r]
    return found[0] if len(found) == 1 else None
