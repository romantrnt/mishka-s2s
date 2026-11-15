import json
import time
from dataclasses import asdict
import pytest
from app.foundation.story_context import StoryContextReceipt,load_story_contracts,StoryBackendContract
from app.foundation.config import Settings
from app.foundation.s2s import BoundedConversationEngine,OllamaLLMProvider,RetryingLLMProvider,MockTTSProvider,RVCProvider,MockPlaybackController
from app.foundation.runtime import S2SRuntimeManager
URL='http://127.0.0.1:11434'
def document():
    def row(model,digest):return asdict(StoryContextReceipt(URL,model,digest*64,'0.40.11',16384,2048,time.time()+300,truncate_false_verified=True,shift_false_verified=True))
    return {'generator':row('big','a'),'validator':row('mini','b')}
def load(path):return load_story_contracts(str(path),base_url=URL,generator_model='big',validator_model='mini')
@pytest.mark.parametrize('bad',['oversize','duplicate','extra','missing','binding','expiry','remote','relative','symlink'])
def test_bad_operator_files_fail_closed(tmp_path,bad):
    path=tmp_path/'receipt.json';data=document()
    if bad=='extra':data['api_override']=True
    if bad=='missing':del data['validator']['runner']
    if bad=='binding':data['validator']['model']='big'
    if bad=='expiry':data['validator']['expires_at']=0
    if bad=='remote':data['validator']['base_url']='http://example.com'
    raw=json.dumps(data)
    if bad=='oversize':raw=' '*16385
    if bad=='duplicate':raw='{"generator":{},"generator":{},"validator":{}}'
    path.write_text(raw)
    if bad=='relative':path='receipt.json'
    if bad=='symlink':link=tmp_path/'link';link.symlink_to(path);path=link
    assert load(path)=={}
@pytest.mark.asyncio
async def test_actual_engine_factory_and_controlled_runtime_refresh(tmp_path,monkeypatch):
    path=tmp_path/'receipt.json';path.write_text(json.dumps(document()))
    settings=Settings(profile='mock',llm_model='big',validation_provider='ollama',validation_model='mini',story_backend_receipt_path=str(path))
    llm=OllamaLLMProvider(URL,'big',1)
    engine=BoundedConversationEngine(RetryingLLMProvider(llm),MockTTSProvider(),RVCProvider(),MockPlaybackController(),settings=settings)
    mini=engine.response_validator.mini
    assert not llm.supports_full_story and not mini.supports_full_story
    calls=[]
    async def metadata(self,path):
        calls.append(path)
        return {'version':'0.40.11'} if path=='/api/version' else {'models':[{'name':self.receipt.model,'digest':self.receipt.model_digest,'runner':'llamacpp','context_length':1024}]}
    monkeypatch.setattr(StoryBackendContract,'_metadata',metadata)
    manager=S2SRuntimeManager(settings,llm,engine.tts,engine.voice,engine)
    await manager.warmup()  # mock ordinary warmup performs no inference
    assert llm.supports_full_story and mini.supports_full_story
    assert sorted(calls)==['/api/ps','/api/ps','/api/version','/api/version']
    assert mini.story_backend_contract is not llm.story_backend_contract
    assert 'story_backend_receipt_path' not in settings.redacted_snapshot()
    assert str(path) not in repr(settings.redacted_snapshot())
def test_env_empty_default_and_explicit_local_path(monkeypatch):
    monkeypatch.delenv('MISHKA_STORY_BACKEND_RECEIPT_PATH',raising=False)
    assert Settings.from_env().story_backend_receipt_path==''
    monkeypatch.setenv('MISHKA_STORY_BACKEND_RECEIPT_PATH','/operator/story.json')
    assert Settings.from_env().story_backend_receipt_path=='/operator/story.json'
