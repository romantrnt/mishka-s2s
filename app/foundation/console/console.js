const $ = id => document.getElementById(id);
const BUTTON_IDS = [1, 2, 3, 4, 5, 6, 7];
const LIGHTING_ELEMENT_IDS = Array.from({ length: 18 }, (_, id) => id);
const ELEMENT_PRESENTATION = {
  0: { kind: 'digit', buttonId: 7, color: 'white' }, 1: { kind: 'fill', buttonId: 7, color: 'purple' },
  2: { kind: 'digit', buttonId: 6, color: 'white' }, 3: { kind: 'fill', buttonId: 6, color: 'blue' },
  4: { kind: 'symbol', buttonId: 6, symbol: 'bear', color: 'brown' }, 5: { kind: 'compensation', buttonId: 6, symbol: 'bear', color: 'blue' },
  6: { kind: 'fill', buttonId: 5, color: 'cyan' }, 7: { kind: 'digit', buttonId: 5, color: 'white' },
  8: { kind: 'digit', buttonId: 4, color: 'white' }, 9: { kind: 'fill', buttonId: 4, color: 'green' },
  10: { kind: 'fill', buttonId: 3, color: 'yellow' }, 11: { kind: 'digit', buttonId: 3, color: 'white' },
  12: { kind: 'digit', buttonId: 2, color: 'white' }, 13: { kind: 'fill', buttonId: 2, color: 'orange' },
  14: { kind: 'fill', buttonId: 1, color: 'red' }, 15: { kind: 'digit', buttonId: 1, color: 'white' },
  16: { kind: 'compensation', buttonId: 7, symbol: 'weather', color: 'purple' }, 17: { kind: 'symbol', buttonId: 7, symbol: 'weather', color: 'skyblue' },
};
const ELEMENTS_BY_BUTTON = Object.fromEntries(BUTTON_IDS.map(buttonId => [
  buttonId, Object.fromEntries(Object.entries(ELEMENT_PRESENTATION)
    .filter(([, item]) => item.buttonId === buttonId)
    .map(([elementId, item]) => [item.kind, Number(elementId)])),
]));
const LED_COLORS = {
  off: { label: 'OFF', className: 'led-off', css: '#8f999a' },
  invalid: { label: 'INVALID', className: 'led-invalid', css: '#d45555' },
  red: { className: 'led-red', css: '#e04b42' },
  orange: { className: 'led-orange', css: '#f39a3d' },
  yellow: { className: 'led-yellow', css: '#e8c34a' },
  green: { className: 'led-green', css: '#4dbb6e' },
  cyan: { className: 'led-cyan', css: '#45c8d6' },
  blue: { className: 'led-blue', css: '#4c93e7' },
  purple: { className: 'led-purple', css: '#a26de8' },
};
let current = null;
let lastSpokenCommand = 0;
let lastResultCount = 0;
let storySpeechGeneration = 0;
let activeStoryPlayback = null;
let publicStoryBuffer = null;
let personalStoryPlayback = null;
let personalStoryReceiptTimer = null;
let stoppedStoryCheckpoint = null;
let stoppedStoryCheckpointTimer = null;
let mic = null;
let micGeneration = 0;
let micStarting = false;
let micOwner = null;
let audioRetry = null;
let ws = null;
let wsSnapshotReady = false;
let wsReloadRequired = false;
let wsHeartbeatTimer = null;
let reconnectAttempt = 0;
let reconnectTimer = null;
let reconnectDelayOverride = 0;
let audioContext = null;
let audioReady = false;
let nextAudioTime = 0;
const AudioContextCtor = window.AudioContext || window.webkitAudioContext;
const activeSources = new Set();
const MIC_DUCK_GAIN = 0.063;
const MIC_DUCK_ATTACK = 0.75;
const MIC_DUCK_RELEASE = 0.35;
let micInputGain = 1;
let audioGeneration = 0;
let failedPCMOwner = null;
let pcmPlaybackError = null;
let pcmTextAttempt = 0;
let minimumPCMGeneration = 0;
let suppressPhoneAudio = false;
let audioReconnectPending = false;
let audioQueueTail = Promise.resolve();
const pendingPCM = [];
const pendingTextInputs = [];
const buttonInputs = Object.fromEntries(BUTTON_IDS.map(id => [id, 'released']));
const buttonInputOwners = new Map();
const buttonRefs = new Map();
let liveClientEntries = [];
let liveSessionId = null;
const liveResponseIds = new Set();
let healthSnapshot = null;
let readinessSnapshot = null;
let firstSnapshotRendered = false;
let refreshInFlight = null;
let refreshAbortController = null;
let refreshTimer = null;
let refreshBackoff = 0;
let wsIntentionalClose = false;
let pageLifecycleStopped = false;
const FETCH_TIMEOUT_MS = 7000;
const POLL_FALLBACK_MS = 5000;
const HEALTH_REFRESH_MS = 15000;
const WS_HEARTBEAT_MS = 15000;
let lastHealthRefresh = 0;
let authBlocked = false;
let stateSnapshotAvailable = false;

const session = () => $('session').value.trim() || 'stand';

async function unlockAudio() {
  if (!audioContext) {
    if (!AudioContextCtor) throw new Error('Web Audio API is unavailable in this browser');
    audioContext = new AudioContextCtor();
    audioContext.addEventListener('statechange', () => {
      audioReady = audioContext.state === 'running';
      if (audioReady && pendingPCM.length) {
        const queued = pendingPCM.splice(0, pendingPCM.length);
        for (const item of queued) enqueuePCM(item.message, item.generation);
      }
    });
  }
  if (audioContext.state === 'suspended') await audioContext.resume().catch(() => {});
  if (window.speechSynthesis) window.speechSynthesis.resume();
  audioReady = audioContext.state === 'running';
  if (audioReady && pendingPCM.length) {
    const queued = pendingPCM.splice(0, pendingPCM.length);
    for (const item of queued) enqueuePCM(item.message, item.generation);
  }
  // Playback is started by render/speakCommands once; never replay the command list per PCM chunk.
  return audioContext;
}

// Mobile browsers commonly create WebAudio in a suspended state unless it is
// resumed from a real user gesture. Unlock once on the first interaction, then
// PCM chunks arriving asynchronously from the WebSocket can play normally.
window.addEventListener('pointerdown', () => { unlockAudio().catch(() => {}); }, { passive: true });

function apiPath(path) {
  return path;
}

const MAX_PENDING_POSTS = 16;
// Input and terminal audio callbacks can await the real LLM/voice route.
// Urgent handset/reset requests keep a short deadline and cancel these tasks.
const TURN_REQUEST_TIMEOUT_MS = 240000;
const MODEL_POST_PATHS = new Set([
  '/stand/text', '/stand/handset', '/stand/button', '/stand/button-hold',
  '/stand/button-long-hold', '/stand/raw', '/stand/audio/completed', '/stand/audio/failed',
]);
const MAX_PENDING_LIFECYCLE_POSTS = 32;
const LIFECYCLE_POST_PATHS = new Set([
  '/stand/button-released', '/stand/audio/lifecycle', '/stand/audio/completed',
  '/stand/audio/failed', '/stand/audio/checkpoint',
]);
let postQueue = [];
let activePost = null;
let postGeneration = 0;
var wsWriteOwner = false;
const wsButtonSockets = new Map();

function postAdmissionAvailable() {
  return !current?.simulator || (wsSnapshotReady && !wsReloadRequired && !authBlocked);
}

function interactionActive(state = current) {
  const active = state?.simulator?.session?.interaction_active;
  return typeof active === 'boolean' ? active : Boolean(state?.session?.handset_up);
}

function sessionActionIsCurrent(payload) {
  const simulator = current?.simulator;
  if (!simulator || authBlocked || !['start', 'end', 'skip_interview'].includes(payload.action)
    || !/^[a-f0-9]{16}$/.test(payload.instance_id || '') || payload.instance_id !== simulator.instance_id
    || !Number.isSafeInteger(payload.epoch) || payload.epoch !== simulator.epoch
    || typeof payload.session_id !== 'string' || payload.session_id !== simulator.session?.session_id) return false;
  // End can cancel an already submitted Start before its first state arrives.
  if (payload.action === 'end') return true;
  return postAdmissionAvailable() && simulator.admission?.input_ready && simulator.admission.connected
    && Array.isArray(simulator.session.session_actions) && simulator.session.session_actions.includes(payload.action)
    && (payload.action !== 'skip_interview' || (Number.isSafeInteger(payload.state_version)
      && payload.state_version === simulator.session.state_version));
}

function personalStoryCancelAvailable(path, payload) {
  const simulator = current?.simulator;
  if (!simulator || !postAdmissionAvailable() || !simulator.admission?.input_ready
      || !simulator.admission.connected || !interactionActive(current)
      || (simulator.session?.interaction_active === undefined && simulator.devices?.handset?.state !== 'up')
      || current.session.node !== 'personal_story_generating'
      || simulator.session?.node !== 'personal_story_generating'
      || current.canonical_state?.mode !== 'personal_story_generating'
      || simulator.session.session_id !== session() || payload.button_id !== 7) return false;
  return path === '/stand/simulator/action'
    ? payload.action === 'button' && payload.press === 'press' && payload.epoch === simulator.epoch
    : path === '/stand/button' && payload.state === 'pressed' && payload.session_id === simulator.session.session_id;
}

function cancelOrdinaryPosts() {
  // Legacy panels retain their HTTP fallback when the observer socket drops.
  if (!current?.simulator) return;
  postQueue = postQueue.filter(item => {
    if (item.urgent || item.lifecycle) return true;
    item.resolve(null);
    return false;
  });
  if (activePost && !activePost.urgent && !activePost.lifecycle) {
    const obsolete = activePost;
    activePost = null;
    obsolete.resolve(null);
    obsolete.controller.abort();
  }
  dispatchNextPost();
}

function clearPCMPlaybackError() {
  const text = pcmPlaybackError?.text;
  if (!text) return;
  const fallback = $('fallback'), phoneError = $('phone-error');
  if (fallback?.textContent === text) { fallback.textContent = ''; fallback.classList.add('hidden'); }
  if (phoneError?.textContent === text) { phoneError.textContent = ''; phoneError.hidden = true; }
  pcmPlaybackError = null;
}

function post(path, body = {}) {
  if (path === '/stand/text') { pcmTextAttempt += 1; clearPCMPlaybackError(); }
  // Capture both the selected session and the wire payload before waiting.
  const wireBody = JSON.stringify(path.startsWith('/stand/simulator/') ? body : { session_id: session(), ...body });
  const payload = JSON.parse(wireBody);
  const simulated = path === '/stand/simulator/action';
  const sessionAction = simulated && ['start', 'end', 'skip_interview'].includes(payload.action);
  if (sessionAction && !sessionActionIsCurrent(payload)) return Promise.resolve(null);
  const storyCancel = personalStoryCancelAvailable(path, payload);
  const urgent = path === '/stand/reset' || (path === '/stand/handset' && body.state === 'down')
    || (simulated && (body.action === 'reset' || (body.action === 'handset' && body.state === 'down')))
    || (path === '/stand/simulator/fault' && body.mode === 'disconnect') || storyCancel || (sessionAction && payload.action === 'end');
  const lifecycle = LIFECYCLE_POST_PATHS.has(path) || (simulated && body.action === 'button' && body.press === 'release');
  // Keep terminal cleanup available, but an open transport alone cannot
  // authorize input against a snapshot retained from an earlier connection.
  if (!urgent && !lifecycle && !postAdmissionAvailable()) {
    const error = new Error(wsReloadRequired ? 'Сервис перезапущен. Обновите страницу, чтобы продолжить.' : 'Дождитесь актуального состояния телефона.');
    showError(error);
    const rejected = Promise.reject(error);
    rejected.catch(() => {});
    return rejected;
  }
  const owner = path.startsWith('/stand/button') || (simulated && body.action === 'button')
    ? JSON.stringify(['button', payload.session_id, payload.button_id])
    : path.startsWith('/stand/audio/')
      ? JSON.stringify(['audio', payload.session_id, payload.response_id, payload.playback_id,
        payload.playback_lease, payload.generation, payload.token])
      : null;
  const legacyButtonType = {
    '/stand/button': 'button',
    '/stand/button-released': 'button_released',
    '/stand/button-hold': 'button_hold',
    '/stand/button-long-hold': 'button_long_hold',
  }[path];
  const isButtonPress = (simulated && payload.action === 'button' && payload.press === 'press')
    || path === '/stand/button';
  const isButtonRelease = (simulated && payload.action === 'button' && payload.press === 'release')
    || path === '/stand/button-released';
  if (!urgent && ((simulated && payload.action === 'button') || legacyButtonType)
      && typeof ws !== 'undefined' && ws && ws.readyState === WebSocket.OPEN
      && wsWriteOwner && postAdmissionAvailable()) {
    const useWs = isButtonPress
      ? (!activePost && !postQueue.length)
      : wsButtonSockets.get(owner) === ws;
    if (useWs) {
      try {
        const wsMsg = legacyButtonType
          ? { type: legacyButtonType, ...payload }
          : { type: 'simulator_action', ...payload };
        ws.send(JSON.stringify(wsMsg));
        if (isButtonPress) wsButtonSockets.set(owner, ws);
        else if (isButtonRelease) wsButtonSockets.delete(owner);
        recordLiveInput(path, payload);
        return Promise.resolve(null);
      } catch (_) {
        wsButtonSockets.delete(owner);
      }
    }
  }
  if (isButtonRelease) wsButtonSockets.delete(owner);
  if (storyCancel) {
    const pending = [activePost, ...postQueue].find(item => item?.storyCancel && item.path === path && item.wireBody === wireBody);
    if (pending) return pending.promise;
  }
  if (urgent) {
    audioReconnectPending = false;
    postGeneration += 1;
    postQueue = postQueue.filter(item => {
      if (item.urgent || (storyCancel && item.lifecycle)) {
        item.generation = postGeneration;
        return true;
      }
      item.resolve(null);
      return false;
    });
    if (activePost && !activePost.urgent) {
      const obsolete = activePost;
      activePost = null;
      obsolete.resolve(null);
      obsolete.controller.abort();
    }
    // Keep cleanup in FIFO order, but collapse repeated identical cleanup.
    const tail = postQueue.at(-1) || activePost;
    if (tail?.urgent && tail.path === path && tail.wireBody === wireBody) {
      tail.generation = postGeneration;
      return tail.promise;
    }
  }
  if (lifecycle) {
    // Repeated callbacks may share pending work only until another event for
    // this exact owner intervenes. In particular press/release/press/release
    // must retain both releases, and audio statuses must keep their order.
    let previous = activePost?.owner === owner ? activePost : null;
    for (const pending of postQueue) if (pending.owner === owner) previous = pending;
    if (previous?.path === path && previous.wireBody === wireBody) return previous.promise;
  }
  // Keep one FIFO, with a reserved bounded budget for release/audio callbacks.
  // Ordinary input cannot consume the space needed to finish accepted work.
  const pendingCount = postQueue.filter(item => item.lifecycle === lifecycle).length;
  if (pendingCount >= (lifecycle ? MAX_PENDING_LIFECYCLE_POSTS : MAX_PENDING_POSTS)) {
    const error = new Error('Очередь команд заполнена. Дождитесь ответа и повторите ввод.');
    showError(error);
    const rejected = Promise.reject(error);
    rejected.catch(() => {});
    return rejected;
  }
  const timeoutMs = !urgent && (MODEL_POST_PATHS.has(path) || (simulated && !lifecycle))
    ? TURN_REQUEST_TIMEOUT_MS : undefined;
  const item = { path, wireBody, owner, lifecycle, urgent, storyCancel, sessionAction, timeoutMs, generation: postGeneration, controller: new AbortController() };
  item.promise = new Promise((resolve, reject) => { item.resolve = resolve; item.reject = reject; });
  // Some DOM handlers ignore the return value; errors are already shown here.
  item.promise.catch(() => {});
  if (storyCancel) postQueue.splice(postQueue.findLastIndex(pending => pending.urgent) + 1, 0, item);
  else postQueue.push(item);
  dispatchNextPost();
  return item.promise;
}

function dispatchNextPost() {
  if (activePost || !postQueue.length) return;
  let item;
  while (postQueue.length) {
    item = postQueue.shift();
    if (item.urgent || item.lifecycle || postAdmissionAvailable()) break;
    item.resolve(null);
    item = null;
  }
  if (!item) return;
  activePost = item;
  runPost(item).then(item.resolve, item.reject).finally(() => {
    // An urgent request may already have replaced this cancelled request.
    if (activePost !== item) return;
    activePost = null;
    dispatchNextPost();
  });
}

async function runPost(item) {
  try {
    if (item.sessionAction && !sessionActionIsCurrent(JSON.parse(item.wireBody))) return null;
    if (item.storyCancel && !personalStoryCancelAvailable(item.path, JSON.parse(item.wireBody))) return null;
    if (item.controller.signal.aborted || (!item.urgent && !item.lifecycle && !postAdmissionAvailable())) return null;
    recordLiveInput(item.path, JSON.parse(item.wireBody));
    const value = await fetchWithTimeout(item.path, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: item.wireBody,
      signal: item.controller.signal,
      readResponse: async response => {
        const raw = await response.text();
        let value;
        try { value = raw ? JSON.parse(raw) : {}; } catch (parseError) {
          if (current?.session?.state === 'ENDED') return null;
          throw new Error(response.ok ? 'Invalid JSON response: ' + raw.slice(0, 160) : 'HTTP ' + response.status + ': ' + raw.slice(0, 160));
        }
        if (!response.ok) {
          if (current?.session?.state === 'ENDED') return null;
          throw new Error(JSON.stringify(value));
        }
        return value;
      },
    }, item.timeoutMs);
    if (item.generation !== postGeneration || item.controller.signal.aborted
      || (!item.urgent && !item.lifecycle && !postAdmissionAvailable())) return null;
    recordLiveResult(value);
    return value;
  } catch (error) {
    if (item.generation !== postGeneration || item.controller.signal.aborted) return null;
    if ([401, 403].includes(Number(error?.status))) {
      authBlocked = true;
      audioReconnectPending = false;
      suppressPhoneAudio = true;
      clearMic();
      interruptAudioForInput();
      stopPolling();
      window.mishkaPhoneConnection?.(false);
    }
    showError(error);
    throw error;
  }
}

function recordLiveInput(path, body) {
  if (path === '/stand/text') pendingTextInputs.push({length: String(body?.text || '').length, at: Date.now()});
  const labels = {
    '/stand/handset': ['HANDSET', body.state === 'up' ? 'Трубка поднята' : 'Трубка положена', 'DEVICE INPUT'],
    '/stand/button': ['BUTTON', `Кнопка ${body.button_id} · нажата`, 'DEVICE INPUT'],
    '/stand/button-hold': ['BUTTON', `Кнопка ${body.button_id} · удержание ${body.duration_ms || 0} ms`, 'DEVICE INPUT'],
    '/stand/button-long-hold': ['BUTTON', `Кнопка ${body.button_id} · длинное удержание`, 'DEVICE INPUT'],
    '/stand/button-released': ['BUTTON', `Кнопка ${body.button_id} · отпущена`, 'DEVICE INPUT'],
    '/stand/text': ['USER', `Текстовый ввод · ${String(body.text || '').length} символов`, 'TEXT INPUT · REDACTED'],
    '/stand/reset': ['SYSTEM', 'Сброс текущей сессии', 'RESET'],
    '/stand/raw': ['UART', 'Raw frame отправлен', 'JSONL / CRC32']
  }[path];
  if (!labels) return;
  if (path === '/stand/reset') {
    liveClientEntries = [];
    liveResponseIds.clear();
    pendingTextInputs.length = 0;
    $('fallback').classList.add('hidden');
    $('fallback').textContent = '';
  }
  liveClientEntries.push({ kind: path === '/stand/text' ? 'user' : 'action', title: labels[0], text: labels[1], meta: labels[2], at: Date.now(), order: liveClientEntries.length });
  liveClientEntries = liveClientEntries.slice(-80);
  if (current) renderLiveDialogue(current);
}

function recordLiveResult(value) {
  if (!value || typeof value !== 'object') return;
  const correlationId = value.correlation_id;
  if (correlationId) liveResponseIds.add(correlationId);
  const entries = outputLiveEntries({ at: Date.now(), result: value });
  if (!entries.length) return;
  liveClientEntries.push(...entries);
  liveClientEntries = liveClientEntries.slice(-80);
  if (current) renderLiveDialogue(current);
}

function liveTime(at) {
  const date = new Date(typeof at === 'number' ? at : Date.now());
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function outputLiveEntries(item) {
  const result = item.result || {};
  const outputs = Array.isArray(result.outputs) ? result.outputs : [];
  const entries = outputs.flatMap(output => {
    const action = String(output.action || output.type || '');
    if (action === 's2s_text' || action === 's2s_response' || action === 'llm_text') return [{ kind: 'assistant', title: 'МИШКА / LLM', text: 'Ответ подготовлен · текст скрыт', meta: 'STREAM · ' + (result.reason || 'response') + ' · REDACTED', at: item.at }];
    if (action === 'text' && output.text) return [{ kind: 'assistant', title: 'МИШКА / LLM', text: 'Ответ подготовлен · текст скрыт', meta: 'RESPONSE · ' + (result.reason || 'response') + ' · REDACTED', at: item.at }];
    if (action === 's2s_fallback') return [{ kind: 'fallback', title: 'FALLBACK', text: `Безопасный маршрут: ${output.reason || result.reason || 'unknown'}`, meta: 'SAFE ROUTE', at: item.at }];
    if (action === 'audio_retry') return [{ kind: 'audio', title: 'AUDIO', text: 'Не удалось распознать речь · попробуйте ещё раз', meta: `RETRY · ${output.reason || 'recognition'}`, at: item.at }];
    if (action === 'audio_ack') return [{ kind: 'audio', title: 'AUDIO ACK', text: 'Секундочку · быстрый ответ', meta: 'LATENCY ACK', at: item.at }];
    if (action === 'audio') return [{ kind: 'audio', title: 'AUDIO', text: `Реплика ${output.response_id || output.asset_id || '—'}`, meta: 'ALLOWLIST ASSET', at: item.at }];
    if (action === 'game_start' || action === 'game_started') return [{ kind: 'action', title: 'GAME', text: `Игра запущена · ${output.game_id || 'canonical'}`, meta: 'RUNTIME ACTION', at: item.at }];
    if (action === 'story_start' || action === 'story_selected') return [{ kind: 'action', title: 'STORY', text: `Сказка · ${output.story_id || output.asset_id || 'canonical'}`, meta: 'RUNTIME ACTION', at: item.at }];
    if (action === 'led_pattern') { const pattern = String(output.pattern || 'pattern'); return [{ kind: 'action', title: 'LED', text: (pattern.endsWith('_wave') ? 'wave' : pattern) + ' · ' + (output.color || 'mixed'), meta: 'DEVICE COMMAND', at: item.at }]; }
    if (action === 'session_ended') return [{ kind: 'system', title: 'SESSION', text: `Сессия завершена · ${output.reason || 'normal'}`, meta: 'LIFECYCLE', at: item.at }];
    return action ? [{ kind: 'action', title: 'ACTION', text: action.replaceAll('_', ' '), meta: 'RUNTIME', at: item.at }] : [];
  });
  const hasDirectText = ['response_text', 'answer', 'text'].some(key => typeof result[key] === 'string' && result[key]);
  if (!entries.length && hasDirectText) entries.push({ kind: 'assistant', title: 'МИШКА / LLM', text: 'Ответ подготовлен · текст скрыт', meta: 'RESPONSE · ' + (result.reason || 'response') + ' · REDACTED', at: item.at });
  return entries;
}

function eventLiveEntry(event) {
  const payload = event.payload || {};
  const type = String(event.type || '');
  if (type === 'SpeechReceived') {
    const now = Date.now();
    const index = pendingTextInputs.findIndex(item => item.length === Number(payload.length || 0) && now - item.at < 20000);
    const hasRecentText = liveClientEntries.some(entry => entry.kind === 'user' && String(entry.meta || '').startsWith('TEXT INPUT') && now - entry.at < 20000);
    if (index >= 0 || hasRecentText) { if (index >= 0) pendingTextInputs.splice(index, 1); return null; }
    return { kind: 'user', title: 'USER', text: `Голосовой ввод · ${payload.length || 0} символов`, meta: 'VOICE INPUT · REDACTED', at: event.timestamp || event.at };
  }
  const mapping = {
    GameStarted: ['GAME', `Игра запущена · ${payload.game_id || '—'}`, 'SCENARIO'],
    GameCompleted: ['GAME', `Игра завершена · ${payload.success === false ? 'ошибка' : 'успех'}`, 'SCENARIO'],
    StorySelected: ['STORY', `Выбрана сказка · ${payload.story_id || '—'}`, 'SCENARIO'],
    ScenarioAdvanced: ['SCENARIO', `Переход · ${payload.node_id || payload.from || '—'}`, 'GRAPH'],
    SafetyFallbackTriggered: ['FALLBACK', `Защитный fallback · ${payload.reason || 'unknown'}`, 'SAFE ROUTE'],
    BargeInDetected: ['BARGE-IN', 'Ответ прерван новым вводом', 'CANCELLED'],
    AudioCancelled: ['AUDIO', 'Очередь аудио очищена', 'CANCELLED'],
    SessionStarted: ['SESSION', 'Сессия запущена', 'LIFECYCLE'],
    SessionTimedOut: ['SESSION', 'Сессия завершена по таймауту', 'TIMEOUT'],
    SessionEnded: ['SESSION', `Сессия завершена · ${payload.reason || 'normal'}`, 'LIFECYCLE']
  }[type];
  return mapping ? { kind: type.includes('Fallback') ? 'fallback' : type === 'BargeInDetected' || type === 'AudioCancelled' ? 'audio' : 'action', title: mapping[0], text: mapping[1], meta: `${mapping[2]} · #${event.sequence ?? '—'}`, at: event.timestamp || event.at } : null;
}

function pipelineEntries(metrics) {
  const events = Array.isArray(metrics?.events) ? metrics.events : [];
  return events.slice(-35).map(item => {
    const event = String(item.event || '');
    const labels = { speech_end: ['INPUT', 'End of speech detected', 'VAD'], audio_ack_requested: ['AUDIO ACK', 'Immediate acknowledgement requested', 'LATENCY'], token_received: ['TOKEN', 'LLM token received', 'STREAM'], chunk_closed: ['CHUNK', 'Text chunk closed', 'PARSER'], tts_started: ['TTS', 'Silero synthesis started', 'PIPELINE'], audio_ready: ['AUDIO', 'PCM chunk ready', 'PIPELINE'], playback_started: ['PLAYBACK', 'Playback started', 'AUDIO OUT'], audio_cancelled: ['BARGE-IN', 'Playback/queue cancelled', 'CANCELLED'], rvc_v2_fallback: ['RVC', 'RVC v2 unavailable · direct fallback', 'FALLBACK'] }[event];
    if (!labels) return null;
    const suffix = item.chunk_index !== undefined ? ` · chunk ${item.chunk_index}` : item.token_index !== undefined ? ` · token ${item.token_index}` : '';
    return { kind: event.includes('fallback') ? 'fallback' : event === 'audio_cancelled' ? 'audio' : 'metric', title: labels[0], text: labels[1] + suffix, meta: labels[2], at: item.at || item.timestamp };
  }).filter(Boolean);
}

function updateLiveMetrics(state, pipelineEvents) {
  const metrics = state.pipeline_metrics || {};
  const events = Array.isArray(metrics.events) ? metrics.events : [];
  const count = name => events.filter(item => item.event === name).length;
  const formatStage = name => {
    const stage = metrics[name];
    return stage ? `${stage.last_ms} / ${stage.avg_ms} ms` : '—';
  };
  const displayNode = state.session.state === 'ENDED' ? 'session_closed' : (state.session.node || '—');
  $('live-status').textContent = (state.session.state || 'IDLE') + ' · ' + displayNode;
  $('live-latency').textContent = state.latencies?.length ? `${state.latencies.at(-1).ms} ms` : '—';
  $('live-tokens').textContent = `${count('token_received')} TOKENS`;
  $('live-llm').textContent = `LLM ${formatStage('llm')}`;
  $('live-tts').textContent = `TTS ${formatStage('silero_tts')}`;
  const inputEnds = events.filter(item => (item.event === 'speech_end' || item.event === 'input_completed' || item.event === 'turn_started') && (item.at || item.timestamp));
  const inputEnd = inputEnds.at(-1);
  const firstToken = events.find(item => item.event === 'token_received' && (item.at || item.timestamp));
  const inputAt = inputEnd && Number(inputEnd.at || inputEnd.timestamp);
  const firstAudio = events.find(item => item.event === 'audio_ready' && (item.at || item.timestamp) && (!inputAt || Number(item.at || item.timestamp) >= inputAt));
  const firstAt = inputAt || firstToken && Number(firstToken.at || firstToken.timestamp);
  const audioAt = firstAudio && Number(firstAudio.at || firstAudio.timestamp);
  $('live-ttfa').textContent = firstAt && audioAt ? `TTFA ${Math.max(0, Math.round((audioAt - firstAt) * (audioAt > 1e12 ? 1 : 1000)))} ms` : `TTFA ${count('audio_ready') ? 'ready' : '—'}`;
  $('live-chunks').textContent = `${count('chunk_closed')} CHUNKS`;
  $('live-audio').textContent = `${count('audio_ready')} / ${count('playback_started')} AUDIO`;
  $('live-fallbacks').textContent = `${events.filter(item => String(item.event).includes('fallback')).length} FALLBACK`;
}

function renderLiveDialogue(state) {
  const root = $('live-dialogue-feed');
  if (!root || !state) return;
  if (liveSessionId !== state.session.session_id) {
    liveSessionId = state.session.session_id;
    liveClientEntries = [];
    liveResponseIds.clear();
  }
  const entries = [...liveClientEntries];
  for (const item of state.results || []) {
    const correlationId = item.result?.correlation_id;
    if (correlationId && liveResponseIds.has(correlationId)) continue;
    entries.push(...outputLiveEntries(item));
  }
  const sessionEvents = (state.events || []).filter(item => !item.session_id || item.session_id === state.session.session_id);
  const latestStart = [...sessionEvents].reverse().find(item => item.type === 'SessionStarted');
  const startSequence = latestStart?.sequence;
  for (const item of sessionEvents) {
    if (startSequence !== undefined && Number(item.sequence) < Number(startSequence)) continue;
    if (item.session_id && item.session_id !== state.session.session_id) continue;
    const mapped = eventLiveEntry(item);
    if (mapped) entries.push(mapped);
  }
  const pipeline = pipelineEntries(state.pipeline_metrics);
  updateLiveMetrics(state, pipeline);
  entries.push(...pipeline);
  entries.forEach((item, index) => { item._renderOrder = index; });
  entries.sort((a, b) => {
    const atA = Number(a.at);
    const atB = Number(b.at);
    const timeA = Number.isFinite(atA) && atA > 0 ? atA : Number.MAX_SAFE_INTEGER;
    const timeB = Number.isFinite(atB) && atB > 0 ? atB : Number.MAX_SAFE_INTEGER;
    return timeA - timeB || a._renderOrder - b._renderOrder;
  });
  const visible = entries.slice(-70);
  root.textContent = '';
  if (!visible.length) {
    const empty = document.createElement('div');
    empty.className = 'dialogue-empty';
    empty.textContent = 'Здесь появятся вход пользователя, действия устройства, ответы Мишки и потоковые метрики.';
    root.append(empty);
    return;
  }
  for (const item of visible) {
    const row = document.createElement('article');
    row.className = `dialogue-entry ${item.kind || 'system'}`;
    const role = document.createElement('span'); role.className = 'dialogue-role'; role.textContent = item.title || 'EVENT';
    const body = document.createElement('div'); body.className = 'dialogue-body';
    const text = document.createElement('div'); text.className = 'dialogue-text'; text.textContent = item.text || '—';
    const meta = document.createElement('div'); meta.className = 'dialogue-meta'; meta.textContent = item.meta || 'LIVE';
    body.append(text, meta);
    const time = document.createElement('time'); time.className = 'dialogue-time'; time.textContent = liveTime(item.at);
    row.append(role, body, time);
    root.append(row);
  }
  root.scrollTop = root.scrollHeight;
}

function showError(error) {
  $('fallback').classList.remove('hidden');
  $('fallback').textContent = String(error.message || error);
  window.mishkaPhoneError?.(String(error.message || error));
}

function scheduleReconnect(delayOverrideMs = 0) {
  if (reconnectTimer !== null || authBlocked || pageLifecycleStopped || document.hidden) return;
  const exponential = Math.min(30000, 1200 * (2 ** Math.min(reconnectAttempt, 5)));
  const base = Math.max(exponential, Number(delayOverrideMs) || reconnectDelayOverride || 0);
  reconnectDelayOverride = 0;
  const jitter = base * (0.8 + Math.random() * 0.4);
  reconnectAttempt += 1;
  reconnectTimer = window.setTimeout(() => { reconnectTimer = null; connect(); }, jitter);
}

function parseMessage(raw) {
  try {
    const value = JSON.parse(raw);
    return value && typeof value === "object" && !Array.isArray(value) ? value : null;
  } catch (_) { return null; }
}

function normalizeState(value) {
  if (!value || typeof value !== "object" || !value.session || typeof value.session !== "object") return null;
  return { ...value, session: { state: "UNKNOWN", scenario: "", node: "", ...value.session },
    results: Array.isArray(value.results) ? value.results : [], events: Array.isArray(value.events) ? value.events : [],
    commands: Array.isArray(value.commands) ? value.commands : [], latencies: Array.isArray(value.latencies) ? value.latencies : [],
    errors: Array.isArray(value.errors) ? value.errors : [] };
}

function stopWebsocketHeartbeat() {
  if (wsHeartbeatTimer !== null) {
    window.clearInterval(wsHeartbeatTimer);
    wsHeartbeatTimer = null;
  }
}

function startWebsocketHeartbeat(socket) {
  stopWebsocketHeartbeat();
  const beat = () => {
    if (ws !== socket || socket.readyState !== WebSocket.OPEN) {
      stopWebsocketHeartbeat();
      return;
    }
    try { socket.send(JSON.stringify({ type: 'ping' })); } catch (_) { /* onclose handles reconnect */ }
    wsHeartbeatTimer = window.setTimeout(beat, WS_HEARTBEAT_MS);
  };
  wsHeartbeatTimer = window.setTimeout(beat, WS_HEARTBEAT_MS);
}

function connect() {
  if (pageLifecycleStopped || document.hidden) return;
  if (ws && [WebSocket.OPEN, WebSocket.CONNECTING].includes(ws.readyState)) return;
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${scheme}://${location.host}${apiPath("/stand/ws")}`);
  ws = socket;
  wsSnapshotReady = false;
  if (typeof wsWriteOwner !== 'undefined') wsWriteOwner = false;
  if (typeof wsButtonSockets !== 'undefined') wsButtonSockets.clear();
  cancelOrdinaryPosts();
  socket.onopen = () => {
    if (ws !== socket) return;
    reconnectAttempt = 0;
    reconnectDelayOverride = 0;
    liveClientEntries = [];
    $("connection").textContent = wsReloadRequired ? "RELOAD REQUIRED" : "SYNCING";
    $("connection").classList.remove("offline");
    $("connection").classList.add("online");
    startWebsocketHeartbeat(socket);
    renderRuntimeStatus();
    window.mishkaPhoneConnection?.(true);
    refresh({ includeStatus: true, includeState: false }).finally(schedulePolling);
  };
  socket.onclose = () => {
    if (ws !== socket) return;
    stopWebsocketHeartbeat();
    ws = null;
    wsSnapshotReady = false;
    if (typeof wsWriteOwner !== 'undefined') wsWriteOwner = false;
    if (typeof wsButtonSockets !== 'undefined') wsButtonSockets.clear();
    cancelOrdinaryPosts();
    clearMic();
    audioReconnectPending = !authBlocked && (audioReconnectPending
      || Boolean(current?.simulator && interactionActive(current) && !suppressPhoneAudio));
    if (current?.simulator) suppressPhoneAudio = true;
    interruptAudioForInput();
    window.mishkaPhoneConnection?.(false);
    $("connection").textContent = document.hidden ? "PAUSED" : "RECONNECTING";
    $("connection").classList.remove("online");
    $("connection").classList.add("offline");
    renderRuntimeStatus();
    if (!wsIntentionalClose && !document.hidden && !pageLifecycleStopped) {
      scheduleReconnect();
      schedulePolling();
    }
    wsIntentionalClose = false;
  };
  socket.onerror = () => {
    // onclose owns reconnect/backoff; do not create another timer here.
    try { socket.close(); } catch (_) {}
  };
  socket.onmessage = event => {
    if (ws !== socket) return;
    try {
      const message = parseMessage(event.data);
      if (!message) { showError("WebSocket sent invalid JSON"); return; }
      if (message.type === "admission_error") {
        wsSnapshotReady = false;
        cancelOrdinaryPosts();
        clearMic();
        const error = message.error || {};
        if (["auth_failure", "unauthorized", "forbidden"].includes(String(error.code || "").toLowerCase())) {
          authBlocked = true;
          audioReconnectPending = false;
          stopPolling();
        }
        reconnectDelayOverride = Math.min(60000, Math.max(5000, Number(error.retry_after_ms) || 30000));
        showError(`Сервис временно недоступен: ${error.reason || "runtime_unavailable"}. Повтор через несколько секунд.`);
        return;
      }
      if (message.type === "stand_state") {
        if (Object.prototype.hasOwnProperty.call(message, "write_owner")) {
          wsWriteOwner = Boolean(message.write_owner);
        }
        const state = normalizeState(message.state);
        if (!state) { showError("WebSocket sent invalid stand state"); return; }
        if (!simulatorSnapshotIsCurrent(state)) {
          const changedInstance = typeof state.simulator?.instance_id === 'string'
            && /^[a-f0-9]{16}$/.test(state.simulator.instance_id)
            && typeof current?.simulator?.instance_id === 'string'
            && state.simulator.instance_id !== current.simulator.instance_id;
          const sameInstance = typeof state.simulator?.instance_id === 'string'
            && state.simulator.instance_id === current?.simulator?.instance_id;
          if (changedInstance || (!sameInstance && !wsSnapshotReady && Number.isSafeInteger(state.simulator?.epoch)
            && Number.isSafeInteger(state.simulator?.journal_cursor)
            && (state.simulator.epoch < current?.simulator?.epoch
              || (state.simulator.epoch === current?.simulator?.epoch
                && state.simulator.journal_cursor < current.simulator.journal_cursor)))) {
            wsReloadRequired = true;
            cancelOrdinaryPosts();
            audioReconnectPending = false;
            suppressPhoneAudio = true;
            clearMic();
            interruptAudioForInput();
            $("connection").textContent = "RELOAD REQUIRED";
            showError('Сервис перезапущен. Обновите страницу, чтобы продолжить.');
            window.mishkaPhoneConnection?.(false);
          }
          return;
        }
        wsSnapshotReady = !wsReloadRequired && !authBlocked;
        $("connection").textContent = wsReloadRequired ? "RELOAD REQUIRED" : "CONNECTED";
        const completion = message.audio_completion;
        const completionSessionId = state.simulator?.session?.session_id || session();
        // Canonical retries omit session_id; their public state must match this snapshot.
        const completionOwnsSnapshot = completion?.session_id === completionSessionId
          || (completion?.session_id === undefined && Number.isSafeInteger(completion?.canonical_state?.state_version)
            && completion.canonical_state.state_version === (state.simulator?.session?.state_version ?? state.canonical_state?.state_version)
            && completion.canonical_state.mode === state.session.node && (completion.canonical_state.interaction_active ?? (completion.canonical_state.handset === 'up')));
        const completionCurrent = completionOwnsSnapshot
          && interactionActive(state) && !authBlocked && !wsReloadRequired
          && (!state.simulator || (!suppressPhoneAudio && state.simulator.epoch === current?.simulator?.epoch
            && state.simulator.session.session_id === current?.simulator?.session?.session_id));
        if (completionCurrent) {
          if (completion.reason === 'audio_retry') audioRetry = {sessionId: completionSessionId, epoch: state.simulator?.epoch, node: state.session.node};
          else if (!completion.pending && !completion.error) audioRetry = null;
        }
        try {
          if (message.lighting_wave) state.lighting_wave = message.lighting_wave;
          render(state, { fromWebSocket: true });
        } catch (error) {
          console.warn('Invalid stand snapshot', error);
          showError('Invalid stand snapshot: ' + (error.message || error));
        }
        if (mic && completionCurrent && !completion.pending) {
          $("mic-state").textContent = audioRetry ? "Речь не распознана · попробуйте ещё раз"
            : microphoneAccepting() ? "Ответ получен · Мишка слушает" : "Ответ получен · приём на паузе";
        }
      }
      if (message.type === "audio_pcm_output") {
        if (typeof message.data_b64 !== "string" || !Number.isFinite(Number(message.sample_rate))) { showError("WebSocket sent invalid audio frame"); return; }
        if (current?.simulator && (!wsSnapshotReady || wsReloadRequired || suppressPhoneAudio || message.session_id !== current.simulator.session.session_id
          || Number(message.generation) < minimumPCMGeneration
          || Number(message.generation) < Number(current.simulator.devices.audio.browser_generation))) return;
        enqueuePCM(message);
      }
      if (message.type === "audio_stop") {
        handleAudioStop(message);
      }
    } catch (error) {
      console.warn('WebSocket message handling failed', error);
      showError('WebSocket message failed: ' + (error.message || error));
    }
  };
}

async function fetchWithTimeout(path, options = {}, timeoutMs = FETCH_TIMEOUT_MS) {
  const { allowStatuses = [], readResponse, ...requestOptions } = options;
  const controller = new AbortController();
  const parentSignal = requestOptions.signal;
  const abortFromParent = () => controller.abort();
  let rejectOnAbort;
  const aborted = new Promise((_, reject) => {
    rejectOnAbort = () => {
      const error = new Error('Request aborted');
      error.name = 'AbortError';
      reject(error);
    };
    controller.signal.addEventListener('abort', rejectOnAbort, { once: true });
  });
  if (parentSignal) {
    if (parentSignal.aborted) controller.abort();
    else parentSignal.addEventListener('abort', abortFromParent, { once: true });
  }
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await Promise.race([(async () => {
      const response = await fetch(apiPath(path), { cache: 'no-store', ...requestOptions, signal: controller.signal });
      if (!response.ok && !allowStatuses.includes(response.status)) {
        const error = new Error(`HTTP ${response.status}`);
        error.status = response.status;
        throw error;
      }
      // POST owns its response body inside the same total request deadline.
      return readResponse ? await readResponse(response) : response;
    })(), aborted]);
  } finally {
    window.clearTimeout(timeout);
    controller.signal.removeEventListener('abort', rejectOnAbort);
    parentSignal?.removeEventListener('abort', abortFromParent);
  }
}

async function refresh({ includeStatus = true, includeState = true } = {}) {
  if (refreshInFlight) return refreshInFlight;
  const controller = new AbortController();
  const run = (async () => {
    refreshAbortController = controller;
    try {
      if (includeStatus && (Date.now() - lastHealthRefresh >= HEALTH_REFRESH_MS || !healthSnapshot)) {
        const [healthResult, readinessResult] = await Promise.allSettled([
          fetchWithTimeout('/health', { signal: controller.signal }),
          fetchWithTimeout('/ready', { signal: controller.signal, allowStatuses: [503] }),
        ]);
        if (healthResult.status === 'fulfilled') healthSnapshot = await healthResult.value.json();
        if (readinessResult.status === 'fulfilled') readinessSnapshot = await readinessResult.value.json();
        if (healthSnapshot || readinessSnapshot) lastHealthRefresh = Date.now();
        renderRuntimeStatus();
      }
      if (includeState && !authBlocked) {
        try {
          const path = document.body.dataset.phone ? '/stand/simulator/state' : '/stand/state';
          const state = await (await fetchWithTimeout(path, { signal: controller.signal })).json();
          render(state);
          stateSnapshotAvailable = true;
        } catch (error) {
          if ([401, 403].includes(Number(error?.status))) {
            authBlocked = true;
            audioReconnectPending = false;
            suppressPhoneAudio = true;
            clearMic();
            interruptAudioForInput();
            stopPolling();
            showError(new Error("WebPanel access denied. Refresh with a valid token."));
          } else {
            throw error;
          }
        }
      }
      refreshBackoff = 0;
      renderRuntimeStatus();
    } catch (error) {
      if (error?.name === 'AbortError' || controller.signal.aborted) return;
      if ([401, 403].includes(Number(error?.status))) {
        authBlocked = true;
        stopPolling();
        showError(new Error('Доступ к WebPanel отклонён. Обновите страницу с корректным токеном.'));
        return;
      }
      $('health').textContent = 'HEALTH UNAVAILABLE';
      if (!document.hidden) showError(error);
      refreshBackoff = Math.min(30000, Math.max(POLL_FALLBACK_MS, refreshBackoff ? refreshBackoff * 2 : POLL_FALLBACK_MS));
    } finally {
      if (refreshAbortController === controller) refreshAbortController = null;
    }
  })();
  refreshInFlight = run;
  try { await run; } finally { refreshInFlight = null; }
  return run;
}

function stopPolling({ abortRefresh = false } = {}) {
  if (refreshTimer !== null) {
    window.clearTimeout(refreshTimer);
    refreshTimer = null;
  }
  if (abortRefresh && refreshAbortController) refreshAbortController.abort();
}

function schedulePolling() {
  stopPolling();
  if (pageLifecycleStopped || document.hidden || authBlocked) return;
  const websocketLive = ws?.readyState === WebSocket.OPEN;
  const delay = websocketLive ? HEALTH_REFRESH_MS : (refreshBackoff || POLL_FALLBACK_MS);
  refreshTimer = window.setTimeout(async () => {
    refreshTimer = null;
    const websocketLive = ws?.readyState === WebSocket.OPEN;
    await refresh({ includeStatus: true, includeState: !websocketLive });
    schedulePolling();
  }, delay);
}

function startPanelLifecycle() {
  pageLifecycleStopped = false;
  if (authBlocked) return;
  refresh({ includeStatus: true }).finally(schedulePolling);
  if (ws?.readyState !== WebSocket.OPEN) connect();
}

function normalizeColor(color) {
  const value = String(color || '').toLowerCase();
  return Object.prototype.hasOwnProperty.call(LED_COLORS, value) ? value : null;
}

function validateButtonContract(contract) {
  const entries = contract && Array.isArray(contract.buttons) ? contract.buttons : null;
  if (!contract || contract.valid !== true || !entries || entries.length !== BUTTON_IDS.length) {
    return { valid: false, reason: 'missing_or_invalid_contract', entries: [], byId: {}, labels: {} };
  }
  const byId = {};
  const labels = {};
  for (const entry of entries) {
    const id = Number(entry?.id);
    const color = normalizeColor(entry?.color);
    if (!Number.isInteger(id) || !BUTTON_IDS.includes(id) || byId[id] || !color || color === 'off' || color === 'invalid') {
      return { valid: false, reason: 'duplicate_or_invalid_contract_entry', entries: [], byId: {}, labels: {} };
    }
    if (String(entry?.label || '').toUpperCase() !== color.toUpperCase()) return { valid: false, reason: 'label_color_mismatch', entries: [], byId: {}, labels: {} };
    byId[id] = color;
    labels[id] = String(entry.label);
  }
  if (BUTTON_IDS.some(id => !byId[id])) return { valid: false, reason: 'missing_contract_button', entries: [], byId: {}, labels: {} };
  return { valid: true, reason: '', entries, byId, labels };
}

function invalidLEDState(reason = 'invalid_led_state') {
  return { colors: Object.fromEntries(BUTTON_IDS.map(id => [id, 'invalid'])), targets: new Set(), pattern: 'invalid', color: 'invalid', valid: false, reason };
}

function deriveLEDState(canonical, contract) {
  if (!contract.valid || !canonical || canonical.valid !== true) return invalidLEDState(contract.reason || canonical?.reason);
  const colors = {};
  for (const id of BUTTON_IDS) {
    const raw = canonical.colors?.[String(id)];
    if (raw === undefined || raw === null || String(raw).toLowerCase() === 'off') {
      colors[id] = 'off';
      continue;
    }
    const actual = normalizeColor(raw);
    if (!actual || actual !== contract.byId[id]) return invalidLEDState('runtime_mapping_mismatch');
    colors[id] = actual;
  }
  return { colors, targets: new Set((canonical.targets || []).map(Number)), pattern: String(canonical.pattern || 'off'), color: String(canonical.color || 'off'), valid: true, reason: '' };
}

function renderButtonLegend(contract) {
  const root = $('button-legend');
  if (!root) return;
  root.replaceChildren();
  root.classList.toggle('invalid', !contract.valid);
  if (!contract.valid) {
    root.textContent = `INVALID BUTTON CONTRACT - ${contract.reason}`;
    return;
  }
  for (const entry of contract.entries) {
    const item = document.createElement('span');
    item.innerHTML = `<b>${entry.id}</b> ${entry.label}`;
    item.dataset.color = entry.color;
    root.append(item);
  }
}

function buttonLegendSummary(contract) {
  if (!contract.valid) return 'Button mapping is invalid; input is blocked until the runtime contract is restored.';
  return `Buttons: ${contract.entries.map(entry => `${entry.id} - ${entry.label}`).join(', ')}.`;
}

function cardStatusProjection(state) {
  // Render only the canonical mode. Card and profile payloads are private.
  switch (state.canonical_state?.mode) {
    case 'profile_ready':
      return { label: 'Карта принята · поднимите трубку', hint: 'Поднимите трубку, чтобы начать.', tone: 'state-ok' };
    default:
      return { label: 'Нет активной карты', hint: '', tone: 'state-warn' };
  }
}

function simulatorSnapshotIsCurrent(nextState, previousState = current) {
  const next = nextState?.simulator;
  const previous = previousState?.simulator;
  if (!next) return !previous;
  if (!Number.isSafeInteger(next.epoch) || next.epoch < 0
    || !Number.isSafeInteger(next.journal_cursor) || next.journal_cursor < 0) return false;
  if (next.snapshot_revision !== undefined
    && (!Number.isSafeInteger(next.snapshot_revision) || next.snapshot_revision < 0)) return false;
  if (next.instance_id !== undefined
    && (typeof next.instance_id !== 'string' || !/^[a-f0-9]{16}$/.test(next.instance_id))) return false;
  if (!previous) return true;
  if (previous.instance_id !== undefined && next.instance_id !== previous.instance_id) return false;
  if (previous.snapshot_revision !== undefined && next.snapshot_revision === undefined) return false;
  // Epoch owns cleanup boundaries; the journal orders changes within one
  // boundary. Session state_version can restart after a clean relift.
  if (next.epoch !== previous.epoch) return next.epoch > previous.epoch;
  if (next.journal_cursor < previous.journal_cursor) return false;
  if (previous.snapshot_revision !== undefined) {
    return Number.isSafeInteger(next.snapshot_revision)
      && next.snapshot_revision >= previous.snapshot_revision;
  }
  return true;
}

function render(state, { fromWebSocket = false } = {}) {
  state = normalizeState(state);
  if (!state) { showError("Invalid stand state"); return; }
  // Check before touching media fences, current state or any projected control.
  // Poll responses, WS snapshots and POST responses all enter here.
  if (!simulatorSnapshotIsCurrent(state)) return;
  if (state.simulator && current?.simulator && (state.simulator.epoch !== current.simulator.epoch
      || state.simulator.session.session_id !== current.simulator.session.session_id)) {
    clearMic();
    interruptAudioForInput();
  }
  if (!interactionActive(state)
    || state.simulator?.session?.session_id !== current?.simulator?.session?.session_id
    || state.simulator?.epoch !== current?.simulator?.epoch) {
    failedPCMOwner = null;
    clearPCMPlaybackError();
  }
  if (audioRetry && (!interactionActive(state) || audioRetry.sessionId !== (state.simulator?.session?.session_id || session())
    || audioRetry.epoch !== state.simulator?.epoch || audioRetry.node !== state.session.node)) audioRetry = null;
  // Recover transport-only suppression from an accepted snapshot on the new
  // socket. Polling and a pending local cleanup cannot reopen this boundary.
  if (fromWebSocket && audioReconnectPending) {
    audioReconnectPending = false;
    if (!authBlocked && wsSnapshotReady && !wsReloadRequired && state.simulator && interactionActive(state)) suppressPhoneAudio = false;
  }
  if (state.simulator && wsSnapshotReady && !wsReloadRequired && interactionActive(state) && current?.simulator
    && (!interactionActive(current) || state.simulator.epoch !== current.simulator.epoch
      || state.simulator.session.session_id !== current.simulator.session.session_id)) suppressPhoneAudio = false;
  if (state.simulator && !interactionActive(state)) suppressPhoneAudio = true;
  current = state;
  if (state.simulator?.session?.session_id) $('session').value = state.simulator.session.session_id;
  syncMicAdmission();
  if (!firstSnapshotRendered) {
    lastSpokenCommand = Math.max(0, ...(state.commands || []).map(item => Number(item.sequence) || 0));
    firstSnapshotRendered = true;
  }
  const contract = validateButtonContract(state.button_contract);
  renderButtonLegend(contract);
  const pipeline = state.pipeline_metrics?.pipeline || {};
  const fallbackEvent = (state.pipeline_metrics?.events || []).some(item => item?.event === 'rvc_v2_fallback');
  const conversion = String(pipeline.voice_conversion || 'rvc_v2').toLowerCase();
  const ttsProvider = String(pipeline.tts_provider || 'silero').toLowerCase();
  const ttsLabel = { silero: 'SILERO', voxcpm765: 'VOXCPM765', mock: 'MOCK' }[ttsProvider] || ttsProvider.toUpperCase();
  const mode = conversion === 'off' || conversion === 'disabled'
    ? `${ttsProvider}+off`
    : fallbackEvent ? 'passthrough' : `${ttsProvider}+rvc`;
  const modeLabel = mode === 'passthrough' ? 'PASSTHROUGH' : `${ttsLabel} + ${mode.endsWith('+off') ? 'OFF' : 'RVC'}`;
  $('pipeline-mode').textContent = modeLabel;
  $('pipeline-mode').dataset.mode = mode;
  const sessionState = String(state.session?.state || '').trim().toUpperCase();
  if (mic && (!interactionActive(state) || ['NONE', 'IDLE', 'ENDED'].includes(sessionState))) clearMic();
  const handsetUp = Boolean(state.session.handset_up);
  $('session-mode-label').textContent = interactionActive(state) ? 'SESSION ACTIVE' : 'STANDBY';
  $('state').textContent = state.session.state;
  $('node').textContent = (state.session.scenario || '—') + ' / ' + (state.session.state === 'ENDED' ? 'session_closed' : (state.session.node || '—'));
  const last = state.latencies.at(-1);
  $('latency').textContent = last ? `${last.operation}: ${last.ms} ms` : '—';
  $('pipeline').textContent = JSON.stringify(state.pipeline_metrics, null, 2);
  updateGuide(state, contract);
  const standby = ['NONE', 'IDLE', 'STANDBY'].includes(state.session.state);
  const cardStatus = cardStatusProjection(state);
  const cardPendingOrReady = ['profile_ready'].includes(state.canonical_state?.mode);
  setRuntimeValue('device-card', cardStatus.label, cardStatus.tone);
  const runtimeLabel = cardPendingOrReady ? cardStatus.label : standby ? 'Тёмное ожидание' : handsetUp ? 'Трубка поднята' : 'Трубка опущена';
  if ($('runtime-state')) $('runtime-state').textContent = runtimeLabel;
  document.getElementById('handset-state').textContent = handsetUp ? 'HANDSET UP' : sessionState === 'ENDED' ? 'HANDSET DOWN / ENDED' : 'HANDSET DOWN'; document.getElementById('device-handset').textContent = handsetUp ? 'UP' : 'DOWN';
  const deviceState = standby ? 'STANDBY' : handsetUp ? 'UP' : 'DOWN';
  document.getElementById('device-handset').textContent = deviceState;
  const deviceMode = document.getElementById('device-mode');
  if (deviceMode) deviceMode.textContent = state.transport?.hardware?.acceptance === 'physical' ? 'PHYSICAL' : 'SIMULATOR';
  const displayNode = state.session.state === 'ENDED' ? 'session_closed' : (state.session.node || state.session.state);
  $('phone-display').textContent = cardPendingOrReady ? cardStatus.label : standby ? 'Тёмное ожидание' : displayNode;
  const latestAudioCommand = state.commands.filter(item => item.type === 'audio_play' || item.type === 'audio_stop').sort((a, b) => b.sequence - a.sequence)[0];
  document.getElementById('audio-state').textContent = latestAudioCommand?.type === 'audio_play'
    ? 'AUDIO: PLAYING' : audioReady ? 'AUDIO: READY' : 'AUDIO: LOCKED';
  $('device-display').textContent = state.session.state === 'ENDED' ? 'session_closed' : (state.session.node || '—');
  $('device-uart').textContent = state.transport?.hardware?.connected === false ? 'DISCONNECTED' : 'element,state,transition';
  renderLog('frames', state.raw_frames, item => `${item.direction} #${item.decoded?.sequence ?? '?'}`, item => item.raw, item => item.error ? 'error' : item.direction.startsWith('host') ? 'host' : 'device');
  renderHumanLog('events', state.events, eventSummary, item => item.error ? 'error' : '', item => item);
  renderHumanLog('results', [...state.results, ...state.errors], resultSummary, item => item.error ? 'error' : '', item => item.result || item);
  renderAudioStatus(state);
  renderStoryStatus(state);
  renderRuntimeStatus();
  const led = deriveLEDState(state.led, contract);
  const lightingResult = normalizeLightingState(state.lighting);
  const lighting = lightingResult.state;
  $('device-led').textContent = lightingResult.valid
    ? '18 ELEMENTS / ' + Object.values(lighting).filter(Boolean).length + ' ON'
    : 'INVALID LIGHTING SNAPSHOT: ' + lightingResult.issues.join('; ');
  if (!lightingResult.valid) showError(new Error(lightingResult.issues.join('; ')));
  else renderButtons(state, led, contract, lighting);
  renderLightingElements(lighting);
  renderLiveDialogue(state);
  speakCommands(state);
  // ``barge_in: false`` is part of every normal audio response.  Searching
  // the whole JSON for the word "barge_in" made the red error notice appear
  // while audio was merely being collected.  Match explicit failure reasons
  // and fallback actions instead.
  const fallback = [...state.results].reverse().find(item => {
    const result = item.result || {};
    const reason = String(result.reason || '');
    const outputs = Array.isArray(result.outputs) ? result.outputs : [];
    const reasonLooksFailed = /(?:^|_)(?:error|failed|blocked|fallback|unavailable)(?:$|_)/i.test(reason);
    const outputLooksFailed = outputs.some(output => {
      const action = String(output?.action || output?.type || '');
      const resolution = String(output?.resolution || '');
      return /(?:blocked|failed|fallback|unavailable)/i.test(`${action} ${resolution}`)
        || ['safe_stop', 'transition_rejected', 's2s_fallback', 'content_blocked', 'privacy_blocked'].includes(action);
    });
    return reasonLooksFailed || outputLooksFailed;
  });
  const busy = [...state.results].reverse().find(item => /installation_busy/i.test(JSON.stringify(item.result)));
  $('fallback').classList.toggle('hidden', !fallback && !busy);
  if (fallback) {
    const result = fallback.result || {};
    const output = (Array.isArray(result.outputs) ? result.outputs : []).find(item => item?.resolution || item?.action || item?.type);
    const detail = output?.resolution || output?.action || output?.type || '';
    $('fallback').textContent = `${String(result.reason || 'runtime fallback').replaceAll('_', ' ')}${detail ? ` · ${String(detail).replaceAll('_', ' ')}` : ''}`;
  }
  if (busy) $('fallback').textContent = 'Активная выставочная сессия уже запущена. Опустите трубку для полного сброса.';
  window.mishkaPhoneUpdate?.(state);
}

function normalizeLightingState(snapshot) {
  const source = snapshot && typeof snapshot === 'object' ? snapshot : null;
  const issues = [];
  if (!source) issues.push('lighting snapshot must be an object');
  const keys = source ? Object.keys(source) : [];
  const expected = new Set(LIGHTING_ELEMENT_IDS.map(String));
  for (const id of expected) if (!source || !Object.prototype.hasOwnProperty.call(source, id)) issues.push('missing lighting element ' + id);
  for (const id of keys) if (!expected.has(id)) issues.push('unknown lighting element ' + id);
  for (const id of expected) if (source && Object.prototype.hasOwnProperty.call(source, id) && source[id] !== 0 && source[id] !== 1) issues.push('invalid state for lighting element ' + id);
  if (issues.length) {
    console.error('Invalid lighting snapshot', { issues, snapshot });
    return { valid: false, state: null, issues };
  }
  return { valid: true, state: Object.fromEntries(LIGHTING_ELEMENT_IDS.map(id => [String(id), source[String(id)]])), issues: [] };
}

function renderButtons(state, led, contract, lighting) {
  const session = state.session || {};
  const guestButtons = new Set([1, 2, 3, 4, 5, 7]);
  const guestMode = !session.state || ['NONE', 'IDLE', 'STANDBY', 'ENDED'].includes(session.state);
  const sessionActive = Boolean(session.handset_up) && !guestMode;
  const enabledButtons = Array.isArray(session.enabled_buttons) ? new Set(session.enabled_buttons.map(Number)) : null;
  for (const id of BUTTON_IDS) {
    const enabled = state.simulator
      ? Boolean(!authBlocked && wsSnapshotReady && !wsReloadRequired && ws?.readyState === WebSocket.OPEN && state.simulator.admission?.input_ready
        && state.simulator.admission?.connected && state.simulator.devices.buttons.find(button => button.id === id)?.enabled)
      : Boolean(session.visual_only) ? guestButtons.has(id) : (guestMode ? guestButtons.has(id) : (sessionActive && (enabledButtons === null || enabledButtons.has(id))));
    const button = buttonRefs.get(id);
    if (!button) continue;
    const buttonElementIds = ELEMENTS_BY_BUTTON[id];
    const fillId = buttonElementIds.fill;
    const digitId = buttonElementIds.digit;
    const symbolId = buttonElementIds.symbol ?? null;
    const compensationId = buttonElementIds.compensation ?? null;
    const fillOn = lighting[fillId] === 1;
    const digitOn = lighting[digitId] === 1;
    const symbolOn = symbolId !== null && lighting[symbolId] === 1;
    const compensationOn = compensationId !== null && lighting[compensationId] === 1;
    const fillPresentation = Object.values(ELEMENT_PRESENTATION).find(item => item.buttonId === id && item.kind === 'fill');
    const colorDef = LED_COLORS[fillPresentation?.color] || LED_COLORS.invalid;
    const buttonLabel = contract.valid ? contract.labels[id] : colorDef.label;
    for (const led of Object.values(LED_COLORS)) button.classList.remove(led.className);
    button.classList.remove('led-target', 'profile-wave');
    button.style.removeProperty('--led-color');
    button.classList.add(colorDef.className);
    button.classList.toggle('led-on', fillOn);
    button.classList.toggle('led-off', !fillOn);
    button.classList.toggle('digit-on', digitOn);
    button.classList.toggle('symbol-on', symbolOn);
    button.classList.toggle('compensation-on', compensationOn);
    button.classList.toggle('pressed', buttonInputs[id] === 'pressed');
    button.classList.toggle('disabled', !enabled);
    button.disabled = !enabled;
    button.setAttribute('aria-label', 'Кнопка ' + id + ', ' + buttonLabel + ', ' + buttonInputs[id] + ', ' + (enabled ? 'enabled' : 'disabled'));
    button.title = enabled ? ('Кнопка ' + id + ': ' + buttonLabel) : (id === 6 && sessionActive ? 'Мишка/LLM: поднимите трубку' : 'Кнопка недоступна на этом этапе');
    const digit = button.querySelector('.button-digit');
    const symbol = button.querySelector('.button-symbol');
    if (digit) digit.classList.toggle('element-on', digitOn);
    if (symbol) symbol.classList.toggle('element-on', symbolOn);
    const inputCell = document.querySelector('#button-input-' + id);
    const ledCell = document.querySelector('#button-led-' + id);
    const enabledCell = document.querySelector('#button-enabled-' + id);
    if (inputCell) inputCell.textContent = buttonInputs[id].toUpperCase();
    if (ledCell) {
      ledCell.textContent = String(fillId) + ':' + (fillOn ? 1 : 0) + ' · ' + String(digitId) + ':' + (digitOn ? 1 : 0) + (symbolId !== null ? ' · ' + symbolId + ':' + (symbolOn ? 1 : 0) : '') + (compensationId !== null ? ' · ' + compensationId + ':' + (compensationOn ? 1 : 0) : '');
      ledCell.className = 'diag-color ' + (fillOn ? '' : 'off');
      ledCell.style.setProperty('--diag-color', colorDef.css);
    }
    if (enabledCell) enabledCell.textContent = enabled ? 'YES' : 'NO';
  }
}
function setRuntimeValue(id, text, tone) {
  const node = $(id);
  if (!node) return;
  node.textContent = text;
  node.classList.remove('state-ok', 'state-warn', 'state-bad');
  node.classList.add(tone || 'state-warn');
}

function renderRuntimeStatus() {
  const connected = ws?.readyState === WebSocket.OPEN;
  const readiness = readinessSnapshot || {};
  const healthOk = ['ok', 'ready'].includes(String(healthSnapshot?.status || '').toLowerCase());
  const runtimeDegraded = readiness.lifecycle?.state === 'DEGRADED' || readiness.ready === false;
  const observerAvailable = stateSnapshotAvailable || Boolean(healthSnapshot || readinessSnapshot);
  const authRequired = readiness.security?.mutation_policy_ready === false;
  const serverStatus = connected ? 'connected' : runtimeDegraded ? 'degraded' : (reconnectTimer !== null ? 'reconnecting' : 'disconnected');
  setRuntimeValue('server-status', serverStatus, connected ? 'state-ok' : runtimeDegraded ? 'state-warn' : 'state-bad');
  setRuntimeValue('health-status', healthSnapshot ? (healthOk ? 'OK' : 'DEGRADED') : observerAvailable ? 'DEGRADED' : 'unknown', healthSnapshot ? (healthOk ? 'state-ok' : 'state-warn') : observerAvailable ? 'state-warn' : 'state-warn');
  setRuntimeValue('software-status', readiness.software_runtime_usable ? 'usable' : readiness.software_runtime_usable === false ? 'unavailable' : 'unknown', readiness.software_runtime_usable ? 'state-ok' : 'state-bad');
  setRuntimeValue('headless-status', readiness.headless_ready ? 'ready' : readiness.headless_ready === false ? 'not ready' : 'unknown', readiness.headless_ready ? 'state-ok' : 'state-bad');
  setRuntimeValue('webpanel-status', connected ? 'connected' : authRequired ? 'auth required' : observerAvailable ? 'degraded' : 'disconnected', connected ? 'state-ok' : authRequired ? 'state-bad' : observerAvailable ? 'state-warn' : 'state-bad');
  setRuntimeValue('simulated-status', readiness.simulated_hardware_ready ? 'available' : readiness.simulated_hardware_ready === false ? 'unavailable' : 'unknown', readiness.simulated_hardware_ready ? 'state-ok' : 'state-bad');
  setRuntimeValue('physical-status', readiness.physical_hardware_ready ? 'available' : readiness.physical_hardware_ready === false ? 'unavailable' : 'unknown', readiness.physical_hardware_ready ? 'state-ok' : 'state-warn');
  setRuntimeValue('audio-content-status', readiness.audio_content_ready ? 'approved' : readiness.audio_content_ready === false ? 'not approved' : 'unknown', readiness.audio_content_ready ? 'state-ok' : 'state-warn');
  setRuntimeValue('guest-status', readiness.guest_ready ? 'ready' : readiness.guest_ready === false ? 'not ready' : 'unknown', readiness.guest_ready ? 'state-ok' : 'state-warn');
  setRuntimeValue('identity-status', readiness.identity_ready ? 'ready' : readiness.identity_ready === false ? 'not ready' : 'unknown', readiness.identity_ready ? 'state-ok' : 'state-warn');
  setRuntimeValue('reader-status', readiness.reader_ready ? 'ready' : readiness.reader_ready === false ? 'not ready' : 'unknown', readiness.reader_ready ? 'state-ok' : 'state-warn');
  setRuntimeValue('production-status', readiness.production_ready ? 'ready' : readiness.production_ready === false ? 'not ready' : 'unknown', readiness.production_ready ? 'state-ok' : 'state-warn');
  const reason = readiness.production_ready === false ? (readiness.physical_hardware_ready === false ? 'physical hardware unavailable' : readiness.content?.voice?.status || 'degraded') : '—';
  if ($('degraded-reason')) $('degraded-reason').textContent = reason.replaceAll('_', ' ');
  if ($('health')) {
    const healthStatus = String(healthSnapshot?.status || '').toLowerCase();
    const healthLabel = healthStatus === 'ok' || healthStatus === 'ready' ? 'OK' : healthStatus === 'degraded' ? 'DEGRADED' : 'ERROR';
    $('health').textContent = healthSnapshot ? 'HEALTH ' + healthLabel : 'HEALTH —';
  }
}

function elementKind(elementId) {
  const value = ELEMENT_PRESENTATION[elementId] || {};
  return value.kind === 'symbol' ? (value.symbol === 'bear' ? 'bear emblem' : 'weather emblem') : value.kind;
}

function renderLightingElements(lighting) {
  const root = $('led-elements');
  if (!root || !lighting) return;
  root.replaceChildren();
  for (const elementId of LIGHTING_ELEMENT_IDS) {
    const on = lighting[elementId] === 1 || lighting[String(elementId)] === 1;
    const cell = document.createElement('div');
    const kind = elementKind(elementId);
    cell.className = 'led-element ' + (on ? 'on' : 'off') + (kind.includes('emblem') ? ' emblem' : '');
    cell.title = 'element_id ' + elementId + ' · ' + kind;
    cell.innerHTML = '<span class="led-element-id">' + elementId + '</span><span class="led-element-kind">' + kind + '</span><span class="led-element-state">' + (on ? 'ON' : 'OFF') + '</span>';
    root.append(cell);
  }
  const wave = current?.lighting_wave;
  const lightingEffect = current?.lighting_effects || {};
  const breathing = lightingEffect.effect === 'breathing';
  const waveRunning = lightingEffect.effect === 'wave';
  const waveHeld = lightingEffect.effect === 'wave_hold';
  const binaryState = lightingEffect.state;
  const transitionMs = lightingEffect.transition_ms;
  const heldElements = Array.isArray(lightingEffect.last_frame?.active_elements) ? lightingEffect.last_frame.active_elements : [];
  const waveLabel = waveRunning
    ? 'WAVE ' + (Number(lightingEffect.wave_stage || 0) + 1) + '/' + (lightingEffect.last_frame?.wave_total_stages || 7)
    : waveHeld
      ? 'WAVE HELD · ' + (heldElements.length === 14 ? 'DIGITS' : 'ALL')
      : LIGHTING_ELEMENT_IDS.every(id => lighting[id] === 0) ? 'LIGHTS OFF' : 'WAVE IDLE';
  if ($('wave-state')) {
    $('wave-state').textContent = wave?.active
      ? 'WAVE ' + wave.stage + '/' + wave.total_stages
      : breathing
        ? 'BREATHING ' + (binaryState === 1 ? 'ON' : 'OFF') + (Number.isFinite(Number(transitionMs)) ? ' · ' + Math.round(Number(transitionMs)) + 'ms' : '')
        : waveLabel;
  }
}

function renderAudioStatus(state) {
  const effects = Array.isArray(state.effects) ? state.effects : [];
  const effect = [...effects].reverse().find(item => item.audio_status || item.asset_status);
  const status = String(effect?.audio_status || state.audio_status || 'idle');
  const asset = String(effect?.asset_status || '—');
  const labels = { queued: 'QUEUED', playing: 'PLAYING', finished: 'COMPLETED', unavailable: 'UNAVAILABLE', cancelled: 'CANCELLED', failed: 'FAILED', idle: 'IDLE' };
  const assetLabels = { placeholder: 'placeholder', recorded_pending_approval: 'awaiting approval', approved: 'approved', absent: 'missing' };
  const suffix = assetLabels[asset] ? ' · ' + assetLabels[asset] : '';
  if ($('audio-state')) $('audio-state').textContent = 'AUDIO: ' + (labels[status] || status.toUpperCase()) + suffix;
  if ($('effects-summary')) $('effects-summary').textContent = effect ? (effect.type || effect.action || 'effect') + ' · ' + (labels[status] || status) + suffix : 'Ожидание canonical effects.';
}

function renderStoryStatus(state) {
  const node = String(state.session?.node || '');
  const canonical = state.canonical_state || {};
  const storyId = canonical.active_story_id || '—';
  const playing = ['fixed_story_playing', 'personal_story_playing'].includes(node);
  const paused = node === 'story_paused';
  const pending = ['accepted', 'preparing'].includes(canonical.story_audio_phase);
  const mode = pending ? canonical.story_audio_phase : playing ? 'playing' : paused ? 'paused'
    : node === 'personal_story_generating' ? 'preparing'
    : node === 'story_pause_announcement' ? 'pausing'
    : node === 'story_resume_announcement' ? 'resuming'
    : ['fixed_story_menu', 'personal_story_theme'].includes(node) ? 'menu' : 'inactive';
  const last = [...(state.results || [])].reverse().find(item => /story|fixed_story|audio/i.test(String(item.reason || '') + JSON.stringify(item.result || {})));
  const checkpoint = canonical.dialogue_memory?.story_checkpoint;
  if ($('story-id')) $('story-id').textContent = storyId;
  if ($('story-playback')) $('story-playback').textContent = mode === 'playing' ? 'playing' : mode === 'paused' ? 'paused · resume available' : mode;
  if ($('story-last-event')) $('story-last-event').textContent = last?.result?.reason || last?.reason || '—';
  if ($('story-checkpoint')) $('story-checkpoint').textContent = checkpoint ? JSON.stringify(checkpoint) : '—';
  if ($('story-playback-badge')) $('story-playback-badge').textContent = mode.toUpperCase();
}

function eventSummary(item) {
  const type = String(item.type || item.event_type || 'event');
  const payload = item.payload || {};
  const labels = { HandsetLifted: 'Handset · lifted', HandsetPlaced: 'Handset · placed', ButtonPressed: 'Button ' + (payload.button_id || '—') + ' · pressed', ButtonReleased: 'Button ' + (payload.button_id || '—') + ' · released', SessionStarted: 'Session · started', SessionEnded: 'Session · ended', AudioCompleted: 'Audio · completed', AudioFailed: 'Audio · failed', ScenarioAdvanced: 'Scenario · ' + (payload.node_id || payload.to || 'advanced') };
  return labels[type] || type.replaceAll('_', ' ');
}

function effectSummary(value) {
  const result = value?.result || value || {};
  const outputs = Array.isArray(result.outputs) ? result.outputs : [];
  const effect = outputs.find(item => item && (item.type || item.action));
  const label = effect ? String(effect.type || effect.action).replaceAll('_', ' ') : 'no effects';
  const state = result.state || result.canonical_state?.mode || '—';
  const reason = result.reason ? String(result.reason).replaceAll('_', ' ') : '';
  return (reason || 'runtime result') + ' · state ' + state + ' · ' + label;
}

function resultSummary(item) {
  if (item.error) return (item.stage || 'runtime') + ' · ' + item.error;
  return effectSummary(item);
}

function renderHumanLog(id, items, summary, klass, payload) {
  const root = $(id);
  root.textContent = '';
  for (const item of items.slice(-100).reverse()) {
    const row = $('log-row').content.firstElementChild.cloneNode(true);
    row.querySelector('.log-meta').textContent = item.type ? item.type + ' #' + (item.sequence ?? '—') : (item.input || item.stage || item.error || 'runtime');
    row.querySelector('.log-summary').textContent = summary(item);
    const rowClass = klass(item);
    if (rowClass) row.classList.add(rowClass);
    const details = document.createElement('details');
    details.className = 'log-details';
    const toggle = document.createElement('summary');
    toggle.textContent = 'Technical details';
    const raw = document.createElement('pre');
    raw.textContent = JSON.stringify(payload(item), null, 2);
    details.append(toggle, raw);
    row.querySelector('.log-content').append(details);
    root.append(row);
  }
}

function renderLog(id, items, meta, value, klass) {
  const root = $(id);
  root.textContent = '';
  for (const item of items.slice(-100).reverse()) {
    const row = $('log-row').content.firstElementChild.cloneNode(true);
    row.querySelector('.log-meta').textContent = meta(item);
    const raw = row.querySelector('pre') || document.createElement('pre');
    if (!raw.parentElement) row.querySelector('.log-content').append(raw);
    raw.textContent = value(item);
    const rowClass = klass(item);
    if (rowClass) row.classList.add(rowClass);
    root.append(row);
  }
}

function handleAudioStop(message) {
  // UART stops also arrive in stand_state.commands. A delayed notification
  // must not cancel a newer phrase started from that same snapshot.
  const sequence = message.command_sequence;
  if (Number.isSafeInteger(sequence) && sequence > 0 && sequence <= lastSpokenCommand) return;
  freezePersonalStoryPosition();
  freezeStoryCheckpoint();
  checkpointPublicStoryTransfer(message.checkpoint_request);
  minimumPCMGeneration = Math.max(minimumPCMGeneration, Number(message.generation) || 0);
  failedPCMOwner = null;
  audioGeneration += 1; storySpeechGeneration += 1; pendingPCM.length = 0;
  for (const source of activeSources) { try { source.stop(); } catch (_) {} }
  activeSources.clear(); nextAudioTime = audioContext ? audioContext.currentTime : 0;
}

function discardStoppedStoryCheckpoint() {
  if (stoppedStoryCheckpointTimer !== null) window.clearTimeout(stoppedStoryCheckpointTimer);
  stoppedStoryCheckpointTimer = null;
  stoppedStoryCheckpoint = null;
}

function freezeStoryCheckpoint() {
  const playback = activeStoryPlayback;
  if (!playback || !audioContext || (!playback.paused && playback.generation !== storySpeechGeneration)) return;
  const offset = playback.paused ? playback.offset :
    Math.min(playback.buffer.duration, playback.offset + Math.max(0, audioContext.currentTime - playback.startedAt));
  discardStoppedStoryCheckpoint();
  // This receipt can only answer an exact server checkpoint request; it owns no audio.
  const checkpoint = {identity: playback.identity, offset_ms: Math.round(offset * 1000), expires: Date.now() + 5000};
  stoppedStoryCheckpoint = checkpoint;
  stoppedStoryCheckpointTimer = window.setTimeout(() => {
    if (stoppedStoryCheckpoint === checkpoint) discardStoppedStoryCheckpoint();
  }, 5000);
  playback.paused = true;
  activeStoryPlayback = null;
}

function checkpointPublicStoryTransfer(request) {
  if (!request) return;
  freezeStoryCheckpoint();
  const checkpoint = stoppedStoryCheckpoint;
  if (!checkpoint) return;
  if (Date.now() >= checkpoint.expires) { discardStoppedStoryCheckpoint(); return; }
  const identity = checkpoint.identity;
  if (request.story_id !== identity.response_id ||
      ['session_id', 'generation', 'token', 'playback_id', 'playback_lease'].some(key => request[key] !== identity[key])) return;
  // Consume locally before stop/onended, including duplicate wire commands.
  discardStoppedStoryCheckpoint();
  post('/stand/audio/checkpoint', {...request, offset_ms: checkpoint.offset_ms}).catch(() => {});
}

function discardPersonalStoryPlayback() {
  if (personalStoryReceiptTimer !== null) window.clearTimeout(personalStoryReceiptTimer);
  personalStoryReceiptTimer = null;
  personalStoryPlayback = null;
}

function freezePersonalStoryPosition() {
  const playback = personalStoryPlayback;
  if (!playback || playback.paused || !audioContext) return;
  let played = playback.played;
  for (const segment of playback.segments) played += Math.min(segment.duration, Math.max(0, audioContext.currentTime - segment.start));
  playback.offsetMs = Math.min(playback.durationMs ?? 180000, playback.offsetMs + Math.floor(played * 1000));
  playback.paused = true;
  playback.revoked.add(playback.identity.playback_lease);
  playback.segments.clear();
}

function audioIdentity(command) {
  return {
    session_id: command.payload.session_id,
    response_id: command.payload.response_id,
    playback_id: command.payload.playback_id,
    playback_lease: command.payload.playback_lease,
    generation: command.payload.generation,
    token: command.payload.token,
  };
}

async function playPhraseAsset(command, generation) {
  const response = await fetch(apiPath(`/stand/audio/phrase/${encodeURIComponent(command.payload.response_id)}`));
  if (!response.ok) throw new Error(`phrase audio HTTP ${response.status}`);
  const data = await response.arrayBuffer();
  const context = await unlockAudio();
  if (generation !== storySpeechGeneration) return;
  const buffer = await context.decodeAudioData(data);
  if (generation !== storySpeechGeneration) return;
  const source = context.createBufferSource();
  source.buffer = buffer;
  source.connect(context.destination);
  activeSources.add(source);
  source.onended = () => {
    activeSources.delete(source);
    if (generation !== storySpeechGeneration) return;
    post('/stand/audio/lifecycle', {...audioIdentity(command), status: 'finished'}).catch(() => {});
    post('/stand/audio/completed', audioIdentity(command)).catch(() => {});
  };
  source.start();
  post('/stand/audio/lifecycle', {...audioIdentity(command), status: 'playing'}).catch(() => {});
  $('audio-state').textContent = 'AUDIO: PHRASE / WAV';
}

async function playGenericAsset(command, generation) {
  const assetId = String(command.payload.asset_path || '');
  const response = await fetch(apiPath(`/stand/audio/asset/${encodeURIComponent(assetId)}`));
  if (!response.ok) throw new Error(`audio asset HTTP ${response.status}`);
  const data = await response.arrayBuffer();
  const context = await unlockAudio();
  if (generation !== storySpeechGeneration) return;
  const buffer = await context.decodeAudioData(data);
  if (generation !== storySpeechGeneration) return;
  const source = context.createBufferSource();
  source.buffer = buffer;
  source.connect(context.destination);
  activeSources.add(source);
  source.onended = () => {
    activeSources.delete(source);
    if (generation !== storySpeechGeneration) return;
    post('/stand/audio/lifecycle', {...audioIdentity(command), status: 'finished'}).catch(() => {});
    post('/stand/audio/completed', audioIdentity(command)).catch(() => {});
    $('audio-state').textContent = audioReady ? 'AUDIO: READY' : 'AUDIO: LOCKED';
  };
  source.start();
  post('/stand/audio/lifecycle', {...audioIdentity(command), status: 'playing'}).catch(() => {});
  $('audio-state').textContent = 'AUDIO: RINGTONE / WAV';
}

async function playStoryAsset(command, generation) {
  discardStoppedStoryCheckpoint();
  const storyId = command.payload.response_id, assetPath = command.payload.asset_path;
  const offsetMs = Number(command.payload.offset_ms || 0);
  // Only server-owned fixed public assets can retain one decoded buffer, never playback authority.
  const publicAsset = command.payload.content_kind === 'story' && /^fixed_[a-z0-9_]+$/.test(storyId)
    && typeof assetPath === 'string' && assetPath.length > 0;
  const cached = publicAsset && offsetMs > 0 && publicStoryBuffer?.storyId === storyId
    && publicStoryBuffer.assetPath === assetPath ? publicStoryBuffer.buffer : null;
  if (!cached) publicStoryBuffer = null;
  let buffer = cached, context;
  if (buffer) {
    context = await unlockAudio();
  } else {
    const response = await fetch(apiPath('/stand/audio/story/' + encodeURIComponent(storyId)));
    if (!response.ok) throw new Error('story audio HTTP ' + response.status);
    const data = await response.arrayBuffer();
    context = await unlockAudio();
    if (generation !== storySpeechGeneration || current?.session.node !== 'fixed_story_playing') return;
    buffer = await context.decodeAudioData(data);
  }
  if (generation !== storySpeechGeneration || current?.session.node !== 'fixed_story_playing') return;
  if (publicAsset) publicStoryBuffer = {storyId, assetPath, buffer};
  const source = context.createBufferSource();
  source.buffer = buffer;
  source.connect(context.destination);
  const offset = Math.min(buffer.duration, Math.max(0, offsetMs / 1000));
  const playback = {
    buffer, source, offset, startedAt: context.currentTime, paused: false, generation,
    identity: audioIdentity(command),
  };
  activeStoryPlayback = playback;
  activeSources.add(source);
  source.onended = () => {
    activeSources.delete(source);
    if (playback.paused || activeStoryPlayback !== playback || generation !== storySpeechGeneration) return;
    activeStoryPlayback = null;
    publicStoryBuffer = null;
    $('audio-state').textContent = audioReady ? 'AUDIO: READY' : 'AUDIO: LOCKED';
    post('/stand/audio/lifecycle', {...playback.identity, status: 'finished'}).catch(() => {});
    post('/stand/audio/completed', playback.identity).catch(() => {});
  };
  source.start(0, offset);
  post('/stand/audio/lifecycle', {...playback.identity, status: 'playing'}).catch(() => {});
  $('audio-state').textContent = 'AUDIO: STORY / SILERO';
}

function pauseStoryAsset() {
  const personal = personalStoryPlayback;
  if (personal) {
    if (personal.expires !== undefined && Date.now() >= personal.expires) { discardPersonalStoryPlayback(); return; }
    if (personal.checkpointSent) return;
    freezePersonalStoryPosition();
    audioGeneration += 1; pendingPCM.length = 0; audioQueueTail = Promise.resolve();
    for (const source of activeSources) { try { source.stop(); } catch (_) {} }
    activeSources.clear(); nextAudioTime = audioContext ? audioContext.currentTime : 0;
    personal.checkpointSent = true;
    post('/stand/audio/checkpoint', {...personal.identity, story_id: 'PERSONAL_STORY', offset_ms: personal.offsetMs}).catch(() => {});
    $('audio-state').textContent = 'AUDIO: STORY PAUSED';
    return;
  }
  const playback = activeStoryPlayback;
  if (!playback || playback.paused || !audioContext || playback.generation !== storySpeechGeneration) return;
  playback.offset = Math.min(playback.buffer.duration, playback.offset + Math.max(0, audioContext.currentTime - playback.startedAt));
  playback.paused = true;
  try { playback.source.stop(); } catch (_) {}
  activeSources.delete(playback.source);
  post('/stand/audio/checkpoint', {
    ...playback.identity, story_id: playback.identity.response_id,
    offset_ms: Math.round(playback.offset * 1000),
  }).catch(() => {});
  post('/stand/audio/lifecycle', {...playback.identity, status: 'paused'}).catch(() => {});
  $('audio-state').textContent = 'AUDIO: STORY PAUSED';
}

function resumeStoryAsset() {
  // Canonical story_audio_resume issues a new owned audio_play with the saved
  // millisecond checkpoint. Resuming this old source would duplicate playback.
  return;
}

function speakCommands(state) {
  if (state.session && (!interactionActive(state) || !['fixed_story_playing', 'story_paused',
      'story_pause_announcement', 'story_resume_announcement'].includes(state.session.node))) publicStoryBuffer = null;
  if (state.session && (!interactionActive(state) || !['personal_story_playing', 'story_paused',
      'story_pause_announcement', 'story_resume_announcement'].includes(state.session.node))) discardPersonalStoryPlayback();
  for (const command of state.commands) {
    if (command.sequence <= lastSpokenCommand) continue;
    if (state.simulator && suppressPhoneAudio && command.type === 'audio_play') {
      lastSpokenCommand = command.sequence;
      continue;
    }
    // Keep audio commands pending until a real user gesture unlocks the
    // browser. Autoplay policy otherwise makes the first response disappear.
    if ((command.type === 'audio_play' || command.type === 'audio_stop') && !audioReady) break;
    lastSpokenCommand = command.sequence;
    if (command.type === 'audio_stop') {
      freezeStoryCheckpoint();
      checkpointPublicStoryTransfer(command.payload?.checkpoint_request);
      interruptAudioForInput({preserveStoryCheckpoint: true});
      window.speechSynthesis?.cancel();
    }
    if (command.type === 'audio_pause') {
      pauseStoryAsset();
      continue;
    }
    if (command.type === 'audio_resume') {
      resumeStoryAsset();
      continue;
    }
    if (command.type === 'audio_play' && command.payload.content_kind === 'phrase' && command.payload.asset_path) {
      const generation = ++storySpeechGeneration;
      playPhraseAsset(command, generation).catch(error => {
        if (generation !== storySpeechGeneration) return;
        post('/stand/audio/failed', {...audioIdentity(command), error_code: 'browser_phrase_playback_failed' }).catch(() => {});
        showError(new Error(`Recorded phrase unavailable; audio path stopped: ${error.message}`));
      });
      continue;
    }
    if (command.type === 'audio_play' && command.payload.content_kind === 'asset' && command.payload.asset_path) {
      const generation = ++storySpeechGeneration;
      playGenericAsset(command, generation).catch(error => {
        if (generation !== storySpeechGeneration) return;
        post('/stand/audio/failed', {...audioIdentity(command), error_code: 'browser_asset_playback_failed'}).catch(() => {});
        showError(new Error('Audio asset unavailable: ' + error.message));
      });
      continue;
    }
    if (command.type === 'audio_play' && command.payload.content_kind === 'story' && command.payload.asset_path) {
      const generation = ++storySpeechGeneration;
      playStoryAsset(command, generation).catch(error => {
        if (generation !== storySpeechGeneration || current?.session.node !== 'fixed_story_playing') return;
        post('/stand/audio/failed', {...audioIdentity(command), error_code: 'browser_story_playback_failed' }).catch(() => {});
        showError(new Error('Silero story unavailable; audio path stopped: ' + error.message));
      });
      continue;
    }
  }
  // Bounded S2S text is rendered as PCM by BrowserPlaybackController. Do not
  // speak it again through speechSynthesis, otherwise every answer is doubled.
  lastResultCount = state.results.length;
}

function interactionGuide(state, media) {
  if (['accepted', 'preparing'].includes(state?.canonical_state?.story_audio_phase)) {
    return ['Выбор принят', 'Готовлю сказку. Можно подождать или нажать 7, чтобы вернуться.'];
  }
  if (state?.canonical_state?.personal_story_checkpoint_pending) {
    return ['Сказка на паузе', 'Сохраняем место в сказке. Подожди немного. Если продолжить не получилось, нажми 7 и выбери новую сказку.'];
  }
  if (state?.canonical_state?.public_story_checkpoint_pending) {
    return ['Сказка приостановлена', 'Восстанавливаем место в сказке. Если она не продолжилась, нажми 7 и выбери сказку заново.'];
  }
  if (state?.session?.state === 'ENDED' || state?.session?.node === 'session_closed') {
    return state.session?.handset_up
      ? ['Сеанс завершён', 'Сеанс завершён. Положи трубку, чтобы начать заново.']
      : ['Сеанс завершён', 'Поднимите трубку'];
  }
  if (state?.canonical_state?.silent_hold_active) {
    return ['Тихий режим', `Держи 6 (${state.canonical_state.silent_hold_step || 1} из 7), чтобы войти без звука…`];
  }
  if (state?.canonical_state?.mode === 'profile_ready') {
    return ['Карта принята', state?.simulator?.session?.session_actions?.includes('start')
      ? 'Карта принята. Нажми «Начать» (без звука) или подними трубку.'
      : 'Карта принята. Подними трубку, чтобы начать.'];
  }
  if (!interactionActive(state)) return state?.simulator?.session?.session_actions?.includes('start')
    ? ['Начнём?', 'Нажми «Начать» (без звука), держи 6 пять секунд или подними трубку.'] : null;
  const node = state.session.node;
  const page = state.canonical_state?.scene_menu_descriptor;
  if (['game_menu', 'fixed_story_menu', 'personal_story_theme', 'schedule_weather'].includes(node) && page?.scene === node) {
    const choices = (page.items || []).map(item => `${item.slot} — ${item.title}`);
    if ((page.buttons || []).includes(5) && !(page.items || []).some(item => item.slot === 5)) choices.push('5 — помощь');
    if ((page.buttons || []).includes(6)) choices.push('6 — следующие варианты');
    if ((page.buttons || []).includes(7)) choices.push('7 — назад');
    return [{game_menu: 'Игры', fixed_story_menu: 'Сказки', personal_story_theme: 'Новая сказка', schedule_weather: 'Мой день и погода'}[node], choices.join('; ') + '.'];
  }
  if (node === 'game_active') {
    const game = state.canonical_state || {};
    if (game.substate === 'game_exit_confirmation') return ['Закончить игру?', '6 — продолжить; 7 — закончить; 5 — помощь.'];
    const title = {free_lights: 'Живой свет', quiz: 'Вопросы Мишки', repeat_pattern: 'Повтори огоньки',
      odd_one_out: 'Выбери лишнее', word_game: 'Игра в слова'}[game.active_game_id] || 'Игра';
    if (game.game_exit_hold?.phase === 'ready') return [title, 'Теперь отпускай 7, чтобы выйти из игры.'];
    if (['pending','progress'].includes(game.game_exit_hold?.phase)) return [title, 'Держи 7. Огоньки заполняются. Когда они заполнятся, можно отпустить.'];
    if (game.game_audio_stage === 'prompt_failed') return [title, 'Не удалось озвучить вопрос. Нажми 5, чтобы повторить, или держи 7 для выхода.'];
    if (['starting', 'prompt'].includes(game.game_audio_stage)
      || ['instruction', 'awaiting_prompt'].includes(game.game_phase)) return [title,
        'Дослушай Мишку. Потом можно будет ответить. Для выхода держи 7 до подсказки «Теперь отпускай», затем отпусти.'];
    if (game.active_game_id === 'repeat_pattern' && game.game_phase === 'demonstrate') return [title, 'Посмотри, как загораются огоньки. Потом повтори последовательность. Для выхода держи 7 до подсказки «Теперь отпускай», затем отпусти.'];
    if (game.active_game_id === 'word_game') {
      const hint = media.pending ? 'Разреши микрофон в окне браузера и назови слово.'
        : !media.microphone ? 'Включи микрофон и назови слово.'
          : media.microphoneAccepting ? 'Назови слово для игры.' : 'Дослушай Мишку. Потом назови слово.';
      return [title, hint + ' Можно написать слово в «Инструментах». Для выхода держи 7 до подсказки «Теперь отпускай», затем отпусти.'];
    }
    return [title, {
      free_lights: 'Нажимай кнопки 1–7 и играй с огоньками. Для выхода держи 7 до подсказки «Теперь отпускай», затем отпусти.',
      repeat_pattern: 'Повтори подсвеченные кнопки в том же порядке. Для выхода держи 7 до подсказки «Теперь отпускай», затем отпусти.',
      odd_one_out: 'Нажми подсвеченную кнопку с лишним предметом. Для выхода держи 7 до подсказки «Теперь отпускай», затем отпусти.',
      quiz: 'Вопрос ' + (Number(game.game_round || 0) + 1) + ' из 8. Нажми подсвеченную кнопку с ответом. Для ответа 7 коротко нажми и отпусти. Для выхода держи 7 до подсказки «Теперь отпускай», затем отпусти.',
    }[game.active_game_id] || 'Следуй текущей подсветке и подсказке Мишки.'];
  }
  if (node === 'support_intake') {
    const text = media.pending
      ? 'Разреши микрофон в окне браузера. Или напиши, что случилось, в «Инструментах».'
      : !media.microphone
        ? (state.simulator && !state.simulator.microphone?.eligible
          ? 'Сейчас микрофон недоступен. Можно написать в «Инструментах».'
          : 'Включи микрофон и расскажи, что случилось. Можно написать в «Инструментах».')
        : media.microphoneAccepting
          ? 'Расскажи, что случилось, или задай вопрос. Можно написать в «Инструментах».'
          : 'Дослушай Мишку. Потом расскажи, что случилось, или напиши в «Инструментах».';
    return ['Расскажи Мишке', text + ' 7 — вернуться в меню.'];
  }
  if (node === 'bear_dialogue') {
    const offered = {free_lights: 'Живой свет', quiz: 'Вопросы Мишки', repeat_pattern: 'Повтори огоньки',
      odd_one_out: 'Выбери лишнее', word_game: 'Игра в слова'}[state.canonical_state?.pending_game_offer];
    if (offered) return ['Поиграем в «' + offered + '»?', state.canonical_state.game_offer_ready
      ? '1 — начать; 7 — продолжить разговор. Можно сказать «да» или «нет».'
      : 'Дослушай предложение Мишки. 7 — продолжить разговор.'];
    const text = media.pending
      ? 'Разреши микрофон в окне браузера. Или напиши Мишке в «Инструментах».'
      : !media.microphone
        ? (state.simulator && !state.simulator.microphone?.eligible
          ? 'Сейчас микрофон недоступен. Можно написать в «Инструментах».'
          : 'Включи микрофон и поговори с Мишкой. Можно написать в «Инструментах».')
        : media.microphoneAccepting
          ? 'Расскажи Мишке что-нибудь или задай вопрос. Можно написать в «Инструментах».'
          : 'Дослушай Мишку. Потом расскажи что-нибудь или задай вопрос. Можно написать в «Инструментах».';
    return ['Поговорим', (media.audioRetry ? 'Мишка не расслышал. Попробуй ещё раз. ' : '') + text + ' 7 — вернуться в меню.'];
  }
  if (node === 'exhibition_name_capture') {
    const text = media.pending
      ? 'Разреши микрофон в окне браузера. Или напиши своё имя в «Инструментах».'
      : !media.microphone
        ? 'Включи микрофон и назови своё имя. Можно написать его в «Инструментах».'
        : media.microphoneAccepting
          ? 'Назови своё имя. Можно написать его в «Инструментах».'
          : 'Дослушай Мишку. Потом назови своё имя или напиши его в «Инструментах».';
    return ['Как тебя зовут?', text];
  }
  return {
    voice_welcome: ['Знакомство', 'Сейчас Мишка поздоровается. Подожди немного.'],
    exhibition_introduction: ['Знакомство', 'Дослушай Мишку.'],
    exhibition_name_question: ['Как тебя зовут?', 'Дослушай вопрос Мишки.'],
    exhibition_age_prompt: ['Сколько тебе лет?', 'Дослушай Мишку. Потом можно будет ответить.'],
    exhibition_age_hint: ['Сколько тебе лет?', 'Дослушай Мишку. Потом можно будет ответить.'],
    exhibition_age_selection: ['Сколько тебе лет?', 'Нажми цифру своего возраста (1–7). Можно включить микрофон или написать возраст в «Инструментах».'],
    exhibition_age_complete: ['Знакомство', 'Мишка завершает знакомство. Подожди немного.'],
    exhibition_onboarding_complete: ['Знакомство завершено', 'Сейчас появится меню. Подожди немного.'],
    fixed_story_playing: ['Сказка', 'Слушай сказку. 6 — пауза; 7 — выйти из сказки.'],
    personal_story_generating: ['Новая сказка', 'Мишка готовит сказку. Подожди немного. 7 — выйти, если ждать не хочется.'],
    personal_story_playing: ['Новая сказка', 'Слушай сказку. 6 — пауза; 7 — выйти из сказки.'],
    story_pause_announcement: ['Сказка на паузе', 'Дослушай Мишку. Затем можно будет продолжить сказку.'],
    story_paused: ['Сказка на паузе', '6 — продолжить сказку; 7 — выйти из сказки.'],
    story_resume_announcement: ['Сказка', 'Дослушай Мишку. Сказка скоро продолжится.'],
    awaiting_handset: ['Подними трубку', 'Подними трубку, чтобы Мишка начал. 7 — вернуться в меню.'],
    session_closed: state.session?.handset_up
      ? ['Сеанс завершён', 'Сеанс завершён. Положи трубку, чтобы начать заново.']
      : ['Сеанс завершён', 'Поднимите трубку'],
    safety_stop: ['Позови взрослого', 'Позови взрослого рядом. Положи трубку, чтобы завершить разговор.'],
    wellbeing_check: ['Нужна помощь?', (state.canonical_state?.support_question === 'fear' ? 'Тебе страшно?' : 'Что-то болит или неприятно?') + ' 1 — нет; 7 — нужна помощь взрослого; 5 — повторить вопрос.'],
  }[node] || null;
}

function updateGuide(state, contract = validateButtonContract(state.button_contract)) {
  const helpAlert = $('phone-support-alert');
  const support = state.canonical_state?.support_alert;
  if (helpAlert) {
    const visible = state.canonical_state?.mode === 'safety_stop'
      && ['help_requested', 'urgent_safety'].includes(support?.kind);
    helpAlert.hidden = !visible;
    helpAlert.textContent = visible
      ? 'Нужна помощь взрослого рядом. Подтверждение взрослого не получено.' : '';
  }
  const skipHint = 'Нажмите любую кнопку, чтобы пропустить подсказку. Затем нажмите нужную кнопку для выбора.';
    const labels = {
    profile_ready: ['Карта принята', 'Поднимите трубку, чтобы начать.'],
    game_menu: ['Игры', 'Варианты загружаются из текущего меню.'],
    game_active: ['1 · Игра', 'Следуйте текущей подсветке.'],
    fixed_story_menu: ['Сказки', 'Варианты загружаются из текущего меню.'],
    fixed_story_playing: ['2 · Сказка', 'Идёт утверждённая запись. 6 — пауза; 7 — остановить сказку.'],
    support_intake: ['4 · Расскажи Мишке', 'Расскажите, спросите или сообщите, что случилось.'],
    personal_story_theme: ['3 · Новые сказки', 'Выберите одну из пяти историй.'],
    wellbeing_check: ['5 · Помощь', '1 — нет, 7 — да.'],
    bear_dialogue: ['6 · Ограниченный разговор', 'Короткий разговор по безопасным темам.'],
    schedule_weather: ['7 · Мой день и погода', 'Варианты загружаются из текущего меню.'],
    safety_stop: ['Разговор остановлен', 'Позови взрослого рядом. Положи трубку, чтобы завершить разговор.'],
    root_menu: ['Главное меню', 'Выберите действие кнопкой 1–7.'],
    exhibition_menu_tutorial_intro: ['Меню', skipHint],
    exhibition_menu_tutorial_prompt: ['Меню', skipHint],
    exhibition_menu_tutorial_buttons: ['Меню', skipHint],
    exhibition_menu_wave: ['Меню · финальная волна', skipHint],
    exhibition_menu_repeat_prompt: ['Повторить меню?', skipHint],
    exhibition_menu_repeat_listen: ['Повторить меню?', 'Скажите «да» или «нет». ' + skipHint],
    exhibition_menu_replay_buttons: ['Меню · повтор', skipHint],
    menu_ready: ['Меню готово', 'Выберите действие кнопкой 1–7.']
  };
  const node = ['profile_ready'].includes(state.canonical_state?.mode)
    ? state.canonical_state.mode : String(state.session.node || '');
  const guideMap = document.querySelector('.guide-map');
  if (guideMap) guideMap.hidden = node === 'bear_dialogue';
  const map = {
    root_menu: ['Главное меню', buttonLegendSummary(contract)],
    game_menu: ['Игры', 'Варианты загружаются из текущего меню.'],
    fixed_story_menu: ['Сказки', 'Варианты загружаются из текущего меню.'],
    bear_dialogue: ['Ограниченный разговор', 'Короткий разговор по безопасным темам.'],
    safety_stop: ['Разговор остановлен', 'Позови взрослого рядом. Положи трубку, чтобы завершить разговор.'],
    game_active: ['Игра', 'Нажимайте кнопки согласно подсветке.'],
    session_closed: ['Сессия завершена', 'Нажмите Reset, чтобы начать следующий тест.']
  };
    const normalizedState = String(state.session.state || '').trim().toUpperCase();
  const content = interactionGuide(state, {microphone: Boolean(mic), pending: micStarting, microphoneAccepting: microphoneAccepting(), audioRetry: Boolean(audioRetry)})
    || (normalizedState === 'ENDED' ? map.session_closed : ((labels[node] || map[node] || (['NONE', 'IDLE', 'STANDBY'].includes(normalizedState) ? ['Готов к знакомству', 'Поднимите трубку или подайте действие через симулятор.'] : ['Тест продолжается', 'Следуйте текущему состоянию.']))));
  if (normalizedState !== 'ENDED' && node === 'game_active' && state.canonical_state?.substate === 'game_exit_confirmation') {
    content[0] = 'Закончить игру?';
    content[1] = '6 — продолжить; 7 — закончить; 5 — помощь.';
  }
  if (normalizedState !== 'ENDED' && node === 'wellbeing_check' && !interactionActive(state)) {
    content[1] = (state.canonical_state?.support_question === 'fear' ? 'Тебе страшно?' : 'Что-то болит или неприятно?') + ' 1 — нет; 7 — нужна помощь взрослого; 5 — повторить.';
  }
  const unavailable = state.simulator && (!state.simulator.admission?.input_ready || !state.simulator.admission?.connected);
  $('guide-title').textContent = unavailable ? 'Телефон пока недоступен' : content[0];
  $('guide-text').textContent = unavailable ? (!state.simulator.admission?.input_ready
    ? 'Сценарий ещё не готов. Дождитесь готовности сервиса.' : 'Виртуальный ввод отключён. Подключите его в инструментах.') : content[1];
}

function enqueuePCM(message, generation = audioGeneration) {
  const textAttempt = pcmTextAttempt;
  audioQueueTail = audioQueueTail.catch(() => {}).then(() => playPCM(message, generation));
  // WebSocket handlers do not await playback. Report errors without leaving an
  // unhandled rejection; callers can still observe the returned failed task.
  audioQueueTail.catch(error => {
    if (generation !== audioGeneration) return;
    if (message.private === true) { showError(error); return; }
    if (textAttempt !== pcmTextAttempt) return;
    const action = micStarting ? 'Разреши микрофон в окне браузера и спроси ещё раз.'
      : !mic ? 'Включи микрофон и спроси ещё раз.'
        : microphoneAccepting() ? 'Спроси ещё раз через микрофон.' : 'Сейчас приём речи на паузе.';
    const text = 'Не получилось воспроизвести ответ. ' + action
      + ' Можно написать в «Инструментах». Чтобы закончить, положи трубку.';
    pcmPlaybackError = {text, owner: ordinaryPCMOwner(message)};
    showError(new Error(text));
  });
  return audioQueueTail;
}

function ordinaryPCMOwner(message) {
  if (message.private === true || typeof message.session_id !== 'string' || !message.session_id
    || !Number.isSafeInteger(message.generation) || message.generation < 0
    || !(Number.isSafeInteger(message.token) || (typeof message.token === 'string' && message.token.length > 0 && message.token.length <= 256))) return null;
  return {sessionId: message.session_id, generation: message.generation, token: message.token};
}

function samePCMOwner(left, right) {
  return Boolean(left && right && left.sessionId === right.sessionId
    && left.generation === right.generation && left.token === right.token);
}

function privatePCMIdentity(message) {
  if (message.private !== true) return null;
  const identity = message.playback;
  if (!identity || !['session_id', 'response_id', 'playback_id', 'playback_lease'].every(
    key => typeof identity[key] === 'string' && identity[key].length > 0 && identity[key].length <= 256)
    || identity.session_id !== message.session_id
    || !Number.isSafeInteger(identity.generation) || identity.generation < 0
    || !(Number.isSafeInteger(identity.token) || (typeof identity.token === 'string' && identity.token.length > 0 && identity.token.length <= 256))) {
    throw new Error('Private audio playback identity is invalid');
  }
  return Object.freeze({session_id: identity.session_id, response_id: identity.response_id,
    playback_id: identity.playback_id, playback_lease: identity.playback_lease,
    generation: identity.generation, token: identity.token});
}

async function playPCM(message, generation = audioGeneration) {
  if (generation !== audioGeneration) return;
  const owner = ordinaryPCMOwner(message);
  if (samePCMOwner(owner, failedPCMOwner)) return;
  const identity = privatePCMIdentity(message);
  let source;
  try {
    const context = await unlockAudio();
    if (generation !== audioGeneration || samePCMOwner(owner, failedPCMOwner)) return;
    if (context.state !== 'running') {
      if (pendingPCM.length >= 32) pendingPCM.shift();
      pendingPCM.push({message, generation});
      $('audio-state').textContent = 'AUDIO: LOCKED';
      return;
    }
    if (generation !== audioGeneration) return;
    let personal = null;
    if (identity?.response_id === 'PERSONAL_STORY') {
      if (current?.session.node !== 'personal_story_playing' || !interactionActive(current)
          || message.generation < minimumPCMGeneration) return;
      const offsetMs = message.story_offset_ms, durationMs = message.story_duration_ms;
      const growing = message.story_streaming === true && durationMs === null && message.story_duration_limit_ms === 180000;
      if (!Number.isSafeInteger(offsetMs) || offsetMs < 0
          || (growing ? offsetMs > 180000 : (!Number.isSafeInteger(durationMs)
            || offsetMs > durationMs || durationMs <= 0 || durationMs > 300000))) throw new Error('Private story position is invalid');
      const previous = personalStoryPlayback;
      const same = previous && Object.keys(identity).every(key => previous.identity[key] === identity[key]);
      if (same) {
        if (previous.paused || message.first || previous.initialOffsetMs !== offsetMs || previous.durationMs !== durationMs) return;
      } else {
        if (!message.first || (previous && (!previous.paused || previous.revoked.has(identity.playback_lease)))) return;
        discardPersonalStoryPlayback();
        personalStoryPlayback = {identity, offsetMs, initialOffsetMs: offsetMs, durationMs,
          paused: false, checkpointSent: false, played: 0, segments: new Set(), revoked: previous?.revoked || new Set()};
      }
      personal = personalStoryPlayback;
    }
    const raw = Uint8Array.from(atob(message.data_b64), character => character.charCodeAt(0));
    const encoding = String(message.encoding || "s16le").toLowerCase();
    const isFloat32 = encoding === "f32le";
    const bytesPerSample = isFloat32 ? 4 : 2;
    const sampleCount = Math.floor(raw.byteLength / bytesPerSample);
    const buffer = context.createBuffer(1, sampleCount, message.sample_rate);
    const channel = buffer.getChannelData(0);
    if (isFloat32) {
      const view = new DataView(raw.buffer, raw.byteOffset, sampleCount * 4);
      for (let index = 0; index < sampleCount; index++) channel[index] = Math.max(-1, Math.min(1, view.getFloat32(index * 4, true)));
    } else {
      const view = new DataView(raw.buffer, raw.byteOffset, sampleCount * 2);
      for (let index = 0; index < sampleCount; index++) channel[index] = view.getInt16(index * 2, true) / 32768;
    }
    source = context.createBufferSource();
    if (owner) source.pcmOwner = owner;
    source.buffer = buffer;
    source.connect(context.destination);
    activeSources.add(source);
    let ended = false;
    let segment;
    source.onended = () => {
      activeSources.delete(source);
      if (ended || generation !== audioGeneration) return;
      if (personal && (personal.paused || personalStoryPlayback !== personal)) return;
      ended = true;
      if (personal && segment) { personal.segments.delete(segment); personal.played += segment.duration; }
      if (!activeSources.size) $('audio-state').textContent = audioReady ? 'AUDIO: READY' : 'AUDIO: LOCKED';
      if (identity && message.final === true) {
        if (personal) {
          freezePersonalStoryPosition();
          personal.segments.clear(); personal.revoked.add(identity.playback_lease);
          // A server-admitted pause can arrive after the final chunk ended.
          // Retain only its position receipt until canonical cleanup, never PCM/source authority.
          personalStoryPlayback = {identity, offsetMs: personal.durationMs ?? personal.offsetMs, durationMs: personal.durationMs,
            paused: true, checkpointSent: false, segments: new Set(), revoked: personal.revoked, expires: Date.now() + 5000};
          const receipt = personalStoryPlayback;
          personalStoryReceiptTimer = window.setTimeout(() => {
            if (personalStoryPlayback === receipt) discardPersonalStoryPlayback();
          }, 5000);
        }
        post('/stand/audio/lifecycle', {...identity, status: 'finished'}).catch(() => {});
        post('/stand/audio/completed', identity).catch(() => {});
      }
    };
    nextAudioTime = Math.max(context.currentTime, nextAudioTime);
    source.start(nextAudioTime);
    if (owner && pcmPlaybackError?.owner && !samePCMOwner(owner, pcmPlaybackError.owner)) clearPCMPlaybackError();
    if (personal) {
      const endMarker = message.stream_end === true && message.final === true && sampleCount === 1 && channel[0] === 0;
      segment = {start: nextAudioTime, duration: endMarker ? 0 : buffer.duration};
      personal.segments.add(segment);
    }
    nextAudioTime += buffer.duration;
    if (identity && message.first === true) {
      post('/stand/audio/lifecycle', {...identity, status: 'playing'}).catch(() => {});
    }
    $('audio-state').textContent = 'AUDIO: PLAYING';
  } catch (error) {
    if (source) {
      source.onended = null;
      activeSources.delete(source);
      try { source.stop(); } catch (_) {}
    }
    if (owner && generation === audioGeneration) {
      failedPCMOwner = owner;
      for (let index = pendingPCM.length - 1; index >= 0; index--) {
        if (samePCMOwner(ordinaryPCMOwner(pendingPCM[index].message), owner)) pendingPCM.splice(index, 1);
      }
      for (const active of activeSources) {
        if (!samePCMOwner(active.pcmOwner, owner)) continue;
        active.onended = null;
        activeSources.delete(active);
        try { active.stop(); } catch (_) {}
      }
      if (!activeSources.size) {
        nextAudioTime = audioContext ? audioContext.currentTime : 0;
        $('audio-state').textContent = audioReady ? 'AUDIO: READY' : 'AUDIO: LOCKED';
      }
    }
    if (identity && generation === audioGeneration) {
      post('/stand/audio/failed', {...identity, error_code: 'browser_private_pcm_failed'}).catch(() => {});
    }
    throw error;
  }
}

function initDiagnostics() {
  const root = $('button-status');
  for (const id of BUTTON_IDS) {
    const row = document.createElement('tr');
    row.innerHTML = `<td>${id}</td><td id="button-input-${id}">RELEASED</td><td id="button-led-${id}" class="diag-color off">OFF</td><td id="button-enabled-${id}">NO</td>`;
    root.append(row);
  }
}

function releaseButton(id, sessionId = session(), cancelled = false, owner = null) {
  if (buttonInputs[id] !== 'pressed' || (owner !== null && buttonInputOwners.get(id) !== owner)) return Promise.resolve(null);
  buttonInputs[id] = 'released';
  buttonInputOwners.delete(id);
  const button = buttonRefs.get(id);
  if (button) button.classList.remove('pressed');
  return post('/stand/button-released', { session_id: sessionId, button_id: Number(id), state: 'released', ...(cancelled ? {cancelled:true} : {}) });
}

function createButtons() {
  for (const id of BUTTON_IDS) {
    const button = document.createElement('button');
    button.className = 'physical-button led-off disabled';
    button.type = 'button';
    button.disabled = true;
    const symbol = id === 6 ? String.fromCodePoint(0x1f43b) : id === 7 ? String.fromCodePoint(0x2600) : '';
    const symbolClass = id === 6 ? 'bear' : id === 7 ? 'weather' : '';
    button.innerHTML = '<span class="button-shell"><span class="button-base" aria-hidden="true"></span><span class="button-led-disc" aria-hidden="true"></span><span class="button-cap" aria-hidden="true"></span>'
      + (symbol ? '<span class="button-symbol ' + symbolClass + '" aria-hidden="true">' + symbol + '</span>' : '')
      + '<span class="button-digit">' + id + '</span></span>';
    button.setAttribute('aria-label', `Кнопка ${id}, OFF, RELEASED, disabled`);
    let down = 0;
    button.onpointerdown = async event => {
      event.preventDefault();
      if (buttonInputs[id] === 'pressed') return;
      down = performance.now();
      buttonInputs[id] = 'pressed';
      buttonInputOwners.set(id,event.pointerId);
      button.classList.add('pressed');
      button.setPointerCapture(event.pointerId);
      try { await post('/stand/button', { button_id: Number(id), state: 'pressed' }); } catch (_) { /* shown in the error panel */ }
    };
    button.onpointerup = async event => {
      event.preventDefault();
      if (buttonInputs[id] !== 'pressed' || buttonInputOwners.get(id) !== event.pointerId) return;
      if (Number(id) === 7) {
        await releaseButton(id,session(),false,event.pointerId).catch(() => {});
        return;
      }
      button.classList.remove('pressed');
      const duration = Math.round(performance.now() - down);
      const sessionId = session();
      if (duration >= 5000) post('/stand/button-long-hold', { session_id: sessionId, button_id: Number(id), duration_ms: duration }).catch(() => {});
      else if (duration >= 600) post('/stand/button-hold', { session_id: sessionId, button_id: Number(id), duration_ms: duration }).catch(() => {});
      await releaseButton(id, sessionId,false,event.pointerId).catch(() => {});
    };
    button.onpointercancel = event => releaseButton(id,session(),true,event.pointerId).catch(() => {});
    button.onlostpointercapture = event => releaseButton(id,session(),true,event.pointerId).catch(() => {});
    button.onblur = () => releaseButton(id,session(),true).catch(() => {});
    buttonRefs.set(id, button);
    $('buttons').append(button);
  }
}

$('enable-audio').onclick = async () => {
  try {
    const context = await unlockAudio();
    if (context.state === 'running') {
      const source = context.createBufferSource();
      source.buffer = context.createBuffer(1, 1, context.sampleRate);
      source.connect(context.destination);
      source.start();
      $('audio-state').textContent = 'AUDIO: READY';
      $('audio-help').textContent = 'Звук включён; playback attempt выполнен, ответы приходят PCM/WAV-потоком.';
      if (current) speakCommands(current);
    } else {
      $('audio-state').textContent = 'AUDIO: LOCKED';
      $('audio-help').textContent = 'Браузер не разрешил звук; нажмите кнопку ещё раз или проверьте вкладку.';
    }
  } catch (error) {
    $('audio-state').textContent = 'AUDIO: UNAVAILABLE';
    $('audio-help').textContent = error.message || 'Web Audio API недоступен в этом браузере.';
    showError(error);
  }
};
const handsetUpButton = $('handset-up');
if (handsetUpButton) handsetUpButton.onclick = () => post('/stand/handset', { state: 'up' });
const handsetDownButton = $('handset-down');
if (handsetDownButton) handsetDownButton.onclick = () => { clearMic(); return post('/stand/handset', { state: 'down' }); };
const resetButton = $('reset');
if (resetButton) resetButton.onclick = () => { clearMic(); return post('/stand/reset'); };
function interruptAudioForInput({preserveStoryCheckpoint = false, preservePCMFailure = false} = {}) {
  if (!preserveStoryCheckpoint) { discardStoppedStoryCheckpoint(); publicStoryBuffer = null; }
  if (preserveStoryCheckpoint) freezePersonalStoryPosition();
  else discardPersonalStoryPlayback();
  micInputGain = 1;
  audioGeneration += 1;
  if (!preservePCMFailure) { failedPCMOwner = null; clearPCMPlaybackError(); }
  storySpeechGeneration += 1;
  pendingPCM.length = 0;
  for (const source of activeSources) {
    try { source.stop(); } catch (_) { /* already ended */ }
  }
  activeSources.clear();
  activeStoryPlayback = null;
  nextAudioTime = audioContext ? audioContext.currentTime : 0;
  audioQueueTail = Promise.resolve();
  $('audio-state').textContent = audioReady ? 'AUDIO: READY' : 'AUDIO: LOCKED';
}

$('text-form').onsubmit = event => {
  event.preventDefault();
  const text = $('text').value.trim();
  if (!text) return;
  // Late chunks from the failed server turn stay fenced until its audio_stop.
  interruptAudioForInput({preservePCMFailure: true});
  post('/stand/text', { text });
  $('text').value = '';
};
document.querySelectorAll('[data-prompt]').forEach(button => {
  button.onclick = () => { $('text').value = button.dataset.prompt; $('text-form').requestSubmit(); };
});
$('send-raw').onclick = () => post('/stand/raw', { frame: $('raw').value });
$('export').onclick = () => {
  if (!current) return;
  const blob = new Blob([JSON.stringify(current, null, 2)], { type: 'application/json' });
  const link = document.createElement('a');
  link.href = URL.createObjectURL(blob);
  link.download = `mishka-stand-${session()}.json`;
  link.click();
  URL.revokeObjectURL(link.href);
};
$('wav').onchange = async event => {
  if (current?.simulator && (!wsSnapshotReady || wsReloadRequired)) {
    showError(new Error(wsReloadRequired ? 'Сервис перезапущен. Обновите страницу, чтобы продолжить.' : 'Дождитесь актуального состояния телефона.'));
    return;
  }
  const file = event.target.files[0];
  if (!file) return;
  const response = await fetch(apiPath(`/stand/audio/wav?session_id=${encodeURIComponent(session())}`), { method: 'POST', headers: { 'content-type': 'audio/wav' }, body: file });
  if (!response.ok) showError(new Error(await response.text()));
};
function bytesToB64(buffer) { const bytes = new Uint8Array(buffer); let output = ''; for (let index = 0; index < bytes.length; index += 8192) output += String.fromCharCode(...bytes.subarray(index, index + 8192)); return btoa(output); }
function createMicResampler(sourceRate, sendFrame) {
  const step = sourceRate / 16000;
  let inputIndex = 0;
  let nextOutput = 0;
  let previous = 0;
  let pending = [];
  const flush = () => {
    if (!pending.length) return;
    sendFrame(new Float32Array(pending));
    pending = [];
  };
  return {
    feed(input) {
      for (const sample of input) {
        while (nextOutput <= inputIndex) {
          const fraction = nextOutput - Math.floor(nextOutput);
          pending.push(fraction ? previous * (1 - fraction) + sample * fraction : sample);
          if (pending.length === 512) flush();
          nextOutput += step;
        }
        previous = sample;
        inputIndex += 1;
      }
    },
    flush,
    reset() { inputIndex = 0; nextOutput = 0; previous = 0; pending = []; }
  };
}
function microphoneEligible() {
  if (authBlocked) return false;
  if (current?.simulator) return Boolean(current.simulator.microphone?.eligible);
  const micCapableNodes = new Set(['bear_dialogue', 'support_intake', 'voice_welcome', 'exhibition_name_capture', 'exhibition_age_selection', 'personal_story_theme', 'wellbeing_check', 'schedule_weather', 'game_menu', 'fixed_story_menu']);
  return Boolean(current && micCapableNodes.has(String(current.session?.node || '')));
}
function microphoneOwnerIsCurrent(owner = micOwner) {
  if (!owner || authBlocked || ws !== owner.socket || ws?.readyState !== WebSocket.OPEN
    || session() !== owner.sessionId || !interactionActive(current)) return false;
  if (current.simulator) return wsSnapshotReady && !wsReloadRequired && current.simulator.session.session_id === owner.sessionId
    && current.simulator.epoch === owner.epoch
    && current.simulator.admission?.input_ready === true && current.simulator.admission?.connected === true;
  return !['NONE', 'IDLE', 'ENDED'].includes(String(current.session.state).toUpperCase());
}
function microphoneAccepting() {
  return Boolean(mic && microphoneOwnerIsCurrent() && microphoneEligible());
}
function syncMicAdmission() {
  if (!(mic || micStarting)) return;
  if (!microphoneOwnerIsCurrent()) { clearMic(); return; }
  if (!microphoneEligible()) { mic?.resampler.reset(); micInputGain = 1; }
  $('mic-state').textContent = microphoneAccepting()
    ? 'Микрофон включён · Мишка слушает' : 'Микрофон включён · приём на паузе';
}
async function startMic() {
  if (mic || micStarting) return;
  if (!microphoneEligible() || ws?.readyState !== WebSocket.OPEN || (current?.simulator && (!wsSnapshotReady || wsReloadRequired))) {
    throw new Error('Микрофон доступен, когда Мишка готов слушать и соединение установлено.');
  }
  const generation = ++micGeneration;
  const owner = Object.freeze({sessionId: session(), epoch: current?.simulator?.epoch, socket: ws});
  micOwner = owner;
  micStarting = true;
  let stream;
  try {
    const context = await unlockAudio();
    if (generation !== micGeneration || !microphoneOwnerIsCurrent(owner)) return;
    if (!navigator.mediaDevices?.getUserMedia) throw new Error('Микрофон недоступен. Откройте телефон по HTTPS и разрешите доступ в браузере.');
    stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
    if (generation !== micGeneration || !microphoneOwnerIsCurrent(owner)) {
      stream.getTracks().forEach(track => track.stop());
      return;
    }
    const source = context.createMediaStreamSource(stream);
    const sessionId = owner.sessionId;
    const resampler = createMicResampler(context.sampleRate, samples => {
      if (generation !== micGeneration || !microphoneOwnerIsCurrent(owner) || !microphoneEligible()) return;
      owner.socket.send(JSON.stringify({ type: 'audio_pcm', session_id: sessionId, sample_rate: 16000, data_b64: bytesToB64(samples.buffer) }));
    });
    const node = context.createScriptProcessor(1024, 1, 1);
    node.onaudioprocess = event => {
      if (generation !== micGeneration) return;
      if (!microphoneOwnerIsCurrent(owner)) { clearMic(); return; }
      if (!microphoneEligible()) { resampler.reset(); micInputGain = 1; return; }
      const input = event.inputBuffer.getChannelData(0);
      const playbackActive = activeSources.size > 0;
      const catalogueInput = ['game_menu', 'fixed_story_menu'].includes(String(current?.session?.node || '')) && microphoneEligible();
      const targetGain = playbackActive && !catalogueInput ? MIC_DUCK_GAIN : 1;
      const alpha = playbackActive ? MIC_DUCK_ATTACK : MIC_DUCK_RELEASE;
      micInputGain += (targetGain - micInputGain) * alpha;
      const ducked = new Float32Array(input.length);
      for (let index = 0; index < input.length; index++) ducked[index] = input[index] * micInputGain;
      resampler.feed(ducked);
    };
    const mute = context.createGain();
    mute.gain.value = 0;
    source.connect(node); node.connect(mute); mute.connect(context.destination);
    mic = { stream, source, node, mute, resampler, sessionId };
    $('mic').textContent = 'Stop microphone';
    syncMicAdmission();
    for (const track of stream.getTracks()) track.onended = () => { if (mic?.stream === stream) clearMic(); };
  } catch (error) {
    stream?.getTracks().forEach(track => track.stop());
    if (generation === micGeneration) throw error;
  } finally {
    if (generation === micGeneration) { micStarting = false; if (!mic) micOwner = null; }
    window.mishkaPhoneMedia?.();
  }
}
function clearMic() {
  audioRetry = null;
  micGeneration += 1;
  micStarting = false;
  micOwner = null;
  if (mic) {
    const previous = mic;
    mic = null;
    previous.node.onaudioprocess = null;
    previous.node.disconnect(); previous.source.disconnect(); previous.mute?.disconnect();
    previous.stream.getTracks().forEach(track => { track.onended = null; track.stop(); });
  }
  $('mic').textContent = 'Start microphone'; $('mic-state').textContent = 'idle';
  window.mishkaPhoneMedia?.();
}
function stopMic() {
  try {
    if (microphoneAccepting()) {
      mic.resampler.flush();
      micOwner.socket.send(JSON.stringify({ type: 'flush_audio', session_id: micOwner.sessionId }));
    }
  } finally { clearMic(); }
}
$('mic').onclick = async () => {
  try {
    if (!mic && !micStarting) { interruptAudioForInput(); return await startMic(); }
    stopMic();
  } catch (error) { showError(error); }
};

initDiagnostics();
createButtons();
startPanelLifecycle();

document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    stopPolling({ abortRefresh: true });
    if (reconnectTimer !== null) {
      window.clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    if (ws && [WebSocket.OPEN, WebSocket.CONNECTING].includes(ws.readyState)) {
      wsIntentionalClose = true;
      try { ws.close(1000, 'panel hidden'); } catch (_) {}
    }
    clearMic();
    interruptAudioForInput();
    return;
  }
  startPanelLifecycle();
});

window.addEventListener('pagehide', () => {
  pageLifecycleStopped = true;
  stopPolling({ abortRefresh: true });
  if (reconnectTimer !== null) window.clearTimeout(reconnectTimer);
  reconnectTimer = null;
  if (ws && [WebSocket.OPEN, WebSocket.CONNECTING].includes(ws.readyState)) {
    wsIntentionalClose = true;
    try { ws.close(1000, 'page unload'); } catch (_) {}
  }
  clearMic();
  interruptAudioForInput();
});

window.addEventListener('keydown', event => {
  if (document.body.dataset.phone) return;
  if (event.repeat || ['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement?.tagName)) return;
  const id = Number(event.key);
  if (BUTTON_IDS.includes(id)) {
    event.preventDefault();
    const button = buttonRefs.get(id);
    if (button && !button.disabled) {
      if (buttonInputs[id] === 'pressed') return;
      buttonInputs[id] = 'pressed';
      buttonInputOwners.set(id,event.code);
      button.classList.add('pressed');
      post('/stand/button', { button_id: id, state: 'pressed' }).catch(() => {});
    }
  } else if (event.code === 'Space') {
    event.preventDefault();
    const up = current?.session?.handset_up;
    post('/stand/handset', { state: up ? 'down' : 'up' }).catch(() => {});
  }
});

window.addEventListener('blur', () => {
  if (document.body.dataset.phone) return;
  for (const id of BUTTON_IDS) releaseButton(id,session(),true).catch(() => {});
});
window.addEventListener('keyup', event => {
  if (document.body.dataset.phone) return;
  const id = Number(event.key);
  if (!BUTTON_IDS.includes(id) || buttonInputs[id] !== 'pressed' || buttonInputOwners.get(id) !== event.code) return;
  event.preventDefault();
  releaseButton(id,session(),false,event.code).catch(() => {});
});

// Narrow bridge: retain the proven observer, PCM, playback ownership and FIFO.
// The device surface owns presentation and gestures; the backend owns behavior.
window.MishkaConsole = Object.freeze({
  snapshot: () => current,
  acceptSnapshot: simulatorSnapshotIsCurrent,
  post, render, unlockAudio, startMic, clearMic, stopMic, interactionGuide, updateGuide, interactionActive, sessionActionIsCurrent,
  cleanup: ({preserveReconnect = false} = {}) => { if (!preserveReconnect) audioReconnectPending = false; suppressPhoneAudio = true; clearMic(); interruptAudioForInput(); },
  media: () => ({ microphone: Boolean(mic), microphoneAccepting: microphoneAccepting(), pending: micStarting, audioReady, audioRetry: Boolean(audioRetry),
    connected: ws?.readyState === WebSocket.OPEN, authBlocked,
    inputReady: wsSnapshotReady && !wsReloadRequired && !authBlocked, reloadRequired: wsReloadRequired,
    playing: activeSources.size > 0, pendingPCM: pendingPCM.length,
    queuedPosts: postQueue.length, activePost: Boolean(activePost) }),
});
