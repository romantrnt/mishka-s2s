# Candidate Validation Guide

This guide is a validation recipe, not evidence that any scenario passed.
Record actual outcomes against tasks.md and update the existing
[remote stand runbook](../../docs/remote-stand-runbook.md) for final operator
handoff. Do not duplicate secrets or private content in either document.

## Prerequisites and candidate identity

1. Confirm repo, branch, existing diff, active writers and the final candidate's
   code/assets hashes. Current feature branch is codex/remote-device-simulator.
2. PM verifies the existing authorized Runpod, current rate at most $0.35/hour,
   persistent assets, actual process/environment and configured HTTPS origin
   through permitted read-only access. Do not start another pod or deploy from
   this guide. Use existing separately authorized runtime ownership.
3. Reuse the current baseline Silero/VC-off launcher and unchanged Max synthetic
   source. Confirm exhibition + max_json + simulator and production_ready=false.
   Select the existing Python environment; dependencies should not be reinstalled
   or upgraded merely to execute this plan.
4. Use synthetic profiles and non-private test utterances. The external Max
   test fixture must be the reviewed unchanged source/ZIP, not a clinical DB.

## Static Spec Kit prerequisites

From the repo root, verify the feature pointer and all required design files:

```bash
SPECIFY_FEATURE_DIRECTORY=specs/001-reliable-runpod-demo \
SPECIFY_FEATURE_NO_PERSIST=1 \
bash .specify/scripts/bash/check-prerequisites.sh \
  --json --require-spec --require-tasks --include-tasks
git branch --show-current
git diff --check -- .specify specs/001-reliable-runpod-demo
```

## Focused regression checks on the final affected state

The commands below are a starting set selected by the changed paths; they are
not a demand to rerun the entire repository without new evidence. Set
MISHKA_PYTHON to the absolute existing canonical Python executable. For Max
integration, set MISHKA_TEST_MAX_BUNDLE privately to the reviewed source/ZIP;
missing fixture means the related integration is not verified, never passed.

```bash
node tests/console_audio_regression.js app/foundation/console/console.js
node tests/console_queue_regression.js app/foundation/console/console.js
node tests/console_guide_regression.js app/foundation/console/console.js
node tests/console_phone_regression.js app/foundation/console
"$MISHKA_PYTHON" -m pytest -q \
  tests/test_game_menu_direct_choice.py tests/test_game_menu_held_entry.py \
  tests/test_engagement_fixed_story.py tests/test_engagement_waiting.py \
  tests/test_remote_simulator.py tests/test_remote_launcher.py \
  tests/test_remote_access.py tests/test_remote_simulator_max.py
```

For changed identity, speech, story, game or safety code, select relevant
existing tests cited in tasks.md; run a new focused regression only for a
reproduced uncovered failure. Check the project canonical contract when its
shared runtime changes:

```bash
"$MISHKA_PYTHON" scripts/verify_canonical_contract.py
git diff --check
```

## Real browser and remote-service acceptance

Chief uses actual Chromium against the exact candidate and approved HTTPS path.
A local/mock browser run covers browser mechanics only. Never substitute a
text/PCM fixture for microphone permission, audible answer or real-provider
latency. If real microphone or hearing is unavailable, record that SC-004 is
blocked and its precise remaining human/device action; continue independent
software checks without claiming completion.

| Sequence | Required observations |
| --- | --- |
| Entry | Unauthenticated HTML/state/assets/WS rejected; permitted login; honest warmup/ready; guest then demo-1/demo-2/unknown/archive identity outcomes; known facts not re-asked |
| Root paths | Visit all seven destinations; usable choices, return, cancellation, interruption/repeat and truthful capability/unavailability; no unanswered greeting |
| Games | Enter and fresh-select during invite/narrating/repeat; held-key guard; five games, wrong/correct/delayed answer, answer 7 versus exit, supported overlap, voice/button exit and cleared light |
| Adaptation | Youngest/middle/oldest curated content, stable colors/pictograms, pattern progression inside one session and variation on re-entry; no medical suitability claim |
| Stories | Verified fixed story and personal-story validation/unavailability; guest active beyond 120s; resume from pause; just-before/at-120s paused idle after announcement; separate Max-expiry case |
| Conversation | Explicit sound/mic permission; ordinary non-private question, heard relevant answer, second turn; cancel output; denied permission/recognition or response failure; capture recovery and no self-listening loop |
| Recovery | Repeated rapid actions, queued text before sync loss, input delay/disconnect/reconnect, reset/hangup, same-epoch lower cursor, stale epoch/callbacks, page refresh and actual server restart; explicit reload and another clean session |
| Rendered UI | Desktop and 390px, pointer/keyboard/focus, seven keys/receiver, sound/mic/tools, flat-view/WebGL fallback, reduced motion and no blocked essential control/overflow; preserve liked visual direction |
| Files | Roman opens and reopens protected Jupyter/persistent workspace via approved route; agent SSH path status recorded; no secret or admin listener exposed |
| Repeat | Three guest and three permitted profile visits from welcome through a chosen activity and clean exit, then one mixed whole-demo sequence spanning games, story, conversation and recovery |

Do not restart the shared service or apply network changes without PM's
coordination and applicable authorization. Reviewers can prepare/reproduce
local browser mechanics while remote access is blocked.

## Handoff evidence and limits

For each sequence record pass/fail/not-run/blocked, candidate identity, environment,
observed behavior and material limit in tasks or the existing runbook. Record
actual latency, deadline and failure recovery; historical 3.046s HTTP/WS and
1.83s fixture timings are not current acceptance or proof of hearing.
Chief's independent review must refer to the final integrated candidate; after
a fix rerun only affected checks plus the relevant mixed continuation. Full
handoff requires every story and SC-001–SC-008, a usable entry guide and file
access, with hardware/clinical limitations explicit. Run convergence only after
implementation against generated tasks has occurred.
