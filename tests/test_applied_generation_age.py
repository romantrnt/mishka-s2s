from dataclasses import replace
import pytest
from app.foundation.age_policy import POLICIES
from app.foundation.s2s import BoundedConversationEngine, MockTTSProvider, MockPlaybackController, RVCProvider, ConversationContext, ProfileContext, S2SConfig

class LLM:
    def __init__(self,text): self.text=text;self.messages=[]
    async def stream(self,messages,request_id,cancelled):
        self.messages=messages
        yield '<say>'+self.text+'</say>'

@pytest.mark.asyncio
@pytest.mark.parametrize('band',list(POLICIES))
@pytest.mark.parametrize('extra',[0,1])
async def test_dialogue_uses_actual_age_limit_before_oversize_audio(band,extra):
    words=POLICIES[band].max_words
    llm=LLM(' '.join(['Привет']*(words+extra))+'.')
    e=BoundedConversationEngine(llm,MockTTSProvider(),RVCProvider(),MockPlaybackController(),config=S2SConfig(),profile='mock')
    context=ConversationContext('age','canonical_product','bear_dialogue',('__all__',),('diagnosis',),10,120,2,
                                profile=ProfileContext(age_band=band))
    result=await e.run_text_turn('Привет',context)
    assert f'at most {words} words' in llm.messages[0]['content']
    if extra:
        assert result.fallback_reason
        assert not e.playback.played
    else:
        assert not result.fallback_reason
        assert e.playback.played
