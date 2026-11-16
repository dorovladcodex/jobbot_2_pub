import unittest

from jobbot.presentation import vacancy_presentation


class PresentationTests(unittest.TestCase):
    def test_compact_facts_use_source_quotes_and_keep_decimal_score(self):
        job = {
            "score": 33, "fit_score": 8.4, "description": "Build Python pipelines.",
            "salary_quote": "Base salary: EUR 90,000–110,000 per year",
            "language_quote": "Working language: English. German B1 required.",
            "contract_quote": "Permanent contract.",
            "evidence": [{"skill": "Python", "cv_quote": "Python pipelines", "vacancy_quote": "Python pipelines"}],
        }
        presented = vacancy_presentation(job, {"max_german_level": "B1"}, ["Python", "SQL"])
        self.assertEqual(presented["fit_score"], 8.4)
        self.assertEqual(presented["salary"], "EUR 90,000–110,000 per year")
        self.assertEqual(presented["language"], "English · German B1")
        self.assertEqual(presented["language_risk"], "medium")
        self.assertEqual(presented["contract_type"], "Permanent")
        self.assertEqual(presented["not_evidenced_skills"], ["SQL"])
        self.assertEqual(job["score"], 33)

    def test_old_vacancies_derive_facts_and_keep_unknowns_explicit(self):
        unknown = vacancy_presentation({
            "score": 30, "description": "Build data pipelines. Revenue EUR 10,000,000.", "evidence": [],
        }, {}, [])
        self.assertIsNone(unknown["salary"])
        self.assertIsNone(unknown["language"])
        self.assertIsNone(unknown["language_risk"])
        self.assertIsNone(unknown["contract_type"])
        self.assertIsNone(unknown["fit_score"])
        high = vacancy_presentation({
            "score": 30, "description": "German C1 required. Salary 85,000–95,000 EUR per year.",
        }, {"max_german_level": "B1"}, [])
        self.assertEqual(high["language_risk"], "high")
        self.assertEqual(high["salary"], "85,000–95,000 EUR per year")

    def test_optional_german_does_not_raise_risk_for_english_role(self):
        result = vacancy_presentation({
            "score": 67, "description": "Fluency in English is required. German C1 optional.",
        }, {"max_german_level": "B1"}, [])
        self.assertEqual(result["language"], "English · German optional")
        self.assertEqual(result["language_risk"], "low")

    def test_saved_run_language_and_contract_wording_is_recognized(self):
        result = vacancy_presentation({
            "score": 13, "description": "Python and SQL.",
            "language_quote": "Excellent communication skills in English and the ability to collaborate.",
            "contract_quote": "fulltime permanent",
        }, {"max_german_level": "B1"}, [])
        self.assertEqual(result["language"], "English")
        self.assertEqual(result["language_risk"], "low")
        self.assertEqual(result["contract_type"], "Permanent")
