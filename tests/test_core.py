import unittest

from jobbot.catalog import DEMO_CV, DEMO_SKILLS, DemoSource, canonical_url, collect_catalog
from jobbot.matching import Evidence, KeywordMatcher, validate_evidence
from jobbot.pipeline import rank, shortlist_candidates
from jobbot.resume import extract_resume


class CoreTests(unittest.TestCase):
    def test_catalog_merges_tracking_variants_and_keeps_source_provenance(self):
        catalog, unavailable = collect_catalog((DemoSource(),))
        self.assertEqual(len(catalog), 4)
        self.assertEqual(len(catalog[0]["sources"]), 2)
        self.assertEqual(unavailable, [])
        self.assertEqual(canonical_url("https://example.com/careers?id=12&utm_campaign=test"),
                         "https://example.com/careers?id=12")

    def test_source_failure_preserves_other_results(self):
        class Failed:
            name = "offline"

            def collect(self):
                raise TimeoutError()

        catalog, unavailable = collect_catalog((Failed(), DemoSource()))
        self.assertEqual(len(catalog), 4)
        self.assertEqual(unavailable, ["offline"])

    def test_rank_and_filters(self):
        catalog, _ = collect_catalog((DemoSource(),))
        included, excluded = shortlist_candidates(catalog, "Germany", "remote")
        ranked = rank(included, DEMO_CV, DEMO_SKILLS, KeywordMatcher())
        self.assertEqual(ranked[0]["title"], "Applied AI Engineer")
        self.assertEqual(ranked[0]["score"], 67)
        self.assertEqual(excluded[0]["reasons"], ["work mode differs"])
        self.assertTrue(any("remote location restriction differs" in job["reasons"] for job in excluded))

    def test_live_title_variants_and_nrw_cities_match_saved_search(self):
        def job(title, location, mode):
            return {"id": title, "title": title, "location": location, "mode": mode, "description": ""}

        germany = "Sr. Forward Deployed AI Engineer (Remote Eligible in Germany)"
        uk = "Sr. Forward Deployed AI Engineer (Remote Eligible in the UK)"
        multi_country = "Senior AI Engineer (Remote in Germany, Ireland and Portugal)"
        hybrid = "Data Engineering Lead, Agentic Infrastructure"
        irrelevant = "Commercial Account Executive"
        vacancies = [
            job(germany, "Munich, DE", "unknown"),
            job(uk, "London, UK", "unknown"),
            job(multi_country, "Europe", "unknown"),
            job(hybrid, "Frankfurt/Düsseldorf", "hybrid"),
            job(irrelevant, "Dortmund", "hybrid"),
        ]
        included, excluded = shortlist_candidates(vacancies, "Germany", "any", {
            "target_roles": ["Data Engineer", "Applied AI Engineer", "Data Platform Engineer",
                             "Data Integration Engineer", "Oracle PL/SQL Developer", "Oracle Cloud Engineer"],
            "work_modes": ["remote", "hybrid"], "local_areas": ["NRW"], "seniority": "senior_plus",
        })
        self.assertEqual([(item["title"], item["mode"]) for item in included],
                         [(germany, "remote"), (multi_country, "remote"), (hybrid, "hybrid")])
        self.assertNotIn("location eligibility unverified", included[0]["unknown"])
        self.assertEqual({item["title"]: item["reasons"] for item in excluded}, {
            uk: ["remote location restriction differs"],
            irrelevant: ["title does not match target roles"],
        })

    def test_fabricated_evidence_is_rejected(self):
        result = validate_evidence([Evidence("Python", "invented", "Python required")],
                                   DEMO_CV, DEMO_SKILLS, "Python required")
        self.assertEqual(result, [])

    def test_invalid_model_evidence_falls_back_and_marks_provenance(self):
        class Invalid:
            name = "invalid matcher"

            def match(self, cv, skills, description):
                return [Evidence("Python", "invented", "Python required")]

        catalog, _ = collect_catalog((DemoSource(),))
        ranked = rank(catalog, DEMO_CV, DEMO_SKILLS, Invalid())
        self.assertEqual(ranked[0]["title"], "Applied AI Engineer")
        self.assertEqual(ranked[0]["scorer"], "keyword fallback (invalid evidence)")

    def test_overall_fit_is_independent_of_candidate_skill_breadth(self):
        class Assessor:
            name = "Codex CLI"

            def assess_many(self, cv, skills, vacancies, preferences):
                self.preferences = preferences
                return [{"id": job["id"], "fit_score": 8.7,
                         "analysis": "Strong pipeline fit; salary requires confirmation.",
                         "recommendation": "Confirm salary and apply.",
                         "evidence": [{"skill": "Python", "cv_quote": "Python APIs",
                                       "vacancy_quote": "Python services"}]} for job in vacancies]

        assessor = Assessor()
        vacancies = [{"id": "sample", "title": "Data Engineer", "description": "Build Python services.",
                      "unknown": []}]
        result = rank(vacancies, "Python APIs", ["Python", "SQL", "Oracle EBS"], assessor,
                      preferences={"base_location": "Germany"})[0]
        self.assertEqual(result["score"], 33)
        self.assertEqual(result["fit_score"], 8.7)
        self.assertEqual(result["recommendation"], "Confirm salary and apply.")
        self.assertEqual(assessor.preferences["base_location"], "Germany")

    def test_unsupported_model_fit_is_not_replaced_with_skill_percentage(self):
        class Assessor:
            name = "Codex CLI"

            def assess_many(self, cv, skills, vacancies, preferences):
                return [{"id": "sample", "fit_score": 9.8, "analysis": "Strong fit.",
                         "recommendation": "Apply.", "evidence": [
                             {"skill": "Python", "cv_quote": "invented", "vacancy_quote": "invented"},
                         ]}]

        result = rank([{"id": "sample", "description": "Python services.", "unknown": []}],
                      "Python APIs.", ["Python"], Assessor())[0]
        self.assertIsNone(result["fit_score"])
        self.assertEqual(result["score"], 100)

    def test_nrw_role_variants_and_foreign_remote_restriction(self):
        def job(id, title, location, mode):
            return {"id": id, "title": title, "location": location, "mode": mode, "description": ""}

        included, excluded = shortlist_candidates([
            job("nrw", "Senior - Data Engineering (m/w/d) Azure und Databricks", "Frechen, Germany", "hybrid"),
            job("foreign", "Senior Data Engineer", "United States", "remote"),
            job("local", "Data Engineer", "Berlin, Germany", "remote"),
        ], "Germany", "any", {"target_roles": ["Senior Data Engineer"],
                             "work_modes": ["remote", "hybrid"], "local_areas": ["NRW"]})
        self.assertEqual([item["id"] for item in included], ["nrw", "local"])
        self.assertNotIn("location eligibility unverified", included[1]["unknown"])
        self.assertEqual(excluded[0]["reasons"], ["remote location restriction differs"])

    def test_resume_intake_removes_contact_strings(self):
        text = extract_resume("resume.txt", b"Experience: Python\nContact: alex@example.com")
        self.assertIn("Python", text)
        self.assertNotIn("@", text)
        with self.assertRaises(ValueError):
            extract_resume("resume.jpg", b"bad format")
