from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from types import SimpleNamespace

import app as app_module


class PayloadTests(unittest.TestCase):
    def test_default_payload_omits_reasoning_and_adds_web_search(self):
        payload = app_module.build_response_payload(
            name="Reviewer",
            prompt="Prompt",
            deck_outline="1. intro",
            model="gpt-5.5",
            reasoning_effort="default",
            enable_web_search=True,
            search_context_size="medium",
        )

        self.assertEqual(payload["model"], "gpt-5.5")
        self.assertEqual(payload["reasoning"], {"summary": "detailed"})
        self.assertEqual(
            payload["tools"],
            [{"type": "web_search", "search_context_size": "medium"}],
        )
        self.assertIn("independently research", payload["input"])

    def test_high_effort_without_web_search(self):
        payload = app_module.build_response_payload(
            name="Reviewer",
            prompt="Prompt",
            deck_outline="1. intro",
            model="gpt-5.4",
            reasoning_effort="high",
            enable_web_search=False,
            search_context_size="low",
        )

        self.assertEqual(payload["reasoning"], {"effort": "high", "summary": "detailed"})
        self.assertNotIn("tools", payload)
        self.assertNotIn("independently research", payload["input"])


class SourceExtractionTests(unittest.TestCase):
    def test_extracts_unique_url_citations(self):
        response = SimpleNamespace(
            model_dump=lambda: {
                "output": [
                    {
                        "content": [
                            {
                                "annotations": [
                                    {
                                        "type": "url_citation",
                                        "url": "https://example.com/a",
                                        "title": "A",
                                    },
                                    {
                                        "type": "url_citation",
                                        "url": "https://example.com/a",
                                        "title": "Duplicate",
                                    },
                                ]
                            }
                        ]
                    }
                ]
            }
        )

        self.assertEqual(
            app_module.extract_sources(response),
            [{"url": "https://example.com/a", "title": "A"}],
        )

    def test_extracts_reasoning_summary(self):
        response = SimpleNamespace(
            model_dump=lambda: {
                "output": [
                    {
                        "type": "reasoning",
                        "summary": [
                            {
                                "type": "summary_text",
                                "text": "Checked market size assumptions.",
                            }
                        ],
                    }
                ]
            }
        )

        self.assertEqual(
            app_module.extract_reasoning_summary(response),
            "Checked market size assumptions.",
        )


class ArchiveTests(unittest.TestCase):
    def test_archive_run_saves_settings_and_draft(self):
        original_root = app_module.ARCHIVE_ROOT
        with TemporaryDirectory() as tmpdir:
            app_module.ARCHIVE_ROOT = Path(tmpdir)
            run_dir = app_module.create_archive_run(
                {
                    "model": "gpt-5.4-mini",
                    "reasoning_effort": "low",
                    "enable_web_search": True,
                    "search_context_size": "medium",
                },
                "1. intro",
            )

            self.assertTrue((run_dir / "00-settings.md").exists())
            self.assertTrue((run_dir / "01-submitted-draft.md").exists())
            self.assertIn("1. intro", (run_dir / "01-submitted-draft.md").read_text())
        app_module.ARCHIVE_ROOT = original_root

    def test_save_review_result_writes_markdown(self):
        with TemporaryDirectory() as tmpdir:
            path = app_module.save_review_result(
                Path(tmpdir),
                {
                    "name": "1. First-Principles VC Read",
                    "text": "Model response",
                    "reasoning_summary": "Reasoning summary",
                    "sources": [{"title": "Example", "url": "https://example.com"}],
                },
            )

            content = path.read_text()
            self.assertEqual(path.name, "1-first-principles-vc-read.md")
            self.assertIn("## Reasoning Summary", content)
            self.assertIn("Model response", content)
            self.assertIn("[Example](https://example.com)", content)


class RouteTests(unittest.TestCase):
    def test_home_renders_controls(self):
        client = app_module.app.test_client()
        response = client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'name="model"', response.data)
        self.assertIn(b'name="reasoning_effort"', response.data)
        self.assertIn(b'name="enable_web_search"', response.data)
        self.assertIn(b'id="results-grid"', response.data)

    def test_empty_outline_preserves_settings(self):
        client = app_module.app.test_client()
        response = client.post(
            "/review",
            data={
                "deck_outline": "",
                "model": "gpt-5.4",
                "reasoning_effort": "high",
                "search_context_size": "high",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Paste a deck outline first.", response.data)
        self.assertIn(b'value="gpt-5.4" selected', response.data)
        self.assertIn(b'value="high" selected', response.data)

    def test_review_stream_returns_ndjson(self):
        original_reviewers = app_module.REVIEWERS
        original_run_review = app_module.run_review
        original_root = app_module.ARCHIVE_ROOT
        app_module.REVIEWERS = [{"name": "Reviewer A", "prompt": "Prompt"}]
        app_module.run_review = lambda *args: {
            "name": "Reviewer A",
            "text": "Done",
            "reasoning_summary": "Summary",
            "sources": [],
        }

        with TemporaryDirectory() as tmpdir:
            app_module.ARCHIVE_ROOT = Path(tmpdir)
            try:
                client = app_module.app.test_client()
                response = client.post(
                    "/review-stream",
                    data={
                        "deck_outline": "1. intro",
                        "model": "gpt-5.4-mini",
                        "reasoning_effort": "low",
                        "enable_web_search": "on",
                        "search_context_size": "low",
                    },
                    buffered=True,
                )
            finally:
                app_module.REVIEWERS = original_reviewers
                app_module.run_review = original_run_review
                app_module.ARCHIVE_ROOT = original_root

            archive_runs = list(Path(tmpdir).iterdir())
            self.assertEqual(len(archive_runs), 1)
            self.assertTrue((archive_runs[0] / "01-submitted-draft.md").exists())
            self.assertTrue((archive_runs[0] / "reviewer-a.md").exists())

        if app_module.REVIEWERS is not original_reviewers:
            app_module.REVIEWERS = original_reviewers
        if app_module.run_review is not original_run_review:
            app_module.run_review = original_run_review
        app_module.ARCHIVE_ROOT = original_root

        self.assertEqual(response.status_code, 200)
        body = response.data.decode()
        self.assertIn('"type": "start"', body)
        self.assertIn('"type": "result"', body)
        self.assertIn('"type": "done"', body)
        self.assertIn('"reasoning_summary": "Summary"', body)


if __name__ == "__main__":
    unittest.main()
