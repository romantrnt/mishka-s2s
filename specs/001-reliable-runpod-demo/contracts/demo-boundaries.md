# Existing Demonstration Boundaries

This is an acceptance contract for existing interfaces, not a new API design.
See the existing repository contracts:
[remote device](../../../docs/remote-device-contract.md),
[remote panel](../../../docs/remote-phone-ui.md),
[remote runbook](../../../docs/remote-stand-runbook.md).

## Operator protection and observation (FR-001, FR-015–FR-019)

- The current HTTPS operator gateway protects HTML, state, assets and WS.
  Reuse existing Basic-to-HttpOnly cookie access; never place secrets in URLs,
  JavaScript, screenshots or reports. Exact allowed Origin applies to cookie
  mutations and WS. Unauthorized and foreign-origin requests remain rejected.
- GET /stand/state and /stand/simulator/state expose redacted canonical state,
  not raw profile/private transcript. GET /stand/simulator/journal is bounded
  and carries epoch/cursor/gap metadata; it cannot authorize stale dispatch.
- /stand/ws is the remote virtual-device receiver. The generic session WS path
  must not bypass that adapter. Reconnect receives current state; it does not
  fabricate handset-down or silently reset stale-state fences.
- In an ambiguous restart namespace, the operator sees reload guidance.
  Reload establishes a fresh observer; ordinary queued text/device actions
  cannot cross sync loss, while terminal callbacks and cleanup remain possible.

## Device ingress and canonical activity (FR-003–FR-008, FR-014, FR-016, FR-018)

- POST /stand/simulator/action accepts existing strict handset/button/RFID/reset
  actions. POST /stand/simulator/fault permits bounded virtual input faults.
  Their current shapes and bounds are in data-model.md and the original contract.
- Named synthetic cards pass through unchanged Max capture/client/TLS/SQLite
  before validated profile readiness. No raw UID/patient JSON or internal
  authorization event is accepted as a public shortcut.
- Existing /stand/text is an engineering input, never evidence of real hearing.
  Its queued dispatch must respect synchronized admission just like device input.
- Physical and virtual inputs reach the same canonical activity logic. The UI
  projects affordances and 18 light channels rather than implementing a second
  scenario machine or decorative animation unrelated to actual light state.
- Preserve the seven existing root paths and contextual mapping. A fresh valid
  game-menu choice is immediate during guidance; held/duplicate/stale choices
  do not create another owner. Other scenes retain their intended semantics.

## Audio, capture, expiry and privacy (FR-009–FR-014, FR-019–FR-020)

- Browser PCM includes the existing session/generation ownership. Started,
  completed and failed acknowledgements retain their exact playback lease;
  do not manufacture a current lease for an obsolete callback.
- Backend microphone eligibility plus explicit browser consent permits capture.
  Late permission, lost connection, cleanup and page exit stop capture/output;
  the next eligible enablement remains explicit. Fixtures establish this
  lifecycle only; real spoken/heard exchange needs separate evidence.
- Playing public guest stories survive idle; pause waiting uses 120 seconds
  after the pause announcement. Max expiry remains authoritative and independent.
- Existing validation, safety responses and private speech boundaries remain.
  No claimed adult send, invented appointment, child recording or archived
  voice-training activation is added.

## Infrastructure/operator files (FR-021–FR-023)

Verify current authorized Runpod state, cost, persistence and access; last-known
pod/process IDs do not establish freshness. Agents use approved SSH; Roman's
protected Jupyter workspace is verified as file access, with credentials
exchanged only privately. No unprotected notebook or permission bypass is an
acceptable substitute. Publication/deployment is outside this artifact's
authority. Hardware/clinical approval remains a separate acceptance boundary.
