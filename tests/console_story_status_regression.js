'use strict';
const assert = require('assert'), fs = require('fs'), vm = require('vm');
const source = fs.readFileSync('app/foundation/console/console.js', 'utf8');
const fields = Object.fromEntries(['story-id','story-playback','story-last-event','story-checkpoint','story-playback-badge'].map(id => [id, {}]));
const context = vm.createContext({$: id => fields[id]});
vm.runInContext(source.slice(source.indexOf('function renderStoryStatus('), source.indexOf('function eventSummary(')), context);
for (const [node, expected] of [['personal_story_theme','menu'],['personal_story_generating','preparing'],['personal_story_playing','playing'],['story_pause_announcement','pausing'],['story_paused','paused'],['story_resume_announcement','resuming'],['fixed_story_playing','playing'],['root_menu','inactive'],['session_closed','inactive']]) {
  context.renderStoryStatus({session:{node}, canonical_state:{active_story_id: node.includes('personal_story') ? 'PERSONAL_STORY' : undefined}});
  assert.strictEqual(fields['story-playback-badge'].textContent, expected.toUpperCase(), node);
}
assert.strictEqual(fields['story-id'].textContent, '—', 'new session must not retain the previous story');
for(const phase of ['accepted','preparing','playing']) {
 context.renderStoryStatus({session:{node:'personal_story_playing'},canonical_state:{story_audio_phase:phase}});
 assert.strictEqual(fields['story-playback-badge'].textContent,phase.toUpperCase());
}
for (const confirmed of [false, true]) {
 const paused={session:{node:'story_paused'},canonical_state:{expected_inputs:confirmed?['button_6','button_7','handset_down']:['button_7','handset_down']}};
 context.renderStoryStatus(paused);
 assert.strictEqual(fields['story-playback-badge'].textContent,'PAUSED');
 assert.strictEqual(fields['story-playback'].textContent,confirmed?'paused · resume available':'paused · checkpoint pending');
}
context.interactionActive=()=>true;
vm.runInContext(source.slice(source.indexOf('function interactionGuide('), source.indexOf('function enqueuePCM(')),context);
const unknown={session:{node:'story_paused'},canonical_state:{expected_inputs:['button_7','handset_down']}};
assert(context.interactionGuide(unknown,{})[1].includes('Место в сказке ещё не подтверждено'));
assert(context.interactionGuide(unknown,{})[1].includes('нажми 7'));
assert(!context.interactionGuide(unknown,{})[1].includes('6 — продолжить'));
const confirmed={session:{node:'story_paused'},canonical_state:{expected_inputs:['button_6','button_7','handset_down']}};
assert(context.interactionGuide(confirmed,{})[1].includes('6 — продолжить сказку; 7 — выйти'));
assert(!context.interactionGuide(confirmed,{})[1].includes('Дослушай'));

console.log('story status: authored/recorded playback, pause/resume, completion and new-session reset passed');
