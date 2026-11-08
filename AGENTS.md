# Instructions for AI assistants

This repository is one person's training dashboard, published so others can fork it. The code encodes their program: routine names, Hevy exercise template IDs, rep ranges, volume floors, progression rules. Expect to change those for a new user.

## Layout

- `training/config.py`: where data and settings live. Read it first.
- `training/sync_hevy.py`: pulls workouts from Hevy into `<data>/training/*.csv`.
- `training/metrics.py`: the metric core (strength index, effective sets, muscle mapping, volume floors).
- `training/routine_roles.py`: per-routine intensity roles keyed by Hevy exercise template ID. The most personal file.
- `training/push_targets.py`: writes next-session loads into Hevy routines. **It writes to the user's Hevy account**; always run `--dry-run` first and show the user the plan.
- `training/build_dashboard.py`: renders `<data>/dashboard/recomp.html`. `training/house.py` is the visual style.
- `training/sync_cardio.py` + `sources/`: heart-rate and recovery sources.
- `sync_scale.py`, `sync_tape.py`, `sync_renpho.py`, `tape_watch.py`: body measurements.
- `demo.py` + `sample-data/`: a demo build from invented data.

## Adapting it for someone

1. Ask which integrations they use (Hevy is the core; everything else is optional), their targets (weight band, protein, calories, height for the body-fat estimate) and what program they run.
2. Write their `settings.toml` from `settings.example.toml`. Secrets go in `<data>/.env`, never in settings or code.
3. Replace the routine roles and template IDs in `routine_roles.py` with theirs (from their `exercises.csv` after a first sync), and review the volume floors in `metrics.py` against their goals.
4. For a new heart-rate or recovery provider, add `sources/<name>.py` with a `Source` class (see `sources/__init__.py`) instead of editing `sync_cardio.py`.
5. Check with `python3 demo.py` and, once they've synced, `python3 training/build_dashboard.py`.

## Rules

- Never commit anything from the data directory, `settings.toml`, `.env` files or token files. Keep the pre-commit hook on (`git config core.hooksPath .githooks`).
- Never bind the dashboard server to `0.0.0.0`.
- Don't run `push_targets.py` without `--dry-run` unless the user asks you to write to Hevy.
