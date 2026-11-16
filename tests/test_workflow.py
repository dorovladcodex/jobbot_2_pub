import os
from pathlib import Path
from subprocess import TimeoutExpired
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from jobbot.api import create_app
from jobbot.catalog import DEMO_CV, DEMO_SKILLS, Vacancy
from jobbot.matching import KeywordMatcher
from jobbot.web_search import CodexSearchPass
from jobbot.workflow import build_workflow


class WorkflowTests(unittest.TestCase):
    def test_web_pass_rejects_search_pages_and_links_outside_its_group(self):
        source = CodexSearchPass("codex:indeed", "Indeed", ("indeed.com",))
        item = {"title": "Senior Data Engineer", "company": "Example", "location": "Germany",
                "mode": "remote", "excerpt": "Build Python data pipelines."}
        urls = ["https://www.indeed.com/jobs?q=data", "https://www.indeed.com/viewjob",
                "https://other.example/jobs/123", "https://de.indeed.com/viewjob?jk=123&utm_source=search"]
        with patch("jobbot.web_search.run_structured", return_value={
            "vacancies": [{**item, "url": url} for url in urls],
        }):
            result = source.collect({"target_roles": ["Data Engineer"]})
        self.assertEqual([vacancy.url for vacancy in result], ["https://de.indeed.com/viewjob?jk=123"])

    def test_parallel_search_passes_merge_duplicate_and_preserve_success_on_timeout(self):
        barrier = Barrier(2, timeout=5)
        sources = (
            CodexSearchPass("codex:careers", "Company career pages"),
            CodexSearchPass("codex:remote", "Remote job boards"),
            CodexSearchPass("codex:failed", "Timed out job boards"),
            CodexSearchPass("codex:unselected", "Unselected"),
        )

        def search(prompt, schema, *, timeout, web_search):
            self.assertTrue(web_search)
            self.assertNotIn("PRIVATE CV", prompt)
            self.assertIn('"target_roles": ["Data Engineer"]', prompt)
            if "Timed out job boards" in prompt:
                raise TimeoutExpired("codex", timeout)
            self.assertNotIn("Unselected", prompt)
            barrier.wait()
            suffix = "?utm_source=search" if "Remote job boards" in prompt else ""
            return {"vacancies": [{"title": "Senior Data Engineer", "company": "Example",
                                    "url": "https://example.com/jobs/123" + suffix, "location": "Germany",
                                    "mode": "remote", "excerpt": "Build Python data pipelines."}]}

        graph = build_workflow(sources, KeywordMatcher(), InMemorySaver())
        with patch("jobbot.web_search.run_structured", side_effect=search) as model:
            result = graph.invoke({
                "cv": "PRIVATE CV: Python.", "skills": ["Python"], "location": "Germany", "mode": "remote",
                "preferences": {"target_roles": ["Data Engineer"]},
                "source_ids": [source.name for source in sources[:3]],
            }, {"configurable": {"thread_id": "live-test"}, "max_concurrency": 3})
        self.assertEqual(model.call_count, 3)
        self.assertEqual(result["catalog_count"], 1)
        self.assertEqual(result["shortlist"][0]["sources"], ["codex:careers", "codex:remote"])
        self.assertEqual(result["unavailable_sources"], ["codex:failed"])
        self.assertEqual([source["status"] for source in result["source_results"]], ["ok", "ok", "unavailable"])

    def test_search_only_collects_selected_configured_sources(self):
        def collect(board):
            if board.kind == "lever":
                raise TimeoutError()
            return [Vacancy("Example", "Data Engineer", "https://example.com/job",
                            "Germany", "remote", "Build Python APIs.", board.name)]

        with TemporaryDirectory() as directory, patch.dict(os.environ, {
            "JOBBOT_MATCHER": "baseline", "JOBBOT_SEARCH_MODE": "live",
            "JOBBOT_ATS_BOARDS": "greenhouse:example,lever:other",
        }), patch("jobbot.api.ATSBoard.collect", autospec=True, side_effect=collect) as adapter, \
                TestClient(create_app(Path(directory) / "test.sqlite")) as client:
            sources = client.get("/api/demo").json()["search_sources"]
            source_ids = [source["id"] for source in sources if source["kind"] == "ats"]
            self.assertEqual(source_ids, ["greenhouse:example", "lever:other"])
            self.assertTrue(any(source["kind"] == "web" and source["default_selected"] for source in sources))
            self.assertFalse(any(source["kind"] == "ats" and source["default_selected"] for source in sources))
            request = {"cv": DEMO_CV, "skills": DEMO_SKILLS}
            for invalid_ids in ([], ["unknown"]):
                response = client.post("/api/runs", json={**request, "source_ids": invalid_ids})
                self.assertEqual(response.status_code, 422)
            adapter.assert_not_called()

            response = client.post("/api/runs", json={**request, "source_ids": [source_ids[0]]})
            self.assertEqual(response.status_code, 201, response.text)
            self.assertEqual(response.json()["shortlist"][0]["sources"], [source_ids[0]])
            self.assertEqual(response.json()["unavailable_sources"], [])
            adapter.assert_called_once()
            self.assertEqual(adapter.call_args.args[0].name, source_ids[0])

            adapter.reset_mock()
            response = client.post("/api/runs", json={**request, "source_ids": [source_ids[1]]})
            self.assertEqual(response.status_code, 201, response.text)
            self.assertEqual(response.json()["status"], "sources_unavailable")
            self.assertEqual(response.json()["unavailable_sources"], [source_ids[1]])
            adapter.assert_called_once()
            self.assertEqual(adapter.call_args.args[0].name, source_ids[1])

    def test_uploaded_resume_populates_skills_and_search_uses_preferences(self):
        with TemporaryDirectory() as directory, patch.dict(os.environ, {
            "JOBBOT_MATCHER": "baseline", "JOBBOT_PROFILE": "baseline", "JOBBOT_SEARCH_MODE": "demo",
            "JOBBOT_ATS_BOARDS": "",
        }), TestClient(create_app(Path(directory) / "test.sqlite")) as client:
            uploaded = client.post("/api/resume", files={
                "file": ("cv.txt", b"Experience with Python, FastAPI and ODI.\ncv@example.com", "text/plain"),
            })
            self.assertEqual(uploaded.status_code, 200, uploaded.text)
            profile = uploaded.json()
            self.assertEqual(profile["skills"], ["Python", "FastAPI", "Oracle Data Integrator"])
            self.assertNotIn("cv@example.com", profile["cv"])

            response = client.post("/api/runs", json={
                **profile, "location": "Germany", "local_areas": ["Berlin"], "work_modes": ["remote", "hybrid"],
                "language": "english", "max_german_level": "B1", "contract_type": "permanent",
                "min_salary_eur": 85000, "target_roles": ["Applied AI Engineer", "AI Platform Engineer",
                                                         "Data Platform Engineer"],
            })
            self.assertEqual(response.status_code, 201, response.text)
            run = response.json()
            self.assertEqual([job["title"] for job in run["shortlist"]],
                             ["Applied AI Engineer", "Data Platform Engineer"])
            self.assertEqual(run["shortlist"][0]["unknown"], [])
            self.assertIn("annual EUR salary unverified", run["shortlist"][1]["unknown"])
            excluded = {job["title"]: job["reasons"] for job in run["excluded"]}
            self.assertIn("German requirement exceeds your level", excluded["AI Platform Engineer"])
            self.assertIn("contract type differs", excluded["AI Platform Engineer"])

    def test_resume_profile_calls_codex_once_and_filters_unsupported_suggestions(self):
        with TemporaryDirectory() as directory, patch.dict(os.environ, {
            "JOBBOT_PROFILE": "codex", "JOBBOT_MATCHER": "baseline", "JOBBOT_SEARCH_MODE": "demo",
            "JOBBOT_ATS_BOARDS": "",
        }), patch("jobbot.resume.run_structured", return_value={
            "skills": [{"name": "Python", "cv_quote": "Built Python data pipelines."}],
            "target_roles": [
                {"name": "Data Engineer", "cv_quote": "Built Python data pipelines."},
                {"name": "Security Engineer", "cv_quote": "Designed a SIEM."},
            ],
        }) as model, TestClient(create_app(Path(directory) / "test.sqlite")) as client:
            response = client.post("/api/resume", files={
                "file": ("cv.txt", b"Built Python data pipelines.\ncv@example.com", "text/plain"),
            })
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["skills"], ["Python"])
            self.assertEqual(response.json()["target_roles"], ["Data Engineer"])
            self.assertEqual(response.json()["method"], "codex")
            model.assert_called_once()
            self.assertNotIn("cv@example.com", model.call_args.args[0])

    def test_interrupt_survives_restart_and_resume_is_idempotent(self):
        with TemporaryDirectory() as directory, patch.dict(os.environ, {
            "JOBBOT_MATCHER": "baseline", "JOBBOT_SEARCH_MODE": "demo", "JOBBOT_ATS_BOARDS": "",
        }):
            db = Path(directory) / "test.sqlite"
            with TestClient(create_app(db)) as client:
                response = client.post("/api/runs", json={
                    "cv": DEMO_CV, "skills": DEMO_SKILLS, "mode": "remote", "location": "Germany",
                })
                self.assertEqual(response.status_code, 201, response.text)
                run = response.json()
                self.assertEqual(run["status"], "awaiting_review")
            with TestClient(create_app(db)) as client:
                route = "/api/runs/" + run["id"]
                self.assertEqual(client.get(route).json()["status"], "awaiting_review")
                self.assertEqual(client.post(route + "/review", json={"selected": ["invalid"]}).status_code, 422)
                choice = [run["shortlist"][0]["id"]]
                response = client.post(route + "/review", json={"selected": choice})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["reviewed_ids"], choice)
                self.assertEqual(response.json()["status"], "reviewed")
                self.assertEqual(client.post(route + "/review", json={"selected": choice}).json(), response.json())
