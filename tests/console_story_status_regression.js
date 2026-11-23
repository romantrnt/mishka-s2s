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
console.log('story status: authored/recorded playback, pause/resume, completion and new-session reset passed');
