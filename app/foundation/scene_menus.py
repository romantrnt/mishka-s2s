"""Canonical chooser ownership; menu text is derived only from public catalogues."""
import re
from .menu_content import build_menu_page

MENU_PCM_MODES = {'MENU_FIXED_STORY': 'fixed_story_menu', 'MENU_GAME': 'game_menu', 'MENU_STORY_THEMES': 'personal_story_theme', 'MENU_DAY': 'schedule_weather'}
SCENE_AUDIO = {mode: audio for audio, mode in MENU_PCM_MODES.items()}
CATALOGUE_CLARIFY = 'CATALOGUE_CLARIFY'
CATALOGUE_CLARIFY_TEXT = 'Выбери один вариант из меню. Скажи название или нажми кнопку.'

class SceneMenuMixin:
    def _menu_page(self, state):
        from .canonical_contract import IMMERSIVE_GAME_SPECS
        return build_menu_page(state.mode, state.age_band,
            state.scene_menu_pages.get(state.mode, 0), stories=self.stories,
            game_specs=IMMERSIVE_GAME_SPECS,
            personal_story_available=getattr(self,'personal_story_capable',False),
            day_items=getattr(self,'day_menu_items',lambda sid:(('date','Дата'),))(state.session_id))

    def _menu_selection(self, state, button):
        page = self._menu_page(state)
        if state.scene_menu_descriptor != page.as_dict():
            return None
        action = page.action_for(button)
        return action[1] if action and action[0] == 'select' else None

    def _prepare_scene_menu(self, state, effects, reason):
        if (state.scene_menu_active in SCENE_AUDIO and
                (state.mode != state.scene_menu_active or reason in {
                    'typed_info_requested', 'schedule_requires_profile', 'typed_information_unavailable'})):
            effects = [{'type':kind} for kind in ('audio_stop','capture_close')
                       if not any(effect.get('type') == kind for effect in effects)] + effects
        if state.mode not in SCENE_AUDIO:
            if state.scene_menu_refresh_required and state.scene_menu_active in state.scene_menu_seen:
                state.scene_menu_seen.remove(state.scene_menu_active)
            state.scene_menu_refresh_required = False
            state.scene_menu_active = None
            state.scene_menu_phase = 'idle'
            state.scene_menu_descriptor = {}
            return effects
        entering = state.scene_menu_active != state.mode
        redraw = entering or reason == 'scene_menu_page'
        if entering:
            state.scene_menu_generation += 1
            if state.mode == 'game_menu':
                # Randomness belongs to an entry, never descriptor rendering.
                import uuid
                state.game_entry_seed = uuid.uuid4().int & ((1 << 63) - 1)
            state.scene_menu_active = state.mode
            state.scene_menu_phase = 'ready'
            if (state.selected_menu_button is not None and not state.scene_menu_held
                    and state.selected_menu_button in getattr(state, "accepted_buttons_down", ())):
                state.scene_menu_held = [state.selected_menu_button]
        page = self._menu_page(state)
        changed = (not entering and reason != 'scene_menu_page'
                   and bool(state.scene_menu_descriptor)
                   and state.scene_menu_descriptor != page.as_dict())
        redraw = redraw or state.scene_menu_descriptor != page.as_dict()
        if changed:
            # Revoke the old spoken mapping, not just its visual projection.
            # The triggering edge is consumed; a fresh edge may interrupt the
            # replacement narration but cannot silently select its new slot.
            state.state_version += 1
            state.scene_menu_refresh_required = True
            state.scene_menu_phase = 'ready'
            if state.mode in state.scene_menu_seen:
                state.scene_menu_seen.remove(state.mode)
            effects = [e for e in effects if e.get('type') not in
                       {'narrate_scene_menu', 'typed_info_request', 'speak', 'capture_open'}]
            effects = [{'type':'audio_stop'}, {'type':'capture_close'}] + effects
        state.scene_menu_descriptor = page.as_dict()
        # Legacy recordings and broad presets must not contradict this page.
        effects = [e for e in effects if e.get('phrase_id') not in {'PHRASE_054','PHRASE_055','PHRASE_075'}
            and not (e.get('type')=='typed_info_request' and e.get('kind')=='menu')
            and not (e.get('type') == 'lighting_preset' and e.get('preset') in {'GAME_MENU','STORY_MENU','PERSONAL_STORY','SCHEDULE_WEATHER'})]
        if redraw:
            effects.append({'type':'scene_menu_page','descriptor':page.as_dict()})
        state.expected_inputs = ['button_'+str(b) for b in page.buttons] + ['handset_down']
        if (state.mode in {'game_menu','fixed_story_menu'} and page.items) or state.scene_menu_phase == 'repeat_listen' or (state.mode in {'personal_story_theme','schedule_weather'}
                and state.scene_menu_phase=='ready' and page.items): state.expected_inputs.append('speech')
        if entering and state.mode == 'game_menu' and state.audio_open:
            state.scene_menu_phase = 'invite'
            effects.extend([{'type':'capture_open'}, {'type':'speak','phrase_id':'PHRASE_019'}])
        if (state.audio_open and getattr(self,'menu_voice_capable',False)
                and state.scene_menu_phase != 'invite'
                and (state.mode not in state.scene_menu_seen or reason in {'scene_menu_page','scene_menu_refresh'})
                and (entering or changed or reason in {'voice_open_activity','typed_menu_after_handset','scene_menu_page','scene_menu_refresh','personal_story_failure_finished'})
                and not any(e.get('type') == 'speak' for e in effects)):
            state.scene_menu_seen.append(state.mode) if state.mode not in state.scene_menu_seen else None
            state.scene_menu_phase = 'narrating'
            effects.extend([{'type':'capture_open' if state.mode in {'game_menu','fixed_story_menu'} else 'capture_close'}, {'type':'narrate_scene_menu',
                'content_id':SCENE_AUDIO[state.mode], 'scene':state.mode}])
        return effects

    def _scene_menu_button(self,state,event):
        button=int(event.payload['button_id'])
        if event.event_type == 'ButtonReleased':
            if button in state.scene_menu_held: state.scene_menu_held.remove(button)
            return None
        if state.mode not in SCENE_AUDIO: return None
        if button == 5 and state.mode != 'game_menu':
            if event.event_type == 'ButtonPressed' and state.scene_menu_phase in {'invite','narrating','repeat_prompt','repeat_listen'}:
                state.scene_menu_answers[state.mode]='skipped'
                state.scene_menu_phase='ready'
            return None
        if event.event_type != 'ButtonPressed':
            return self._decision(state, [], 'scene_menu_hold_ignored')
        if button in state.scene_menu_held:
            return self._decision(state, [], 'scene_menu_release_required')
        state.scene_menu_held.append(button)
        if state.scene_menu_phase in {'invite','narrating','repeat_prompt','repeat_listen'}:
            page = self._menu_page(state)
            action = page.action_for(button)
            stable_back = (action == ('back', None) and state.scene_menu_descriptor.get('scene') == state.mode
                           and button in state.scene_menu_descriptor.get('buttons', ()))
            if state.scene_menu_descriptor != page.as_dict() and not stable_back:
                action = None
            if action and (action[0] == 'back' or not state.scene_menu_refresh_required):
                state.scene_menu_answers[state.mode]='skipped'
                state.scene_menu_phase='ready'
                if action[0] != 'next_page':return None
                # Paging below owns its replacement narration on this edge.
            else:
                state.scene_menu_phase='ready'
                state.scene_menu_answers[state.mode]='skipped'
                state.state_version+=1
                return self._decision(state,[{'type':'audio_stop'},{'type':'capture_close'}] +
                    ([{'type':'capture_open'}] if state.mode=='game_menu' else []),
                    'scene_menu_skipped',clear_audio=True)
        if state.scene_menu_refresh_required and button in ((1,2,3,4,5) if state.mode == 'game_menu' else (1,2,3,4)):
            return self._decision(state,[{'type':'audio_stop'},{'type':'capture_close'}],
                                  'scene_menu_refresh',clear_audio=True)
        if button==6:
            page=self._menu_page(state)
            if page.page_count<=1:return self._decision(state,[],'scene_menu_no_next_page')
            state.scene_menu_pages[state.mode]=(page.page+1)%page.page_count
            state.state_version+=1
            return self._decision(state,[{'type':'audio_stop'}],'scene_menu_page',clear_audio=True)
        return None

    def _scene_menu_audio(self,state,event,response_id):
        if state.mode not in SCENE_AUDIO:return None
        if response_id == CATALOGUE_CLARIFY:
            if (state.mode not in {'game_menu','fixed_story_menu'}
                    or state.active_audio_id != response_id or not state.playback_lease
                    or event.payload.get('playback_lease') != state.playback_lease):
                return self._decision(state, [], 'scene_menu_stale_audio')
            state.state_version += 1
            state.audio_status = 'failed' if event.event_type == 'AudioFailed' else 'finished'
            return self._decision(state, [{'type':'capture_open'}],
                                  'scene_menu_clarification_finished', clear_audio=True)
        if state.scene_menu_phase not in {'invite','narrating','repeat_prompt'}:return None
        expected=('PHRASE_019' if state.scene_menu_phase=='invite' else
                  SCENE_AUDIO[state.mode] if state.scene_menu_phase=='narrating' else 'PHRASE_009')
        if (response_id!=expected or not state.active_audio_id
                or not event.payload.get('playback_lease')
                or event.payload['playback_lease']!=state.playback_lease):
            return self._decision(state,[],'scene_menu_stale_audio')
        state.state_version+=1
        if event.event_type=='AudioFailed':
            state.scene_menu_phase='ready'
            state.scene_menu_answers[state.mode]='failed'
            state.audio_status='failed'
            capture = 'capture_open' if state.mode == 'game_menu' and state.interaction_open else 'capture_close'
            return self._decision(state,[{'type':capture}],'scene_menu_audio_failed',clear_audio=True)
        state.audio_status='finished'
        if state.scene_menu_phase=='invite':
            state.scene_menu_phase='narrating'
            if state.mode not in state.scene_menu_seen: state.scene_menu_seen.append(state.mode)
            return self._decision(state,[{'type':'narrate_scene_menu',
                'content_id':SCENE_AUDIO[state.mode], 'scene':state.mode}],
                'game_menu_invitation_finished',clear_audio=True)
        if state.scene_menu_phase=='narrating':
            state.scene_menu_refresh_required=False
        if state.scene_menu_phase=='narrating' and state.mode not in state.scene_menu_answers:
            state.scene_menu_phase='repeat_prompt'
            return self._decision(state,[{'type':'speak','phrase_id':'PHRASE_009'}],'scene_menu_repeat_question')
        if state.scene_menu_phase=='repeat_prompt':
            state.scene_menu_phase='repeat_listen'
            return self._decision(state,[{'type':'capture_open'}],'scene_menu_repeat_listen',clear_audio=True)
        state.scene_menu_phase='ready'
        return self._decision(state,[{'type':'capture_open'}] if state.mode=='game_menu' else [],
                              'scene_menu_finished',clear_audio=True)

    def _scene_menu_answer(self,state,intent,text,verified,admitted_descriptor=None):
        if state.mode not in SCENE_AUDIO:return None
        if state.mode in {'game_menu','fixed_story_menu'} and verified and not state.scene_menu_refresh_required:
            from .menu_content import catalogue_choice
            from .game_engine import IMMERSIVE_GAME_SPECS
            named = catalogue_choice(text, state.mode, state.age_band, stories=self.stories, game_specs=IMMERSIVE_GAME_SPECS)
            selected = named
            if (named is None and admitted_descriptor != state.scene_menu_descriptor
                    and catalogue_choice(text, state.mode, state.age_band, stories=self.stories,
                        game_specs=IMMERSIVE_GAME_SPECS, descriptor=admitted_descriptor)):
                return self._decision(state, [], 'scene_menu_choice_stale')
            if selected is None and admitted_descriptor == state.scene_menu_descriptor:
                selected = catalogue_choice(text, state.mode, state.age_band, stories=self.stories,
                    game_specs=IMMERSIVE_GAME_SPECS, descriptor=admitted_descriptor)
            if selected:
                state.scene_menu_answers[state.mode] = 'skipped'
                state.scene_menu_phase = 'ready'
                return (self._start_game(state, selected, self.monotonic_now()) if state.mode == 'game_menu'
                        else self._start_fixed_story(state, selected))
            if str(text or '').casefold().strip(' .!?') in {'назад','отмена','вернись назад','выйти'}:
                return self._activity_back(state, 'scene_menu_back_by_voice')
            if re.fullmatch(r'(?:я )?(?:ничего не хочу|не хочу (?:играть|поиграть|игру|игры|сказку|сказки|слушать сказку|слушать сказки))',
                    str(text or '').casefold().strip(' .!?…')):
                return self._activity_back(state, 'scene_menu_declined')
            # A title containing "хочу" is not consent to repeat the menu.
            repeat_atom = r'(?:да|давай|ага|угу|конечно|хорошо|ладно|хочу|повтори|повторить|ещ[её] раз|пожалуйста|спасибо|нет|неа|не хочу|не надо|не нужно|не повторяй|достаточно|понятно)'
            repeat = re.fullmatch(repeat_atom + rf'(?:[\s,]+{repeat_atom}){{0,5}}',
                str(text or '').casefold().strip(' .!?…'))
            if not repeat:
                if state.active_audio_id == CATALOGUE_CLARIFY:
                    return self._decision(state, [], 'scene_menu_choice_clarification_waiting')
                state.scene_menu_answers[state.mode] = 'skipped'
                state.scene_menu_phase = 'ready'
                state.state_version += 1
                return self._decision(state, [{'type':'audio_stop'}, {'type':'capture_close'}, {'type':'capture_open'},
                    self._dialogue_prompt(CATALOGUE_CLARIFY, CATALOGUE_CLARIFY_TEXT)],
                    'scene_menu_choice_clarification', clear_audio=True)
        if state.scene_menu_phase in {'narrating','repeat_prompt'}:
            return self._decision(state,[],'scene_menu_not_listening')
        if state.scene_menu_refresh_required:
            return self._decision(state,[],'scene_menu_refresh_required')
        if state.scene_menu_phase!='repeat_listen':return None
        if not verified:return self._decision(state,[],'scene_menu_answer_unverified')
        from .canonical_services import day_information_intent
        from .personal_story import story_theme
        request = ((state.mode == 'schedule_weather' and day_information_intent(text or '') is not None)
                   or (state.mode == 'personal_story_theme' and getattr(self,'personal_story_capable',False)
                       and story_theme(text) is not None))
        if request:
            state.scene_menu_answers[state.mode]='skipped'
            state.scene_menu_phase='ready'
            state.state_version+=1
            return None
        from .canonical_contract import normalize_exhibition_repeat_answer
        answer=normalize_exhibition_repeat_answer(text or '') or (intent if intent in {'yes','no'} else None)
        if answer is None:return self._decision(state,[],'scene_menu_answer_unknown')
        state.scene_menu_answers[state.mode]=answer
        state.scene_menu_phase='narrating' if answer=='yes' else 'ready'
        state.state_version+=1
        effects=[{'type':'capture_close'}]
        if answer=='yes':effects.append({'type':'narrate_scene_menu','content_id':SCENE_AUDIO[state.mode],'scene':state.mode})
        elif state.mode=='game_menu':effects.append({'type':'capture_open'})
        return self._decision(state,effects,'scene_menu_repeat_'+answer,clear_audio=True)
