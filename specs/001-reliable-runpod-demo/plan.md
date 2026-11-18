# Implementation Plan: Reliable Runpod Demonstration

**Branch**: `codex/remote-device-simulator` | **Date**: 2026-11-18 | **Spec**: [spec.md](spec.md)

**Input**: `specs/001-reliable-runpod-demo/spec.md`

## Summary

Reconcile the existing brownfield candidate with the whole-demo mission, repair
reproduced gaps in shared ownership/transition paths and prove real end-to-end
use on Runpod. Preserve the liked panel, every requested root path and game,
Max's boundary and current exhibition policy. The two recovery findings are
reported as implemented in the saved candidate; independent acceptance and
live delivery remain pending, not presumed missing implementation.

The symptom list is a starting point for causal discovery across whole MISHKA,
including unnamed failures and interactions. Trace ownership, lifecycle,
cancellation, resource handling and error recovery, then fix demonstrated causes
and adapt coverage across affected paths. Component coordination or weaker
fallback alone does not complete this work; speculative rewrites add no evidence.

Start with the existing canonical runtime, Stand adapter, bounded speech/audio
owners and browser transport. Roman permits necessary code or architecture
replacement when evidence shows it helps achieve the complete working outcome;
this is permission, not a rewriting objective. A second scenario engine, frontend
framework, voice engine or deployment stack requires a demonstrated benefit,
complete affected callers and validation, and preserved functions/data/access
boundaries. Setup is this additive baseline; product changes remain with PM's
assigned writers.

Operational constraints from Roman: preserve GitHub branches (our own may be
created/changed), keep the Runpod pod running, and never issue pod stop/terminate.
These constraints survive broad code/architecture permission. Approval/security
settings are not changed by this work.

Naming inventory: the operator name is `mishka`; the existing GitHub repository
is `mishka-s2s`. The runbook records pod display name `mishka` and operator alias
`/workspace/mishka`, while pod ID/hostname, active checkout, runtime/environment,
model links and SSH/Jupyter references remain distinct. Do not duplicate this
already applied naming work or move active paths for cosmetic consistency.
Any later migration must inventory real paths and references, preserve data,
verify launch/access/recovery and use a safe boundary outside the live session.

## Technical Context

**Language/Version**: Python 3.12+ (requirements identify 3.12.3 as canonical
verification baseline); browser JavaScript/HTML/CSS and Bash. Verify the actual
Runpod interpreter before execution.

**Primary Dependencies**: Existing FastAPI/Uvicorn, LangGraph, Pydantic,
faster-whisper, Ollama, Silero, and bundled Three.js. Reuse installed/pinned
project dependencies; no dependency change is planned.

**Storage**: Persistent Runpod /workspace assets, model caches and private
runtime state; unchanged Max demo TLS/SQLite state; existing session history.
Profile/child content must not enter public diagnostics or evidence artifacts.

**Testing**: Existing pytest scenarios and Node regression scripts; independent
actual Chromium checks on the real candidate and authorized HTTPS/browser path.
Mocks and synthetic microphone fixtures remain labeled.

**Target Platform**: Existing authorized Runpod Community GPU at at most
$0.35/hour with persistent storage, browser desktop and 390-pixel width;
no physical-device/garage requirement. Last reported pod is historical until
read-only infrastructure verification.

**Project Type**: Brownfield Python web-service and operator workbench with
virtual hardware and real speech providers.

**Performance Goals**: All finite acceptance chains complete within the
existing configured request/model/playback deadlines; valid game selection
takes one fresh press, no stale media/input after owner loss. Record real
first-audio/end-to-end latency and failure bounds; the historical 0.001s and
TTFA assistant suggestions are not imposed or claimed achieved.

**Constraints**: `runtime_mode=exhibition`, `identity_mode=max_json`,
`hardware_mode=simulator`, synthetic identity, `production_ready=false`;
baseline Silero, `voice_conversion=off`. Max's existing authorization expiry
is distinct from engagement idle and may be at most 120 seconds under his
client policy. Active story playback suspends idle; paused waiting expires
120 seconds after pause announcement completes. Guest long-story verification
must not be used to bypass profile expiry. SSH remains primary agent access;
protected Jupyter is required for Roman's files, not an execution substitute.

**Scale/Scope**: One coherent demonstration session with repeated visits,
all seven root destinations, five games, story and conversational paths,
operator controls and recovery. This is software demo acceptance, not production,
physical hardware or clinical certification.
Current exhibition + max_json is runtime evidence; the clinical-policy conflict
remains a separate documented context, not automatically future/unrequested work.
Any policy change requires deliberate source reconciliation.

## Constitution Check

Gate before research: PASS against [.specify/memory/constitution.md](../../.specify/memory/constitution.md).
The current instructions are restated rather than replaced by new governance.

| Principle | Design evidence / acceptance gate |
| --- | --- |
| Complete outcome | All five stories required; SC-001–SC-008 and final mixed sequence |
| Smallest complete change / ownership | Existing runtime paths; one product writer per area; conditional fixes only |
| Evidence before claims | Reproduce first; test final state; Chief independently reviews actual browser/live behavior |
| Source and policy separation | research.md records precedence, stale docs, distinct idle/profile/clinical policies |
| Access/privacy/continuity | Unchanged Max source; protected secrets/files; no deploy/purchase/hooks authority |

Post-design recheck: PASS. No proposed constitution violation or additional
permission is needed to author this baseline. Real runtime, access and child
suitability unknowns are validation work, not grounds to claim product readiness.

## Project Structure

### Documentation (this feature)

```text
specs/001-reliable-runpod-demo/
├── spec.md
├── plan.md
├── research.md
├── data-model.md
├── contracts/demo-boundaries.md
├── quickstart.md
├── tasks.md
└── checklists/requirements.md
```

### Source Code (repository root)

```text
app/foundation/
├── canonical_contract.py
├── application.py, runtime.py, config.py
├── max_json.py, max_client_runtime.py
├── stand.py, remote_devices.py, remote_access.py
├── scene_menus.py, menu_content.py, menu_capabilities.py
├── game_engine.py, game_content.py, game_presentation.py
├── engagement_idle.py, personal_story.py, private_welcome.py
├── s2s.py, owned_speech.py, schedule_weather.py
└── console/console.js, phone.js, phone-model.js, console.css, phone.css
scripts/run_remote_stand.py
deployment/remote-stand/
content/stories/fixed_manifest.json
tests/
docs/remote-stand-runbook.md
```

**Structure Decision**: Preserve current modules and ownership. The existing
working tree has substantial concurrent modifications and untracked assets;
this baseline touches only .specify/ and this feature directory. No AGENTS.md,
.agents/, product files, Git history, services or deployment is changed.

## Delivery and Ownership

| Role | Responsibility and boundary |
| --- | --- |
| Steward | Reconcile sources, keep complete spec/scope and evidence interpretation; additive spec artifacts |
| PM | Assign sole product writers, integrate fixes, own product version/candidate/runbook and authorized commit/push on our branch; deployment retains its applicable authority |
| Implementation writers | Roman requested GPT-6.1 Sol XHIGH; preserve other changes; no overlapping writers |
| Chief | Independent code and actual Chromium/live review, validation of acceptance and recovery; not the product author for its review |
| Tool installer | Completed official specify-cli/bootstrap and static Ponytail setup; no ongoing product writer |
| Roman | Product authority and eventual real microphone/hearing/physical acceptance where required |

PM/Chief/coordinator routing preference is GPT-6.1 Sol; use Astra only for
targeted complex analysis. Do not interrupt useful work to switch. Runtime model selection and
availability must be verified by the coordinator; this document does not claim
any role has run. Do not create duplicate workers to satisfy the table.

Product versioning starts at 0.1.0; this is not the final mission target. PM uses the
simplest version convention suitable for the repository and binds the delivered
version to its exact commit/branch/push and observed checks in the existing
runbook. Chief reviews that exact candidate; Steward reconciles completeness.
Keep failed/not-run checks and hardware limits explicit. An intermediate commit
may save work without declaring a working release. No duplicate release writer
or reporting system is required. Releasing 0.1.0 does not end the assignment:
continue substantive implementation and independent verification toward the
original full, reliable MISHKA outcome; preserve functions and report intermediate
limits truthfully.

## Execution Phases

0. Source/candidate baseline and fresh read-only runtime/access inventory.
   Research already resolves scope/policy choices; verify environment facts.
1. Preserve existing authentication, synthetic Max ownership and lifecycle
   boundaries; discover shared causes and previously unnamed interactions,
   reproduce demonstrated gaps before a fix and adapt regression/live coverage.
2. Incremental US1 → US2 → US3 → US4 → US5 checks/repairs on a fixed candidate.
   Independent read-only review may overlap implementation on disjoint evidence
   paths, but only PM assigns writers and integrates shared runtime changes.
3. Chief checks the candidate in a real browser and remote service; PM resolves
   findings, then reruns only affected final checks. The recovery candidate's
   saved checks are historical evidence, not this phase's pass.
4. Execute the mixed whole-demo acceptance and protected file access, record
   actual entry and limits in the existing runbook, then hand off.
   Do not stop at US1 or after the two recovery JavaScript fixes.
   Three visits and the initial 30 tasks are samples and a work map, not a scope
   ceiling or stop condition; extend them when causal findings reveal more work.

No `speckit-converge` run is valid yet: first execute implementation against
tasks.md and record evidence. Later convergence may append remaining tasks;
it cannot stand in for the first implementation or fabricate completion.

## Complexity Tracking

No architecture expansion or new application dependency is proposed. Validation
documents are the official Spec Kit design outputs, not a parallel reporting
system. Keep final observed outcomes in the existing demo runbook and tasks;
do not proliferate daily reports.
