# Research and Brownfield Decisions

## Source precedence and reading boundary

1. Roman's latest direct mission/engineering decisions, relayed in this task,
   govern: whole-demo outcome, Runpod-only, baseline Silero/VC off, no false adult
   delivery, active-story versus 120-second paused idle, preserved UI/functions,
   Max ownership and no quota bypass.
2. The following local research notes were read as historical evidence, not as
   newly verified original recordings or current deployment acceptance. Root:
   `/home/khugardin/Documents/Obsidian/Mishka/05 Исследования/`:
   `00 Археология Мишки.md`, `01 Голос заказчицы — требования и цитаты.md`,
   `02 Кнопки — исходная геометрия и назначения.md`,
   `03 Решения — хронология и противоречия.md`,
   `04 Ноябрь — рабочий контекст и незавершённое.md`,
   `05 Покрытие и первоисточники.md`,
   `06 Codex — прямые указания 12 октября.md`,
   `07 TTS — первичные решения ChatGPT.md`.
3. Current repo evidence read: remote launcher and launch script, relevant
   config validation, Max boundary, engagement-idle owner, game-menu selection
   tests, fixed-story idle tests, remote runbook/device contract/phone UI docs,
   immersive-games candidate report and Max JSON runtime guide. Branch checked:
   `codex/remote-device-simulator`; extensive other writers' changes exist.
4. Saved recovery record read:
   `/home/khugardin/Documents/Codex/2026-11-16/project-manager/work/mishka-remote-stand/recovery-20261117/paused-ui-candidate.json`.
   It reports both review findings implemented, status
   `candidate_stable_awaiting_independent_review`, `candidate_deployed=false`,
   updated at `2026-11-17T22:19:47.374019+00:00`. Its checks are supplied/historical;
   this baseline did not rerun them or verify live hashes.

Not read directly in this subtask: complete PM/Chief chat archives, original
meeting audio, the entire transcript/photo collection, external clinical advice
or current remote processes. The parent Steward owns further source/chat
reconciliation. This document does not claim all context is gathered or that
an earlier researcher's reading was repeated here.

## Decisions

### Preserve exhibition policy for the current synthetic remote demo

**Decision**: Keep the launcher at exhibition + max_json + simulator. Config
explicitly rejects max_json with clinical. Normal handset-down cleanup and the
urgent safety exception remain canonical.

**Rationale**: October clinical silent handset-down play/RAM suspension and
exhibition full reset are distinct source decisions. Current explicit runtime
selection resolves this demo's mode without rewriting clinical behavior.

**Alternatives considered**: Universal handset-down reset would erase clinical
history; clinical redesign would exceed this accepted remote demo baseline.

### Separate each timer and authority

**Decision**: Playing guest stories stay active. Paused waiting uses the current
120-second engagement window from completion of the pause announcement; expiry
returns to root_menu with interaction_idle and clears owned media/light.
Max's client deadline, card expiry and profile-ready TTL stay independent;
speech end detection and child response/hint timing are not reused as idle.

**Rationale**: `tests/test_engagement_fixed_story.py` expresses +400 seconds
active playback and the pause boundary at 119.999/120 seconds. `max_json.py`
documents Max's existing 120-second client deadline and separate server expiry.
These are current source/test expectations, not evidence that the live path passes.

**Alternatives considered**: Infinite pause contradicts Roman; extending profile
authorization to preserve story playback weakens an external boundary. A 10-second
classroom guideline is not a clinical device timeout norm.

**Integration finding, 18 November (implementation/review pending)**: The unchanged
external fixture at `../project-manager/work/mishka-remote-stand/deploy-v1/max-client-fixture/`
documents mandatory deletion of patient state after at most 120 seconds
(`PRODUCTION_CLIENT_RU.md:108–123`), while `API_CONTRACT_RU.md:64–72` explicitly
leaves playback and scenario control to the phone. The local client deadline is
distinct from server `valid_until` and credentials. Our adapter currently discards
the clear reason; canonical/application cleanup then destroys public fixed-story
playback and its checkpoint along with private state. Four verified recordings
last 238.964–522.716 seconds. Separate passing guest-story and Max-expiry tests
therefore do not establish the required profile → long-story interaction.

Review a narrow trusted-expiry transition that erases all profile-derived state
and pending private output before any await, yet preserves only an already
validated public fixed story as a guest. Preserve an existing paused deadline;
expiry is not fresh user activity. New scans, reader/API failures, safety,
hangup/reset and private/generated playback must retain their stop/cleanup
semantics. This is a proposed repair boundary, not a verified implementation or
permission to alter Max. T005/T014/T016 must cover the combined unchanged-fixture
path, absence of private state, public completion and real elapsed expiry.

### Use direct valid game choices without generalizing every menu

**Decision**: A fresh valid games-menu slot selects once during invite, narrating,
repeat_prompt or repeat_listen; held/stale/changed mappings remain fenced.

**Rationale**: `tests/test_game_menu_direct_choice.py` has this expectation.
The older immersive-games report's skip-then-press recipe conflicts and needs
checking/updating by its owner. Fixed-story menu narration currently has its
own documented skip/select semantics; do not invent a universal rewrite.

**Alternatives considered**: Preserve the old game recipe would codify a likely
stale doc; silently make every menu direct-select is an unsupported expansion.

### Preserve the whole product and its visual source

**Decision**: Retain the liked operator panel and seven root destinations;
preserve five immersive games, age bands, stable colors/pictograms and actual
18-channel lighting projection. Use existing reference-driven phone/UI work.

**Rationale**: Client archaeology describes contextual fingers; permanent root
assignments came later from Roman. Do not label an engineering 5+2 layout as
client physical geometry or replace the liked panel with a generic redesign.
The November game content targets remain validation scope: curated variation,
within-session pattern progress and the existing deck coverage must be checked,
not erased to make the demo appear stable.

**Alternatives considered**: A reduced game-only demo fails scope. Unrelated
visual redesign and physical CAD claims add risk without meeting this task.

### Baseline voice and truthful safety/privacy

**Decision**: Use baseline Silero with voice conversion off. Preserve archived
VoxCPM/RVC assets without restarting training. Keep local safety; no fictitious
adult-send path or newly implemented persistent adult-RFID alert. Do not persist
child recordings or disclose profile/private speech in public evidence.

**Rationale**: Later direct mission overrides the historical voiceclone research
and unresolved recording proposals. No confirmed adult delivery channel exists.

**Alternatives considered**: New model training, adding notification delivery or
expanding profile capture are independent work requiring separate direction.

### Reuse existing infrastructure, verify unknowns before changing

**Decision**: Fresh read-only pod/cost/runtime/access inventory; reuse existing
persistent data and allowed access. SSH is primary for agents; protected Jupyter
is Roman's requested file view. A route-change approval refusal must not be
bypassed. No action in this spec deploys, purchases or modifies permissions.

**Rationale**: Last-known pod/cost/process and SSH failure reports are not fresh
facts. Garage/Ethernet are explicitly irrelevant to this remote stage.

**Alternatives considered**: Buying another pod blindly or exposing an admin
listener would create costs/access risk without proving whole-demo acceptance.

## Verification unknowns, not newly asserted defects

- Whether the current local two-fix recovery candidate passes Chief's actual
  browser tests and matches the live assets. Supplied implementation exists.
- Real ordinary-question ASR → response → Silero → heard output and the return
  to capture, including permission denial/cancellation/provider failure.
- Real every-activity traversal, profile expiry during long audio, repeated
  sessions, narrow/keyboard usability and restart recovery of the final candidate.
- Current permitted SSH/Jupyter accessibility and file persistence.
- Clinical suitability, acoustic levels and physical hardware remain outside
  software proof. Child adaptation uses sources and qualified review where
  available; do not claim expert endorsement from this specification.

## Tooling provenance and actual application

Ponytail was read at `/home/khugardin/.agents/skills/ponytail/SKILL.md`; the
minimum complete diff includes this full mission, not only current JS fixes.
Official Spec Kit 1.4.2 specify/plan/tasks/analyze skills were read from
`/home/khugardin/.local/share/mishka-tools/spec-kit-bootstrap/.agents/skills/`.
Only required core Bash resolver/setup/check scripts and four templates were
copied into `.specify/`, byte-identical to that reviewed bootstrap, with its MIT
license. The feature pointer and minimal init metadata are project state.
No extensions.yml, extension/preset layers or hooks existed; no branch hook ran.
No .agents/, AGENTS.md, dependency, product file or service was modified.

The real spec resolver created the initial spec file. The real plan setup
resolved its template and returned the feature paths; its BRANCH value is a
feature identifier in this version, not a Git checkout operation. Tasks setup
and read-only analyze prerequisites follow after the design artifacts. No
implementation or convergence is claimed by this baseline.
