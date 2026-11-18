# Feature Specification: Reliable Runpod Demonstration

**Feature Branch**: `codex/remote-device-simulator`

**Created**: 2026-11-18

**Status**: Active Stage 1 product-integrity work; previous acceptance is a baseline

**Input**: Roman's full MISHKA demonstration mission relayed to the Steward, the
historical research and current evidence identified in [research.md](research.md).
An unfamiliar operator must use the whole demonstration repeatedly and recover
from errors. The intended audience is children roughly 1–7, including children
with autism; the demonstration does not establish clinical suitability.

## User Scenarios & Testing *(mandatory)*

All five stories are required for handoff. Priority orders execution; it does
not permit omitting a later story. Relevant regression coverage and actual
browser/remote-service checks are required where they establish different facts.
Roman's clarification (Sentinel_d5bd7df25de48191b68a7e148ebaf4e0) makes these
symptoms and scenarios starting probes, not an exhaustive scope or closed
acceptance checklist. Investigate causes and interactions across whole MISHKA,
including previously unnamed defects; adapt coverage to what that work reveals.

### User Story 1 - Arrive with or without a profile (Priority: P1)

The operator opens the protected demonstration and starts a guest or permitted
synthetic-profile session. The welcome leads to an actionable menu.

**Why this priority**: Every demonstration needs an understandable start.

**Independent Test**: Guest and known-profile arrival → activity → hangup →
repeat; also check unknown/expired profile handling.

**Acceptance Scenarios**:

1. **Given** a guest visit, **When** the receiver is lifted, **Then** a short
   welcome leads to choices with the existing unknown-age default, without
   requiring physical reader or handset access.
2. **Given** a valid profile with name and age, **When** entry begins, **Then**
   supplied facts personalize it without asking those questions again or
   exposing the profile to observers.
3. **Given** an unknown/invalid/expired profile, **When** it is presented, **Then**
   its result is explicit, no identity is fabricated, and safe guest entry and
   cleanup remain available.
4. **Given** exhibition policy, **When** the receiver is replaced and lifted,
   **Then** old speech, capture, private context and input cannot resume in the
   new session; urgent safety keeps its existing protected recovery policy.

### User Story 2 - Reach and use every activity (Priority: P1)

The child reaches games, fixed stories, telling/asking/support, personal stories,
wellbeing, conversation and calendar through the existing menu.

**Why this priority**: Isolated activities do not satisfy the requested demo.

**Independent Test**: Reach each root destination and return; complete a
meaningful round in light waves, quizzes, repeat-pattern, odd-one-out and
spoken words, checking intended hints and button/voice exits.

**Acceptance Scenarios**:

1. **Given** games invitation, narration or repeat guidance, **When** a fresh
   valid choice is pressed, **Then** that game starts once on that press.
   A held entry key must first be released to become a new choice.
2. **Given** any menu, **When** the child selects, interrupts, repeats or returns,
   **Then** current affordances work without unnecessary repeated long narration
   or stale instructions. Other menus keep their documented scene semantics.
3. **Given** any of the five games, **When** correct, wrong, delayed, repeated
   or supported simultaneous input occurs, **Then** progression/hints, sound
   and light match, and exit works. An offered answer 7 scores before acting
   as an exit.
4. **Given** an age group 1–3, 3–5 or 5–7, **When** a game starts or is revisited,
   **Then** curated difficulty, content variation and within-session pattern
   progress remain available, with stable colors and pictograms.
5. **Given** support, wellbeing, personal-story or calendar entry, **When** its
   offered path is followed, **Then** it completes or explains a real unavailable
   capability with recovery; no appointment or adult delivery is invented.
6. **Given** ordinary conversation, **When** a real model response proposes a
   supported game and the child confirms the current offer, **Then** that game
   starts with exclusive media/control ownership. Completion or cancellation
   returns to the valid conversation caller; root-launched games return to root.
   Refusal, a different request, cleanup or expired identity revokes old consent.
7. **Given** a game whose answer includes button 7, **When** the child uses its
   documented nonvoice exit gesture, **Then** that gesture exits without also
   scoring an answer. Short answer presses remain usable. Waiting and gentle
   reminders do not silently change the 120-second inactivity rule.
8. **Given** ordinary conversation, **When** the child asks generally to play
   or names any currently supported game, **Then** a short real catalogue
   choice or the matching game is offered without an unnecessary full-menu
   detour. Voice and button selection, including during chooser narration,
   converge on one owned launch and retain the valid conversation caller.
9. **Given** a game with a hold-seven exit, **When** the child holds beyond
   ordinary short-press recognition, **Then** calm progress reflects the actual
   supported lighting channels. Once ready, the exit remains ready until a
   valid release; holding longer does not miss an exit window. An early release
   or interrupted gesture restores current game feedback without an unintended
   answer or exit. Spoken instructions explain the gesture without relying on
   sight; rapid/uneven repeats and session cleanup cannot revive old progress.

### User Story 3 - Listen, pause, continue and leave stories (Priority: P1)

The child selects a fixed or personal story, listens, pauses, continues and
leaves. Playback and waiting have distinct rules.

**Why this priority**: Long audio crosses boundaries short smoke tests miss.

**Independent Test**: Guest fixed-story playback beyond idle → pause/resume →
pause-idle exit. Observe authorized profile expiry as a separate sequence.

**Acceptance Scenarios**:

1. **Given** a playing guest story, **When** more than 120 seconds pass without
   a button press, **Then** playback remains active rather than ending as idle.
2. **Given** a paused story after its announcement finishes, **When** resumed
   before 120 seconds of waiting, **Then** the existing checkpoint continues;
   **When** waiting reaches 120 seconds without fresh activity, **Then**
   engagement ends at the idle root menu with story media/light cleared.
   The receiver can remain up; pause is never indefinite.
3. **Given** independently expiring profile authorization, **When** its deadline
   arrives during a story, **Then** private access is revoked using the existing
   safe policy, without extending the credential or calling that ordinary idle.
4. **Given** exit/cancellation or a new session, **When** an old story completes
   late, **Then** it cannot restart speech, capture, light or the previous scene.
5. **Given** a general or named fixed-story request from conversation or its
   chooser, **When** an unambiguous available title is selected, including
   during narration, **Then** old menu audio stops and the exact verified
   catalogue asset plays. Unknown/ambiguous titles are clarified, never
   generated from memory or inferred from a historical asset ID.
6. **Given** a story called from conversation, **When** it completes or the
   child intentionally leaves, **Then** the valid caller resumes naturally.
   Pause/resume retains the checkpoint; safety, hangup, reset and identity
   change cannot restore the old caller or media.

### User Story 4 - Have a spoken exchange and recover (Priority: P1)

With explicit sound and microphone consent, the operator speaks an ordinary
question, hears a relevant answer and continues. Listening claims match capture.

**Why this priority**: A listening announcement followed by silence breaks the
basic promise of the interactive phone.

**Independent Test**: Real permitted browser microphone → non-private question →
heard answer → next question → cancel output → begin again; separately deny
permission and exercise a controlled recognition/response failure.

**Acceptance Scenarios**:

1. **Given** admitted listening and microphone consent, **When** a normal question
   is spoken, **Then** a relevant answer is audible and another turn is possible.
2. **Given** permission denial, low-confidence recognition or response/audio
   failure, **When** it occurs, **Then** a bounded understandable retry/exit
   replaces unbounded silence; eligible capture can be explicitly enabled again.
3. **Given** active/pending capture or output, **When** hangup/reset, page exit
   or authenticated connection loss occurs, **Then** media stops and late
   permission/output cannot reappear under a new session owner.
4. **Given** a safety-sensitive statement, **When** existing safety handling
   activates, **Then** local response remains truthful without claiming delivery
   to an adult through a nonexistent channel.
5. **Given** an explicit weather or personal-day question in conversation or
   section 7, **When** the relevant source and authorization are available,
   **Then** sourced current facts are spoken; missing schedule details,
   credentials or fresh observations produce an honest limitation. A guest
   never receives another child's data, and a doctor list is not an itinerary.
6. **Given** an innocuous everyday or character question, **When** the model
   answers, **Then** calm age-appropriate playful fiction is permitted within
   the established persona. Real clinical, child, schedule and weather facts
   remain grounded; necessary safety checks and justified fallbacks remain.

### User Story 5 - Operate, recover and repeat (Priority: P2)

The operator uses the liked panel and protected file workspace. Connection and
service failures have a clear route back without stale work executing.

**Why this priority**: An unfamiliar operator must manage a second attempt and
recovery, not depend on the developer during the demonstration.

**Independent Test**: Desktop/narrow layout and keyboard → disconnect/reconnect →
service restart/reload → complete another guest/profile visit → reopen files.

**Acceptance Scenarios**:

1. **Given** desktop/narrow use, **When** navigating by pointer or keyboard,
   **Then** existing controls remain reachable, focus/state are clear and the
   flat-view fallback works.
2. **Given** queued ordinary work and lost synchronization, **When** dispatch
   would occur, **Then** it cannot act on retained old state; terminal release,
   hangup, reset and media cleanup remain available.
3. **Given** restart or regressed state history, **When** current state cannot be
   trusted on reconnect, **Then** explicit reload guidance is shown and reload
   recovers while preserving stale-work protections.
4. **Given** rapid/repeated/overlapping input, virtual input failure, refresh or
   service restart, **When** recovery is followed, **Then** old input/media do
   not revive and another full visit succeeds, respecting safety recovery.
5. **Given** the approved protected file-access method, **When** Roman opens and
   reconnects to the workspace, **Then** persistent files remain usable without
   exposing an unprotected administrative endpoint.

### Edge Cases

- Held entry keys, duplicate presses, simultaneous input, focus loss and release
  around cancellation/disconnect.
- Locked sound, late microphone permission and backend capture disallowance.
- Old playback/text/identity results after hangup, reset, idle exit or restart.
- Same epoch with lower cursor, older epoch, history gaps and a new session whose
  local state version is lower.
- Public story activity versus profile expiry; unrecognized speech/no valid answer.
- Unknown/duplicate scan, warmup, unavailable story/schedule and urgent safety.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: Support guest and authorized synthetic-profile entry through the
  protected demonstration; identify warming, denied and unavailable states.
- **FR-002**: Use supplied profile facts without re-asking known name/age or
  manufacturing missing identity.
- **FR-003**: Preserve all seven root destinations and return, cancel, repeat
  and interruption routes; no decorative unanswered greeting question.
- **FR-004**: A fresh valid games-menu choice during guidance selects once on
  that press; held/stale input must not become a new selection.
- **FR-005**: Preserve all five games with meaningful progression, hints and
  button/voice exits.
- **FR-006**: Preserve supported age bands, curated content variation and
  within-session progression with stable physical colors/pictograms.
- **FR-007**: Preserve context-dependent button meaning, legitimate answer 7
  and supported simultaneous inputs without breaking navigation.
- **FR-008**: Preserve verified fixed stories and validated personal-story
  creation, truthful unavailability, pause/resume and exit.
- **FR-009**: Playing stories are active; paused waiting ends engagement after
  120 seconds of inactivity through the existing idle-root behavior.
- **FR-010**: Keep profile authorization, ready-to-enter waiting, engagement idle,
  child response time and speech boundaries distinct; do not extend Max's
  authorization to make a long story pass.
- **FR-011**: Answer an ordinary spoken question with relevant audible content
  using the approved baseline voice, then allow another turn.
- **FR-012**: Require explicit microphone consent, indicate actual capture and
  stop it on cleanup/connection loss, including late permission.
- **FR-013**: Recognition, response, audio and loading failures terminate within
  configured bounds and expose an understandable retry/exit.
- **FR-014**: Align sound/light, scene and available input; cancel/exit/reset
  clears effects owned by the old activity.
- **FR-015**: Preserve the liked panel and functional control access on desktop,
  narrow layouts, keyboard and flat-view fallback.
- **FR-016**: Unsynchronized queued work, obsolete state, late identity and stale
  media cannot mutate a recovered/new session; cleanup remains usable.
- **FR-017**: Reconnect/reload guidance must recover after refresh/service restart
  and permit another usable session.
- **FR-018**: Preserve exhibition handset-down cleanup and urgent safety
  exceptions; historical clinical suspended behavior stays a distinct policy.
- **FR-019**: Integrate Max's identity result within his ownership; protect
  private profiles, credentials and child audio from observer/log exposure,
  without introducing recording or expanded collection.
- **FR-020**: Preserve safety and truthful support, wellbeing and calendar
  behavior; never invent adult delivery or appointments.
- **FR-021**: Keep this remote demo independent of garage/hardware access and
  archived voice-training work, preserving assets, weights and working changes.
- **FR-022**: Provide protected persistent file access and reproducible entry/
  recovery guidance using approved access paths and the cost ceiling.
- **FR-023**: Record delivered-candidate acceptance, failures, blockers and
  independent review with evidence limits; readiness and fixtures cannot
  establish whole-demo, real hearing, hardware or clinical acceptance.
- **FR-024**: Discover and fix demonstrated systemic causes across ownership,
  lifecycle, cancellation, resources and error recovery, including affected
  paths beyond the listed symptoms. Extend regression and real-flow coverage
  for newly discovered interactions; restore intended behavior rather than
  closing recoverable defects through weaker fallback alone. Avoid speculative
  rewrites without evidence of a needed change. Roman permits necessary code or
  architecture replacement; the preference for a small coherent change must not
  prevent a complete causal repair. Preserve functions, data and access boundaries.
- **FR-025**: Track MISHKA product versions beginning at 0.1.0. Each delivered
  version identifies its committed code and actual verification of a coherent
  working product with required functions retained. Distinguish passed, failed,
  not-run and hardware limitations; do not label the target version ready before
  acceptance. Authorized commit/push and own-branch changes do not permit branch
  deletion or Runpod pod stop/terminate. Version 0.1.0 is only the starting point
  of versioning; publishing it does not complete the mission. Continue meaningful
  implementation and independent verification until the original complete,
  reliable MISHKA outcome is established, independent of version numbers.
- **FR-026**: Treat voice games and conversation as contextual callers of the
  same real speech pipeline. Support owned model proposals and confirmation for
  game entry, preserve committed caller memory separately from game history,
  and restore a valid caller on ordinary completion/cancellation. Stale results,
  repeated consent, safety, hangup and identity cleanup cannot revive a caller.
- **FR-027**: Provide understandable nonvoice game exits without stealing a
  legitimate answer. Keep bounded rounds and gentle reminders appropriate to
  the actual game; examples of 10 questions, 20 seconds or a 60-second exit are
  not approved constants. System reminders are not user activity. Distinguish
  technical dialogue budgets, engagement idle, playback and authorization expiry.
  Where short-seven answers share a hold-and-release exit, distinguish a short
  answer from an intentional hold, show supported calm progress, and latch
  readiness until release rather than exiting while held. Explain readiness
  and recovery accessibly; abort and cleanup restore the current scenario.
  Choose timings from actual constraints and experience, not illustrative
  user numbers, and verify web and physical behavior as separate evidence.
- **FR-028**: Dim readable OFF digits/emblems and show their actual independent
  hardware channels accurately in flat and 3D simulation. Preserve fill behavior;
  do not invent channels or infer physical-board acceptance from the browser.
- **FR-029**: Order all HTTP and WS panel snapshots, including background light
  frames without journal events, so delayed delivery cannot roll back state.
  Continue investigating significant unnamed interactions across all scenarios
  rather than treating the existing matrix or these requirements as a ceiling.
- **FR-030**: Make ordinary conversation an actual gateway to every supported
  game and verified fixed-story asset, plus existing typed information
  capabilities. General requests receive concise catalogue choices; precise
  requests avoid redundant menu traversal. Uncertain names are clarified.
  Support fresh voice/button choices during narration without double launch,
  overlapping old audio or loss of the valid caller. Preserve contextual
  button assignments, including game answer 7.
- **FR-031**: Ground section 7 and conversational information in existing
  integration contracts and current authorization. Do not modify Max's code
  or database or invent appointment time/order/room from `doctors_today`.
  A valid current card may supply a bounded source-qualified doctor list
  through its existing personalization permission; this does not grant
  schedule permission. Preserve the source visit date, verify it before
  saying today, and clear private facts on identity expiry or replacement.
  Current Max values are test/provisional data. Verify the integration with
  those values without presenting them as real clinical records; absent fields
  remain honest contract limitations, not invented appointments.
  Support configured real weather through existing validated settings; inspect
  credential presence without disclosing values or buying a connection.
  Reject stale/malformed observations and explain absent data honestly.
  Current key and coordinates are absent. Configured-source behavior and
  missing/fault handling are separate from a real-API acceptance. Obtain any
  city name from the coordinate API or another available supported source;
  never default unconditionally to Saint Petersburg or require a manual city
  question when unnecessary. State an unavailable name honestly.
- **FR-032**: Investigate fallback causes across parsing, age limits,
  recognition, validation and scenario ownership. Remove demonstrated false
  refusals and contradictory instructions while retaining safety and age
  bounds. Permit harmless playful persona details separately from real
  clinical/private/provider facts; do not use a broad location matcher to
  turn a character question into a personal schedule query.

### Key Entities *(include if feature involves data)*

- **Session**: Guest/profile interaction, policy, lifecycle, activity and owner.
- **Profile context**: Max-supplied private facts with independent validity.
- **Activity**: Menu, game, story, support, wellbeing, conversation or calendar
  with its current affordances and return path.
- **Media ownership**: Speech, capture, playback and light bound to session/activity.
- **Operator observation**: Redacted current state and recovery guidance, never
  authority for an obsolete action or a disclosure of private content.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: An operator following the guide completes three consecutive guest
  visits and three profile visits from welcome to activity and clean exit
  without developer intervention or prior-session media leaking.
- **SC-002**: Each root destination is reached and returned from; all five games
  complete meaningful progress/hint/exit. A valid fresh games-menu choice needs
  one press even during narration.
- **SC-003**: Guest fixed-story playback survives beyond 120 seconds; pause/resume
  and pause-idle are checked just before and at 120 seconds. Profile expiry is
  separately observed without extending authorization.
- **SC-004**: Two consecutive ordinary spoken turns are heard and answered through
  the permitted browser path; cancellation and one controlled failure recover
  to another turn. Record actual latency/bounds instead of invented targets.
- **SC-005**: Desktop and 390-pixel layouts support receiver, seven keys, sound,
  microphone, tools, recovery and flat-view fallback with keyboard access and
  no obstructed essential control or horizontal page overflow.
- **SC-006**: Each User Story 5 recovery case succeeds and another visit completes;
  no obsolete ordinary action/media executes after losing its owner.
- **SC-007**: Roman opens and reopens the protected persistent file workspace;
  the guide identifies current entry and recovery without publishing secrets
  or causing data loss.
- **SC-008**: Handoff ties the delivered candidate to observed sequences and
  causal findings, adaptive regression coverage and independent review; no
  demonstration-blocking failure is hidden by a mock
  check, and unknown hearing/hardware/clinical properties stay unverified.

## Assumptions

- Later direct decisions override historical notes and assistant proposals.
  Research lists what was read and gaps; the entire archive is not claimed read.
- The current remote launcher selects exhibition with synthetic Max profiles.
  Clinical handset-down requirements remain a documented separate policy
  context; this runtime evidence does not automatically defer them or resolve
  the conflict universally. Policy changes require deliberate source reconciliation.
- Synthetic profiles and non-private utterances suffice for software acceptance.
  Clinical suitability, measured acoustic safety and physical hardware require
  separate evidence.
- Reuse authorized remote hosting, baseline voice and protected credentials;
  verify freshness/access before use. This spec grants no purchase or deployment
  authority.
- Three visits and 390 pixels are bounded engineering acceptance samples, not
  customer-prescribed medical thresholds or proof of universal reliability.
  They and the initial task count are neither a scope ceiling nor a stop condition.
