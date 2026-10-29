from pathlib import Path
import subprocess


CONSOLE = Path(__file__).parents[1] / "app" / "foundation" / "console" / "console.js"


def test_webpanel_projects_successful_runtime_results_without_raw_json_fallback() -> None:
    source = CONSOLE.read_text(encoding="utf-8")
    assert "JSON.stringify(fallback.result)" not in source
    assert "effect_id" not in source[source.index("const fallback ="):source.index("function normalizeLightingState")]
    assert "Array.isArray(result.outputs) ? result.outputs : []" in source
    assert "reasonLooksFailed" in source and "outputLooksFailed" in source


def test_webpanel_has_current_handset_input_route_and_state_projection() -> None:
    source = CONSOLE.read_text(encoding="utf-8")
    assert "post('/stand/handset', { state: 'up' })" in source
    assert 'message.type === "stand_state"' in source
    assert "handset_up" in source


def test_welcome_phrase_completion_reaches_canonical_audio_endpoint():
    source = CONSOLE.read_text(encoding="utf-8")
    phrase_player = source[source.index("async function playPhraseAsset"):source.index("async function playStoryAsset")]
    assert "generation !== storySpeechGeneration" in phrase_player
    assert "post('/stand/audio/completed', audioIdentity(command))" in phrase_player
    assert "playback_id: command.payload.playback_id" in source


def test_webpanel_observer_lifecycle_is_single_and_bounded():
    source = CONSOLE.read_text(encoding="utf-8")
    html = (CONSOLE.parent / "index.html").read_text(encoding="utf-8")
    assert "fetchWithTimeout" in source
    assert "refreshInFlight" in source
    assert "setInterval" not in source
    assert "visibilitychange" in source and "pagehide" in source
    assert "schedulePolling" in source and "scheduleReconnect" in source
    assert "AbortController" in source
    assert "id=\"present-rfid\"" not in html
    assert "id=\"device-rfid\"" not in html
    assert "post('/stand/rfid'" not in source


def test_webpanel_state_is_redacted_and_backend_observer_disconnect_is_non_mutating():
    source = (CONSOLE.parent.parent / "stand.py").read_text(encoding="utf-8")
    assert "def panel_snapshot" in source
    assert "MAX_CLIENTS = 16" in source
    assert "SEND_TIMEOUT_S = 0.75" in source
    assert "if websocket not in stand.hub.clients" in source
    assert "must never reset the exhibition session" in source
    assert '@api.get(\"/stand/trace\")' in source


def test_webpanel_lifecycle_uses_ws_for_state_and_aborts_hidden_refreshes():
    source = CONSOLE.read_text(encoding="utf-8")
    assert "includeState: false" in source
    assert "includeState: !websocketLive" in source
    assert "if (ws !== socket) return;" in source
    assert "stopPolling({ abortRefresh: true })" in source
    assert "parentSignal?.removeEventListener" in source


def test_webpanel_never_renders_user_or_assistant_text():
    source = CONSOLE.read_text(encoding="utf-8")
    html = (CONSOLE.parent / "index.html").read_text(encoding="utf-8")
    assert "Ответ подготовлен · текст скрыт" in source
    assert "String(output.text || output.resolved_text" not in source
    assert "text: String(directText)" not in source
    assert "rfid_wave" not in source
    assert "profile-label" not in html
    assert "pipeline-profile" not in html


def test_panel_command_projection_excludes_private_identity_and_transcripts():
    source = (CONSOLE.parent.parent / "stand.py").read_text(encoding="utf-8")
    panel = source[source.index("def panel_snapshot"):source.index("def close", source.index("def panel_snapshot"))]
    for private_field in ("resolved_text", "tag_id", "display_name", "date_of_birth", "profile_ref"):
        assert private_field not in panel


def test_webpanel_allows_microphone_on_exhibition_name_capture_and_tracks_live_effects():
    source = CONSOLE.read_text(encoding="utf-8")
    html = (CONSOLE.parent / "index.html").read_text(encoding="utf-8")
    assert "exhibition_name_capture" in source
    assert "exhibition_age_selection" in source
    assert "console.js?v=20261029-rfid-status-" in html
    assert 'id="wave-state">LIGHTS OFF<' in html
    assert "Тёмное ожидание" in html
    assert "BREATHING" not in html


def test_webpanel_projects_only_backend_rfid_modes_without_private_payloads():
    script = r"""
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const start = source.indexOf('function cardStatusProjection(');
const end = source.indexOf('function render(', start);
assert(start >= 0 && end > start);
const context = vm.createContext({});
vm.runInContext(source.slice(start, end), context);
const project = mode => context.cardStatusProjection({
  canonical_state: {mode, tag_id: 'PRIVATE_UID', display_name: 'PRIVATE_NAME', profile: {secret: 'PRIVATE_PROFILE'}},
  identity: {status: 'authorized', tag_id: 'PRIVATE_UID'}
});
assert.strictEqual(project('rfid_lookup').label, 'Проверка карты');
assert.strictEqual(project('rfid_ready').label, 'Карта принята · поднимите трубку');
for (const mode of ['idle', 'voice_welcome', 'unknown', 'PRIVATE_UID']) {
  assert.strictEqual(project(mode).label, 'Нет активной карты');
}
assert(!JSON.stringify([project('rfid_lookup'), project('rfid_ready')]).includes('PRIVATE_'));
"""
    result = subprocess.run(["node", "-e", script, str(CONSOLE)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_webpanel_keeps_guest_identity_and_reader_readiness_separate():
    source = CONSOLE.read_text(encoding="utf-8")
    html = (CONSOLE.parent / "index.html").read_text(encoding="utf-8")
    for name in ("guest", "identity", "reader"):
        assert f"readiness.{name}_ready" in source
        assert f'id="{name}-status">unknown<' in html
    assert 'id="device-card" aria-live="polite"' in html
