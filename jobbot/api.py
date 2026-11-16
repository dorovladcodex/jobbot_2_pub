"""Local API for starting, inspecting and resuming search runs."""
from contextlib import asynccontextmanager, closing
from datetime import datetime, timezone
import os
import subprocess
from pathlib import Path
from threading import Lock
from typing import Literal
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.staticfiles import StaticFiles
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from pydantic import BaseModel, Field, ValidationError

from .ats import ATSBoard
from .applications import ApplicationStore
from .application_sync import build_application_sync
from .catalog import DEMO_CV, DEMO_ROLES, DEMO_SKILLS, DemoSource
from .codex import CodexMatcher
from .history import saved_runs, vacancy_history
from .feeds import PUBLIC_FEEDS
from .matching import KeywordMatcher
from .mailbox import gmail_app_id
from .presentation import vacancy_presentation
from .resume import extract_resume, redact_contacts, suggest_profile
from .web_search import WEB_SEARCH_PASSES
from .workflow import build_workflow


class SearchRequest(BaseModel):
    cv: str = Field(min_length=1, max_length=15000)
    skills: list[str] = Field(min_length=1, max_length=20)
    location: str = Field(default="", max_length=100)
    mode: Literal["any", "remote", "hybrid", "onsite"] = "any"
    work_modes: list[Literal["remote", "hybrid", "onsite"]] = Field(default_factory=list, max_length=3)
    local_areas: list[str] = Field(default_factory=list, max_length=12)
    target_roles: list[str] = Field(default_factory=list, max_length=12)
    language: Literal["any", "english"] = "any"
    max_german_level: Literal["A1", "A2", "B1", "B2", "C1", "C2"] | None = None
    contract_type: Literal["any", "permanent", "fixed_term", "freelance"] = "any"
    seniority: Literal["any", "senior_plus"] = "any"
    min_salary_eur: int | None = Field(default=None, gt=0, le=2_000_000)
    source_ids: list[str] | None = None


class ReviewRequest(BaseModel):
    selected: list[str] = Field(max_length=100)

class EmailSyncRequest(BaseModel):
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=254)


class LinkApplicationRequest(BaseModel):
    job_id: str = Field(min_length=1, max_length=100)


def snapshot_result(run_id: UUID, snapshot) -> dict:
    state = snapshot.values
    if not state:
        raise HTTPException(404, "Run not found")
    return {"id": str(run_id), "status": state.get("status", "awaiting_review"),
            "created_at": state.get("created_at"), "historical_count": state.get("historical_count", 0),
            "catalog_count": state.get("catalog_count", 0), "excluded": state.get("excluded", []),
            "shortlist": [vacancy_presentation(job, state.get("preferences", {}), state.get("skills", []))
                          for job in state.get("shortlist", [])],
            "unavailable_sources": state.get("unavailable_sources", []),
            "source_results": state.get("source_results", []),
            "reviewed_ids": state.get("reviewed_ids", [])}


def config_for(run_id: UUID) -> dict:
    return {"configurable": {"thread_id": str(run_id)}, "max_concurrency": 3}


def create_app(db_path: Path | None = None) -> FastAPI:
    lock = Lock()
    sync_lock = Lock()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        path = db_path or Path(os.getenv("JOBBOT_DB", "runtime/jobbot.sqlite"))
        path.parent.mkdir(parents=True, exist_ok=True)
        board_specs = os.getenv("JOBBOT_ATS_BOARDS", "").split(",")
        boards = tuple(ATSBoard(*spec.split(":", 1))
                       for spec in dict.fromkeys(spec.strip() for spec in board_specs if spec.strip()))
        search_mode = os.getenv("JOBBOT_SEARCH_MODE", "live")
        if search_mode not in {"live", "demo"}:
            raise ValueError("JOBBOT_SEARCH_MODE must be live or demo")
        if search_mode == "demo":
            demo_source = DemoSource()
            sources = (demo_source,)
            search_sources = [
                {"id": demo_source.name, "label": "Demo catalog", "kind": "demo", "default_selected": True},
            ]
        else:
            sources = (*boards, *PUBLIC_FEEDS, *WEB_SEARCH_PASSES)
            search_sources = [
                {"id": source.name, "label": source.label, "kind": "web", "domains": source.domains,
                 "default_selected": source.selected_by_default}
                for source in WEB_SEARCH_PASSES
            ] + [
                {"id": feed.name, "label": feed.label, "kind": "feed", "default_selected": True}
                for feed in PUBLIC_FEEDS
            ] + [
                {"id": board.name, "label": board.name, "kind": "ats", "default_selected": False}
                for board in boards
            ]
        mode = os.getenv("JOBBOT_MATCHER", "codex" if search_mode == "live" else "baseline")
        if mode not in {"baseline", "codex"}:
            raise ValueError("JOBBOT_MATCHER must be baseline or codex")
        profile_mode = os.getenv("JOBBOT_PROFILE", "codex")
        if profile_mode not in {"baseline", "codex"}:
            raise ValueError("JOBBOT_PROFILE must be baseline or codex")
        matcher = CodexMatcher() if mode == "codex" else KeywordMatcher()
        with closing(ApplicationStore(path)) as applications, SqliteSaver.from_conn_string(str(path)) as saver:
            app.state.saver = saver
            app.state.applications = applications
            app.state.graph = build_workflow(sources, matcher, saver)
            app.state.matcher = matcher.name
            app.state.profile_uses_codex = profile_mode == "codex"
            app.state.catalog = "Codex web search" if search_mode == "live" else "synthetic catalog"
            app.state.search_sources = search_sources
            yield

    app = FastAPI(title="JobBot", lifespan=lifespan)

    def known_jobs() -> list[dict]:
        return vacancy_history(saved_runs(app.state.saver), include_latest=True)

    def visible_result(result: dict) -> dict:
        applied = app.state.applications.linked_job_ids(known_jobs())
        result["shortlist"] = [job for job in result["shortlist"] if job["id"] not in applied]
        if not result["shortlist"] and result["status"] == "awaiting_review":
            result["status"] = "empty"
        visible = {job["id"] for job in result["shortlist"]}
        result["reviewed_ids"] = [job_id for job_id in result["reviewed_ids"] if job_id in visible]
        return result

    def application_results() -> dict:
        jobs = known_jobs()
        by_id = {job["id"]: job for job in jobs}
        app.state.applications.reconcile(jobs)
        result = app.state.applications.list()
        for application in result["applications"]:
            application["vacancy"] = by_id.get(application["job_id"])
        try:
            gmail_app_id()
            result["plugin_installed"] = True
        except (OSError, ValueError):
            result["plugin_installed"] = False
        return result

    @app.get("/api/applications")
    def get_applications():
        with lock:
            return application_results()

    @app.post("/api/applications/sync")
    def sync_applications(request: EmailSyncRequest):
        mailbox = request.email.casefold()
        if not sync_lock.acquire(blocking=False):
            raise HTTPException(409, "An email sync is already running")
        try:
            with lock:
                since, cursor = app.state.applications.cursor(mailbox)
                seen = app.state.applications.seen_messages(mailbox)

            def save_events(state):
                with lock:
                    app.state.applications.save(mailbox, state["batch"], state["events"], known_jobs(), since)
                    return {**application_results(), "scanned": len(state["batch"]["messages"]),
                            "updates": len(state["events"]), "has_more": bool(state["batch"]["next_page_token"])}

            graph = build_application_sync(save_events)
            return graph.invoke({"mailbox": mailbox, "since": since, "cursor": cursor,
                                 "processed_ids": list(seen)})["result"]
        except ValidationError as error:
            raise HTTPException(502, "Email analysis returned invalid data. No sync changes were saved.") from error
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
            detail = str(error) if isinstance(error, ValueError) else (
                "Email sync failed. Check the Gmail connection and Codex access, then retry."
            )
            raise HTTPException(502, detail) from error
        finally:
            sync_lock.release()

    @app.post("/api/applications/{application_id}/link")
    def link_application(application_id: str, request: LinkApplicationRequest):
        with lock:
            jobs = {job["id"]: job for job in known_jobs()}
            if request.job_id not in jobs:
                raise HTTPException(422, "Choose a saved vacancy")
            try:
                app.state.applications.link(application_id, jobs[request.job_id])
            except ValueError as error:
                raise HTTPException(404, str(error)) from error
            return application_results()

    @app.get("/api/demo")
    def demo():
        return {"cv": DEMO_CV, "skills": DEMO_SKILLS, "target_roles": DEMO_ROLES,
                "profile_uses_codex": app.state.profile_uses_codex, "matcher": app.state.matcher,
                "catalog": app.state.catalog, "search_sources": app.state.search_sources}

    @app.post("/api/resume")
    def resume(file: UploadFile):
        try:
            cv = extract_resume(file.filename or "", file.file.read(2_000_001))
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        finally:
            file.file.close()
        return {"cv": cv, **suggest_profile(cv, use_codex=app.state.profile_uses_codex)}

    @app.post("/api/runs", status_code=201)
    def start(request: SearchRequest):
        cv = redact_contacts(request.cv).strip()
        skills = list({skill.strip().casefold(): skill.strip() for skill in request.skills if skill.strip()}.values())
        if not cv or not skills or any(len(skill) > 80 for skill in skills):
            raise HTTPException(422, "Add a resume and up to 20 skills of at most 80 characters each")
        source_ids = request.source_ids
        if source_ids is None:
            source_ids = [source["id"] for source in app.state.search_sources if source["default_selected"]]
        available_ids = {source["id"] for source in app.state.search_sources}
        if not source_ids or not available_ids.issuperset(source_ids):
            raise HTTPException(422, "Select at least one configured search source")
        preferences = request.model_dump(exclude={"cv", "skills", "location", "mode", "source_ids"})
        for field in ("target_roles", "local_areas"):
            preferences[field] = list(dict.fromkeys(value.strip() for value in preferences[field] if value.strip()))
            if any(len(value) > 100 for value in preferences[field]):
                raise HTTPException(422, "Target roles and local areas must be at most 100 characters each")
        run_id = uuid4()
        config = config_for(run_id)
        graph = app.state.graph
        with lock:
            saved_jobs = known_jobs()
            seen_ids = [job["id"] for job in saved_jobs]
            seen_ids.extend(app.state.applications.linked_job_ids(saved_jobs))
            graph.invoke({"cv": cv, "skills": skills, "location": request.location.strip(),
                          "created_at": datetime.now(timezone.utc).isoformat(), "seen_ids": seen_ids,
                          "mode": request.mode, "preferences": preferences, "source_ids": source_ids}, config=config)
            return visible_result(snapshot_result(run_id, graph.get_state(config)))

    @app.get("/api/history")
    def history():
        with lock:
            runs = saved_runs(app.state.saver)
            latest = None
            if runs:
                run_id = UUID(runs[-1]["id"])
                latest = visible_result(snapshot_result(run_id, app.state.graph.get_state(config_for(run_id))))
                latest["created_at"] = runs[-1]["created_at"]
            states = {run["id"]: run["state"] for run in runs}
            jobs = []
            applied = app.state.applications.linked_job_ids(known_jobs())
            for job in vacancy_history(runs):
                if job["id"] in applied:
                    continue
                state = states[job["run_id"]]
                jobs.append(vacancy_presentation(job, state.get("preferences", {}), state.get("skills", [])))
            return {"jobs": jobs, "latest_run": latest}

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: UUID):
        with lock:
            return visible_result(snapshot_result(run_id, app.state.graph.get_state(config_for(run_id))))

    @app.post("/api/runs/{run_id}/review")
    def review(run_id: UUID, request: ReviewRequest):
        config = config_for(run_id)
        graph = app.state.graph
        with lock:
            snapshot = graph.get_state(config)
            result = snapshot_result(run_id, snapshot)
            selected = list(dict.fromkeys(request.selected))
            if result["status"] == "reviewed" and set(result["reviewed_ids"]) == set(selected):
                return visible_result(result)
            if result["status"] != "awaiting_review" or "review" not in snapshot.next:
                raise HTTPException(409, "Run has no pending review")
            if not set(selected).issubset({job["id"] for job in result["shortlist"]}):
                raise HTTPException(422, "Review contains vacancies outside this run")
            graph.invoke(Command(resume=selected), config=config)
            return visible_result(snapshot_result(run_id, graph.get_state(config)))

    frontend = Path(__file__).resolve().parents[1] / "frontend" / "dist"
    if frontend.is_dir():
        app.mount("/", StaticFiles(directory=frontend, html=True), name="frontend")
    return app


app = create_app()
