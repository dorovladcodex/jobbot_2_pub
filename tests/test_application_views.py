from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import UUID

from langgraph.checkpoint.sqlite import SqliteSaver

from jobbot.api import SearchRequest, create_app
from jobbot.applications import ApplicationStore
from jobbot.catalog import Vacancy
from jobbot.matching import KeywordMatcher
from jobbot.workflow import build_workflow


class ApplicationViewTests(unittest.TestCase):
    def test_automatic_matching_excludes_applied_jobs_from_both_views_and_future_searches(self):
        class Source:
            name = "test-source"
            vacancies = []

            def collect(self):
                return self.vacancies

        def posting(number, title):
            return Vacancy("Example", title, f"https://example.com/jobs/{number}",
                           "Germany", "remote", "Python", "test-source")

        def record(store, number, title):
            raw = {"message_id": f"mail000{number}", "thread_id": f"thread0{number}",
                   "received_at": "2026-11-16T10:00:00+00:00", "subject": "Application received",
                   "body": "We received your application."}
            event = {**raw, "company": "Example GmbH", "title": title, "job_url": None,
                     "status": "applied", "evidence_quote": raw["body"], "interview_at": None}
            store.save("candidate@example.com", {"messages": [raw], "next_page_token": ""},
                       [event], [], "2026-05-01")

        with TemporaryDirectory() as directory:
            path = Path(directory) / "test.sqlite"
            with closing(ApplicationStore(path)) as store, SqliteSaver.from_conn_string(str(path)) as saver:
                source = Source()
                app = create_app(path)
                app.state.saver = saver
                app.state.applications = store
                app.state.graph = build_workflow((source,), KeywordMatcher(), saver)
                app.state.search_sources = [{"id": source.name, "default_selected": True}]
                routes = {route.path: route.endpoint for route in app.routes if hasattr(route, "endpoint")}
                request = SearchRequest(cv="Python", skills=["Python"], source_ids=[source.name])

                source.vacancies = [posting(1, "Data Engineer (m/f/d)")]
                first = routes["/api/runs"](request)
                source.vacancies = [posting(2, "AI Engineer")]
                second = routes["/api/runs"](request)
                before = routes["/api/history"]()
                self.assertEqual(len(before["jobs"]), 1)
                self.assertEqual(len(before["latest_run"]["shortlist"]), 1)

                record(store, 1, "Data Engineer")
                record(store, 2, "AI Engineer")
                after = routes["/api/history"]()
                self.assertEqual(after["jobs"], [])
                self.assertEqual(after["latest_run"]["shortlist"], [])
                for run in (first, second):
                    self.assertEqual(routes["/api/runs/{run_id}"](UUID(run["id"]))["shortlist"], [])
                applications = routes["/api/applications"]()["applications"]
                self.assertTrue(all(item["vacancy"] for item in applications))

                # An application imported before discovery is matched in the search response itself.
                record(store, 3, "Platform Engineer")
                source.vacancies = [posting(3, "Platform Engineer")]
                third = routes["/api/runs"](request)
                self.assertEqual(third["shortlist"], [])
                self.assertEqual(third["status"], "empty")
                self.assertEqual(len(routes["/api/applications"]()["applications"]), 3)
