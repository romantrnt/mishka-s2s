'use strict';
const assert=require('assert'),fs=require('fs'),vm=require('vm');
const source=fs.readFileSync('app/foundation/console/console.js','utf8');
const context=vm.createContext({interactionActive:()=>false});
vm.runInContext(source.slice(source.indexOf('function interactionGuide('),source.indexOf('function updateGuide(')),context);
for(const up of [false,true]){
 const state={session:{state:'ENDED',node:'session_closed',handset_up:up},simulator:{session:{session_actions:['start']}}};
 const hint=context.interactionGuide(state,{});
 assert.match(hint[1],/Нажми «Начать»/,'ended '+(up?'UP':'DOWN')+' explicitly offers its advertised Start action');
 if(up)assert.doesNotMatch(hint[1],/без звука/,'UP Start does not promise silent operation');
 else {
  assert.match(hint[1],/без звука/);
  assert.match(hint[1],/держи 6 пять секунд/);
  assert.match(hint[1],/подними трубку/);
 }
 state.simulator.session.session_actions=[];
 const fallback=context.interactionGuide(state,{});
 assert.strictEqual(fallback[1],up?'Сеанс завершён. Положи трубку, чтобы начать заново.':'Поднимите трубку','no Start admission retains the existing handset fallback');
 const physical={session:state.session,transport:{hardware:{acceptance:'physical'}}};
 assert.match(context.interactionGuide(physical,{})[1],/Управляйте трубкой и кнопками на устройстве/,'physical observer hint is preserved');
}
console.log('post-End DOWN/UP guides follow actual Start availability and preserve no-Start/physical fallbacks');
