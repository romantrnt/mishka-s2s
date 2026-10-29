# Optional RFID software path

The current implementation preserves the existing exhibition handset, welcome, menu and hard-hangup behavior. Optional identity is independent of the runtime profile; it does not select clinical scenarios. Clinical+optional_rfid is rejected for this software slice.

## Modes and trust

- Normal startup/reset/hangup is dark: all18 lighting channels0, no standby breathing task.
- MISHKA_IDENTITY_MODE=guest_only is the default. MISHKA_IDENTITY_PROVIDER=disabled grants no profile access.
- optional_rfid accepts a bounded asynchronous lookup. A still-current typed authorized decision can enter rfid_ready, run one wave, and continue directly into breathing.
- synthetic provider is restricted to mock profile with fake/simulator hardware and explicit constructor fixtures. Empty/unknown fixtures do not authorize a card. No actual reader or Max service is implemented.
- Operator HTTP/WS authorization only permits control requests; it is not child authorization. Generic ingress cannot assert internal identity outcomes.

Default bounds are lookup2s, ready60s capped by provider expiry, debounce0.5s, all validated/configurable. Pending/ready repeats do not extend deadlines. Lift during lookup immediately starts guest and invalidates the old request. Cards during a call are ignored. Different cards while down replace ownership. Explicit removal/cancel clears identity. Real reader field-loss/removal mapping awaits Max specification.

Expiry while off-hook stops private work and enters session_closed until actual handset down/up. Hangup/reset/cancel/guest conversion scrub names, age/provenance, permissions, schedule refs, profile and dialogue caches. An urgent incident marker and its SAFE_STOP policy remain; these operations do not resolve it.

## Lighting and cancellation

The canonical runtime remains the state authority; application owns I/O/tasks; UARTLEDController drives the existing effect engine. RFID acknowledgement uses seven120ms groups, holds all18 ON, then first breathing target all1/1000ms. No clear/OFF is inserted between hold and breathing or before lift preemption from ready. Existing intentional wave OFFs and the gradual low point of breathing remain.

Physical writer work is bounded and generation-fenced before lines/retries. A line already in progress cannot be unsent. Close drains a terminal dark frame or reports timeout/failure; adapter close follows separately. Host write/drain is not firmware ACK. Hardware fade continuity, actual reader/DB authorization and physical acceptance remain open.

## Readiness and validation

Guest admission does not depend on optional identity provider availability. identity_ready means provider availability, not current child authorization. reader_ready remains false and synthetic never claims production_ready.

Regression evidence is delivered separately in the implementation report: immutable baseline comparison; typed identity, privacy/age, lookup/session races; real canonical→application→UARTLEDController/effect-engine trace; serial writer ordering; ASGI auth/admission; UI and Node checks. The remaining TestClient/simulator hang was traced to sandbox-denied local socketpair writes, not an application or package fix. Standard per-command approved local IPC execution completes the bounded simulator/lifespan/transport suite on canonical Python3.12.3. No OS/security setting, production module or asyncio transport was changed. Real profiles, secrets, devices and external services were not used.

## Release and rollback

Work remains on feat/rfid-production-20261029; no push/merge/deploy is part of this software phase. Do not enable production identity until Max provides reader/UID/topology, trusted decision/schema/scopes/expiry/revocation and firmware contracts, then physical acceptance is completed. No real DB migration is invented or applied. Disabling optional identity returns to the dark guest path; code rollback uses ordinary revert commits, preserving history.

## HTTP/PTY acceptance boundary

The dedicated tests/test_rfid_http_acceptance.py exercises actual ASGI HTTP middleware/routes and encoded CRC/sequence-checked PTY frames. Positive cases explicitly assume control-plane READY on the same real StartupLifecycle object after first asserting the genuine mock startup is DEGRADED/full_e2e_warmup_required. They do not forge a nonmock E2E result, monkeypatch admits_users, or disable auth/owner/privacy checks. /ready remains503 and production_ready=false. A separate default-mock regression proves authenticated RFID remains503, unauthenticated access remains401, and no lookup/session is created.

The positive cases verify authorized ready→lift→hangup, pending lookup→guest lift→hangup→late allow rejection, redacted public state, dark reset, and bounded lifespan shutdown. A cancellation-resistant test fixture is always released/drained inside the client context. This is synthetic control-plane acceptance, not speech-model readiness, a TCP-server deployment test, real RFID/backend integration or physical LED acceptance.

Tests need normal local AF_UNIX/socketpair IPC for worker-thread/event-loop wakeups. If managed execution denies send with EPERM, preserve the failure evidence and request the standard explicit per-command approval; do not alter OS/security settings, replace the transport or add heartbeat workarounds to production. Canonical validation uses isolated Python3.12.3 and declared lightweight package pins; no torch/CUDA/models or real data are required for this test boundary.
