import json
import re
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, Response, render_template, request, stream_with_context
from openai import OpenAI

from prompts import REVIEWERS


load_dotenv()

app = Flask(__name__)
client = OpenAI(timeout=300)

MODEL_OPTIONS = [
    {"value": "gpt-5.5", "label": "GPT-5.5"},
    {"value": "gpt-5.4", "label": "GPT-5.4"},
    {"value": "gpt-5.4-mini", "label": "GPT-5.4 Mini"},
    {"value": "gpt-5.2", "label": "GPT-5.2"},
]
EFFORT_OPTIONS = [
    {"value": "default", "label": "Default"},
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
DEFAULT_EFFORT = "default"
DEFAULT_SEARCH_CONTEXT_SIZE = "medium"
REVIEW_TIMEOUT_SECONDS = 300
ARCHIVE_ROOT = Path("archives")
WEB_RESEARCH_INSTRUCTIONS = """
When web search is enabled, independently research investor-relevant market,
competitor, procurement, pricing, category, and timing context. Cite sources
where available. Separate facts found externally from assumptions and inferences.
"""


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
        "settings": {
            "model": DEFAULT_MODEL,
            "reasoning_effort": DEFAULT_EFFORT,
            "enable_web_search": True,
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
    }

    if reasoning_effort != "default":
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

    return payload


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
) -> dict:
    payload = build_response_payload(
        name=name,
        prompt=prompt,
        deck_outline=deck_outline,
        model=model,
        reasoning_effort=reasoning_effort,
        enable_web_search=enable_web_search,
        search_context_size=search_context_size,
    )
    thread_client = OpenAI(timeout=REVIEW_TIMEOUT_SECONDS, max_retries=2)
    response = thread_client.responses.create(**payload)

    return {
        "name": name,
        "text": response.output_text,
        "reasoning_summary": extract_reasoning_summary(response),
        "sources": extract_sources(response),
    }


@app.get("/")
def index():
    return render_template("index.html", **template_context())


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

    with ThreadPoolExecutor(max_workers=len(REVIEWERS)) as executor:
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
            ): reviewer
            for reviewer in REVIEWERS
        }

        for future in as_completed(future_to_reviewer):
            reviewer = future_to_reviewer[future]
            try:
                result = future.result()
            except Exception as exc:
                result = {
                    "name": f"{reviewer['name']} failed",
                    "text": f"{type(exc).__name__}: {exc}",
                    "reasoning_summary": "",
                    "sources": [],
                }
            save_review_result(archive_dir, result)
            results.append(result)

    results.sort(key=lambda item: item["name"])

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

    def generate():
        yield json_line(
            {
                "type": "start",
                "settings": settings,
                "reviewers": [reviewer["name"] for reviewer in REVIEWERS],
                "archive_dir": str(archive_dir),
            }
        )

        with ThreadPoolExecutor(max_workers=len(REVIEWERS)) as executor:
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
                ): reviewer
                for reviewer in REVIEWERS
            }

            for future in as_completed(future_to_reviewer):
                reviewer = future_to_reviewer[future]
                try:
                    result = future.result()
                    archive_path = save_review_result(archive_dir, result)
                    result["archive_path"] = str(archive_path)
                    yield json_line({"type": "result", "result": result})
                except Exception as exc:
                    app.logger.warning(
                        "Reviewer failed: %s\n%s",
                        reviewer["name"],
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
                            "type": "result",
                            "result": result,
                        }
                    )

        yield json_line({"type": "done"})

    return Response(stream_with_context(generate()), mimetype="application/x-ndjson")


if __name__ == "__main__":
    app.run(debug=True)
