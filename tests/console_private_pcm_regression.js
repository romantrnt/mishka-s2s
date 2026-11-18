'use strict';
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(process.argv[2], 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));

function harness() {
  const posts = [], sources = [], errors = [];
  let failStart = false, permission;
  const audioContext = {
    state: 'running', currentTime: 10, destination: {},
    createBuffer: (_channels, count, rate) => ({duration: count / rate, getChannelData: () => new Float32Array(count)}),
    createBufferSource: () => {
      const item = {connect: () => {},
        start: time => { if (failStart) throw new Error('start failed'); item.started = time; },
        stop: () => { item.onended?.(); },
      };
      sources.push(item);
      return item;
    },
  };
  const context = vm.createContext({
    audioQueueTail: Promise.resolve(), audioGeneration: 0, storySpeechGeneration: 0,
    pendingPCM: [], audioContext, nextAudioTime: 0, activeSources: new Set(),
    stoppedStoryCheckpoint: null, stoppedStoryCheckpointTimer: null, window: {clearTimeout() {},setTimeout() {}},
    activeStoryPlayback: null, publicStoryBuffer: null, audioReady: true, micInputGain: 1,
    minimumPCMGeneration: 0, lastSpokenCommand: 0,
    unlockAudio: async () => { if (permission) await permission; return audioContext; },
    atob: value => Buffer.from(value, 'base64').toString('binary'),
    $: () => ({}), showError: error => errors.push(error),
    post: async (path, payload) => { posts.push({path, payload}); },
  });
  const start = source.indexOf('function enqueuePCM('), end = source.indexOf('function initDiagnostics(', start);
  vm.runInContext(source.slice(start, end), context);
  const stopStart = source.indexOf('function handleAudioStop('), stopEnd = source.indexOf('function audioIdentity(', stopStart);
  vm.runInContext(source.slice(stopStart, stopEnd), context);
  const interruptStart = source.indexOf('function interruptAudioForInput('), interruptEnd = source.indexOf("$('text-form').onsubmit", interruptStart);
  vm.runInContext(source.slice(interruptStart, interruptEnd), context);
  return {context, posts, sources, errors, audioContext,
    failStart: () => { failStart = true; },
    holdUnlock: () => { let release; permission = new Promise(resolve => { release = resolve; }); return release; },
  };
}
function frame(flags = {}) {
  return {type: 'audio_pcm_output', session_id: 'owner', generation: 71,
    private: true, first: false, final: false,
    sample_rate: 16000, data_b64: 'AABAIA==',
    playback: {session_id: 'owner', response_id: 'PERSONAL_WELCOME',
      playback_id: 'play-1', playback_lease: 'lease-1', generation: 4, token: 12}, ...flags};
}
(async () => {
  const h = harness();
  const first = frame({first: true});
  await h.context.enqueuePCM(first);
  assert.strictEqual(h.posts.length, 1, 'first actual source start emits playing');
  assert.strictEqual(h.posts[0].path, '/stand/audio/lifecycle');
  assert.strictEqual(h.posts[0].payload.status, 'playing');
  assert.strictEqual(h.posts[0].payload.generation, 4, 'lifecycle uses canonical generation, not transport generation');
  const final = frame({final: true});
  const expected = {...final.playback};
  await h.context.enqueuePCM(final);
  assert.strictEqual(h.posts.length, 1, 'receiving/scheduling final PCM cannot complete canonical playback');
  h.sources[0].onended();
  assert.strictEqual(h.posts.length, 1, 'nonfinal source cannot complete');
  final.playback.playback_id = 'mutated-after-scheduling';
  h.sources[1].onended();
  assert.strictEqual(h.posts[1].payload.status, 'finished');
  assert.deepStrictEqual(JSON.parse(JSON.stringify(h.posts[2].payload)), expected, 'completion retains immutable original full owner');
  assert.strictEqual(h.posts[2].path, '/stand/audio/completed');
  h.sources[1].onended();
  assert.strictEqual(h.posts.length, 3, 'duplicate source callback cannot complete twice');

  for (const cancel of [context => context.interruptAudioForInput(), context => context.handleAudioStop({generation: 72})]) {
    const c = harness();
    await c.context.enqueuePCM(frame({first: true, final: true}));
    cancel(c.context);
    c.sources[0].onended();
    assert.strictEqual(c.posts.length, 1, 'hangup/reset/transport stop fences terminal callbacks');
    const release = c.holdUnlock();
    const late = c.context.enqueuePCM(frame({first: true, final: true}));
    await tick();
    cancel(c.context);
    release();
    await late;
    assert.strictEqual(c.sources.length, 1, 'late unlock cannot start canceled PCM');
  }

  const legacy = harness();
  const plain = frame({private: false}); delete plain.playback;
  await legacy.context.enqueuePCM(plain); legacy.sources[0].onended();
  assert.strictEqual(legacy.posts.length, 0, 'legacy PCM without playback metadata preserves existing behavior');

  const failure = harness(); failure.failStart();
  await assert.rejects(failure.context.enqueuePCM(frame({first: true, final: true})), /start failed/);
  assert.strictEqual(failure.posts.length, 1);
  assert.strictEqual(failure.posts[0].path, '/stand/audio/failed');
  assert.strictEqual(failure.posts[0].payload.error_code, 'browser_private_pcm_failed');
  assert.strictEqual(failure.posts[0].payload.playback_lease, 'lease-1');
  assert(!failure.posts.some(item => item.path === '/stand/audio/completed'));
  const decode = harness();
  decode.context.atob = () => { throw new Error('decode failed'); };
  await assert.rejects(decode.context.enqueuePCM(frame({first: true, final: true})), /decode failed/);
  assert.strictEqual(decode.posts[0].payload.error_code, 'browser_private_pcm_failed');
  assert.strictEqual(decode.sources.length, 0);
  const malformed = harness();
  const wrongOwner = frame(); wrongOwner.playback.session_id = 'other';
  await assert.rejects(malformed.context.enqueuePCM(wrongOwner), /identity is invalid/);
  assert.strictEqual(malformed.sources.length, 0, 'mismatched private owner cannot play');
  assert.strictEqual(malformed.posts.length, 0);
  console.log('private PCM browser lifecycle: actual start/end, owner identity, cancellation, late unlock, legacy PCM and failure passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
