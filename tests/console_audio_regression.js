"use strict";
const assert = require("assert");
const fs = require("fs");
const vm = require("vm");

const source = fs.readFileSync(process.argv[2], "utf8");
const start = source.indexOf("function audioIdentity(");
const end = source.indexOf("function speakCommands(", start);
assert(start >= 0 && end > start, "browser audio functions missing");

const posts = [];
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
  activeStoryPlayback: null,
  storySpeechGeneration: 1,
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

  let wireBody = null;
  const transport = vm.createContext({
    postQueue: Promise.resolve(), session: () => "display-session",
    recordLiveInput: () => {}, recordLiveResult: () => {},
    fetchWithTimeout: async (_path, options) => {
      wireBody = JSON.parse(options.body);
      return {ok: true, text: async () => "{}"};
    },
    current: null, showError: error => { throw error; },
  });
  const postStart = source.indexOf("function post(path, body = {}) {");
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
