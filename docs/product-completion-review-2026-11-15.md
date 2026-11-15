# Product completion candidate, 2026-11-15

Base: `c4c94a1722eb8ba2a4e2488b754cfaf3a45db9b9`. Separate local review branch. This document is not a deployment, clinical validation or physical acceptance claim.

## Changes

- Six distinct visual games: catch one target, free lights, find two different lights, overlap two pressed buttons, hold then release, and repeat a demonstrated sequence. Gameplay uses 1–4; help 5 is never a game target. Exit 7 requires its explicit 6/7 confirmation. The sequence has three finite 2/3/4-step rounds. Timings and round counts are engineering parameters, not clinical norms.
- Existing age policy now drives actual response windows and bounded hints through private game/run/round/epoch-owned timers. Timeout does not score a failure, lower ability, or fabricate success. The hold release cue comes from a real observed down-edge deadline, not a nonexistent physical `Held` event. A pending exit survives clinical hangup without another light owner overwriting it.
- Local spoken menus for fixed stories, games, generated-story themes and day information share one descriptor for narration, selection, lights and console. Age pages contain 1/2/3 choices; 6 changes page, 5 remains help and 7 is back. All five verified fixed stories and the sixth game are reachable. The first press interrupts narration; release plus a fresh press selects. Repeat/skip state is kept for the session. Unknown/changed capabilities cannot remap an old displayed slot to a different action.
- Day choices expose public date and configured weather, and only authorized, current loaded schedule choices. Discovery performs no provider calls or disclosure of schedule titles. A provider can still fail at request time; configuration is not readiness.
- Complete original-story rendering has a separate bounded generation request, four required plot sections, whole-candidate checks before synthesis, measured age-specific duration, private TTS/RVC, one admitted render job and RAM-only playback ownership. It is not a short dialogue response masquerading as a story. Pause/resume reuses the same private PCM and never regenerates it. Failure produces the existing honest 080 and a working fixed-story chooser.
- Ordinary generated dialogue is constrained by the existing age word policy. Approved help/greeting recordings are not truncated to this cap.
- Native playback returns immutable per-request accepted-write progress. Both generated and recorded stories use their own explicit resume offset, including queued-before-start and close-vs-pause races. A stale previous stream position cannot jump the current story forward. Incomplete playback cannot claim completion.
- A changed menu capability cannot silently reuse a previously announced button for another item. Replacement narration has a completion gate; failure, on-hook or interruption cannot silently enable the new mapping. Help and back remain available.
- Games inherit accepted button levels across chooser/root transitions without fabricated down-times. A wrong key still held from a preceding menu/game cannot disappear and permit simultaneous-game scoring. This tracks admitted logical edges, not unobserved hardware levels.
- Safety retains priority over menu answers. Optional capability discovery does not break dialogue, hangup cleanup or retained clinical memory when an optional provider is absent. Late nested story fallback/ready callbacks cannot restore or index a session removed during an await.

## Full-story backend gate

Generation and the separate mini-validator require individually bound operator-local receipts. Empty configuration leaves full-story capability unavailable, with no fake successful story. The loader is connected to the real engine and existing warmup/controlled metadata refresh. It adds no public enabling endpoint or continuous polling. See `story_backend_acceptance.md` for the exact schema and procedure.

Story-only requests use a bounded context estimate, `truncate:false`, `shift:false`, exact serving version/model/digest/runner checks before and after, and a mandatory clean `done=true, done_reason=stop` terminal. Input overflow, `length`, missing terminal, expiry or changed bindings fail closed. The byte budget is a resource estimate, not a tokenizer or semantic guarantee. Ordinary dialogue options are not mutated.

Read-only observation of the current server and the matching official Ollama/llama.cpp source supports the protocol trace, not installed-binary provenance, real overflow behavior, semantic quality or physical performance. No model inference or server change was performed for that investigation. A real deployment receipt has not been installed.

## Verification status

The immutable frozen3 tree `4e1264383811830df2daea20ac875f01165fcd8f` completed **2097 passed, 4 subtests passed**, no failures/skips, in 726.51 seconds. Its 482 files were independently matched to both SHA256 manifest and Git tree. The canonical verifier and both JavaScript checks passed (11 guide assertions plus fake Audio regressions). All 53 original Max fixture files remain byte-identical.

A fresh independent Astra review then found two additional P2 edge cases not covered by that full run: silent menu-slot remapping after capability loss and dropped held-key state on game entry/re-entry. Both original independent probes are now green in isolated fixes. Menu refresh had 92 targeted passes; the broader held-level fix had 106; the combined correction has 46. These overlap and must not be summed as independent coverage. A new immutable full run is required for the corrected candidate.

Earlier diagnostic runs remain recorded: 1878 passed / 69 failed / 4 subtests, then 2082 passed / 6 failed / 4 subtests. Real capability/lifetime integration defects and navigation-fixture changes were diagnosed, not blanket-dismissed. The final six navigation tests now retain and strengthen explicit no-grant/no-provider-access checks, including the real unchanged Max demo path.

## Still not established

- Root recording 008 retains the existing fixed seven-activity map. It is not capability-adaptive; unavailable generation is explained inside its chooser. This content/product limitation is not silently claimed solved.
- Real model story quality, validator performance, pronunciation, actual sample rate, 60-second rendering availability, RSS and GPU VRAM. Full RAM PCM can be substantial: the conservative code bounds permit up to 28.8 million samples. Low-rate synthetic tests do not validate this memory envelope.
- Story context/shift changes can reload Ollama runners; the next ordinary dialogue request may reload again. Do not silently alter global dialogue settings to hide that cost.
- Fixed-story menu capability discovery currently performs synchronous asset verification (roughly 52 MiB for five WAV files per call in the review inventory). Warm review-host calls were around 37 ms median, with a first call around 120 ms. These are not hardware latency acceptance; measure input-to-stop under menu activity before deployment. No unsafe cache was introduced.
- Max's supplied client independently limits a card to 120 seconds from receipt or its earlier server expiry, not inactivity. User activity cannot extend it; its clear must override busy work. The separate requested 60-to-120 engagement lifecycle is documented in `engagement-idle-review-2026-11-15.md`; it never overrides Max authorization. Clinical Max configuration remains blocked; a rejected config change was restored byte-for-byte in its isolated copy, not applied here.
- Physical sound, actual LED delivery, simultaneous-key hardware limits, and the installed firmware match. The observed historical 6KRO constraint is not repaired in software.
- Real adult transport remains unavailable. No simulated ACK may be spoken as delivery.
- No changes to Max's source, main, live application, model configuration or firmware; no publication by this candidate.

## Suggested controlled live checks after separately approved installation

1. Authorized and guest lift flow, the five-second welcome boundary, personal greeting, one completed answer before root menu; hangup stops audio and applies the configured exhibition/clinical policy.
2. First entry, interruption, release/new selection, repeat yes/no and same-session re-entry in every local menu; traverse all story/game pages without losing help.
3. Each game with wrong/repeated/overlapping presses; hold cue without autorepeat; sequence spoken instruction before flashing, correct 2/3/4-step answers, help and exit at every phase.
4. Rapid lift/place and 5/6/7 combinations during menu, audio, pause and sequence demonstration; no stale light or audio owner after reset. Do not infer support for more simultaneous keys than firmware reports.
5. Ordinary conversation plus safe explicit help requests; inspect sanitized reason/latency diagnostics, not saved child speech. Do not treat this as a clinical safety evaluation.
6. Date, configured weather, absent/outage provider, authorized schedule and expired/revoked profile; no borrowed facts or hidden remapped choices.
7. Only after backend receipt and resource acceptance: a full original story, measured duration/latency, pause before first write and near its end, resume without regeneration, cancellation and ordinary dialogue afterwards. Until then this function is intentionally unavailable, not accepted.


## Local checkpoint status

The corrected frozen4 full run finished with **2108 passed, 5 failed and 4 subtests passed** in 741.11 seconds. The five failures were in two authorization/fallback test fixtures that replaced the provider *after* announcing a menu, correctly triggering the new capability-refresh gate. The fixtures now install the missing provider before initial day-menu entry; the original no-grant/no-provider-call and audible-fallback assertions remain. Both entire files plus scene safety and the eight unchanged P2 tests passed: **62 passed**. No application source changed for this correction. A fresh complete run of that exact corrected test tree has not yet been claimed.

The coordinating agent confirmed the new independent reviewer was explicitly started with model `gpt-6-astra`, reasoning effort `xhigh`, and no inherited turns. This confirms its configured launch, not quota/billing or the model configuration of older named reviewers. It matched all 484 frozen4 files and ran 174 integrated tests, the canonical verifier and both JS regressions successfully.

This local checkpoint deliberately excludes the newer RFID engagement-idle implementation, which is undergoing independent race review in a separate copy. It is not a deployment or production release.


The subsequent engagement-idle candidate is now described separately in `engagement-idle-review-2026-11-15.md`. The historical a5d4c0e checkpoint above remains unchanged.


## Final integrated software verification

Frozen5 tree `3a384ef9489b9110642f24565d560105a9980136`, including the engagement-idle corrections: **2157 passed, 4 subtests passed**, no failures/skips, 734.45 seconds. Canonical verifier and both JavaScript suites passed. The explicitly configured independent `gpt-6-astra`/`xhigh` reviewer matched all 493 files and the exact source union, retained original probes, and passed 40 final focused tests. All 53 Max source files remain identical to the original ZIP. After this full run, only reports and one trailing blank line in a retained test were normalized; the test AST and application code are unchanged. The two retained boundary tests were rerun after normalization. See the separate idle review for its complete scope and unresolved clinical-Max/physical/backend gates. No publication, main change or server deployment occurred.
