# Field feedback follow-up, 2026-11-25

Baseline: deployed `98eb56031885c8c0913d9f0ae5034e4fe999740b`.
Candidate: `ag/mishka-field-feedback-20261125`. This follow-up does not deploy,
restart or inject test inputs into the running stand, or mutate Runpod/Max.
No visitor recordings, transcripts, profile fields or credentials are evidence artifacts.

## Voice provenance and acoustic boundary

A fresh read-only runtime receipt matches the pinned VoxCPM2 base
`f7f964cf…`, adapter `1142e35d…`, reference `e5af4c97…`, source `300e4260…`,
48 kHz native output, no fallback and RVC off. The three approved introduction
assets (`PHRASE_001/078/099`) also match their existing hashes. This verifies
unchanged configuration and bytes, not perceived narrator similarity.

Five authorized adult-corpus provenance manifests were searched (1,353 rows,
including duplicates). One unique original “Привет, я Мишка!” was found; no
“Давай познакомимся”, “Как тебя зовут” or complete introduction take was found
in these manifests. The latter catalogue assets lack actor/model provenance;
an `approved` label alone does not identify the actor.

The exact baseline `SoundDevicePlaybackController.play_stream` executed against
a collecting PortAudio sink on three saved historical public synthetic WAVs.
All three raw/final PCM arrays were identical, including lengths. Inputs were
repacketized at 0.4/0.32 seconds; original arrival timing, underflow, current
fresh synthesis and acoustic device output were not reproduced. This test
cannot recover or assess phonemes already missing in the saved synthesis.

Two unapproved offline catalogue montages are available outside Git: A retains
all source padding, 7.150167 seconds; B applies documented low-energy edge trims
with 20 ms margins and constant per-phrase gain, 6.54 seconds. Both are matched
to -23 LUFS. No time stretching or new synthesis; neither was adopted into the
product or judged on listening. The original callback/microphone/help/end
lifecycle remains unchanged.

Fresh same-text current-primary/adult-original comparison, actual listening and
safe tube-output capture remain open. The adult greeting original exists, but
no matching fresh Vox render is saved. Local complete Vox base (~4.96 GB) is
absent, local free disk ~1.4 GB, RTX2060 has no native BF16. No model/fallback
substitution or live GPU inference was used to bypass those constraints.

Existing live numerical latency aggregates were read separately without text,
trace IDs or new input: LLM n=3 avg626.53 ms; STT n=8 avg310.87 ms; legacy
`silero_tts` n=38 avg104.22 ms. They are unpaired stage aggregates, not time to
first audible sound, a summable end-to-end trace, or proof of a Silero primary.

Reproducible scripts, hashes, public WAVs and exact boundaries are physically
saved in the existing Obsidian vault:
`Antigravity/demo-checkpoint-20261124/field-feedback-20261125/voice/README.md`.
No cloud sync or subjective voice acceptance is asserted.

## Name and attention-call changes

Name-slot baseline: four actual typed/native-PCM-fixture paths demonstrated
incorrect automatic commitment of “На” and an imperative after a naming prefix.
Negative grammatical ambiguity now uses the existing single owned confirmation;
explicit strong naming cues, short/rare/unknown names and dictionary homographs
remain available. Dictionary membership is not an admission requirement. No
ASR threshold, age policy, profile accessibility setting or Max data changed.
Optional morphology absence has a bounded grammatical fallback. Clarification
keeps existing voice/button correction, skip, help, lighting and End ownership.

Attention baseline: two actual bear-entry scripted-provider cases allowed a
reintroduction into TTS despite an already played owned introduction and empty
first-model-turn history. Complete attention calls now carry a continuation
requirement in the generator and the same mandatory output validator. A rejected
candidate gets at most one repair of the same input, then existing safe fallback;
a healthy acknowledgement still uses one provider and validator turn. Explicit
greetings and real End/Start remain legitimate. Attention preserves heard active
inline content/answer while never reviving offered consent. Four held-abort
cases exposed an engine shared-context stale restoration boundary; token and
captured owner are checked before restoration/repair and after queue completion.
The APP already guards detached context commits: no live corruption is claimed.

Final joined source `1419a9f862499609a5b963844da957b591e9c610`: **119 passed in
63.14 seconds**, no Torch/model loads, working tree clean, eleven production/test
file hashes identical before and after. Checks: new name/attention modules plus
existing dialogue-initiative, private-PCM and cancelled-WebSocket cleanup tests.
The Stand HTTP reconnect, real canonical resume path, End/Start and synthetic
native CRC/STT fixtures exercise the normal application entry. Fixtures do not
establish the original field ASR transcript, actual LLM adherence or acoustics.
Independent final review read the exact diff/callers, author and joined logs,
11/11 source hashes and 36/36 voice artifacts: no remaining actionable findings.

Causal REDs and intermediate failed fixtures remain preserved, including a
corrected dictionary homograph assertion and an incorrect await of synchronous
fixture disconnect. The full project suite, live ASR/LLM/audio and physical tube
acceptance were not run. There is no deployment in this follow-up.

Next acoustic step: isolate the pinned compatible Vox runtime and render the
available adult original's exact greeting text; compare loudness-matched adult,
raw native output and the normal final software path, timestamp synthesis and
first playback separately, then capture the tube only in an allowed safe window.
Inspect raw speaker/articulation before considering a justified reference/model
change. The current offline montages are audition drafts, not a voice fix.
