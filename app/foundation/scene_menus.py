"""Canonical chooser ownership; menu text is derived only from public catalogues."""
from .menu_content import build_menu_page

MENU_PCM_MODES = {'MENU_FIXED_STORY': 'fixed_story_menu', 'MENU_GAME': 'game_menu', 'MENU_STORY_THEMES': 'personal_story_theme', 'MENU_DAY': 'schedule_weather'}
SCENE_AUDIO = {mode: audio for audio, mode in MENU_PCM_MODES.items()}

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
            if state.mode == 'game_menu':
                # Randomness belongs to an entry, never descriptor rendering.
                import uuid
                state.game_entry_seed = uuid.uuid4().int & ((1 << 63) - 1)
            state.scene_menu_active = state.mode
            state.scene_menu_phase = 'ready'
            if state.selected_menu_button is not None and not state.scene_menu_held:
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
        if (state.mode == 'game_menu' and state.scene_menu_phase == 'ready') or state.scene_menu_phase == 'repeat_listen' or (state.mode in {'personal_story_theme','schedule_weather'}
                and state.scene_menu_phase=='ready' and page.items): state.expected_inputs.append('speech')
        if entering and state.mode == 'game_menu' and state.handset == 'up':
            state.scene_menu_phase = 'invite'
            effects.extend([{'type':'capture_close'}, {'type':'speak','phrase_id':'PHRASE_019'}])
        if (state.handset == 'up' and getattr(self,'menu_voice_capable',False)
                and state.scene_menu_phase != 'invite'
                and (state.mode not in state.scene_menu_seen or reason in {'scene_menu_page','scene_menu_refresh'})
                and (entering or changed or reason in {'voice_open_activity','typed_menu_after_handset','scene_menu_page','scene_menu_refresh','personal_story_failure_finished'})
                and not any(e.get('type') == 'speak' for e in effects)):
            state.scene_menu_seen.append(state.mode) if state.mode not in state.scene_menu_seen else None
            state.scene_menu_phase = 'narrating'
            effects.extend([{'type':'capture_close'}, {'type':'narrate_scene_menu',
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
            capture = 'capture_open' if state.mode == 'game_menu' and state.handset == 'up' else 'capture_close'
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

    def _scene_menu_answer(self,state,intent,text,verified):
        if state.mode not in SCENE_AUDIO:return None
        if state.scene_menu_phase in {'narrating','repeat_prompt'}:
            return self._decision(state,[],'scene_menu_not_listening')
        if state.scene_menu_refresh_required:
            return self._decision(state,[],'scene_menu_refresh_required')
        if state.scene_menu_phase!='repeat_listen':return None
        if not verified:return self._decision(state,[],'scene_menu_answer_unverified')
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
