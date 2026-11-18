"use strict";
const assert = require("assert");
const fs = require("fs");
const vm = require("vm");

const source = fs.readFileSync(process.argv[2], "utf8");
const start = source.indexOf("function handleAudioStop(");
const end = source.indexOf("function speakCommands(", start);
assert(start >= 0 && end > start, "browser audio functions missing");

const posts = [];
const timers = new Map();
let timerSequence = 0;
const timerWindow = {setTimeout: callback => {const id=++timerSequence;timers.set(id,callback);return id;}, clearTimeout: id=>timers.delete(id)};
const sources = [];
const audioContext = {
  currentTime: 10,
  decodeAudioData: async () => ({duration: 100}),
  createBufferSource: () => {
    const item = {
      buffer: null, onended: null, started: null, stopped: false,
      connect: () => {},
      start: function(...args) { this.started = args; },
      stop: function() { this.stopped = true; if (this.onended) this.onended(); },
    };
    sources.push(item);
    return item;
  },
  destination: {},
};
const context = vm.createContext({
  fetch: async () => ({ok: true, arrayBuffer: async () => new ArrayBuffer(4)}),
  apiPath: value => value,
  unlockAudio: async () => audioContext,
  audioContext,
  activeSources: new Set(),
  activeStoryPlayback: null, publicStoryBuffer: null, personalStoryPlayback: null, personalStoryReceiptTimer: null, stoppedStoryCheckpoint: null, stoppedStoryCheckpointTimer: null,
  window: timerWindow,
  storySpeechGeneration: 1,
  audioGeneration: 0,
  failedPCMOwner: null,
  minimumPCMGeneration: 0,
  lastSpokenCommand: 0,
  pendingPCM: [],
  nextAudioTime: 0,
  audioReady: true,
  current: {session: {node: "fixed_story_playing"}},
  $: () => ({textContent: ""}),
  post: async (path, payload) => { posts.push({path, payload}); return {}; },
  showError: error => { throw error; },
  liveSessionId: "sid",
});
vm.runInContext(source.slice(start, end), context);

function command(id, responseId, offsetMs) {
  return {payload: {
    session_id: "sid", response_id: responseId,
    playback_id: id, playback_lease: "lease-" + id,
    generation: 3, token: 7, offset_ms: offsetMs,
  }};
}
function ofPath(path) { return posts.filter(item => item.path === path); }
async function run() {
  const a = command("A", "fixed_aibolit_v1", 30000);
  await context.playStoryAsset(a, 1);
  assert.deepStrictEqual(sources[0].started, [0, 30], "story starts at canonical millisecond checkpoint");
  assert.strictEqual(ofPath("/stand/audio/lifecycle")[0].payload.playback_id, "A");
  audioContext.currentTime = 14.25;
  context.pauseStoryAsset();
  const checkpoints = ofPath("/stand/audio/checkpoint");
  assert.strictEqual(checkpoints.length, 1);
  assert.strictEqual(checkpoints[0].payload.offset_ms, 34250);
  assert.strictEqual(checkpoints[0].payload.playback_id, "A");
  assert.strictEqual(ofPath("/stand/audio/completed").length, 0, "pause cannot complete story");

  context.storySpeechGeneration = 2;
  const b = command("B", "fixed_aibolit_v1", 34250);
  await context.playStoryAsset(b, 2);
  assert.deepStrictEqual(sources[1].started, [0, 34.25], "resume uses canonical checkpoint");
  sources[0].onended();
  assert.strictEqual(ofPath("/stand/audio/completed").length, 0, "old completion cannot finish resume");
  sources[1].onended();
  const completions = ofPath("/stand/audio/completed");
  assert.strictEqual(completions.length, 1);
  assert.strictEqual(completions[0].payload.playback_id, "B");
  assert.strictEqual(completions[0].payload.playback_lease, "lease-B");

  context.storySpeechGeneration = 3;
  context.activeStoryPlayback = null;
  sources[1].onended();
  assert.strictEqual(ofPath("/stand/audio/completed").length, 1, "hangup fences callback");

  const phraseA = command("phrase-A", "PHRASE_084", 0);
  const phraseB = command("phrase-B", "PHRASE_084", 0);
  context.storySpeechGeneration = 4;
  await context.playPhraseAsset(phraseA, 4);
  context.storySpeechGeneration = 5;
  await context.playPhraseAsset(phraseB, 5);
  const finishedBefore = ofPath("/stand/audio/lifecycle").filter(x => x.payload.status === "finished").length;
  sources[2].onended();
  assert.strictEqual(ofPath("/stand/audio/lifecycle").filter(x => x.payload.status === "finished").length,
                     finishedBefore, "late phrase A cannot mark B");
  sources[3].onended();
  const finished = ofPath("/stand/audio/lifecycle").filter(x => x.payload.status === "finished");
  assert.strictEqual(finished.at(-1).payload.playback_id, "phrase-B");
  assert.strictEqual(ofPath("/stand/audio/completed").at(-1).payload.playback_id, "phrase-B");

  const ringtone = command("ringtone", "RINGTONE", 0);
  ringtone.payload.asset_path = "assets/sounds/ringtone.wav";
  context.storySpeechGeneration = 6;
  await context.playGenericAsset(ringtone, 6);
  assert.strictEqual(ofPath("/stand/audio/lifecycle").at(-1).payload.playback_id, "ringtone");
  sources.at(-1).onended();
  assert.strictEqual(ofPath("/stand/audio/completed").at(-1).payload.playback_lease, "lease-ringtone");
  const ringCompletions = ofPath("/stand/audio/completed").length;
  context.storySpeechGeneration = 7;
  sources.at(-1).onended();
  assert.strictEqual(ofPath("/stand/audio/completed").length, ringCompletions, "hangup fences ringtone callback");

  // Real wire order: snapshot(stop43, phrase44), then delayed stop43.
  // The duplicate used to invalidate the phrase while its WAV fetch awaited.
  context.lastSpokenCommand = 44;
  context.storySpeechGeneration = 8;
  const welcome = command("welcome-after-ring", "PHRASE_001", 0);
  const pendingWelcome = context.playPhraseAsset(welcome, 8);
  const beforeWelcome = sources.length;
  context.handleAudioStop({generation: 71, command_sequence: 43});
  await pendingWelcome;
  assert.strictEqual(sources.length, beforeWelcome + 1, "late ringtone stop cannot discard the welcome fetch");
  assert.strictEqual(context.storySpeechGeneration, 8);
  sources.at(-1).onended();
  assert.strictEqual(ofPath("/stand/audio/completed").at(-1).payload.playback_id, "welcome-after-ring");

  context.handleAudioStop({generation: 72, command_sequence: 45});
  assert.strictEqual(context.storySpeechGeneration, 9, "unconsumed stop still cancels immediately");
  assert.strictEqual(context.minimumPCMGeneration, 72);
  context.handleAudioStop({generation: 73});
  assert.strictEqual(context.storySpeechGeneration, 10, "direct PCM stop without a UART sequence still cancels");
  assert.strictEqual(context.minimumPCMGeneration, 73);

  // Expiry stop must capture the exact browser position before onended and
  // epoch cleanup discard the old execution. Duplicate command paths are inert.
  context.storySpeechGeneration = 20;
  context.lastSpokenCommand = 100;
  context.current = {session: {node: 'fixed_story_playing'}};
  const transferStory = command('transfer-story', 'fixed_aibolit_v1', 30000);
  audioContext.currentTime = 50;
  await context.playStoryAsset(transferStory, 20);
  audioContext.currentTime = 54.25;
  const request = {...context.activeStoryPlayback.identity,
    story_id: 'fixed_aibolit_v1', target_generation: 4};
  const checkpointsBefore = ofPath('/stand/audio/checkpoint').length;
  context.handleAudioStop({generation: 90, command_sequence: 101,
    checkpoint_request: {...request, playback_id: 'foreign'}});
  assert.strictEqual(ofPath('/stand/audio/checkpoint').length, checkpointsBefore,
    'foreign transfer cannot capture an unrelated playback');
  // Start it again after the deliberately foreign stop.
  context.storySpeechGeneration = 21;
  await context.playStoryAsset(transferStory, 21);
  audioContext.currentTime = 58.5;
  context.handleAudioStop({generation: 91, command_sequence: 102, checkpoint_request: request});
  const transferred = ofPath('/stand/audio/checkpoint').at(-1).payload;
  assert.strictEqual(transferred.offset_ms, 34250);
  assert.strictEqual(transferred.target_generation, 4);
  assert.strictEqual(transferred.playback_id, 'transfer-story');
  assert.strictEqual(context.activeStoryPlayback, null);
  const checkpointCount = ofPath('/stand/audio/checkpoint').length;
  context.checkpointPublicStoryTransfer(request);
  assert.strictEqual(ofPath('/stand/audio/checkpoint').length, checkpointCount,
    'snapshot copy cannot duplicate the one-shot transfer');

  // The snapshot command path must perform the same transfer before cleanup.
  const speakStart = source.indexOf('function speakCommands(');
  const speakEnd = source.indexOf('function interactionGuide(', speakStart);
  vm.runInContext(source.slice(speakStart, speakEnd), context);
  context.storySpeechGeneration = 22;
  await context.playStoryAsset(transferStory, 22);
  const snapshotBefore = ofPath('/stand/audio/checkpoint').length;
  context.interruptAudioForInput = () => {
    assert.strictEqual(ofPath('/stand/audio/checkpoint').length, snapshotBefore + 1,
      'snapshot stop must post exact checkpoint before destructive audio cleanup');
  };
  context.window.speechSynthesis = {cancel() {}};
  context.lastResultCount = 0;
  context.speakCommands({results: [], commands: [{type: 'audio_stop', sequence: 103,
    payload: {checkpoint_request: request}}]});
  assert.strictEqual(ofPath('/stand/audio/checkpoint').length, snapshotBefore + 1);

  // Real expiry order: an unsequenced PCM stop lands before the exact UART
  // checkpoint request. The second stop must use the position at the FIRST stop.
  context.storySpeechGeneration = 30;
  context.lastSpokenCommand = 200;
  audioContext.currentTime = 100;
  const delayed = command('unscoped-before-transfer', 'fixed_aibolit_v1', 30000);
  await context.playStoryAsset(delayed, 30);
  const delayedRequest = {...context.activeStoryPlayback.identity,
    story_id: 'fixed_aibolit_v1', target_generation: 4};
  audioContext.currentTime = 104.25;
  const beforeUnscoped = ofPath('/stand/audio/checkpoint').length;
  const completionsBeforeUnscoped = ofPath('/stand/audio/completed').length;
  context.handleAudioStop({generation: 94});
  assert.strictEqual(ofPath('/stand/audio/checkpoint').length, beforeUnscoped,
    'an ordinary stop has no authority to transfer a checkpoint');
  audioContext.currentTime = 107.25;
  context.handleAudioStop({generation: 95, command_sequence: 201,
    checkpoint_request: delayedRequest});
  assert.strictEqual(ofPath('/stand/audio/checkpoint').length, beforeUnscoped + 1,
    'exact transfer still works after an earlier unsequenced stop');
  assert.strictEqual(ofPath('/stand/audio/checkpoint').at(-1).payload.offset_ms, 34250,
    'checkpoint is frozen at first stop rather than including subsequent silence');
  assert.strictEqual(ofPath('/stand/audio/completed').length, completionsBeforeUnscoped,
    'stopped old source never gains completion authority');

  // The checkpoint-only receipt has the same bounded lifetime as the server
  // transfer. Its timer grants no late playback or completion authority.
  context.storySpeechGeneration = 40;
  await context.playStoryAsset(command('expiring-receipt', 'fixed_aibolit_v1', 30000), 40);
  const expiringRequest = {...context.activeStoryPlayback.identity,
    story_id: 'fixed_aibolit_v1', target_generation: 4};
  const beforeExpired = ofPath('/stand/audio/checkpoint').length;
  context.handleAudioStop({generation: 96});
  assert(context.stoppedStoryCheckpoint);
  timers.get(context.stoppedStoryCheckpointTimer)();
  assert.strictEqual(context.stoppedStoryCheckpoint, null);
  context.checkpointPublicStoryTransfer(expiringRequest);
  assert.strictEqual(ofPath('/stand/audio/checkpoint').length, beforeExpired);

  let uncachedFetches = 0;
  context.fetch = async () => {uncachedFetches++;return {ok: true, arrayBuffer: async () => new ArrayBuffer(4)};};
  for (const responseId of ['placeholder:fixed_story:fixed_aibolit_v1', 'PERSONAL_STORY']) {
    for (let attempt = 0; attempt < 2; attempt++) {
      const value = command('uncached-' + responseId + attempt, responseId, 1000);
      Object.assign(value.payload, {content_kind: 'story', asset_path: 'unverified.wav'});
      await context.playStoryAsset(value, ++context.storySpeechGeneration);
      assert.strictEqual(context.publicStoryBuffer, null, 'placeholder/personal content never enters the public cache');
    }
  }
  assert.strictEqual(uncachedFetches, 4, 'nonpublic positive-offset commands always fetch independently');

  let wireBody = null;
  const transport = vm.createContext({
    AbortController, session: () => "display-session",
    recordLiveInput: () => {}, recordLiveResult: () => {},
    fetchWithTimeout: async (_path, options) => {
      wireBody = JSON.parse(options.body);
      return options.readResponse({ok: true, text: async () => "{}"});
    },
    current: null, showError: error => { throw error; },
  });
  const postStart = source.indexOf("const MAX_PENDING_POSTS =");
  const postEnd = source.indexOf("function recordLiveInput(", postStart);
  vm.runInContext(source.slice(postStart, postEnd), transport);
  await transport.post("/stand/audio/completed", {session_id: "owner-session", playback_id: "B"});
  assert.strictEqual(wireBody.session_id, "owner-session",
                     "callback must retain immutable session instead of current UI selection");
}
run().then(() => console.log("browser fake Audio regressions passed")).catch(error => {
  console.error(error);
  process.exitCode = 1;
});
