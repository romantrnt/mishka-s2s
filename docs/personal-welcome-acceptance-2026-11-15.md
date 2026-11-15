> Historical report: caregiver-delivery and adult-resolution behavior described below has been removed. Current local-only behavior is documented in [OPERATIONS.md](OPERATIONS.md#local-safety-response).

# Personalized welcome acceptance candidate, 2026-11-15

This is an isolated review candidate, not a deployment or production/clinical
acceptance. The external Max client and its data are unchanged. Main is untouched.

## Exact behavior

For a currently authorized Max JSON profile with a usable first name, the intended
single utterance is `Привет, ИМЯ. Как твои дела?`. The name comes only from the
accepted private profile. There is no LLM-generated name, nickname or declension.
The bounded name structure, known profanity/instruction backstops and existing
trusted-output validator reject questionable values rather than repairing them.
These checks do not prove that an arbitrary string is a person's real name.

Preparation starts during the ringtone. At five seconds after the accepted lift,
the ringtone is stopped and the application chooses one outcome:

- Valid trusted TTS PCM ready before the deadline, with the same live
  profile/request/epoch: owned personal welcome. Clean Silero is the personal
  fallback while optional private RVC is cold, missing, failed or invalid. A valid
  RVC result replaces it only while preparation is current and before the deadline.
- Missing/invalid name, unsupported PCM port, invalid PCM from TTS, failed or late TTS:
  the existing recorded PHRASE_001. A still-authorized profile then follows the
  existing menu path without another name/age interview.
- Revoked/expired/replaced identity: the existing identity/session shutdown path,
  not an old-name greeting or silent reassignment to a guest.

Only the selected PCM is played once. Late TTS is discarded, never played after
the generic fallback; late RVC cannot replace or replay the selected personal PCM.
Native inference may finish after cancellation, but its result has no playback authority.
The five-second boundary is the application dispatch time, not a measured audible
onset: Silero's existing pre-roll and the device buffer still require listening tests.
No additional five seconds are added after a long ringtone file.

Only matching successful personal playback completion opens `personal_welcome_listen`.
The main menu does not begin over the question or automatically after it. One
nonempty trusted STT result that passes the input safety gate starts PHRASE_007 and
the existing menu tutorial. The response is not a new clinical mood assessment.
Danger disclosures and explicit requests for an adult retain safety priority;
uncertain classification uses the existing PHRASE_074 retry. Empty/untrusted STT
is not an answer. Failed playback is not reported as successful completion and
ordinary speech cannot bypass a failed retry into the menu.

While listening, 5 keeps existing help and 7 explicitly opens the main menu.
Other keys do not encode a mood. Hangup fully resets the exhibition session.
Guest onboarding remains separate and does not borrow the last authorized name.

### Silence limitation

There is no new answer-silence duration or automatic repeat timer. Pure silence
can leave the listener waiting; help/menu/hangup and identity expiry remain.
The general idle mechanism is not a reliable silence deadline because incoming
PCM can update activity. This is explicitly unfinished UX for live acceptance,
not a claimed automatic timeout/retry feature.

## Ownership and privacy boundaries

- Private PCM has its own playback ID, lease, epoch and session binding. Identity
  capability/expiry and profile/request binding are checked again immediately
  before physical playback, including after old-audio cancellation is drained,
  after the native playback lock, and at native stream start/write boundaries.
  Ordinary queued asset stop-generation semantics are preserved.
- Capture-time acceptance and private-listening phase ownership reject delayed
  microphone callbacks from the greeting or retry audio, not just frames that
  happen to be queued while the current state is blocked. Pending STT and bounded
  VAD partial-frame/pre-roll buffers keep their original listening-phase owner.
  A completed retry cannot reauthorize an old ordinary answer; a danger disclosure
  or adult request remains safety-only while its physical/session owner is live.
  A fresh answer overlapping surviving old pre-roll may conservatively remain
  safety-only until that utterance ends or new silence naturally replaces the
  bounded old pre-roll. The next fresh answer is accepted. This is an explicit
  live-acceptance UX boundary, not acoustic calibration or a new VAD threshold.
- The complete name-bearing sentence bypasses the global TTS text cache. No name,
  phrase or PCM is put in effects, public diagnostics, command metadata or history.
- Optional RVC calls only the primary `convert_private` path; the ordinary
  `ResilientVoiceProvider.convert` path is never used for name-bearing audio.
  Real file-based RVC uses a verified writable Linux tmpfs and a private temporary
  directory, with no disk-backed fallback. Selection diagnostics use fixed codes:
  `personal_welcome_tts_fallback` for clean TTS or `personal_welcome_ready` for
  a timely valid RVC result; errors expose no name, phrase or vendor message.
- Raw vendor stderr is discarded, so a private error cannot contaminate a later
  ordinary error. Structured protocol errors and counters remain. This deliberately
  sacrifices raw vendor stderr troubleshooting, without restarting a warm worker
  for every successful greeting.
- Repeated cancellation drains/reaps the RVC worker before deleting temporary
  files, preventing a late writer from recreating them. Completed native TTS job
  references are released rather than retained by an idle worker thread.

These are application-level protections, not guaranteed memory zeroization,
swap/core-dump prevention, vendor-cache auditing or survival of abrupt SIGKILL.

## Additional supervised adult checks

1. Authorized card, lift: hear the exact name-bearing greeting once after the
   ringtone, with natural pronunciation and no overlap. Measure audible delay.
2. Answer normally: no menu while the question or answer is still playing/captured;
   after one accepted answer, hear the existing menu. Check a short answer as well.
3. Stay silent: confirm the documented waiting behavior, then use 7; repeat with 5.
   Do not infer a new automatic silence deadline from the general idle timer.
4. Hang up during preparation and during the spoken name; lift as guest. No old
   name, delayed greeting or old answer should return. Repeat with identity expiry
   or revocation in the controlled demo setup.
5. Check microphone echo and a late retry: greeting/retry audio must not become the
   child's answer. Synthetic adult safety tests still must not claim real delivery
   to an adult when no real transport exists.

Physical audio, real-model synthesis latency, pronunciation, child-speech acoustic
accuracy and production Max approval are not established by mock/synthetic tests.
The earlier candidate's adult-handoff dependency is historical: caregiver delivery
was removed in favor of the local safety response linked above. Its acceptance
results do not establish the current safety path or this optional-RVC change.

## Historical verification before the optional-RVC fix

The earlier frozen integrated candidate passed **1370 tests and 4 subtests in 499.74
seconds**, exit 0, with the unchanged external Max demo bundle. All 147 source,
test and JSON freeze hashes still matched after the run. Test-log SHA256:
`f3a6eb448abea6e4337b6c292090cecc52a48f241269d1cb02ed9466954d8f69`.

Independent adversarial review reproduced and re-tested two ownership defects:
private PCM waiting past identity expiry at native audio boundaries, and an old
ordinary answer crossing a retry through pending STT or a residual VAD/pre-roll
buffer. Both were closed with original repros, fresh-answer controls, safety-only
cases and asynchronous ownership checks. Ordinary queued-asset semantics remain.

The earlier full run (1324 passed, 4 failed) exposed four stale fixture expectations
for the explicit ringtone-stop effect and added listener state; those assertions
were updated to preserve exact ordering and timing, not removed. Partial and
interrupted runs are excluded from the final full-suite result. Physical devices,
real synthesis latency/pronunciation and clinical coverage remain unverified.

## Optional-RVC fix verification, 2026-11-16

Before the production-code change, the new blocked-private-RVC regression failed
in both normal and cancellation-resistant cases: the five-second timer selected
`welcome_started` / PHRASE_001 despite completed trusted TTS (**2 failed, 78
deselected**, exit 1). After the change, the focused suite passed **122 tests in
37.88 seconds**, exit 0:

```sh
env PYTHONPATH=/home/server/mishka-worktrees/logic-audit-20261115:/home/server/mishka-stage-testdeps-20261113 \
  /home/server/mishka/.venv/bin/python -m pytest -q \
  tests/test_private_welcome.py tests/test_private_greeting_rvc.py tests/test_profile_onboarding.py
```

Coverage includes TTS/RVC deadline boundaries, missing/failed/invalid private RVC,
timely conversion, cancellation-resistant late results, hangup, authorization
revocation, profile replacement, and invalid/late TTS retaining PHRASE_001. These
are synthetic application/provider checks, not real-model or physical-audio
acceptance. The full suite was not rerun. `tests/test_optional_identity.py` is
absent from this worktree; existing profile-onboarding checks cover identity loss
and guest reuse. No dependencies or deployment files changed.
