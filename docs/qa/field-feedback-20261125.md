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

Implementation, final regression and independent review receipts are appended
only after the candidate is frozen. Baseline causal failures and intermediate
fixtures remain preserved in the same Obsidian field-feedback directory.
