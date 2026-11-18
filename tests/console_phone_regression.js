'use strict';
const assert=require('assert'),fs=require('fs'),vm=require('vm');
const root=process.argv[2]||'app/foundation/console';
const phone=fs.readFileSync(root+'/phone.js','utf8'),source=fs.readFileSync(root+'/console.js','utf8');
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const micState=(epoch=3,id='owner')=>({session:{handset_up:true,state:'ACTIVE'},simulator:{epoch,session:{session_id:id},admission:{input_ready:true,connected:true},microphone:{eligible:true}}});
function lightingProjection(){
 const modelSource=fs.readFileSync(root+'/phone-model.js','utf8');
 const css=fs.readFileSync(root+'/phone.css','utf8'),off='#53626a',on='#ffffff';
 const declarations=selector=>{
  const escaped=selector.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
  const rule=css.match(new RegExp(escaped+'\\s*\\{([^}]+)\\}'));
  assert(rule,'indicator CSS rule exists: '+selector);
  return Object.fromEntries(rule[1].split(';').filter(value=>value.includes(':')).map(value=>{const split=value.indexOf(':');return[value.slice(0,split).trim(),value.slice(split+1).trim()];}));
 };
 const flatOff=declarations('body[data-phone] .phone-key').color;
 const flatDigitOn=declarations('body[data-phone] .phone-key[data-digit="true"] span').color;
 const flatSymbolOn=declarations('body[data-phone] .phone-key[data-symbol="true"] svg').color;
 assert.strictEqual(flatOff,off,'flat OFF indicators are gray rather than luminous white');
 assert.strictEqual(flatDigitOn,on);assert.strictEqual(flatSymbolOn,on);
 for(const name of ['labelMaterial','symbolMaterial'])assert.match(modelSource,new RegExp(name+"=new THREE\\.MeshBasicMaterial\\([^\\n]+color:'"+off+"'"),'initial 3D '+name+' is dim before the first state');
 // Pin the physical contract independently of either browser renderer:
 // button 2 uses channel 13 for its orange fill and 12 for its white digit.
 const expected={1:[14,15],2:[13,12],3:[10,11],4:[9,8],5:[6,7],6:[3,2,4,5],7:[1,0,17,16]};
 const extract=(text,pattern)=>JSON.parse(JSON.stringify(vm.runInNewContext('('+text.match(pattern)[1]+')')));
 const flat=extract(phone,/const channels=(\{[^;]+\});/);
 const three=extract(modelSource,/const CHANNELS = (\{[^;]+\});/);
 assert.deepStrictEqual(flat,expected,'flat phone must follow physical fill/digit channels');
 assert.deepStrictEqual(three,expected,'3D phone must follow physical fill/digit channels');
 const material=()=>({color:{set(value){this.value=value;}},emissiveIntensity:0});
 const keys=new Map(Object.keys(expected).map(id=>[Number(id),{color:'enabled-'+id,material:material(),labelMaterial:material(),symbolMaterial:material()}]));
 const start=modelSource.indexOf('update(simulator,localPressed=new Set())'),end=modelSource.indexOf('destroy()',start);
 const model=vm.runInNewContext('({'+modelSource.slice(start,end)+'})',{keys,CHANNELS:three,Set,up:false});
 const snapshot={devices:{handset:{state:'up'},buttons:[],lighting:{14:1,13:1,10:1,9:1,6:1,3:1,1:1,5:1,16:1}}};
 model.update(snapshot);
 for(const [id,key] of keys){assert.strictEqual(key.material.color.value,'enabled-'+id,'root menu fill remains colored for button '+id);assert.strictEqual(key.material.emissiveIntensity,.32);}
 assert.strictEqual(keys.get(2).labelMaterial.color.value,off,'unlit digit is independent of lit orange fill');
 snapshot.devices.lighting={12:1};model.update(snapshot);
 assert.strictEqual(keys.get(2).material.color.value,'#8b8b8b','white digit alone never lights the orange fill');
 assert.strictEqual(keys.get(2).material.emissiveIntensity,0);
 assert.strictEqual(keys.get(2).labelMaterial.color.value,'#ffffff');
 const controls=new Map([6,7].map(id=>[id,{dataset:{},setAttribute(){}}]));
 const context=vm.createContext({simulator:snapshot,model:null,input:{pressed:()=>new Set()},keys:controls,channels:flat,canMutate:()=>true});
 vm.runInContext(phone.slice(phone.indexOf('function renderMechanical('),phone.indexOf('function cleanup(')),context);
 snapshot.devices.buttons=[6,7].map(id=>({id,enabled:true,pressed:false,color:'unchanged'}));
 const check=(id,fill,digit,symbol,compensation=false)=>{
  const [fillChannel,digitChannel,symbolChannel,compensationChannel]=expected[id];
  snapshot.devices.lighting={[fillChannel]:Number(fill),[digitChannel]:Number(digit),[symbolChannel]:Number(symbol),[compensationChannel]:Number(compensation)};
  context.simulator=snapshot;context.renderMechanical();model.update(snapshot);
  const key=keys.get(id),control=controls.get(id);
  assert.strictEqual(key.labelMaterial.color.value,digit?on:off,'3D digit '+id+' follows only its digit channel');
  assert.strictEqual(key.symbolMaterial.color.value,symbol?on:off,'3D emblem '+id+' follows only its symbol channel');
  assert.strictEqual(control.dataset.digit,String(digit),'flat digit '+id+' follows its physical channel');
  assert.strictEqual(control.dataset.symbol,String(symbol),'flat emblem '+id+' follows its physical channel');
  assert.strictEqual(control.dataset.fill,String(fill));
  assert.strictEqual(control.dataset.digit==='true'?flatDigitOn:flatOff,key.labelMaterial.color.value);
  assert.strictEqual(control.dataset.symbol==='true'?flatSymbolOn:flatOff,key.symbolMaterial.color.value);
  assert.strictEqual(key.material.color.value,fill?'enabled-'+id:'#8b8b8b','indicator channels never alter fill '+id);
  assert.strictEqual(key.material.emissiveIntensity,fill?.32:0);
  const other=keys.get(id===6?7:6);
  assert.strictEqual(other.labelMaterial.color.value,off,'no cross-button digit leak');
  assert.strictEqual(other.symbolMaterial.color.value,off,'no cross-button emblem leak');
 };
 for(const id of [6,7]){
  for(const fill of [false,true])for(const [digit,symbol] of [[false,false],[true,false],[false,true],[true,true]])check(id,fill,digit,symbol);
  check(id,false,false,false,true); // Compensation is a distinct physical channel, not another emblem.
  for(const [digit,symbol] of [[true,true],[false,true],[true,false],[false,false],[true,true],[false,false]])check(id,true,digit,symbol);
 }
 context.simulator=null;context.renderMechanical();model.update(null);
 for(const id of [6,7]){
  const key=keys.get(id),control=controls.get(id);
  assert.strictEqual(key.labelMaterial.color.value,off);assert.strictEqual(key.symbolMaterial.color.value,off);
  assert.strictEqual(key.material.color.value,'#8b8b8b');assert.strictEqual(key.material.emissiveIntensity,0);
  assert.strictEqual(control.dataset.digit,'false');assert.strictEqual(control.dataset.symbol,'false');assert.strictEqual(control.dataset.fill,'false');
 }
}
async function gestures(){
 const sent=[],errors=[];let now=100,epoch=3,allow=true;
 const context=vm.createContext({performance:{now:()=>now},Map,Set});
 vm.runInContext(phone.slice(phone.indexOf('class DeviceInput'),phone.indexOf('function mountPhone'))+';globalThis.Adapter=DeviceInput;',context);
 const input=new context.Adapter({send:async body=>{sent.push(body);},snapshot:()=>({epoch}),enabled:()=>allow,change:()=>{},error:error=>errors.push(error),clock:()=>now});
 assert(input.press(3));assert(!input.press(3),'repeated keydown does not send another press');
 now+=601;await input.release(3);
 assert.deepStrictEqual(sent.map(v=>v.press),['press','hold','release']);assert.strictEqual(sent[1].duration_ms,601);
 sent.length=0;input.press(7);now+=5001;await input.release(7);
 assert.deepStrictEqual(sent.map(v=>v.press),['press','long_hold','release']);
 sent.length=0;input.press(2);now+=7000;epoch=4;input.cancel();await tick();
 assert.deepStrictEqual(sent.map(v=>v.press),['press','release'],'cancel never produces a held action');
 assert(sent.every(v=>v.epoch===3),'release retains the original input epoch');
 allow=false;assert(!input.press(1));assert.strictEqual(input.active.size,0);
 allow=true;sent.length=0;input.send=async body=>{sent.push(body);if(body.press==='hold')throw new Error('hold rejected');};
 input.press(1);now+=800;await input.release(1);
 assert.deepStrictEqual(sent.map(v=>v.press),['press','hold','release'],'release survives a rejected hold');assert.strictEqual(errors.length,1);
}
async function latePermission(){
 let permission,trackStopped=0,created=0,flushed=0;
 const fields={};const stream={getTracks:()=>[{stop:()=>trackStopped++}]};
 const mediaContext={state:'running',sampleRate:48000,destination:{},createMediaStreamSource:()=>{created++;return{connect:()=>{},disconnect:()=>{}};},createScriptProcessor:()=>({connect:()=>{},disconnect:()=>{}}),createGain:()=>({gain:{},connect:()=>{},disconnect:()=>{}})};
 const context=vm.createContext({
  current:micState(),authBlocked:false,wsSnapshotReady:true,wsReloadRequired:false,mic:null,micStarting:false,micGeneration:0,micOwner:null,window:{},
  ws:{readyState:1,send:()=>{}},WebSocket:{OPEN:1},navigator:{mediaDevices:{getUserMedia:()=>new Promise(resolve=>permission=resolve)}},
  unlockAudio:async()=>mediaContext,session:()=> 'owner',createMicResampler:()=>({flush:()=>flushed++,feed:()=>{},reset:()=>{}}),
  MIC_DUCK_GAIN:.063,MIC_DUCK_ATTACK:.75,MIC_DUCK_RELEASE:.35,micInputGain:1,activeSources:new Set(),
  $:id=>(fields[id]||={}),bytesToB64:()=>'',Float32Array,
 });
 const start=source.indexOf('function microphoneEligible()'),end=source.indexOf("$('mic').onclick",start);
 vm.runInContext(source.slice(start,end),context);
 const pending=context.startMic();await tick();assert(context.micStarting);
 context.clearMic();permission(stream);await pending;
 assert.strictEqual(trackStopped,1,'permission resolved after hangup stops every returned track');
 assert.strictEqual(created,0,'late permission never creates a capture node');assert.strictEqual(context.mic,null);assert.strictEqual(flushed,0,'cleanup never flushes captured speech');
 const active=context.startMic();await tick();permission(stream);await active;
 assert(context.mic,'explicit enable starts a real capture path');assert.strictEqual(created,1);
 context.current.simulator.microphone.eligible=false;context.clearMic();
 assert.strictEqual(trackStopped,2);assert.strictEqual(context.mic,null);assert.strictEqual(flushed,0);
}
async function microphoneWireFlow(){
 const sent=[],fields={};let stopped=0;
 const track={stop:()=>stopped++};const stream={getTracks:()=>[track]};
 const mediaContext={state:'running',sampleRate:48000,destination:{},createMediaStreamSource:()=>({connect:()=>{},disconnect:()=>{}}),createScriptProcessor:()=>({connect:()=>{},disconnect:()=>{}}),createGain:()=>({gain:{},connect:()=>{},disconnect:()=>{}})};
 const context=vm.createContext({
  current:micState(),authBlocked:false,wsSnapshotReady:true,wsReloadRequired:false,mic:null,micStarting:false,micGeneration:0,micOwner:null,window:{},
  ws:{readyState:1,send:value=>sent.push(JSON.parse(value))},WebSocket:{OPEN:1},
  navigator:{mediaDevices:{getUserMedia:async()=>stream}},unlockAudio:async()=>mediaContext,session:()=> 'owner',
  MIC_DUCK_GAIN:.063,MIC_DUCK_ATTACK:.75,MIC_DUCK_RELEASE:.35,micInputGain:1,activeSources:new Set(),
  $:id=>(fields[id]||={}),btoa:value=>Buffer.from(value,'binary').toString('base64'),Float32Array,
 });
 const start=source.indexOf('function bytesToB64('),end=source.indexOf("$('mic').onclick",start);
 vm.runInContext(source.slice(start,end),context);
 await context.startMic();
 const process=context.mic.node.onaudioprocess;
 for(let i=0;i<6;i++)process({inputBuffer:{getChannelData:()=>new Float32Array(1024).fill(.25)}});
 assert(sent.length>0,'actual capture/resampler sends WS microphone frames');
 for(const frame of sent){
  assert.strictEqual(frame.type,'audio_pcm');assert.strictEqual(frame.session_id,'owner');
  assert.strictEqual(frame.sample_rate,16000);assert.strictEqual(Buffer.from(frame.data_b64,'base64').length,512*4);
 }
 const count=sent.length;context.current.simulator.microphone.eligible=false;context.syncMicAdmission();
 process({inputBuffer:{getChannelData:()=>new Float32Array(1024)}});
 assert.strictEqual(sent.length,count,'authoritative disallowance blocks more microphone PCM');
 assert(context.mic,'temporary disallowance retains the enabled microphone');assert.strictEqual(stopped,0);
 context.current.simulator.microphone.eligible=true;context.syncMicAdmission();
 assert.strictEqual(context.microphoneAccepting(),true);
 for(let i=0;i<2;i++)process({inputBuffer:{getChannelData:()=>new Float32Array(1024).fill(.75)}});
 assert(sent.length>count,'intake resumes without another permission request');
 const resumed=Buffer.from(sent[count].data_b64,'base64');
 assert.strictEqual(resumed.readFloatLE(0),.75,'old resampler tail is discarded at the pause boundary');
 const beforeStop=sent.length;context.stopMic();assert.strictEqual(stopped,1);
 assert.strictEqual(sent.at(-1).type,'flush_audio','explicit stop terminates the current capture');
 assert(sent.length>beforeStop+1,'explicit stop flushes the remaining PCM first');
 assert.strictEqual(sent.filter(frame=>frame.type==='flush_audio').length,1);
 context.stopMic();const stoppedCount=sent.length;
 process({inputBuffer:{getChannelData:()=>new Float32Array(1024)}});
 assert.strictEqual(sent.length,stoppedCount,'late callback and repeated stop cannot send after cleanup');
 for(const change of [s=>s.simulator.epoch++,s=>s.simulator.session.session_id='new-owner',s=>s.simulator.admission.input_ready=false,s=>s.session.handset_up=false]){
  context.current=micState();await context.startMic();assert(context.mic);change(context.current);context.syncMicAdmission();assert.strictEqual(context.mic,null,'terminal context change closes capture');
 }
 context.current=micState();await context.startMic();context.ws={readyState:1,send:()=>{throw new Error('stale capture sent to replacement socket');}};context.stopMic();assert.strictEqual(context.mic,null);
}
function snapshotFences(){
 const fields={},updates=[],speeches=[];
 const field=id=>(fields[id]||={value:'stand',textContent:'',dataset:{},classList:{add:()=>{},remove:()=>{},toggle:()=>{},contains:()=>true},setAttribute:()=>{},addEventListener:()=>{},replaceChildren:()=>{},style:{removeProperty:()=>{}},focus:()=>{}});
 const context=vm.createContext({current:null,suppressPhoneAudio:false,audioReconnectPending:false,authBlocked:false,wsSnapshotReady:true,wsReloadRequired:false,mic:null,micStarting:false,audioRetry:null,failedPCMOwner:null,pcmPlaybackError:null,pcmTextAttempt:0,clearPCMPlaybackError:()=>{},firstSnapshotRendered:false,lastSpokenCommand:0,audioReady:true,
  normalizeState:value=>value,showError:error=>{throw error;},$:field,document:{getElementById:field,querySelector:()=>null},
  window:{mishkaPhoneUpdate:value=>updates.push(value)},validateButtonContract:()=>({valid:true}),
  renderButtonLegend:()=>{},clearMic:()=>{},syncMicAdmission:()=>{},buttonLegendSummary:()=>'',microphoneAccepting:()=>false,cardStatusProjection:()=>({label:'',tone:''}),setRuntimeValue:()=>{},
  renderLog:()=>{},renderHumanLog:()=>{},eventSummary:()=>{},resultSummary:()=>{},renderAudioStatus:()=>{},renderStoryStatus:()=>{},renderRuntimeStatus:()=>{},
  deriveLEDState:()=>({}),normalizeLightingState:()=>({valid:true,state:{}}),renderButtons:()=>{},renderLightingElements:()=>{},renderLiveDialogue:()=>{},speakCommands:value=>speeches.push(value),
 });
 const helperStart=source.indexOf('function simulatorSnapshotIsCurrent(');
 const start=helperStart>=0?helperStart:source.indexOf('function render(state)');
 const end=source.indexOf('function normalizeLightingState(',start);
 vm.runInContext(source.slice(start,end),context);
 vm.runInContext(source.slice(source.indexOf('function interactionGuide('),source.indexOf('function enqueuePCM(')),context);
 const snapshot=(epoch,cursor,up,version=0)=>({
  session:{state:up?'ACTIVE':'ENDED',handset_up:up,node:up?'root_menu':'session_closed'},
  simulator:{epoch,journal_cursor:cursor,session:{session_id:'stand',state_version:version},microphone:{eligible:up},admission:{input_ready:true,connected:true},devices:{handset:{state:up?'up':'down'},buttons:[],lighting:{},rfid:{},audio:{}},fault:{}},
  commands:[],latencies:[],results:[],errors:[],events:[],raw_frames:[],lighting:{},
 });
 const ended=snapshot(8,20,false,10);context.render(ended);
 const count=updates.length;
 context.render(snapshot(7,19,true,9));
 assert.strictEqual(context.current,ended,'late pre-hangup snapshot cannot replace canonical ended state');
 assert.strictEqual(context.suppressPhoneAudio,true,'stale handset-up cannot reopen the local audio fence');
 assert.strictEqual(updates.length,count,'stale snapshot cannot reach phone controls');
 assert.strictEqual(speeches.length,count,'stale snapshot cannot enter owned asset playback');
 context.render(snapshot(8,19,true,8));
 assert.strictEqual(context.current,ended,'same-epoch older journal snapshot cannot roll the UI back');
 const relift=snapshot(8,21,true,1);context.render(relift);
 assert.strictEqual(context.current,relift,'new input may recreate stand session with lower state_version');
 assert.strictEqual(context.suppressPhoneAudio,false,'fresh canonical relift restores media');
 const reset=snapshot(9,22,false,0);context.render(reset);
 assert.strictEqual(context.current,reset,'new reset epoch is accepted');
 assert.strictEqual(context.suppressPhoneAudio,true);
 context.render(snapshot(8,999,true,999));
 assert.strictEqual(context.current,reset,'older epoch remains invalid even with larger cursor or state_version');
 const malformed=snapshot(10,23,true);malformed.simulator.epoch='10';context.render(malformed);
 assert.strictEqual(context.current,reset,'invalid epoch cannot reopen media');
 const missing=snapshot(10,23,true);delete missing.simulator;context.render(missing);
 assert.strictEqual(context.current,reset,'a known simulator cannot be replaced by an unversioned snapshot');
 // Exercise the actual WebSocket close/reconnect handlers and actual render.
 Object.assign(context,{
  ws:null,pageLifecycleStopped:false,wsIntentionalClose:false,console,
  location:{protocol:'https:',host:'local.test'},apiPath:value=>value,
  startWebsocketHeartbeat:()=>{},stopWebsocketHeartbeat:()=>{},
  scheduleReconnect:()=>{},schedulePolling:()=>{},stopPolling:()=>{},cancelOrdinaryPosts:()=>{},refresh:async()=>{},
  interruptAudioForInput:()=>{},parseMessage:JSON.parse,
  WebSocket:class {static OPEN=1;static CONNECTING=0;constructor(){this.readyState=1;}},
 });
 context.document.hidden=false;
 Object.assign(context,{
  post:async()=>null,unlockAudio:async()=>{},startMic:async()=>{},stopMic:()=>{},microphoneAccepting:()=>false,
  activeSources:new Set(),pendingPCM:[],postQueue:[],activePost:null,
  setTimeout:()=>0,clearTimeout:()=>{},performance:{now:()=>0},
 });
 Object.assign(context.document,{querySelectorAll:()=>[],addEventListener:()=>{},body:{dataset:{phone:'true'}}});
 context.window.addEventListener=()=>{};
 // Mount the actual phone on the actual console bridge. Its disconnect callback
 // used to call cleanup() and accidentally erase the console's reconnect intent.
 vm.runInContext(source.slice(source.indexOf('window.MishkaConsole =')),context);
 vm.runInContext(phone,context);
 assert.strictEqual(typeof context.window.mishkaPhoneConnection,'function');
 const connectStart=source.indexOf('function connect()'),connectEnd=source.indexOf('async function fetchWithTimeout(',connectStart);
 vm.runInContext(source.slice(connectStart,connectEnd),context);
 context.render(snapshot(9,24,true));
 context.connect();context.ws.onopen();
 context.ws.onclose();
 assert.strictEqual(context.suppressPhoneAudio,true,'disconnect immediately blocks PCM');
 context.connect();context.ws.onopen();
 context.ws.onmessage({data:JSON.stringify({type:'stand_state',state:snapshot(9,25,true)})});
 assert.strictEqual(context.suppressPhoneAudio,false,'new connection restores PCM from current handset-up snapshot without requiring relift');
 assert.strictEqual(context.window.MishkaConsole.media().inputReady,true,'accepted same-epoch snapshot enables this connection');
 const oldSocket=context.ws;
 context.ws.onclose();context.connect();context.ws.onopen();
 assert.strictEqual(context.window.MishkaConsole.media().inputReady,false,'open socket waits for its own accepted state');
 assert.strictEqual(fields['phone-text'].disabled,true);assert.strictEqual(fields['phone-microphone'].disabled,true);
 assert.strictEqual(fields['phone-handset'].disabled,false,'cleanup remains available while snapshot is pending');
 const beforeOld=context.current;
 oldSocket.onmessage({data:JSON.stringify({type:'stand_state',state:snapshot(99,999,true)})});
 assert.strictEqual(context.current,beforeOld,'late old-socket state cannot cross the connection boundary');
 context.render(snapshot(9,26,true));
 assert.strictEqual(context.suppressPhoneAudio,true,'HTTP polling cannot restore disconnected audio');
 assert.strictEqual(context.window.MishkaConsole.media().inputReady,false,'old-process HTTP cannot verify the replacement socket');
 context.showError=()=>{};
 context.ws.onmessage({data:JSON.stringify({type:'stand_state',state:snapshot(0,0,true)})});
 assert.strictEqual(context.suppressPhoneAudio,true,'lower epoch cannot restore PCM');
 assert.strictEqual(context.window.MishkaConsole.media().reloadRequired,true);
 assert.strictEqual(fields.connection.textContent,'RELOAD REQUIRED');
 assert.match(fields['phone-instruction'].textContent,/Обновите страницу/);
 assert.strictEqual(fields['phone-text'].disabled,true);assert.strictEqual(fields['phone-microphone'].disabled,true);
 assert.strictEqual(fields['phone-handset'].disabled,false);assert.strictEqual(fields['phone-reset'].disabled,false);
 context.ws.onmessage({data:JSON.stringify({type:'stand_state',state:snapshot(0,10,true)})});
 context.render(snapshot(0,20,true));
 assert.strictEqual(context.current.simulator.epoch,9,'new process cannot erase the retained epoch fence');
 context.render(snapshot(12,100,true));
 assert.strictEqual(context.window.MishkaConsole.media().inputReady,false,'even an accepted old-process HTTP snapshot cannot clear reload admission');
 context.audioReconnectPending=false; // explicit local cleanup revokes reconnect recovery
 context.ws.onmessage({data:JSON.stringify({type:'stand_state',state:snapshot(12,101,true)})});
 assert.strictEqual(context.suppressPhoneAudio,true,'local cleanup remains suppressed despite later handset-up snapshot');
 context.audioReconnectPending=true;context.authBlocked=true;
 context.ws.onmessage({data:JSON.stringify({type:'stand_state',state:snapshot(12,102,true)})});
 assert.strictEqual(context.suppressPhoneAudio,true,'auth block prevents reconnect audio recovery');
 return {context,fields,snapshot};
}
function serverInstanceRecovery(){
 const {context,fields,snapshot}=snapshotFences();
 context.authBlocked=false;context.wsReloadRequired=false;context.wsSnapshotReady=false;context.current=null;
 const captured=(id,revision,cursor=0)=>{const value=snapshot(0,cursor,false);Object.assign(value.simulator,{instance_id:id,snapshot_revision:revision});return value;};
 const old=captured('a'.repeat(16),500);context.render(old);
 context.ws=null;context.connect();context.ws.onopen();
 context.ws.onmessage({data:JSON.stringify({type:'stand_state',state:captured('b'.repeat(16),1)})});
 assert.strictEqual(context.current,old,'a fresh server incarnation cannot replace retained state');
 assert.strictEqual(context.wsReloadRequired,true,'restart at identical epoch/cursor still offers reload');
 assert.strictEqual(fields.connection.textContent,'RELOAD REQUIRED');
 // A delayed first WS snapshot from the same process is ordinary transport reordering.
 context.current=null;context.wsReloadRequired=false;context.wsSnapshotReady=false;
 context.render(captured('b'.repeat(16),10,10));
 context.ws.onmessage({data:JSON.stringify({type:'stand_state',state:captured('b'.repeat(16),9,9)})});
 assert.strictEqual(context.wsReloadRequired,false,'same-instance delivery delay is not a restart');
 context.ws.onmessage({data:JSON.stringify({type:'stand_state',state:captured('b'.repeat(16),11,11)})});
 assert.strictEqual(context.wsSnapshotReady,true,'a later same-instance frame restores admission');
}
async function handsetGeneration(){
 const {context,fields,snapshot}=snapshotFences(),requests=[];
 let permission;
 context.authBlocked=false;context.wsReloadRequired=false;context.wsSnapshotReady=true;context.current=null;context.render(snapshot(10,30,false));
 context.recordLiveInput=()=>{};context.recordLiveResult=()=>{};context.AbortController=AbortController;context.session=()=> 'stand';
 context.fetchWithTimeout=(path,options)=>new Promise(resolve=>requests.push({path,options,resolve:()=>resolve({})}));
 vm.runInContext(source.slice(source.indexOf('const MAX_PENDING_POSTS ='),source.indexOf('function recordLiveInput(')),context);
 context.wsSnapshotReady=false;context.wsReloadRequired=true;
 await assert.rejects(context.post('/stand/text',{text:'old context'}),/Обновите страницу/);
 await assert.rejects(context.post('/stand/simulator/action',{action:'button',button_id:1,press:'press'}),/Обновите страницу/);
 assert.strictEqual(requests.length,0,'unverified input never reaches transport');
 const cleanup=context.post('/stand/simulator/action',{action:'reset'});await tick();
 assert.strictEqual(requests.length,1,'reset remains available without a verified socket');requests[0].resolve();await cleanup;requests.length=0;
 context.wsReloadRequired=false;context.wsSnapshotReady=true;
 context.unlockAudio=async()=>{};
 vm.runInContext(source.slice(source.indexOf('window.MishkaConsole =')),context);
 // Remount so gestures capture the real FIFO bridge instead of a test send shim.
 vm.runInContext(phone.slice(phone.indexOf('function mountPhone')),context);
 const lift=fields['phone-handset'].onclick();await tick();
 assert.strictEqual(fields['phone-handset'].disabled,false,'pending lift leaves urgent hangup available');
 assert.strictEqual(fields['phone-handset'].textContent,'Положить трубку');
 const down=fields['phone-handset'].onclick();await tick();
 assert.deepStrictEqual(requests.map(r=>JSON.parse(r.options.body).state),['up','down']);
 assert(requests[0].options.signal.aborted,'urgent down aborts the outstanding lift through the actual FIFO');
 requests[1].resolve();await down;requests[0].resolve();await lift;
 assert.strictEqual(fields['phone-handset'].textContent,'Поднять трубку','late lift completion cannot revive pending UI');
 context.unlockAudio=()=>new Promise(resolve=>permission=resolve);
 vm.runInContext(source.slice(source.indexOf('window.MishkaConsole =')),context);
 vm.runInContext(phone.slice(phone.indexOf('function mountPhone')),context);
 const lockedLift=fields['phone-handset'].onclick();
 const cancelled=fields['phone-handset'].onclick();await tick();
 assert.strictEqual(requests.length,3);assert.strictEqual(JSON.parse(requests[2].options.body).state,'down');
 permission();await lockedLift;assert.strictEqual(requests.length,3,'late audio unlock cannot submit an obsolete lift');
 requests[2].resolve();await cancelled;
}
function menuCopy(){
 const {context,fields,snapshot}=snapshotFences();
 context.authBlocked=false;context.wsReloadRequired=false;context.wsSnapshotReady=true;context.current=null;
 context.document.querySelector=()=>null;context.buttonLegendSummary=()=>'';
 vm.runInContext(source.slice(source.indexOf('function updateGuide('),source.indexOf('function enqueuePCM(')),context);
 const hint='Нажмите любую кнопку, чтобы пропустить подсказку. Затем нажмите нужную кнопку для выбора.';
 const nodes=['exhibition_menu_tutorial_intro','exhibition_menu_tutorial_prompt','exhibition_menu_tutorial_buttons','exhibition_menu_wave','exhibition_menu_repeat_prompt','exhibition_menu_repeat_listen','exhibition_menu_replay_buttons'];
 nodes.forEach((node,index)=>{
  const state=snapshot(13,index,true);state.session.node=node;state.simulator.session.node=node;context.render(state);
  const expected=(node==='exhibition_menu_repeat_listen'?'Скажите «да» или «нет». ':'')+hint;
  assert.strictEqual(fields['phone-instruction'].textContent,expected,'tutorial fallback follows '+node);
  assert.strictEqual(fields['guide-text'].textContent,expected,'console guide follows '+node);
  state.simulator.prompt={text:'Текущая записанная реплика'};context.render(state);
  assert.strictEqual(fields['phone-instruction'].textContent,'Текущая записанная реплика','active transcript takes priority over skip hint');
 });
 const ready=snapshot(13,20,true);ready.session.node='menu_ready';ready.simulator.session.node='menu_ready';context.render(ready);
 assert.strictEqual(fields['phone-instruction'].textContent,'Выберите кнопку на лапе');
 assert.strictEqual(fields['guide-text'].textContent,'Выберите действие кнопкой 1–7.');
}
function gameGuidance(){
 const {context,fields,snapshot}=snapshotFences();
 context.authBlocked=false;context.wsReloadRequired=false;context.wsSnapshotReady=true;context.current=null;
 let cursor=0;
 const show=(id,stage='playing',phase='playing',substate='')=>{
  const state=snapshot(13,++cursor,true);state.session.node=state.simulator.session.node='game_active';
  state.canonical_state={mode:'game_active',active_game_id:id,game_audio_stage:stage,game_phase:phase,substate};
  context.render(state);return state;
 };
 const check=pattern=>{
  assert.match(fields['guide-text'].textContent,pattern);
  assert.strictEqual(fields['phone-instruction'].textContent,fields['guide-text'].textContent,'mounted phone shares game guidance');
 };
 for(const id of ['free_lights','quiz','repeat_pattern','odd_one_out','word_game']){
  show(id,'starting');check(/Дослушай Мишку/);
  show(id,'prompt');check(/Дослушай Мишку/);
  show(id,'playing','instruction');check(/Дослушай Мишку/);
  show(id,'playing','playing','game_exit_confirmation');check(/6 — продолжить; 7 — закончить; 5 — помощь/);
 }
 show('free_lights');check(/1–6.*7 — выйти/);
 show('repeat_pattern','playing','demonstrate');check(/Посмотри.*Потом повтори/);
 show('repeat_pattern');check(/Повтори подсвеченные кнопки.*7 — выйти/);
 show('odd_one_out');check(/подсвеченную кнопку.*7 — выйти/);
 show('quiz');check(/подсвеченную кнопку.*ответа 7 нажми и отпусти.*удерживай 7 пять секунд и отпусти/);
 show('quiz','playing','demonstrate');check(/подсвеченную кнопку.*удерживай 7 пять секунд и отпусти/);
 assert.doesNotMatch(fields['guide-text'].textContent,/7 —/,'quiz 7 may be an answer');
 const word=show('word_game');check(/Включи микрофон и назови слово/);
 context.micStarting=true;context.window.mishkaPhoneMedia();check(/Разреши микрофон/);
 context.micStarting=false;context.mic={};context.microphoneAccepting=()=>true;context.window.mishkaPhoneMedia();check(/Назови слово для игры/);
 context.microphoneAccepting=()=>false;context.window.mishkaPhoneMedia();check(/Дослушай Мишку.*Потом назови слово/);
 word.simulator.prompt={text:'Текущий вопрос Мишки'};context.window.mishkaPhoneMedia();
 assert.strictEqual(fields['phone-instruction'].textContent,'Текущий вопрос Мишки','active game transcript keeps priority');
}
async function onboardingGuidance(){
 const {context,fields,snapshot}=snapshotFences(),permissions=[];
 context.authBlocked=false;context.wsReloadRequired=false;context.wsSnapshotReady=true;context.current=null;
 const mediaContext={sampleRate:48000,destination:{},createMediaStreamSource:()=>({connect:()=>{},disconnect:()=>{}}),createScriptProcessor:()=>({connect:()=>{},disconnect:()=>{}}),createGain:()=>({gain:{},connect:()=>{},disconnect:()=>{}})};
 Object.assign(context,{micOwner:null,micGeneration:0,micInputGain:1,MIC_DUCK_GAIN:.063,MIC_DUCK_ATTACK:.75,MIC_DUCK_RELEASE:.35,
  unlockAudio:async()=>mediaContext,session:()=>fields.session.value,
  navigator:{mediaDevices:{getUserMedia:()=>new Promise((resolve,reject)=>permissions.push({resolve,reject}))}},
 });
 context.ws.send=()=>{};
 vm.runInContext(source.slice(source.indexOf('function bytesToB64('),source.indexOf("$('mic').onclick",source.indexOf('function bytesToB64('))),context);
 vm.runInContext(source.slice(source.indexOf('window.MishkaConsole =')),context);
 vm.runInContext(phone.slice(phone.indexOf('function mountPhone')),context);
 let cursor=0;
 const show=(node,up=true)=>{
  const state=snapshot(13,++cursor,up);state.session.node=node;state.simulator.session.node=node;context.render(state);return state;
 };
 const assertHint=expected=>{
  assert.strictEqual(fields['phone-instruction'].textContent,expected);
  assert.strictEqual(fields['guide-text'].textContent,expected,'phone and console guide share the current instruction');
 };
 for(const [node,hint] of Object.entries({
  voice_welcome:'Сейчас Мишка поздоровается. Подожди немного.',
  exhibition_introduction:'Дослушай Мишку.',
  exhibition_name_question:'Дослушай вопрос Мишки.',
  exhibition_age_prompt:'Дослушай Мишку. Потом можно будет ответить.',
  exhibition_age_hint:'Дослушай Мишку. Потом можно будет ответить.',
  exhibition_age_selection:'Нажми цифру своего возраста (1–7). Можно включить микрофон или написать возраст в «Инструментах».',
  exhibition_age_complete:'Мишка завершает знакомство. Подожди немного.',
  exhibition_onboarding_complete:'Сейчас появится меню. Подожди немного.',
  fixed_story_playing:'Слушай сказку. 6 — пауза; 7 — выйти из сказки.',
  story_pause_announcement:'Дослушай Мишку. Затем можно будет продолжить сказку.',
  story_paused:'6 — продолжить сказку; 7 — выйти из сказки.',
  story_resume_announcement:'Дослушай Мишку. Сказка скоро продолжится.',
 })){
  const state=show(node);assertHint(hint);
  state.simulator.prompt={text:'Текущая записанная реплика'};context.render(state);
  assert.strictEqual(fields['phone-instruction'].textContent,'Текущая записанная реплика','valid-state transcript keeps priority');
 }
 const off='Включи микрофон и назови своё имя. Можно написать его в «Инструментах».';
 const pending='Разреши микрофон в окне браузера. Или напиши своё имя в «Инструментах».';
 const accepting='Назови своё имя. Можно написать его в «Инструментах».';
 const paused='Дослушай Мишку. Потом назови своё имя или напиши его в «Инструментах».';
 const name=show('exhibition_name_capture');assertHint(off);
 const enable=fields['phone-microphone'].onclick();assertHint(pending);await tick();
 permissions.shift().resolve({getTracks:()=>[{stop:()=>{}}]});await enable;assertHint(accepting);
 name.simulator.microphone.eligible=false;context.syncMicAdmission();context.window.mishkaPhoneMedia();assertHint(paused);
 name.simulator.microphone.eligible=true;context.window.mishkaPhoneMedia();assertHint(accepting);
 name.simulator.prompt={text:'Имя в активной реплике'};context.window.mishkaPhoneMedia();
 assert.strictEqual(fields['phone-instruction'].textContent,'Имя в активной реплике');delete name.simulator.prompt;
 await fields['phone-microphone'].onclick();assertHint(off);
 const denied=fields['phone-microphone'].onclick();assertHint(pending);await tick();
 permissions.shift().reject(Object.assign(new Error('denied'),{name:'NotAllowedError'}));await denied;assertHint(off);
 assert.match(fields['phone-media-status'].textContent,/Доступ к микрофону запрещён/);
 const supportOff='Включи микрофон и расскажи, что случилось. Можно написать в «Инструментах».';
 const supportPending='Разреши микрофон в окне браузера. Или напиши, что случилось, в «Инструментах».';
 const supportAccepting='Расскажи, что случилось, или задай вопрос. Можно написать в «Инструментах».';
 const supportPaused='Дослушай Мишку. Потом расскажи, что случилось, или напиши в «Инструментах».';
 const support=show('support_intake');assertHint(supportOff);
 const supportEnable=fields['phone-microphone'].onclick();assertHint(supportPending);await tick();
 permissions.shift().resolve({getTracks:()=>[{stop:()=>{}}]});await supportEnable;assertHint(supportAccepting);
 support.simulator.microphone.eligible=false;context.window.mishkaPhoneMedia();assertHint(supportPaused);
 support.simulator.microphone.eligible=true;context.window.mishkaPhoneMedia();assertHint(supportAccepting);
 support.simulator.prompt={text:'Текущая реплика поддержки'};context.window.mishkaPhoneMedia();
 assert.strictEqual(fields['phone-instruction'].textContent,'Текущая реплика поддержки');
 delete support.simulator.prompt;await fields['phone-microphone'].onclick();assertHint(supportOff);
 const closed=show('session_closed');closed.session.state='ENDED';closed.simulator.prompt={text:'Старая реплика'};context.render(closed);
 assertHint('Сеанс завершён. Положи трубку, чтобы начать заново.');
 assert.strictEqual(fields['phone-handset'].disabled,false,'closed session still offers handset cleanup');
 show('session_closed',false);assert.strictEqual(fields['phone-instruction'].textContent,'Поднимите трубку');
 const profile=show('profile_ready',false);profile.session.state='STANDBY';profile.canonical_state={mode:'profile_ready'};context.render(profile);
 assert.strictEqual(fields['phone-instruction'].textContent,'Поднимите трубку');
 assert.strictEqual(fields['guide-text'].textContent,'Поднимите трубку, чтобы начать.');
 const menu=show('game_menu');
 menu.canonical_state={mode:'game_menu',scene_menu_descriptor:{scene:'game_menu',items:[{slot:1,title:'Огоньки'},{slot:5,title:'Игра в слова'}],buttons:[1,5,7]}};
 context.render(menu);assertHint('1 — Огоньки; 5 — Игра в слова; 7 — назад.');
 assert.doesNotMatch(fields['guide-text'].textContent,/5 — помощь/);
 show('wellbeing_check');assertHint('Ответь на вопрос Мишки: 1 — нет; 7 — да, нужна помощь; 5 — повторить вопрос.');
 show('personal_story_generating');assertHint('Мишка сочиняет и проверяет сказку. Подожди немного. 7 — выйти, если ждать не хочется.');
 show('personal_story_playing');assertHint('Слушай сказку. 6 — пауза; 7 — выйти из сказки.');
 const pendingStory=show('story_paused');pendingStory.canonical_state={personal_story_checkpoint_pending:true};context.render(pendingStory);
 assertHint('Сохраняем место в сказке. Подожди немного. Если продолжить не получилось, нажми 7 и выбери новую сказку.');
 show('safety_stop');assertHint('Позови взрослого рядом. Положи трубку, чтобы завершить разговор.');
 const dialogueOff='Включи микрофон и поговори с Мишкой. Можно написать в «Инструментах».';
 const dialoguePending='Разреши микрофон в окне браузера. Или напиши Мишке в «Инструментах».';
 const dialogueAccepting='Расскажи Мишке что-нибудь или задай вопрос. Можно написать в «Инструментах».';
 const dialoguePaused='Дослушай Мишку. Потом расскажи что-нибудь или задай вопрос. Можно написать в «Инструментах».';
 const retry='Мишка не расслышал. Попробуй ещё раз. ';
 const dialogue=show('bear_dialogue');assertHint(dialogueOff);
 const talking=fields['phone-microphone'].onclick();assertHint(dialoguePending);await tick();
 permissions.shift().resolve({getTracks:()=>[{stop:()=>{}}]});await talking;assertHint(dialogueAccepting);
 dialogue.simulator.microphone.eligible=false;context.window.mishkaPhoneMedia();assertHint(dialoguePaused);
 assert.doesNotMatch(fields['phone-media-status'].textContent,/Мишка слушает/);
 dialogue.simulator.microphone.eligible=true;context.window.mishkaPhoneMedia();assertHint(dialogueAccepting);
 const completion=(reason,sessionId='stand',state=show('bear_dialogue'))=>context.ws.onmessage({data:JSON.stringify({type:'stand_state',state,audio_completion:sessionId===null
  ? {reason,canonical_state:{mode:state.session.node,state_version:state.simulator.session.state_version,handset:'up'}}
  : {reason,session_id:sessionId,pending:false}})});
 completion('audio_retry',null);assertHint(retry+dialogueAccepting);
 assert.match(fields['phone-media-status'].textContent,/Мишка не расслышал/,'retry is visible outside the microphone diagnostics');
 context.current.simulator.microphone.eligible=false;context.window.mishkaPhoneMedia();assertHint(retry+dialoguePaused);
 assert.doesNotMatch(fields['phone-media-status'].textContent,/Мишка слушает/,'retry never implies a paused microphone is accepting');
 context.current.simulator.microphone.eligible=true;context.window.mishkaPhoneMedia();assertHint(retry+dialogueAccepting);
 context.current.simulator.prompt={text:'Активная реплика Мишки'};context.window.mishkaPhoneMedia();
 assert.strictEqual(fields['phone-instruction'].textContent,'Активная реплика Мишки','retry never replaces an active transcript');
 assert.strictEqual(fields['guide-text'].textContent,retry+dialogueAccepting);
 completion('bounded_s2s_audio',null);assertHint(dialogueAccepting);
 const successPaused=show('bear_dialogue');successPaused.simulator.microphone.eligible=false;
 completion('bounded_s2s_audio','stand',successPaused);assertHint(dialoguePaused);
 assert.doesNotMatch(fields['mic-state'].textContent,/слушает/,'successful response does not claim a paused microphone is listening');
 context.current.simulator.microphone.eligible=true;context.window.mishkaPhoneMedia();assertHint(dialogueAccepting);
 completion('audio_retry','other-session');assertHint(dialogueAccepting);
 const wrongVersion=show('bear_dialogue');context.ws.onmessage({data:JSON.stringify({type:'stand_state',state:wrongVersion,audio_completion:{reason:'audio_retry',canonical_state:{mode:'bear_dialogue',state_version:99,handset:'up'}}})});assertHint(dialogueAccepting);
 context.ws.onmessage({data:JSON.stringify({type:'stand_state',state:wrongVersion,audio_completion:{reason:'audio_retry'}})});assertHint(dialogueAccepting);
 const stale=snapshot(13,cursor-1,true);stale.session.node='bear_dialogue';
 completion('audio_retry','stand',stale);assertHint(dialogueAccepting);
 const socket=context.ws;context.ws={readyState:1,send:()=>{}};
 const oldSocketState=snapshot(13,++cursor,true);oldSocketState.session.node='bear_dialogue';
 socket.onmessage({data:JSON.stringify({type:'stand_state',state:oldSocketState,audio_completion:{reason:'audio_retry',session_id:'stand'}})});
 assertHint(dialogueAccepting);
 context.ws=socket;
 completion('audio_retry');assertHint(retry+dialogueAccepting);
 context.window.MishkaConsole.cleanup();assertHint(dialogueOff);
 assert.doesNotMatch(fields['phone-media-status'].textContent,/не расслышал/,'cleanup clears retry feedback');
 completion('audio_retry');assertHint(dialogueOff);
 context.suppressPhoneAudio=false;
 completion('audio_retry');assertHint(retry+dialogueOff);
 const next=show('bear_dialogue');next.simulator.session.session_id='new-session';context.render(next);assertHint(dialogueOff);
 context.suppressPhoneAudio=false;
 completion('audio_retry','new-session',next);assertHint(retry+dialogueOff);
 const newEpoch=show('bear_dialogue');newEpoch.simulator.epoch=14;newEpoch.simulator.session.session_id='new-session';context.render(newEpoch);assertHint(dialogueOff);
}
async function simulatorQueue(){
 const requests=[];let resolveFirst;const context=vm.createContext({AbortController,session:()=> 'display-only',recordLiveInput:()=>{},recordLiveResult:()=>{},showError:()=>{},current:null,
 fetchWithTimeout:(path,options)=>new Promise(resolve=>{requests.push({path,options,resolve:()=>resolve({})});}),
 });
 const start=source.indexOf('const MAX_PENDING_POSTS ='),end=source.indexOf('function recordLiveInput(',start);
 vm.runInContext(source.slice(start,end),context);
 const press=context.post('/stand/simulator/action',{action:'button',button_id:1,press:'press',epoch:2});
 const release=context.post('/stand/simulator/action',{action:'button',button_id:1,press:'release',epoch:2});
 assert(!JSON.parse(requests[0].options.body).session_id,'simulator payload contains no invented session field');
 const down=context.post('/stand/simulator/action',{action:'handset',state:'down',epoch:2});await tick();
 assert.strictEqual(await press,null);assert.strictEqual(await release,null);
 assert(requests[0].options.signal.aborted,'hangup cancels a stalled press immediately');
 assert.strictEqual(JSON.parse(requests[1].options.body).state,'down');requests[1].resolve();await down;
}
async function queuedAdmissionBoundaries(){
 for(const boundary of ['disconnect','stale-first-snapshot','dispatch']){
  const {context,fields,snapshot}=snapshotFences(),requests=[],results=[];
  context.authBlocked=false;context.wsReloadRequired=false;context.wsSnapshotReady=true;
  context.current=null;context.render(snapshot(0,100,true));
  context.recordLiveInput=()=>{};context.recordLiveResult=value=>results.push(value);
  context.AbortController=AbortController;context.session=()=> 'stand';
  context.fetchWithTimeout=(path,options)=>new Promise(resolve=>requests.push({path,options,resolve:()=>resolve({late:true})}));
  vm.runInContext(source.slice(source.indexOf('const MAX_PENDING_POSTS ='),source.indexOf('function recordLiveInput(')),context);
  const first=context.post('/stand/text',{text:'first'});
  const held=context.post('/stand/text',{text:'second'});
  const terminal=context.post('/stand/audio/lifecycle',{status:'finished'});
  assert.strictEqual(requests.length,1,'second input and terminal callback begin queued behind the first request');
  if(boundary==='disconnect') context.ws.onclose();
  else if(boundary==='stale-first-snapshot'){
   context.ws.readyState=3;context.connect();context.ws.onopen();
   context.ws.onmessage({data:JSON.stringify({type:'stand_state',state:snapshot(0,0,true)})});
   assert.strictEqual(context.wsReloadRequired,true,'same-epoch lower cursor requires reload instead of indefinite syncing');
   assert.strictEqual(fields.connection.textContent,'RELOAD REQUIRED');
   assert.match(fields['phone-instruction'].textContent,/Обновите страницу/);
   assert.strictEqual(context.current.simulator.journal_cursor,100,'restart detection preserves the old cursor fence');
   context.render(snapshot(0,101,true));
   assert.strictEqual(context.window.MishkaConsole.media().inputReady,false,'late old-process HTTP cannot unblock a cursor-reset connection');
  }else{
   // A dispatch-time loss of admission must be safe even without a close event.
   context.wsSnapshotReady=false;requests[0].resolve();
  }
  await tick();
  assert.strictEqual(await first,null,'the obsolete in-flight input settles without publishing its result');
  assert.strictEqual(await held,null,'held input settles as cancelled');
  assert.deepStrictEqual(requests.map(request=>request.path),['/stand/text','/stand/audio/lifecycle'],'no held input reaches fetch across the boundary; terminal cleanup still dispatches');
  requests[1].resolve();await terminal;await tick();
  const down=context.post('/stand/simulator/action',{action:'handset',state:'down'});
  assert.strictEqual(requests.length,3,'urgent hangup remains dispatchable after loss of admission');
  requests[2].resolve();await down;
  requests[0].resolve();await tick();
  assert.strictEqual(results.filter(value=>value?.late).length,2,'late cancelled input cannot publish a result; terminal and hangup each complete');
 }
}
async function legacyObserverFallback(){
 const {context}=snapshotFences(),requests=[],results=[];
 context.authBlocked=false;context.wsReloadRequired=false;context.wsSnapshotReady=true;
 context.current={session:{state:'ACTIVE',handset_up:true}};
 context.recordLiveInput=()=>{};context.recordLiveResult=value=>results.push(value.order);
 context.AbortController=AbortController;context.session=()=> 'legacy';
 context.fetchWithTimeout=(path,options)=>new Promise(resolve=>{
  const order=requests.length+1;requests.push({path,options,resolve:()=>resolve({order})});
 });
 vm.runInContext(source.slice(source.indexOf('const MAX_PENDING_POSTS ='),source.indexOf('function recordLiveInput(')),context);
 const first=context.post('/stand/text',{text:'first'}),second=context.post('/stand/text',{text:'second'});
 let secondSettled=false;second.then(()=>{secondSettled=true;});
 assert.strictEqual(requests.length,1);
 context.ws.onclose();await tick();
 assert.strictEqual(requests[0].options.signal.aborted,false,'legacy observer loss does not abort an active HTTP input');
 assert.strictEqual(secondSettled,false,'legacy queued HTTP input remains pending after observer loss');
 assert.strictEqual(requests.length,1,'legacy fallback retains FIFO order');
 requests[0].resolve();assert.strictEqual((await first).order,1);await tick();
 assert.strictEqual(requests.length,2,'queued legacy input dispatches after the first HTTP result');
 requests[1].resolve();assert.strictEqual((await second).order,2);
 assert.deepStrictEqual(results,[1,2],'both legacy HTTP results complete in order without a WebSocket');
}
(async()=>{lightingProjection();serverInstanceRecovery();await gestures();await latePermission();await microphoneWireFlow();await handsetGeneration();menuCopy();gameGuidance();await onboardingGuidance();await simulatorQueue();await queuedAdmissionBoundaries();await legacyObserverFallback();console.log('remote phone lighting, gestures, onboarding/dialogue/story guidance, scoped speech retry, menu copy, verified reconnect/reload admission, simulator queue cancellation, legacy HTTP fallback, handset cancellation, owned microphone pause/resume/flush and stale snapshot fences passed');})().catch(error=>{console.error(error);process.exitCode=1;});
