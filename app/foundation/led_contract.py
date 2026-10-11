from __future__ import annotations
from typing import Any, Iterable
BUTTON_LED_COLORS = {1: "red", 2: "orange", 3: "yellow", 4: "green", 5: "cyan", 6: "blue", 7: "purple"}
BUTTON_LED_LABELS = {button_id: color.upper() for button_id, color in BUTTON_LED_COLORS.items()}

def button_contract() -> dict[str, Any]:
    """Return the single product button/color contract consumed by the UI."""
    return {
        "valid": True,
        "version": "1",
        "buttons": [
            {"id": button_id, "color": color, "label": BUTTON_LED_LABELS[button_id]}
            for button_id, color in BUTTON_LED_COLORS.items()
        ],
    }

def validate_button_contract(value: Any) -> list[str]:
    """Validate a serialized contract without silently normalizing errors."""
    entries = value.get("buttons") if isinstance(value, dict) else value
    if not isinstance(entries, list):
        return ["buttons must be a list"]
    issues: list[str] = []
    seen: set[int] = set()
    allowed = set(BUTTON_LED_COLORS.values())
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            issues.append(f"buttons[{index}] must be an object")
            continue
        button_id = entry.get("id")
        if isinstance(button_id, bool) or not isinstance(button_id, int):
            issues.append(f"buttons[{index}].id must be an integer")
            continue
        if button_id in seen:
            issues.append(f"duplicate button {button_id}")
        seen.add(button_id)
        if button_id not in BUTTON_LED_COLORS:
            issues.append(f"unknown button {button_id}")
        color = str(entry.get("color", "")).lower()
        if color not in allowed:
            issues.append(f"invalid color for button {button_id}: {color or '<empty>'}")
        if entry.get("label") != color.upper():
            issues.append(f"label mismatch for button {button_id}")
    missing = sorted(set(BUTTON_LED_COLORS) - seen)
    if missing:
        issues.append(f"missing buttons: {','.join(map(str, missing))}")
    return issues

def fixed_button_colors(button_ids: Iterable[int] | None = None):
    ids = sorted(BUTTON_LED_COLORS if button_ids is None else {int(x) for x in button_ids if int(x) in BUTTON_LED_COLORS})
    return {str(x): BUTTON_LED_COLORS[x] for x in ids}
def _ids(pattern):
    ids = tuple(sorted({int(c) for c in str(pattern) if c in "1234567"}))
    return ids or tuple(BUTTON_LED_COLORS)
def canonical_led_command(payload: dict[str, Any]):
    pattern = str(payload.get("pattern", "off"))
    if not pattern: raise ValueError("LED pattern must be non-empty")
    duration = int(payload.get("duration_ms", 0))
    if duration < 0: raise ValueError("LED duration must be non-negative")
    if pattern == "off":
        return {"valid": True, "pattern": "off", "color": "off", "colors": {}, "targets": [], "duration_ms": duration}
    raw = payload.get("colors")
    if raw is not None:
        if not isinstance(raw, dict): raise ValueError("LED colors must be an object")
        ids=[]
        for key, value in raw.items():
            if not str(key).isdigit() or int(key) not in BUTTON_LED_COLORS: raise ValueError("LED color map contains an unknown button")
            if str(value).lower() != "off": ids.append(int(key))
        return {"valid": True, "pattern": pattern, "color": "rainbow" if pattern == "rfid_wave" else "mixed", "colors": fixed_button_colors(ids), "targets": sorted(ids), "duration_ms": duration}
    color = str(payload.get("color", "off")).lower()
    if color not in {"off", "red", "orange", "yellow", "green", "cyan", "blue", "purple", "white"}: raise ValueError("LED command contains an unknown color")
    if color == "off" or pattern == "off": return {"valid": True, "pattern": pattern, "color": "off", "colors": {}, "targets": [], "duration_ms": duration}
    ids = _ids(pattern)
    return {"valid": True, "pattern": pattern, "color": "fixed", "colors": fixed_button_colors(ids), "targets": list(ids), "duration_ms": duration}
def canonical_led_state(commands):
    candidates=[(i,c) for i,c in enumerate(commands) if isinstance(c,dict) and c.get("type")=="led_pattern"]
    if not candidates: return {"valid": True, "pattern": "off", "color": "off", "colors": {}, "targets": [], "sequence": 0}
    _, command=max(candidates, key=lambda x:(int(x[1].get("sequence",0)),x[0]))
    try: state=canonical_led_command(dict(command.get("payload") or {}))
    except (TypeError,ValueError,KeyError) as exc: return {"valid": False, "pattern": "invalid", "color": "invalid", "colors": {}, "targets": [], "sequence": int(command.get("sequence",0)), "reason": str(exc)}
    state["sequence"]=int(command.get("sequence",0)); return state
