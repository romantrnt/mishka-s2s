'use strict';
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const source = fs.readFileSync(process.argv[2], 'utf8');
const queueStart = source.indexOf('const MAX_PENDING_POSTS =');
const oldQueueStart = source.indexOf('let postQueue =');
const queueEnd = source.indexOf('function recordLiveInput(');
const fetchStart = source.indexOf('async function fetchWithTimeout(');
const fetchEnd = source.indexOf('async function refresh(', fetchStart);
assert(queueEnd > oldQueueStart && fetchEnd > fetchStart, 'console transport missing');
const tick = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
};
const response = (value = {}, status = 200) => ({
  ok: status < 400, status, text: async () => JSON.stringify(value),
});

function harness() {
  const requests = [], inputs = [], results = [], errors = [], timers = new Map();
  let sessionId = 'first-session';
  let timerId = 0, elapsed = 0;
  const context = vm.createContext({
    AbortController, FETCH_TIMEOUT_MS: 7000, current: null,
    session: () => sessionId, apiPath: path => path,
    recordLiveInput: (path, body) => inputs.push({path, body}),
    recordLiveResult: value => results.push(value),
    showError: error => errors.push(error),
    window: {
      setTimeout: (callback, delay) => { const id = ++timerId; timers.set(id, {callback, due: elapsed + delay}); return id; },
      clearTimeout: id => timers.delete(id),
    },
    // Deliberately ignore AbortSignal: late network work must be fenced even
    // when cancellation loses a race with a completed server request.
    fetch: (path, options) => {
      if (!options) return Promise.resolve({ok: true, arrayBuffer: async () => new ArrayBuffer(4)});
      const task = deferred();
      requests.push({path, options, ...task});
      return task.promise;
    },
  });
  vm.runInContext(source.slice(queueStart >= 0 ? queueStart : oldQueueStart, queueEnd), context);
  vm.runInContext(source.slice(fetchStart, fetchEnd), context);
  return {
    context, requests, inputs, results, errors, timers,
    selectSession: value => { sessionId = value; },
    expire: () => { for (const timer of [...timers.values()]) timer.callback(); },
    advance: milliseconds => { elapsed += milliseconds; for (const timer of [...timers.values()]) if (timer.due <= elapsed) timer.callback(); },
  };
}

function installBrowserHandlers(h) {
  const sources = [], buttons = [], fields = {};
  const audioContext = {
    currentTime: 10, destination: {},
    decodeAudioData: async () => ({duration: 100}),
    createBufferSource: () => {
      const item = {
        connect: () => {}, start: () => {},
        stop: () => { if (item.onended) item.onended(); },
      };
      sources.push(item);
      return item;
    },
  };
  Object.assign(h.context, {
    BUTTON_IDS: [1], buttonInputs: {}, buttonRefs: new Map(),
    performance: {now: () => 700},
    document: {createElement: () => ({
      setAttribute: () => {}, setPointerCapture: () => {},
      classList: {add: () => {}, remove: () => {}},
    })},
    $: id => id === 'buttons' ? {append: button => buttons.push(button)} : (fields[id] ||= {}),
    audioContext, unlockAudio: async () => audioContext,
    activeSources: new Set(), activeStoryPlayback: null, publicStoryBuffer: null, stoppedStoryCheckpoint: null, stoppedStoryCheckpointTimer: null,
    storySpeechGeneration: 1, audioReady: true,
    current: {session: {node: 'fixed_story_playing'}},
  });
  vm.runInContext(source.slice(source.indexOf('function releaseButton('), source.indexOf("$('enable-audio').onclick")), h.context);
  vm.runInContext(source.slice(source.indexOf('function handleAudioStop('), source.indexOf('function speakCommands(')), h.context);
  h.context.createButtons();
  return {sources, button: buttons[0]};
}

async function lifecycleUnderNormalBacklog() {
  const h = harness();
  const active = h.context.post('/stand/button', {button_id: 1, state: 'pressed'});
  const normal = Array.from({length: 16}, () => h.context.post('/stand/button', {button_id: 2}));
  const browser = installBrowserHandlers(h);
  const released = browser.button.onpointerup({preventDefault: () => {}});
  await tick(); // The rejected hold must still enqueue its finally release.
  const identity = id => ({
    session_id: 'audio-owner', response_id: id, playback_id: id,
    playback_lease: 'lease-' + id, generation: 3, token: 7,
  });
  await h.context.playPhraseAsset({payload: identity('phrase')}, 1);
  browser.sources[0].onended(); // Exercise the actual fire-and-forget terminal callers.
  await h.context.playStoryAsset({payload: identity('story')}, 1);
  h.context.pauseStoryAsset();
  const failed = h.context.post('/stand/audio/failed', {...identity('failed'), error_code: 'browser_phrase_playback_failed'});
  const cleanup = [
    ['/stand/button-released', null],
    ['/stand/audio/lifecycle', 'playing'],
    ['/stand/audio/lifecycle', 'finished'],
    ['/stand/audio/completed', null],
    ['/stand/audio/lifecycle', 'playing'],
    ['/stand/audio/checkpoint', null],
    ['/stand/audio/lifecycle', 'paused'],
    ['/stand/audio/failed', null],
  ];
  h.selectSession('different-ui-session');
  for (let i = 0; i < 17 + cleanup.length; i++) {
    await tick();
    assert(h.requests[i], 'normal backlog must retain release and all audio lifecycle callbacks');
    h.requests[i].resolve(response());
  }
  const values = await Promise.all([active, ...normal, released, failed]);
  await tick();
  assert(values.every(value => value !== null), 'lifecycle admission does not invalidate ordinary input');
  const admitted = h.requests.slice(17);
  assert.deepStrictEqual(admitted.map(item => [item.path, JSON.parse(item.options.body).status || null]), cleanup,
    'lifecycle callbacks keep FIFO including playing/finished/completed and checkpoint/paused');
  assert.strictEqual(JSON.parse(admitted[0].options.body).session_id, 'first-session');
  for (const item of admitted.slice(1)) {
    const payload = JSON.parse(item.options.body);
    assert.strictEqual(payload.session_id, 'audio-owner');
    assert.strictEqual(payload.playback_lease, 'lease-' + payload.playback_id);
  }
  assert.strictEqual(h.context.buttonInputs[1], 'released');
  assert.strictEqual(h.errors.length, 1, 'only overflowing ordinary hold is rejected');
  assert(!h.requests[0].options.signal.aborted, 'lifecycle admission is not destructive urgent cleanup');
  assert.strictEqual(h.timers.size, 0);
}

async function lifecycleCoalescing() {
  const h = harness();
  const active = h.context.post('/stand/button', {button_id: 1});
  const firstRelease = h.context.post('/stand/button-released', {button_id: 1, state: 'released'});
  for (let i = 0; i < 100; i++) {
    assert.strictEqual(h.context.post('/stand/button-released', {button_id: 1, state: 'released'}), firstRelease);
  }
  const nextPress = h.context.post('/stand/button', {button_id: 1});
  const nextRelease = h.context.post('/stand/button-released', {button_id: 1, state: 'released'});
  assert.notStrictEqual(nextRelease, firstRelease, 'new accepted press requires a later release');
  const owner = {session_id: 'audio-owner', response_id: 'phrase', playback_id: 'A', playback_lease: 'lease-A', generation: 3, token: 7};
  const finished = h.context.post('/stand/audio/lifecycle', {...owner, status: 'finished'});
  const completed = h.context.post('/stand/audio/completed', owner);
  const anotherOwner = h.context.post('/stand/audio/completed', {...owner, playback_lease: 'lease-B'});
  assert.notStrictEqual(completed, anotherOwner, 'different playback lease cannot coalesce');
  assert.strictEqual(h.context.post('/stand/audio/completed', owner), completed, 'equivalent callback retains exact owner');
  for (let i = 0; i < 7; i++) {
    await tick();
    assert(h.requests[i]);
    h.requests[i].resolve(response());
  }
  await Promise.all([active, firstRelease, nextPress, nextRelease, finished, completed, anotherOwner]);
  await tick();
  assert.deepStrictEqual(h.requests.slice(0, 4).map(item => item.path),
    ['/stand/button', '/stand/button-released', '/stand/button', '/stand/button-released']);
  assert.deepStrictEqual(h.errors, []);
}

async function lifecycleReserveBound() {
  const h = harness();
  const active = h.context.post('/stand/text', {text: 'pending'});
  const normal = Array.from({length: 16}, () => h.context.post('/stand/button', {button_id: 1}));
  const lifecycle = Array.from({length: 32}, (_, index) => h.context.post('/stand/audio/completed', {
    session_id: 'owner', response_id: 'phrase', playback_id: String(index),
    playback_lease: 'lease-' + index, generation: 3, token: 7,
  }));
  const overflow = h.context.post('/stand/audio/completed', {
    session_id: 'owner', response_id: 'phrase', playback_id: 'overflow',
    playback_lease: 'lease-overflow', generation: 3, token: 7,
  }).catch(error => error);
  await tick();
  assert((await overflow).message.includes('Очередь'));
  assert.strictEqual(h.errors.length, 1, 'lifecycle budget has an explicit independent bound');
  assert.strictEqual(h.requests.length, 1);
  const reset = h.context.post('/stand/reset');
  await tick();
  const cancelled = await Promise.all([active, ...normal, ...lifecycle]);
  assert(cancelled.every(value => value === null), 'urgent cleanup invalidates both bounded lanes');
  h.requests[1].resolve(response());
  await reset;
  assert.strictEqual(h.timers.size, 0);
}

async function backlogAndHangup() {
  const h = harness();
  const pending = h.context.post('/stand/text', {text: 'pending'});
  const outcomes = [];
  const buttons = Array.from({length: 100}, (_, id) =>
    h.context.post('/stand/button', {button_id: id % 7 + 1})
      .then(value => ({value}), error => { outcomes.push(error); return {error}; }));
  await tick();
  assert.strictEqual(outcomes.length, 84, 'only 16 ordinary requests may wait behind the active request');
  assert.deepStrictEqual(h.requests.map(item => item.path), ['/stand/text']);

  const hangup = h.context.post('/stand/handset', {state: 'down'});
  await tick();
  assert.strictEqual(h.requests[1]?.path, '/stand/handset', 'hangup bypasses stalled text and full ordinary queue');
  assert(h.requests[0].options.signal.aborted, 'obsolete in-flight request is aborted');
  assert.strictEqual(await pending, null, 'cancelled caller settles without waiting for network');
  const settled = await Promise.all(buttons);
  assert.strictEqual(settled.filter(item => item.value === null).length, 16, 'older queued commands are invalidated');

  h.selectSession('second-session');
  const body = {button_id: 2, nested: {value: 'at-enqueue'}};
  const next = h.context.post('/stand/button', body);
  body.nested.value = 'mutated';
  h.selectSession('third-session');
  await tick();
  assert.strictEqual(h.requests.length, 2, 'ordinary input waits for urgent cleanup');
  h.requests[0].resolve(response({obsolete: true}));
  await tick();
  assert.deepStrictEqual(h.results, [], 'late obsolete response cannot enter dialogue');
  h.requests[1].resolve(response({closed: true}));
  await hangup;
  await tick();
  const payload = JSON.parse(h.requests[2].options.body);
  assert.strictEqual(payload.session_id, 'second-session', 'session is captured at enqueue');
  assert.strictEqual(payload.nested.value, 'at-enqueue', 'queued body is an immutable wire snapshot');
  h.requests[2].resolve(response({button: true}));
  await next;
  assert.deepStrictEqual(JSON.parse(JSON.stringify(h.results)), [{closed: true}, {button: true}]);
  assert.strictEqual(h.timers.size, 0, 'completed and cancelled requests release deadlines');
}

async function cleanupOrdering() {
  const h = harness();
  const old = h.context.post('/stand/text', {text: 'pending'});
  await tick();
  const down = h.context.post('/stand/handset', {state: 'down'});
  const cancelled = h.context.post('/stand/button', {button_id: 1});
  const reset = h.context.post('/stand/reset');
  const repeatedReset = h.context.post('/stand/reset');
  const up = h.context.post('/stand/handset', {state: 'up'});
  await tick();
  assert.strictEqual(await old, null);
  assert.strictEqual(await cancelled, null);
  assert.deepStrictEqual(h.requests.map(item => item.path), ['/stand/text', '/stand/handset']);
  h.requests[1].resolve(response({down: true}));
  await down;
  await tick();
  assert.strictEqual(h.requests[2].path, '/stand/reset', 'reset retains order after active hangup');
  assert.strictEqual(h.requests.length, 3);
  h.requests[2].resolve(response({reset: true}));
  await Promise.all([reset, repeatedReset]);
  await tick();
  assert.strictEqual(h.requests[3].path, '/stand/handset');
  assert.strictEqual(JSON.parse(h.requests[3].options.body).state, 'up');
  h.requests[3].resolve(response({up: true}));
  await up;
  h.requests[0].reject(new Error('late obsolete network failure'));
  await tick();
  assert.deepStrictEqual(h.errors, [], 'obsolete errors are suppressed');
  assert.strictEqual(h.requests.length, 4, 'identical adjacent cleanup requests coalesce');
}

async function orderedRecoveryAndAudioIdentity() {
  const h = harness();
  const first = h.context.post('/stand/button', {button_id: 1}).catch(error => error);
  const second = h.context.post('/stand/audio/completed', {
    session_id: 'owner-session', playback_id: 'B', playback_lease: 'lease-B', generation: 3, token: 7,
  });
  const third = h.context.post('/stand/button-released', {button_id: 1});
  await tick();
  h.requests[0].resolve(response({}, 503));
  assert.strictEqual((await first).status, 503);
  await tick();
  assert.strictEqual(h.requests.length, 2, 'ordinary requests remain serial after failure');
  const payload = JSON.parse(h.requests[1].options.body);
  assert.strictEqual(payload.session_id, 'owner-session', 'audio callback retains canonical owner session');
  assert.strictEqual(payload.playback_lease, 'lease-B');
  h.requests[1].resolve(response());
  await second;
  await tick();
  assert.strictEqual(h.requests[2].path, '/stand/button-released');
  h.requests[2].resolve(response());
  await third;
  assert.strictEqual(h.errors.length, 1);
}

async function resetAndCleanupPressure() {
  const h = harness();
  const old = h.context.post('/stand/text', {text: 'pending'});
  await tick();
  const reset = h.context.post('/stand/reset');
  for (let i = 0; i < 100; i++) {
    assert.strictEqual(h.context.post('/stand/reset'), reset, 'repeated reset shares the active cleanup');
  }
  await tick();
  assert.strictEqual(await old, null);
  assert.strictEqual(h.requests[1].path, '/stand/reset', 'reset itself bypasses stalled ordinary input');
  const cleanups = Array.from({length: 100}, (_, index) =>
    (index % 2 ? h.context.post('/stand/reset') : h.context.post('/stand/handset', {state: 'down'}))
      .then(value => ({value}), error => ({error})));
  await tick();
  assert.strictEqual(h.requests.length, 2, 'only one urgent request is active');
  h.requests[1].resolve(response());
  await reset;
  for (let i = 0; i < 16; i++) {
    await tick();
    assert.strictEqual(h.requests.length, i + 3);
    h.requests[i + 2].resolve(response());
  }
  const outcomes = await Promise.all(cleanups);
  assert(outcomes.some(item => item.error), 'alternating urgent overflow is reported instead of growing backlog');
  assert.strictEqual(h.requests.length, 18, 'active requests and admitted cleanup remain bounded');
  assert.strictEqual(h.timers.size, 0);
}

async function inferenceDeadline() {
  const h = harness();
  const body = deferred();
  const text = h.context.post('/stand/text', {text: 'slow trained voice'});
  await tick();
  h.advance(7001);
  await tick();
  assert(!h.requests[0].options.signal.aborted, 'trained text generation survives the metadata deadline');
  h.requests[0].resolve({ok: true, status: 200, text: () => body.promise});
  await tick();
  h.advance(7001);
  await tick();
  assert(!h.requests[0].options.signal.aborted, 'text response body retains the longer total deadline');
  body.resolve('{"turn":"completed"}');
  assert.strictEqual((await text).turn, 'completed');
  await tick();
  assert.strictEqual(h.timers.size, 0);

  const metadata = h.context.fetchWithTimeout('/ready').catch(error => error);
  h.advance(7001);
  assert.strictEqual((await metadata).name, 'AbortError', 'metadata keeps its short bounded deadline');
  h.context.audioReconnectPending = true;
  const cleanup = h.context.post('/stand/reset').catch(error => error);
  assert.strictEqual(h.context.audioReconnectPending, false, 'urgent cleanup revokes reconnect recovery');
  await tick();
  h.advance(7001);
  assert.strictEqual((await cleanup).name, 'AbortError', 'cleanup keeps its short bounded deadline');
  await tick();
  assert.strictEqual(h.timers.size, 0);
}

async function modelProducingDeadlines() {
  for (const [path, body] of [
    ['/stand/simulator/action', {action: 'button', button_id: 1, press: 'press', epoch: 3}],
    ['/stand/simulator/action', {action: 'handset', state: 'up', epoch: 3}],
    ['/stand/button', {button_id: 1}], ['/stand/handset', {state: 'up'}],
    ['/stand/audio/completed', {response_id: 'PERSONAL_WELCOME'}],
    ['/stand/audio/failed', {response_id: 'PERSONAL_WELCOME'}],
  ]) {
    const h = harness();
    const task = h.context.post(path, body);
    await tick(); h.advance(7001); await tick();
    assert(!h.requests[0].options.signal.aborted, path + ' can await model output beyond metadata timeout');
    const down = h.context.post('/stand/simulator/action', {action: 'handset', state: 'down', epoch: 3});
    await tick();
    assert.strictEqual(await task, null, path + ' remains immediately cancellable by hangup');
    assert(h.requests[0].options.signal.aborted);
    h.requests[1].resolve(response()); await down;
  }
  const h = harness();
  const release = h.context.post('/stand/simulator/action', {action: 'button', button_id: 1, press: 'release', epoch: 3}).catch(error => error);
  h.advance(7001);
  assert.strictEqual((await release).name, 'AbortError', 'release-only callback keeps short deadline');
}

async function totalPostDeadline() {
  const h = harness();
  const body = deferred();
  const text = h.context.post('/stand/text', {text: 'pending body'}).catch(error => error);
  const next = h.context.post('/stand/button', {button_id: 1});
  await tick();
  h.requests[0].resolve({ok: true, status: 200, text: () => body.promise});
  await tick();
  assert.strictEqual(h.timers.size, 1, 'deadline remains active after response headers');
  h.expire();
  assert.strictEqual((await text).name, 'AbortError');
  await tick();
  assert.strictEqual(h.requests.length, 2, 'stalled response body cannot retain the queue');
  body.resolve('{"late":true}');
  h.requests[1].resolve(response());
  await next;
  assert.deepStrictEqual(JSON.parse(JSON.stringify(h.results)), [{}], 'timed-out body is fenced');
  assert.strictEqual(h.timers.size, 0);

  const header = h.context.post('/stand/text', {text: 'pending headers'}).catch(error => error);
  await tick();
  h.expire();
  assert.strictEqual((await header).name, 'AbortError');
  assert(h.requests.at(-1).options.signal.aborted);
  assert.strictEqual(h.timers.size, 0);
}

async function ignoredCallerErrors() {
  const h = harness();
  const unhandled = [];
  const onUnhandled = error => unhandled.push(error);
  process.on('unhandledRejection', onUnhandled);
  try {
    h.context.post('/stand/text', {text: 'ignored DOM return value'});
    for (let i = 0; i < 100; i++) h.context.post('/stand/button', {button_id: 1});
    await tick();
    h.requests[0].resolve(response({}, 503));
    await tick();
    const reset = h.context.post('/stand/reset');
    await tick();
    h.requests.at(-1).resolve(response());
    await reset;
    await tick();
    assert.strictEqual(unhandled.length, 0, 'ignored DOM handler promises do not emit unhandled rejections');
  } finally {
    process.removeListener('unhandledRejection', onUnhandled);
  }
}

async function run() {
  await lifecycleUnderNormalBacklog();
  await lifecycleCoalescing();
  await lifecycleReserveBound();
  await backlogAndHangup();
  await cleanupOrdering();
  await orderedRecoveryAndAudioIdentity();
  await resetAndCleanupPressure();
  await inferenceDeadline();
  await modelProducingDeadlines();
  await totalPostDeadline();
  await ignoredCallerErrors();
  console.log('console queue regression: backlog, urgent cleanup, stale fences, FIFO, audio identity and total deadlines passed');
}
run().catch(error => { console.error(error); process.exitCode = 1; });
