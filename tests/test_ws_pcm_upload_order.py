"""Actual WS/VAD/ASR boundary; synthetic PCM/STT, no model or device calls."""
import asyncio
import base64
import struct
import time

import pytest
from app.foundation.s2s import STTResult
from test_browser_capture_lifecycle import BrowserCapture, until

SPEECH=(.25,)*2048+(.5,)*2048+(.75,)*1024
SILENCE=(0.,)*4096
PCM=SPEECH+SILENCE

async def setup():
    h=await BrowserCapture().start()
    h.seen=[];h.recognized=[]
    original=h.app.receive_audio_async
    async def observe(sid,samples,sequence,**kwargs):
        h.seen.extend(samples)
        return await original(sid,samples,sequence,**kwargs)
    h.app.receive_audio_async=observe
    class Recognizer:
        async def transcribe(self,audio,cancelled):
            h.recognized.append(audio.samples)
            return STTResult('Стоп.',.99,0.,False)
    h.app.s2s_audio.recognizer=Recognizer()
    return h

async def send(socket,samples,frame_samples,pace=0):
    for offset in range(0,len(samples),frame_samples):
        values=samples[offset:offset+frame_samples]
        socket.messages.put_nowait({'type':'audio_pcm','sample_rate':16000,
            'data_b64':base64.b64encode(struct.pack('<'+'f'*len(values),*values)).decode()})
        if pace:await asyncio.sleep(pace)

@pytest.mark.asyncio
async def test_valid_burst_2048_uploads_reach_asr_in_exact_wire_order():
    h=await setup()
    try:
        ws,_=await h.connect()
        await send(ws,PCM,2048)
        await until(lambda:len(h.seen)==len(PCM))
        assert h.seen==list(PCM),'concurrent uploads reordered actual 512-sample UART/VAD ingress'
        await until(lambda:bool(h.recognized))
        await until(lambda:h.app.canonical_runtime.sessions[h.sid].mode=='root_menu')
        assert h.recognized==[PCM]
        assert not any(m.get('type')=='error' for m in ws.sent)
    finally:await h.close()

@pytest.mark.asyncio
async def test_normal_browser_paced_512_frames_keep_order_and_recognize_once():
    h=await setup()
    try:
        ws,_=await h.connect();started=time.perf_counter()
        await send(ws,PCM,512,.032)
        await until(lambda:h.app.canonical_runtime.sessions[h.sid].mode=='root_menu')
        assert h.seen==list(PCM) and h.recognized==[PCM]
        assert time.perf_counter()-started<4.,'paced synthetic ingress exceeded the bounded local acceptance deadline'
        assert not any(m.get('type')=='error' for m in ws.sent)
    finally:await h.close()

@pytest.mark.asyncio
async def test_revoked_queued_upload_cannot_reappear_or_block_replacement_capture():
    h=await setup();entered,release=asyncio.Event(),asyncio.Event()
    original=h.stand.inject_pcm;calls=[]
    async def delayed(samples,*args,**kwargs):
        calls.append(samples[0])
        if samples[0]==.125:
            entered.set()
            try:await release.wait()
            except asyncio.CancelledError:await release.wait()
        return await original(samples,*args,**kwargs)
    h.stand.inject_pcm=delayed
    try:
        old,task=await h.connect()
        await send(old,(.125,)*2048,2048);await asyncio.wait_for(entered.wait(),2)
        await send(old,(.375,)*2048,2048)
        old.messages.put_nowait(None)
        await until(lambda:not h.stand.hub.is_owner(old))
        fresh,_=await h.connect()
        await send(fresh,PCM,2048)
        await until(lambda:bool(h.recognized))
        assert h.recognized==[PCM]
        release.set();await asyncio.wait_for(task,3)
        assert .375 not in calls,'revoked upload entered after waiting for the old transaction'
        assert h.recognized==[PCM] and .125 not in h.seen and .375 not in h.seen
        assert h.stand.hub.is_owner(fresh)
    finally:release.set();await h.close()
