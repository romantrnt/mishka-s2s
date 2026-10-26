# MISHKA P0 AdultResolved incident boundary

Date: 2026-10-26. Branch: fix/p0-lifecycle-20261026 in
/home/server/mishka-fixes. Clean HEAD baseline: d8af32c40adacabdb28e84b1cc766cc0f20f6782.

## Implemented and verified

- A new safety_handoff entry generates a server-owned opaque incident_id.
  Urgent incidents keep that ID through acknowledgement, handset-down and
  generic application reset. A later incident on the same session ID gets a
  different ID. The ID is not a credential.
- Handoff requests carry the generated incident_id separately from handoff_id.
  The safe adult_handoff_request effect carries it even when delivery is
  unavailable. Simulated delivery acknowledgement carries the ID and cannot
  close the incident. An acknowledgement with a different ID is ignored.
- AdultResolved has two independent gates: an out-of-band server argument to
  canonical runtime and an exact, nonempty incident_id match. Event source,
  actor and authorized fields in the payload have no authority. Wrong,
  missing and old IDs leave safety_handoff intact even with a fresh version
  and sequence. Replayed successful resolution cannot close a later incident.
- Generic parse_event rejects AdultResolved, covering authenticated
  /ws/{session_id}, /stand/raw and physical UART dispatch. Direct generic
  application/canonical event ingress also fails closed. A private
  _resolve_adult_incident_internal seam exists for trusted in-process tests;
  no production route invokes it. Adult identity and production authorization
  are intentionally pending product choice.
- Existing unit tests that had treated a generic AdultResolved event as
  authorized now use the explicit incident-matched internal runtime argument.
  They still test the same post-resolution state.

## Evidence

New tests in tests/test_p0_adult_resolution.py: **7 passed in 2.70 s**.
They exercise valid-CRC /stand/raw, authenticated application WebSocket,
forged physical/LLM/source/payload events, missing/mismatched/old incident IDs,
matching internal resolution, replay, acknowledgement, urgent hangup/reset,
and handoff request/effect binding. Before implementation, four tests reached
the missing incident-ID failure; the raw-route fixture initially returned
503 until the established test-only admission override was applied. The
authenticated WebSocket fixture used a pending handoff fake after inspection
showed that simulated acknowledgement audio caused an unrelated
stale_session_generation result.

Focused prior lifecycle/acceptance/graph and new tests: **32 passed,
1 failed**. The sole failure is the previously recorded acceptance test
expecting story playback while the handset is down.

Final full headless suite from the isolated checkout cwd: **319 passed,
16 failed, 1 xfailed in 36.47 s**. The same 16 failure names were present
after bounded S2S (**312 passed, 16 failed, 1 xfailed**). Historical clean
HEAD was **272 passed, 17 failed, 1 xfailed**; those counts do not establish
that every historical failure was fixed. Staged and unstaged git diff --check
passed. Tests used the existing /home/server/mishka/.venv with an absent
/tmp MISHKA_ENV_FILE; no dependency, device, audio, LED or paid service
was installed or invoked.

## Pending authorization and scope

The product choice is still open: whether a protected operator panel using
a shared token suffices or an individually authenticated employee is
required. A shared bearer token is not proof of adult identity. No public
closure endpoint, account, token, SSO integration, push or deployment was
added. Existing operator flows that sent AdultResolved through generic
WebSocket/UART now fail closed until an authorized production path is
designed; this is an intentional compatibility change, not a completed
rollout.

One proposed change remains **denied and unexecuted**: broadening generic
reset preservation from urgent incidents to all concern/hardware
safety_handoff incidents in canonical_contract.py and application.py.
Automatic approval review rejected that action as a product behavior
expansion without clear approval for concern incidents. Therefore an
existing concern/hardware incident can still be cleared by generic reset;
its incident_id is generated, and any AdultResolved event still requires
matching identity and internal authority. Concern/hardware reset semantics
remain a separate product decision and do not block the completed urgent,
incident-ID and ingress guards. An earlier attempt to accept
stale_session_generation as a WebSocket test result was also denied as test
weakening; it was not executed. A pending handoff test fake isolated the
route instead.

Prior staged lifecycle, unstaged audio/S2S changes and console.js were
preserved. No changes to the live /home/server/mishka checkout and no
commit, merge, reset, clean, push or deploy.
