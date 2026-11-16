"""Durable human review and deterministic routing around model calls."""
from dataclasses import asdict
from operator import add
from subprocess import SubprocessError
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send, interrupt

from .catalog import Source, Vacancy, merge_catalog
from .matching import Matcher
from .pipeline import rank, shortlist_candidates
from .web_search import CodexSearchPass


class SourceTask(TypedDict):
    source_id: str
    criteria: dict


class SourceBatch(TypedDict):
    id: str
    status: str
    vacancies: list[dict]


class RunState(TypedDict, total=False):
    created_at: str
    seen_ids: list[str]
    historical_count: int
    cv: str
    skills: list[str]
    location: str
    mode: str
    preferences: dict
    source_ids: list[str] | None
    source_batches: Annotated[list[SourceBatch], add]
    source_results: list[dict]
    catalog_count: int
    source_count: int
    unavailable_sources: list[str]
    candidates: list[dict]
    excluded: list[dict]
    shortlist: list[dict]
    status: str
    reviewed_ids: list[str]


def build_workflow(sources: tuple[Source | CodexSearchPass, ...], matcher: Matcher, checkpointer):
    by_id = {source.name: source for source in sources}
    source_order = {source_id: index for index, source_id in enumerate(by_id)}

    def plan(state: RunState) -> list[Send] | str:
        source_ids = state.get("source_ids")
        selected = list(by_id) if source_ids is None else list(dict.fromkeys(source_ids))
        criteria = {"skills": state["skills"], "base_location": state["location"],
                    "mode": state["mode"], **state.get("preferences", {})}
        return [Send("collect_source", {"source_id": source_id, "criteria": criteria})
                for source_id in selected] or "merge_sources"

    def collect_source(task: SourceTask) -> dict:
        source = by_id[task["source_id"]]
        try:
            vacancies = source.collect(task["criteria"]) if isinstance(source, CodexSearchPass) else source.collect()
        except (OSError, ValueError, KeyError, TypeError, SubprocessError):
            return {"source_batches": [{"id": source.name, "status": "unavailable", "vacancies": []}]}
        return {"source_batches": [{
            "id": source.name, "status": "ok" if vacancies else "empty",
            "vacancies": [asdict(vacancy) for vacancy in vacancies],
        }]}

    def merge_sources(state: RunState) -> dict:
        batches = sorted(state.get("source_batches", []), key=lambda batch: source_order[batch["id"]])
        vacancies = merge_catalog(Vacancy(**item) for batch in batches for item in batch["vacancies"])
        candidates, excluded = shortlist_candidates(
            vacancies, state["location"], state["mode"], state.get("preferences"),
        )
        seen_ids = set(state.get("seen_ids", []))
        new_candidates = [job for job in candidates if job["id"] not in seen_ids]
        return {"catalog_count": len(vacancies), "source_count": len(batches),
                "unavailable_sources": [batch["id"] for batch in batches if batch["status"] == "unavailable"],
                "source_results": [{"id": batch["id"], "status": batch["status"], "count": len(batch["vacancies"])}
                                   for batch in batches],
                "candidates": new_candidates, "excluded": excluded,
                "historical_count": len(candidates) - len(new_candidates)}

    def score(state: RunState) -> dict:
        return {"shortlist": rank(
            state["candidates"], state["cv"], state["skills"], matcher,
            preferences={"base_location": state["location"], "mode": state["mode"], **state.get("preferences", {})},
        )[:100]}

    def review(state: RunState) -> dict:
        decision = interrupt({"action": "review_shortlist", "vacancy_ids": [job["id"] for job in state["shortlist"]]})
        allowed = {job["id"] for job in state["shortlist"]}
        if not set(decision).issubset(allowed):
            raise ValueError("Review contains vacancies outside this run")
        return {"reviewed_ids": list(dict.fromkeys(decision)), "status": "reviewed"}

    def empty(state: RunState) -> dict:
        source_count = state.get("source_count", len(sources))
        status = "sources_unavailable" if source_count and len(state["unavailable_sources"]) == source_count else "empty"
        return {"shortlist": [], "status": status}

    graph = StateGraph(RunState)
    graph.add_node("collect_source", collect_source)
    graph.add_node("merge_sources", merge_sources)
    graph.add_node("score", score)
    graph.add_node("review", review)
    graph.add_node("empty", empty)
    graph.add_conditional_edges(START, plan, ["collect_source", "merge_sources"])
    graph.add_edge("collect_source", "merge_sources")
    graph.add_conditional_edges("merge_sources", lambda state: "score" if state["candidates"] else "empty",
                                {"score": "score", "empty": "empty"})
    graph.add_edge("score", "review")
    graph.add_edge("review", END)
    graph.add_edge("empty", END)
    return graph.compile(checkpointer=checkpointer)
