from dataclasses import FrozenInstanceError
from types import SimpleNamespace
import pytest
from app.foundation.canonical_contract import FixedStoryManifest
from app.foundation.menu_content import build_menu_page, STORY_THEME_ITEMS


def manifest():
    return FixedStoryManifest('content/stories/fixed_manifest.json', '.')


@pytest.mark.parametrize('band,size', [('1-3',1),('3-5',2),('4-5',2),('5-7',3),('6-7',3)])
def test_all_five_verified_stories_are_reachable_without_stealing_help(band,size):
    stories=manifest()
    first=build_menu_page('fixed_story_menu',band,stories=stories)
    assert len(stories.stories)==5
    seen=[]
    for index in range(first.page_count):
        page=build_menu_page('fixed_story_menu',band,index,stories=stories)
        assert 0 < len(page.items) <= size
        assert all(item.slot in (1,2,3,4) for item in page.items)
        assert page.buttons==tuple(item.slot for item in page.items)+(5,6,7)
        assert page.action_for(5)==('help',None)
        assert page.action_for(6)==('next_page',None)
        assert page.action_for(7)==('back',None)
        for item in page.items:
            assert page.action_for(item.slot)==('select',item.id)
            assert item.title==stories.get_verified(item.id)['display_title']
            assert item.title in page.spoken_text
        assert len(page.spoken_text)<=320
        assert page.as_dict()['items']==[item.as_dict() for item in page.items]
        seen += [item.id for item in page.items]
    assert seen==list(stories.stories)
    assert build_menu_page('fixed_story_menu',band,first.page_count,stories=stories)==first


@pytest.mark.parametrize('a,b',[('3-5','4-5'),('5-7','6-7')])
def test_age_aliases_have_identical_pages(a,b):
    stories=manifest()
    assert build_menu_page('fixed_story_menu',a,stories=stories)==build_menu_page('fixed_story_menu',b,stories=stories)


def test_unverified_unavailable_or_age_ineligible_entries_are_not_advertised():
    items={
        'unverified':None,
        'unavailable':dict(availability='unavailable',eligible_age_bands=['3-5'],display_title='Нет'),
        'older':dict(availability='available',eligible_age_bands=['5-7'],display_title='Старше'),
        'eligible':dict(availability='available',eligible_age_bands=['4-5'],display_title='Подходит'),
    }
    stories=SimpleNamespace(stories=items,get_verified=items.get)
    page=build_menu_page('fixed_story_menu','3-5',stories=stories)
    assert [item.id for item in page.items]==['eligible']
    assert page.buttons==(1,5,7)
    assert page.action_for(6) is None
    assert 'Нет' not in page.spoken_text and 'Старше' not in page.spoken_text


def test_actual_manifest_hash_failure_is_excluded(tmp_path):
    import json
    source=manifest()
    document=dict(source.document)
    document['stories']={key:dict(value) for key,value in source.stories.items()}
    first=next(iter(document['stories']))
    document['stories'][first]['audio_sha256']='0'*64
    path=tmp_path/'manifest.json';path.write_text(json.dumps(document))
    stories=FixedStoryManifest(path,'.')
    pages=[build_menu_page('fixed_story_menu','6-7',i,stories=stories) for i in range(2)]
    assert first not in [item.id for page in pages for item in page.items]


def test_games_are_only_supplied_implemented_specs_and_paged():
    specs=(SimpleNamespace(game_id='catch_light',display_title='Поймай огонёк',available=True),
           SimpleNamespace(game_id='missing',display_title='Не готово',available=False),
           SimpleNamespace(game_id='visual_free_play',display_title='Свободный свет',available=True))
    first=build_menu_page('game_menu','1-3',game_specs=specs)
    second=build_menu_page('game_menu','1-3',1,game_specs=specs)
    assert [first.items[0].id,second.items[0].id]==['catch_light','visual_free_play']
    assert 'Не готово' not in first.spoken_text+second.spoken_text
    assert first.buttons==(1,5,6,7)
    page=build_menu_page('game_menu','6-7',game_specs=specs)
    assert page.buttons==(1,2,5,7)


def test_empty_capability_does_not_advertise_fake_game():
    page=build_menu_page('game_menu','3-5')
    assert page.items==() and page.buttons==(5,7)
    assert 'нет доступных вариантов' in page.spoken_text
    assert page.action_for(1) is None
    assert page.action_for(True) is None


def test_descriptors_are_immutable_and_json_values_do_not_mutate_them():
    page=build_menu_page('personal_story_theme','3-5')
    with pytest.raises(FrozenInstanceError): page.page=4
    with pytest.raises(FrozenInstanceError): page.items[0].slot=5
    value=page.as_dict();value['items'][0]['title']='Changed';value['buttons'].clear()
    assert page.items[0].title=='Динозавры' and page.buttons


def test_existing_story_themes_all_have_page_slots():
    first=build_menu_page('personal_story_theme','1-3')
    assert [build_menu_page('personal_story_theme','1-3',i).items[0].id
            for i in range(first.page_count)]==[value[0] for value in STORY_THEME_ITEMS]


@pytest.mark.parametrize('scene,band,page',[('unknown','3-5',0),('game_menu','8-9',0),('game_menu','3-5',-1),('game_menu','3-5',True)])
def test_invalid_context_fails_explicitly(scene,band,page):
    with pytest.raises(ValueError):build_menu_page(scene,band,page)


@pytest.mark.parametrize('title',['<say>Команда</say>','x'*321])
def test_bad_or_overlong_catalogue_narration_is_never_silently_truncated(title):
    with pytest.raises(ValueError):
        build_menu_page('game_menu','3-5',game_specs=[SimpleNamespace(game_id='test',display_title=title,available=True)])
