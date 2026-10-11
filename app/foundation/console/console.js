const $ = id => document.getElementById(id);
const BUTTON_IDS = [1, 2, 3, 4, 5, 6, 7];
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
let reconnectAttempt = 0;
let reconnectTimer = null;
let audioContext = null;
let audioReady = false;
let nextAudioTime = 0;
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

const session = () => $('session').value.trim() || 'stand';

async function unlockAudio() {
  audioContext ||= new AudioContext();
  if (audioContext.state === 'suspended') await audioContext.resume().catch(() => {});
  if (window.speechSynthesis) speechSynthesis.resume();
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
  const token = new URLSearchParams(window.location.search).get('token');
  if (!token || !path.startsWith('/')) return path;
  const separator = path.includes('?') ? '&' : '?';
  return `${path}${separator}token=${encodeURIComponent(token)}`;
}

let postQueue = Promise.resolve();

function post(path, body = {}) {
  const run = async () => {
    recordLiveInput(path, body);
    try {
      const response = await fetch(apiPath(path), {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ ...body, session_id: session() })
      });
      const raw = await response.text();
      let value;
      try { value = raw ? JSON.parse(raw) : {}; } catch (parseError) {
        if (current?.session?.state === 'ENDED' || current?.session?.state === 'ENDING') return null;
        throw new Error(response.ok ? 'Invalid JSON response: ' + raw.slice(0, 160) : 'HTTP ' + response.status + ': ' + raw.slice(0, 160));
      }
      if (!response.ok) {
        if (current?.session?.state === 'ENDED' || current?.session?.state === 'ENDING') return null;
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
  const labels = { fixture_game_repeat_pattern: ['5 · Повтори паттерн', 'Повторите световой и кнопочный паттерн на семи кнопках.'], fixture_schedule_weather: ['7 · Расписание и погода', 'Показываются расписание ребёнка по RFID и актуальная погода.'], fixture_game_one: ['2 · Игра: повтори паттерн', 'Нажимайте кнопки в ответ на световой паттерн.'], fixture_game_two: ['2 · Игра: повтори паттерн', 'Нажимайте кнопки в ответ на световой паттерн.'],
    '/stand/rfid': ['RFID', `Метка ${body.tag_id || '—'}`, 'DEVICE INPUT'],
    '/stand/handset': ['HANDSET', body.state === 'up' ? 'Трубка поднята' : 'Трубка положена', 'DEVICE INPUT'],
    '/stand/button': ['BUTTON', `Кнопка ${body.button_id} · нажата`, 'DEVICE INPUT'],
    '/stand/button-hold': ['BUTTON', `Кнопка ${body.button_id} · удержание ${body.duration_ms || 0} ms`, 'DEVICE INPUT'],
    '/stand/button-long-hold': ['BUTTON', `Кнопка ${body.button_id} · длинное удержание`, 'DEVICE INPUT'],
    '/stand/button-released': ['BUTTON', `Кнопка ${body.button_id} · отпущена`, 'DEVICE INPUT'],
    '/stand/text': ['USER', body.text || 'Текстовый ввод', 'TEXT INPUT'],
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
    const action = String(output.action || '');
    if (action === 's2s_text' || action === 's2s_response' || action === 'llm_text') return [{ kind: 'assistant', title: 'МИШКА / LLM', text: String(output.text || output.resolved_text || '—'), meta: `STREAM · ${result.reason || 'response'}`, at: item.at }];
    if (action === 'text' && output.text) return [{ kind: 'assistant', title: 'МИШКА / LLM', text: String(output.text), meta: `RESPONSE · ${result.reason || 'response'}`, at: item.at }];
    if (action === 's2s_fallback') return [{ kind: 'fallback', title: 'FALLBACK', text: `Безопасный маршрут: ${output.reason || result.reason || 'unknown'}`, meta: 'SAFE ROUTE', at: item.at }];
    if (action === 'audio_retry') return [{ kind: 'audio', title: 'AUDIO', text: 'Не удалось распознать речь · попробуйте ещё раз', meta: `RETRY · ${output.reason || 'recognition'}`, at: item.at }];
    if (action === 'audio_ack') return [{ kind: 'audio', title: 'AUDIO ACK', text: 'Секундочку · быстрый ответ', meta: 'LATENCY ACK', at: item.at }];
    if (action === 'audio') return [{ kind: 'audio', title: 'AUDIO', text: `Реплика ${output.response_id || output.asset_id || '—'}`, meta: 'ALLOWLIST ASSET', at: item.at }];
    if (action === 'game_start' || action === 'game_started') return [{ kind: 'action', title: 'GAME', text: `Игра запущена · ${output.game_id || 'fixture'}`, meta: 'RUNTIME ACTION', at: item.at }];
    if (action === 'story_start' || action === 'story_selected') return [{ kind: 'action', title: 'STORY', text: `Сказка · ${output.story_id || output.asset_id || 'fixture'}`, meta: 'RUNTIME ACTION', at: item.at }];
    if (action === 'led_pattern') return [{ kind: 'action', title: 'LED', text: `${output.pattern || 'pattern'} · ${output.pattern === 'rfid_wave' ? 'rainbow' : (output.color || 'mixed')}`, meta: 'DEVICE COMMAND', at: item.at }];
    if (action === 'session_ended') return [{ kind: 'system', title: 'SESSION', text: `Сессия завершена · ${output.reason || 'normal'}`, meta: 'LIFECYCLE', at: item.at }];
    return action ? [{ kind: 'action', title: 'ACTION', text: action.replaceAll('_', ' '), meta: 'RUNTIME', at: item.at }] : [];
  });
  const directText = result.response_text || result.answer || result.text;
  if (!entries.length && directText) entries.push({ kind: 'assistant', title: 'МИШКА / LLM', text: String(directText), meta: `RESPONSE · ${result.reason || 'response'}`, at: item.at });
  return entries;
}

function eventLiveEntry(event) {
  const payload = event.payload || {};
  const type = String(event.type || '');
  if (type === 'SpeechReceived') {
    const now = Date.now();
    const index = pendingTextInputs.findIndex(item => item.length === Number(payload.length || 0) && now - item.at < 20000);
    const hasRecentText = liveClientEntries.some(entry => entry.kind === 'user' && entry.meta === 'TEXT INPUT' && String(entry.text || '').length === Number(payload.length || 0) && now - entry.at < 20000);
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
    const labels = { fixture_game_repeat_pattern: ['5 · Повтори паттерн', 'Повторите световой и кнопочный паттерн на семи кнопках.'], fixture_schedule_weather: ['7 · Расписание и погода', 'Показываются расписание ребёнка по RFID и актуальная погода.'], fixture_game_one: ['2 · Игра: повтори паттерн', 'Нажимайте кнопки в ответ на световой паттерн.'], fixture_game_two: ['2 · Игра: повтори паттерн', 'Нажимайте кнопки в ответ на световой паттерн.'], speech_end: ['INPUT', 'End of speech detected', 'VAD'], audio_ack_requested: ['AUDIO ACK', 'Immediate acknowledgement requested', 'LATENCY'], token_received: ['TOKEN', 'LLM token received', 'STREAM'], chunk_closed: ['CHUNK', 'Text chunk closed', 'PARSER'], tts_started: ['TTS', 'Silero synthesis started', 'PIPELINE'], audio_ready: ['AUDIO', 'PCM chunk ready', 'PIPELINE'], playback_started: ['PLAYBACK', 'Playback started', 'AUDIO OUT'], audio_cancelled: ['BARGE-IN', 'Playback/queue cancelled', 'CANCELLED'], rvc_v2_fallback: ['RVC', 'RVC v2 unavailable · direct fallback', 'FALLBACK'] }[event];
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
  const displayNode = state.session.state === 'ENDED' ? 'fixture_end' : (state.session.node || '—');
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

function scheduleReconnect() {
  if (reconnectTimer !== null) return;
  const exponential = Math.min(30000, 1200 * (2 ** Math.min(reconnectAttempt, 5)));
  const jitter = exponential * (0.8 + Math.random() * 0.4);
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
  return { ...value, session: { state: "UNKNOWN", anonymous: null, scenario: "", node: "", ...value.session },
    results: Array.isArray(value.results) ? value.results : [], events: Array.isArray(value.events) ? value.events : [],
    commands: Array.isArray(value.commands) ? value.commands : [], latencies: Array.isArray(value.latencies) ? value.latencies : [],
    errors: Array.isArray(value.errors) ? value.errors : [] };
}

function connect() {
  if (ws && [WebSocket.OPEN, WebSocket.CONNECTING].includes(ws.readyState)) return;
  const token = new URLSearchParams(location.search).get("token");
  if (token) document.cookie = `stand_token=${encodeURIComponent(token)}; Path=/; SameSite=Lax`;
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${scheme}://${location.host}${apiPath("/stand/ws")}`);
  ws.onopen = () => {
    reconnectAttempt = 0;
    $("connection").textContent = "CONNECTED";
    $("connection").classList.remove("offline");
    $("connection").classList.add("online");
  };
  ws.onclose = () => {
    ws = null; clearMic();
    $("connection").textContent = "OFFLINE";
    $("connection").classList.add("offline");
    scheduleReconnect();
  };
  ws.onmessage = event => {
    const message = parseMessage(event.data);
    if (!message) { showError("WebSocket sent invalid JSON"); return; }
    if (message.type === "stand_state") {
      const state = normalizeState(message.state);
      if (!state) { showError("WebSocket sent invalid stand state"); return; }
      render(state);
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
  };
}

async function refresh() {
  const state = await (await fetch(apiPath('/stand/state'))).json();
  render(state);
  const health = await (await fetch(apiPath('/health'))).json();
  const readiness = await (await fetch(apiPath('/ready'))).json();
  const resources = readiness.dependencies?.s2s?.resources;
  $('health').textContent = resources?.available_vram_gb === null && resources?.minimum_vram_gb ? 'GPU UNAVAILABLE' : `HEALTH ${health.status === 'ok' ? 'OK' : 'ERROR'}`;
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

function triggerRfidWave(led) {
  const buttons = Array.from(document.querySelectorAll('.physical-button'));
  buttons.forEach((button, index) => {
    const color = normalizeColor(led?.colors?.[String(index + 1)]);
    if (!color) return;
    button.classList.remove('rfid-wave');
    button.style.setProperty('--led-color', LED_COLORS[color].css);
    void button.offsetWidth;
    window.setTimeout(() => button.classList.add('rfid-wave'), index * 110);
    window.setTimeout(() => { button.classList.remove('rfid-wave'); button.style.removeProperty('--led-color'); }, index * 110 + 900);
  });
}

let previousSessionState = null;
function render(state) {
  state = normalizeState(state);
  if (!state) { showError("Invalid stand state"); return; }
  current = state;
  const contract = validateButtonContract(state.button_contract);
  renderButtonLegend(contract);
  $('pipeline-profile').textContent = state.pipeline_metrics?.pipeline?.label || 'runtime';
  const sessionState = String(state.session?.state || '').trim().toUpperCase();
  if (mic && ['NONE', 'IDLE', 'ENDING', 'ENDED'].includes(sessionState)) clearMic();
  $('profile-label').textContent = state.session.anonymous === null ? 'NO PROFILE' : state.session.scenario === 'dev_core' ? 'DEV MODE' : state.session.anonymous ? 'ANONYMOUS' : 'RFID PROFILE';
  $('state').textContent = state.session.state;
  $('node').textContent = (state.session.scenario || '—') + ' / ' + (state.session.state === 'ENDED' ? 'fixture_end' : (state.session.node || '—'));
  const last = state.latencies.at(-1);
  $('latency').textContent = last ? `${last.operation}: ${last.ms} ms` : '—';
  $('pipeline').textContent = JSON.stringify(state.pipeline_metrics, null, 2);
  updateGuide(state, contract);
  if (previousSessionState !== 'AUTHENTICATED' && sessionState === 'AUTHENTICATED') triggerRfidWave(state.led);
  previousSessionState = sessionState; const handsetUp = Boolean(state.session.handset_up);
  const waitingRFID = ['NONE', 'IDLE', 'WAITING_RFID'].includes(state.session.state);
  $('rfid-state').textContent = waitingRFID ? 'RFID: WAITING' : state.session.anonymous ? 'RFID: ANONYMOUS' : 'RFID: AUTHENTICATED';
  document.getElementById('handset-state').textContent = handsetUp ? 'HANDSET UP' : sessionState === 'ENDING' ? 'HANDSET DOWN / ENDED' : 'HANDSET DOWN'; document.getElementById('device-handset').textContent = handsetUp ? 'UP' : 'DOWN';
  const activeRfid = state.rfid_tag || [...(state.events || [])].reverse().find(item => item.type === 'RfidPresented')?.payload?.tag_id;
  document.getElementById('device-rfid').textContent = waitingRFID ? '—' : (activeRfid || (state.session.anonymous ? 'ANONYMOUS' : '—'));
  const displayNode = state.session.state === 'ENDED' ? 'fixture_end' : (state.session.node || state.session.state);
  $('phone-display').textContent = displayNode;
  const latestAudioCommand = state.commands.filter(item => item.type === 'audio_play' || item.type === 'audio_stop').sort((a, b) => b.sequence - a.sequence)[0];
  document.getElementById('audio-state').textContent = latestAudioCommand?.type === 'audio_play'
    ? 'AUDIO: PLAYING' : audioReady ? 'AUDIO: READY' : 'AUDIO: LOCKED';
  $('device-display').textContent = state.session.state === 'ENDED' ? 'fixture_end' : (state.session.node || '—');
  $('device-uart').textContent = state.transport?.hardware?.connected === false ? 'DISCONNECTED' : 'JSONL / CRC32';
  renderLog('frames', state.raw_frames, item => `${item.direction} #${item.decoded?.sequence ?? '?'}`, item => item.raw, item => item.error ? 'error' : item.direction.startsWith('host') ? 'host' : 'device');
  renderLog('events', state.events, item => `${item.type} #${item.sequence}`, item => JSON.stringify(item.payload), () => '');
  renderLog('results', [...state.results, ...state.errors], item => item.input || item.stage || item.error, item => JSON.stringify(item.result || item, null, 2), item => item.error ? 'error' : '');
  const led = deriveLEDState(state.led, contract);
  $('device-led').textContent = `${led.color.toUpperCase()} / ${led.pattern}`;
  renderButtons(state, led, contract);
  renderLiveDialogue(state);
  speakCommands(state);
  // ``barge_in: false`` is part of every normal audio response.  Searching
  // the whole JSON for the word "barge_in" made the red error notice appear
  // while audio was merely being collected.  Match explicit failure reasons
  // and fallback actions instead.
  const fallback = [...state.results].reverse().find(item => {
    const result = item.result || {};
    const reason = String(result.reason || '');
    const outputs = JSON.stringify(result.outputs || []);
    return /fallback|error|unknown|safety|blocked|provider_or_validation_failure/i.test(`${reason} ${outputs}`);
  });
  const busy = [...state.results].reverse().find(item => /installation_busy/i.test(JSON.stringify(item.result)));
  $('fallback').classList.toggle('hidden', !fallback && !busy);
  if (fallback) $('fallback').textContent = JSON.stringify(fallback.result);
  if (busy) $('fallback').textContent = 'Активная сессия уже запущена. Нажмите Reset перед сменой RFID.';
}

function renderButtons(state, led, contract) {
    const session = state.session || {};
  const guestButtons = new Set([1, 2, 3, 4, 5, 7]);
  const guestMode = !session.state || ['NONE', 'WAITING_RFID'].includes(session.state);
  const sessionActive = ['AUTHENTICATED', 'ACTIVE', 'FALLBACK', 'ENDING'].includes(session.state);
  const hasButtonContract = Array.isArray(session.enabled_buttons);
    const enabledButtons = hasButtonContract ? new Set(session.enabled_buttons.map(Number)) : null
    for (const id of BUTTON_IDS) {
    const visualGuest = Boolean(session.visual_only);
    const enabled = visualGuest ? guestButtons.has(id) : (guestMode ? guestButtons.has(id) : (sessionActive && (enabledButtons === null || enabledButtons.has(id))));
    const button = buttonRefs.get(id);
    if (!button) continue;
    const color = normalizeColor(led.colors[id]);
    const colorDef = LED_COLORS[color] || LED_COLORS.invalid;
    const buttonLabel = contract.valid ? contract.labels[id] : colorDef.label;
    button.classList.add('physical-button');
    for (const led of Object.values(LED_COLORS)) button.classList.remove(led.className);
    button.classList.add(colorDef.className);
    button.classList.toggle('led-on', color !== 'off');
    button.classList.toggle('led-target', color !== 'off' && led.targets.has(id));
    button.classList.toggle('pressed', buttonInputs[id] === 'pressed');
    button.classList.toggle('disabled', !enabled);
    button.disabled = !enabled;
    button.setAttribute('aria-label', `Кнопка ${id}, ${buttonLabel}, ${buttonInputs[id]}, ${enabled ? 'enabled' : 'disabled'}`);
    button.title = enabled ? ('Кнопка ' + id + ': ' + buttonLabel) : (id === 6 && sessionActive ? 'Мишка/LLM: поднимите трубку' : 'Кнопка недоступна на этом этапе');
    const inputCell = document.querySelector(`#button-input-${id}`);
    const ledCell = document.querySelector(`#button-led-${id}`);
    const enabledCell = document.querySelector(`#button-enabled-${id}`);
    if (inputCell) inputCell.textContent = buttonInputs[id].toUpperCase();
    if (ledCell) {
      ledCell.textContent = buttonLabel;
      ledCell.className = `diag-color ${color === 'off' ? 'off' : ''}`;
      ledCell.style.setProperty('--diag-color', colorDef.css);
    }
    if (enabledCell) enabledCell.textContent = enabled ? 'YES' : 'NO';
  }
}

function renderLog(id, items, meta, value, klass) {
  const root = $(id);
  root.textContent = '';
  for (const item of items.slice(-100).reverse()) {
    const row = $('log-row').content.firstElementChild.cloneNode(true);
    row.querySelector('.log-meta').textContent = meta(item);
    row.querySelector('pre').textContent = value(item);
    const rowClass = klass(item);
    if (rowClass) row.classList.add(rowClass);
    root.append(row);
  }
}

async function playStoryAsset(command, generation) {
  const response = await fetch(apiPath(`/stand/audio/story/${encodeURIComponent(command.payload.response_id)}`));
  if (!response.ok) throw new Error(`story audio HTTP ${response.status}`);
  const data = await response.arrayBuffer();
  const context = await unlockAudio();
  if (generation !== storySpeechGeneration || !current?.session.node?.startsWith('fixture_story_')) return;
  const buffer = await context.decodeAudioData(data);
  if (generation !== storySpeechGeneration || !current?.session.node?.startsWith('fixture_story_')) return;
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
      post('/stand/button', { button_id: 7 }).catch(() => {});
    }
  };
  source.start();
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
      speechSynthesis.cancel();
    }
    if (command.type === 'audio_play' && command.payload.resolved_text) {
      if (command.payload.content_kind === 'story' && state.transport?.story_audio_mode === 'silero_wav') {
        const generation = ++storySpeechGeneration;
        playStoryAsset(command, generation).catch(error => {
          if (generation !== storySpeechGeneration || !current?.session.node?.startsWith('fixture_story_')) return;
          showError(new Error(`Silero story unavailable; audio path stopped: ${error.message}`));
        });
        continue;
      }
      // Normal responses already arrive as PCM over the websocket; speaking them again creates overlap.
      if (command.payload.content_kind !== 'story') continue;
      const utterance = new SpeechSynthesisUtterance(command.payload.resolved_text.replace('{name}', 'Fixture'));
      utterance.lang = 'ru-RU';
      const russianVoice = speechSynthesis.getVoices().find(voice => voice.lang?.toLowerCase().startsWith('ru'));
      if (russianVoice) utterance.voice = russianVoice;
      if (command.payload.content_kind === 'story') {
        const generation = ++storySpeechGeneration;
        utterance.onend = () => {
          if (generation === storySpeechGeneration && current?.session.node?.startsWith('fixture_story_')) {
            post('/stand/button', { button_id: 7 }).catch(() => {});
          }
        };
      }
      speechSynthesis.speak(utterance);
    }
  }
  // Bounded S2S text is rendered as PCM by BrowserPlaybackController. Do not
  // speak it again through speechSynthesis, otherwise every answer is doubled.
  lastResultCount = state.results.length;
}

function updateGuide(state, contract) {
    const labels = { fixture_game_repeat_pattern: ['5 · Повтори паттерн', 'Повторите световой и кнопочный паттерн на семи кнопках.'], fixture_schedule_weather: ['7 · Расписание и погода', 'Показываются расписание ребёнка по RFID и актуальная погода.'], fixture_game_one: ['2 · Игра: повтори паттерн', 'Нажимайте кнопки в ответ на световой паттерн.'], fixture_game_two: ['2 · Игра: повтори паттерн', 'Нажимайте кнопки в ответ на световой паттерн.'], fixture_game_menu: ['2 · Игра', 'Выберите игру кнопками на панели.'], fixture_game_two: ['2 · Игра: повтори паттерн', 'Повторите световой и кнопочный паттерн из семи кнопок.'], fixture_story_menu: ['3 · Выбрать сказку', 'Выберите сказку кнопками на панели.'], fixture_story_1: ['3 · Сказка: Колобок', 'Сказка рассказывается; положите трубку, чтобы завершить.'], fixture_story_2: ['3 · Сказка: Курочка Ряба', 'Сказка рассказывается; положите трубку, чтобы завершить.'], fixture_free: ['6 · Мишка / LLM', 'Отправьте короткую нейтральную фразу или включите микрофон.'], dev_welcome: ['3 · DEV-диалог', 'Свободный диалог активен. Можно говорить или вводить текст.'], dev_dialogue: ['3 · DEV-диалог', 'Свободный диалог активен. Можно говорить или вводить текст.'], dev_fallback: ['DEV · Защитный маршрут', 'Система обрабатывает ввод и возвращает безопасный ответ.'], fixture_led_test: ['4 · LED-тест', 'Проверьте световые паттерны кнопками.'] };
  const node = String(state.session.node || '');
  const guideMap = document.querySelector('.guide-map');
  if (guideMap) guideMap.hidden = String(state.session.scenario || '').toLowerCase().includes('dev') || node.startsWith('dev_');
  const map = {
    fixture_choose: ['2 · Выбрать режим', buttonLegendSummary(contract)],
    fixture_game_menu: ['3 · Выбрать игру', 'Нажмите 1, 2 или 3 для игры. Нажмите 4 для паттерна 2 → 5 → 1 → 7. Нажмите 5 для LED-теста.'],
    fixture_story_menu: ['3 · Выбрать сказку', 'Нажмите кнопку 1–5. После рассказа система вернёт вас в меню.'],
    fixture_free: ['6 · Мишка / LLM', 'Отправьте короткую нейтральную фразу, например «Как настроение?» или включите микрофон.'],
    dev_dialogue: ['DEV · Свободный диалог активен', 'Разговор уже начат после подъёма трубки. Отправляйте текст или включите микрофон; кнопки в DEV-режиме не меняют сценарий.'],
    dev_fallback: ['DEV · Safety fallback', 'Модель не смогла подготовить безопасный ответ. Нажмите кнопку 1 или попробуйте другую формулировку.'],
    fixture_game_repeat_pattern: ['4 · Повторить паттерн', 'Нажимайте кнопки согласно подсветке.'],
    fixture_led_mix: ['LED · Проверка подсветки', buttonLegendSummary(contract)],
    fixture_end: ['Сессия завершена', 'Нажмите Reset, чтобы начать следующий тест.']
  };
    const normalizedState = String(state.session.state || '').trim().toUpperCase();
  const content = normalizedState === 'ENDED' ? map.fixture_end : (node === 'fixture_schedule_weather' ? ['7 · Расписание и погода', 'Показываются расписание ребёнка по RFID и актуальная погода.'] : node === 'fixture_game_repeat_pattern' ? ['5 · Повтори паттерн', 'Повторяйте кнопки согласно подсветке.'] : (labels[node] || map[node] || (normalizedState === 'NONE' ? ['1 · Начать сессию', buttonLegendSummary(contract)] : ['Тест продолжается', 'Следуйте текущему состоянию.'])));
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
    button.innerHTML = `<span class="button-shell"><span class="button-base" aria-hidden="true"></span><span class="button-led-disc" aria-hidden="true"></span><span class="button-cap" aria-hidden="true"></span><span class="button-label">${id}</span></span>`;
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

$('present-rfid').onclick = async () => {
  clearMic();
  if (current && current.session.state !== 'NONE') await post('/stand/reset');
  await post('/stand/rfid', { tag_id: $('rfid').value });
};
$('enable-audio').onclick = async () => {
  const context = await unlockAudio();
  if (context.state === 'running') {
    const source = context.createBufferSource();
    source.buffer = context.createBuffer(1, 1, context.sampleRate);
    source.connect(context.destination);
    source.start();
    $('audio-state').textContent = 'AUDIO: READY';
    $('audio-help').textContent = 'Звук включён; ответы приходят PCM-потоком.';
    if (current) speakCommands(current);
  } else {
    $('audio-state').textContent = 'AUDIO: LOCKED';
    $('audio-help').textContent = 'Браузер не разрешил звук; нажмите кнопку ещё раз или проверьте вкладку.';
  }
};
const anonymousButton = $('anonymous');
if (anonymousButton) anonymousButton.onclick = () => { clearMic(); return post('/stand/handset', { state: 'up' }); };
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
  if (!current || !['fixture_free', 'dev_dialogue'].includes(current.session.node)) {
    showError(new Error('Сначала откройте разговор: кнопка 3 в детском режиме или подъём трубки в DEV-режиме.'));
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
connect();
refresh();
setInterval(() => refresh().catch((error) => console.warn("status refresh failed", error)), 1000);
