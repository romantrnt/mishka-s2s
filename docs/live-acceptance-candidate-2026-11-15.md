# Live acceptance candidate, 2026-11-15

This branch is for supervised adult acceptance, not a production or clinical
release. Main is intentionally unchanged. The external Max client, certificates,
RFID database and original checkout are not modified by these source changes.

## Implemented and verified scope

- False simulated adult receipts are rejected at adapter and canonical boundaries.
  Missing transport uses the existing honest delivery-failure recording, never a
  claim that a real adult received the request. See the separate incident report.
- Semantic unavailability is a blocked/retry decision, not an invented concern.
  Known dangerous disclosures and explicit caregiver requests retain priority,
  including while an ordinary welcome timer advances the same session. Hangup
  and identity-generation fences still reject stale work.
- Known alarming substring collisions were corrected with real-path regressions.
  Low-quality STT retains text for safety-only inspection but cannot collect a
  profile or drive normal conversation. Its score is still an uncalibrated heuristic.
- Ordinary bear dialogue is no longer rejected before the LLM by a seven-category
  lexical allowlist. Other modes retain their scope. Input/output safety, history,
  cancellation, age policy and five-turn / 120-second dialogue bounds remain.
- Output validation rejects demonstrated secrecy/isolation instructions and
  unsupported claims of external calls/messages/delivery. Direct requests such as
  calling a parent route to the real handoff boundary, not generated promises.
- An already-authorized, live Max JSON profile can receive the private personalized
  welcome and then one safety-checked answer before the menu, without re-interviewing
  for name/age. The recorded fallback retains the existing menu path. See
  `personal-welcome-acceptance-2026-11-15.md` for exact timing, fallback and silence limits. Missing fields remain unknown. No raw UID
  or second identity database is introduced. Guest, denial, expiry, revocation and
  hangup remain separate. Medical appointments are not inferred from unrelated facts.
- Root buttons 4 and 7 reach their existing story and day/plans entry points.
  The welcome deadline is exactly five seconds after the accepted lift event;
  independent tests cover immediately before, exactly at and after the deadline.
- Diagnostics expose bounded fixed codes and timing, not child speech, names,
  model output, exception prose or a transcript hash.

## Explicitly unfinished product work

### Spoken story menus

The current global help key is 5, while existing story-menu recordings advertise
five stories on keys 1–5. They cannot both be truthful. This candidate preserves
help, suppresses those two mismatched recordings and uses approved PHRASE_075
once per session. The operator panel lists the actual 1–4 stories, 5 help, 7 back.
Help acts immediately; other keys stop the hint and require release/new press.
There is no invented menu or repeat prompt for an incomplete recording.

This does **not** fulfill a complete spoken menu in every block. Main-menu
interrupt/repeat/session behavior is preserved and tested. A corrected approved
recording, or an explicit five-story navigation decision preserving help, is still
required. See `story-menu-content-blocker-2026-11-15.md`.

### Generated personal stories

The old path could enter a silent playing state from 4 then 2. It had no working
full-story generation/audio pipeline. The existing bounded dialogue returns short
responses, while the story contract requires a validated age-specific complete
story, duration and safe ending. Treating a short answer as that implementation
would be false completion.

The candidate uses owned PHRASE_080 and returns to fixed-story selection. Completion
releases its audio lease; stale completion cannot affect new playback. Help/back
also work in legacy story states. Personal-story generation remains unavailable.

## Local model investigation, not safeguarding certification

On the existing GPU server, qwen3:0.6b misclassified common short replies with low
confidence and missed demonstrated danger. qwen3:14b improved the small synthetic
comparison (16/18 expected classes, all eight dangerous samples urgent), but those
18 examples do not establish real-world accuracy. No confidence threshold was
lowered to conceal failures.

The new defaults use qwen3:14b with a shared 4096 context for input/output validation
and generation. Explicit operator overrides are honored. Switching the same model
between 1024 and 4096 repeatedly cost 2.3–7.3 seconds on this server; matching them
avoids that demonstrated reload penalty. Lower-capacity hosts need separate resource
and latency acceptance and must not silently fall back to an unverified classifier.

An isolated actual application -> input gate -> actual local LLM -> output gate
probe, with mock TTS/voice/playback, produced a model answer for each of five ordinary
replies (yes, no, dinosaurs, pet, school), about 1.3–2.0 seconds per text turn. Two
explicit danger probes generated no dialogue and took the honest handoff-failure
path. A real model's false promise to call a parent was reproduced before the fix
and blocked afterwards. These are text-path results, not physical-audio latency.

## Remaining acceptance and safety limitations

- No accepted real adult transport/recipient, durable delivery queue, authenticated
  receipt/resolution ownership or restart recovery. Do not rely on this system to
  summon an adult. An adult must remain present during acceptance.
- Exhibition hangup clears the session. Retention of undelivered concern and the
  concern-versus-support policy need explicit product/safeguarding decisions.
- Synthetic regex/model tests do not certify comprehensive threat detection or
  Russian child-speech accuracy. STT quality and thresholds need acoustic evaluation.
- Unknown RFID UX, scan during a conversation and identity TTL have not been
  redefined. No foreign profile is assigned as a workaround.
- Production Max endpoint/credentials/schema acceptance and physical sound,
  microphone, barge-in and real lighting still require separate verification.
- Content and demo readiness flags must remain honest; technical `/ready` is not
  `production_ready` and must not be presented as such.

## Adult live test checklist

1. With the handset down: darkness. Lift without RFID: normal wave and greeting
   after five seconds, not four. Replace before five seconds: no delayed greeting.
2. Apply a known authorized demo card, then lift: wave/breathing without a blackout;
   the profile is consumed without asking name/age again. Hang up, then try as a
   guest: no previous name/profile is reused.
3. Main menu: interrupt with a button, release, then choose. Let it finish once,
   answer the repeat prompt yes/no and revisit within the same session. Hangup
   starts a fresh session, rather than resuming old speech or menu choices.
4. Talk mode: ordinary yes/no, dinosaurs, pet and school replies should reach a
   relevant answer rather than instant fallback. Stop speech with a button/hangup;
   no old voice should resume. Record the observed delay, not private speech.
5. Story chooser: one brief hint, actual keys 1–4 select stories, 5 remains help,
   7 goes back. Key 4's new-story path honestly falls back to existing stories;
   it must not become silent or claim a generated story is playing.
6. An adult says a synthetic request to call a parent or a synthetic danger phrase:
   no claim that an adult was contacted. With no real transport, the honest
   delivery-failure guidance is expected. This is not an emergency-service test.
7. Hang up during response preparation and during playback, wait, then lift again:
   no stale audio, previous profile or prior private conversation returns.

Do not have a child perform distress/abuse scenarios. Passing these seven checks
cannot establish clinical coverage, real handoff delivery, acoustic calibration,
complete menu content, or production Max approval.

## Verification record

The final frozen-snapshot test result is recorded with the candidate commit/PR.
Interrupted runs and targeted subsets are not represented as a full-suite pass.
Independent review confirmed and re-tested the explicit-help race, silent story
trap, help-key priority and stale fallback lease fixes. Physical audio is unverified.

Prior a9105e5 local aggregate, before the personalized-welcome change: **1204 passed and 4
subtests passed in 708.91 seconds**, exit 0, with the unchanged external Max demo
bundle. The earlier interrupted runs are excluded. Canonical acceptance, browser
fake-Audio regression, 126-file AST parse and the 108-asset audio catalog check
passed. All 52 Max manifest hashes matched; 31 original-checkout source hashes
matched their preserved baseline. No live physical-audio acceptance is implied.
