'use strict';
const assert = require('assert'), fs = require('fs'), vm = require('vm');
const source = fs.readFileSync('app/foundation/console/console.js', 'utf8');
const posts = [], sources = [], timers = new Map();let timerId=0, clockMs=0;
const audioContext = {state: 'running', currentTime: 10, destination: {},
  createBuffer: (_channels, count, rate) => ({duration: count / rate, getChannelData: () => new Float32Array(count)}),
  createBufferSource() {
    const value = {connect() {}, start(time) {this.started=time;}, stop() {this.stopped=true;this.onended?.();}};
    sources.push(value);return value;
  }};
const context = vm.createContext({console, Set, Map, Date:{now:()=>clockMs},
  audioContext, audioReady: true, activeSources: new Set(), activeStoryPlayback: null,
  publicStoryBuffer: null, personalStoryPlayback: null, personalStoryReceiptTimer: null, stoppedStoryCheckpoint: null, stoppedStoryCheckpointTimer: null,
  audioQueueTail: Promise.resolve(), audioGeneration: 0, storySpeechGeneration: 0, failedPCMOwner: null, pcmTextAttempt: 0, clearPCMPlaybackError() {},
  minimumPCMGeneration: 0, lastSpokenCommand: 0, pendingPCM: [], nextAudioTime: 0, micInputGain: 1,
  current: {session: {node: 'personal_story_playing', handset_up: true}},
  window: {clearTimeout(id) {timers.delete(id);}, setTimeout(fn) {const id=++timerId;timers.set(id,fn);return id;}}, $: () => ({}),
  unlockAudio: async () => audioContext,
  atob: value => Buffer.from(value, 'base64').toString('binary'),
  post: async (path,payload) => {posts.push({path,payload});return {};}, showError: error => {throw error;},
});
function load(start,end) {vm.runInContext(source.slice(source.indexOf(start),source.indexOf(end,source.indexOf(start))),context);}
load('function interactionActive(', 'function sessionActionIsCurrent(');
load('function handleAudioStop(', 'function speakCommands(');
load('function enqueuePCM(', 'function initDiagnostics(');
load('function interruptAudioForInput(', "$('text-form').onsubmit");
async function operate(value) {
  if (value.node) context.current.session.node=value.node;
  if (value.time !== undefined) audioContext.currentTime=value.time;
  if (value.op==='frames') for (const message of value.frames) await context.enqueuePCM(message);
  if (value.op==='pause') context.pauseStoryAsset();
  if (value.op==='stop') context.handleAudioStop(value.message);
  if (value.op==='end') for (const index of value.indices) sources[index].onended?.();
  if (value.op==='cleanup') context.interruptAudioForInput();
  if (value.op==='expire_receipt') {clockMs+=5000;for(const fn of [...timers.values()])fn();}
  return {posts: posts.splice(0), active: context.activeSources.size,
    starts: sources.map(item=>item.started), stopped: sources.map(item=>Boolean(item.stopped)),
    publicCached: Boolean(context.publicStoryBuffer), personalRetained: Boolean(context.personalStoryPlayback),
    personalHasAudio: Boolean(context.personalStoryPlayback?.source || context.personalStoryPlayback?.buffer)};
}
function frame(identity,first,final) {
  return {type:'audio_pcm_output', private:true, session_id:identity.session_id,
    playback:identity, token:identity.token, generation:3, sample_rate:16000,
    first, final, story_offset_ms:1000, story_duration_ms:5000,
    data_b64:Buffer.alloc(16000).toString('base64')};
}
async function regression() {
  const identity={session_id:'story',response_id:'PERSONAL_STORY',generation:2,
    playback_id:'old',playback_lease:'old-lease',token:7};
  await operate({op:'frames',frames:[frame(identity,true,false),frame(identity,false,true)]});
  const paused=await operate({op:'pause',node:'story_pause_announcement',time:10.25});
  assert.strictEqual(paused.active,0,'pause stops all scheduled private story chunks');
  const checkpoint=paused.posts.find(item=>item.path==='/stand/audio/checkpoint');
  assert(checkpoint,'private PCM pause reports its exact old lease checkpoint');
  assert.strictEqual(checkpoint.payload.offset_ms,1250,'future scheduled chunk is not counted as played');
  assert.strictEqual(checkpoint.payload.token,7);
  let result=await operate({op:'frames',frames:[frame(identity,true,true)],node:'personal_story_playing'});
  assert.strictEqual(result.active,0,'late old first/final cannot restart paused audio');
  const next={...identity,playback_id:'new',playback_lease:'new-lease',token:8};
  const resumed=frame(next,true,true);resumed.story_offset_ms=1250;resumed.generation=4;
  result=await operate({op:'frames',frames:[resumed]});
  assert.strictEqual(result.active,1,'new owned stream starts after pause');
  result=await operate({op:'end',indices:[0,1]});
  assert(!result.posts.some(item=>item.path==='/stand/audio/completed'),'old callbacks cannot finish new stream');
  await operate({op:'pause',node:'story_pause_announcement',time:10.5});
  result=await operate({op:'frames',frames:[frame(identity,true,true)],node:'personal_story_playing'});
  assert.strictEqual(result.active,0,'an older paused lease cannot revive while another resume is pending');
  result=await operate({op:'cleanup'});
  assert.strictEqual(result.active,0);assert(!result.personalRetained);assert(!result.publicCached);

  const gapIdentity={...identity,playback_id:'gap',playback_lease:'gap-lease'};
  const beforeGap=sources.length;
  await operate({op:'frames',frames:[frame(gapIdentity,true,false)],time:20});
  await operate({op:'end',indices:[beforeGap],time:21.5});
  await operate({op:'frames',frames:[frame(gapIdentity,false,true)]});
  // The actual transport may deliver an unscoped stop before UART pause.
  await operate({op:'stop',message:{generation:4},time:21.75});
  result=await operate({op:'pause',node:'story_pause_announcement',time:25});
  assert.strictEqual(result.posts.find(item=>item.path==='/stand/audio/checkpoint').payload.offset_ms,1750,
    'position excludes both a scheduling gap and silence after the first stop');
  assert.strictEqual(result.active,0);
  result=await operate({op:'pause'});assert.strictEqual(result.posts.length,0,'duplicate pause sends no second checkpoint');
  await operate({op:'cleanup'});
  const tail=frame({...identity,playback_id:'tail',playback_lease:'tail-lease'},true,true);
  tail.generation=5;
  tail.data_b64=Buffer.alloc(4*16000*2).toString('base64');
  await operate({op:'frames',node:'personal_story_playing',frames:[tail],time:30});
  result=await operate({op:'end',indices:[sources.length-1],time:34});
  assert(result.personalRetained && !result.personalHasAudio,'natural completion retains only a short position receipt');
  result=await operate({op:'expire_receipt'});assert(!result.personalRetained,'completed receipt is removed after five seconds');
  result=await operate({op:'pause',node:'story_pause_announcement'});
  assert.strictEqual(result.posts.length,0);assert.strictEqual(result.active,0,'expired receipt cannot checkpoint or restart audio');
  await operate({op:'cleanup'});
  const growing=frame({...identity,playback_id:'growing',playback_lease:'growing-lease'},true,true);
  growing.generation=6;growing.story_duration_ms=null;
  growing.story_streaming=true;growing.story_duration_limit_ms=180000;
  await operate({op:'frames',node:'personal_story_playing',frames:[growing],time:40});
  await operate({op:'end',indices:[sources.length-1],time:40.5});
  result=await operate({op:'pause',node:'story_pause_announcement',time:40.6});
  assert.strictEqual(result.posts.find(item=>item.path==='/stand/audio/checkpoint').payload.offset_ms,1500,
    'a growing stream retains actual played position, never a null duration as checkpoint');
  await operate({op:'cleanup'});
  const tiny=frame({...identity,playback_id:'tiny',playback_lease:'tiny-lease'},true,false);
  tiny.generation=7;tiny.story_duration_ms=null;tiny.story_streaming=true;tiny.story_duration_limit_ms=180000;
  tiny.data_b64=Buffer.alloc(15*2).toString('base64');
  const sentinel={...tiny,first:false,final:true,stream_end:true,data_b64:Buffer.alloc(2).toString('base64')};
  const tinyStart=sources.length;
  await operate({op:'frames',node:'personal_story_playing',frames:[tiny,sentinel],time:60});
  await operate({op:'end',indices:[tinyStart,tinyStart+1],time:60.1});
  result=await operate({op:'pause',node:'story_pause_announcement',time:60.2});
  assert.strictEqual(result.posts.find(item=>item.path==='/stand/audio/checkpoint').payload.offset_ms,1000,
    'a stream end sentinel is not a sample of the story or an extra millisecond');
  console.log('personal PCM pause, exact scheduled position, late chunks/new lease and cleanup passed');
}
if (process.argv.includes('--bridge')) {
  const lines=require('readline').createInterface({input:process.stdin});
  let tail=Promise.resolve();
  lines.on('line',line=>{tail=tail.then(async()=>{
    try {process.stdout.write(JSON.stringify(await operate(JSON.parse(line)))+'\n');}
    catch(error) {process.stdout.write(JSON.stringify({error:error.message})+'\n');}
  });});
} else regression().catch(error=>{console.error(error);process.exitCode=1;});
