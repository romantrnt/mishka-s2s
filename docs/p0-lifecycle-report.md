# MISHKA P0: session ownership, urgent safety, playback identity

Date: 2026-10-26. Base: d8af32c40adacabdb28e84b1cc766cc0f20f6782.
Branch: fix/p0-lifecycle-20261026, worktree: /home/server/mishka-fixes.
Requirements: [critical architecture audit, 24 Oct 2026](https://app.notion.com/p/402a5543e6598191ad12dc11ba33ebd5?pvs=204).

## Verified invariants

1. Once handset-down owns the session generation, an older handler or STT result cannot dispatch audio, publish its old outcome, invoke the completion hook, or recreate the deleted session.
2. An open urgent incident remains urgent and in safety_handoff through ordinary speech, concern/request-adult follow-ups, RFID events, exhibition handset-down, generic SessionCancel/SessionReset, and generic application/stand reset. Only AdultResolved clears it. Follow-up intents are bounded and contain no transcript.
3. Two executions of the same phrase receive distinct playback leases. Callback A cannot remove playback B's lease or cancel B's watchdog.

## Regression evidence

tests/test_p0_lifecycle_regressions.py contains eleven headless tests. Before the fixes, urgent could become concern, a delayed help handler could restore deleted state after handset-down, and callback A could cancel B's watchdog. The new tests exercise clinical and exhibition hangup races, late feed_audio, repeated phrase identity, and generic reset. The final new-test result is **11 passed**.

Implementation:
- app/foundation/application.py: per-session event revisions and physical epochs, fences before effects and after provider awaits, stale STT rejection, cancellation of both audio owners, conditional watchdog cancellation, preservation of urgent during generic reset.
- app/foundation/canonical_contract.py: urgent lifecycle guards, exhibition handset-down preservation, bounded follow-up intent metadata, distinct playback lease per execution, urgent-safe AudioFailed projection.
- app/foundation/stand.py: generic stand reset respects a blocked urgent reset.

| Fix | Regression | Result |
| --- | --- | --- |
| Session generation and post-await fences | clinical and exhibition help/hangup; late STT in both modes | 4 passed |
| Urgent priority across speech, RFID, handset, cancel, reset and delivery acknowledgement | urgent incident, rejected RFID/private context, exhibition handset/reset, application reset, delivery ack | 5 passed |
| Unique repeated playback lease and watchdog ownership | repeated phrase lease; A completion starts B | 2 passed |

No global lock surrounds provider work. Exceptions remain observable through existing diagnostics; stale work is returned as stale_session_generation.

## Test comparison

Relevant set (test_audio_closeout.py, test_gr02_exhibition.py, test_canonical_acceptance.py, test_canonical_graph.py, new P0 tests): **35 passed, 2 failed**. Both failures were reproduced on the untouched source checkout: the mock has both ringtone and greeting commands (test expected one), and the story test expects playback with handset down.

Final full headless pytest run uses the existing /home/server/mishka/.venv via an ignored .venv symlink in this worktree, an empty /tmp/mishka-p0-no-env, no device or paid provider. Baseline from setup manifest: **272 passed, 17 failed, 1 xfailed** on installed checkout; its 17 failures were also confirmed on clean HEAD in the same environment. Final full result: **283 passed, 17 failed, 1 xfailed**. The failure list is the same 17 baseline cases; no new failing case was introduced. A first worktree-only full run showed one additional smoke failure because the fresh worktree had no .venv/bin/python; the symlink resolved that setup artifact without changing the environment.

No setup compatibility diff, .env, keys, profiles, or runtime data were copied. The source checkout and its setup changes remain untouched. There was no deployment, push, merge, physical sound, or LED test.

## Open P0 risks and next group

The audio adapter contract is still incomplete. Physical playback currently reads the current mutable lease when its callback finishes, so an old physical callback could claim a newer execution of the same phrase. The browser adapter and physical adapter also report different completion stages. Story pause does not persist the physical checkpoint, long verified stories exceed the fixed 60-second timeout, general AudioFailed lacks an audio stop, and verified browser stories omit the expected asset path. The next P0 group should pass an immutable (session, generation, playback_id) handle into every mock/browser/physical playback and callback, define accepted/started/position/completed/cancelled/failed events, persist pause checkpoints, and derive watchdog limits from verified asset duration. It needs adapter parity tests and later physical acceptance with an observer.

AdultResolved is an explicit event, but the available contract does not bind it to an authenticated adult identity or an incident ID. That authorization and delivery evidence require a dedicated safety delivery review before production acceptance.

The current application still has nested canonical event processing for simulated adult acknowledgements. This patch keeps that child event in the parent's revision; a dedicated sequential per-installation event owner remains a follow-up architecture step. Hardware readiness, audible playback, capture quality, and production admission were not asserted by these headless tests.

## Diff summary

Phase-one staged diff: 5 files, 540 insertions, 13 deletions (including 330 regression-test lines and this report). The subsequent audio work remains unstaged and is documented separately in docs/p0-audio-report.md. No commits, push, merge or deploy.

## Independent review follow-ups closed in this phase

A gated reset test showed that a new lift could create a fresh state while old cleanup waited, then old reset deleted that state. New ingress for the same session now waits for the reset owner to finish cleanup and publication; callback failures arriving during reset are stale. This barrier is per session and does not hold a lock around generation.

A clinical RFID test showed that rejected tag B left canonical incident/identity A unchanged but replaced the application's private profile cache with B. Profile cache updates now occur only after an accepted identity transition; an unchanged urgent incident retains A in both canonical state and the derived audio/system context. The two tests failed before the fix and pass afterward.
