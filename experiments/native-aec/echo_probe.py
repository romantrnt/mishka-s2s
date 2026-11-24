"""Offline synthetic echo/double-talk probe; never opens a microphone or speaker."""
import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import time
import wave

import numpy as np
from scipy.signal import resample_poly
from pywebrtc_audio import AudioProcessor

RATE = 16000
FRAME = RATE // 100


def read_wav(path):
    with wave.open(str(path), 'rb') as source:
        if source.getnchannels() != 1 or source.getsampwidth() != 2:
            raise ValueError('Expected mono PCM16 authored WAV')
        samples = np.frombuffer(source.readframes(source.getnframes()), dtype='<i2').astype(np.float32)/32768
        rate = source.getframerate()
    divisor = np.gcd(rate, RATE)
    return resample_poly(samples, RATE//divisor, rate//divisor).astype(np.float32)


def db_ratio(before, after):
    return float(10*np.log10((np.mean(before**2)+1e-12)/(np.mean(after**2)+1e-12)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads((args.corpus/'result.json').read_text())
    files = {row['wav']: row['sha256'] for row in manifest['rows']}
    def authored(name):
        path = args.corpus/name
        if hashlib.sha256(path.read_bytes()).hexdigest() != files[name]:
            raise ValueError('Corpus WAV hash mismatch')
        return read_wav(path)
    far = np.resize(authored('story-baseline-stream.wav'), RATE*16)*.6
    child = np.zeros_like(far)
    child[4*RATE:10*RATE] = np.resize(authored('greeting-baseline-stream.wav'), RATE*6)*.6
    echo_only = np.r_[np.arange(2*RATE, 4*RATE), np.arange(11*RATE, 16*RATE)]
    talking = slice(5*RATE, 10*RATE)
    rows = []
    args.output.mkdir(parents=True, exist_ok=True)
    for delay_ms in (40, 80, 150):
        for double_talk, echo_present in ((False, True), (True, True), (True, False)):
            echo = np.zeros_like(far)
            for extra, gain in ((0, .45), (7, .15), (15, .07)):
                offset = (delay_ms+extra)*RATE//1000
                echo[offset:] += gain*far[:-offset]
            if not echo_present:
                echo.fill(0)
            target = child if double_talk else np.zeros_like(child)
            near = echo + target
            processor = AudioProcessor(sample_rate=RATE, echo_cancellation=True,
                                       stream_delay_ms=delay_ms)
            clean = np.zeros_like(near)
            timings = []
            for begin in range(0, len(near), FRAME):
                started = time.perf_counter()
                clean[begin:begin+FRAME] = processor.process(near[begin:begin+FRAME], far[begin:begin+FRAME])
                timings.append((time.perf_counter()-started)*1000)
            if not np.isfinite(clean).all():
                raise ValueError('Non-finite AEC output')
            row = {'delay_ms': delay_ms, 'double_talk': double_talk, 'echo_present': echo_present,
                   'frame_cpu_p95_ms': round(float(np.percentile(timings, 95)), 3)}
            if echo_present:
                row['echo_only_erle_db'] = round(db_ratio(echo[echo_only], clean[echo_only]), 3)
            if double_talk:
                # AEC introduces processing delay. Report the best aligned
                # correlation/gain, without claiming human intelligibility.
                reference = child[talking]
                candidates = []
                for lag in range(0, 3*FRAME+1):
                    output = clean[5*RATE+lag:10*RATE+lag]
                    gain = float(np.dot(output, reference)/(np.dot(reference, reference)+1e-12))
                    corr = float(np.dot(output, reference)/(np.linalg.norm(output)*np.linalg.norm(reference)+1e-12))
                    candidates.append((corr, gain, lag))
                corr, gain, lag = max(candidates)
                row.update(near_speech_correlation=round(corr, 4), near_speech_gain=round(gain, 4), alignment_samples=lag)
            stem = f'delay{delay_ms}-double{int(double_talk)}-echo{int(echo_present)}'
            for name, samples in [('mixed', near), ('clean', clean), ('target', target)]:
                with wave.open(str(args.output/f'{stem}-{name}.wav'), 'wb') as output:
                    output.setnchannels(1); output.setsampwidth(2); output.setframerate(RATE)
                    output.writeframes((np.clip(samples, -1, 1)*32767).astype('<i2').tobytes())
            rows.append(row)
            print(json.dumps(row), flush=True)
    (args.output/'result.json').write_text(json.dumps({'scope': 'Synthetic digital echo; no physical AEC acceptance',
        'dependency': importlib.metadata.version('pywebrtc-audio'), 'rows': rows}, indent=2)+'\n')


if __name__ == '__main__':
    main()
