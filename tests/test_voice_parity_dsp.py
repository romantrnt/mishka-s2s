"""Cheap analytic signal checks; no model/audio device dependencies."""
import importlib.util
from pathlib import Path
import numpy as np
import pytest

spec=importlib.util.spec_from_file_location('qa_voice_parity',Path(__file__).resolve().parents[1]/'scripts/qa_voice_parity.py')
qa=importlib.util.module_from_spec(spec);spec.loader.exec_module(qa)

def tone(rate=12000,seconds=1.):
    return .15*np.sin(2*np.pi*120*np.arange(round(rate*seconds))/rate)

def test_pitch_activity_and_clipping_have_known_signal_baseline():
    audio=np.concatenate([np.zeros(1200),tone(),np.zeros(2400)])
    result=qa.measure(audio,12000)
    assert result['pitch_proxy']['median_hz']==pytest.approx(120,abs=2)
    assert result['leading_quiet_s']==pytest.approx(.1,abs=.02)
    assert result['trailing_quiet_s']==pytest.approx(.2,abs=.02)
    assert result['clipped_sample_fraction']==0

def test_level_matching_preserves_relative_dynamics_and_samples():
    a=tone();b=a*.2
    aa,bb,meta=qa.equal_level_pair(a,b,12000)
    assert np.max(np.abs(aa))<=.95 and len(aa)==len(a)
    assert np.allclose(aa,bb)
    assert np.corrcoef(a,aa)[0,1]==pytest.approx(1.)
    assert qa.compare(a,a,12000)['frames_equal']
    assert qa.compare(a,a,12000)['waveform_correlation']==pytest.approx(1.)

def test_hash_or_unknown_speaker_cannot_enter_audit(tmp_path):
    with pytest.raises(ValueError,match='speaker'):
        qa.read_approved({'speaker_scope':'unknown','path':str(tmp_path/'missing'),'sha256':'x'})

def test_mismatched_hash_cannot_enter_audit(tmp_path):
    path=tmp_path/'approved-fixture.wav'
    qa.wavfile.write(path,12000,tone().astype(np.float32))
    with pytest.raises(ValueError,match='size/hash'):
        qa.read_approved({'speaker_scope':'approved_adult_original','path':str(path),'sha256':'0'*64})
