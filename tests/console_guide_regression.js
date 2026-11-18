'use strict';
const assert=require('assert'), fs=require('fs'), vm=require('vm');
const source=fs.readFileSync(process.argv[2],'utf8');
const start=source.indexOf('function interactionGuide('), end=source.indexOf('function enqueuePCM(',start);
assert(start>=0&&end>start);
const fields={}, context=vm.createContext({mic:null,micStarting:false,audioRetry:null,microphoneAccepting:()=>false,$:id=>(fields[id] ||= {}), document:{querySelector:()=>({hidden:false})}, buttonLegendSummary:()=>''});
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
assert(guide('wellbeing_check','',false)[1].includes('Поднимите трубку'));
assert(!guide('schedule_weather')[1].includes('4 — куда идти'));
assert(!guide('game_menu')[1].includes('Повтори паттерн'));
assert(!guide('fixed_story_menu')[1].includes('Прививок'));
console.log('console guide regression: 11 assertions passed');
