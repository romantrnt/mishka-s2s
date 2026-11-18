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

## Active Stage 1 extension — 2026-11-18

Roman's latest direction reopens substantial software work. Earlier passes below
are a source-bound baseline, not completion or a reason to wait for personal
hearing/access checks. Future user narratives are not prerequisites. The matrix
remains an investigation aid rather than a scope limit.

| Work | Confirmed baseline / intended observable result | Sole writer / independent check |
| --- | --- | --- |
| T031 Conversation and games | `word_game` really uses S2S; normal dialogue cannot yet offer/confirm/launch games, and game exit always returns to root. Add actual allowlisted model proposals, owned confirmation, preserved caller RAM and contextual return; reject stale/duplicate consent and clear on cleanup. | contextual_game_flow / Chief |
| T032 Game controls and waiting | Quiz can use answer 7; preserve it with short-release scoring and a consumed long-release exit based on actual HID edges. Clarify word-game round budget and provide bounded gentle reminders without changing engagement/authorization deadlines or treating system speech as user activity. A reminder must not invalidate speech admitted before its deadline while mandatory safety classification is pending, including a spoken quiz exit. | contextual_game_flow / Chief |
| T033 Indicator presentation | Existing 18 channels already separate digits and emblems. Dim readable OFF marks and distinguish each ON channel in flat/3D views; cover combinations, rapid updates and session reset. No physical-brightness claim. | menu_guidance_design_review / Chief rendered matrix |
| T034 Snapshot ordering | Background lighting frames can share journal cursor with a delayed HTTP snapshot. Give all panel snapshots an independent order so late HTTP cannot roll back newer WS frames, while idle animations still move. Serialize detached capture with its revision; identify a restarted service and offer reload without mistaking delayed same-instance frames for restart. | PM / Chief reproduction and review |
| T035 Other product chains | Reproduced: root-3 safe questions repeat a listening invitation without calling dialogue; advertised day-menu voice queries other than `where_now` are ignored; back from queued typed date leaves the audio owner active. Repair through existing bounded speech/typed-provider ownership and cover affected cleanup. Apply the reviewed narrow current-question wellbeing reassurance before semantic severity merge; only verified safe current answers advance, preserving urgent/concern/failure/stale protections. Personal-story references must not steal button 7 from a later help question or root menu; preserve genuine story back/pause/resume and public checkpoints. Continue discovery beyond these findings. | contextual_game_flow after core flow / Chief |
| T036 Integrated acceptance | Browser Runpod: word game → actual scoped S2S reply → same game/round/next turn → exit; dialogue → proposal/confirmation → game → finish/cancel → caller; rapid input and transition interruptions; faults and new-session cleanup. Inspect actual sound ownership, lights, guide and capture eligibility. | PM integration / Chief live acceptance |

Decisions are grounded in current code, not the illustrative 10-question,
20-second or 60-second values. Preserve Silero without voice conversion,
120-second engagement semantics, independent Max expiry, active story playback,
all activities, data and access controls. Do not stop the pod or delete branches.
Real microphone/hearing and physical board behavior remain distinct evidence.

The preceding reviewed V5 source `33dc48bfef038e059b100ec5c5d3196c560de248`
was deployed as application PID `18670`. Its guarded rollout verified all 16 runtime hashes,
readiness and served client `50615a1b…`; it retained the environment, private
assets and running pod/Ollama. Chief owns the active actual-provider/browser
acceptance. T031–T035 have implementation, focused local checks and independent
review; their integrated hosted results and T036 remain open until recorded in
the [runbook](../../docs/remote-stand-runbook.md). Earlier passes below are not
relabelled as evidence for these new paths.

The initial hosted pass found repeat-menu interception, incomplete compound
consent and internal-event identity/order defects. The follow-up corrects the
shared causes, including existing theme requests and adult-help priority;
215 linked cases and independent review passed on the v4 source. The new
VAD/STT reminder/cleanup correction passed 30 final-source affected cases and
independent replay before V5 deployment; its five diagnostic-code additions
passed 25 projection/privacy cases. Chief replayed the affected native paths
against PID `18670`; the initial first-turn success does
not establish multiple turns, and ordinary exit is separate from answer-7/hold
threshold acceptance.

The preceding PID `18180` WAV request → Whisper → quiz proposal → typed
compound consent → real quiz → manual exit → caller passed. Its later word
party completed four successful real-model turns with one validation retry,
natural final drain and another caller answer. Actual VAD/STT reproduction
established the reminder race fixed by V5. Isolated actual Whisper replay found
the three old eSpeak fixtures misrecognized; they establish neither spoken
consent/word routing nor a false safety reaction. Human microphone/hearing,
and hosted reminder-boundary replay remain separate gates. PID `18670` later
passed real short-release answer 7 and a fresh 5.25-second hold/release exit on
actual quiz questions, with no deck injection; source-bound evidence is in the
runbook and `runpod-stage1-v5-older-18670.json`. Qualified replacement Silero
fixtures then passed one continuous actual Whisper/model request → separate
WAV consent → quiz → caller chain, with no typed consent. This is synthetic-WAV
software evidence, not browser capture or human microphone/hearing. The word
boundary fixture reached the model but produced an age-budget retry; successful
word progress and a full hosted causal deadline crossing are not claimed.

### Conversational access addition — deployed in V6, integrated acceptance active

V6 source `6fcc7ca6fe5892b5eac236b4c511fdbdde07cbda` was deployed as
application PID `19481`, instance `5f755e980e04bc02`. All 23 runtime hashes and
the served client `2fce8160…` matched the committed candidate. The one guarded
app-only rollout preserved the pod, Ollama, environment/context settings,
Silero without voice conversion, protected access and private assets. Initial
readback was `READY`, handset down, no pressed keys or browser owner. Chief
owns the linked browser/provider pass; deployment does not complete Stage 1.

The last successful V7 checkpoint was source `0bdd692d1453d0cde8e6d62a9719804199c9b51a`,
application PID `20029`, instance `e17f1515cd2bd668`. One guarded app-only rollout
after Chief's clean release replaced only three independently reviewed prompt
files, verified all 23 hashes and served client, and preserved environment,
Silero/disabled conversion, pod, Ollama and private assets. The mutually exclusive
speech/action format and young single-thought instructions passed 66 focused
cases and independent boundary review; actual model replay is active. Local
hold-seven and weather-scope work is not deployed in this checkpoint.
The first V7 native persona and fake-microphone quiz request repeated the
age-budget and mixed-action failures. These remain open despite source/mock
review. Immutable-source delivery inspection confirmed the final instructions reach
Ollama. A serialized actual-model synthetic probe confirmed mixed speech plus
action, without literal escaping defects. Bounded structured-output support
checks precede further model changes; parser, age and semantic limits stay.
Separately, frozen local ingress reproduced full-catalogue WAV hashing on each
capture guard evaluation: 213 ms of audio took 20.45 seconds. A scoped committed
descriptor correction `3c03497` is independently reviewed and pushed, with
selected asset verification and invalidation retained. Local causal replay
reduced twenty frames to 0.03 seconds with zero repeated verification; actual
early browser voice/Whisper acceptance after deployment remains open.

Historical recovery state: V11 PID `22134`/instance `57242870c1ddf223` ran
source `db0a744` with strict READY and all eight real startup stages passed,
correlation `b62ccf09-b758-4263-8395-bec3ebe4b384`. All27 runtime hashes and
served console/phone clients match the reviewed Git source. Helper `22129`
ran once; no rollback or Pod/Ollama restart occurred. Chief owns the active
native browser/provider pass; the actual slot is explicitly serialized. Human hearing,
physical board and real external data remain separate open acceptance.

Previous V12 checkpoint: source209c3f81034673d5a37bcff551f75fe0ac7c52e7,
PID22776, instancee64ad25fc72cf3d9. Guarded helper22771 ran once with the
strict-ready V11 baseline, six reviewed runtime replacements/full27, archive
111891B/SHA95303178b3cdff27212379a44207f273f768cb48f61744ae4b18acffbe8a5b25.
Independent fresh readback passed strictREADY/listener/full27/served console,
phone and3D modelff03, Silero/VCoff, no clients/down/keys[]/18dark. No rollback.
Startup E2Ea901903c-3062-4ed8-9274-34bcf7e8d8bb all8PASS, server bounded queue
1356.11ms, LLM→firstPCM1354.65ms and ingress→complete1762.85ms; final playback
silent, not browser/acoustic latency. The separate
cold public-WAV core probe93b8ec10 also passed8/8 in6.613s with the exact new
quality parser. Chief owns the native slot; PM actual/browser/provider0.

Preceding V13: source45bb465db25395697b08a868743dfbf1ac2a0172/PID23547/
instance42d641725b792ede. Helper23542 ran once; archive143578B/SHA45e5808c68e415f093bccfd02935646e5f1ffb0ca75a8089c09196d54655a338,
six runtime replacements/full30 verified before and after launch. StrictREADY
and actual E2E82337612-82c0-4576-abb3-d43413dadfc7 passed8/8; silent startup
queue2064.37ms is not browser latency. Silero/VCoff, served3JS, owner0/down/
keys[]/18dark were independently checked, then actual slot released to Chief.
No rollback or Pod/Ollama restart. Pagination/auth/nonvoice candidates remain
local work; they are not in V13.

Current live V14: source `263cac19c6b9dee6e485d39b16ef10a26738bf62`,
PID `23949`, instance `67b2f42553e4f8fd`. Guarded helper `23944` ran once;
six runtime replacements and all 31 hashes matched the reviewed Git package.
Strict READY and actual startup E2E `41602a17-07a2-4e53-91f4-cd893da32a87`
passed 8/8 with silent final playback. Paging, truthful dialogue hints and
fresh authorization replacement are included. Final auth focused tests passed
96 cases and independent counterexamples became GREEN. External unchanged Max
integration had 32 passes/two subtests and one separately reproduced baseline
failure: route/time requests are remapped to a doctor list; this remains open.
Explicit screen Start/End, panel route and full four-way auth/hook acceptance
remain active work. Chief owns the bounded native follow-up; PM has no live
writer or model job. No Pod/Ollama restart or physical/human acceptance claim.

First V12 native follow-up remains HOLD: early known synthetic microphone
input yielded catalogue clarification rather than Aibolit (no longer
transition_rejected), and a fresh harmless young persona question produced
provider_or_validation_failure, correlated to mini_llm_review. Its lost raw
candidate/verdict do not prove a false block. Exact causes are being traced; neither the
source tests nor startup/controlled model examples replace these live failures.
No human microphone capture or physical acceptance is claimed.

Historical V8/V9 timeouts have no retained candidate body and no exact cause.
The instrumented V10 candidate `21653` captured a clean JSON terminal followed
by the bridge's `age_word_budget_exceeded`, before semantic/TTS. Its proven
source mismatch was older-schema permission for three sentences that the
bridge rejected. The final11-line grammar correction keeps 8/18/30 global
word caps, younger bytes and mandatory semantic checks. It passed93 focused
cases,4 independent causal groups and one actual public-WAV core probe8/8
before V11 deployment. Raw21653 output was lost; no later probe is presented
as that response. Full deployment/failed history and measured boundaries are
in the existing runbook.

The JSON/warmup correction and positive weather scope passed295 combined
local cases and independent source review. Weather `d48bc15` has scoped
independent CLEAR; `557c7d6` adds common rain/snow/degrees aliases with final208
local and28 independent route cases. Both are included in V11. Real weather
API data is unavailable; Max provisional fixtures are not clinical acceptance.
Broader linked browser acceptance remains open until Chief records it.
V11 actual youngest-profile quality is on HOLD: two generator responses glued
words and the real mandatory Mini allowed the malformed example at1.0.
A scoped literal-orthography rubric is being repaired without weakening safety,
word limits or replacing the single semantic call. It requires actual negative
and natural-positive model evidence, not another mock acceptance claim.
Subsequent actual V1/V2 rubrics blocked several corruptions and preserved
natural controls, but both still allowed the observed adjective/noun glue;
quality remains HOLD. A Russian producer instruction yielded a natural eight-
word persona response; the overlong verbatim request still glued words and
must be rejected, not repaired. The final structured source is now included in V12; its native limits remain below.
The final structured quality-first source passed independent review and298
affected local checks. Its three request bodies exactly match actual tested
prototype bytes (two adjective/noun negatives blocked, actual new natural
persona allowed). Eight additional actual source-built controls all passed,
including word-game final turn, names/hyphenation, normal solid spelling and
inflection; no retry, extra model pass or changed safety threshold. This is
bounded synthetic model evidence. V12 actual startup passed; native acceptance remains pending.

Catalogue safety/projection P1 passed26 new +92 existing local cases and
independent actual Application→Graph review. Only trusted classification grants
support/safety; ordinary launches stay in their own catalogue. Rejected state
publication now matches authoritative rollback. Native early-choice failure
remains open until replay with actual updated source and causal receipts.

Task-local warmup playback isolation passed independent review and is deployed in V12:88 local
checks plus3 mock-wire terminal controls. It prevents preadmitted user queues
or empty probe cleanup from capturing/stopping the wrong audio sink. Full
manual maintenance provider/admission/VAD concurrency has a source-cleared
candidate in45bb465, now deployed inV13:134 affected checks plus51 follow-up,
independent review and two independent local ASGI occupancy/entry controls.
Cancelled native worker occupancy remains busy until actual completion;
remote model termination after HTTP cancellation is not established.
V11 native hold early restore/latched owner-release/mixed-pointer/hangup cleanup
and two long-source stop receipts passed. V12 native held3D visibility passed.
Older short7 scoring remains NOT_PROVEN because the QA question map was stale.
Actual synthetic-mic request→offer→voice consent→quiz passed; free-light,
repeat-light and odd-one-out returned to the same dialogue. Final word-game
completion versus immediate public projection was traced to premature QA input:
the server had returned to bear before QA7, and the queued bear frame arrived
before root. Pinned-source local publication replay was clear; QA must wait
for returned-owner publication. Actual delivery latency remains unexplained.
Linked catalogue capture/ASR choice and full conversation/game acceptance
remain open. Three local capture comparisons localized known-fake waveform
alteration before the JS resampler; this does not justify a global microphone
processing change or establish human microphone behavior.
The cross-clock47.6second latency claim was withdrawn with paired clock
bounds; same-server STT was141.6ms. Root investigates actual transition reasons.

Roman's next addition extends the same investigation. The preceding inventory:
all five immersive games accepted model offers, but no generic owned voice
chooser existed. All five fixed WAVs were present, including Aibolit; the old
`fixed_neznayka_v1` ID now means **Telephone**, not an alias for Neznayka.
Early button choices worked; early spoken catalogue selection did not. Story
finish/exit lacked a conversation caller. V6 corrects these boundaries; its
native results remain separate from the local implementation evidence.

Max's current contract supplies `doctors_today` code/name and a source visit
date, explicitly not a timed plan, sequence or room. MISHKA previously dropped
that list and granted only personalization. V6 retains bounded source-qualified
card facts under the same grant; it still has no actual appointment lookup
behind `schedule_ref`.
Do not present the list as a next appointment or modify Max/client/database.
The preceding launcher forced demo. Read-only
PID `18180` readiness confirmed provider `demo` and no configured key or
coordinates; this is effective configuration, not a search of unrelated
credential stores. V6 binds actual weather settings and checks observation
freshness; current readiness reports `yandex` with no configured key or
coordinates. A real positive weather-provider response remains unverified.

Historical V6 native negatives await verification against V11: the actual age-two persona question
exceeded the applied eight-word cap; real browser fake microphone capture and
STT succeeded, but the first quiz request failed the strict mixed-action
parser and the later consent opened a generic catalogue. This does not pass
the desired owned offer/confirmation chain. Effective-prompt inspection found
competing format instructions; the independently reviewed prompt correction is
now deployed in V7, with actual efficacy awaiting replay. Evidence:
`runpod-stage1-v6-games-capture-19481.json`. No safety or age boundary is relaxed.

Roman's next immersion addition does not interrupt active checks. After the
current work, investigate calm hold-seven progress using the actual three-field
lighting protocol, readiness latched until release, safe early/uneven release,
restored game lighting and accessible spoken instruction. Preserve short-seven
answers and distinguish web evidence from a physical-board check. Apply the
same clear, patient feedback principle across other first-use paths for ages
one to seven, including children with ASD. Max data are explicitly test and
provisional; validate scoped integration, guest/missing-data behavior and the
existing contract without changing Max code/database or inventing real facts.
The weather key and coordinates are explicitly absent. Prepare configured API,
missing/fault and sourced city-name handling; no unconditional Saint Petersburg
or mandatory manual city input, and no real-API claim before actual access.
Local provider-stub routes additionally confirmed that tomorrow/yesterday/weekend
requests lose their temporal scope and receive current observations. The same
bounded weather correction must explain unsupported periods before an unrelated
fetch; it must preserve ordinary/current requests and existing safety/ownership.

| Work | Observable result / current boundary | Ownership / independent check |
| --- | --- | --- |
| T037 Catalogue gateway | All five games and verified story assets reachable by precise or general conversation requests; early voice/button choice launches once; ambiguous/unknown names clarify. Restore a valid caller across chooser, playback, pause, finish and cancellation. Include actual capture/VAD/STT admission, not only text routing. | contextual_game_flow after reviewed audio freeze / Chief |
| T038 Sourced day information | Section 7 and conversation use actual scoped facts and honest missing-data responses. Explicit accepted factual requests reach the typed executor before model generation. Add bounded private card doctor names/date with the existing personalize grant only; describe a source-qualified card list, never a next appointment or invented room/time. Reject invalid DTOs, stale identity and late results; check source date before saying today. Character/location questions stay ordinary conversation. Context-sensitive 7 remains intact. | contextual_game_flow: coupled routes/rendering; console_recovery_fix: max_json.py/identity.py DTO; Chief independent privacy/date/expiry review |
| T039 Weather configuration, location and freshness | Bind existing canonical/legacy weather settings without replacing explicit voice/private configuration. Current/no-key/error/stale observations are distinguished; no paid calls or fake facts. Missing/fault local checks pass; actual API/city naming remain unverified. Preserve an explicit requested-location scope so fixed configured coordinates cannot silently stand for a named other city. | console_recovery_fix: request-local scope, DAY availability and aliases independently reviewed and deployed in V11; real API/city data remains unavailable / Chief native |
| T040 Natural safe dialogue | Establish precise native fallback reasons; align actual age caps and final-word instructions; remove reproduced harmless-noun/persona false refusals while preserving real privacy, medical and safety checks. Validation 82cb42f4 and final prompts/core are independently cleared and deployed in V6; actual persona/model behavior remains a native gate. | contextual_game_flow: prompts/coupled routes; menu_guidance_design_review: validation.py and dedicated tests; Chief / independent review |
| T041 Hold-seven feedback | Implementation `990d394` and independent core/UI review passed; included in V11, with scoped browser feedback evidence. A new immutable-ed27eba controlled-delay RED shows a 100 ms web tap becoming a long exit because release waits in the HTTP response FIFO. Preserve actual short answers, supported progress, latched READY, early/cancel restoration and current inactivity rules; fix paired web delivery without fabricating hardware timing. | PM queues one coupled writer after panel freeze; menu_guidance_design_review supplied the concrete RED and scoped fix design; Chief independently reviews and verifies |
| T042 Nonvoice paths and independent auth/hook | V14 includes fresh authorization fencing, actual-hook preservation, current menu paging and truthful hints. Hosted auth/paging acceptance is active. Explicit panel Start/End/skip and logical interaction route are being implemented locally; cover guest/auth × down/up, both orders, reauthorization, expiry/end/next visit, contextual choice and exit. No autolift, fake authorization, new Max data or unverified physical audio claims. | PM integration; contextual_game_flow: coupled backend route; console_recovery_fix: frontend; Chief independent review and native acceptance. story_idle_review completed the reviewed identity boundary and diagnosed the separate doctor-list remap. |

The new matrix is another investigation aid. Native coverage must include
different actual games, exact story assets and interruptions, source failure
and missing/private-data boundaries. One quiz, synthetic WAV or local mock
cannot establish all routes or human microphone/hearing acceptance.

## Previous observed outcomes — 2026-11-18

Candidate: application PID `16858`, source `9fe4f90`, branch
`codex/remote-device-simulator`; authenticated HTTP serves client SHA-256
`c28ed3fc6e63e95897b82c71e7ecb685e6726095bca9ffc7c3715cc45e82377a`.
The existing runbook identifies exact earlier PIDs and source transfers.
Current proof is software/browser acceptance, not physical hearing or clinical
acceptance. New findings and work above supersede the earlier stopping point.

| Tasks | Observed status and limits |
| --- | --- |
| T001–T005 | Current source/runtime/access readbacks passed. Protected HTTP/WS, private projection, Max ownership/expiry and unchanged external-fixture evidence are reused where their source is unchanged. No authorization scope was widened. |
| T006–T008 | Three guest and three permitted synthetic profile visits passed, including known-fact entry and clean endpoints. Entry-source evidence transfers across the isolated story/client changes; this does not establish physical RFID. |
| T009–T012 | All five games, seven root destinations, age samples, direct/contextual choices and exits have actual browser evidence. Real positive weather/schedule data was not exercised: missing provider/scope remains truthful unavailability, not a positive provider pass. |
| T013–T016 | Actual personal PCM passed for young/middle/older ages: 53.625/69.075/104.150 seconds. Young grammar has actual converter plus 44 boundary checks; whole validation, real Silero, pause/checkpoint/new lease/active exit passed. Final older control proved received/rendered wave movement after full resumed PCM. Earlier public-story active/pause-idle and independent Max-expiry controls are reused; no timer was extended. |
| T017–T020 | Software S2S and two earlier synthetic microphone/STT turns passed. Final client fault → same-visit useful answer → explicit microphone capture/accepting → clean hangup passed. T020/SC-004 physical microphone and two heard answers remain NOT OBSERVED; Roman has been asked to check. |
| T021–T026 | Existing desktop/390px, keyboard/flat view, overlapping input, reconnect/reload, actual app restart and next-visit evidence is retained with unchanged-source limits. Final client error/retry was inspected in the real browser. Protected Jupyter agent login/reopening passed; Roman's personal opening/reopening remains NOT OBSERVED for T025/SC-007. |
| T027–T029 | Chief independently reviewed each integrated source change, reproduced the phone-text race, and reviewed the corrected exact client. Relevant checks passed: 114 Python cases, canonical acceptance, four scheduler cases, eight affected Node suites and final diff checks. Steward's 07:40 and Chief's 07:43 final documentation/source consistency reviews passed; no broad unchanged suite is claimed rerun. |
| T030 | Ordered young game → successful story pause/resume → conversation → fault/retry plus clean recovery/new visit is recorded. Older browser and final client recovery artifacts close those previous software controls. Whole mission remains open for the active Stage 1 software work above as well as the explicit hearing/personal-access observations; neither version 0.1.0 nor these software passes declare completion. |

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
