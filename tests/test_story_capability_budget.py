import pytest
from test_story_backend_context import contract,URL,MODEL
from app.foundation.s2s import OllamaLLMProvider
from app.foundation.validation import OllamaMiniLLMValidator
from test_personal_story_app import start_app,send

@pytest.mark.asyncio
async def test_impossible_generator_not_advertised_but_mini_independent():
    c=await contract(context_ceiling=4096)
    llm=OllamaLLMProvider(URL,MODEL,1,story_backend_contract=c)
    mini=OllamaMiniLLMValidator(URL,MODEL,story_backend_contract=c)
    assert not llm.supports_full_story
    assert mini.supports_full_story

@pytest.mark.asyncio
@pytest.mark.parametrize('ceiling',[4096,8192,16384])
async def test_local_authored_chooser_does_not_require_dynamic_generator_budget(ceiling):
    from test_scene_menu_runtime import Harness
    from test_personal_story_service import Mini
    from app.foundation.validation import AsyncResponseValidator
    h=Harness()
    try:
        c=await contract(context_ceiling=ceiling)
        h.app.bounded_s2s.llm=OllamaLLMProvider(URL,MODEL,1,num_ctx=4096,story_backend_contract=c)
        h.app.bounded_s2s.response_validator=AsyncResponseValidator(mini=Mini())
        assert h.app.bounded_s2s.response_validator.supports_full_story
        await h.button(3)
        assert h.app.personal_story_available()
        page=h.app.canonical_runtime._menu_page(h.state)
        assert len(page.items)==5
        assert h.app.canonical_runtime.personal_story_capable
        assert h.render.texts==[page.spoken_text]
    finally:await h.close()

@pytest.mark.asyncio
async def test_actual_prompt_preflight_not_just_empty_request():
    from app.foundation.personal_story import PersonalStoryService
    from test_personal_story_service import engine
    c=await contract(context_ceiling=8192)
    llm=OllamaLLMProvider(URL,MODEL,1,story_backend_contract=c)
    assert llm.supports_full_story  # empty request fits, actual fixed prompt does not
    e=engine();e.llm=llm
    assert not PersonalStoryService(e).capable()

@pytest.mark.asyncio
async def test_validator_own_minimum_budget_guard():
    c=await contract(context_ceiling=4096,template_reserve=4096)
    assert not OllamaMiniLLMValidator(URL,MODEL,story_backend_contract=c).supports_full_story
