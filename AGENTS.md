# MISHKA — Engineering Rules (Antigravity)

- **Branch & Git safety**: Work only in `ag/mishka-*` branches. Never push to, merge into, or delete other branches or existing tags. Never commit secrets, tokens, SSH keys, or real child data/audio.
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
