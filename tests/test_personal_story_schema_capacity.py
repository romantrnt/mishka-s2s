import json
import pytest
from app.foundation.personal_story import PersonalStoryService
from test_personal_story_service import engine, PLOT_SENTENCES

@pytest.mark.asyncio
@pytest.mark.parametrize('size,accepted',[(6000,True),(6001,False)])
async def test_derived_story_body_limit_independent_of_wire_limit(size,accepted):
    e=engine();sections={}
    # Four distinct sections, each using short complete sentences. Fill the
    # exact character budget with ordinary spaces, not synthesized silence.
    base=[' '.join(part[:7]) for part in PLOT_SENTENCES]
    remaining=size-(sum(map(len,base))+3)
    for index,key in enumerate(('setting','problem','attempt','resolution')):
        extra=remaining//4+(index<remaining%4)
        sections[key]=base[index][:-1]+' '*extra+'.'
    candidate={'title':'Путь','plot':sections,'age_band':'3-5',
               'estimated_duration_sec':60,'safety_tags':['safe'],'ending_state':'safe_resolved'}
    raw=json.dumps(candidate,ensure_ascii=False)
    assert len(' '.join(sections.values()))==size
    assert len(raw)<8000
    async def streamed(*args):yield raw
    e.llm.stream_story=streamed
    seen=[]
    class Validator:
        supports_full_story=True
        async def validate(self,text,context=()):
            seen.append(text)
            # Stop at semantic boundary; this is a capacity test, not a
            # recommendation to accept padded prose or a quality claim.
            from app.foundation.validation import ValidationDecision
            return ValidationDecision('block',False,'test_boundary',1.,'test')
    e.response_validator=Validator()
    assert await PersonalStoryService(e).render('лес','3-5',(),1,lambda:True) is None
    assert bool(seen)==accepted
    if accepted:assert seen[0]=='Путь. '+' '.join(sections.values())
    assert not e.tts.calls
