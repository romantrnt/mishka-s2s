# Tasks: Reliable Runpod Demonstration

**Input**: Design documents in `specs/001-reliable-runpod-demo/`

**Prerequisites**: spec.md, plan.md, research.md, data-model.md,
contracts/demo-boundaries.md and quickstart.md, resolved by the official
`setup-tasks.sh --json` script.

**Tests**: Roman requested relevant regressions plus real user-flow checks.
Existing tests are baseline evidence to run or reuse only when they match the
final candidate; add a failing regression before fixing a reproduced uncovered
defect. Do not invent missing implementation from an unchecked verification task.

**Organization**: Setup → existing foundation → five independently testable
stories → full acceptance. All five stories are required, even though US1 is
the first useful slice. The original task boxes are the decomposition; the
dated outcome ledger below records actual/reused/blocked evidence. An open
composite task does not imply that its supplied implementation is missing.

## Format: `[ID] [P?] [Story] Description`

Every task has a sequential ID, an explicit owner/type, source files and
requirement mapping. "Conditional fix" means inspect/reproduce first, preserve
compliant code and finish with actual checks; it is not a declaration of a bug.
PM assigns the sole writer per overlapping product area. Chief reviews without
changing those files. [P] is used only for disjoint artifact work after its
prerequisites; it does not authorize simultaneous product writers.

## Path Conventions

All paths are repository-relative. In this file, `feature/` means
`specs/001-reliable-runpod-demo/`. Existing `docs/remote-stand-runbook.md` is
the operator outcome record. Do not add a parallel daily-report system.

## Observed outcomes — 2026-11-18

Candidate: application PID `16858`, source `9fe4f90`, branch
`codex/remote-device-simulator`; authenticated HTTP serves client SHA-256
`c28ed3fc6e63e95897b82c71e7ecb685e6726095bca9ffc7c3715cc45e82377a`.
The existing runbook identifies exact earlier PIDs and source transfers.
Current proof is software/browser acceptance, not physical hearing or clinical
acceptance. No further software blocker was established in the final controls.

| Tasks | Observed status and limits |
| --- | --- |
| T001–T005 | Current source/runtime/access readbacks passed. Protected HTTP/WS, private projection, Max ownership/expiry and unchanged external-fixture evidence are reused where their source is unchanged. No authorization scope was widened. |
| T006–T008 | Three guest and three permitted synthetic profile visits passed, including known-fact entry and clean endpoints. Entry-source evidence transfers across the isolated story/client changes; this does not establish physical RFID. |
| T009–T012 | All five games, seven root destinations, age samples, direct/contextual choices and exits have actual browser evidence. Real positive weather/schedule data was not exercised: missing provider/scope remains truthful unavailability, not a positive provider pass. |
| T013–T016 | Actual personal PCM passed for young/middle/older ages: 53.625/69.075/104.150 seconds. Young grammar has actual converter plus 44 boundary checks; whole validation, real Silero, pause/checkpoint/new lease/active exit passed. Final older control proved received/rendered wave movement after full resumed PCM. Earlier public-story active/pause-idle and independent Max-expiry controls are reused; no timer was extended. |
| T017–T020 | Software S2S and two earlier synthetic microphone/STT turns passed. Final client fault → same-visit useful answer → explicit microphone capture/accepting → clean hangup passed. T020/SC-004 physical microphone and two heard answers remain NOT OBSERVED; Roman has been asked to check. |
| T021–T026 | Existing desktop/390px, keyboard/flat view, overlapping input, reconnect/reload, actual app restart and next-visit evidence is retained with unchanged-source limits. Final client error/retry was inspected in the real browser. Protected Jupyter agent login/reopening passed; Roman's personal opening/reopening remains NOT OBSERVED for T025/SC-007. |
| T027–T029 | Chief independently reviewed each integrated source change, reproduced the phone-text race, and reviewed the corrected exact client. Relevant checks passed: 114 Python cases, canonical acceptance, four scheduler cases, eight affected Node suites and final diff checks. Steward's 07:40 and Chief's 07:43 final documentation/source consistency reviews passed; no broad unchanged suite is claimed rerun. |
| T030 | Ordered young game → successful story pause/resume → conversation → fault/retry plus clean recovery/new visit is recorded. Older browser and final client recovery artifacts close the remaining software controls. Whole mission remains open for the explicit hearing/personal-access observations; neither version 0.1.0 nor these software passes declare completion. |

Final artifacts: `runpod-young-mixed-16858.json`,
`runpod-young-mixed-recovery-16858.json`, `runpod-old-final-16858.json`, and
`runpod-final-guest-retry-16858.json` under ignored
`runtime/local-integration-20261117/`. Detailed outcomes and earlier source-bound
evidence remain in `docs/remote-stand-runbook.md`; raw personal text/PCM and
original WAV assets are not added to Git.

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Pin sources/candidate and fresh environment facts without changing
the existing runtime or expanding access.

- [ ] T001 Steward / source-and-candidate verification: confirm the latest PM/Chief decisions and local/live hashes for app/foundation/console/console.js and phone.js; reconcile supplied recovery status in feature/research.md and feature/quickstart.md, preserving explicit reading gaps and final candidate identity. Maps FR-023, SC-008.
- [ ] T002 PM / read-only runtime verification: inspect the current authorized Runpod, rate, persistent files, interpreter/providers, approved SSH and protected Jupyter status against deployment/remote-stand/launch.sh and scripts/run_remote_stand.py; record actual access/blockers in docs/remote-stand-runbook.md without secrets, purchases, route changes or a deployment. Maps FR-021, FR-022, SC-007.

## Phase 2: Foundational (Existing Shared Boundaries)

**Purpose**: Establish a trustworthy current baseline before accepting stories.
These are brownfield checks, not instructions to recreate infrastructure.

- [ ] T003 PM with Chief / causal discovery and regression baseline: trace ownership/lifecycle/cancellation/resource/error-recovery interactions through app/foundation/application.py, runtime.py, s2s.py and console/console.js, including previously unnamed failures; run affected existing console suites in tests/console_audio_regression.js, console_queue_regression.js, console_guide_regression.js and console_phone_regression.js plus relevant launcher/access tests, compare supplied candidate evidence and adapt coverage/tasks to demonstrated causes. Record outcomes in feature/tasks.md. Both reported recovery fixes have implementation supplied; final independent acceptance is pending. Maps FR-016, FR-017, FR-023, FR-024.
- [ ] T004 PM / boundary verification and conditional fix: preserve gateway/Origin/private-state and virtual ingress protection in app/foundation/remote_access.py, remote_devices.py and stand.py; reproduce any regression before changing it and use tests/test_remote_access.py, test_remote_simulator.py and test_remote_launcher.py. Map contracts/demo-boundaries.md protection/ingress clauses; no alternate bypass transport. Maps FR-001, FR-016, FR-019.
- [ ] T005 PM / identity-policy integration and conditional fix: preserve SessionPolicy.runtime_mode, SessionPolicy.identity_mode, ProfileContext.authorization_deadline and ProfileContext.private_values as canonically defined in feature/data-model.md while checking app/foundation/config.py, max_json.py, max_client_runtime.py and scripts/run_remote_stand.py; use tests/test_max_json_integration.py, test_max_json_stand.py and test_remote_simulator_max.py with the reviewed unchanged external fixture. Preserve Max ownership, independent expiry, guest recovery and urgent exhibition cleanup; do not modify Max's source/DB/certificates. Maps FR-001, FR-002, FR-010, FR-018, FR-019.

**Checkpoint**: Current trust, policy and evidence boundaries are understood.
Unavailable real access is a specific blocker to dependent live checks, not a
reason to stop independent local acceptance work.

## Phase 3: User Story 1 - Arrive with or without a profile (Priority: P1)

**Goal**: A coherent guest/profile entry and repeatable clean exit.

**Independent Test**: Guest and valid/unknown/expired synthetic profile → welcome
→ activity → hangup → clean new visit, with known facts not asked again.

### Tests and implementation

- [ ] T006 [US1] PM / focused verification: exercise tests/test_private_welcome.py, test_profile_onboarding.py, test_root_menu_profile_routing.py and test_personal_welcome_stand_integration.py for guest/known/invalid entry, unanswered greeting and repeated sessions; add only a missing reproduced regression before a fix. Maps FR-001, FR-002, FR-003, FR-018.
- [ ] T007 [US1] PM / conditional fix: inspect and repair only demonstrated arrival/cleanup gaps in app/foundation/private_welcome.py, canonical_contract.py and application.py, including all affected callers/tests; preserve explicit current exhibition policy and actual menu response instead of a decorative question. Maps FR-001, FR-002, FR-003, FR-018.
- [ ] T008 [US1] Chief / actual acceptance: run User Story 1 in real Chromium against the authorized candidate, check guest and synthetic profile plus failure/expiry and next visit, then record observation/limits in feature/tasks.md; local synthetic evidence alone does not establish live entry. Maps FR-001, FR-002, FR-018, SC-001.

## Phase 4: User Story 2 - Reach and use every activity (Priority: P1)

**Goal**: Seven root paths and all five games remain usable with contextual
controls, age adaptation and real output.

**Independent Test**: Root traversal plus each game round/hint/exit and at least
one valid immediate choice during each guidance phase.

### Tests and implementation

- [ ] T009 [US2] PM / focused verification: run relevant tests/test_game_menu_direct_choice.py, test_game_menu_held_entry.py, test_game_exit_affordance.py and test_immersive_game_content.py, test_immersive_game_engine.py, test_immersive_game_runtime.py, test_immersive_game_routing.py; check correct/wrong/delayed/answer-7/supported-overlap cases and existing curated deck coverage for all three age groups before any change. Maps FR-003, FR-004, FR-005, FR-006, FR-007, FR-014.
- [ ] T010 [US2] PM / game integration and conditional fix: preserve VirtualButton.button_id as canonically defined in feature/data-model.md while inspecting app/foundation/canonical_contract.py, scene_menus.py, game_engine.py, game_content.py and game_presentation.py; repair only reproduced shared selection/progression/lighting/exit gaps with affected tests, preserving the five games and all requested content. Maps FR-003, FR-004, FR-005, FR-006, FR-007, FR-014.
- [ ] T011 [US2] PM / route verification and conditional fix: trace all existing root destinations through app/foundation/menu_content.py, menu_capabilities.py, personal_story.py and schedule_weather.py; retain support/wellbeing/safety paths, truthful schedule/unavailability and return/repeat/interrupt behavior, and remove no function to simplify the demo. Reconcile the stale game skip-then-select recipe in docs/immersive-games-2026-11-16.md with current tested semantics rather than generalizing every menu. Maps FR-003, FR-008, FR-020.
- [ ] T012 [US2] Chief / real acceptance: exercise the seven root paths and all five games, age-band samples, meaningful hints/progress, supported button overlap, direct choice, contextual 7 and voice/button exits against the candidate; observe sound/light and record results in feature/tasks.md. Maps FR-003, FR-004, FR-005, FR-006, FR-007, FR-014, FR-020, SC-002.

## Phase 5: User Story 3 - Stories and separate timers (Priority: P1)

**Goal**: Fixed/personal stories remain usable across activity, pause and cleanup
without confusing idle with authorization.

**Independent Test**: Guest story beyond idle → pause/resume → just-before/at
120-second pause-idle; separate authorized-profile expiry and stale completion.

### Tests and implementation

- [ ] T013 [US3] PM / focused verification: run tests/test_engagement_fixed_story.py, test_engagement_waiting.py, test_fixed_story_queued_checkpoint.py and relevant tests/test_personal_story_service.py, test_personal_story_terminal_fence.py, test_personal_story_unavailable.py; cover cancellation, invalid generation, stale completion and known fixed-story assets before a demonstrated fix. Maps FR-008, FR-009, FR-010, FR-013, FR-014.
- [ ] T014 [US3] PM / story-idle integration and conditional fix: preserve EngagementPolicy.wait_seconds as canonically defined in feature/data-model.md in app/foundation/engagement_idle.py, application.py and canonical_contract.py; repair only reproduced ownership/timer gaps, keep active playback and paused waiting distinct from Max expiry, and rerun the boundary regressions. Maps FR-008, FR-009, FR-010, FR-014.
- [ ] T015 [US3] PM / content and terminal verification with conditional fix: check content/stories/fixed_manifest.json, app/foundation/personal_story.py and owned_speech.py for verified fixed assets, validated personal generation, truthful unavailable/retry/exit and stale-terminal cleanup; preserve original story assets and all associated callers/tests. Maps FR-008, FR-013, FR-014, FR-019, FR-021.
- [ ] T016 [US3] Chief / real acceptance: execute Story 3 and its separate guest long-play/profile-expiry sequences, pause announcement completion, resume checkpoint and late callback after exit/new visit; record actual timing and behavior in feature/tasks.md rather than presenting mocked time as a live 120-second observation. Maps FR-008, FR-009, FR-010, FR-014, SC-003.

## Phase 6: User Story 4 - Spoken exchange and recovery (Priority: P1)

**Goal**: Relevant audible answers, next-turn capture and bounded failure
recovery with explicit consent and truthful local safety.

**Independent Test**: Two real permitted microphone turns → cancel → recover,
plus denied/late permission and controlled recognition/response failure.

### Tests and implementation

- [ ] T017 [US4] PM / focused verification: select tests/test_headless_turn_taking.py, test_native_audio_stream_queue.py, test_websocket_audio_backpressure.py, test_browser_private_pcm.py, console_audio_regression.js and console_private_pcm_regression.js plus the affected local-safety tests; reproduce silence/return-to-capture or cancellation failures if present before adding a regression. Maps FR-011, FR-012, FR-013, FR-014, FR-020.
- [ ] T018 [US4] PM / media integration and conditional fix: preserve MediaOwner.identity and MicrophonePolicy.explicit_enable_required as canonically defined in feature/data-model.md in app/foundation/s2s.py, owned_speech.py, stand.py, console/console.js and phone.js; keep baseline Silero/VC off, exact terminal ownership, late-permission cleanup and explicit recapture, repairing only demonstrated gaps with their affected callers/tests. Maps FR-011, FR-012, FR-013, FR-014, FR-016, FR-019, FR-021.
- [ ] T019 [US4] PM / response/safety verification and conditional fix: inspect app/foundation/validation.py, input_safety.py, s2s.py and canonical_contract.py for ordinary relevant answers, bounded recognition/provider failure and existing local safety; preserve content validation, no false adult delivery and no new recording/notification backend, using relevant tests/test_local_safety_application.py and test_local_safety_audio.py. Maps FR-011, FR-013, FR-019, FR-020.
- [ ] T020 [US4] Chief with available real microphone/hearing / actual acceptance: execute Story 4 against authorized Runpod and browser; record two heard relevant turns, real latency/configured limits, cancellation, failure and recapture in feature/tasks.md. If microphone/hearing is unavailable, mark that precise part blocked and continue software checks; fixtures/text never satisfy this task. Maps FR-011, FR-012, FR-013, FR-014, FR-020, SC-004.

## Phase 7: User Story 5 - Panel, recovery and files (Priority: P2)

**Goal**: Preserve the liked working panel, trustworthy reconnect/reload and
another session after failure; Roman can use protected persistent files.

**Independent Test**: Desktop/390px and keyboard → queued-input sync loss →
restart/reload → another visit → reopen protected workspace.

### Tests and implementation

- [ ] T021 [US5] Chief / independent rendered baseline: inspect app/foundation/console/index.html, console.css, phone.css, phone.js and phone-model.js in actual Chromium at desktop/390px for existing controls, focus, keyboard/pointer gestures, sound/mic/tools, reduced motion and flat/WebGL fallback; record concrete failures in feature/tasks.md, preserving the supplied liked visual direction. Maps FR-015, SC-005.
- [ ] T022 [US5] PM / conditional UI fix: only if T021 establishes a gap, repair its smallest coherent path in app/foundation/console/phone.js, phone-model.js, phone.css, console.css or index.html with affected callers/tests; apply relevant design/accessibility skills, use original references and retain functional reachability instead of substituting a generic redesign. Maps FR-015, SC-005.
- [ ] T023 [US5] Chief / recovery regression reproduction: use actual Chromium plus tests/console_queue_regression.js and console_phone_regression.js against the supplied implementation to verify queued /stand/text at lost sync and same-epoch lower-cursor reload guidance; also exercise stale epochs/media, cleanup, refresh and real server restart when coordinated. Record candidate identity and actual evidence in feature/tasks.md. Maps FR-016, FR-017, FR-023, SC-006.
- [ ] T024 [US5] PM / observer/fault integration and conditional fix: preserve ObserverSnapshot.order and InputFault.delay_ms as canonically defined in feature/data-model.md in app/foundation/console/console.js, phone.js and remote_devices.py; if T023 or a fault check fails, fix dispatch admission/queue cancellation or reload guidance without weakening stale fences or terminal cleanup, rerun affected Node/real-browser checks and relevant tests/test_remote_simulator.py. Existing implementation may require no change. Maps FR-016, FR-017, FR-018, SC-006.
- [ ] T025 [US5] PM with Chief / protected file-access acceptance: verify Roman can open/reopen the existing persistent workspace through protected Jupyter and record approved SSH/Jupyter entry/recovery and precise remaining access limitations in docs/remote-stand-runbook.md; no route/permission/admin exposure is authorized by this task. Maps FR-021, FR-022, SC-007.
- [ ] T026 [US5] Chief / combined recovery acceptance: repeat Story 5 at desktop/390px after integration, including rapid/overlapping actions, virtual delay/disconnect/reconnect, hangup/reset, observer drop, reload and a new usable visit; record that no old action/capture/audio/light revives in feature/tasks.md. Maps FR-012, FR-014, FR-015, FR-016, FR-017, FR-018, SC-005, SC-006.

## Phase 8: Polish & Cross-Cutting Acceptance

**Purpose**: Close the whole mission on the actual delivered candidate. No broad
refactor, publish, deployment, new dependency or speculative security checklist
is created by this phase.

- [x] T027 Chief / independent final review: inspect the final integrated product diff and relevant affected checks for correctness, privacy, security, accessibility and shared ownership/lifecycle/cancellation/resource/error-recovery causes, actively investigating unnamed defects and missing interaction coverage beyond the example symptoms; bind findings/results to candidate identity in feature/tasks.md and docs/remote-stand-runbook.md. Keep mocked/live/physical evidence separate. Maps FR-019, FR-020, FR-023, FR-024, SC-008.
- [x] T028 PM / causal repair and final checks: resolve validated systemic findings across affected callers through assigned sole writers, extend tasks and regression/real-flow coverage for newly found interactions, restore intended behavior rather than relying on weaker fallback alone, rerun changed-path checks and scripts/verify_canonical_contract.py if canonical runtime changed, then inspect the final diff for unrelated files/secrets; update exact outcomes in feature/tasks.md. Maps FR-023, FR-024, SC-008.
- [x] T029 [P] Steward / source-and-evidence consistency: read-only compare feature/spec.md, plan.md and tasks.md to the final source decisions/evidence; report remaining gaps without claiming implementation or running converge before its prerequisites. This writes no product/runtime file and can overlap T027 review. Maps FR-023, SC-008.
- [ ] T030 PM with Chief / whole-demo acceptance and handoff: execute feature/quickstart.md, including three guest plus three profile visits and a mixed menu → game → story pause/resume → conversation → recovery/new-session chain; confirm protected files, usable entry/recovery instructions and honest remaining limits in docs/remote-stand-runbook.md. Mark only observed passes; any blocking missing voice/access/function keeps the demo incomplete. PM tracks product versions starting at 0.1.0 with exact commit/branch/push and real verification in the existing runbook; Chief verifies each candidate and Steward checks scope/evidence. Version 0.1.0 and its publication do not finish the mission: continue substantive implementation and independent verification until the original full reliable MISHKA outcome is achieved, independently of version numbers. Intermediate work may be committed without declaring a release. Preserve explicit failed/not-run/hardware limits, all required functions, GitHub branches and the running Runpod pod. Maps FR-001, FR-003, FR-005, FR-008, FR-011, FR-015, FR-017, FR-021, FR-022, FR-023, FR-025, SC-001, SC-002, SC-003, SC-004, SC-005, SC-006, SC-007, SC-008.

## Dependencies & Execution Order

- T001–T002 establish candidate/environment facts; T003–T005 establish the shared
  foundation. They do not prohibit useful read-only source review while live
  access is blocked.
- US1 T006–T008, US2 T009–T012, US3 T013–T016, US4 T017–T020 and
  US5 T021–T026 can each be evaluated from a known admitted state after the
  foundation. Their final combined entry chain depends on US1 acceptance.
- Within each story run the focused baseline/reproduction first, then a
  conditional fix with the sole assigned writer, then relevant final checks
  and independent actual acceptance. Never require a compliant test to fail.
- Serialize overlapping canonical/runtime/S2S/console changes through PM.
  Actual server restart, deployment and network changes require coordination
  and their existing authority; this task list grants none.
- T027 follows all story integration. T028 follows T027 findings if any.
  T029 is read-only and can overlap T027. T030 follows final review/checks.
  A later product fix invalidates its affected acceptance and needs only the
  relevant rerun and continuation.

### Parallel examples per user story

| Story | Safe work that may overlap an existing PM writer | Limit |
| --- | --- | --- |
| US1 | Chief prepares synthetic guest/profile/expiry scenarios | Do not mutate Max or shared running state concurrently |
| US2 | Chief checks disjoint content/test evidence for games and root routes | Shared canonical/menu writers remain serialized |
| US3 | Review fixed-story asset/catalogue evidence while timer fix is written | Real time/live expiry uses a coordinated candidate |
| US4 | Prepare permission/late-track browser fixture checks | Fixtures do not claim real hearing or real ASR |
| US5 | Chief captures current desktop/390px behavior while PM verifies file access | Do not run restart/cleanup against another live acceptance session |

### Requirement coverage index

| Requirements | Tasks |
| --- | --- |
| FR-001–FR-002 | T004–T008, T030 |
| FR-003–FR-007 | T006–T012, T030 |
| FR-008–FR-010 | T005, T011, T013–T016, T030 |
| FR-011–FR-014 | T009–T010, T013–T020, T026, T030 |
| FR-015–FR-018 | T003–T008, T018, T021–T024, T026, T030 |
| FR-019–FR-020 | T004–T005, T011–T012, T015, T018–T020, T027 |
| FR-021–FR-023 | T001–T003, T015, T018, T023, T025, T027–T030 |
| FR-024 | T003, T027, T028 |
| FR-025 | T030 |
| SC-001–SC-008 | T008, T012, T016, T020–T022, T025–T030 |

## Implementation Strategy

US1 is the first independently useful slice, not the accepted final MVP.
Continue through all five stories and final mixed acceptance. Preserve already
working behavior rather than rewriting it to make this list look productive.
Reuse actual matching candidate evidence, investigate real gaps, fix shared
causes and rerun only affected checks. A blocker is scoped to its affected
acceptance, with an independent useful next action; garage access is never a
blocker for this stage. No claim of unattended continuation or `converge`
completion is made by generating these tasks.
The initial 30 tasks, three-visit sample and listed symptoms are a work map and
starting probes, not a closed checklist, scope ceiling or stop condition.
Extend the work and acceptance sequences for demonstrated systemic causes and
newly discovered interactions across whole MISHKA without speculative rewrites.
Keep exhibition + max_json as current runtime evidence; clinical-policy context
is documented separately and changes only through deliberate source reconciliation,
not automatic deferral or silent redefinition.
