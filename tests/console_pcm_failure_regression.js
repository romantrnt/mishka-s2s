'use strict';
const assert=require('assert'),fs=require('fs'),vm=require('vm');
const source=fs.readFileSync(process.argv[2]||'app/foundation/console/console.js','utf8');
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const ordinary=(token=12,extra={})=>({session_id:'owner',generation:71,token,sample_rate:16000,data_b64:'AABAIA==',...extra});
function harness({microphone=false,pending=false,accepting=false}={}){
 const fields={},visible=[],posts=[],sources=[];let fail=false,permission;
 const contextAudio={state:'running',currentTime:10,destination:{},
  createBuffer:(_channels,count,rate)=>({duration:count/rate,getChannelData:()=>new Float32Array(count)}),
  createBufferSource:()=>{
   const item={connect:()=>{},start(time){if(fail){fail=false;throw new Error('DOMException: source.start failed');}this.started=time;},stop(){this.stopped=true;this.onended?.();}};
   sources.push(item);return item;
  },
 };
 const context=vm.createContext({audioQueueTail:Promise.resolve(),audioGeneration:0,storySpeechGeneration:0,
  failedPCMOwner:null,pcmPlaybackError:null,pcmTextAttempt:0,pendingPCM:[],audioContext:contextAudio,nextAudioTime:0,activeSources:new Set(),
  audioReady:true,micInputGain:1,activeStoryPlayback:null,publicStoryBuffer:null,personalStoryPlayback:null,
  mic:microphone?{}:null,micStarting:pending,microphoneAccepting:()=>accepting,clearMic:()=>{},
  unlockAudio:async()=>{if(permission)await permission;return contextAudio;},
  atob:value=>Buffer.from(value,'base64').toString('binary'),
  $:id=>fields[id]||(fields[id]={textContent:'',dataset:{},classList:{remove:()=>{},add:()=>{},toggle:()=>{}}}),
  window:{mishkaPhoneError:value=>{visible.push(value);fields['phone-error']={textContent:value,hidden:false};}},post:async(path,payload)=>posts.push({path,payload}),
  discardStoppedStoryCheckpoint:()=>{},discardPersonalStoryPlayback:()=>{},freezePersonalStoryPosition:()=>{},
  freezeStoryCheckpoint:()=>{},checkpointPublicStoryTransfer:()=>{},minimumPCMGeneration:0,lastSpokenCommand:0,
 });
 vm.runInContext(source.slice(source.indexOf('function interactionActive('),source.indexOf('function sessionActionIsCurrent(')),context);
 vm.runInContext(source.slice(source.indexOf('function enqueuePCM('),source.indexOf('function initDiagnostics(')),context);
 const errorStart=source.indexOf('function showError(');
 vm.runInContext(source.slice(errorStart,source.indexOf('\nfunction ',errorStart+1)),context);
 const clearStart=source.indexOf('function clearPCMPlaybackError(');
 vm.runInContext(source.slice(clearStart,source.indexOf('\nfunction ',clearStart+1)),context);
 const stopStart=source.indexOf('function handleAudioStop(');
 vm.runInContext(source.slice(stopStart,source.indexOf('\nfunction ',stopStart+1)),context);
 vm.runInContext(source.slice(source.indexOf('function interruptAudioForInput('),source.indexOf("$('text-form').onsubmit")),context);
 return {context,fields,visible,posts,sources,failNext:()=>{fail=true;},
  rejectUnlock:()=>{let reject;permission=new Promise((_resolve,rejectPromise)=>{reject=rejectPromise;});return reject;},
 };
}
async function damagedOwner(){
 const h=harness();
 await h.context.enqueuePCM(ordinary());
 const privateFrame=ordinary(12,{private:true,first:true,final:true,playback:{session_id:'owner',response_id:'PERSONAL_WELCOME',playback_id:'play',playback_lease:'lease',generation:4,token:99}});
 await h.context.enqueuePCM(privateFrame);
 await h.context.enqueuePCM(ordinary(11));
 const story={stop(){this.stopped=true;}};h.context.activeSources.add(story);
 h.context.pendingPCM.push({message:ordinary(),generation:0},{message:ordinary(13),generation:0},{message:privateFrame,generation:0});
 h.failNext();
 const failed=h.context.enqueuePCM(ordinary()),queued=h.context.enqueuePCM(ordinary()),next=h.context.enqueuePCM(ordinary(13));
 await assert.rejects(failed,/source.start failed/);await queued;await next;
 assert.match(h.visible[0],/Не получилось воспроизвести ответ/,'ordinary failure has an understandable Russian message');
 assert.doesNotMatch(h.visible[0],/DOMException|source.start/,'raw browser exception stays out of the interface');
 assert.match(h.visible[0],/микрофон/);assert.match(h.visible[0],/положи трубку/);
 assert.strictEqual(h.visible.length,1,'remaining chunks cannot repeat the same failure');
 assert.strictEqual(h.sources.length,5,'queued chunk from the damaged owner never creates a source; next token does');
 assert(h.sources[0].stopped,'already scheduled chunks from the damaged response stop');
 assert(h.sources[3].stopped,'the failed source is cleaned up');
 assert(!h.sources[1].stopped,'private playback remains owned by its existing lifecycle');
 assert(!h.sources[2].stopped,'an active ordinary source with another token remains untouched');
 assert(!story.stopped,'story playback is not canceled by an ordinary response failure');
 assert(h.sources[4].started!==undefined,'next turn with the same transport generation is admitted');
 assert.deepStrictEqual(h.context.pendingPCM.map(item=>item.message.token),[13,12],'only pending chunks from the damaged owner are removed');
 assert.strictEqual(h.context.minimumPCMGeneration,0,'failure never bans the transport generation');
 assert(!h.posts.some(item=>item.path==='/stand/audio/failed'),'ordinary PCM cannot fabricate private failure identity');
 h.sources[1].onended();assert(h.posts.some(item=>item.path==='/stand/audio/completed'),'private terminal callback still works');
 await h.context.enqueuePCM(ordinary(12,{session_id:'other-session'}));
 assert(h.sources.at(-1).started!==undefined,'another session is not blocked by the damaged tuple');
 h.context.interruptAudioForInput();assert.strictEqual(h.context.failedPCMOwner,null,'cleanup releases the bounded failure owner');
 await h.context.enqueuePCM(ordinary());assert(h.sources.at(-1).started!==undefined,'cleanup permits a new visit even if token is reused');
 h.failNext();await assert.rejects(h.context.enqueuePCM(ordinary()),/source.start failed/);
 h.context.handleAudioStop({generation:72});assert.strictEqual(h.context.failedPCMOwner,null,'transport stop releases the failure owner');
}
async function microphoneCopy(){
 for(const [media,expected] of [[{},/Включи микрофон/],[{pending:true},/Разреши микрофон/],[{microphone:true},/приём речи на паузе/],[{microphone:true,accepting:true},/Спроси ещё раз/]]){
  const h=harness(media);h.failNext();await assert.rejects(h.context.enqueuePCM(ordinary()));
  assert.match(h.visible[0],expected);assert.match(h.visible[0],/«Инструментах»/);assert.match(h.visible[0],/положи трубку/);
  assert.doesNotMatch(h.visible[0],/слушаю|Мишка слушает|DOMException/);
 }
}
async function staleRejection(){
 const h=harness(),reject=h.rejectUnlock();
 const late=h.context.enqueuePCM(ordinary());late.catch(()=>{});await tick();
 h.context.interruptAudioForInput();reject(new Error('DOMException: late permission failed'));
 await assert.rejects(late,/late permission failed/);await tick();
 assert.deepStrictEqual(h.visible,[],'rejection from a cleaned-up generation cannot change phone/fallback DOM');
 assert.strictEqual(h.context.failedPCMOwner,null,'stale rejection cannot fence a new response');
 assert.strictEqual(h.sources.length,0);assert.strictEqual(h.posts.length,0);
}
async function newTextAttempt(){
 const h=harness();h.failNext();await assert.rejects(h.context.enqueuePCM(ordinary()));
 Object.assign(h.context,{AbortController,current:null,session:()=> 'owner',recordLiveInput:()=>{},recordLiveResult:()=>{},fetchWithTimeout:async()=>({})});
 vm.runInContext(source.slice(source.indexOf('const MAX_PENDING_POSTS ='),source.indexOf('function recordLiveInput(')),h.context);
 await h.context.post('/stand/text',{text:'Попробуем ещё раз'});
 assert.strictEqual(h.fields['phone-error'].hidden,true,'a new text attempt clears the previous playback error from the phone');
 assert.strictEqual(h.fields.fallback.textContent,'');
 assert.strictEqual(h.context.failedPCMOwner.token,12,'text retry retains the failed response fence until server stop');
 const count=h.sources.length;await h.context.enqueuePCM(ordinary());assert.strictEqual(h.sources.length,count,'late old-token PCM remains blocked during retry');
 h.context.showError(new Error('Новая ошибка соединения'));
 await h.context.post('/stand/text',{text:'Ещё одна попытка'});
 assert.strictEqual(h.fields['phone-error'].textContent,'Новая ошибка соединения','a fresh unrelated error is not cleared');
 h.failNext();await assert.rejects(h.context.enqueuePCM(ordinary(13)));
 h.context.document={querySelectorAll:()=>[quick]};
 const quick={dataset:{prompt:'Привет'}};
 h.context.$('text-form').requestSubmit=()=>h.context.$('text-form').onsubmit({preventDefault:()=>{}});
 const handlerStart=source.indexOf("$('text-form').onsubmit");
 vm.runInContext(source.slice(handlerStart,source.indexOf("$('send-raw')",handlerStart)),h.context);
 quick.onclick();await tick();
 assert.strictEqual(h.fields['phone-error'].hidden,true,'the existing quick-prompt/form route clears playback errors too');
 assert.strictEqual(h.context.failedPCMOwner.token,13,'form input interruption must not release a damaged turn');
 const before=h.sources.length;await h.context.enqueuePCM(ordinary(13));assert.strictEqual(h.sources.length,before);
 await h.context.enqueuePCM(ordinary(14));assert(h.sources.at(-1).started!==undefined,'new turn remains playable after a text retry');
}
async function errorAcrossServerStop(){
 const h=harness();h.failNext();await assert.rejects(h.context.enqueuePCM(ordinary()));
 h.context.handleAudioStop({generation:72});assert.strictEqual(h.context.failedPCMOwner,null);
 Object.assign(h.context,{AbortController,current:null,session:()=> 'owner',recordLiveInput:()=>{},recordLiveResult:()=>{},fetchWithTimeout:async()=>({})});
 vm.runInContext(source.slice(source.indexOf('const MAX_PENDING_POSTS ='),source.indexOf('function recordLiveInput(')),h.context);
 await h.context.post('/stand/text',{text:'Повтори ответ'});
 assert.strictEqual(h.fields['phone-error'].hidden,true,'server stop must not lose the displayed playback error marker before text retry');
 assert.strictEqual(h.fields.fallback.textContent,'');
 h.failNext();await assert.rejects(h.context.enqueuePCM(ordinary(13,{generation:72})));
 h.context.handleAudioStop({generation:73});h.context.showError(new Error('Новая ошибка соединения'));
 await h.context.post('/stand/text',{text:'Ещё раз'});
 assert.strictEqual(h.fields['phone-error'].textContent,'Новая ошибка соединения','stop then text retry preserves a fresh unrelated error');
}
async function displayedErrorCleanup(){
 const snapshot=(id='owner',epoch=1,up=true)=>({session:{state:up?'ACTIVE':'ENDED',handset_up:up,node:up?'bear_dialogue':'session_closed'},
  simulator:{epoch,journal_cursor:1,session:{session_id:id}},commands:[],latencies:[],results:[],errors:[],events:[],raw_frames:[],lighting:{}});
 for(const boundary of ['interrupt','hangup','epoch','session'])for(const fresh of [false,true]){
  const h=harness();h.failNext();await assert.rejects(h.context.enqueuePCM(ordinary()));h.context.handleAudioStop({generation:72});
  if(fresh)h.context.showError(new Error('Другая свежая ошибка'));
  if(boundary==='interrupt')h.context.interruptAudioForInput();
  else{
   Object.assign(h.context,{current:snapshot(),normalizeState:value=>value,session:()=> 'owner',audioRetry:null,
    suppressPhoneAudio:false,audioReconnectPending:false,authBlocked:false,wsSnapshotReady:true,wsReloadRequired:false,firstSnapshotRendered:true,lastSpokenCommand:0,
    document:{getElementById:h.context.$},validateButtonContract:()=>({valid:true}),cardStatusProjection:()=>({label:'',tone:''}),
    deriveLEDState:()=>({}),normalizeLightingState:()=>({valid:true,state:{}}),
   });
   for(const name of ['syncMicAdmission','renderButtonLegend','updateGuide','setRuntimeValue','renderLog','renderHumanLog','eventSummary','resultSummary','renderAudioStatus','renderStoryStatus','renderRuntimeStatus','renderButtons','renderLightingElements','renderLiveDialogue','speakCommands'])h.context[name]=()=>{};
   vm.runInContext(source.slice(source.indexOf('function simulatorSnapshotIsCurrent('),source.indexOf('function normalizeLightingState(')),h.context);
   h.context.render(snapshot(boundary==='session'?'new-owner':'owner',boundary==='epoch'?2:1,boundary!=='hangup'));
  }
  assert.strictEqual(h.context.pcmPlaybackError,null,boundary+' clears the displayed error marker even after server stop released its owner');
  if(fresh)assert.strictEqual(h.fields['phone-error'].textContent,'Другая свежая ошибка',boundary+' preserves a fresh unrelated error');
  else{assert.strictEqual(h.fields['phone-error'].hidden,true,boundary+' removes old playback UI');assert.strictEqual(h.fields.fallback.textContent,'');}
 }
}
async function spokenRetry(){
 for(const fresh of [false,true]){
  const h=harness();h.failNext();await assert.rejects(h.context.enqueuePCM(ordinary()));h.context.handleAudioStop({generation:72});
  const fallback=ordinary(99,{generation:72,private:true,first:true,playback:{session_id:'owner',response_id:'PERSONAL_WELCOME',playback_id:'fallback',playback_lease:'fallback-lease',generation:4,token:99}});
  await h.context.enqueuePCM(fallback);assert.strictEqual(h.fields['phone-error'].hidden,false,'private fallback cannot clear the ordinary playback error');
  h.failNext();await assert.rejects(h.context.enqueuePCM(ordinary(13,{generation:72})));
  assert.strictEqual(h.fields['phone-error'].hidden,false,'failed retry cannot clear the error before source.start succeeds');
  if(fresh)h.context.showError(new Error('Другая свежая ошибка'));
  await h.context.enqueuePCM(ordinary(14,{generation:72}));
  assert.strictEqual(h.context.pcmPlaybackError,null,'successful new ordinary owner clears the old display marker without needing a text post');
  if(fresh)assert.strictEqual(h.fields['phone-error'].textContent,'Другая свежая ошибка','spoken recovery preserves a fresh unrelated error');
  else assert.strictEqual(h.fields['phone-error'].hidden,true,'spoken recovery clears the old matching playback UI');
  assert.strictEqual(h.context.failedPCMOwner.token,13,'successful new owner does not release the damaged turn fence');
 }
}
async function phonePostFencesLateError(){
 for(const fresh of [false,true]){
  const h=harness(),reject=h.rejectUnlock();
  Object.assign(h.context,{AbortController,current:null,session:()=> 'owner',recordLiveInput:()=>{},recordLiveResult:()=>{},fetchWithTimeout:async()=>({})});
  vm.runInContext(source.slice(source.indexOf('const MAX_PENDING_POSTS ='),source.indexOf('function recordLiveInput(')),h.context);
  const old=h.context.enqueuePCM(ordinary());old.catch(()=>{});await tick();
  await h.context.post('/stand/text',{text:'Новая попытка с телефона'});
  assert.strictEqual(h.context.audioGeneration,0,'actual phone text post does not change playback/barge-in generation');
  if(fresh)h.context.showError(new Error('Свежая ошибка новой попытки'));
  reject(new Error('previous turn unlock failed'));await assert.rejects(old,/previous turn unlock failed/);await tick();
  assert.strictEqual(h.visible.length,fresh?1:0,'actual post fences the previous turn error even without a form interrupt');
  if(fresh)assert.strictEqual(h.fields['phone-error'].textContent,'Свежая ошибка новой попытки');
  assert.strictEqual(h.context.failedPCMOwner.token,12,'late old failure may fence its own PCM but cannot revive its UI');
 }
}
(async()=>{await damagedOwner();await microphoneCopy();await staleRejection();await newTextAttempt();await errorAcrossServerStop();await displayedErrorCleanup();await spokenRetry();await phonePostFencesLateError();console.log('ordinary PCM failure: Russian retry/exit, exact owner fail-stop, queued/pending/active cleanup, next token, private/story isolation, stale error fence, stop→text retry, phone-post late-error fence, spoken retry and displayed-error cleanup passed');})().catch(error=>{console.error(error);process.exitCode=1;});
