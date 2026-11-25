'use strict';
const assert=require('assert'),fs=require('fs'),vm=require('vm');
const source=fs.readFileSync('app/foundation/console/console.js','utf8');
const context=vm.createContext({interactionActive:()=>true});
vm.runInContext(source.slice(source.indexOf('function interactionGuide('),source.indexOf('function updateGuide(')),context);
const state={session:{state:'ACTIVE',node:'exhibition_name_capture'},simulator:{},canonical_state:{expected_inputs:['speech','button_5','button_7']}};
const guide=()=>context.interactionGuide(state,{microphoneAccepting:true});
assert.doesNotMatch(guide()[1],/1 —/,'ordinary name capture does not invent confirmation');
state.canonical_state.expected_inputs=['speech','button_1','button_5','button_7'];
for(const mic of [true,false]){
 const hint=context.interactionGuide(state,{microphoneAccepting:mic});
 assert.match(hint[1],/1 — верно/,'accepted confirmation is available without voice');
 assert.match(hint[1],/7 — пропустить/);
 assert.match(hint[1],/5 — помощь/);
}
state.canonical_state.expected_inputs=['button_5','button_7','audio_completed'];
state.simulator.prompt={phrase_id:'NAME_CONFIRMATION'};
assert.match(guide()[1],/Дослушай/);
assert.match(guide()[1],/7/);
assert.doesNotMatch(guide()[1],/1 —/,'confirmation cannot precede the owned terminal');
delete state.simulator.prompt;
assert.doesNotMatch(guide()[1],/1 —/,'new ordinary name phase does not retain the previous mapping');
console.log('name capture, speaking, ready confirmation, no-microphone buttons and phase reset guides passed');
