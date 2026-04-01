# TODOs

## Deduplicate add-on options parsing (run.sh + config.py)

**What:** `run.sh` and `_apply_addon_options()` in `config.py:128` both read `/data/options.json` and set env vars, but they map different keys. run.sh handles 6 keys (including station_name, claude_model, playlist_spotify_url). config.py handles 4 keys (including admin_password which isn't in config.yaml schema).

**Why:** Divergent key maps mean adding a new option requires updating two places. The `admin_password` mapping in config.py is dead code since config.yaml doesn't expose it.

**Pros:** Single source of truth for option→env var mapping. Removes dead code.

**Cons:** Touching config.py's startup path needs careful testing. Low urgency since both paths work today.

**Context:** Found during eng review outside voice analysis (2026-04-01). The run.sh jq approach is now canonical. The Python fallback in config.py exists for non-HA Docker runs where options.json doesn't exist. Could consolidate by having run.sh be the sole parser and removing `_apply_addon_options()` entirely, or by making config.py the sole parser and removing the shell loop. The shell approach is better because it runs before any Python imports.

**Depends on / blocked by:** Nothing. Can be done independently.
