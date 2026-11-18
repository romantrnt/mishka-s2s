'use strict';
const assert = require('assert'), fs = require('fs'), vm = require('vm');
const root = process.argv[2] || 'app/foundation/console';
const source = fs.readFileSync(root + '/console.js', 'utf8');
const phone = fs.readFileSync(root + '/phone.js', 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));
const posts = [], sources = [], timers = new Map(), fields = {};
let clock = 10000, timerId = 0, storyFetches = 0, decodes = 0, decodeGate = null;
const field = id => (fields[id] ||= {value: 'stand', textContent: '', dataset: {}, open: false,
  classList: {add() {}, remove() {}, toggle() {}, contains() {return true;}},
  setAttribute() {}, addEventListener() {}, replaceChildren() {}, focus() {}, style: {removeProperty() {}}});
const audioContext = {state: 'running', currentTime: 100, destination: {},
  decodeAudioData: async () => {decodes++;if(decodeGate)await decodeGate;return {duration: 522.716};},
  createBufferSource() {
    const value = {connect() {}, start(...args) {this.started = args;}, stop() {this.onended?.();}};
    sources.push(value); return value;
  }};
const context = vm.createContext({console, AbortController, Map, Set, Date: {now: () => clock},
  performance: {now: () => clock}, setTimeout: fn => {const id=++timerId;timers.set(id,fn);return id;},
  clearTimeout: id => timers.delete(id), current: null, suppressPhoneAudio: false, audioReconnectPending: false,
  authBlocked: false, wsSnapshotReady: true, wsReloadRequired: false, mic: null, micStarting: false,
  audioRetry: null, firstSnapshotRendered: false, lastSpokenCommand: 0, lastResultCount: 0,
  audioReady: true, audioContext, activeSources: new Set(), activeStoryPlayback: null, publicStoryBuffer: null, personalStoryPlayback: null, personalStoryReceiptTimer: null,
  stoppedStoryCheckpoint: null, stoppedStoryCheckpointTimer: null, storySpeechGeneration: 0,
  audioGeneration: 0, minimumPCMGeneration: 0, pendingPCM: [], nextAudioTime: 0,
  audioQueueTail: Promise.resolve(), micInputGain: 1, postQueue: [], activePost: null,
  ws: null, pageLifecycleStopped: false, wsIntentionalClose: false,
  location: {protocol: 'https:', host: 'local.test'}, apiPath: path => path, session: () => 'stand',
  normalizeState: value => value, parseMessage: JSON.parse, $: field,
  document: {body: {dataset: {phone: 'true'}}, hidden: false,
    getElementById: field, querySelector: () => null, querySelectorAll: () => [], addEventListener() {}},
  window: {addEventListener() {}, speechSynthesis: {cancel() {}}},
  validateButtonContract: () => ({valid: true}), renderButtonLegend() {}, clearMic() {}, syncMicAdmission() {},
  buttonLegendSummary: () => '', microphoneAccepting: () => false,
  cardStatusProjection: () => ({label: '', tone: ''}), setRuntimeValue() {},
  renderLog() {}, renderHumanLog() {}, eventSummary() {}, resultSummary() {}, renderAudioStatus() {}, renderStoryStatus() {},
  renderRuntimeStatus() {}, deriveLEDState: () => ({}), normalizeLightingState: () => ({valid: true, state: {}}),
  renderButtons() {}, renderLightingElements() {}, renderLiveDialogue() {},
  startWebsocketHeartbeat() {}, stopWebsocketHeartbeat() {}, cancelOrdinaryPosts() {},
  scheduleReconnect() {}, schedulePolling() {}, stopPolling() {}, refresh: async () => {},
  fetch: async path => {if(path.startsWith('/stand/audio/story/'))storyFetches++;return {ok: true, arrayBuffer: async () => new ArrayBuffer(4)};}, unlockAudio: async () => audioContext,
  startMic: async () => {}, stopMic() {}, showError: error => {throw new Error(String(error));},
  post: async (path, payload) => {posts.push({path, payload});return {};},
  WebSocket: class {static OPEN=1;static CONNECTING=0;constructor() {this.readyState=1;}}
});
context.window.setTimeout = context.setTimeout; context.window.clearTimeout = context.clearTimeout;
const run = (start, end) => vm.runInContext(source.slice(source.indexOf(start), source.indexOf(end, source.indexOf(start))), context);
run('function simulatorSnapshotIsCurrent(', 'function normalizeLightingState(');
run('function handleAudioStop(', 'function interactionGuide(');
run('function interactionGuide(', 'function enqueuePCM(');
run('function interruptAudioForInput(', "$('text-form').onsubmit");
run('function connect()', 'async function fetchWithTimeout(');
vm.runInContext(source.slice(source.indexOf('window.MishkaConsole =')), context);
vm.runInContext(phone, context);
const identity = {session_id: 'stand', response_id: 'fixed_aibolit_v1',
  playback_id: 'old-story', playback_lease: 'old-lease', generation: 0, token: 4};
const command = (sequence, value=identity, offset=30000) => ({sequence, type: 'audio_play',
  payload: {...value, offset_ms: offset, content_kind: 'story', asset_path: 'approved.wav'}});
const request = {...identity, story_id: identity.response_id, target_generation: 1};
function snapshot(cursor, node, commands=[], epoch=0, up=true) {
  return {session: {state: up?'ACTIVE':'ENDED', handset_up: up, node}, canonical_state: {
      mode: node, public_story_checkpoint_pending: node==='story_paused'},
    simulator: {epoch, journal_cursor: cursor, session: {session_id: 'stand', node, state_version: cursor},
      microphone: {eligible: false}, admission: {input_ready: true, connected: true}, profile: 'anonymous',
      devices: {handset: {state: up?'up':'down'}, buttons: [], lighting: {}, rfid: {}, audio: {}}, fault: {}},
    commands, results: [], errors: [], events: [], latencies: [], raw_frames: [], lighting: {}};
}
(async () => {
  context.connect(); const socket = context.ws; socket.onopen();
  const wire = message => socket.onmessage({data: JSON.stringify(message)});
  wire({type: 'stand_state', state: snapshot(1, 'root_menu')});
  wire({type: 'stand_state', state: snapshot(2, 'fixed_story_playing', [command(40)])});
  await tick(); assert.strictEqual(sources.length, 1);
  wire({type: 'stand_state', state: snapshot(3, 'fixed_story_playing', [command(40)])});
  await tick(); assert.strictEqual(sources.length, 1, 'release snapshot never replays the story');
  audioContext.currentTime = 104.25;
  wire({type: 'audio_stop', generation: 4});
  assert.strictEqual(context.activeStoryPlayback, null);
  assert.strictEqual(context.stoppedStoryCheckpoint.offset_ms, 34250);
  assert.strictEqual('buffer' in context.stoppedStoryCheckpoint, false, 'receipt retains no decoded audio');
  audioContext.currentTime = 107.25;
  const stop = {sequence: 41, type: 'audio_stop', payload: {token: 5, checkpoint_request: request}};
  wire({type: 'stand_state', state: snapshot(4, 'story_paused', [command(40), stop])});
  await tick();
  const checkpoints = () => posts.filter(item => item.path === '/stand/audio/checkpoint');
  assert.strictEqual(checkpoints().length, 1, 'actual WS/render/phone sequence posts the transfer');
  assert.strictEqual(checkpoints()[0].payload.offset_ms, 34250);
  assert.strictEqual(checkpoints()[0].payload.target_generation, 1);
  wire({type: 'audio_stop', generation: 5, command_sequence: 41, checkpoint_request: request});
  assert.strictEqual(checkpoints().length, 1, 'later UART notification cannot transfer twice');
  const next = {...identity, generation: 1, token: 6, playback_id: 'new-story', playback_lease: 'new-lease'};
  wire({type: 'stand_state', state: snapshot(5, 'fixed_story_playing', [stop, command(42, next, 34250)])});
  await tick(); assert.deepStrictEqual(sources[1].started, [0, 34.25]);
  assert.strictEqual(storyFetches, 1, 'new anonymous lease reuses the same public story WAV');
  assert.strictEqual(decodes, 1, 'new anonymous lease reuses the decoded public buffer');
  assert.deepStrictEqual(Object.keys(context.publicStoryBuffer).sort(), ['assetPath', 'buffer', 'storyId'],
    'public cache retains no old execution, identity or callback');
  sources[0].onended();
  assert(!posts.some(item => item.path === '/stand/audio/completed' && item.payload.playback_id === 'old-story'));

  // The normal pause path also requires a new owned command, with no second WAV/decode.
  audioContext.currentTime = 110;
  wire({type: 'stand_state', state: snapshot(6, 'story_pause_announcement', [{sequence: 43, type: 'audio_pause'}])});
  assert.strictEqual(checkpoints().length, 2);
  const resumed = {...next, token: 7, playback_id: 'pause-resume', playback_lease: 'pause-resume-lease'};
  wire({type: 'stand_state', state: snapshot(7, 'fixed_story_playing', [command(44, resumed, 37000)])});
  await tick();assert.deepStrictEqual(sources[2].started, [0, 37]);
  assert.strictEqual(storyFetches, 1);assert.strictEqual(decodes, 1);
  sources[1].onended();
  assert(!posts.some(item => item.path === '/stand/audio/completed' && item.payload.playback_id === next.playback_id));

  const startOwned = async (sequence, value, offset, asset='approved.wav') => {
    const play = command(sequence, value, offset);play.payload.asset_path = asset;
    wire({type: 'stand_state', state: snapshot(sequence, 'fixed_story_playing', [
      {sequence: sequence - 1, type: 'audio_stop', payload: {}}, play])});
    await tick();
  };
  await startOwned(46, {...resumed, playback_id: 'changed-asset'}, 37000, 'approved-v2.wav');
  assert.strictEqual(storyFetches, 2, 'changed asset never reuses an earlier decoded buffer');
  await startOwned(48, {...resumed, response_id: 'fixed_squirrel_v1', playback_id: 'different-story'}, 37000);
  assert.strictEqual(storyFetches, 3, 'same asset label cannot alias another story');
  await startOwned(50, {...resumed, response_id: 'fixed_squirrel_v1', playback_id: 'fresh-story'}, 0);
  assert.strictEqual(storyFetches, 4, 'a fresh story always fetches and decodes again');
  wire({type: 'stand_state', state: snapshot(51, 'fixed_story_menu', [{sequence: 51, type: 'audio_stop', payload: {}}])});
  assert.strictEqual(context.publicStoryBuffer, null, 'leaving the story releases its decoded buffer');
  await startOwned(54, {...resumed, response_id: 'fixed_squirrel_v1', playback_id: 'after-exit'}, 37000);
  assert.strictEqual(storyFetches, 5, 'resume cannot reuse a buffer from an exited story');

  wire({type: 'audio_stop', generation: 6});
  assert(context.stoppedStoryCheckpoint);
  // Actual mounted-phone cleanup on remote epoch/session replacement destroys
  // the checkpoint-only receipt too; old transfer cannot cross this boundary.
  wire({type: 'stand_state', state: snapshot(55, 'session_closed', [], 1, false)});
  assert.strictEqual(context.stoppedStoryCheckpoint, null);
  assert.strictEqual(context.publicStoryBuffer, null, 'terminal mounted-phone cleanup releases the public buffer');
  const newRequest = {...next, story_id: next.response_id, target_generation: 2};
  wire({type: 'audio_stop', generation: 7, checkpoint_request: newRequest});
  assert.strictEqual(checkpoints().length, 2);

  wire({type: 'stand_state', state: snapshot(56, 'root_menu', [], 1)});
  const owned = {...resumed, generation: 2, playback_id: 'after-terminal'};
  wire({type: 'stand_state', state: snapshot(57, 'fixed_story_playing', [command(56, owned, 37000)], 1)});
  await tick();assert.strictEqual(storyFetches, 6, 'terminal cleanup forces a new fetch even at a positive offset');
  assert.strictEqual(decodes, 6);

  // An already-started decode cannot repopulate the cache after actual Reset cleanup.
  let releaseDecode;decodeGate = new Promise(resolve => {releaseDecode=resolve;});
  wire({type: 'stand_state', state: snapshot(59, 'fixed_story_playing', [
    {sequence: 58, type: 'audio_stop', payload: {}}, command(59, {...owned, playback_id: 'pending-decode'}, 0)], 1)});
  await tick();const beforeReset = sources.length;
  await fields['phone-reset'].onclick();
  releaseDecode();await tick();decodeGate = null;
  assert.strictEqual(context.publicStoryBuffer, null);
  assert.strictEqual(sources.length, beforeReset, 'late decode after Reset creates no source');
  console.log('actual WebSocket/render/phone transfer, public buffer reuse, new leases and terminal/pending-decode cleanup passed');
})().catch(error => {console.error(error);process.exitCode=1;});
