# Personal Record

My strength, body-composition, nutrition and cardio dashboard, plus the scripts that feed it and the progression logic that writes my next loads back into Hevy. It's built around my training, my gear and my integrations. Fork it and make it yours.

**Try it with sample data:** `python3 demo.py`, then open the file it prints. No accounts needed.

## What it does

The dashboard is one self-contained HTML page answering:

1. **Am I getting stronger?** A composite strength index: each lift's best estimated 1RM per session, normalised and trend-fitted, so progress at any rep range counts. Hevy's own e1RM favours heavy low-rep sets and hides hypertrophy progress.
2. **What do I lift next session?** Target loads and reps per lift, from double progression with guardrails.
3. **How is each lift trending?**
4. **Is each muscle getting enough work?** Effective sets per week against per-muscle floors.
5. **Is my weight holding steady?** Trend weight against a target band.
6. **Am I eating enough protein?**
7. **Is my body changing?** Tape measurements, scale and a US Navy body-fat estimate.
8. **Am I getting my cardio in?** Moderate sessions and 4x4 intervals detected from minute-by-minute heart rate, with lifting masked out, plus resting HR and HRV trends.
9. **Did I show up?**
10. **This week's postcard:** a shareable image of the week's strength progress and PRs, with nothing about weight, food or attendance.

`push_targets.py` writes the next session's loads (and warm-up sets) into the matching Hevy routines after each workout, with snapshots and a restore command.

## Data sources

| Source | Script | Needs |
|---|---|---|
| Hevy workouts | `training/sync_hevy.py`, `training/push_targets.py`, `training/hevy_hook.py` (webhook) | Hevy Pro API key |
| Smart scale via Home Assistant | `sync_scale.py` | a weight sensor in Home Assistant |
| Renpho smart tape via Home Assistant | `sync_tape.py`, `tape_watch.py` | the `renpho_tape` integration |
| Renpho cloud (tape history) | `sync_renpho.py` | Renpho login |
| MacroFactor | drop daily exports in `nutrition/` | |
| Apple Health calories and protein | `training/nutrition_hook.py` (via the webhook) | an iPhone Shortcut |
| Heart rate and recovery | `training/sync_cardio.py` + `sources/` | Google Health API, or your own source |

Every integration is optional. Add your own heart-rate source by copying the `Source` shape in `sources/google_health.py`; see `sources/__init__.py`.

## Setup

```sh
cp settings.example.toml settings.toml      # your targets, sensors, server address
mkdir -p data && echo "HEVY_API_KEY=..." > data/.env
python3 training/sync_hevy.py               # pull your workouts
python3 training/build_dashboard.py         # build data/dashboard/recomp.html
python3 serve_dashboard.py                  # serve it (rebuilds when data changes)
```

Python 3.11+, standard library only, except the Google Health source, which needs `requests`.

Your data lives in the data directory (`./data` by default, or `data_dir` in `settings.toml`), never in the repository. `training/config.py` documents the layout. `deploy/` has example cron lines and systemd units.

## Privacy

Code and data are separate on purpose. `.gitignore` excludes the data directory, `settings.toml` and every `.env`. The pre-commit hook blocks data files and anything that looks like a token; turn it on with:

```sh
git config core.hooksPath .githooks
```

The dashboard server binds to `127.0.0.1` by default. Put it behind something private (Tailscale, a VPN) rather than on the open internet: it's health data.

## Making it yours

[AGENTS.md](AGENTS.md) is written for AI coding assistants. Open the repository in Claude Code, Codex or Cursor and ask it to adapt the dashboard to your program and integrations.

## License

Code: MIT (see `LICENSE`). Fonts: Poiret One and IBM Plex Mono under the SIL Open Font License, texts in `assets/fonts/`.
