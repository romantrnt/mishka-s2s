# MISHKA P0 audio execution contract

Date: 2026-10-26. Base: d8af32c40adacabdb28e84b1cc766cc0f20f6782.
Branch: fix/p0-lifecycle-20261026. Worktree: /home/server/mishka-fixes.
Requirements: Notion critical architecture audit, 24 Oct 2026.
This is phase two, unstaged after the separately staged session/safety phase.

## Contract and tests

Each tracked playback has an immutable session ID, physical session generation,
unique playback ID, unique canonical lease, content ID, and cancellation token.
Dispatch, UART/WebPanel command, physical/browser callback, watchdog
correlation ID, and lifecycle projection carry the execution identity.
Canonical decisions give distinct leases to repeated plays and every declared
audio effect in a multi-audio decision. Support callbacks use the actual
recorded phrase ID.

| Fix | Main files | Test evidence |
| --- | --- | --- |
| Repeated A/B phrase executions remain distinct through canonical idempotency; duplicate and late A cannot complete B | application.py, canonical_contract.py, stand.py, domain.py, adapters.py | Real physical callback, repeated physical error, browser completion route, watchdog regressions passed |
| Late lifecycle A cannot mark B; unidentified lifecycle is ignored | application.py, stand.py, console.js | Direct projection and authenticated browser route regressions passed |
| Long verified stories and resumed stories use WAV duration plus ten seconds; AudioFailed/timeout stops output | application.py, canonical_contract.py, stand.py | Duration, resume, actual stop regressions passed |
| Physical and browser pause save millisecond checkpoint; resume starts from it with a new execution | application.py, canonical_contract.py, stand.py, console.js | Physical pause, browser checkpoint/owner, fake Audio execution passed |
| Callback POST retains the original command session when UI selection changes | console.js | Fake transport execution passed |

There are 17 new Python audio tests in tests/test_p0_audio_contract.py.
tests/console_audio_regression.js executes the actual WebPanel functions under
local Node with fake AudioContext, source stop/ended events, late old callbacks,
repeated same clip, pause/resume, hangup generation, and transport payload.
It passed; node --check passed. WSL has no JS runner, so Node used temporary
copies of these two repository files on the local host. No dependency was
installed and no real sound was played.

Relevant Python suite (P0 audio/lifecycle, audio closeout, GR02, canonical
acceptance/graph): **53 passed, 1 failed**. The remaining failure is a
baseline case. Full headless suite on final code: **301 passed, 16 failed,
1 xfailed**. Clean HEAD baseline in the same environment: **272 passed,
17 failed, 1 xfailed**. All 16 remaining failures were present at baseline.
The former visual-welcome handset-lift failure now passes after the canonical
multi-effect lease fix.

The old timeout test now expects zero pending commands after actual stop.
A source-text WebPanel assertion was updated for the new callback identity;
the separate fake Audio test verifies behavior. A legacy completion missing
playback_id is enriched only if its unique lease, content, and generation
match the active execution. Lifecycle status has no such fallback.

Tests used the existing /home/server/mishka/.venv without modifying it, an
empty MISHKA_ENV_FILE, temporary cache, and no real profiles or runtime data.
There was no physical sound, LED, USB, paid-service, or real-browser test.

## Remaining P0 work

A supervised device/browser acceptance should cover long stories,
pause/resume, repeat, hangup, stale callbacks and audible output stop.
The fake Audio and API tests do not prove real-browser timing or audibility.
Browser postQueue serializes checkpoint before subsequent resume button
mutation; this still needs observation in the real browser.

AdultResolved lacks authenticated adult identity and incident binding;
safety delivery authorization and persistence require a dedicated review.
Bounded generated/dialogue text streaming still needs its own playback
ownership and cancellation review. Games, new story content, Windows audio,
models, installed environment and live runtime were not changed.

## Diff and custody

Phase-two tracked unstaged delta: 8 files, 348 insertions and 164 deletions,
plus 631 lines of Python audio tests, 118 lines of JS fake Audio tests, and
this report. Phase one remains staged: 5 files, 540 insertions and 13
deletions. No commit, push, merge, deploy, reset or cleanup occurred. Original
checkout and setup compatibility changes remain untouched.
