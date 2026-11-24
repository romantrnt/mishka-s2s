"""Small sequential WAV audit. No Torch, model, device or network imports.

F0/activity estimates are DSP proxies, never a listening or voice-identity score.
Input files must be explicitly approved adult/public assets with exact hashes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import resource
import shutil
import sys
import time
import scipy
from math import gcd
from pathlib import Path

import numpy as np
from scipy.io import wavfile
from scipy.signal import correlate, find_peaks, resample_poly


def sha256(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):digest.update(block)
    return digest.hexdigest()


def read_approved(asset):
    if asset['speaker_scope'] not in {'approved_adult_original','approved_adult_derived','public_synthetic_vox555'}:
        raise ValueError('unapproved speaker scope')
    path=Path(asset['path'])
    if path.stat().st_size>40*1024*1024 or sha256(path)!=asset['sha256']:
        raise ValueError('asset size/hash mismatch')
    rate,raw=wavfile.read(path)
    if raw.ndim!=1 or not 8000<=rate<=96000:
        raise ValueError('audit requires bounded mono WAV')
    if raw.dtype.kind=='i':
        audio=raw.astype(np.float64)/(2**(raw.dtype.itemsize*8-1))
    elif raw.dtype.kind=='u':
        audio=(raw.astype(np.float64)-128.)/128.
    elif raw.dtype.kind=='f':audio=raw.astype(np.float64)
    else:raise ValueError('unsupported PCM')
    if not len(audio) or not np.isfinite(audio).all():raise ValueError('invalid PCM')
    return int(rate),audio


def db(value):return float(20*np.log10(max(float(value),1e-12)))


def frame_rms(audio,rate):
    window=max(1,round(rate*.02));hop=max(1,round(rate*.01))
    return np.array([np.sqrt(np.mean(audio[start:start+window]**2))
                     for start in range(0,len(audio),hop)])


def activity(audio,rate):
    rms=frame_rms(audio,rate)
    threshold=max(10**(-50/20),float(np.percentile(rms,95))*10**(-35/20))
    active=rms>=threshold
    indices=np.flatnonzero(active)
    if not len(indices):return rms,active,threshold,0.,len(audio)/rate
    return rms,active,threshold,float(indices[0])*.01,max(0.,len(audio)/rate-(indices[-1]*.01+.02))


def pitch_proxy(audio,rate):
    target=12000;divisor=gcd(rate,target)
    small=resample_poly(audio,target//divisor,rate//divisor) if rate!=target else audio
    window=round(target*.04);hop=round(target*.01)
    values=[];strengths=[]
    global_rms=float(np.sqrt(np.mean(small**2)))
    for start in range(0,len(small)-window+1,hop):
        frame=small[start:start+window]
        if np.sqrt(np.mean(frame**2))<max(.001,global_rms*.1):continue
        frame=(frame-np.mean(frame))*np.hanning(window)
        corr=correlate(frame,frame,mode='full',method='fft')[window-1:]
        if corr[0]<1e-12:continue
        low,high=round(target/450),round(target/65)
        peaks,_=find_peaks(corr[low:high+1])
        if not len(peaks):continue
        best=int(peaks[np.argmax(corr[low+peaks])])+low
        strength=float(corr[best]/corr[0])
        if strength<.55:continue
        shift=0.
        if 0<best<len(corr)-1:
            denominator=corr[best-1]-2*corr[best]+corr[best+1]
            if denominator:shift=float(.5*(corr[best-1]-corr[best+1])/denominator)
        values.append(target/(best+shift));strengths.append(strength)
    return {'algorithm':'40ms window/10ms hop autocorrelation,65–450Hz,strength>=0.55;octave errors possible',
            'voiced_frames':len(values),'median_hz':float(np.median(values)) if values else None,
            'p10_hz':float(np.percentile(values,10)) if values else None,
            'p90_hz':float(np.percentile(values,90)) if values else None,
            'median_strength':float(np.median(strengths)) if strengths else None}


def measure(audio,rate):
    rms,active,threshold,leading,trailing=activity(audio,rate)
    middle=~active
    if np.any(active):middle[:np.flatnonzero(active)[0]]=False;middle[np.flatnonzero(active)[-1]+1:]=False
    pauses=[];start=None
    for index,quiet in enumerate([*middle,False]):
        if quiet and start is None:start=index
        if not quiet and start is not None:
            seconds=(index-start)*.01
            if seconds>=.08:pauses.append(seconds)
            start=None
    active_rms=float(np.sqrt(np.mean(rms[active]**2))) if np.any(active) else 0.
    return {'sample_rate':rate,'frames':len(audio),'duration_s':len(audio)/rate,
            'peak':float(np.max(np.abs(audio))),'rms_dbfs':db(np.sqrt(np.mean(audio**2))),
            'active_rms_dbfs':db(active_rms),'dc':float(np.mean(audio)),
            'clipped_sample_fraction':float(np.mean(np.abs(audio)>=.999)),
            'activity_threshold_dbfs':db(threshold),'leading_quiet_s':leading,'trailing_quiet_s':trailing,
            'internal_pauses_s':pauses,'active_fraction':float(np.mean(active)),
            'pitch_proxy':pitch_proxy(audio,rate)}


def equal_level_pair(a,b,rate,target_dbfs=-23.):
    def level(audio):
        rms,active,*_=activity(audio,rate)
        return float(np.sqrt(np.mean(rms[active]**2))) if np.any(active) else 0.
    levels=[level(a),level(b)]
    if min(levels)<=0:raise ValueError('silent comparison')
    # One shared target; preserve all relative dynamics and avoid clipping.
    target=min(10**(target_dbfs/20),*(.95*level/max(float(np.max(np.abs(audio))),1e-12) for audio,level in zip((a,b),levels)))
    gains=[target/level for level in levels]
    return a*gains[0],b*gains[1],{'kind':'active-frame RMS,not EBU integrated loudness','target_dbfs':db(target),'gains_db':[db(gain) for gain in gains]}


def compare(a,b,rate):
    count=min(len(a),len(b));aa=a[:count];bb=b[:count]
    envelope_a=frame_rms(aa,rate);envelope_b=frame_rms(bb,rate)
    return {'duration_delta_ms':1000*(len(b)-len(a))/rate,
            'waveform_correlation':float(np.corrcoef(aa,bb)[0,1]),
            'envelope_correlation':float(np.corrcoef(envelope_a,envelope_b)[0,1]),
            'difference_rms_dbfs':db(np.sqrt(np.mean((aa-bb)**2))),
            'max_absolute_difference':float(np.max(np.abs(aa-bb))),
            'frames_equal':len(a)==len(b)}


def game_onset_probe(audio,rate):
    # Approximate resampling with SciPy, then inspect the exact trim threshold.
    # No fade, application helper or production signal is exercised here.
    divisor=gcd(rate,24000)
    small=resample_poly(audio,24000//divisor,rate//divisor)
    first=small[:9600]
    indices=np.flatnonzero(np.abs(first)>=.004)
    onset=int(indices[0]) if len(indices) else len(first)
    removed=max(0,onset-360)
    energy=float(np.sum(first**2))
    return {'scope':'SciPy resampling surrogate plus current game first-subchunk trim rule; excludes fade/app execution/phoneme recognition',
            'threshold':.004,'retained_pre_onset_ms':15,'removed_ms':removed/24,
            'removed_energy_fraction':float(np.sum(first[:removed]**2)/energy) if energy else 0.}


def run(manifest_path,output):
    started=time.monotonic()
    manifest=json.loads(Path(manifest_path).read_text());output=Path(output);output.mkdir(parents=True,exist_ok=True)
    results=[];files={}
    for pair in manifest['pairs']:
        rate,a=read_approved(pair['a']);other,b=read_approved(pair['b'])
        if rate!=other:raise ValueError('comparison pair rates differ')
        result={key:pair[key] for key in ('id','text','scope')}
        result.update(a=measure(a,rate),b=measure(b,rate),comparison=compare(a,b,rate),game_onset_probe=game_onset_probe(b,rate))
        normalized_a,normalized_b,matching=equal_level_pair(a,b,rate)
        result['level_matching']=matching
        result['assets']={}
        for label,signal in [('a',normalized_a),('b',normalized_b)]:
            name=pair['id']+'-'+label+'-level-matched.wav'
            wavfile.write(output/name,rate,np.round(np.clip(signal,-1,1)*32767).astype('<i2'))
            result['assets'][label]=name;files[name]=sha256(output/name)
        results.append(result)
    originals=[]
    for asset in manifest.get('originals',[]):
        rate,a=read_approved(asset)
        name='original-target-'+asset['id']+'.wav'
        shutil.copyfile(asset['path'],output/name)
        files[name]=sha256(output/name)
        originals.append({'id':asset['id'],'provenance':asset,'metrics':measure(a,rate),'asset':name})
    report={'source_commit':manifest['source_commit'],'scope':manifest['scope'],'pairs':results,'originals':originals,
            'listening_performed':False,'original_to_vox_exact_text_pairs':manifest.get('original_to_vox_exact_text_pairs',0),
            'limits':['F0 proxies are not verified pitch/identity or articulation scores.','Active-frame RMS matching is not a hearing test or EBU loudness certification.','Saved renders are historical metadata-pinned samples, not a new live/runtime render.']}
    report['execution']={'elapsed_s':round(time.monotonic()-started,3),'peak_rss_kib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,'torch_imported':'torch' in sys.modules,'numpy':np.__version__,'scipy':scipy.__version__}
    (output/'MEASUREMENTS.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    files['MEASUREMENTS.json']=sha256(output/'MEASUREMENTS.json')
    (output/'AUDIO_SHA256.json').write_text(json.dumps(files,indent=2)+'\n')
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--manifest',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args();report=run(args.manifest,args.output)
    print(json.dumps({'pairs':len(report['pairs']),'originals':len(report['originals']),'listening_performed':False}))
