from __future__ import annotations

from typing import Any, TypedDict

try:
    from langgraph.graph import END, StateGraph
except ImportError:  # pragma: no cover - clean mock environments may omit optional package
    END = "__end__"
    StateGraph = None


class ScenarioGraphState(TypedDict, total=False):
    node_id: str
    trigger: str
    requested_target: str | None
    next_node: str | None
    ok: bool
    reason: str
    trace: list[str]


def _nodes(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(node.get("id")): node for node in document.get("nodes", []) if node.get("id")}


def _trigger(reason: str) -> str:
    value = str(reason or "").strip().lower()
    if value.startswith("button:"):
        return "button_" + value.split(":", 1)[1]
    if value.startswith("intent:"):
        return value.split(":", 1)[1]
    aliases = {
        "auto": "next",
        "advance": "next",
        "say_done": "next",
        "story_completed": "next",
        "s2s_return": "next",
        "handset_lifted": "next",
        "game_success": "success",
        "game_failure": "failure",
        "game_timeout": "timeout",
        "s2s_timeout": "timeout",
        "handset_down": "on_handset_down",
        "barge_in": "on_barge_in",
        "safety": "on_safety_signal",
    }
    if value.startswith("button_") and value.endswith(("_dialogue", "_schedule_weather")):
        return value.split("_", 2)[0] + "_" + value.split("_", 2)[1]
    return aliases.get(value, value)


def _build_graph(document: dict[str, Any]):
    if StateGraph is None:
        return None
    node_map = _nodes(document)
    entry = str(document.get("start") or next(iter(node_map), ""))
    graph = StateGraph(ScenarioGraphState)
    graph.add_node("__dispatch__", lambda state: state)

    def make_node(node_id: str):
        def visit(state: ScenarioGraphState) -> dict[str, Any]:
            node = node_map[node_id]
            transitions = {str(k): str(v) for k, v in (node.get("transitions") or {}).items() if v}
            trigger = str(state.get("trigger") or "")
            requested = state.get("requested_target")
            target = transitions.get(trigger)
            if target is None and trigger in {"next", "enter"}:
                target = transitions.get("next")
            if target is None and requested and str(requested) in set(transitions.values()) and trigger in {"next", "enter", "timeout", "success", "failure"}:
                target = str(requested)
            fallback = str(node.get("fallback") or transitions.get("fallback") or "")
            if target is None and requested and str(requested) == fallback:
                target = str(requested)
            if node.get("type") == "end" and not target:
                target = node_id
            if target and target in node_map:
                return {
                    "next_node": target,
                    "ok": True,
                    "reason": "transition_allowed",
                    "trace": list(state.get("trace") or []) + [node_id, target],
                }
            return {
                "next_node": fallback if fallback in node_map else None,
                "ok": False,
                "reason": "transition_not_allowlisted",
                "trace": list(state.get("trace") or []) + [node_id],
            }
        return visit

    for node_id in node_map:
        graph.add_node(node_id, make_node(node_id))
    graph.set_entry_point("__dispatch__")
    graph.add_conditional_edges("__dispatch__", lambda state: state.get("node_id") or entry, {node_id: node_id for node_id in node_map})
    for node_id in node_map:
        graph.add_edge(node_id, END)
    return graph.compile()


class LangGraphRuntime:
    """Deterministic LangGraph gate for scenario transitions.

    The content JSON remains the source of truth. The compiled StateGraph executes
    exactly one transition per application event, and rejects targets that are not
    allowlisted by the current scenario node. The legacy runtime can be retained as
    a fallback when the optional dependency is unavailable or explicitly disabled.
    """

    def __init__(self, documents: dict[str, dict[str, Any]], *, enabled: bool = True, strict: bool = True):
        self.enabled = bool(enabled)
        self.strict = bool(strict)
        self.documents = {str(key): value for key, value in documents.items() if isinstance(value, dict)}
        self.graphs: dict[str, Any] = {}
        self.errors: dict[str, str] = {}
        if self.enabled:
            for scenario_id, document in self.documents.items():
                try:
                    self.graphs[scenario_id] = _build_graph(document)
                    if self.graphs[scenario_id] is None:
                        self.errors[scenario_id] = "scenario_engine_unavailable"
                except Exception as exc:  # content cannot take down the application
                    self.errors[scenario_id] = f"scenario_compile_error:{type(exc).__name__}"

    def authorize(self, scenario_id: str, current_node: str | None, target: str, reason: str) -> dict[str, Any]:
        document = self.documents.get(scenario_id)
        environment = "production" if self.strict else "mock"
        if not self.enabled:
            if self.strict:
                return {
                    "enabled": False, "ok": False, "next_node": None,
                    "reason": "legacy_fallback_disabled",
                    "environment": environment, "evidence": "strict_production",
                }
            return {
                "enabled": False, "ok": True, "next_node": target,
                "reason": "legacy_fallback",
                "environment": environment, "evidence": "explicit_mock_or_local",
            }
        if document is None:
            if self.strict:
                return {
                    "enabled": True, "ok": False, "next_node": None,
                    "reason": "scenario_document_missing",
                    "environment": environment, "evidence": "strict_production",
                }
            return {
                "enabled": True, "ok": True, "next_node": target,
                "reason": "legacy_fallback",
                "environment": environment, "evidence": "explicit_mock_or_local",
            }
        graph = self.graphs.get(scenario_id)
        if graph is None:
            raw_reason = self.errors.get(scenario_id, "scenario_engine_unavailable")
            reason = raw_reason if raw_reason in {"scenario_engine_unavailable"} else (
                "scenario_compile_error" if raw_reason.startswith("scenario_compile_error") else "provider_or_validation_failure"
            )
            return {
                "enabled": True, "ok": False, "next_node": None,
                "reason": reason, "detail": raw_reason,
                "environment": environment,
            }
        node_map = _nodes(document)
        entry = str(document.get("start") or next(iter(node_map), ""))
        current = str(current_node or entry)
        trigger = _trigger(reason)
        try:
            result = graph.invoke({"node_id": current, "trigger": trigger, "requested_target": str(target), "trace": []})
        except Exception as exc:
            return {
                "enabled": True, "ok": False, "next_node": None,
                "reason": "provider_or_validation_failure",
                "detail": f"graph_invoke_error:{type(exc).__name__}",
                "environment": environment,
            }
        next_node = result.get("next_node")
        ok = bool(result.get("ok")) and str(next_node or "") == str(target)
        if current == entry and not current_node and str(target) == entry:
            ok = True
            next_node = entry
        return {
            "enabled": True,
            "ok": ok,
            "next_node": next_node,
            "reason": result.get("reason", "transition_rejected") if ok else "transition_rejected",
            "trigger": trigger,
            "trace": result.get("trace", []),
        }

    def status(self) -> dict[str, Any]:
        ready = bool(self.enabled and self.documents and not self.errors)
        return {
            "enabled": self.enabled,
            "strict": self.strict,
            "provider": "langgraph",
            "environment": "production" if self.strict else "mock",
            "ready": ready,
            "reason": None if ready else (
                "legacy_fallback_disabled" if self.strict and not self.enabled
                else "scenario_compile_error" if self.errors
                else "scenario_document_missing"
            ),
            "compiled": sorted(self.graphs),
            "errors": dict(self.errors),
            "scenario_count": len(self.documents),
        }
