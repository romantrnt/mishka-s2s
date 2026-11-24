'use strict';
const assert=require('assert'),fs=require('fs'),vm=require('vm');
const root=process.argv[2]||'app/foundation/console';
const source=fs.readFileSync(`${root}/console.js`,'utf8'),phone=fs.readFileSync(`${root}/phone.js`,'utf8');
// Reuse the existing actual console/phone VM fixture, without executing its suite.
const existing=fs.readFileSync('tests/console_phone_regression.js','utf8');
const fixtureContext={assert,vm,source,phone,console};
vm.runInNewContext(existing.slice(existing.indexOf('function snapshotFences(){'),existing.indexOf('function serverInstanceRecovery(){'))+';this.fixture=snapshotFences;',fixtureContext);
const tick=()=>new Promise(resolve=>setImmediate(resolve));
function setup(){
 const {context,fields,snapshot}=fixtureContext.fixture(),requests=[];let unlock,unlocks=0;
 Object.assign(context,{authBlocked:false,wsReloadRequired:false,wsSnapshotReady:true,current:null,recordLiveInput:()=>{},recordLiveResult:()=>{},AbortController,session:()=> 'stand'});
 context.unlockAudio=()=>{unlocks++;return new Promise(resolve=>unlock=resolve);};
 context.startMic=()=>{throw new Error('sound admission must not request a microphone');};
 context.fetchWithTimeout=(path,options)=>new Promise(resolve=>requests.push({path,body:JSON.parse(options.body),signal:options.signal,resolve}));
 vm.runInContext(source.slice(source.indexOf('const MAX_PENDING_POSTS ='),source.indexOf('function recordLiveInput(')),context);
 vm.runInContext(source.slice(source.indexOf('window.MishkaConsole =')),context);
 vm.runInContext(phone.slice(phone.indexOf('function mountPhone')),context);
 let cursor=0;
 const state=({epoch=30,version=1,sid='stand',quiet=true,node='awaiting_handset',active=true,contextGeneration=0}={})=>{
  const value=snapshot(epoch,++cursor,false,version);value.session.state=active?'ACTIVE':'IDLE';value.session.node=node;
  Object.assign(value.simulator,{instance_id:'d'.repeat(16)});
  Object.assign(value.simulator.session,{session_id:sid,context_generation:contextGeneration,node,interaction_active:active,interaction_route:active?'panel':null,session_actions:active?(quiet?['end','enable_audio']:['end']):['start']});
  value.canonical_state={mode:node,interaction_active:active,silent_mode:quiet};value.simulator.microphone.eligible=!quiet;return value;
 };
 const show=options=>{const value=state(options);context.render(value);return value;};
 return {context,fields,requests,state,show,unlock:value=>unlock(value),unlocks:()=>unlocks};
}
async function explicitSound(){
 const h=setup(),attributes={};h.fields['phone-sound'].setAttribute=(key,value)=>{attributes[key]=value;};h.show();const button=h.fields['phone-sound'];
 assert.strictEqual(button.textContent,'Включить звук сеанса','running local audio is not runtime sound admission');
 assert.strictEqual(attributes['aria-pressed'],'false');assert.match(h.fields['phone-media-status'].textContent,/Тихий режим/);
 assert.match(h.fields['phone-instruction'].textContent,/Включить звук.*поднять трубку.*7/);
 const enabling=button.onclick({detail:0});assert.strictEqual(h.unlocks(),1);assert.strictEqual(h.requests.length,0);
 h.unlock({state:'running'});await tick();
 assert.deepStrictEqual(h.requests[0].body,{action:'enable_audio',instance_id:'d'.repeat(16),epoch:30,session_id:'stand',context_generation:0,state_version:1});
 assert.strictEqual(h.requests[0].path,'/stand/simulator/action');
 h.requests[0].resolve({state:h.state({quiet:false,version:2,node:'game_active'})});await enabling;
 assert.strictEqual(h.context.current.session.handset_up,false,'sound never manufactures a handset edge');
 assert.strictEqual(button.textContent,'Звук включён');assert.strictEqual(attributes['aria-pressed'],'true');
 const locked=setup();locked.show();const failed=locked.fields['phone-sound'].onclick({detail:0});locked.unlock({state:'suspended'});await failed;
 assert.strictEqual(locked.requests.length,0);assert.match(locked.fields['phone-error'].textContent,/Браузер не разрешил звук/);
}
async function staleUnlock(){
 for(const mutation of ['version','visit','epoch','context','socket','disconnect','end']){
  const h=setup();h.show();const enabling=h.fields['phone-sound'].onclick({detail:0});let ending;
  if(mutation==='version')h.show({version:2});
  if(mutation==='visit')h.show({sid:'next'});
  if(mutation==='epoch')h.show({epoch:31});
  if(mutation==='context')h.show({contextGeneration:1});
  if(mutation==='socket')h.context.ws={readyState:1};
  if(mutation==='disconnect')h.context.ws.readyState=3;
  if(mutation==='end'){ending=h.fields['phone-end'].onclick({detail:0});assert.strictEqual(h.requests[0].body.action,'end');}
  h.unlock({state:'running'});await enabling;
  assert.strictEqual(h.requests.filter(r=>r.body.action==='enable_audio').length,0,`${mutation} revokes a pending sound unlock`);
  if(ending){h.requests[0].resolve({state:h.state({epoch:31,active:false})});await ending;}
 }
 const h=setup();h.show();const enabling=h.fields['phone-sound'].onclick({detail:0});h.unlock({state:'running'});await tick();
 const ending=h.fields['phone-end'].onclick({detail:0});assert.strictEqual(h.requests[0].signal.aborted,true,'End aborts submitted sound grant');
 h.requests[1].resolve({state:h.state({epoch:31,active:false})});await ending;
 h.requests[0].resolve({state:h.state({quiet:false})});await enabling;
 assert.strictEqual(h.context.current.simulator.epoch,31,'late audio response cannot revive the ended visit');
}
function quietDayHint(){
 const h=setup(),value=h.state({node:'schedule_weather'});value.canonical_state.pending_information_kind='date';h.context.render(value);
 assert.match(h.fields['phone-instruction'].textContent,/Включить звук.*подними трубку.*выбранный вопрос.*7/);
}
async function staleContextEnd(){
 const h=setup();h.show();const end=h.fields['phone-end'];end.onkeydown({code:'Enter',repeat:false});
 h.show({contextGeneration:1});await end.onclick({detail:0});
 assert.strictEqual(h.requests.length,0,'End captured before a fresh card cannot close the new child context');
 const currentEnd=end.onclick({detail:0});assert.strictEqual(h.requests[0].body.context_generation,1);
 h.requests[0].resolve({state:h.state({contextGeneration:1,active:false,epoch:31})});await currentEnd;
}
async function gestureAndQueue(){
 for(const mutation of ['version','visit','context','socket','cancel']){
  const h=setup();h.show();const button=h.fields['phone-sound'];button.onpointerdown({button:0});
  if(mutation==='context')h.show({contextGeneration:1});if(mutation==='version')h.show({version:2});if(mutation==='visit')h.show({sid:'next'});if(mutation==='socket')h.context.ws={readyState:1};if(mutation==='cancel')button.onpointercancel();
  await button.onclick({detail:1});assert.strictEqual(h.unlocks(),0,`${mutation} invalidates the captured media gesture`);assert.strictEqual(h.requests.length,0);
 }
 const h=setup();h.show();const held=h.context.post('/stand/text',{text:'synthetic queue blocker'});
 const enabling=h.fields['phone-sound'].onclick({detail:0});h.unlock({state:'running'});await tick();assert.strictEqual(h.requests.length,1);
 h.show({version:2});h.requests[0].resolve({});await held;await enabling;
 assert.strictEqual(h.requests.length,1,'actual FIFO revalidates exact state_version at dispatch');
 const global=setup();global.show();let pointerdown;global.context.window.addEventListener=(event,handler)=>{if(event==='pointerdown')pointerdown=handler;};
 vm.runInContext(source.split('\n').find(line=>line.startsWith("window.addEventListener('pointerdown'")),global.context);
 pointerdown();global.unlock({state:'running'});await tick();assert.strictEqual(global.requests.length,0,'global pointerdown unlock has no server sound grant');
 const local=setup();local.show({active:false});const onlyUnlock=local.fields['phone-sound'].onclick({detail:0});local.unlock({state:'running'});await onlyUnlock;
 assert.strictEqual(local.requests.length,0,'local media unlock without advertised action cannot enable a runtime session');
}
function physicalLighting(){
 const key={dataset:{},setAttribute(){}};const c=vm.createContext({keys:new Map([[2,key]]),channels:{2:[13,12]},state:{physical_lighting:{13:1,12:0}},simulator:null,model:null,input:{pressed:()=>new Set()},canMutate:()=>false});
 vm.runInContext(phone.slice(phone.indexOf('function renderMechanical('),phone.indexOf('function cleanup(')),c);c.renderMechanical();
 assert.strictEqual(key.dataset.fill,'true');assert.strictEqual(key.dataset.digit,'false');assert.strictEqual(key.disabled,true);
 c.state.physical_lighting={13:0,12:1};c.renderMechanical();assert.strictEqual(key.dataset.fill,'false');assert.strictEqual(key.dataset.digit,'true');
}
async function physicalObserver(){
 const h=setup();h.context.current=null;
 const value={session:{state:'IDLE',node:'root_menu',handset_up:false},transport:{hardware:{connected:true,transport_mode:'physical',acceptance:'physical'}},physical_lighting:{14:1,15:0},commands:[],latencies:[],results:[],errors:[],events:[],raw_frames:[],lighting:{}};
 h.context.render(value);
 assert.strictEqual(h.context.document.body.dataset.panelMode,'observer');
 assert.match(h.fields['phone-context'].textContent,/Физический телефон.*динамик платы/);
 assert.match(h.fields['phone-render-notice'].textContent,/Фактический свет.*на устройстве/);
 for(const id of ['phone-start','phone-end','phone-skip-interview'])assert.strictEqual(h.fields[id].hidden,true);
 for(const id of ['phone-handset','phone-reset','phone-sound','phone-microphone','phone-text'])assert.strictEqual(h.fields[id].disabled,true);
 await h.fields['handset-up'].onclick();assert.strictEqual(h.requests.length,0,'observer has no simulator command transport');
 h.context.fetch=()=>{throw new Error('physical observer cannot request the simulator journal');};
 await h.fields['phone-refresh-journal'].onclick();await h.fields['phone-sound'].onclick({detail:0});await h.fields['phone-reset'].onclick();assert.strictEqual(h.requests.length,0);
 vm.runInContext(source.slice(source.indexOf('function speakCommands('),source.indexOf('function interactionGuide(')),h.context);
 h.context.speakCommands({...value,commands:[{type:'PlayAudio',payload:{}}]});
 vm.runInContext(source.slice(source.indexOf('function microphoneEligible('),source.indexOf('function microphoneAccepting(')),h.context);
 assert.strictEqual(h.context.microphoneEligible(),false,'physical observer never admits the browser microphone');
 const paths=[];Object.assign(h.context,{refreshInFlight:null,lastHealthRefresh:0,HEALTH_REFRESH_MS:1000,healthSnapshot:null,AbortController,refreshAbortController:null,fetchWithTimeout:async path=>{paths.push(path);return {json:async()=>value};}});
 vm.runInContext(source.slice(source.indexOf('async function refresh('),source.indexOf('function stopPolling(')),h.context);
 await h.context.refresh({includeStatus:false});assert.deepStrictEqual(paths,['/stand/state'],'phone polling uses the supported public snapshot in physical mode');
}
(async()=>{quietDayHint();await staleContextEnd();await explicitSound();physicalLighting();await staleUnlock();await gestureAndQueue();await physicalObserver();console.log('panel audio: explicit media gesture, exact owner/version, End, reconnect, stale FIFO and honest physical observer passed');})().catch(error=>{console.error(error);process.exitCode=1;});
