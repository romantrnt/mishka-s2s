# Private remote simulator

This launcher exposes the existing Stand with synthetic Max HTTPS/SQLite data,
browser audio and simulated devices. It never runs physical capture, never
forces lifecycle readiness, and always remains `production_ready=false`.
The clean baseline is faster-whisper `large-v3-turbo` (CUDA float16), Ollama
`qwen3:14b`, Silero `v5_5_ru` / `aidar` / 48000 Hz, voice conversion `off`.
Optional trained voices require separate, explicitly selected runtimes and
are unavailable in this deployment.

Current live checkpoint (2026-11-18): pod `fihihgl91rnwu4`, application PID
`23949`, source `263cac19c6b9dee6e485d39b16ef10a26738bf62` on
`codex/remote-device-simulator`, instance `67b2f42553e4f8fd`.
Open `https://fihihgl91rnwu4-8000.proxy.runpod.net/console` with the protected
credentials below. V14 passed strict **READY**, real PCM/VAD/STT/LLM/mandatory
validation/Silero/queue/silent playback, all 31 runtime hashes and the served
console, phone and phone-model clients. Initial state: no browser owner, handset down, no held buttons, all
18 channels dark. Actual browser/provider acceptance is in progress;
software readiness is not whole-product, human-hearing or physical acceptance.

V14 helper `23944` ran once after independent source and exact package review.
Six runtime files changed; all 31 baseline and candidate hashes were checked.
The 176444-byte archive SHA-256 is
`fd1d718fdc2949808cb39c4b4730c470ca20d0a028700e2f92dc29bd4a7733bd`.
E2E `41602a17-07a2-4e53-91f4-cd893da32a87` passed all eight stages;
bounded queue time was 1889.6 ms, with silent final playback. This is a server
startup observation, not browser or acoustic latency. No rollback or Pod/Ollama
restart occurred. Silero, disabled conversion, both 16384 model contexts, Max
fixture and private story receipt were preserved. Remote receipts are under
`/workspace/mishka-artifacts/product-integrity-20261118-v14-product-controls`;
verified local copies are retained in the corresponding PM recovery directory.
Chief owns the bounded native follow-up. Explicit screen Start/End and panel
route changes remain local work and are excluded from this Git-built package.

Preceding V13 helper23542 ran once, replacing the application only. The143578-byte
archive SHA-256 is45e5808c68e415f093bccfd02935646e5f1ffb0ca75a8089c09196d54655a338;
all30 baseline files, including the three newly tracked maintenance paths,
were checked before launch. New E2E82337612-82c0-4576-abb3-d43413dadfc7
passed8/8: STT361.4ms, LLM874.2ms, Mini1036.19ms, Silero98.38ms and
bounded queue2064.37ms. Final playback is silent; these are server startup
timings. No rollback, Pod/Ollama restart, Max or user-asset change occurred.
Remote receipts: /workspace/mishka-artifacts/product-integrity-20261118-v13-maintenance;
local copies are in the corresponding PM recovery directory/remote-receipts.
Pagination, auth/hook and new nonvoice controls were not included in V13.

One subsequent V13 native youngest-profile text turn completed through actual
LLM/Mini/Silero with 38 PCM frames and 38 browser Source.start calls, without
fallback. The QA harness observed approximately 3 seconds: 2982 ms between Node
request/PCM observer callbacks. The separate Source.start timestamp was recorded
in the page; observer latency was not isolated or calibrated. These are not
precise browser, speech-input or acoustic latency measurements. It returned to the same dialogue; 7 then returned
to root and hangup cleared the session. Two separate source-built reconstructed
generator/Mini calls also allowed a natural eight-word answer. These are new
bounded positives, not the lost V12 candidate, STT, human microphone or hearing
acceptance. Older quiz short-7 scoring was not reached in two actual questions;
no random retry loop or fabricated game state was used.

Historical V11 native model quality was on HOLD: both actual youngest-profile generator
probes returned formally valid eight-word JSON with unnatural glued words.
The mandatory semantic call allowed malformed spelling at confidence1.0.
Two rubric-only candidates still allowed one observed adjective/noun glue;
neither was deployed. The reviewed source correction instead requires a closed
quality-first verdict in the same Mini request: literal speech must be valid
AND the existing safety decision must allow it at the unchanged threshold.
No dictionary, candidate repair, extra inference or relaxed limit was added.
All three source-built prototype requests were byte-identical to the actual
tested requests: two adjective/noun corruptions blocked, the newly generated
natural eight-word persona answer allowed. Eight other source-built controls
also passed actual qwen3:14b inference: two preposition/pronoun corruptions
blocked; persona, hyphen/name, word-game, final-turn, normal solid spelling
and inflection controls allowed. Those eight calls took315.5–519.8ms each.
These bounded synthetic model checks and298 local affected tests passed
independent review. The exact209c3f8 source then passed a standalone real
public-WAV eight-stage probe, with one generation and one semantic request,
quality=valid/allow1.0/clean terminal, in6.613s in a separate cold process.
The V12 launcher startup also passed all8 stages on the actual app. Youngest
natural conversational quality and linked browser acceptance remain pending;
these bounded checks do not establish universal language quality.

The first V12 native follow-up still has two open failures. Early known-fake
microphone input produced an owned catalogue clarification instead of selecting
Aibolit; unlike V11, its completion no longer reported transition_rejected.
The same-browser first-frame→clarification interval was4.326s, not a
cross-clock STT measurement. A bounded known synthetic PCM capture is retained
privately for causal ASR investigation; no human microphone recording was made.
A fresh young-profile ordinary persona question also fell back with
provider_or_validation_failure, correlated to mini_llm_review. The lost raw
candidate and Mini response do not establish whether this was a false refusal.
The controlled semantic examples and startup pass do not close this live case.

One bounded causal replay of the retained synthetic capture found incorrect
Whisper text both with and without the VAD window, while the original fixture
decoded as the requested title. Three isolated local browser captures then
localized waveform alteration before the JS resampler: input correlation was
0.53942 with AEC/NS/AGC, 0.87159 with AEC alone, and 0.999924 with all three
disabled. The actual resampler preserved the sampled input exactly. This is
fake-device capture qualification, not evidence to disable echo cancellation
for real microphones; the hosted capture also had menu playback, unlike the
silent local comparison. Early production-route selection remains on HOLD.

Guarded V12 helper `22771` ran once, gracefully replacing PID22134 and six
reviewed runtime files. Its111,891-byte archive SHA-256 is
`95303178b3cdff27212379a44207f273f768cb48f61744ae4b18acffbe8a5b25`.
The strict preflight required the exact ready V11 baseline, full27 hashes,
listener ownership, no browser client, handset down and all18 lights dark.
New startup correlation `a901903c-3062-4ed8-9274-34bcf7e8d8bb` passed all8
actual stages: STT344.18ms, LLM428.91ms, semantic852.06ms, Silero55.29ms and
bounded queue1356.11ms. The same monotonic trace measured LLM request to
first PCM1354.65ms and ingress to turn completion1762.85ms. Final startup
playback is silent; these measurements are not browser/acoustic latency. Fresh independent readback confirmed
READY, instance/revision, full27, all three served client hashes, Silero and
disabled voice conversion. No rollback, Pod/Ollama/Max changes or user-asset
writes occurred. Receipts:
`/workspace/mishka-artifacts/product-integrity-20261118-v12-integrated/`.
Chief received the clean serialized browser/provider slot for the next native
pass; no other role may launch actual inference concurrently.

Guarded V11 helper `22129` ran once, replacing only the 15 reviewed runtime
files. Source combines the constrained JSON response, sentence-aware grammar,
hold-seven/UI, catalogue hot-path and weather-scope corrections. Archive:
260,349 bytes, SHA-256
`a21b0a6daeabf0604da7b897f042a47d171337fad3890435bdf6037c3b4fc8af`.
Receipt directory: `/workspace/mishka-artifacts/product-integrity-20261118-v11-sentence/`.
The V11 E2E correlation was `b62ccf09-b758-4263-8395-bec3ebe4b384`.
Startup measured LLM 408.26 ms, validation 672.31 ms and Silero 104.48 ms;
LLM request to first PCM was 1,204 ms and ingress to completed probe 1,614 ms.
The final playback is deliberately silent in the startup probe, so these are
server measurements, not browser/acoustic latency. Contexts remain 16384,
Silero is active and voice conversion disabled. Pod/Ollama, credentials,
private state, original story WAVs and the story receipt were preserved.
No rollback occurred in V11.

Prior failed deployments are retained as historical evidence. V8 candidate
`20640` and V9 candidate `21166` did not pass their old 90-second rollout
gates. Their candidate readiness bodies were not retained before rollback;
their exact failures are unknown. Old-source rollback failures with
`unknown model tag` do not establish either candidate's cause. V10 helper
`21647` ran once with the same V9 source, a 360-second monotonic deadline,
strict readiness and a private append-only response timeline. Candidate
`21653` completed with HTTP503/DEGRADED, correlation
`75ad7bb4-e540-450b-b1d4-7220089331b2`: PCM/VAD/STT passed, JSON completed with
a clean terminal, then `age_word_budget_exceeded` blocked speech before
semantic validation/TTS/queue/playback. Prerequisites and listener ownership
passed. Full candidate stage/body history was captured before rollback under
`/workspace/mishka-artifacts/product-integrity-20261118-v10-diagnostic/`;
raw model text was not captured. A reproduced source mismatch allowed three
short sentences in the older schema, while the bridge allowed at most two.
The reviewed correction preserves the global 8/18/30-word limits and younger
pattern, constraining older responses to one or two sentences before inference.
Its standalone actual public-WAV probe passed all eight stages in 6.368 s,
with one generator and one real semantic request, before the successful V11
rollout. It is a new synthetic response, not the lost candidate21653 output.
V10's rollback PID `21747` remained degraded until V11 replaced it.

## Stage 1 product integrity — 2026-11-18

V11 browser controls, source `db0a744` / PID22134: a recognized early hold
restored the same young quiz question, lights and owned prompt. Button7 was
enabled even though that question had no answer7. READY remained latched for
another3.9seconds; a genuine owner release exited. A real mixed-pointer order
(touch owner down, foreign mouse down/up, then owner touch up) preserved READY
until the owner's release. Hold→hangup→late release/old deadline left all18
channels dark, no held keys, no hold state and no pending PCM. A two-touch
helper did not deliver its foreign release before the owner's and remains
NOT_PROVEN; older-quiz short7 scoring still needs the separate native case.
Aibolit and Squirrel playback each received successful immediate Source.stop
on a running AudioContext; these are cancellation receipts, not natural-end
proof. Human microphone/hearing and physical HID/optics remain unverified.

The same pass found held3D caps occluded by their base; the one-float local
pressed-travel correction is reviewed with real local WebGL before/after and
all-seven geometry/reduced-motion checks, and is now deployed in V12. The native
held-seven screenshot confirms visible caps, digits and emblems; physical
rendering remains unverified. Early
catalogue speech did not select a story within20seconds despite one actual
STT completion; its cause is under investigation. A47.6second browser/server
comparison was withdrawn: a paired receipt measured server clock ahead by
45.107–46.432seconds. Same-server STT completed in141.6ms. Full local actual
WS ingress with five real WAVs and fake STT took0.291s for136frames, with no
WAV reads or get_verified calls. That local result neither proves real ASR
selection nor establishes a remaining hashing/backlog defect.


The reviewed V12 catalogue correction passed26 dedicated and92 existing
local checks through actual Application→Graph. Verified concern, urgent and
explicit-help routes now preempt both catalogues; the exact matching activity
remains the only ordinary catalogue launch target. Rejection restores the
published snapshot from the authoritative rolled-back owner. Unverified/forged
input, late classification after hangup and late menu audio cannot grant a
transition or create owner/audio/light/caller effects. The native early-story
failure already had a transition_rejected receipt, but its exact recognized
input is unknown; this related source defect does not prove that browser case
fixed.

The V12 warmup audio correction uses an immutable task-local stack of
engine/sink bindings instead of globally replacing engine.playback. The entire
probe, including its cancellation cleanup, stays silent while preadmitted user
tasks retain their actual playback controller. Four reproduced run_now/recover
races, cleanup failure, timeout/late children, nested engines and the next user
turn are covered by88 local checks plus3 mock-wire terminal controls. This is
audio isolation only. Source45bb465 separately adds task-owned manual-maintenance
admission and tracks cancelled native worker occupancy until actual completion.
The busy409 guards, cancellation/recovery ownership and unchanged idle policy
passed134 affected checks,51 follow-up checks and independent source review.
The reviewer additionally exercised actual local ASGI admission during native
Silero completion and a private profile entry. That source is deployed in V13;
V12 lacked this guard. Remote model termination after a
transport cancellation and actual physical/audio acceptance remain unproven.

V12's older-profile browser pass accepted a synthetic microphone game request,
an owned offer and a separate spoken consent, then entered the quiz through
real STT/LLM/validation/Silero without a typed launch. Free-light, repeat-light
and odd-one-out linked entries returned to the same dialogue. Short-seven
scoring is NOT_PROVEN: the QA question map became stale. The final word-game
response reported completion while the immediate browser state was still the
game. The server had already returned to dialogue: QA's next7 produced
activity_back, and the queued bear frame arrived before the QA-induced root
frame. A pinned-source local Application→Stand→Hub replay confirmed the
committed state and projection were bear while delivery waited. QA must await
that returned owner before the next input. The 4229 ms Node response→WS observer
interval does not measure UI rendering: page and Node callbacks observed root
through different paths, with no page-onmessage/refresh provenance. Actual
delivery/render latency remains unmeasured, and no product state patch was
justified by this evidence.

The next local inventory found real nonvoice gaps: Stand disables the next-page
button offered by story and DAY descriptors, and DAY omits the supported
card_doctors action title. Source389751b fixes both via the current committed
descriptor and a fixed public action label: 58 local checks, independent review,
16 independent boundary controls and two targeted tests passed. V14 includes it
and the hint-only dialogue exit/microphone guidance from ed27eba.

V14 also includes fresh verified profile replacement, with old private context
and jobs fenced before cleanup. Real lift during cleanup preserves pending
authorization and immediately reports the real UP state without guest welcome.
Final focused verification passed 96 cases; independent review reproduced both
the original lift race and postgrant source-revocation counterexamples as GREEN.
The unchanged external Max loopback HTTPS/temporary SQLite suite passed 32 cases
and two subtests, with one remaining failed schedule assertion also reproduced
on immutable pre-auth ed27eba. It is not a passing suite: the older broad
where_now/what_next-to-card_doctors remap is a separate product logic defect.
Do not weaken its no-schedule assertion or invent missing time/room data.
All four auth/hook combinations and visual on-hook use still await the explicit
panel-route implementation and hosted acceptance. Max data remain provisional;
the external Max source and clinical database were not changed.

A separate controlled-delay counterexample on immutable ed27eba established
that the real browser post FIFO can hold a short-7 release behind a delayed
press response. A 100 ms local tap then reached the server as a 5.1 s hold and
exited the quiz instead of answering. This is a local transport defect, not a
measured hosted delay or physical-board result. A scoped gesture/delivery fix
is queued after the active panel writers; existing hold-feedback passes do not
close this boundary. The archived extra menu button remains excluded.


The historical successful V7 application-only rollout retained the existing environment, model assets,
private state, original story WAV files, Max fixture and story-backend receipt.
The pod and loopback Ollama server were not restarted. Both model context
settings remain `16384`; the current voice is Silero without voice conversion.
All 23 runtime files matched the final reviewed manifest. Authenticated HTTP
served `console.js` SHA-256
`2fce8160a985afb57e50095be6c3d788556064bf65b20fcbeae04a8dacc284d5`.
Readiness returned `READY`; the initial handset was down with no browser owner.
Prior files, the manifest and rollout/readiness receipts are retained under
`/workspace/mishka-artifacts/product-integrity-20261118-v7/`. The 52,240-byte
archive has SHA-256 `b865d16a18b8f1a69d100a9eae4b180ef40a229456e2018f66d64e69b23ff54b`.
After Chief's explicit clean browser release, guarded helper `20027` ran once,
verified all 23 baseline hashes and no browser owner/pressed keys, and replaced
only three prompt files from committed Git blobs. PID `19481` shut down
gracefully; PID `20029` reached `READY`, instance `e17f1515cd2bd668`, initial
revision 1, handset down and all 18 channels dark. Final 23 runtime hashes,
served client, unchanged model contexts, story receipt and Silero/disabled
conversion were verified. No rollback was needed. In-progress local hold-seven
and weather-scope edits were excluded from this archive.

The preceding V6 archive was 261,084 bytes,
retained under `/workspace/mishka-artifacts/product-integrity-20261118-v6/`, and
has SHA-256 `bf2f6950448e18eebad6b844cdcb49d057046731f3ce131931f3fd1bae8159a1`.
The guarded helper ran once, checked all 23 preceding and candidate hashes,
and replaced 19 runtime files from the exact committed source. PID `18670`
shut down gracefully; PID `19481` reached `READY` with instance
`5f755e980e04bc02`, initial snapshot revision 1, no browser owner or pressed
keys, Silero and disabled voice conversion. No rollback was required.

The preceding V5 two-file
audio/diagnostics follow-up builds on v4's three-file menu/identity correction;
both retain complete 16-file manifests and backups. V5's helper ran once,
PID `18180` shut down gracefully, and PID `18670` reached `READY`. Initial
capture was down without a browser owner; simulator instance
`8bc8e7a8c4a77577` and revision 2 were read back. Earlier v1/v2 archives and
v5's local-only db5 draft were superseded during review and never executed.

Implemented and independently reviewed:

- Actual allowlisted model game proposals use an owned, spoken confirmation.
  A game called from conversation preserves its caller and returns there;
  root-launched games return to the menu. Early/stale consent cannot start a
  game. Control proposals do not consume a spoken dialogue turn or reset its
  existing budget. The saved conversation's technical clock pauses during the
  nested game; its previously spent time, turns and history are retained.
  Engagement inactivity and Max authorization expiry remain independent. A
  150-second game test verified this pause without resetting the caller budget.
  The word game uses the actual bounded, age-scoped S2S path.
- Quiz answer 7 scores on short release; hold it five seconds and release to
  exit, including during instructions. Other games keep their back controls.
  Word-game instructions explain the finite party and age-specific rules.
  Bounded reminders preserve the 120-second engagement rule and separate Max
  expiry. System speech does not renew user activity.
- Admitted game speech cannot be invalidated by a reminder while mandatory
  safety classification is pending. Cancellation/failure restores the original
  deadline; old checks cannot hush a new run. Sequence and hold mechanics keep
  their own wakeups, including a hold deadline after an earlier pending hint.
- OFF digits/emblems are dim gray, with independent ON rendering using the
  existing 18 channels. Atomic snapshot capture, ordered revisions and a service
  instance identifier prevent delayed HTTP from undoing newer WS lighting and
  preserve explicit reload after a service restart.
- Root-3 ordinary speech now reaches bounded dialogue; day-menu speech reaches
  the existing typed capability checks, with truthful provider unavailability.
  Back cancels its queued audio. Narrow verified wellbeing reassurances retain
  semantic escalation. Personal-story references cannot steal button 7 from a
  later help question or root menu; genuine story pause/back/resume and public
  checkpoints are retained.
- Current descriptor selections, Next and Back act on the same fresh press
  during menu narration/repeat; held keys and stale mappings remain fenced.
  Existing day/theme voice requests can dismiss the repeat question without
  losing their action. Complete positive consent compounds are accepted only
  for the current ready offer; explicit adult-help requests retain priority.
- Eleven internal follow-up builders share original-event identity and their
  own source ordering. Missing original correlation and a switch between
  independently numbered input sources cannot turn a new reply into a false
  duplicate/stale event. Actual original replays remain inert.
- Actual game VAD/STT also owns its pending utterance: a reminder cannot cancel
  speech admitted before its deadline. Silence/pre-roll does not defer hints;
  confirmed collection has a fixed wall cap derived from the existing speech
  and recognition limits. Disconnect, reset, terminal cleanup, detached failure
  and cancellation release the exact owned wait without resurrecting a timer.
  A late ordinary transcript cannot answer the next round after typed input;
  an urgent disclosure from the same valid capture retains priority. Five
  additional fixed parser reason codes survive diagnostics; arbitrary detail
  remains redacted. No parser, safety or age limit was relaxed.
- Conversation can open the actual five-game or verified five-recording
  catalogue, or request a named recording. Fresh named or advertised-slot
  voice/button choices can interrupt catalogue narration. A separate entry
  generation fences old VAD/STT and delayed safety results after leave/reentry;
  numeric choices also retain their admitted page. Story playback, pause,
  completion and cancellation preserve a valid conversation caller.
- An uncertain or unavailable catalogue choice receives one short application
  prompt rather than silence or a guessed asset. Its exact static text, lease,
  entry and descriptor are checked before rendering and after queued PCM drain.
  Fresh selection interrupts it; complete refusal returns to the caller.
  Complete compound repeat answers retain their existing yes/no meaning.
- Accepted explicit factual questions use the typed executor before ordinary
  generation. Personal appointment questions, general explanations, character
  questions, fiction and refusal have distinct tested routes. The current card
  supplies only source-qualified doctor names/date through personalization,
  without a schedule grant or an invented next visit, time or room. These names
  enter neither generative nor semantic model requests; ordinary model output
  still requires semantic validation. Age instructions now match the unchanged
  8/18/30-word limits and final word-game instructions take precedence.

Local checks and their limits:

- Focused source, ownership, cancellation, safety and caller tests passed in
  several overlapping batches; counts are not additive. The final timer delta
  passed 26 cases. The story/help batch passed 65 cases, with 27 related public
  checkpoint/expiry cases. Nine affected Node suites, canonical verification,
  syntax checks and final diff checks passed.
- The v4 three-file follow-up passed 215 linked cases on its exact frozen
  sources, including actual Stand ingress with empty correlation, four turns
  and natural completion, source-order changes, repeated offers/fallbacks,
  original replay, menu phases, both help-priority contexts, personal stories,
  onboarding and PCM ownership. Canonical verification, syntax and diff checks
  passed; Chief independently reviewed the final hashes and replayed the REDs.
- Chief independently reproduced the defects and reviewed the final hashes.
  The final crossing-deadline case passed without changing original wait
  deadlines. An actual local Chromium matrix passed 64 indicator combinations
  across flat/WebGL and desktop/390px, including rapid update and reset. This
  establishes local rendering, not physical-board brightness.
- An expanded legacy timer run initially returned **34 passed / 9 failed**.
  All failed IDs were classified: four shared a missing legacy hold timer,
  four shared an obsolete extra menu skip, and one used a separate obsolete
  full-entry press sequence. Representative failures from all three causes
  were reproduced on the preceding source. A test-only correction now uses an
  explicit real legacy engine state and the actual current public chooser.
  The two affected modules passed **17 cases**, reaching the original strict
  HID, owned lease, timing, scoring, demonstration and hangup checks. PM reviewed
  their diff independently; no runtime file or public game route was changed.
- V5's final audio source passed 30 affected cases, including all 25 new
  VAD/STT boundaries and five cleanup/idle controls. An earlier 63-case batch
  passed before the final three-line token-membership correction; it is not
  relabelled as a final-source run. The Trust reviewer independently replayed
  13 narrow controls and the cleanup REDs on the frozen final hash; Chief gave
  the integrated rollout approval. The separate public
  diagnostic projection module passed 25 cases and independent privacy review.

V6 local acceptance includes source-bound batches rather than one additive total:
83 new gateway cases before the isolated later corrections; 65 factual/card
cases and 96 private/public-menu/PCM cases after their respective corrections;
68 factual-family cases; and 12 final clarification/repeat/cancellation cases.
The earlier related run had 285 passes and two failures, closed by 68 affected
cases. The clarification run had 152 passes and one test-barrier timeout; its
corrected barrier passed in isolation and the final 12-case run. Neither old
mixed-result run is reported as a wholly green final-source suite. Canonical
verification, syntax and diff checks passed; the nine affected Node suites
passed on the unchanged final client hash. Independent reviewers replayed the
authority, catalogue ABA, private-model, static-receipt and queued-PCM defects.
The actual unmodified `PhoneUbuntuClient.poll` → `MaxJsonBoundary.step` path
also passed a controlled-clock test with a synthetic card: system prompts did
not renew the 120-second lease, exact expiry cleared facts, and old queued PCM
and callbacks stayed inert. This is real client logic, not real RFID or a
clinical API/data pass.

Current hosted acceptance is owned by Chief against V7 PID `20029`: actual model
proposal/confirmation/game/caller, scoped word turns and natural PCM drain,
all five games/recordings, early browser capture, short/held exit, clarification,
interruptions, sourced information, repaired root/day/help paths and a clean
next visit. Readiness, mocks and local rendering do not establish those results.
Physical microphone/hearing, physical RFID/board, positive external schedule or
weather data and clinical suitability remain outside the software proof.

The first V6 browser pass found two open model-output failures. In the real
guest age-two flow, “Мишка, где ты живёшь?” reached the actual model but exceeded
the correctly applied eight-word limit: `stream_parser/age_word_budget_exceeded`.
Actual Chromium fake `getUserMedia` then delivered two voiced clips through
capture and real STT, but the initial quiz request produced
`stream_parser/game_offer_mixed_or_out_of_context`. The later standalone
consent opened the general games catalogue. This is **not** a passed
request → owned offer → spoken consent chain. Capture remained healthy;
dialogue recovered through the bounded retry without a UI error. Read-only
effective-prompt inspection confirmed the age/profile and eight-word boundary,
and found competing speech-only and catalogue-action format instructions.
The prompt correction is independently cleared and deployed in V7; actual-model
replay remains open. The strict parser, age limit and safety checks are retained. Evidence:
`runtime/local-integration-20261117/runpod-stage1-v6-games-capture-19481.json`.
That pass ended handset down, all 18 channels dark, no pressed keys and
280 sources / 280 ended, with no browser errors; explicit cancellations mean
these are not 280 natural completions. Fake browser capture is software
evidence, not a human microphone or hearing check.

The V7 correction removes competing output instructions: bear dialogue uses
either speech with its existing permitted accompaniment or one catalogue
control action alone. Other modes keep catalogue actions forbidden. The final
trusted format follows profile/history data; ages one to three receive a
single-thought/single-sentence target of three to six words, with the unchanged
eight-word hard limit and word-game final-turn priority. The exact commit
passed 66 focused cases plus independent source/mock boundary replay. Actual
model compliance remains a separate hosted gate. The first V7 actual-model
replays remained negative: the age-two persona question again produced
`age_word_budget_exceeded`, and an actual fake-microphone quiz request reached
Whisper then produced `game_offer_mixed_or_out_of_context`, with no pending
offer. No second consent clip was delivered after that failed offer wait.
The prompt-only change therefore has not closed either behavioral defect.
A subsequent immutable-source wire audit confirmed the complete final system
instructions reach Ollama unchanged. One serialized actual-model synthetic
probe returned both a five-word speech block and a quiz offer, confirming the
mixed-output class without reproducing a captured private failed turn. There
were no literal escaped quotes or escaped newlines in that response; the early
escaping hypothesis was withdrawn. A bounded structured-output capability
check is the next step; parser, age and semantic limits stay.
V6's later named-word-game
and named-Lukomorye requests repeated the mixed-action failure. Early browser
story selection sent voiced frames during narration but did not establish
selection/STT completion; that boundary remains under separate investigation.
V7's app restart is not claimed to fix it. Aibolit's typed selection, pause at
3.957 seconds, actual source offset on resume and return to the same caller
were observed on V6. On V7 all five recordings were played and matched their
verified assets: four hashes came from actual browser response bodies; the
large Aibolit body was evicted, so its hash came from a separate authenticated
same-app readback alongside observed playback duration. Lukomorye naturally
finished after 105.069 seconds and returned to its catalogue. The final visit
ended down, with no pressed keys and all 18 channels dark. Two cancelled long
sources lacked observed ended callbacks (234 created, 232 observed ended,
229 natural); stop/callback instrumentation remains a separate acceptance
check, rather than proof of either complete drain or an orphan defect.

Independent frozen-source ingress replay found a concrete catalogue bottleneck:
`_catalogue_voice_scope` rebuilt the story catalogue and reread/hashed all five
141,376,322-byte WAV assets on every capture guard evaluation. Ten 341-sample
frames (213 ms of audio) took 20.45 seconds with 205 integrity checks; twenty
frames took 45.29 seconds with 405 checks. An already-verified RAM isolation
control sustained the same local stream across three owners, distinguishing
this cost from a demonstrated permanent capture-owner lockout. That control
is not a proposed permanent cache or an actual native-provider pass. Reusing
the current owned committed descriptor in the hot scope must preserve entry,
epoch, page/receipt and selected-file integrity verification before playback.
Correction `3c03497` uses the current committed owned descriptor for the hot
scope; selected-file integrity checks before playback remain. Independent
causal replay with all five real files reduced twenty frames to 0.03 seconds
and 100 frames to 0.17 seconds, with zero repeated catalogue verification. A
full 119-frame local route reached the selected Aibolit in 1.897 seconds,
including 1.541 seconds for the retained selected-file checks. Invalidation,
rights revocation and changed/missing assets were checked. This is local
software evidence with a fake STT boundary; actual early browser voice and
Whisper acceptance after deployment remain open.

Roman's subsequent immersion addition preserves the active work. Local
implementation `990d394` and its independent core/UI review are complete; it
is included in V11; actual browser acceptance is pending. Quiz seven remains a short-release answer. A recognized
hold shows a calm cumulative binary wave, speaks readiness once, and stays
ready until an owned non-cancelled release. Early release restores the current
question without scoring; pointer/key ownership, cancel/blur, late audio and
stale timers are guarded. Independent replay covered six core causal groups
and actual 2D/3D/workbench JavaScript handlers, including focus moving into an
input before key release. The final local focused run passed 64 Python cases
and six Node suites; these are not rendered-browser, hearing or physical-board
acceptance. The following continuing criteria apply: the hold must retain a
short press as an answer where applicable,
show calm progress using the actual lighting protocol, latch readiness until
release, and restore game state after an early release or interruption.
Instructions must also explain the gesture without relying on sight. Timing
and effects require investigation; the proposed wave is not a fixed design.
The three wire fields are element ID, on/off state and transition duration,
not RGB or arbitrary brightness. Physical-board behavior remains untested.
Max's supplied data are test/provisional values, not real clinical facts;
missing schedule/room/doctor fields are not defects to fill with invented
content. Roman also confirms the weather key and coordinates are absent.
Configured-source, missing-data and fault behavior can be accepted separately
from a real API call. A future city name must come from a supported source,
without an unconditional Saint Petersburg default or an unnecessary manual
city question; an unavailable name must remain an explicit limitation.

On the preceding PID `18180`, a continuous synthetic-WAV request → actual Whisper → native
quiz proposal → typed “Да, давай” → real quiz → button 7 → preserved caller
passed. This does not establish spoken consent: two affirmation WAVs produced
a retry or ordinary dialogue rather than starting the game, and their actual
recognition had not been established. A separate word WAV crossed the reminder
boundary during VAD/STT and returned `stale_session_generation`; the actual
local VAD/STT barrier reproduced the cause and V5 fixes it. A later synthetic
utterance reaching help is not classified as a false safety reaction without
recognition evidence. A fresh typed party then completed four real-model word
turns, with one validation fallback/retry between successful turns, returned to
the caller and completed another ordinary answer. All 43 sources in the final
word response ended naturally, with no audio stop after its first PCM.
`runpod-stage1-v4-party-18180.json` records that source-bound result; seamless
no-failure or fully spoken acceptance is not claimed. Server result timestamps
describe completion, and server/browser clocks differed by about 44.8 seconds;
after alignment, the suspected foreign inputs matched the test's own requests.

On PID `18670`, isolated actual Whisper plus Silero VAD replay found all three
old eSpeak fixtures unsuitable: both consent fixtures produced unexpected words
or non-confirmation classification; the word fixture was unknown below the
unchanged 0.45 confidence threshold. Safe metadata is retained in
`runtime/local-integration-20261117/stt-provider-diagnostic-result.json` without
transcripts. This is an actual-provider fixture diagnosis, not a successful
live-session voice exchange or permission to relax recognition/safety guards.

The same runtime then passed the actual answer-7 quiz boundary. Short press
returned `game_answer_pending_release`; release accepted the answer and advanced the same
quiz. On a later question where 7 was again an actual answer, a fresh press
followed by a 5.25-second hold and release returned
`game_exited_by_long_button`, root menu, no active audio and no pressed keys.
The current 7 was indicated by its color channel, so observing only its digit
channel would have misidentified the available answer. Real keyboard edges and
`runpod-stage1-v5-older-18670.json` record the path; no deck/state injection or
stale initial challenge ID was used. Natural human hearing remains unobserved.

Replacement fixtures were generated once with the existing cached Silero
voice and qualified through actual Whisper and Silero VAD. A continuous visit
then passed request WAV → Whisper → actual model quiz offer → separate consent
WAV → Whisper confirmation → quiz → button exit → the same conversation caller.
No typed input occurred between request and consent. The artifact is
`runpod-stage1-v5-qualified-voice-18670.json`; the local fixture directory keeps
the matching hashes. This establishes synthetic-WAV software voice consent,
not browser `getUserMedia`, a human microphone or human hearing. The last visit
closed with handset down, all 18 channels dark, no pressed keys, no browser
errors and no remaining browser owner.

A word WAV admitted 17.615 seconds after the prompt ended reached actual
recognition and the model, but the reply exceeded its age word budget and
produced an owned retry. This is not successful word progress or a hosted
causal proof of crossing the full reminder deadline. The same precise parser
reason was reproduced after a typed word: the base prompt allowed 45 words
while the young-game parser allowed eight. V6 aligns the instructions with the
existing limits, without raising them; further V6 model-compliance failures
are tracked above. A separate live wellbeing compound reproduced a missed
complete reassurance. Its narrow correction is deployed in V6 and awaits the
linked replay; semantic escalation remains mandatory.

The V5 browser pass also accepted a typed date request after the full day-menu narration,
cancelled its playing typed answer with Back, recovered from a controlled
browser audio-source failure to a useful real-model reply, and inspected the
390px layout. These are browser/software observations; they do not establish
human hearing or a spoken date query.

The first hosted Stage 1 pass on PID `17636` established ordinary native
dialogue/Silero drain, root-3 useful speech, verified pain/fear reassurance,
typed game proposal → button confirmation → manual exit → caller → next answer,
and a real personal story → pause → help → button-7 confirmation → owned
PHRASE_091 with natural drain. That story contained 66,237 ms of PCM; its pause
checkpoint was 45,845 ms. Hangup left all 18 channels dark, no pressed keys,
capture off and no browser errors. Evidence is retained in ignored
`runtime/local-integration-20261117/runpod-stage1-story-help-17636.json` and the
older/recovery/young Stage 1 artifacts for the same PID.

That first pass also found real defects: the repeat-menu gate swallowed a valid date
request and the first Back press; compound consent such as “Да, давай” cleared
an offer; absent original correlation and inherited external sequence numbers
made later internal replies duplicate/stale. Actual Stand parsing uses an empty
correlation string. The local transport regression reproduced the second-word
failure on the exact preceding source. These findings motivated the v4/v5
follow-ups; the later source-bound replays above record their outcomes.
Synthetic-WAV request → offer and the separate typed full game chain are not
combined into one continuous voice-path pass. A 5.2-second gesture on a quiz
question without answer 7 established ordinary button exit, not the long-hold
threshold; the later PID `18670` result above supplies that separate proof.

The conversational-access addition remains active (FR-030–032 / T037–040).
Generic game/story choice, named verified assets during narration, contextual
story return, direct sourced day questions and warmer safe persona dialogue
are deployed in V6; integrated acceptance is still open. They were not features
of the preceding PID `18670`. All five
current fixed WAVs exist, and `fixed_neznayka_v1` means Telephone rather than
Neznayka. Max's contract provides a card's assigned doctor names/codes and
visit date, not timed visits, order or rooms. A bounded private DTO and a
source-qualified typed card answer are integrated with the existing
personalization grant, without granting schedule access or modifying Max.
The source date must not be inferred from the current clock, and a card list
must not be described as the next appointment. Synthetic fixture integration
does not prove real clinical assignment.

The validation correction was committed as `6c09b83`: harmless device
nouns and first-person Mishka play reach the mandatory semantic gate, while
real identifiers, unsafe clauses and the no-semantic trusted path retain
their protections. The final validation hash `82cb42f4…` passed 99 focused and
249 affected cases, plus independent actual engine-to-mock-TTS replay of the
privacy regressions and benign controls. These overlapping counts are not
additive. Actual semantic-model behavior and persona wording are not yet
accepted on this source. It is deployed in V6, and was absent from V5.

The weather correction binds existing validated weather settings
without overriding voice/private settings and checks actual `fact.obs_time`
rather than the API server's `now_dt`. The two-hour observation age and
five-minute future skew, plus an outdoor temperature plausibility range of
−100…+70 °C, are local policy, not a Yandex SLA or documented provider limits.
Out-of-range values are rejected rather than clamped. Adapter/config/hardening
checks passed 51 cases on the final source; 30 launcher/menu cases passed on
their unchanged configuration code. They use mocked HTTP/factory boundaries,
without a paid provider call. Independent cancellation/config/freshness review
and actual HTTP-mock → service → renderer range probes passed. These files
are deployed in V6. Authenticated V6 readiness reports provider `yandex`, with
key, latitude and longitude presence flags all false. The preceding V5
configuration was demo; no actual Yandex access has been verified. The
[official REST contract](https://yandex.ru/dev/weather/doc/ru/concepts/forecast-rest)
documents the measurement timestamp and existing endpoint; its
[release notes](https://yandex.ru/dev/weather/doc/ru/concepts/releases) list v2
as current. Actual account entitlement and successful source access have not
been checked; no purchase or subscription was created.

The subsequent read-only city audit found no unconditional Saint Petersburg
default. The current adapter uses only configured latitude/longitude and does
not retain a city name. The documented REST `info` fields do not promise a
city; a timezone or URL is not a city-name source. Eight selected existing
missing-configuration/typed-information tests and four additional in-memory
day/conversation cases passed: missing configuration made no fetch, a
configured fetch failure produced truthful `PHRASE_100`, and contextual return
was preserved. These are local provider-stub checks, not HTTP/API acceptance.
The investigation reproduced an explicit named-city request reducing to
`weather`, losing its qualifier while the provider used its fixed configured
point. The scoped executor correction retains that request distinction and
states an unavailable location honestly; ordinary
weather requests must not gain a mandatory city question. No geocoder,
invented city or claim that configured coordinates equal the phone's location
has been added.
The related temporal probe also reproduced tomorrow, yesterday and weekend
queries receiving the identical current-observation answer through the actual
local typed route. This is provider-stub evidence, not real API acceptance.
The local request-scope correction carries a closed, private, request-local
scope only after admitted verified input, or from the currently accepted DAY
button. Named/unknown places, source-city questions and unsupported periods
receive honest limitations before an unrelated fetch. Current/bare/today and
ordinary clothes questions keep the configured-point path. The intermediate module passed 115 cases, but independent review reproduced
unresolved place and time qualifiers still defaulting to current observations
(for example a bare city or «к вечеру»). That intermediate candidate remained on HOLD and was
excluded from the attempted hot-path/hold rollout. The final positive admission correction requires coverage of the entire
supported current request and passed independent review of 28 actual local
application routes. Commit `d48bc15` preserves that exact seven-file scope;
it is included in V11. The adjacent rain/snow/degrees aliases were corrected in commit `557c7d6`: final
208 local weather cases and 28 independent application-route cases passed.
These aliases are also included in V11; actual external API data remains unavailable. No new scope/location
field is added to events, history, model input, public state or logs. Existing
authorization and late-owner fences remain, including rechecking origin after
identity expiry awaits. Young DAY voice uses all current validated capabilities
without broadening current-page numeric buttons. A missing weather source is
explained in DAY narration. This correction is included in V11; real API and
city data remain unavailable.

## Runtime and server boundary

Use only the new separately authorized Community server. Never start or change
the old training pod `1ymrrmw7a2e8hh`. The current provisioned base image is
`runpod/pytorch@sha256:5f605ecfc24bd0a7743dcc3f61a9bcf71fc2083f0fd700b6eb67278b9be0e601`
(Ubuntu 22.04 / torch 2.6 base only). Its host reports CUDA 13.0 capability.
Provision a verified Python 3.12 interpreter, then `bootstrap.sh` creates an
isolated `/workspace/mishka-env` with torch 2.8.0 + torchaudio 2.8.0 cu128 and
the unchanged application `requirements.txt`. Do not use the base image's
Python/torch environment. The alternative official image
`runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404` still requires an adequate host
driver and verification; an image label alone is not runtime evidence.

Copy this checkout and the reviewed unchanged Max source to `/workspace`.
Keep the whole `/workspace` tree on the authorized persistent volume; do not
copy environments, model caches or private runtime data into Git.

```bash
MISHKA_PYTHON312=/path/to/python3.12 bash deployment/remote-stand/bootstrap.sh
```

The container must also provide OpenSSL, CA certificates, PortAudio and
libsndfile. Bootstrap does not mutate cloud resources, install an interpreter,
or install/start Ollama. Install a reviewed pinned Ollama binary separately,
record its version/hash, and bind it only to `127.0.0.1:11434`. Set
`OLLAMA_MODELS=/workspace/models/ollama`, pull `qwen3:14b`, and record
`ollama show qwen3:14b` plus its manifest/blob digests. The first application
warmup can fetch baseline Whisper and Silero model assets to `/workspace`;
record their resolved revisions/hashes after installation. Do not download
these heavyweight assets on the local workstation.

For the current server use the already installed loopback Ollama daemon; the
launcher never starts or stops it. `launch.sh` selects the pinned local Whisper
tree `/workspace/models/faster-whisper-large-v3-turbo` (override explicitly with
`MISHKA_WHISPER_MODEL`). Direct launcher calls accept `--whisper-model` for the
same purpose and validate model/config/tokenizer presence before startup.
Copy the verified Silero asset to
`/workspace/mishka-runtime/cache/silero/v5_5_ru.pt` before launch to reuse it.
The protected launch manifest records whether Whisper uses local assets and
the selected model directory; no local model paths enter public projections.

```bash
export MISHKA_EXTERNAL_ORIGIN=https://YOUR-EXACT-8000-PROXY-HOST
export MISHKA_MAX_SOURCE=/workspace/max-client-fixture
bash deployment/remote-stand/launch.sh
```

Port 8000 is a private application behind an external HTTPS proxy. The Max TLS
server and Ollama remain loopback-only; no notebook listener is needed. The
launcher does not trust forwarded headers. Its configured HTTPS origin makes
cookies Secure even when the HTTPS proxy uses HTTP upstream. Direct browser
HTTP to a remote host is not a supported login path.

## Login and local checks

Open `/console` (or `/simulator` when installed by the UI) in a browser and use
HTTP Basic username `mishka`. The password is the generated token in
`/workspace/mishka-runtime/stand-token`, an owner-only `0600` file. Transfer the
secret privately; never put it in a URL, command-line argument, screenshot,
report or log. An existing `STAND_ACCESS_TOKEN` environment value may seed a
new token file; conflicting values on restart fail closed. A successful Basic
request issues `stand_token` with `HttpOnly; SameSite=Strict; Path=/; Secure`
on HTTPS. Browser scripts use the cookie and never need the password in JS.

All HTTP and WebSocket routes are protected, including health, static assets,
OpenAPI and unknown URLs. Query authentication is rejected. Cookie-based
mutations and WS require the exact configured Origin. Other origins are not
implicitly allowed; repeat `--external-origin` only for intentionally allowed
browser origins. Basic API clients may omit Origin, but a supplied foreign
Origin is rejected on mutations/WS. Authentication does not bypass canonical
admission; `/ready` can honestly remain 503 in mock mode.

### Current recovery instance and protected files (2026-11-18)

The active instance is `fihihgl91rnwu4`, display name `mishka` (RTX 3090, reported compute rate
`$0.22/hour`, persistent `/workspace` 80 GB). The panel is
`https://fihihgl91rnwu4-8000.proxy.runpod.net/console`. Silero and voice
conversion `off` remain the baseline. The onboarding/welcome fixes were
deployed with backups in
`/workspace/mishka-artifacts/onboarding-repeat-20261117T224411Z`.
The subsequent browser-capture cancellation, dialogue/story guidance and
input-classifier prompt fixes were deployed with backups and exact hashes in
`/workspace/mishka-artifacts/capture-ui-safety-20261117T230642Z`
(application PID 6678 after a graceful restart). Application readiness and
exact served JavaScript hashes passed; subsequent live outcomes are recorded below.
This is not whole-product or physical-device acceptance.

On PID 6678, actual Chromium accepted a cold first game entry and returned to
root on the voice-exit text. Root button 2 had channel 13 on and `datafill=true`.
Two consecutive microphone turns completed through real STT, LLM, Silero and
Web Audio using synthetic Silero WAV input in one browser/WS session (184 PCM
frames total), with the microphone still enabled/accepting and no JS/HTTP
errors. Evidence: `runtime/local-integration-20261117/runpod-silero-microphone-acceptance.json`.
This verifies the software path; physical microphone capture and audibility
remain unverified.

The game audio failure was reproduced with the installed CUDA Silero model:
two of 154,200 samples exceeded the valid range (peak 1.0080756), causing the
entire utterance to be rejected. The engine now attenuates only finite
over-range buffers; normal waveforms and positional pre-roll remain unchanged,
and invalid/empty audio still fails validation. The same native phrase passed
after the change, alongside 24 focused tests and independent review. The exact
engine candidate was deployed to application PID 7614 with backups in
`/workspace/mishka-artifacts/silero-overload-20261117T2340Z`; readiness returned
`READY` / `warmup_complete`. Commit `2960010` and its focused regression are
pushed to `origin/codex/remote-device-simulator`; this is an intermediate fix,
not a release acceptance.

A second native failure was reproduced after 48→24 kHz resampling: one sample
of the complete game hint exceeded the range (peak 1.0010872). Conditional
attenuation of the assembled utterance now preserves all 338,820 samples and
seven cue offsets while producing valid PCM. This and the independently reviewed
public-story expiry handoff and catalogue-derived menu guidance were deployed
together in application PID 8291. Exact eight-file hashes, prior files and
deployment metadata are under
`/workspace/mishka-artifacts/public-story-ui-20261117T2347Z`. Readiness returned
`READY` / `warmup_complete`, and served console JavaScript matched the candidate.
Actual browser Max expiry FAILED: the public story stopped at identity expiry,
with `story_paused` / checkpoint pending and no browser checkpoint POST despite
a matching playback identity. The earlier playing lifecycle was rejected as
`stale_audio_lifecycle`; the exact ordering still needs investigation. Evidence:
`runtime/local-integration-20261117/runpod-public-story-expiry.json`.
Chief closed the failed test after handset-down cleanup. Work has since resumed:
an existing Sol writer is repairing the demonstrated browser stop/checkpoint and
current-lease lifecycle gaps; Chief owns independent review and coordinated live
acceptance. Coordinate before any restart or deployment. The Runpod pod and
providers remain running. Whole-product acceptance is incomplete.

The follow-up causal repair was accepted in application PID 9303. Current-lease
lifecycle no longer depends on the last effect list after a button release;
a stopped public story retains only an exact one-shot position receipt for
five seconds. The same deployment supplies contextual game guidance and a
fixed-field TTS failure event without utterance/error text. Four-file manifests,
backups and rollback metadata are in
`/workspace/mishka-artifacts/expiry-word-diagnostic-20261118`.
Readiness and exact served JavaScript passed. Actual Max expiry while playing
passed: one checkpoint transferred offset 91,221 ms, the anonymous generation
resumed at 91.221 seconds, and a late old completion was rejected. The observed
resume gap was 7.779 seconds because the public WAV was fetched/decoded again;
the public-buffer reuse fix is now deployed for acceptance. The paused idle
boundary also passed after 120.145 seconds, followed by a successful new game.
Evidence: `runtime/local-integration-20261117/runpod-expiry-repair-9303.json`.
Source checkpoint `d4bf136` is pushed to `codex/remote-device-simulator`; it
contains no raw operator recordings, weights, credentials or runtime state.

The next reviewed bundle is live in application PID 10134: a single public
story buffer reused only by a new owned positive-offset command, explicit
launcher binding of the verified story receipt, and filtering of standalone
decorative TTS chunks. A wholly nonverbal model reply still takes the honest
fallback. Two native probes after the word fix produced valid meaningful PCM
and no Silero failure. Exact three-file hashes and backups are under
`/workspace/mishka-artifacts/story-buffer-receipt-20261118`. Readiness and the
served console hash passed. Actual browser public pause/resume and Max expiry
passed with zero repeat WAV fetches and a 229 ms stop-to-start gap, against
7.779 seconds previously. The pause idle boundary passed at 120.155 seconds,
followed by a new word game; both «Кот» and «Мяч» completed actual PCM with
zero native TTS failures, and voice exit returned to the root menu. Evidence:
`runtime/local-integration-20261117/runpod-batch-10134.json`.
Personal themes became available but generation still failed after about
13 seconds without personal PCM. A separate native synthetic 3–5/dinosaurs
request reproduced a non-integer duration contract rejection before semantic
validation or TTS. Its exact other field type and the original browser age
were not retained, so that native probe does not establish an identical
browser failure cause. Source fixes `cfbca9a` (private browser PCM pause and
three reproduced races) and `0e5914b` (typed story-only JSON plus the integer
prompt) are independently reviewed and pushed. The strict semantic and actual
PCM-duration checks remain intact. The combined native after probe confirmed
the actual typed HTTP format, integer duration 80 and matching age, then failed
the next existing sentence gate: 24 words against the 3–5 limit of 18. Semantic
validation and TTS were not reached, so no personal PCM success is established.
A short-sentence correction subsequently passed the age gate and full semantic
validation. All 13 real Silero chunks were valid (peak 0.96044), but their
2,757,600 samples at 48 kHz totalled 57.45 seconds, below the unchanged 60-second
minimum. No silence, rate change or metadata override was used. The next drafting
volume guide is approximate and still requires actual duration acceptance.
The volume guide subsequently passed a complete real native request: 16 Silero
chunks, 3,134,400 samples at 48 kHz = 65.3 seconds, peak 0.99784; generation and
validation took 29.195 seconds within the 60-second deadline. This proves the
synthetic 3–5/dinosaurs service path, not physical hearing or other ages.
Evidence: operator scratch `work/personal-native-probe-20261118/after-volume-result.json`.
A separate actual-code test found browser button 7 queued behind personal
generation while the generating state was unpublished. Early state publication
and a scoped urgent cancel now preserve the queued releases and reject late
HTTP/provider work; ASGI-to-Node tests and independent review passed.
The exact five-file bundle is deployed in application PID 12428, with readiness
`READY` and served console SHA `4b3c30aeac2fdc29990b0793650a0f304a16d84e4ac2ac1f8fec687648f90e49`.
Hashes, prior files and deployment metadata are in
`/workspace/mishka-artifacts/personal-pcm-pause-20261118`. Pod/providers were not
restarted. Actual browser cancellation passed: button 7 returned to the root in
2.463 seconds through the proxy, both held keys were released, and no private
PCM remained. The next generation in the same visit failed after about 300 ms;
a clean visit also lost the generation choices. Chief reproduced a capability
deadlock: cancelling metadata refresh clears verification, while subsequent
entry requires verification before it can refresh. The live source matches
that reproduction and the receipt is unexpired; the precise live rejection
stage was not logged. Recovery after cancellation is being fixed with fresh
verification before any model request, preserving fail-closed model, receipt
and context checks. Whole personal browser playback/pause/resume remains open.
Evidence: `runtime/local-integration-20261117/runpod-personal-12428.json` and
`story-contract-cancel-deadlock-probe.json` in the same directory. Chief closed
the browser and released the live audio/GPU slot after this failed acceptance.
Recovery commit `629f255` changes only `story_context.py` in production: cancelled
verification preserves the last successful availability observation, while any
real metadata failure invalidates it and fences older successes. Each actual
model request still performs a fresh bounded verification. The 22 new recovery
regressions passed independently, and Chief/Reviewer approved the exact source.
It is deployed as PID 12927 with story-context SHA
`827a1c1fcf2500dbbb95f010f6ad0529fe33550eafbf9316e1e95e269328971a`;
readiness is `READY`, console `4b3c` is unchanged, and the pod/providers were not
restarted. Deployment evidence is in
`/workspace/mishka-artifacts/story-contract-cancel-recovery-20261118`.
The subsequent PID 12927 browser run still failed: cancellation passed in
2.522 seconds, but the next generation fell back after 297 ms without private
PCM. The corrected contract bug does not establish the cause of this live
failure. No further generation fix is inferred from the isolated service probe.
PID 13655 now adds local whitelisted stage diagnostics in `personal_story.py`
only (SHA `e032b10e998d66474eeaa55ab1e6ee74c21628743179351e68ffcf397b1ddc2d`).
Ten diagnostic/privacy tests passed independently; Chief approved the exact
diff. Constant stages/reasons, booleans, counts, known age bands and bounded
error classes reach local WARNING logs; story text, PCM, identities and raw
exceptions do not. Generation conditions remain unchanged. The coordinated
restart intentionally interrupted Chief's public story to check remote recovery.
Evidence: `/workspace/mishka-artifacts/personal-story-diagnostics-20261118`.
The live control request without prior cancellation passed: 64.85 seconds of
personal PCM, pause checkpoint 25.898 seconds, and resume at the exact offset
with a new lease. It then finished naturally; active button-7 exit was not
observed in that run. Remote restart/reload and the subsequent clean guest visit
also passed. The repeated generation-cancel path still failed at generation
with `StoryContextError/story_backend_changed`, after admission/jobs/budget
had passed. A read-only Ollama check found zero running models with a matching
server version and unexpired receipt; the scheduler recorded a load failure
whose bounded error classification mentioned cancellation, not memory failure.
Pinned scheduler source reloads for differing `num_ctx` or `shift`.
The candidate aligns the shared model's context at 16384 through the
existing two environment settings and pins `shift=false` for its receipt-bound
warmup, ordinary generation, story generation and both validator paths. Story
`truncate=false`, fresh verification and budgets remain unchanged. This avoids
the demonstrated reload trigger; automatic recovery from unrelated external
model eviction or a failed runner ping is not claimed. The exact three-file
candidate is deployed as PID 14544 with readiness `READY`. Both process context
settings are 16384; read-only `/api/ps` found one running model at context 16384
with the generator and validator receipt digests/runners matching. GPU memory
was 14,041 MiB used and 10,084 MiB free out of 24,576 MiB. Pod/providers were not
restarted. Deployment evidence is in
`/workspace/mishka-artifacts/stable-shared-runner-20261118`.
PID 14544 still failed after cancellation. The remaining caller was the separate
`OllamaInputClassifier`: it constructed its own payload, lacked the engine's
validator receipt, and switched the shared runner back to `shift=true` during
input classification. Live daemon launch flags corroborated that transition;
the pod clock was approximately 46.6 seconds ahead of the local QA clock at
the comparison, so cross-host event times require that correction.
The exact two-file repair is now deployed as PID 15307 with readiness `READY`:
`application.py` SHA `2a3d90f7c634dcf463e0eda1c07025c1c34b9687617f0c6f6861b098d64a4ae6`,
`input_safety.py` SHA `84bcef55a2108ed752080a51638862380cbe8d54a57b5e270aa225f89f73ecaa`.
The existing validator receipt reaches the actual application input classifier,
which retains the same runner pin while preserving classification policy,
thresholds, timeouts and failure handling. Eleven runner-profile cases passed
independently, including the actual application classifier; Chief approved the
exact diff. One running model at context 16384 was confirmed after startup.
Evidence: `/workspace/mishka-artifacts/input-classifier-runner-20261118`.
On PID 15307, actual browser cancellation returned in 2.928 seconds without
private PCM. Same-visit generation then reached whole-text semantic validation
and all 13 TTS sentences; one 16,384-context model remained resident. This
confirms recovery of that previously failing runner path. The complete personal
story still failed its final measured duration gate: 2,757,000 samples at
48,000 Hz gave 57.4375 seconds, below the 60-second minimum for this age band.
The joined body was 794 characters. Evidence:
`runtime/local-integration-20261117/runpod-input-classifier-15307.json`.
A bounded drafting-volume correction was subsequently implemented; padding, slowed
playback and weaker PCM limits are not accepted substitutes. This failure does
not invalidate the earlier 64.85-second browser pause/resume control, but full
repeated personal-story acceptance was not yet complete at that checkpoint.

Two further guest visits completed on PID 15307 with no JavaScript errors.
Age 2 selected the 1–3 policy, heard the offered date, received truthful
profile-required schedule unavailability, then exercised distinct free-light
responses and exited. Age 7 selected the 6–7 policy; rainbow answer 7 was
accepted as an answer and continued the quiz. The typed game-exit command
returned to root. Both handset-down endpoints cleared all 18 lights, held keys
and media. Evidence:
`runtime/local-integration-20261117/runpod-age-calendar-15307.json`.
Weather, a separate `what_next` request and profile calendar outcomes are not
established by these visits; navigation input here was typed, not a physical
microphone test.

Two permitted synthetic profile visits (`demo-1`, `demo-2`) also completed on
PID 15307: identified welcome reached the menu without recapturing name or
age, the offered date played, and both schedule requests were truthfully
blocked by missing identity scope. Identified entry alone does not establish
schedule permission. Both visits returned to root, then handset-down cleared
authorization, lights, held keys and media without JavaScript errors. Evidence:
`runtime/local-integration-20261117/runpod-profile-calendar-15307.json`.
This establishes two completed guest and two profile visits, not the required
three of each or a positive real schedule/weather-provider path.

The independently reviewed drafting-volume correction is now deployed as
application PID 15780, with readiness `READY`, one resident model and both
context settings still 16,384. Only `personal_story.py` changed in the runtime
(SHA-256 `c260a5c3d9ac301ef8f9a8c3c85d7a0354c504aae86e36b721559421cf24ea08`).
An undersized four-part draft can receive one measured revision before whole
semantic validation and synthesis, within the original owner deadline and
fresh provider context budget. Sufficient drafts make no second generation
request. The final measured PCM-duration gate remains unchanged. The exact
794-character regression, repeated-short failure, cancellation, deadline and
context-budget cases passed; 82 final affected tests passed, and Chief
independently reviewed the frozen source and ran the 15 new cases. Deployment
backups and manifest are under
`/workspace/mishka-artifacts/story-draft-volume-20261118`.
Actual repeated-generation, full PCM, pause/resume and active-exit acceptance
on PID 15780 was pending at deployment; drafting character counts do not establish seconds.

The PID 15780 browser run exposed an overly strict drafting guard: both draft
outputs measured 1,045 Russian-word characters, below the 1,120-character
target, and were rejected before semantic validation or TTS. Equal lengths do
not establish equal text. The target had incorrectly become a hard floor.
The reviewed correction separates a lower drafting floor (630/840/1260 by age
group) from the unchanged desired target (805/1120/1680). The actual two-request
HTTP regression confirms that the revision includes the original system
message and measured feedback, with strict schema and context options intact.
The 794-character/779-normalized regression still requires revision; sufficient
text with 57.4375 seconds of PCM still fails. All 18 final targeted cases and
Chief's independent 18-case run passed.

One native synthetic anonymous 4–5/dinosaurs request on this exact correction
passed whole semantic validation and all 17 real Silero chunks: 3,453,000
samples at 48,000 Hz, or 71.9375 seconds, completed in 20.962 seconds within the
unchanged 60-second request deadline. It used one generation, no revision,
and a 1,006-character body with at most 16 words per sentence. Cleanup passed.
This is native software evidence, not a browser or physical-hearing pass.
The correction was deployed as PID 16069, readiness `READY`, one resident model,
with exact `personal_story.py` SHA-256
`229c12f3bc82d76a3fe439034c05b93f660dc18649e0532bbe4174eaba569384`.
Manifest, backups, deployment metadata and the native result are under
`/workspace/mishka-artifacts/story-volume-floor-20261118`.
Real browser cancel/reentry, PCM pause/resume and active exit passed on this
version: cancellation returned in 2.483 seconds without private PCM; the next
request in the same visit produced 69.075 seconds of real PCM. Pause checkpoint
returned 200, resume used a new private lease, and button 7 while playback was
still active returned to root and stopped the source. The artifact records
`playing=true` immediately before active exit and `playing=false` afterward,
with no JavaScript errors:
`runtime/local-integration-20261117/runpod-floor-16069.json`.
Mixed continuation and the remaining visits were still pending at that point.
This single native control did not establish duration for other ages.

Three guest and three permitted profile visits have now completed with clean
handset-down endpoints. Entry and game source remained unchanged between the
recorded PIDs. The exact ordered game → successful story → conversation →
recovery chain was still incomplete at that point: its youngest story was rejected before TTS.

Further native age controls were isolated from the live application. A bounded
shared revision can now address both long sentences and insufficient plot
volume. The older 6–7 control passed on candidate `ef1b3d48`: 25 real Silero
chunks, 5,495,400 samples at 48,000 Hz (114.4875 seconds), completed in 37.846
seconds after one revision, with whole semantic allow and cleanup complete.
The youngest 1–3 control still failed the unchanged eight-word limit: the first
revision left one nine-word sentence. A subsequent addressed-feedback candidate
`1d92c2b5` also failed with two nine-word sentences. Neither produced TTS audio
or was deployed. Their exact source, checks and numeric-only native evidence
are preserved under `/workspace/mishka-artifacts/story-age-sentence-20261118`
and `/workspace/mishka-artifacts/story-sentence-target-20261118`.
The addressed extension leaves the successful older volume-only HTTP payload
unchanged, as checked by regression. Final targeted checks passed (29 cases),
but young model compliance remains a real failure. The next candidate applies
a request-local pattern only to the four youngest plot strings; older payloads
and three-argument custom providers retain their existing path. An actual
zero-evaluation conversion on the pinned runner produced four explicit
eight-word GBNF rules, without a generic-string fallback, in 0.049 seconds.
The schema and generated grammar are preserved under
`/workspace/mishka-artifacts/story-young-pattern-20261118`. Independent replay
of that exact generated grammar passed all 44 boundary cases. The young native
control then passed: one generation, whole semantic allow, 16 real Silero
chunks, 2,760,000 samples at 48,000 Hz (57.5 seconds), and at most eight words
per sentence. It completed in 18.532 seconds without a revision or deadline
expiry; cleanup completed. This measures full-story preparation, not ordinary
conversation latency or browser hearing. The candidate's 114 focused tests and
canonical acceptance passed. The reviewed candidate was then deployed as PID 16858, readiness
`READY`, with unchanged providers, receipt and both 16,384-token contexts.
Its manifest and rollback sources are in the same artifact directory. Actual
young guest game → personal story → pause/resume → active exit passed with
53.625 seconds of browser PCM. Initial wave movement and pause hold were
observed. Two samples over an early three-second resumed window did not differ;
that window ended before full PCM transfer and did not establish a stopped
effect. Evidence:
`runtime/local-integration-20261117/runpod-young-mixed-16858.json`.
A subsequent actual older 6–7 browser story passed at 104.150 seconds, with
pause checkpoint 22,869 ms, a new resumed lease at the same offset, and active
exit followed by clean hangup. After the resumed lease's final PCM frame,
received and rendered lighting both changed: `task_active=true`, frame counts
304 → 305 → 306, wave stages 6 → 0 → 1, and no engine error. Initial wave and
pause hold also passed. This closes resumed-light movement without changing
`lighting.py` or generating a story solely for lighting. Evidence:
`runtime/local-integration-20261117/runpod-old-final-16858.json`.
Physical microphone/hearing and final whole-demo acceptance remain unverified.

A one-shot browser `AudioBufferSourceNode.start` failure was injected during
ordinary PCM playback on PID 16069. The panel exposed an error and subsequent
chunks continued; ordinary PCM has no canonical playback identity, so this
did not exercise `/stand/audio/failed`. Hangup cleared the error, and a new
guest visit completed a useful ordinary turn with an actual new source and
drained audio, followed by a clean dark endpoint. Same-visit retry was not
established in that run: the first follow-up arrived after the separate 120-second profile
expiry and cannot establish audio recovery success or failure. Evidence:
`runtime/local-integration-20261117/runpod-controlled-audio-failure-16069.json`.
This is a browser playback-fault check, not a provider, physical microphone or
hearing check. A subsequent guest control on PID 16858 passed the same-visit
functional boundary: controlled start failure, a bounded fallback prompt,
then a completed new ordinary answer with an actual source and drained audio.
It exposed a concrete UI defect: the raw playback error persisted after that
successful answer. The focused client correction was independently reviewed
and applied without restarting PID 16858; authenticated HTTP serves
`console.js` SHA-256 `c28ed3fc6e63e95897b82c71e7ecb685e6726095bca9ffc7c3715cc45e82377a`.
It stops only chunks of the failed ordinary turn, preserves private playback
and the next turn, gives Russian retry/exit guidance, and clears the matching
old message at a new text attempt or successful new ordinary playback. Separate
attempt and cleanup fences suppress late ordinary errors, including the real
phone text bridge path; server stop cannot discard the displayed-error marker.
Eight affected Node suites and independent focused review passed. Actual final
guest acceptance on this exact client also passed: a controlled ordinary start
failure showed Russian retry/Tools/hangup guidance, remaining damaged chunks
stopped, and the next same-visit question completed with new audio and no stale
error. Explicit microphone enable reached capture/accepting with no pending
permission; hangup then cleared all media, pressed keys and all 18 lights.
Evidence: `runtime/local-integration-20261117/runpod-final-guest-retry-16858.json`.
The last retry took 4.405 seconds from the UI text action to the first source;
this includes opening/filling Tools, and its HTTP-start timestamp was not logged.
It excludes STT and physical hearing. The older full 104.150-second story took
25.852 seconds from request to first source, including full preparation.
Background scheduler diagnostics also passed both cancellation orders
through multiple full lighting cycles; no engine defect was reproduced, and
`lighting.py` was preserved.

On PID 10134, actual support and wellbeing paths passed, including local support
recording, declined assistance, and requested adult help. After an injected input
fault, reconnect alone did not clear the safety latch; a full handset placement
and subsequent lift restored a clean welcome. Use handset placement/reset for
operator recovery rather than treating transport reconnect as recovery. The UI
does not persist a distinct technical cause for that latch; do not infer it from
the last event. Authenticated Jupyter login and workspace reopening also passed
in Chief's actual browser; this does not claim personal observation of Roman's
login or physical hearing.

Pinned protocol source: [Ollama v0.40.16 API](https://github.com/ollama/ollama/blob/v0.40.16/api/types.go), [runner scheduling](https://github.com/ollama/ollama/blob/v0.40.16/server/sched.go), and [llamacpp execution](https://github.com/ollama/ollama/blob/v0.40.16/llm/llama_server.go). Native probe results are retained beside the four-file deployment evidence.

Full-story admission now uses the operator-owned receipt
`/workspace/private/story-backend-receipt.json` (0600, 30-day expiry), bound to
the observed Ollama 0.40.16 / Qwen3:14b digest and llamacpp runner. The actual
backend rejected an oversized 20,017-token prompt against context 4,096 with
HTTP 400 under `truncate=false`; `shift=false` launched without context shifting.
A 16,384-context load and clean-stop request passed (11.85 GB model VRAM).
Both generator and validator metadata contracts refreshed successfully;
all curated generation briefs fit the accepted ceiling. Full-story quality,
duration and live playback remain subject to actual acceptance. This receipt
is deployment evidence, not clinical or child-safety certification. Reuse the
existing verified receipt explicitly on a manual relaunch:

```bash
export MISHKA_STORY_BACKEND_RECEIPT_PATH=/workspace/private/story-backend-receipt.json
export LLM_NUM_CTX=16384
export VALIDATION_NUM_CTX=16384
```

The two context settings apply to this verified shared Qwen3:14b deployment.
Keep them equal to avoid reloading its runner between generation and validation.

The short operator path `/workspace/mishka` points to the existing application
checkout. It is an alias, not a migration of active process paths. The pod name
change preserved its ID, running process, ports and persistent storage.
After an accidental Jupyter move, the existing Whisper path and Ollama model
links were restored without restarting providers or downloading weights.
Preserve `/workspace/mishka-runtime/models/ollama`: its original blobs remain in
use through verified hardlinks. A real classifier request passed after recovery.

Versioning starts at `0.1.0`; this is a starting checkpoint, not the mission's
completion criterion. Each recorded version must identify its exact source and
actual checks, including unresolved limits. Implementation and independent
acceptance continue until the originally requested complete, reliable product
works. Existing API schema versions are not product-release evidence.


Jupyter runs on the pod's loopback port 8888 with token authentication. The
authorized SSH tunnel exposes it only at `http://127.0.0.1:18888/lab` on Roman's
laptop. SSH host identity was checked through the Runpod management channel;
strict host-key checking stays enabled. The current Wi-Fi source address is
`192.168.1.195`; this is a connection-specific bind, not a VPN or routing change.
Authenticated contents API returned 200 twice on reopening, unauthenticated API returned 403,
and agent browser sign-in/reopening passed. Roman's personal sign-in and
reopening have not been observed.

The owner-only local token file is
`/home/khugardin/Documents/Codex/2026-11-16/project-manager/work/mishka-remote-stand/recovery-20261117/private/jupyter-token`.
Paste its value into the Jupyter login form; never put it in a URL or report.
The remote token remains `/workspace/private/jupyter-token` (mode 0600).
If the laptop connection ends, reopen the tunnel using the current approved
source address and the verified task-specific known-hosts file:

```bash
ssh -b 192.168.1.195 -o BatchMode=yes -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile=/home/khugardin/Documents/Codex/2026-11-16/project-manager/work/mishka-remote-stand/recovery-20261117/runpod-known-hosts \
  -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
  -N -L 127.0.0.1:18888:127.0.0.1:8888 -p 8191 root@64.119.209.250
```

No model downloads are needed for the local mock control path:

```bash
/path/to/existing/python3.12 scripts/run_remote_stand.py \
  --profile mock --host 127.0.0.1 --port 8765 \
  --external-origin http://127.0.0.1:8765 \
  --max-source /absolute/reviewed/max-client-fixture \
  --state-dir "$PWD/runtime/remote-local"
```

The state root must be owner-only and contain no symlinks. A fresh `max-demo/`
is initialized once; an existing database/TLS identity is only validated and
reused. A port collision fails instead of replacing an existing listener.
SIGTERM/interrupt closes the Stand, capture client and only this launcher's
owned Max TLS server. A restart reuses the same credentials/database; choose a
new state directory for a fresh synthetic session store.

## Evidence and limits

`launch.json` in the protected state directory records profile, lifecycle
snapshot, listener/Origin, unchanged Max source hashes and final cleanup flags.
It contains no token or patient card. `stand-token` and Max demo state remain
private. Uvicorn access logs are disabled so rejected URL credentials are not
logged. Bootstrap saves GPU/runtime versions, dependency freeze and requirements
hash in `/workspace/mishka-artifacts/`.

Test the public HTTPS path unauthenticated first (401 for HTML, state and
assets; rejected WS), then login, same-origin device controls, canonical WS,
microphone permission/capture/playback, cancellation, hangup and reconnect.
Check `/ready` separately from `production_ready`; the latter must stay false.
Mock checks prove control/auth/cleanup behavior only. Real server CUDA model
warmup and an ASR → LLM → Silero audio request, latency, cancellation and joint
VRAM use require measurements on the authorized server and browser.
