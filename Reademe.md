# Gemini Live Voice App (Python + FastAPI)

A separate, from-scratch web app that does live voice chat with Gemini 2.5 Flash Native Audio using the Live API.

## Prereqs
- Python 3.11+ recommended
- A Gemini API key (AI Studio)

## Setup
```bash
cd gemini_live_voice_app
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env

# uvicorn app.main:app --reload --host 127.0.0.1 --port 8003

