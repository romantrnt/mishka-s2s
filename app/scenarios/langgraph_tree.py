"""Compile the content scenario into a LangGraph-compatible inspection tree.

The JSON scenario remains the source of truth for behaviour.  This module
turns that document into a stable graph descriptor for the engineering stand
and, when the optional ``langgraph`` package is installed, into a real
StateGraph.  The foundation runtime still owns side effects (audio, LED,
history and cancellation); the graph is deliberately side-effect free.
"""

from __future__ import annotations

from typing import Any, TypedDict


class ScenarioGraphState(TypedDict, total=False):
    node_id: str
    transition: str


def describe_scenario(document: dict[str, Any]) -> dict[str, Any]:
    """Return a JSON-safe graph used by the console and test reports."""

    nodes = document.get("nodes", [])
    node_by_id = {str(node.get("id")): node for node in nodes if isinstance(node, dict)}
    graph_nodes: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []
    for node_id, node in node_by_id.items():
        transitions = node.get("transitions") or {}
        node_edges: list[dict[str, str]] = []
        for trigger, target in transitions.items():
            if target and str(target) in node_by_id:
                edge = {"trigger": str(trigger), "target": str(target)}
                node_edges.append(edge)
                edges.append({"source": node_id, **edge})
        for key in ("fallback", "fallback_node", "timeout_node", "on_handset_down", "on_barge_in", "on_safety_signal", "return_node"):
            target = node.get(key)
            if target and str(target) in node_by_id and not any(item["trigger"] == key for item in node_edges):
                edge = {"trigger": key, "target": str(target)}
                node_edges.append(edge)
                edges.append({"source": node_id, **edge})
        graph_nodes.append({
            "id": node_id,
            "type": str(node.get("type", "unknown")),
            "prompt": node.get("prompt"),
            "timeout": node.get("timeout", 0),
            "edges": node_edges,
            "metadata": node.get("metadata") or {},
        })
    return {
        "engine": "langgraph",
        "source": str(document.get("id", "scenario")),
        "version": (document.get("metadata") or {}).get("version"),
        "entry": str(document.get("start", "")),
        "terminal": [item["id"] for item in graph_nodes if item["type"] == "end"],
        "nodes": graph_nodes,
        "edges": edges,
    }


def to_mermaid(tree: dict[str, Any]) -> str:
    """Render a compact diagram suitable for docs and issue comments."""

    lines = ["flowchart TD"]
    for node in tree.get("nodes", []):
        label = f"{node['id']}\\n[{node['type']}]"
        lines.append(f"    {node['id']}[{label!r}]")
    for edge in tree.get("edges", []):
        lines.append(f"    {edge['source']} -->|{edge['trigger']}| {edge['target']}")
    return "\n".join(lines)


def build_langgraph(document: dict[str, Any]):
    """Build a side-effect-free StateGraph when LangGraph is available.

    The optional dependency is intentionally not required for the mock stand.
    This keeps ``make run-stand`` usable in a clean CPU environment while the
    same content can be visualized/executed with LangGraph on a model host.
    """

    try:
        from langgraph.graph import END, StateGraph
    except ImportError:
        return None

    tree = describe_scenario(document)
    graph = StateGraph(ScenarioGraphState)
    for node in tree["nodes"]:
        node_id = node["id"]

        def visit(state: ScenarioGraphState, current: str = node_id) -> ScenarioGraphState:
            return {**state, "node_id": current}

        graph.add_node(node_id, visit)
    graph.set_entry_point(tree["entry"])
    for node in tree["nodes"]:
        node_id = node["id"]
        targets = {edge["target"] for edge in node["edges"]}
        if node["type"] == "end" or not targets:
            graph.add_edge(node_id, END)
        elif len(targets) == 1:
            graph.add_edge(node_id, next(iter(targets)))
        else:
            graph.add_conditional_edges(node_id, lambda state, current=node_id: state.get("transition", "timeout"), {edge["trigger"]: edge["target"] for edge in node["edges"]})
    return graph.compile()

