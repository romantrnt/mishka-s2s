'use strict';
const assert=require('assert'), fs=require('fs'), vm=require('vm');
const source=fs.readFileSync(process.argv[2],'utf8');
const start=source.indexOf('function interactionGuide('), end=source.indexOf('function enqueuePCM(',start);
assert(start>=0&&end>start);
const fields={}, context=vm.createContext({mic:null,micStarting:false,audioRetry:null,microphoneAccepting:()=>false,$:id=>(fields[id] ||= {}), document:{querySelector:()=>({hidden:false})}, buttonLegendSummary:()=>''});
vm.runInContext(source.slice(source.indexOf('function interactionActive('),source.indexOf('function sessionActionIsCurrent(')),context);
vm.runInContext(source.slice(start,end),context);
function guide(mode,substate='',up=true,page=null) {
 context.updateGuide({canonical_state:{mode,substate,scene_menu_descriptor:page},session:{node:mode,state:'ACTIVE',handset_up:up}},{});
 return [fields['guide-title'].textContent,fields['guide-text'].textContent];
}
const page=(scene,items,buttons=[1,2,5,6,7])=>({scene,items,buttons});
assert(!guide('game_active')[0].includes('5'));
assert(guide('game_active','game_exit_confirmation')[1].includes('6 — продолжить; 7 — закончить'));
assert(guide('game_menu','',true,page('game_menu',[]))[1].includes('5 — помощь'));
assert(!guide('fixed_story_playing')[1].includes('Трубка вниз — пауза'));
const stories=page('fixed_story_menu',[
 {slot:1,id:'synthetic-fourth',title:'Про Бегемота, который боялся прививок'},
 {slot:2,id:'synthetic-fifth',title:'Как бельчонок страх преодолел'}]);
assert(guide('fixed_story_menu','',true,stories)[1].includes('1 — Про Бегемота, который боялся прививок'));
assert(guide('fixed_story_menu','',true,stories)[1].includes('2 — Как бельчонок страх преодолел'));
const day=page('schedule_weather',[{slot:1,id:'where_now',title:'Текущее занятие'}],[1,5,7]);
assert(guide('schedule_weather','',true,day)[1].includes('1 — Текущее занятие'));
assert(guide('wellbeing_check','',false)[1].includes('7 — нужна помощь взрослого'));
context.updateGuide({canonical_state:{mode:'safety_stop',support_alert:{kind:'help_requested',message:'PRIVATE_INJECTED_MESSAGE',adult_acknowledged:true}},session:{node:'safety_stop',state:'ACTIVE'}},{});
assert(fields['phone-support-alert'].hidden === false);
assert(fields['phone-support-alert'].textContent.includes('Подтверждение взрослого не получено'));
assert(!fields['phone-support-alert'].textContent.includes('PRIVATE_'));
guide('root_menu');
assert(fields['phone-support-alert'].hidden === true && fields['phone-support-alert'].textContent === '');
assert(!guide('schedule_weather')[1].includes('4 — куда идти'));
assert(!guide('game_menu')[1].includes('Повтори паттерн'));
assert(!guide('fixed_story_menu')[1].includes('Прививок'));
const media = {microphone: false, pending: false, microphoneAccepting: false};
const offered = ready => ({session:{node:'bear_dialogue',handset_up:true},
 canonical_state:{pending_game_offer:'quiz',game_offer_ready:ready}});
assert(context.interactionGuide(offered(false),media)[1].includes('Дослушай предложение'));
assert(!context.interactionGuide(offered(false),media)[1].includes('1 — начать'));
assert(context.interactionGuide(offered(true),media)[0].includes('Вопросы Мишки'));
assert(context.interactionGuide(offered(true),media)[1].includes('1 — начать; 7 — продолжить разговор'));
for(const stage of ['prompt','playing']) {
 const quiz={session:{node:'game_active',handset_up:true},canonical_state:{active_game_id:'quiz',game_audio_stage:stage}};
 assert(context.interactionGuide(quiz,media)[1].includes('держи 7 до подсказки «Теперь отпускай», затем отпусти'));
}
console.log('console guide regression: contextual offer, nonvoice exit and existing guidance passed');

for (const active of [true,false]) {
 context.updateGuide({canonical_state:{mode:'wellbeing_check',support_question:'fear',interaction_active:active},session:{node:'wellbeing_check',state:'ACTIVE',handset_up:false}},{});
 assert(fields['guide-text'].textContent.includes('Тебе страшно?'));
 assert(fields['guide-text'].textContent.includes('7 — нужна помощь взрослого'));
}
for (const node of ['exhibition_name_question', 'exhibition_name_capture']) {
 for (const pending of [false, true]) {
  const text = context.interactionGuide({session:{node,handset_up:true}}, {...media,pending})[1];
  assert(text.includes('7') && text.includes('пропустить'), 'name skip stays discoverable without microphone');
 }
}
for (const node of ['exhibition_age_prompt', 'exhibition_age_hint', 'exhibition_age_selection']) {
 const text = context.interactionGuide({session:{node,handset_up:true}}, media)[1];
 assert(text.includes('1') && text.includes('7'), 'age choices are announced during speech too');
 assert(!text.includes('Дослушай'), 'the guide must not claim safe buttons are unavailable');
}
