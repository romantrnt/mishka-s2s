'use strict';
const assert=require('assert'),fs=require('fs'),vm=require('vm');
const source=fs.readFileSync('app/foundation/console/console.js','utf8');
const context=vm.createContext({interactionActive:()=>true});
vm.runInContext(source.slice(source.indexOf('function interactionGuide('),source.indexOf('function updateGuide(')),context);
for(const up of [false,true])for(const severity of ['concern','urgent']){
 const state={session:{state:'ACTIVE',node:'safety_stop',handset_up:up},canonical_state:{safety_severity:severity},simulator:{session:{session_actions:['end']}}};
 const hint=context.interactionGuide(state,{});
 assert.strictEqual(hint[0],'Позови взрослого');
 assert.match(hint[1],/^Позови взрослого рядом\./,'adult assistance stays first');
 assert.match(hint[1],/Взрослый может нажать «Завершить»/,'the available panel End is offered to the adult');
 assert.doesNotMatch(hint[1],/Положи трубку/,'panel End does not demand an already-DOWN handset');
 state.simulator.session.session_actions=[];
 assert.strictEqual(context.interactionGuide(state,{})[1],'Позови взрослого рядом. Положи трубку, чтобы завершить разговор.');
 delete state.simulator;state.transport={hardware:{acceptance:'physical'}};
 assert.match(context.interactionGuide(state,{})[1],/Управляйте трубкой и кнопками на устройстве/);
}
console.log('safety guide keeps adult priority, offers admitted panel End for DOWN/UP and preserves physical/no-End fallback');
