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
