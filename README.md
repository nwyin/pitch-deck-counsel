# Deck Review Council

Tiny Flask app for running a pitch deck outline through several OpenAI-powered reviewer prompts.

## Setup

```bash
uv venv --python 3.12
uv pip install -r requirements.txt
cp .env.example .env
```

Add your API key to `.env`:

```bash
OPENAI_API_KEY=your_api_key_here
```

## Run

```bash
uv run flask --app app run --debug --port 5050
```

Then open:

```text
http://127.0.0.1:5050
```

Port `5000` may already be occupied on macOS, so `5050` is the documented default here.

## OpenAI reliability knobs

The app defaults to one reviewer call at a time. Each reviewer can use reasoning plus web search, so low concurrency keeps quota and rate-limit pressure predictable.

Optional `.env` settings:

```bash
REVIEW_MAX_WORKERS=1
OPENAI_BACKGROUND_RESPONSES=true
OPENAI_BACKGROUND_POLL_SECONDS=5
OPENAI_BACKGROUND_MAX_WAIT_SECONDS=1800
OPENAI_RETRIEVE_TIMEOUT_SECONDS=60
OPENAI_APP_MAX_RETRIES=1
OPENAI_SDK_MAX_RETRIES=0
OPENAI_READ_TIMEOUT_SECONDS=900
OPENAI_CONNECT_TIMEOUT_SECONDS=20
OPENAI_WRITE_TIMEOUT_SECONDS=60
OPENAI_POOL_TIMEOUT_SECONDS=60
REVIEW_MAX_OUTPUT_TOKENS=64000
LOG_LEVEL=INFO
LOG_PAYLOAD_PREVIEW_CHARS=0
```

The app uses Responses API background mode by default. That avoids holding a single long HTTP request open while reviewers perform reasoning and web search.

The app leaves SDK retries off by default. That makes each logged app attempt correspond to one OpenAI create request instead of one app attempt hiding several SDK retries.

Set `LOG_LEVEL=DEBUG` for noisier Flask/OpenAI request lifecycle logs. `LOG_PAYLOAD_PREVIEW_CHARS` can log the first N characters of the prompt payload, but it defaults to `0` so deck text is not printed to the terminal by accident.

For the leanest debugging run, use reasoning `Off`, leave web search unchecked, and set `REVIEW_MAX_OUTPUT_TOKENS=1200`.

## Test

```bash
uv run python -m unittest -v
```

## Archives

Each submitted review creates a timestamped directory under `archives/`.

Each run includes:

```text
00-settings.md
01-submitted-draft.md
<reviewer-name>.md
```

Reviewer files include the reasoning summary, model response, and sources when available.

The home page includes an archive dropdown sorted newest-first. Selecting a run loads the submitted draft back into the textarea and renders the saved reviewer artifacts below it.
