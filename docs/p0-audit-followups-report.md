# MISHKA P0 audit follow-ups

Date: 2026-10-26. Isolated branch `fix/p0-lifecycle-20261026`, checkout
`/home/server/mishka-fixes`. Clean HEAD baseline: `d8af32c40adacabdb28e84b1cc766cc0f20f6782`.
The live `/home/server/mishka` checkout, its processes and devices were not changed.

## Changes

- `DialogueFallback` is internal-only: generic event parsing rejects it for
  WebSocket and CRC-valid UART input, and canonical runtime requires an
  out-of-band application capability. Payload `source`, `actor` and
  `authorized` carry no authority. The real bounded provider path still
  emits tracked PHRASE_095 on its limit.
- Guest age is taken from validated canonical exhibition selection. The
  system prompt and `ProfileContext` now agree on age 7 / band 6-7 and its
  source/policy. Arbitrary external profile `age` is ignored; DOB-derived
  profile policy remains authoritative. Invalid canonical age keeps the safe
  fallback.
- Public canonical snapshots omit private history and bounded dialogue turns.
  Completion and stand result storage use one allowlist, and public stand
  diagnostics redact raw UART frames, event/effect payloads, trace metadata
  and audio runtime details. A synthetic private marker remains available to
  the private voice prompt and is absent from state, stand snapshots,
  diagnostics and WebSocket completion.
- Restored `STORY_MENU` definition that a literal backslash-n had hidden
  inside a comment. An actual story-menu transition lights digit channels
  1-7 and only resume/stop fills.
- Recovery awaits asynchronous dependency readiness, accepts synchronous
  readiness, validates a Mapping with boolean `ready`, and fails closed for
  malformed or unavailable status. Resource and warmup gates remain intact.
- Lifespan closes the stand-owned PTY, adapter and executors on normal exit
  and startup failure. Loop-owned task cancellation stays on the loop;
  blocking worker shutdown runs in a thread. Externally owned application
  objects are not closed. Closing after hardware stop avoids duplicate audio
  input and lighting output close calls.

## Tests and baseline

Each issue was reproduced with a red regression before its fix. New
`tests/test_p0_audit_followups.py` has **9 passing tests** covering actual
WebSocket/UART ingress, provider fallback, canonical age/profile policy,
public privacy projection, real story transition, async/sync/malformed
readiness, owned shutdown and external ownership. Together with existing
hardware stand tests: **36 passed in 6.84 s**. No real device, physical audio,
LED, model or paid service was used.

Final full headless suite after the final privacy projection fix:
**328 passed, 16 failed, 1 xfailed in 37.87 s**. The same 16 failure names
were present immediately before this group (**319 passed, 16 failed,
1 xfailed**); no prior failure is counted resolved without evidence. The
initial full pass before the final nested-trace fix had the same
**328/16/1** counts. Historical clean HEAD had **272 passed, 17 failed,
1 xfailed**. Both staged and unstaged `git diff --check` passed. Tests
used the existing `/home/server/mishka/.venv` only, with an absent
`/tmp` `MISHKA_ENV_FILE`. No dependencies were installed.

The 16 standing failures remain in canonical acceptance (1), exhibition
timer/audio (3), onboarding (1), content readiness (1), operational health
shape (1), S2S chunking/queue/streaming (7) and Silero TTS (2). Their
individual names are recorded in the final pytest output; this group
does not claim to fix them.

## Open decisions and limits

Positive production `AdultResolved` authority is still pending the product
choice between a protected shared-token operator panel and individually
authenticated staff. The internal test seam is not a production endpoint;
existing generic operator resolution is fail-closed. Concern/hardware
incident reset semantics are a separate product decision. A proposed
concern/reset expansion was rejected by automatic approval review before
execution and was not retried. Existing urgent incident guards remain.

The separate `max_session_seconds=1200` policy remains unanswered and
unused; this group did not enable it. No IAM, credentials, new story/game
content, console.js changes in this group, push, merge, deployment, reset
or clean was performed. Prior staged and unstaged changes were preserved.

## Review amendment: shutdown exception path

Independent review identified that an exception from `manager.close()` skipped
stand-owned PTY/executor cleanup. A new red regression reproduced this with a
failing `voice.close()`. Lifespan now places owned hardware stop and
`stand.aclose()` in an outer `finally`; the original voice exception remains
visible and the stand closes once. Targeted audit-followup file: **10 passed
in 2.56 s**. The full-suite **328/16/1** count above predates this narrow
cleanup amendment; no second full suite was run while the streaming/Silero
failure analysis is pending.
