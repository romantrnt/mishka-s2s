# Native AEC isolation probe

This experiment never opens devices, starts an application, changes turn-taking
or touches the deployed environment. It uses the previously generated public
Vox555 corpus, validates input SHA256, and processes 10 ms mono 16 kHz frames
on CPU with `pywebrtc-audio==0.2.0`. NS, AGC and high-pass flags are requested off.
The inspected upstream binding enables a high-pass filter when AEC is enabled,
even with that flag off. Its exact source-to-wheel identity still needs checking;
do not attribute all measured attenuation to the echo suppressor alone.

```sh
python3 -m venv /tmp/mishka-aec-env
/tmp/mishka-aec-env/bin/python -m pip install --only-binary=:all: -r experiments/native-aec/requirements.txt
/tmp/mishka-aec-env/bin/python experiments/native-aec/echo_probe.py \
  --corpus /outside/git/phonetics-vox555-62dd254 --output /outside/git/native-aec-synthetic
```

The far end is an authored story; the simulated near-end speaker is another
authored greeting WAV, not a child recording. Synthetic room echoes have 40,
80 or 150 ms delay plus two reflections. Nine cases cover echo alone, double
talk, and near speech with a far reference but no actual echo. Mixed, target
and processed WAVs remain outside Git for listening.

Actual 24 November result: echo-only ERLE about 67–68 dB; CPU p95 below 0.31 ms
per 10 ms frame. **The preservation check is not accepted**: best aligned
near-speech correlation in double talk was 0.53–0.58, gain 0.34–0.43; no-echo
controls also showed attenuation. These digital metrics do not prove perceived
quality. Do not integrate this configuration or enable native full duplex from
echo suppression alone. Investigate attenuation/alignment with independent
human speech and review the actual binding/version before choosing the path.

## Integration boundary still to implement and verify

The current native application captures ALSA USB input directly. Pulse showing
only `RDPSink.monitor` does not mean the real microphone is absent; its actual
capture FD was `/dev/snd/pcmC0D0c`, running at 48 kHz. Blind Pulse default-source
changes would not provide a valid AEC playback reference for that path.

A production integration needs bounded render-reference frames from the exact
samples submitted to the current output stream, consistent resampling/timing,
estimated hardware delay, and generation/session ownership on both input and
output. Reset AEC/reference state on stop, device loss and new visit. One owner
processes each instance; the binding is not thread-safe. Underflow, overflow,
cancellation, changing sample rates and real double talk need checks before
turn-taking can be safely relaxed. Keep microphone data in RAM; do not add
conversation recording. Browser `echoCancellation: true` and native AEC are
different paths and need separate acceptance.

Primary sources:
[binding repository](https://github.com/strands-labs/pywebrtc-audio),
[actual float scaling/buffering implementation](https://github.com/strands-labs/pywebrtc-audio/blob/main/bindings/webrtc_audio_bindings.cpp),
[real-time example](https://github.com/strands-labs/pywebrtc-audio/blob/main/examples/pyaudio_realtime.py).
Upstream is Apache-2.0; preserve its NOTICE and dependencies' licenses when
packaging. It is an experiment-only dependency, absent from application requirements.
