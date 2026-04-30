import json
import logging
import os
import random
import re
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv
from flask import Flask, Response, abort, jsonify, render_template, request, stream_with_context
from openai import APIConnectionError, APIStatusError, APITimeoutError, DefaultHttpxClient, OpenAI, RateLimitError

from prompts import REVIEWERS


load_dotenv()

app = Flask(__name__)


def int_from_env(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if not raw_value:
        return default
    try:
        value = int(raw_value)
    except ValueError:
        return default
    return value if value > 0 else default


def log_level_from_env() -> int:
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    return getattr(logging, level_name, logging.INFO)

MODEL_OPTIONS = [
    {"value": "gpt-5.5", "label": "GPT-5.5"},
    {"value": "gpt-5.4", "label": "GPT-5.4"},
    {"value": "gpt-5.4-mini", "label": "GPT-5.4 Mini"},
    {"value": "gpt-5.2", "label": "GPT-5.2"},
]
EFFORT_OPTIONS = [
    {"value": "none", "label": "Off"},
    {"value": "default", "label": "Default"},
    {"value": "minimal", "label": "Minimal"},
    {"value": "low", "label": "Low"},
    {"value": "medium", "label": "Medium"},
    {"value": "high", "label": "High"},
    {"value": "xhigh", "label": "Extra high"},
]
SEARCH_CONTEXT_OPTIONS = [
    {"value": "low", "label": "Low"},
    {"value": "medium", "label": "Medium"},
    {"value": "high", "label": "High"},
]

DEFAULT_MODEL = "gpt-5.5"
DEFAULT_EFFORT = "none"
DEFAULT_SEARCH_CONTEXT_SIZE = "medium"
REVIEW_TIMEOUT_SECONDS = int_from_env("OPENAI_READ_TIMEOUT_SECONDS", 900)
OPENAI_CONNECT_TIMEOUT_SECONDS = int_from_env("OPENAI_CONNECT_TIMEOUT_SECONDS", 20)
OPENAI_WRITE_TIMEOUT_SECONDS = int_from_env("OPENAI_WRITE_TIMEOUT_SECONDS", 60)
OPENAI_POOL_TIMEOUT_SECONDS = int_from_env("OPENAI_POOL_TIMEOUT_SECONDS", 60)
OPENAI_APP_MAX_RETRIES = int_from_env("OPENAI_APP_MAX_RETRIES", int_from_env("OPENAI_MAX_RETRIES", 2))
OPENAI_SDK_MAX_RETRIES = int_from_env("OPENAI_SDK_MAX_RETRIES", 0)
REVIEW_MAX_WORKERS = int_from_env("REVIEW_MAX_WORKERS", 3)
REVIEW_MAX_OUTPUT_TOKENS = int_from_env("REVIEW_MAX_OUTPUT_TOKENS", 3500)
LOG_PAYLOAD_PREVIEW_CHARS = int_from_env("LOG_PAYLOAD_PREVIEW_CHARS", 0)
ARCHIVE_ROOT = Path("archives")
WEB_RESEARCH_INSTRUCTIONS = """
When web search is enabled, independently research investor-relevant market,
competitor, procurement, pricing, category, and timing context. Cite sources
where available. Separate facts found externally from assumptions and inferences.
"""

OPENAI_TIMEOUT = httpx.Timeout(
    connect=OPENAI_CONNECT_TIMEOUT_SECONDS,
    read=REVIEW_TIMEOUT_SECONDS,
    write=OPENAI_WRITE_TIMEOUT_SECONDS,
    pool=OPENAI_POOL_TIMEOUT_SECONDS,
)
OPENAI_HTTP_CLIENT = DefaultHttpxClient(
    timeout=OPENAI_TIMEOUT,
    limits=httpx.Limits(
        max_connections=max(REVIEW_MAX_WORKERS * 2, 6),
        max_keepalive_connections=max(REVIEW_MAX_WORKERS, 3),
        keepalive_expiry=30,
    ),
)
OPENAI_CLIENT = OpenAI(
    timeout=OPENAI_TIMEOUT,
    max_retries=OPENAI_SDK_MAX_RETRIES,
    http_client=OPENAI_HTTP_CLIENT,
)

logging.basicConfig(
    level=log_level_from_env(),
    format="%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s",
)
app.logger.setLevel(log_level_from_env())


def option_values(options: list[dict[str, str]]) -> set[str]:
    return {option["value"] for option in options}


def sanitize_choice(value: str | None, allowed: set[str], default: str) -> str:
    if value in allowed:
        return value
    return default


def form_settings(form) -> dict[str, str | bool]:
    return {
        "model": sanitize_choice(
            form.get("model"),
            option_values(MODEL_OPTIONS),
            DEFAULT_MODEL,
        ),
        "reasoning_effort": sanitize_choice(
            form.get("reasoning_effort"),
            option_values(EFFORT_OPTIONS),
            DEFAULT_EFFORT,
        ),
        "enable_web_search": form.get("enable_web_search") == "on",
        "search_context_size": sanitize_choice(
            form.get("search_context_size"),
            option_values(SEARCH_CONTEXT_OPTIONS),
            DEFAULT_SEARCH_CONTEXT_SIZE,
        ),
    }


def template_context(**overrides):
    context = {
        "model_options": MODEL_OPTIONS,
        "effort_options": EFFORT_OPTIONS,
        "search_context_options": SEARCH_CONTEXT_OPTIONS,
        "archive_runs": list_archive_runs(),
        "settings": {
            "model": DEFAULT_MODEL,
            "reasoning_effort": DEFAULT_EFFORT,
            "enable_web_search": False,
            "search_context_size": DEFAULT_SEARCH_CONTEXT_SIZE,
        },
    }
    context.update(overrides)
    return context


def build_response_payload(
    name: str,
    prompt: str,
    deck_outline: str,
    model: str,
    reasoning_effort: str,
    enable_web_search: bool,
    search_context_size: str,
) -> dict:
    instructions = prompt.strip()
    if enable_web_search:
        instructions = f"{instructions}\n\n{WEB_RESEARCH_INSTRUCTIONS.strip()}"

    payload = {
        "model": model,
        "input": f"{instructions}\n\nDeck outline:\n{deck_outline}",
        "max_output_tokens": REVIEW_MAX_OUTPUT_TOKENS,
    }

    if reasoning_effort == "none":
        pass
    elif reasoning_effort != "default":
        payload["reasoning"] = {"effort": reasoning_effort, "summary": "detailed"}
    else:
        payload["reasoning"] = {"summary": "detailed"}

    if enable_web_search:
        payload["tools"] = [
            {
                "type": "web_search",
                "search_context_size": search_context_size,
            }
        ]
        payload["include"] = ["web_search_call.action.sources"]

    return payload


def reviewer_worker_count() -> int:
    return min(len(REVIEWERS), REVIEW_MAX_WORKERS)


def payload_log_summary(payload: dict) -> dict:
    summary = {
        "model": payload.get("model"),
        "input_chars": len(payload.get("input") or ""),
        "max_output_tokens": payload.get("max_output_tokens"),
        "reasoning": payload.get("reasoning"),
        "tools": [tool.get("type") for tool in payload.get("tools", [])],
        "include": payload.get("include", []),
    }
    if LOG_PAYLOAD_PREVIEW_CHARS:
        summary["input_preview"] = (payload.get("input") or "")[:LOG_PAYLOAD_PREVIEW_CHARS]
    return summary


def response_log_summary(response) -> dict:
    usage = getattr(response, "usage", None)
    if hasattr(usage, "model_dump"):
        usage = usage.model_dump()
    return {
        "id": getattr(response, "id", None),
        "status": getattr(response, "status", None),
        "output_chars": len(getattr(response, "output_text", "") or ""),
        "usage": usage,
    }


def exception_log_summary(exc: Exception) -> dict:
    cause = exc.__cause__
    return {
        "error_type": type(exc).__name__,
        "error": str(exc),
        "cause_type": type(cause).__name__ if cause else None,
        "cause": str(cause) if cause else None,
    }


def create_response_with_retries(payload: dict, reviewer_name: str = "unknown", run_id: str = "unknown"):
    request_started = time.monotonic()
    for attempt in range(1, OPENAI_APP_MAX_RETRIES + 1):
        attempt_started = time.monotonic()
        app.logger.info(
            "openai_attempt_start run=%s reviewer=%r attempt=%s/%s payload=%s",
            run_id,
            reviewer_name,
            attempt,
            OPENAI_APP_MAX_RETRIES,
            payload_log_summary(payload),
        )
        try:
            response = OPENAI_CLIENT.responses.create(**payload)
            app.logger.info(
                "openai_attempt_success run=%s reviewer=%r attempt=%s elapsed=%.2fs total_elapsed=%.2fs response=%s",
                run_id,
                reviewer_name,
                attempt,
                time.monotonic() - attempt_started,
                time.monotonic() - request_started,
                response_log_summary(response),
            )
            return response
        except (APIConnectionError, APITimeoutError) as exc:
            app.logger.warning(
                "openai_attempt_connection_error run=%s reviewer=%r attempt=%s/%s elapsed=%.2fs error=%s",
                run_id,
                reviewer_name,
                attempt,
                OPENAI_APP_MAX_RETRIES,
                time.monotonic() - attempt_started,
                exception_log_summary(exc),
            )
            if attempt == OPENAI_APP_MAX_RETRIES:
                raise
            delay = min(2**attempt, 20) + random.uniform(0, 0.5)
            app.logger.warning(
                "openai_attempt_retry_sleep run=%s reviewer=%r delay=%.1fs next_attempt=%s/%s",
                run_id,
                reviewer_name,
                delay,
                attempt + 1,
                OPENAI_APP_MAX_RETRIES,
            )
            time.sleep(delay)
        except RateLimitError:
            app.logger.exception(
                "openai_rate_limit_no_retry run=%s reviewer=%r attempt=%s elapsed=%.2fs",
                run_id,
                reviewer_name,
                attempt,
                time.monotonic() - attempt_started,
            )
            raise
        except APIStatusError:
            app.logger.exception(
                "openai_status_error_no_retry run=%s reviewer=%r attempt=%s elapsed=%.2fs",
                run_id,
                reviewer_name,
                attempt,
                time.monotonic() - attempt_started,
            )
            raise


def as_plain_data(value):
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        return value
    return None


def extract_sources(response) -> list[dict[str, str]]:
    data = as_plain_data(response)
    sources = []
    seen = set()

    def visit(value):
        plain = as_plain_data(value)
        if isinstance(plain, dict):
            if plain.get("type") == "url_citation" and plain.get("url"):
                url = plain["url"]
                if url not in seen:
                    seen.add(url)
                    sources.append(
                        {
                            "url": url,
                            "title": plain.get("title") or url,
                        }
                    )
            for child in plain.values():
                visit(child)
        elif isinstance(plain, list):
            for child in plain:
                visit(child)

    visit(data)
    return sources


def extract_reasoning_summary(response) -> str:
    data = as_plain_data(response)
    summaries = []

    def text_from_summary(summary_item):
        plain = as_plain_data(summary_item)
        if isinstance(plain, dict):
            return plain.get("text") or plain.get("summary_text")
        if isinstance(summary_item, str):
            return summary_item
        return None

    def visit(value):
        plain = as_plain_data(value)
        if isinstance(plain, dict):
            if plain.get("type") == "reasoning":
                for item in plain.get("summary") or []:
                    text = text_from_summary(item)
                    if text:
                        summaries.append(text)
            for child in plain.values():
                visit(child)
        elif isinstance(plain, list):
            for child in plain:
                visit(child)

    visit(data)
    return "\n\n".join(summaries)


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "review"


def list_archive_runs() -> list[str]:
    if not ARCHIVE_ROOT.exists():
        return []
    return sorted(
        [
            path.name
            for path in ARCHIVE_ROOT.iterdir()
            if path.is_dir() and re.fullmatch(r"\d{8}-\d{6}(?:-\d{2})?", path.name)
        ],
        reverse=True,
    )


def archive_run_path(run_id: str) -> Path:
    if not re.fullmatch(r"\d{8}-\d{6}(?:-\d{2})?", run_id):
        abort(404)
    path = ARCHIVE_ROOT / run_id
    if not path.is_dir():
        abort(404)
    return path


def extract_fenced_text(markdown: str) -> str:
    match = re.search(r"```(?:text)?\n(.*?)\n```", markdown, flags=re.DOTALL)
    if match:
        return match.group(1)
    return markdown


def markdown_section(markdown: str, heading: str, next_headings: list[str] | None = None) -> str:
    next_headings = next_headings or []
    start = markdown.find(heading)
    if start == -1:
        return ""
    start += len(heading)
    end_candidates = [markdown.find(next_heading, start) for next_heading in next_headings]
    end_candidates = [candidate for candidate in end_candidates if candidate != -1]
    end = min(end_candidates) if end_candidates else len(markdown)
    return markdown[start:end].strip()


def parse_sources(markdown: str) -> list[dict[str, str]]:
    sources_markdown = markdown_section(markdown, "## Sources")
    sources = []
    for title, url in re.findall(r"^- \[(.*?)\]\((.*?)\)$", sources_markdown, flags=re.MULTILINE):
        sources.append({"title": title, "url": url})
    return sources


def parse_review_markdown(path: Path) -> dict:
    markdown = path.read_text(encoding="utf-8")
    title_match = re.search(r"^# (.+)$", markdown, flags=re.MULTILINE)
    name = title_match.group(1) if title_match else path.stem
    return {
        "name": name,
        "reasoning_summary": markdown_section(markdown, "## Reasoning Summary", ["## Model Response", "## Sources"]),
        "text": markdown_section(markdown, "## Model Response", ["## Sources"]),
        "sources": parse_sources(markdown),
        "archive_path": str(path),
    }


def parse_run_settings(path: Path) -> dict[str, str | bool]:
    settings = template_context()["settings"].copy()
    if not path.exists():
        return settings

    markdown = path.read_text(encoding="utf-8")
    setting_patterns = {
        "model": r"- Model: `([^`]+)`",
        "reasoning_effort": r"- Reasoning effort: `([^`]+)`",
        "search_context_size": r"- Search context: `([^`]+)`",
    }
    for key, pattern in setting_patterns.items():
        match = re.search(pattern, markdown)
        if match:
            settings[key] = match.group(1)

    web_search_match = re.search(r"- Web search: `(on|off)`", markdown)
    if web_search_match:
        settings["enable_web_search"] = web_search_match.group(1) == "on"

    return settings


def load_archive_run(run_id: str) -> dict:
    run_dir = archive_run_path(run_id)
    draft_path = run_dir / "01-submitted-draft.md"
    deck_outline = extract_fenced_text(draft_path.read_text(encoding="utf-8")) if draft_path.exists() else ""
    review_paths = sorted(
        path
        for path in run_dir.glob("*.md")
        if path.name not in {"00-settings.md", "01-submitted-draft.md"}
    )
    return {
        "run_id": run_id,
        "archive_dir": str(run_dir),
        "deck_outline": deck_outline,
        "settings": parse_run_settings(run_dir / "00-settings.md"),
        "results": [parse_review_markdown(path) for path in review_paths],
    }


def create_archive_run(settings: dict, deck_outline: str) -> Path:
    ARCHIVE_ROOT.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = ARCHIVE_ROOT / timestamp
    counter = 2

    while run_dir.exists():
        run_dir = ARCHIVE_ROOT / f"{timestamp}-{counter:02d}"
        counter += 1

    run_dir.mkdir(parents=True)
    save_run_metadata(run_dir, settings)
    save_submitted_draft(run_dir, deck_outline)
    app.logger.info(
        "archive_created run=%s path=%s deck_chars=%s settings=%s",
        run_dir.name,
        run_dir,
        len(deck_outline),
        settings,
    )
    return run_dir


def save_run_metadata(run_dir: Path, settings: dict) -> None:
    lines = [
        "# Review Run Settings",
        "",
        f"- Model: `{settings['model']}`",
        f"- Reasoning effort: `{settings['reasoning_effort']}`",
        f"- Web search: `{'on' if settings['enable_web_search'] else 'off'}`",
        f"- Search context: `{settings['search_context_size']}`",
        "",
    ]
    (run_dir / "00-settings.md").write_text("\n".join(lines), encoding="utf-8")


def save_submitted_draft(run_dir: Path, deck_outline: str) -> None:
    content = "\n".join(
        [
            "# Submitted Draft",
            "",
            "```text",
            deck_outline,
            "```",
            "",
        ]
    )
    (run_dir / "01-submitted-draft.md").write_text(content, encoding="utf-8")


def result_markdown(result: dict) -> str:
    lines = [
        f"# {result['name']}",
        "",
    ]

    if result.get("reasoning_summary"):
        lines.extend(
            [
                "## Reasoning Summary",
                "",
                result["reasoning_summary"],
                "",
            ]
        )

    lines.extend(
        [
            "## Model Response",
            "",
            result.get("text") or "",
            "",
        ]
    )

    if result.get("sources"):
        lines.extend(["## Sources", ""])
        for source in result["sources"]:
            lines.append(f"- [{source['title']}]({source['url']})")
        lines.append("")

    return "\n".join(lines)


def save_review_result(run_dir: Path, result: dict) -> Path:
    path = run_dir / f"{slugify(result['name'])}.md"
    path.write_text(result_markdown(result), encoding="utf-8")
    return path


def run_review(
    name: str,
    prompt: str,
    deck_outline: str,
    model: str,
    reasoning_effort: str,
    enable_web_search: bool,
    search_context_size: str,
    run_id: str = "manual",
) -> dict:
    started = time.monotonic()
    app.logger.info("review_start run=%s reviewer=%r", run_id, name)
    payload = build_response_payload(
        name=name,
        prompt=prompt,
        deck_outline=deck_outline,
        model=model,
        reasoning_effort=reasoning_effort,
        enable_web_search=enable_web_search,
        search_context_size=search_context_size,
    )
    response = create_response_with_retries(payload, reviewer_name=name, run_id=run_id)

    result = {
        "name": name,
        "text": response.output_text,
        "reasoning_summary": extract_reasoning_summary(response),
        "sources": extract_sources(response),
    }
    app.logger.info(
        "review_success run=%s reviewer=%r elapsed=%.2fs output_chars=%s reasoning_chars=%s sources=%s",
        run_id,
        name,
        time.monotonic() - started,
        len(result["text"] or ""),
        len(result["reasoning_summary"] or ""),
        len(result["sources"]),
    )
    return result


@app.get("/")
def index():
    return render_template("index.html", **template_context())


@app.get("/archive/<run_id>")
def archive(run_id: str):
    return jsonify(load_archive_run(run_id))


@app.post("/review")
def review():
    deck_outline = request.form.get("deck_outline", "").strip()
    settings = form_settings(request.form)

    if not deck_outline:
        return render_template(
            "index.html",
            **template_context(
                error="Paste a deck outline first.",
                deck_outline=deck_outline,
                settings=settings,
            ),
        )

    results = []
    archive_dir = create_archive_run(settings, deck_outline)
    run_id = archive_dir.name
    app.logger.info(
        "review_batch_start run=%s mode=sync reviewers=%s workers=%s",
        run_id,
        len(REVIEWERS),
        reviewer_worker_count(),
    )

    with ThreadPoolExecutor(max_workers=reviewer_worker_count()) as executor:
        future_to_reviewer = {
            executor.submit(
                run_review,
                reviewer["name"],
                reviewer["prompt"],
                deck_outline,
                settings["model"],
                settings["reasoning_effort"],
                settings["enable_web_search"],
                settings["search_context_size"],
                run_id,
            ): reviewer
            for reviewer in REVIEWERS
        }

        for future in as_completed(future_to_reviewer):
            reviewer = future_to_reviewer[future]
            try:
                result = future.result()
            except Exception as exc:
                app.logger.warning(
                    "review_failed run=%s reviewer=%r error=%s\n%s",
                    run_id,
                    reviewer["name"],
                    exception_log_summary(exc),
                    traceback.format_exc(),
                )
                result = {
                    "name": f"{reviewer['name']} failed",
                    "text": f"{type(exc).__name__}: {exc}",
                    "reasoning_summary": "",
                    "sources": [],
                }
            save_review_result(archive_dir, result)
            results.append(result)
            app.logger.info(
                "review_result_archived run=%s reviewer=%r completed=%s/%s",
                run_id,
                result["name"],
                len(results),
                len(REVIEWERS),
            )

    results.sort(key=lambda item: item["name"])
    app.logger.info("review_batch_done run=%s mode=sync results=%s", run_id, len(results))

    return render_template(
        "results.html",
        **template_context(
            deck_outline=deck_outline,
            results=results,
            settings=settings,
            archive_dir=str(archive_dir),
        ),
    )


def json_line(event: dict) -> str:
    return json.dumps(event) + "\n"


@app.post("/review-stream")
def review_stream():
    deck_outline = request.form.get("deck_outline", "").strip()
    settings = form_settings(request.form)

    if not deck_outline:
        return Response(
            json_line({"type": "error", "message": "Paste a deck outline first."}),
            mimetype="application/x-ndjson",
        )

    archive_dir = create_archive_run(settings, deck_outline)
    run_id = archive_dir.name
    app.logger.info(
        "review_batch_start run=%s mode=stream reviewers=%s workers=%s",
        run_id,
        len(REVIEWERS),
        reviewer_worker_count(),
    )

    def generate():
        yield json_line(
            {
                "type": "start",
                "settings": settings,
                "reviewers": [reviewer["name"] for reviewer in REVIEWERS],
                "archive_dir": str(archive_dir),
                "worker_count": reviewer_worker_count(),
            }
        )

        with ThreadPoolExecutor(max_workers=reviewer_worker_count()) as executor:
            for reviewer in REVIEWERS:
                yield json_line(
                    {
                        "type": "progress",
                        "reviewer": reviewer["name"],
                        "status": "queued",
                        "message": "Queued",
                    }
                )

            future_to_reviewer = {
                executor.submit(
                    run_review,
                    reviewer["name"],
                    reviewer["prompt"],
                    deck_outline,
                    settings["model"],
                    settings["reasoning_effort"],
                    settings["enable_web_search"],
                    settings["search_context_size"],
                    run_id,
                ): reviewer
                for reviewer in REVIEWERS
            }
            for reviewer in REVIEWERS:
                yield json_line(
                    {
                        "type": "progress",
                        "reviewer": reviewer["name"],
                        "status": "submitted",
                        "message": "Submitted to worker pool",
                    }
                )

            for future in as_completed(future_to_reviewer):
                reviewer = future_to_reviewer[future]
                try:
                    result = future.result()
                    archive_path = save_review_result(archive_dir, result)
                    result["archive_path"] = str(archive_path)
                    app.logger.info(
                        "review_result_archived run=%s reviewer=%r path=%s",
                        run_id,
                        result["name"],
                        archive_path,
                    )
                    yield json_line(
                        {
                            "type": "progress",
                            "reviewer": reviewer["name"],
                            "status": "completed",
                            "message": "Completed and archived",
                        }
                    )
                    yield json_line({"type": "result", "result": result})
                except Exception as exc:
                    app.logger.warning(
                        "review_failed run=%s reviewer=%r error=%s\n%s",
                        run_id,
                        reviewer["name"],
                        exception_log_summary(exc),
                        traceback.format_exc(),
                    )
                    result = {
                        "name": f"{reviewer['name']} failed",
                        "text": f"{type(exc).__name__}: {exc}",
                        "reasoning_summary": "",
                        "sources": [],
                    }
                    archive_path = save_review_result(archive_dir, result)
                    result["archive_path"] = str(archive_path)
                    yield json_line(
                        {
                            "type": "progress",
                            "reviewer": reviewer["name"],
                            "status": "failed",
                            "message": "Failed and archived",
                        }
                    )
                    yield json_line(
                        {
                            "type": "result",
                            "result": result,
                        }
                    )

        app.logger.info("review_batch_done run=%s mode=stream", run_id)
        yield json_line({"type": "done"})

    return Response(stream_with_context(generate()), mimetype="application/x-ndjson")


if __name__ == "__main__":
    app.run(debug=True)
