from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from types import SimpleNamespace

import app as app_module
from prompts import ANGEL_REVIEWERS, VENTURE_REVIEWERS, reviewers_for_mode


class PayloadTests(unittest.TestCase):
    def test_default_background_reasoning_and_search_payload(self):
        payload = app_module.build_response_payload(
            name="Reviewer",
            prompt="Prompt",
            deck_outline="1. intro",
            model="gpt-5.5",
            reasoning_effort="low",
            enable_web_search=True,
            search_context_size="low",
        )

        self.assertEqual(payload["max_output_tokens"], app_module.REVIEW_MAX_OUTPUT_TOKENS)
        self.assertEqual(payload["reasoning"], {"effort": "low", "summary": "detailed"})
        self.assertEqual(payload["tools"], [{"type": "web_search", "search_context_size": "low"}])
        self.assertTrue(payload["background"])
        self.assertTrue(payload["store"])

    def test_no_reasoning_payload_omits_reasoning_and_adds_web_search(self):
        payload = app_module.build_response_payload(
            name="Reviewer",
            prompt="Prompt",
            deck_outline="1. intro",
            model="gpt-5.5",
            reasoning_effort="none",
            enable_web_search=True,
            search_context_size="medium",
        )

        self.assertEqual(payload["model"], "gpt-5.5")
        self.assertNotIn("reasoning", payload)
        self.assertEqual(
            payload["tools"],
            [{"type": "web_search", "search_context_size": "medium"}],
        )
        self.assertIn("independently research", payload["input"])

    def test_default_reasoning_requests_summary_without_effort(self):
        payload = app_module.build_response_payload(
            name="Reviewer",
            prompt="Prompt",
            deck_outline="1. intro",
            model="gpt-5.5",
            reasoning_effort="default",
            enable_web_search=False,
            search_context_size="medium",
        )

        self.assertEqual(payload["reasoning"], {"summary": "detailed"})

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


class InvestorModeTests(unittest.TestCase):
    def test_reviewers_for_mode_routes_to_selected_council(self):
        self.assertIs(reviewers_for_mode("venture"), VENTURE_REVIEWERS)
        self.assertIs(reviewers_for_mode("angel"), ANGEL_REVIEWERS)
        self.assertIs(reviewers_for_mode("unknown"), VENTURE_REVIEWERS)

    def test_form_settings_sanitizes_investor_mode(self):
        settings = app_module.form_settings(
            {
                "investor_mode": "angel",
                "model": "gpt-5.4-mini",
                "reasoning_effort": "low",
                "search_context_size": "low",
            }
        )

        self.assertEqual(settings["investor_mode"], "angel")

        settings = app_module.form_settings({"investor_mode": "bad"})
        self.assertEqual(settings["investor_mode"], "venture")


class BackgroundResponseTests(unittest.TestCase):
    def test_background_response_polls_until_completed(self):
        original_client = app_module.OPENAI_CLIENT
        original_poll_seconds = app_module.OPENAI_BACKGROUND_POLL_SECONDS

        class FakeResponses:
            def __init__(self):
                self.retrieve_calls = 0

            def create(self, **payload):
                self.payload = payload
                return SimpleNamespace(id="resp_1", status="queued", output_text="")

            def retrieve(self, response_id, **kwargs):
                self.retrieve_calls += 1
                if self.retrieve_calls == 1:
                    return SimpleNamespace(id=response_id, status="in_progress", output_text="")
                return SimpleNamespace(id=response_id, status="completed", output_text="Done")

        fake_responses = FakeResponses()
        app_module.OPENAI_CLIENT = SimpleNamespace(responses=fake_responses)
        app_module.OPENAI_BACKGROUND_POLL_SECONDS = 0
        events = []

        try:
            response = app_module.create_response_with_retries(
                {
                    "model": "gpt-5.4-mini",
                    "input": "Prompt",
                    "background": True,
                    "store": True,
                },
                reviewer_name="Reviewer A",
                run_id="run-1",
                progress_callback=events.append,
            )
        finally:
            app_module.OPENAI_CLIENT = original_client
            app_module.OPENAI_BACKGROUND_POLL_SECONDS = original_poll_seconds

        self.assertEqual(response.status, "completed")
        self.assertEqual(fake_responses.retrieve_calls, 2)
        self.assertIn("submitted_openai", [event["status"] for event in events])
        self.assertIn("polling", [event["status"] for event in events])
        self.assertIn("completed_openai", [event["status"] for event in events])

    def test_background_response_raises_on_terminal_status(self):
        original_client = app_module.OPENAI_CLIENT
        original_poll_seconds = app_module.OPENAI_BACKGROUND_POLL_SECONDS

        class FakeResponses:
            def create(self, **payload):
                return SimpleNamespace(id="resp_1", status="queued", output_text="")

            def retrieve(self, response_id, **kwargs):
                return SimpleNamespace(
                    id=response_id,
                    status="incomplete",
                    output_text="",
                    incomplete_details=SimpleNamespace(model_dump=lambda: {"reason": "max_output_tokens"}),
                )

        app_module.OPENAI_CLIENT = SimpleNamespace(responses=FakeResponses())
        app_module.OPENAI_BACKGROUND_POLL_SECONDS = 0

        try:
            with self.assertRaises(app_module.OpenAIResponseTerminalError):
                app_module.create_response_with_retries(
                    {
                        "model": "gpt-5.4-mini",
                        "input": "Prompt",
                        "background": True,
                        "store": True,
                    },
                    reviewer_name="Reviewer A",
                    run_id="run-1",
                )
        finally:
            app_module.OPENAI_CLIENT = original_client
            app_module.OPENAI_BACKGROUND_POLL_SECONDS = original_poll_seconds


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
                    "investor_mode": "venture",
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

    def test_load_archive_run_reads_draft_settings_and_results(self):
        original_root = app_module.ARCHIVE_ROOT
        with TemporaryDirectory() as tmpdir:
            app_module.ARCHIVE_ROOT = Path(tmpdir)
            try:
                run_dir = app_module.create_archive_run(
                    {
                        "investor_mode": "angel",
                        "model": "gpt-5.4-mini",
                        "reasoning_effort": "none",
                        "enable_web_search": False,
                        "search_context_size": "low",
                    },
                    "1. intro\n2. problem",
                )
                app_module.save_review_result(
                    run_dir,
                    {
                        "name": "1. First-Principles VC Read",
                        "text": "Model response",
                        "reasoning_summary": "Reasoning summary",
                        "sources": [{"title": "Example", "url": "https://example.com"}],
                    },
                )

                archive = app_module.load_archive_run(run_dir.name)
            finally:
                app_module.ARCHIVE_ROOT = original_root

        self.assertEqual(archive["deck_outline"], "1. intro\n2. problem")
        self.assertEqual(archive["settings"]["investor_mode"], "angel")
        self.assertEqual(archive["settings"]["model"], "gpt-5.4-mini")
        self.assertFalse(archive["settings"]["enable_web_search"])
        self.assertEqual(archive["results"][0]["name"], "1. First-Principles VC Read")
        self.assertEqual(archive["results"][0]["text"], "Model response")
        self.assertEqual(archive["results"][0]["reasoning_summary"], "Reasoning summary")
        self.assertEqual(archive["results"][0]["sources"], [{"title": "Example", "url": "https://example.com"}])


class RouteTests(unittest.TestCase):
    def test_home_renders_controls(self):
        original_root = app_module.ARCHIVE_ROOT
        with TemporaryDirectory() as tmpdir:
            app_module.ARCHIVE_ROOT = Path(tmpdir)
            try:
                (Path(tmpdir) / "20260430-120000").mkdir()
                client = app_module.app.test_client()
                response = client.get("/")
            finally:
                app_module.ARCHIVE_ROOT = original_root

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'name="model"', response.data)
        self.assertIn(b'name="investor_mode"', response.data)
        self.assertIn(b'name="reasoning_effort"', response.data)
        self.assertIn(b'name="enable_web_search"', response.data)
        self.assertIn(b'id="results-grid"', response.data)
        self.assertIn(b'id="archive-select"', response.data)
        self.assertIn(b"20260430-120000", response.data)

    def test_archive_route_returns_json(self):
        original_root = app_module.ARCHIVE_ROOT
        with TemporaryDirectory() as tmpdir:
            app_module.ARCHIVE_ROOT = Path(tmpdir)
            try:
                run_dir = app_module.create_archive_run(
                    {
                        "investor_mode": "venture",
                        "model": "gpt-5.4-mini",
                        "reasoning_effort": "none",
                        "enable_web_search": False,
                        "search_context_size": "low",
                    },
                    "1. intro",
                )
                app_module.save_review_result(
                    run_dir,
                    {
                        "name": "Reviewer A",
                        "text": "Done",
                        "reasoning_summary": "",
                        "sources": [],
                    },
                )
                client = app_module.app.test_client()
                response = client.get(f"/archive/{run_dir.name}")
            finally:
                app_module.ARCHIVE_ROOT = original_root

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["deck_outline"], "1. intro")
        self.assertEqual(data["results"][0]["name"], "Reviewer A")

    def test_empty_outline_preserves_settings(self):
        client = app_module.app.test_client()
        response = client.post(
            "/review",
            data={
                "deck_outline": "",
                "investor_mode": "angel",
                "model": "gpt-5.4",
                "reasoning_effort": "high",
                "search_context_size": "high",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Paste a deck outline first.", response.data)
        self.assertIn(b'value="angel" selected', response.data)
        self.assertIn(b'value="gpt-5.4" selected', response.data)
        self.assertIn(b'value="high" selected', response.data)

    def test_review_stream_returns_ndjson(self):
        original_reviewers_for_mode = app_module.reviewers_for_mode
        original_run_review = app_module.run_review
        original_root = app_module.ARCHIVE_ROOT
        app_module.reviewers_for_mode = lambda mode: [{"name": "Reviewer A", "prompt": "Prompt"}]
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
                        "investor_mode": "angel",
                        "model": "gpt-5.4-mini",
                        "reasoning_effort": "low",
                        "enable_web_search": "on",
                        "search_context_size": "low",
                    },
                    buffered=True,
                )
            finally:
                app_module.reviewers_for_mode = original_reviewers_for_mode
                app_module.run_review = original_run_review
                app_module.ARCHIVE_ROOT = original_root

            archive_runs = list(Path(tmpdir).iterdir())
            self.assertEqual(len(archive_runs), 1)
            self.assertTrue((archive_runs[0] / "01-submitted-draft.md").exists())
            self.assertTrue((archive_runs[0] / "reviewer-a.md").exists())

        if app_module.reviewers_for_mode is not original_reviewers_for_mode:
            app_module.reviewers_for_mode = original_reviewers_for_mode
        if app_module.run_review is not original_run_review:
            app_module.run_review = original_run_review
        app_module.ARCHIVE_ROOT = original_root

        self.assertEqual(response.status_code, 200)
        body = response.data.decode()
        self.assertIn('"type": "start"', body)
        self.assertIn('"type": "result"', body)
        self.assertIn('"type": "done"', body)
        self.assertIn('"reasoning_summary": "Summary"', body)

    def test_review_stream_uses_selected_investor_mode(self):
        original_run_review = app_module.run_review
        original_root = app_module.ARCHIVE_ROOT
        calls = []

        def fake_run_review(name, *args):
            calls.append(name)
            return {
                "name": name,
                "text": "Done",
                "reasoning_summary": "",
                "sources": [],
            }

        app_module.run_review = fake_run_review
        with TemporaryDirectory() as tmpdir:
            app_module.ARCHIVE_ROOT = Path(tmpdir)
            try:
                client = app_module.app.test_client()
                response = client.post(
                    "/review-stream",
                    data={
                        "deck_outline": "1. intro",
                        "investor_mode": "angel",
                        "model": "gpt-5.4-mini",
                        "reasoning_effort": "low",
                        "enable_web_search": "on",
                        "search_context_size": "low",
                    },
                    buffered=True,
                )
            finally:
                app_module.run_review = original_run_review
                app_module.ARCHIVE_ROOT = original_root

        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, [reviewer["name"] for reviewer in ANGEL_REVIEWERS])


if __name__ == "__main__":
    unittest.main()
