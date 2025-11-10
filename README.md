# Nag Me

Voice accountability app that calls users at scheduled intervals. Uses Twilio for calls, OpenAI Whisper for transcription, and GPT for responses.

## Features

- Scheduled calls at configurable intervals
- AI conversations with Whisper transcription and GPT responses
- Personality-based coaching (strict, sarcastic, supportive)
- Amazon Polly Neural voices
- Web UI for management

## Setup

```bash
git clone https://github.com/Liamoiknine/nag-me
cd nag-me
```

**Use Python 3.11 or 3.12.**

```bash
brew install python@3.12
python3.12 -m venv venv
source venv/bin/activate
```

**Dependencies**


```bash
pip install --upgrade pip
pip install -r requirements.txt
```

Set vars in `.env`:
- `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER`
- `OPENAI_API_KEY`
- `WEBHOOK_BASE_URL` (ngrok URL)
- `VERIFIED_PHONE_NUMBER` (required for trial accounts)

**Twillio Setup**

1. Create account and get a phone number
2. Get Account SID and Auth Token
3. For trial accounts: verify your phone number in Console → Verified Caller IDs

**ngrok to expose localhost**

```bash
ngrok http 8000
```

Update `WEBHOOK_BASE_URL` in `.env` with the ngrok HTTPS URL.

**Run program**

```bash
python main.py
```

Web UI: http://localhost:8000 | API docs: http://localhost:8000/docs


## Development
APScheduler checks for due calls every minute. When triggered:
1. Twilio calls user with personality-based greeting (Polly Neural)
2. User speech is recorded → Whisper transcription → GPT response → TwiML playback
3. Conversation continues with maintained history until AI ends or user hangs up

### Project Structure

```
nag-me/
├── main.py              # FastAPI app, webhooks, web UI
├── database.py          # SQLAlchemy models
├── scheduler.py         # APScheduler
├── openai_client.py     # OpenAI integration
├── static/style.css     # CSS
├── requirements.txt
├── env.example
└── voice_accountability.db
```

### Tech Stack

FastAPI, SQLite/SQLAlchemy, APScheduler, Twilio, OpenAI (Whisper + GPT-3.5), Amazon Polly

### Cost

~$0.02-0.03 per call minute. 1-minute calls every hour ≈ $15-20/month.
