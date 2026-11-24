'use strict';
const assert=require('assert'),fs=require('fs'),vm=require('vm');
const source=fs.readFileSync('app/foundation/console/phone.js','utf8');
const context=vm.createContext({performance:{now:()=>0},Map,Set,Promise,Math,Date,Error,JSON});
vm.runInContext(source.slice(source.indexOf('class DeviceInput'),source.indexOf('function mountPhone')),context);
const DeviceInput=vm.runInContext('DeviceInput',context);
const consoleSource=fs.readFileSync('app/foundation/console/console.js','utf8');
const tick=()=>new Promise(r=>setImmediate(r));
function make(send){let now=0,snapshot={epoch:1,instance_id:'synthetic',session:{session_id:'mock',context_generation:1,node:'game_active'}};const errors=[];
 const input=new DeviceInput({send,snapshot:()=>snapshot,enabled:()=>true,change(){},error:e=>errors.push(String(e)),clock:()=>now});
 return {input,errors,advance:ms=>{now+=ms;},epoch:()=>{snapshot={...snapshot,epoch:snapshot.epoch+1};}};}
async function actualReceipts(){
 const sent=[],errors=[];
 const ctx=vm.createContext({Promise,Map,Set,JSON,Error,AbortController,setTimeout,clearTimeout,
  current:{session:{node:'game_active'},simulator:{epoch:1,session:{session_id:'mock'}}},
  wsSnapshotReady:true,wsReloadRequired:false,authBlocked:false,session:()=> 'mock',
  recordLiveInput(){},recordLiveResult(){},showError:error=>errors.push(error),
  ws:null,pageLifecycleStopped:false,document:{hidden:false},location:{protocol:'http:',host:'mock'},
  apiPath:path=>path,parseMessage:JSON.parse,FETCH_TIMEOUT_MS:1000,
  WebSocket:class {static OPEN=1;static CONNECTING=0;constructor(){this.readyState=1;}send(body){sent.push(JSON.parse(body));}},
 });
 vm.runInContext(consoleSource.slice(consoleSource.indexOf('const MAX_PENDING_POSTS ='),consoleSource.indexOf('function recordLiveInput(')),ctx);
 vm.runInContext(consoleSource.slice(consoleSource.indexOf('function connect()'),consoleSource.indexOf('async function fetchWithTimeout(')),ctx);
 ctx.connect();ctx.wsWriteOwner=true;ctx.wsSnapshotReady=true;
 const wire={action:'button',button_id:1,press:'press',gesture_id:'receipt',epoch:1};
 let settled=false;const receipt=ctx.post('/stand/simulator/action',wire).then(()=>{settled=true;});
 const first=sent[0],ack=(message,stage,extra={})=>ctx.ws.onmessage({data:JSON.stringify({type:'button_ack',
  request_id:message.request_id,button_id:message.button_id,gesture_id:message.gesture_id,stage,...extra})});
 await tick();assert(!settled,'socket.send is not admission');
 ack(first,'queued');await tick();assert(!settled,'queued is not completion');
 ack(first,'completed',{gesture_id:'other'});await tick();assert(!settled,'receipt must match gesture');
 ack(first,'completed');await receipt;assert(settled);
 const release=ctx.post('/stand/simulator/action',{...wire,press:'release'});ack(sent[1],'completed');await release;
 assert.strictEqual(vm.runInContext('wsButtonSockets.size + wsButtonAcks.size',ctx),0);
 const rejected=ctx.post('/stand/simulator/action',{...wire,gesture_id:'rejected'});
 ack(sent.at(-1),'rejected',{error:{code:'validation_error'}});await assert.rejects(rejected,/validation_error/);
 const lost=ctx.post('/stand/simulator/action',{...wire,gesture_id:'lost'});
 ctx.rejectButtonAcks(ctx.ws,Error('lost acknowledgement'));await assert.rejects(lost,/lost acknowledgement/);
 // A press queued behind an HTTP command must keep its terminal on HTTP too.
 vm.runInContext("activePost={owner:'other'}",ctx);sent.length=0;
 const queuedPress=ctx.post('/stand/simulator/action',{...wire,gesture_id:'http'});
 const queuedRelease=ctx.post('/stand/simulator/action',{...wire,gesture_id:'http',press:'release'});
 assert.strictEqual(sent.length,0,'release cannot switch an HTTP press to WS');
 const queued=vm.runInContext('postQueue',ctx);assert.strictEqual(queued.length,2);
 queued.forEach(item=>item.resolve(null));await Promise.all([queuedPress,queuedRelease]);
 assert(!errors.length);
}
(async()=>{
 {const sent=[];let pressAck;const h=make(body=>{sent.push(body);return body.press==='press'?new Promise(r=>pressAck=r):Promise.resolve();});
  assert(h.input.press(7,'pointer'));h.advance(10);await h.input.release(7,false,'pointer');
  assert.deepStrictEqual(sent.map(x=>x.press),['press','release'],'release must not wait for press ACK');
  assert(!h.input.pending.size);pressAck();await tick();}
 {const sent=[];let lost=true;const h=make(body=>{sent.push(body);if(body.press==='release'&&body.cancelled&&lost){lost=false;return Promise.reject(Error('lost cancel'));}return Promise.resolve();});
  h.input.press(7,'old');h.advance(6000);await h.input.release(7,true,'old');assert(h.input.pending.size===1);
  assert(h.input.press(7,'fresh'));h.advance(10);const done=h.input.release(7,false,'fresh');await done;await tick();
  assert.deepStrictEqual(sent.map(x=>x.press),['press','release','release','press','release']);
  assert(sent[1].cancelled&&sent[2].cancelled,'unknown terminal outcome retries CANCEL only');
  assert.strictEqual(sent[0].gesture_id,sent[2].gesture_id);assert.notStrictEqual(sent[0].gesture_id,sent[3].gesture_id);
  assert(!sent[4].cancelled&&!sent.some(x=>x.press==='hold'||x.press==='long_hold'));
  assert(!h.input.pending.size&&!h.input.active.size);}
 {const sent=[];const h=make(body=>{sent.push(body);return body.press==='press'?Promise.reject(Error('lost press ACK')):Promise.resolve();});
  h.input.press(1,'pointer');await tick();await tick();
  assert.deepStrictEqual(sent.map(x=>x.press),['press','release']);assert(sent[1].cancelled);
  assert(!h.input.active.size&&!h.input.pending.size,'uncertain press is canceled, never replayed');}
 {const sent=[];const h=make(body=>{sent.push(body);return Promise.resolve();});h.input.press(1,'a');
  assert(!h.input.press(1,'b'));await h.input.release(1,false,'b');assert(h.input.active.size===1);
  await h.input.release(1,false,'a');assert(sent.length===2);}
 {const sent=[];let lost=true,recoverAck;const h=make(body=>{sent.push(body);if(body.press==='release'&&lost){lost=false;return Promise.reject(Error('lost'));}if(body.press==='release'&&body.cancelled)return new Promise(r=>recoverAck=r);return Promise.resolve();});
  h.input.press(7);await h.input.release(7,true);h.input.press(7);const done=h.input.release(7);h.epoch();recoverAck();await done;
  assert(sent.filter(x=>x.press==='press').length===1,'old-context fresh intent cannot replay after epoch change');}
 await actualReceipts();
 console.log('5 DeviceInput boundaries + actual console ACK correlation/queue/HTTP transport boundaries PASS; synthetic promises and sockets only');
})().catch(e=>{console.error(e);process.exitCode=1});
