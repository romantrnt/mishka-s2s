'use strict';
const assert=require('assert'),fs=require('fs'),vm=require('vm');
const source=fs.readFileSync(process.argv[2]||'app/foundation/console/console.js','utf8');
const context=vm.createContext({});
vm.runInContext(source.slice(source.indexOf('function interactionActive('),source.indexOf('function sessionActionIsCurrent(')),context);
vm.runInContext(source.slice(source.indexOf('function interactionGuide('),source.indexOf('function updateGuide(')),context);
for(let round=0;round<8;round++){
 const state={session:{node:'game_active',state:'ACTIVE',handset_up:true},canonical_state:{active_game_id:'quiz',game_audio_stage:'playing',game_round:round}};
 const [title,hint]=context.interactionGuide(state,{});
 assert.strictEqual(title,'Вопросы Мишки');
 assert(hint.startsWith(`Вопрос ${round+1} из 8.`),'the bounded zero-based panel round reaches the visible guide');
 assert(hint.includes('Для ответа 7 коротко нажми и отпусти.'));
}
console.log('quiz guide displays every projected round 1–8 and retains short-seven guidance');
