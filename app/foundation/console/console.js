const $ = id => document.getElementById(id);
const BUTTON_IDS = [1, 2, 3, 4, 5, 6, 7];
const LIGHTING_ELEMENT_IDS = [10, 11, 20, 21, 30, 31, 40, 41, 50, 51, 60, 61, 62, 70, 71, 72];
const ELEMENT_PRESENTATION = {
  10: { kind: 'fill', buttonId: 1, color: 'red' }, 11: { kind: 'digit', buttonId: 1, color: 'white' },
  20: { kind: 'fill', buttonId: 2, color: 'orange' }, 21: { kind: 'digit', buttonId: 2, color: 'white' },
  30: { kind: 'fill', buttonId: 3, color: 'yellow' }, 31: { kind: 'digit', buttonId: 3, color: 'white' },
  40: { kind: 'fill', buttonId: 4, color: 'green' }, 41: { kind: 'digit', buttonId: 4, color: 'white' },
  50: { kind: 'fill', buttonId: 5, color: 'cyan' }, 51: { kind: 'digit', buttonId: 5, color: 'white' },
  60: { kind: 'fill', buttonId: 6, color: 'blue' }, 61: { kind: 'digit', buttonId: 6, color: 'white' },
  62: { kind: 'symbol', buttonId: 6, symbol: 'bear', color: 'brown' },
  70: { kind: 'fill', buttonId: 7, color: 'purple' }, 71: { kind: 'digit', buttonId: 7, color: 'white' },
  72: { kind: 'symbol', buttonId: 7, symbol: 'weather', color: 'skyblue' },
};
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
let mic = null;
let ws = null;
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
let audioQueueTail = Promise.resolve();
const pendingPCM = [];
const pendingTextInputs = [];
const buttonInputs = Object.fromEntries(BUTTON_IDS.map(id => [id, 'released']));
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

let postQueue = Promise.resolve();

function post(path, body = {}) {
  const run = async () => {
    recordLiveInput(path, body);
    try {
      const response = await fetchWithTimeout(path, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ ...body, session_id: session() })
      });
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
      recordLiveResult(value);
      return value;
    } catch (error) {
      showError(error);
      throw error;
    }
  };
  const next = postQueue.then(run, run);
  postQueue = next.catch(() => {});
  return next;
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
  socket.onopen = () => {
    if (ws !== socket) return;
    reconnectAttempt = 0;
    reconnectDelayOverride = 0;
    liveClientEntries = [];
    $("connection").textContent = "CONNECTED";
    $("connection").classList.remove("offline");
    $("connection").classList.add("online");
    startWebsocketHeartbeat(socket);
    renderRuntimeStatus();
    refresh({ includeStatus: true, includeState: false }).finally(schedulePolling);
  };
  socket.onclose = () => {
    if (ws !== socket) return;
    stopWebsocketHeartbeat();
    ws = null;
    clearMic();
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
    try {
      const message = parseMessage(event.data);
      if (!message) { showError("WebSocket sent invalid JSON"); return; }
      if (message.type === "admission_error") {
        const error = message.error || {};
        if (["auth_failure", "unauthorized", "forbidden"].includes(String(error.code || "").toLowerCase())) {
          authBlocked = true;
          stopPolling();
        }
        reconnectDelayOverride = Math.min(60000, Math.max(5000, Number(error.retry_after_ms) || 30000));
        showError(`Сервис временно недоступен: ${error.reason || "runtime_unavailable"}. Повтор через несколько секунд.`);
        return;
      }
      if (message.type === "stand_state") {
        const state = normalizeState(message.state);
        if (!state) { showError("WebSocket sent invalid stand state"); return; }
        try {
          if (message.lighting_wave) state.lighting_wave = message.lighting_wave;
          render(state);
        } catch (error) {
          console.warn('Invalid stand snapshot', error);
          showError('Invalid stand snapshot: ' + (error.message || error));
        }
        if (mic && message.audio_completion) {
          const failed = message.audio_completion.reason === "audio_retry";
          $("mic-state").textContent = failed ? "Речь не распознана · попробуйте ещё раз" : "Ответ получен · микрофон слушает";
        }
      }
      if (message.type === "audio_pcm_output") {
        if (typeof message.data_b64 !== "string" || !Number.isFinite(Number(message.sample_rate))) { showError("WebSocket sent invalid audio frame"); return; }
        enqueuePCM(message);
      }
      if (message.type === "audio_stop" && audioContext) {
        audioGeneration += 1; storySpeechGeneration += 1; pendingPCM.length = 0;
        for (const source of activeSources) { try { source.stop(); } catch (_) {} }
        activeSources.clear(); nextAudioTime = audioContext.currentTime;
      }
    } catch (error) {
      console.warn('WebSocket message handling failed', error);
      showError('WebSocket message failed: ' + (error.message || error));
    }
  };
}

async function fetchWithTimeout(path, options = {}, timeoutMs = FETCH_TIMEOUT_MS) {
  const { allowStatuses = [], ...requestOptions } = options;
  const controller = new AbortController();
  const parentSignal = requestOptions.signal;
  const abortFromParent = () => controller.abort();
  if (parentSignal) {
    if (parentSignal.aborted) controller.abort();
    else parentSignal.addEventListener('abort', abortFromParent, { once: true });
  }
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(apiPath(path), { cache: 'no-store', ...requestOptions, signal: controller.signal });
    if (!response.ok && !allowStatuses.includes(response.status)) {
      const error = new Error(`HTTP ${response.status}`);
      error.status = response.status;
      throw error;
    }
    return response;
  } finally {
    window.clearTimeout(timeout);
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
          const state = await (await fetchWithTimeout('/stand/state', { signal: controller.signal })).json();
          render(state);
          stateSnapshotAvailable = true;
        } catch (error) {
          if ([401, 403].includes(Number(error?.status))) {
            authBlocked = true;
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

function render(state) {
  state = normalizeState(state);
  if (!state) { showError("Invalid stand state"); return; }
  current = state;
  if (!firstSnapshotRendered) {
    lastSpokenCommand = Math.max(0, ...(state.commands || []).map(item => Number(item.sequence) || 0));
    firstSnapshotRendered = true;
  }
  const contract = validateButtonContract(state.button_contract);
  renderButtonLegend(contract);
  const pipeline = state.pipeline_metrics?.pipeline || {};
  const fallbackEvent = (state.pipeline_metrics?.events || []).some(item => item?.event === 'rvc_v2_fallback');
  const conversion = String(pipeline.voice_conversion || 'rvc_v2').toLowerCase();
  const mode = conversion === 'off' || conversion === 'disabled'
    ? 'silero+off'
    : fallbackEvent ? 'passthrough' : 'silero+rvc';
  const modeLabel = { 'silero+rvc': 'SILERO + RVC', 'silero+off': 'SILERO + OFF', passthrough: 'PASSTHROUGH' }[mode] || mode.toUpperCase();
  $('pipeline-mode').textContent = modeLabel;
  $('pipeline-mode').dataset.mode = mode;
  const sessionState = String(state.session?.state || '').trim().toUpperCase();
  if (mic && ['NONE', 'IDLE', 'ENDED'].includes(sessionState)) clearMic();
  const handsetUp = Boolean(state.session.handset_up);
  $('session-mode-label').textContent = handsetUp ? 'SESSION ACTIVE' : 'STANDBY';
  $('state').textContent = state.session.state;
  $('node').textContent = (state.session.scenario || '—') + ' / ' + (state.session.state === 'ENDED' ? 'session_closed' : (state.session.node || '—'));
  const last = state.latencies.at(-1);
  $('latency').textContent = last ? `${last.operation}: ${last.ms} ms` : '—';
  $('pipeline').textContent = JSON.stringify(state.pipeline_metrics, null, 2);
  updateGuide(state, contract);
  const standby = ['NONE', 'IDLE', 'STANDBY'].includes(state.session.state);
  const runtimeLabel = standby ? 'Ожидание действия' : handsetUp ? 'Трубка поднята' : 'Трубка опущена';
  if ($('runtime-state')) $('runtime-state').textContent = runtimeLabel;
  document.getElementById('handset-state').textContent = handsetUp ? 'HANDSET UP' : sessionState === 'ENDED' ? 'HANDSET DOWN / ENDED' : 'HANDSET DOWN'; document.getElementById('device-handset').textContent = handsetUp ? 'UP' : 'DOWN';
  const deviceState = standby ? 'STANDBY' : handsetUp ? 'UP' : 'DOWN';
  document.getElementById('device-handset').textContent = deviceState;
  const deviceMode = document.getElementById('device-mode');
  if (deviceMode) deviceMode.textContent = state.transport?.hardware?.acceptance === 'physical' ? 'PHYSICAL' : 'SIMULATOR';
  const displayNode = state.session.state === 'ENDED' ? 'session_closed' : (state.session.node || state.session.state);
  $('phone-display').textContent = displayNode;
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
    ? '16 ELEMENTS / ' + Object.values(lighting).filter(Boolean).length + ' ON'
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
    const reasonLooksFailed = /(?:^|_)(?:error|failed|blocked|fallback|unavailable|retry)(?:$|_)/i.test(reason);
    const outputLooksFailed = outputs.some(output => {
      const action = String(output?.action || output?.type || '');
      const resolution = String(output?.resolution || '');
      return /(?:blocked|failed|fallback|unavailable|retry)/i.test(`${action} ${resolution}`)
        || ['safe_stop', 'transition_rejected', 'audio_retry', 's2s_fallback', 'content_blocked', 'privacy_blocked'].includes(action);
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
    const enabled = Boolean(session.visual_only) ? guestButtons.has(id) : (guestMode ? guestButtons.has(id) : (sessionActive && (enabledButtons === null || enabledButtons.has(id))));
    const button = buttonRefs.get(id);
    if (!button) continue;
    const fillId = id * 10;
    const digitId = fillId + 1;
    const symbolId = id === 6 ? 62 : id === 7 ? 72 : null;
    const fillOn = lighting[fillId] === 1;
    const digitOn = lighting[digitId] === 1;
    const symbolOn = symbolId !== null && lighting[symbolId] === 1;
    const fillPresentation = Object.values(ELEMENT_PRESENTATION).find(item => item.buttonId === id && item.kind === 'fill');
    const colorDef = LED_COLORS[fillPresentation?.color] || LED_COLORS.invalid;
    const buttonLabel = contract.valid ? contract.labels[id] : colorDef.label;
    for (const led of Object.values(LED_COLORS)) button.classList.remove(led.className);
    button.classList.remove('led-target', 'rfid-wave');
    button.style.removeProperty('--led-color');
    button.classList.add(colorDef.className);
    button.classList.toggle('led-on', fillOn);
    button.classList.toggle('led-off', !fillOn);
    button.classList.toggle('digit-on', digitOn);
    button.classList.toggle('symbol-on', symbolOn);
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
      ledCell.textContent = String(fillId) + ':' + (fillOn ? 1 : 0) + ' · ' + String(digitId) + ':' + (digitOn ? 1 : 0) + (symbolId ? ' · ' + symbolId + ':' + (symbolOn ? 1 : 0) : '');
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
      : 'WAVE IDLE';
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
  const playing = node === 'fixed_story_playing';
  const paused = node === 'story_paused';
  const mode = playing ? 'playing' : paused ? 'paused' : node === 'fixed_story_menu' ? 'menu' : 'inactive';
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
    post('/stand/audio/lifecycle', { response_id: command.payload.response_id, status: 'finished' }).catch(() => {});
    const canonicalCompletion = command.payload.response_id === 'PHRASE_001' ||
      ['PHRASE_007', 'PHRASE_008', 'PHRASE_016'].includes(command.payload.response_id) ||
      String(command.payload.response_id || '').startsWith('PHRASE_');
    if (generation === storySpeechGeneration && canonicalCompletion) {
      post('/stand/audio/completed', { session_id: command.payload.session_id, response_id: command.payload.response_id, playback_lease: command.payload.playback_lease, generation: command.payload.generation }).catch(() => {});
    }
  };
  source.start();
  post('/stand/audio/lifecycle', { response_id: command.payload.response_id, status: 'playing' }).catch(() => {});
  $('audio-state').textContent = 'AUDIO: PHRASE / WAV';
}

async function playStoryAsset(command, generation) {
  const response = await fetch(apiPath(`/stand/audio/story/${encodeURIComponent(command.payload.response_id)}`));
  if (!response.ok) throw new Error(`story audio HTTP ${response.status}`);
  const data = await response.arrayBuffer();
  const context = await unlockAudio();
  if (generation !== storySpeechGeneration || current?.session.node !== 'fixed_story_playing') return;
  const buffer = await context.decodeAudioData(data);
  if (generation !== storySpeechGeneration || current?.session.node !== 'fixed_story_playing') return;
  const source = context.createBufferSource();
  source.buffer = buffer;
  source.connect(context.destination);
  activeSources.add(source);
  source.onended = () => {
    activeSources.delete(source);
    if (generation === storySpeechGeneration) {
      $("audio-state").textContent = audioReady ? "AUDIO: READY" : "AUDIO: LOCKED";
      // Silero/WAV playback has the same completion contract as the browser
      // voice fallback: advance the story exactly once after the asset ends.
      post('/stand/audio/completed', { session_id: command.payload.session_id, response_id: command.payload.response_id, playback_lease: command.payload.playback_lease, generation: command.payload.generation }).catch(() => {});
    }
  };
  source.start();
  post('/stand/audio/lifecycle', { response_id: command.payload.response_id, status: 'playing' }).catch(() => {});
  $('audio-state').textContent = 'AUDIO: STORY / SILERO';
}

function speakCommands(state) {
  for (const command of state.commands) {
    if (command.sequence <= lastSpokenCommand) continue;
    // Keep audio commands pending until a real user gesture unlocks the
    // browser. Autoplay policy otherwise makes the first response disappear.
    if ((command.type === 'audio_play' || command.type === 'audio_stop') && !audioReady) break;
    lastSpokenCommand = command.sequence;
    if (command.type === 'audio_stop') {
      storySpeechGeneration += 1;
      window.speechSynthesis?.cancel();
    }
    if (command.type === 'audio_play' && command.payload.content_kind === 'phrase' && command.payload.asset_path) {
      const generation = ++storySpeechGeneration;
      playPhraseAsset(command, generation).catch(error => {
        if (generation !== storySpeechGeneration) return;
        post('/stand/audio/failed', { session_id: command.payload.session_id, response_id: command.payload.response_id, playback_lease: command.payload.playback_lease, generation: command.payload.generation, error_code: 'browser_phrase_playback_failed' }).catch(() => {});
        showError(new Error(`Recorded phrase unavailable; audio path stopped: ${error.message}`));
      });
      continue;
    }
    if (command.type === 'audio_play' && command.payload.content_kind === 'story' && command.payload.asset_path) {
      const generation = ++storySpeechGeneration;
      playStoryAsset(command, generation).catch(error => {
        if (generation !== storySpeechGeneration || current?.session.node !== 'fixed_story_playing') return;
        post('/stand/audio/failed', { session_id: command.payload.session_id, response_id: command.payload.response_id, playback_lease: command.payload.playback_lease, generation: command.payload.generation, error_code: 'browser_story_playback_failed' }).catch(() => {});
        showError(new Error('Silero story unavailable; audio path stopped: ' + error.message));
      });
      continue;
    }
  }
  // Bounded S2S text is rendered as PCM by BrowserPlaybackController. Do not
  // speak it again through speechSynthesis, otherwise every answer is doubled.
  lastResultCount = state.results.length;
}

function updateGuide(state, contract) {
    const labels = {
    game_menu: ['1 · Игры', 'Выберите игру кнопкой.'],
    game_active: ['1 · Игра', 'Следуйте текущей подсветке.'],
    fixed_story_menu: ['2 · Сказки', 'Выберите одну из пяти разрешённых сказок кнопками 1–5.'],
    fixed_story_playing: ['2 · Сказка', 'Идёт утверждённая запись. Трубка вниз — пауза.'],
    support_intake: ['3 · Я слушаю', 'Расскажите, спросите или сообщите, что случилось.'],
    personal_story_theme: ['4 · Новая сказка', 'Выберите безопасную тему.'],
    wellbeing_check: ['5 · Помощь', '1 — нет, 7 — да.'],
    bear_dialogue: ['6 · Ограниченный разговор', 'Короткий разговор по безопасным темам.'],
    schedule_weather: ['7 · День и планы', 'Дата, погода и расписание из typed-провайдеров.'],
    safety_handoff: ['Помощь взрослого', 'Ожидается подтверждение безопасного контура.'],
    root_menu: ['Главное меню', 'Выберите действие кнопкой 1–7.']
  };
  const node = String(state.session.node || '');
  const guideMap = document.querySelector('.guide-map');
  if (guideMap) guideMap.hidden = node === 'bear_dialogue';
  const map = {
    root_menu: ['Главное меню', buttonLegendSummary(contract)],
    game_menu: ['Игры', 'Выберите игру кнопкой 1–7.'],
    fixed_story_menu: ['Сказки', 'Выберите сказку кнопками 1–5.'],
    bear_dialogue: ['Ограниченный разговор', 'Короткий разговор по безопасным темам.'],
    safety_handoff: ['Помощь взрослого', 'Ожидается подтверждение безопасного контура.'],
    game_active: ['Игра', 'Нажимайте кнопки согласно подсветке.'],
    session_closed: ['Сессия завершена', 'Нажмите Reset, чтобы начать следующий тест.']
  };
    const normalizedState = String(state.session.state || '').trim().toUpperCase();
  const content = normalizedState === 'ENDED' ? map.session_closed : (node === 'schedule_weather' ? ['7 · Расписание и погода', 'Показываются день, планы и доступная погода.'] : node === 'game_active' ? ['5 · Повтори паттерн', 'Повторяйте кнопки согласно подсветке.'] : (labels[node] || map[node] || (['NONE', 'IDLE', 'STANDBY'].includes(normalizedState) ? ['Готов к знакомству', 'Поднимите трубку или подайте действие через симулятор.'] : ['Тест продолжается', 'Следуйте текущему состоянию.'])));
  $('guide-title').textContent = content[0];
  $('guide-text').textContent = content[1];
}

function enqueuePCM(message, generation = audioGeneration) {
  audioQueueTail = audioQueueTail.catch(() => {}).then(() => playPCM(message, generation));
  return audioQueueTail;
}

async function playPCM(message, generation = audioGeneration) {
  const context = await unlockAudio();
  if (generation !== audioGeneration) return;
  if (context.state !== 'running') {
    if (pendingPCM.length >= 32) pendingPCM.shift();
    pendingPCM.push({message, generation});
    $('audio-state').textContent = 'AUDIO: LOCKED';
    return;
  }
  if (generation !== audioGeneration) return;
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
  const source = audioContext.createBufferSource();
  source.buffer = buffer;
  source.connect(audioContext.destination);
  activeSources.add(source);
  source.onended = () => {
  activeSources.delete(source);
  if (!activeSources.size) $('audio-state').textContent = audioReady ? 'AUDIO: READY' : 'AUDIO: LOCKED';
};
  nextAudioTime = Math.max(context.currentTime, nextAudioTime);
  source.start(nextAudioTime);
  nextAudioTime += buffer.duration;
  $('audio-state').textContent = 'AUDIO: PLAYING';
}

function initDiagnostics() {
  const root = $('button-status');
  for (const id of BUTTON_IDS) {
    const row = document.createElement('tr');
    row.innerHTML = `<td>${id}</td><td id="button-input-${id}">RELEASED</td><td id="button-led-${id}" class="diag-color off">OFF</td><td id="button-enabled-${id}">NO</td>`;
    root.append(row);
  }
}

function releaseButton(id) {
  buttonInputs[id] = 'released';
  const button = buttonRefs.get(id);
  if (button) button.classList.remove('pressed');
  return post('/stand/button-released', { button_id: Number(id), state: 'released' });
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
      down = performance.now();
      buttonInputs[id] = 'pressed';
      button.classList.add('pressed');
      button.setPointerCapture(event.pointerId);
      try { await post('/stand/button', { button_id: Number(id), state: 'pressed' }); } catch (_) { /* shown in the error panel */ }
    };
    button.onpointerup = async event => {
      event.preventDefault();
      button.classList.remove('pressed');
      const duration = Math.round(performance.now() - down);
      try {
        if (duration >= 5000) await post('/stand/button-long-hold', { button_id: Number(id), duration_ms: duration });
        else if (duration >= 600) await post('/stand/button-hold', { button_id: Number(id), duration_ms: duration });
        await releaseButton(id);
      } catch (_) { /* shown in the error panel */ }
    };
    button.onpointercancel = () => releaseButton(id).catch(() => {});
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
function interruptAudioForInput() {
  micInputGain = 1;
  audioGeneration += 1;
  storySpeechGeneration += 1;
  pendingPCM.length = 0;
  for (const source of activeSources) {
    try { source.stop(); } catch (_) { /* already ended */ }
  }
  activeSources.clear();
  nextAudioTime = audioContext ? audioContext.currentTime : 0;
  audioQueueTail = Promise.resolve();
  $('audio-state').textContent = audioReady ? 'AUDIO: READY' : 'AUDIO: LOCKED';
}

$('text-form').onsubmit = event => {
  event.preventDefault();
  const text = $('text').value.trim();
  if (!text) return;
  interruptAudioForInput();
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
    flush
  };
}
async function startMic() {
  const micCapableNodes = new Set(['bear_dialogue', 'support_intake', 'voice_welcome', 'exhibition_name_capture', 'personal_story_theme', 'wellbeing_check', 'schedule_weather']);
  if (!current || !micCapableNodes.has(String(current.session?.node || ''))) {
    showError(new Error('Голосовой ввод доступен на активном голосовом шаге; для имени сначала поднимите трубку и дождитесь вопроса «Как тебя зовут?»'));
    return;
  }
  const context = await unlockAudio();
  const stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
  const source = context.createMediaStreamSource(stream);
  const sessionId = session();
  const resampler = createMicResampler(context.sampleRate, samples => {
    if (ws?.readyState !== WebSocket.OPEN) return;
    ws.send(JSON.stringify({ type: 'audio_pcm', session_id: sessionId, sample_rate: 16000, data_b64: bytesToB64(samples.buffer) }));
  });
  const node = context.createScriptProcessor(1024, 1, 1);
  node.onaudioprocess = event => {
    const input = event.inputBuffer.getChannelData(0);
    const playbackActive = activeSources.size > 0;
    const targetGain = playbackActive ? MIC_DUCK_GAIN : 1;
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
  $('mic-state').textContent = `${context.sampleRate} Hz → 16 kHz UART PCM → VAD`;
}
function clearMic(flush = false) {
  if (!mic) return;
  if (flush && ws?.readyState === WebSocket.OPEN) mic.resampler.flush();
  mic.node.disconnect(); mic.source.disconnect(); mic.mute?.disconnect(); mic.stream.getTracks().forEach(track => track.stop()); mic = null;
  $('mic').textContent = 'Start microphone'; $('mic-state').textContent = 'idle';
}
$('mic').onclick = async () => {
  try {
    if (!mic) { interruptAudioForInput(); return await startMic(); }
    const sessionId = mic.sessionId;
    clearMic(true);
    if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'flush_audio', session_id: sessionId }));
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
});

window.addEventListener('keydown', event => {
  if (event.repeat || ['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement?.tagName)) return;
  const id = Number(event.key);
  if (BUTTON_IDS.includes(id)) {
    event.preventDefault();
    const button = buttonRefs.get(id);
    if (button && !button.disabled) {
      buttonInputs[id] = 'pressed';
      button.classList.add('pressed');
      post('/stand/button', { button_id: id, state: 'pressed' }).catch(() => {});
    }
  } else if (event.code === 'Space') {
    event.preventDefault();
    const up = current?.session?.handset_up;
    post('/stand/handset', { state: up ? 'down' : 'up' }).catch(() => {});
  }
});
window.addEventListener('keyup', event => {
  const id = Number(event.key);
  if (!BUTTON_IDS.includes(id) || ['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement?.tagName)) return;
  event.preventDefault();
  releaseButton(id).catch(() => {});
});
