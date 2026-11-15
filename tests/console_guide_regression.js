'use strict';
const assert=require('assert'), fs=require('fs'), vm=require('vm');
const source=fs.readFileSync(process.argv[2],'utf8');
const start=source.indexOf('function updateGuide('), end=source.indexOf('function enqueuePCM(',start);
assert(start>=0&&end>start);
const fields={}, context=vm.createContext({$:id=>(fields[id] ||= {}), document:{querySelector:()=>({hidden:false})}, buttonLegendSummary:()=>''});
vm.runInContext(source.slice(start,end),context);
function guide(mode,substate='',up=true) {
 context.updateGuide({canonical_state:{mode,substate},session:{node:mode,state:'ACTIVE',handset_up:up}},{});
 return [fields['guide-title'].textContent,fields['guide-text'].textContent];
}
assert(!guide('game_active')[0].includes('5'));
assert(guide('game_active','game_exit_confirmation')[1].includes('6 — продолжить; 7 — закончить'));
assert(guide('game_menu')[1].includes('5 — помощь'));
assert(!guide('fixed_story_playing')[1].includes('Трубка вниз — пауза'));
assert(guide('fixed_story_menu')[1].includes('Про Бегемота, который боялся прививок'));
assert(guide('fixed_story_menu')[1].includes('Как бельчонок страх преодолел'));
assert(guide('schedule_weather')[1].includes('4 — куда идти'));
assert(guide('wellbeing_check','',false)[1].includes('Поднимите трубку'));
console.log('console guide regression: 8 assertions passed');
