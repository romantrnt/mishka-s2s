# MISHKA — Engineering Rules (Antigravity)

- **Branch & Git safety**: Develop in `ag/mishka-*` branches. Publish reviewed work to `main` only with Roman's explicit authorization (the 2026-11-25 main preparation is authorized). Never force-push, rewrite history, or delete branches or existing tags. Never commit secrets, tokens, SSH keys, or real child data/audio.
- **Runpod & Live Stand safety**: Never stop, recreate, or delete Runpod pod `fihihgl91rnwu4`. Do not deploy, restart the app, or run tests against the live stand while Roman is testing unless explicitly signaled.
- **Max isolation**: Never modify external Max code or Max database. Treat Max profile JSON as an external contract.
- **Voice & ASR stack**: The 2026-11-23 takeover preserves the working AG VoxCPM2 `555` primary with voice conversion OFF. The older Silero rule in recovery material is historical. Do not change the configured primary or add automatic Silero/RVC fallback. Preserve Whisper `large-v3-turbo`, `qwen3:14b` and mandatory input/output validation.
- **Truthfulness & boundaries**: Never invent weather, city, temperature, doctor, room, or appointment time when keys/coordinates/fields are absent. Separate local mock, CPU app, Runpod pipeline, native browser, physical board, and human audio evidence.
- **Product UX invariants**:
  - Support all 4 entry combinations: handset UP/DOWN × authorized/guest.
  - Ordinary actions (menu, games, stories, "Мой день", return, end, new session) must work via buttons even without microphone access; live voice is primary for free dialogue and word game.
  - Recorded stories must play from verified assets, never improvised by LLM. Active playing story is not cut by idle timer; paused story ends after 120s idle.
  - Button 7: short press performs contextual action (answer 7 in quiz, etc.); hold shows clear light progress and exits on release after threshold without requiring millisecond precision.
  - 18-LED board logical state stays binary (`element_id,state,transition_ms`, IDs 0..17). Fill, digit, and emblem are independent; off-state is dim/gray. Serial digit wire state 2 is opt-in via LIGHTING_DIGIT_MODE=fill_color only; default legacy remains 0/1. See docs/digit-contract.md; physical firmware/color acceptance is separate.
- **Coordination**: One writer per overlapping file set. Independent QA/reviewer inspects actual diffs and edge-case scenarios, not author summaries.

## 2026-11-23 demo takeover
- Original AG worktree `/home/khugardin/Documents/AG/Mishka` is preserved. Current demo worktree: `/home/khugardin/Documents/AG/Mishka-Codex-Demo`, branch `ag/mishka-codex-demo-20261123`. Never reset/clean the original.
- Root controls: 2 recorded stories, 3 local authored new stories, 4 the preserved tell-or-ask function, 5 help. Keep text, recorded PHRASE_008, menu actions and timing aligned.
- Local author-name library and synthetic profiles are allowed; visitor names/audio/UID/transcripts/secrets are not Git/log/Obsidian artifacts. Raw source name recordings remain outside Git.
- Garage `/home/server/mishka-candidate` is a dirty working deployment, not a disposable clone. No live switch/restart, .env/device/Max overwrite, Pod start/stop, branch removal or force push without an agreed deployment window.

## 2026-11-25 main preparation and absolute live freeze
- Canonical prepared checkout: `/home/server/mishka` on WSL Ubuntu-24.04, user `server`. Preserve its `.env`, ignored data/models/environment and untracked local files before updating it. The running stand resolves its `.venv` into this directory: never rename/replace the directory or modify that shared environment during preparation.
- Roman explicitly prohibited stopping, restarting or switching the current `98eb560` stand on `:18024`, including when idle. Prepare commands only; wait for a later explicit decision before activation or updating `C:\Users\Users\mishka-commands.txt`.
- `start.sh` is a legacy hardware launcher: it adjusts ALSA capture and microphone gain. Do not execute it as a preflight or with `--help`; use syntax checks and configuration-only validation.
- Operational INFO logs and the opt-in private session journal are separate. Keep `NO_VOICE_RECORDING=true` and the journal disabled for ordinary diagnostics. The current reply-quality judgement shares mandatory output safety/initiative validation; do not disable the shared gate to satisfy an optional-quality request.
