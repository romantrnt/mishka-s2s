# MISHKA P0 bounded dialogue / S2S

Date: 2026-10-26. Base: d8af32c40adacabdb28e84b1cc766cc0f20f6782.
Branch: fix/p0-lifecycle-20261026; isolated checkout: /home/server/mishka-fixes.
This phase follows the separately staged lifecycle work and unstaged audio work.

## Verified contract

- Canonical audio ingress uses an ephemeral ConversationContext for VAD/STT,
  then re-ingresses SpeechReceived. Bounded dialogue keeps a distinct context
  owned by session generation and profile. Prompt/profile refresh preserves
  turns, history and started_at. Handset-down, accepted profile change/removal,
  actual dialogue exit, reset and cleanup clear it. Late provider work is fenced.
- Existing five-turn/120-second bounds apply to the whole dialogue. A sixth
  turn or expired dialogue calls neither LLM nor TTS. In-flight work uses the
  lesser of the turn budget and the remaining dialogue budget.
- Generation/validation failure and exceptions use canonical DialogueFallback
  with PHRASE_104; limit/exit uses PHRASE_095 and leaves bear_dialogue.
  Recorded fallback dispatch gets a canonical owned handle and lease. Missing
  assets report unavailable rather than played. Both phrases are approved in
  the manifest and have WAV files under content/audio/voice.
- The story voice pause/stop/resume edges pass the application and graph;
  unrelated transitions remain denied.
- Independent review reproduced one leftover STT/VAD retry that directly
  played PHRASE_091 (reserved for adult-handoff failure) without a lease. A
  new red regression confirmed it. The narrow fix removes that direct play;
  STT/VAD retry remains the existing visual/diagnostic audio_retry.

## Test evidence

The ten inherited bounded-dialogue regressions were independently rerun
before the last fix: **10 passed in 2.71 s**. The new STT retry test failed
before its fix; the final dialogue file: **11 passed in 2.75 s**.

Final full headless suite from the isolated checkout cwd, using the existing
/home/server/mishka/.venv, absent /tmp MISHKA_ENV_FILE and
PYTHONDONTWRITEBYTECODE=1: **312 passed, 16 failed, 1 xfailed in 33.53 s**.
Before the narrow fix, the same checkout gave **311 passed, 16 failed,
1 xfailed in 33.37 s**. All 16 failure names were identical. Documented
clean d8af32c baseline: **272 passed, 17 failed, 1 xfailed**. Prior P0 audio
checkpoint: **301 passed, 16 failed, 1 xfailed**. The baseline comparison
does not label all historical failures resolved.

The 16 remaining failures are: canonical acceptance 1; GR01 exhibition 3;
GR04/05 1; GR06/07 1; GR09/11 1; S2S 7; Silero TTS 2. They are the same
16 reported before this phase. An initial run from the Windows SSH cwd
produced relative-path failures; it was discarded and repeated from the
checkout cwd. Staged and unstaged git diff --check both passed.

## Open decisions and limits

- max_session_seconds=1200 exists in application/settings but is not applied
  to dialogue. Product must decide whether it is a separate installation
  session limit; this phase did not activate it.
- STT/VAD retry has no audible generic phrase now. A phrase and canonical
  transition across all modes require product authorization; PHRASE_091 must
  stay specific to adult-handoff failure.
- Streaming generated speech beyond recorded fallback effects merits a
  separate end-to-end playback/cancellation review. Headless tests and
  provider barriers do not establish physical or browser audibility.
- No hardware, physical audio/LED, paid provider, real child profile or
  installed dependency was used.

Earlier staged and unstaged work was preserved. console.js was not edited in
this phase. No commit, push, merge, deploy, reset or clean occurred.
