from pathlib import Path


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
    assert "command.payload.response_id === 'PHRASE_001'" in phrase_player
    assert "generation === storySpeechGeneration" in phrase_player
    assert "post('/stand/audio/completed', { session_id: command.payload.session_id, response_id: command.payload.response_id, playback_lease: command.payload.playback_lease, generation: command.payload.generation })" in phrase_player


def test_webpanel_observer_lifecycle_is_single_and_bounded():
    source = CONSOLE.read_text(encoding="utf-8")
    html = (CONSOLE.parent / "index.html").read_text(encoding="utf-8")
    assert "fetchWithTimeout" in source
    assert "refreshInFlight" in source
    assert "setInterval" not in source
    assert "visibilitychange" in source and "pagehide" in source
    assert "schedulePolling" in source and "scheduleReconnect" in source
    assert "AbortController" in source
    assert "Ожидание RFID" not in html
    assert "Без RFID" not in html
    assert "id=\"present-rfid\"" not in html
    assert "id=\"device-rfid\"" not in html


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


def test_panel_command_projection_excludes_tokens_and_transcripts():
    source = (CONSOLE.parent.parent / "stand.py").read_text(encoding="utf-8")
    panel = source[source.index("def panel_snapshot"):source.index("def close", source.index("def panel_snapshot"))]
    assert '"token"' not in panel
    assert "resolved_text" not in panel


def test_webpanel_allows_microphone_on_exhibition_name_capture_and_tracks_live_effects():
    source = CONSOLE.read_text(encoding="utf-8")
    html = (CONSOLE.parent / "index.html").read_text(encoding="utf-8")
    assert "exhibition_name_capture" in source
    assert "20261014-live-effects-5" in html
    assert "BREATHING" in html
