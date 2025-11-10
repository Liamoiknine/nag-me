# Voice Accountability App

A FastAPI-based voice accountability app that calls users at scheduled intervals to check on their productivity. The app uses Twilio for voice calls, OpenAI Whisper for speech transcription, and GPT for dynamic conversational responses.

## Features

- **Automated scheduling**: Users receive calls at their chosen intervals
- **AI-powered conversations**: OpenAI Whisper transcribes speech, GPT-3.5 generates contextual responses
- **Personality-based coaching**: Choose from strict, sarcastic, or supportive coaching styles
- **Natural voice**: Uses Amazon Polly Neural voices for high-quality text-to-speech
- **Web interface**: Beautiful, modern UI for user management
- **Real-time conversation**: Multi-turn conversations that adapt to user responses

## Setup Instructions

### 1. Clone the Repository

```bash
git clone https://github.com/Liamoiknine/nag-me
cd nag-me
```

### 2. Create Virtual Environment

```bash
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure Environment Variables

Copy `env.example` to `.env` and fill in your credentials:

```bash
cp env.example .env
```

Edit `.env` with your actual values:
- `TWILIO_ACCOUNT_SID`: Your Twilio Account SID
- `TWILIO_AUTH_TOKEN`: Your Twilio Auth Token  
- `TWILIO_PHONE_NUMBER`: Your Twilio phone number (e.g., +1234567890)
- `OPENAI_API_KEY`: Your OpenAI API key
- `WEBHOOK_BASE_URL`: Your ngrok URL (see step 6)
- `VERIFIED_PHONE_NUMBER`: The phone number verified with Twilio (required for trial accounts)

### 5. Set Up Twilio

1. Create a Twilio account at https://www.twilio.com
2. Get a phone number with voice capabilities
3. Note your Account SID and Auth Token from the Twilio Console
4. **Important for Trial Accounts**: Verify your personal phone number in the Twilio Console under Phone Numbers → Verified Caller IDs. This is the number you'll use for testing.

### 6. Set Up ngrok for Local Development

Since Twilio needs to send webhooks to your local server, you'll need ngrok:

```bash
# Install ngrok (if not already installed)
# Download from https://ngrok.com/download

# Start ngrok tunnel
ngrok http 8000
```

Copy the HTTPS URL (e.g., `https://abc123.ngrok.io`) and update your `.env` file:
```
WEBHOOK_BASE_URL=https://abc123.ngrok.io
```

### 7. Run the Application

```bash
python main.py
```

The app will be available at:
- Web interface: http://localhost:8000
- API docs: http://localhost:8000/docs

## Usage

### Web Interface

1. Open http://localhost:8000 in your browser
2. Fill out the registration form:
   - Phone number (must match your verified number for trial accounts)
   - Call interval in minutes (minimum 5 minutes)
   - Coach personality (supportive, strict, or sarcastic)
3. Click "Register & Start"
4. You'll receive your first call immediately as a welcome call
5. Subsequent calls will arrive at your specified interval

### API Endpoints

#### Register a User
```bash
curl -X POST "http://localhost:8000/register" \
  -H "Content-Type: application/json" \
  -d '{
    "phone_number": "+1234567890",
    "interval_minutes": 60,
    "personality": "supportive"
  }'
```

#### Start Scheduling
```bash
curl -X POST "http://localhost:8000/start" \
  -H "Content-Type: application/json" \
  -d '{"user_id": 1}'
```

#### Stop Scheduling
```bash
curl -X POST "http://localhost:8000/stop" \
  -H "Content-Type: application/json" \
  -d '{"user_id": 1}'
```

#### Trigger Immediate Call
```bash
curl -X POST "http://localhost:8000/call-now" \
  -H "Content-Type: application/json" \
  -d '{"user_id": 1}'
```

#### Delete User
```bash
curl -X POST "http://localhost:8000/delete-user" \
  -H "Content-Type: application/json" \
  -d '{"user_id": 1}'
```

## How It Works

1. **Scheduling**: APScheduler runs a background job every minute to check for users due for calls
2. **Call Initiation**: When a user is due, the app triggers a Twilio call to their phone number
3. **Voice Interaction**: 
   - Twilio calls the user with a personality-based greeting using Amazon Polly Neural voices
   - User speaks their response
   - Audio is recorded and downloaded from Twilio
   - OpenAI Whisper transcribes the speech to text
   - GPT-3.5-turbo generates a contextual response based on the conversation history and personality
   - Response is spoken back via TwiML using Polly
   - Conversation continues for multiple turns until the AI decides to end or user hangs up
4. **Conversation Management**: Each call maintains conversation history for context-aware responses

## Personality Types

- **Supportive**: Encouraging and understanding, ends positively when user shows commitment
- **Strict**: Direct and demanding, interrogates about productivity, ends when satisfied or frustrated  
- **Sarcastic**: Uses wit and humor to challenge excuses, decides when enough is enough

## Database

The app uses SQLite with a simple `users` table storing:
- Phone number, call interval, personality type
- Active status and next call time
- Creation timestamp

## Troubleshooting

### Common Issues

1. **"Only verified number" error**: For Twilio trial accounts, you must use a verified phone number. Verify your number in the Twilio Console and add it to `VERIFIED_PHONE_NUMBER` in your `.env` file.

2. **Calls not triggering**: 
   - Check that the scheduler is running (look for startup logs)
   - Verify the user is marked as active
   - Check that `next_call_time` is in the past

3. **Webhook errors**: 
   - Ensure ngrok is running and the URL hasn't changed
   - Update `WEBHOOK_BASE_URL` in `.env` if ngrok URL changed
   - Check ngrok web interface at http://localhost:4040 for webhook requests

4. **OpenAI errors**: 
   - Verify your API key is correct
   - Ensure you have sufficient credits
   - Check for rate limiting

5. **Database issues**: If you need to reset the database, simply delete `voice_accountability.db` and restart the app.

### Logs

The app provides detailed logging. Check the console output for:
- Scheduler status and call triggers
- Twilio webhook requests and responses
- Whisper transcription results
- GPT response generation
- Call flow and conversation state

## Development

### Project Structure

```
creative_proj/
├── main.py              # FastAPI app with webhooks and web interface
├── database.py          # SQLAlchemy models and database operations
├── scheduler.py         # APScheduler for automated calls
├── openai_client.py     # OpenAI/Whisper/GPT integration
├── requirements.txt     # Python dependencies
├── env.example          # Environment variable template
├── .gitignore          # Git ignore rules
├── README.md           # This file
└── voice_accountability.db  # SQLite database (auto-generated)
```

### Adding New Features

- **New personality types**: Add system prompts in `openai_client.py`
- **Different voices**: Change the `VOICE` constant in `main.py` to any Polly voice
- **Custom scheduling**: Modify `scheduler.py` for time-of-day restrictions or different intervals
- **Additional user data**: Extend the `User` model in `database.py`
- **Webhook security**: Add Twilio request signature validation
- **Analytics**: Track conversation data and user engagement

## Tech Stack

- **Backend**: FastAPI (Python)
- **Database**: SQLite with SQLAlchemy ORM
- **Scheduling**: APScheduler
- **Voice Platform**: Twilio (calls, TwiML)
- **AI**: OpenAI Whisper (transcription) + GPT-3.5 (responses)
- **Text-to-Speech**: Amazon Polly Neural voices via Twilio

## Cost Considerations

- **Twilio**: ~$0.013/min for voice calls + phone number rental ($1-2/month)
- **OpenAI Whisper**: $0.006 per minute of audio
- **OpenAI GPT-3.5**: ~$0.002 per conversation turn
- **Total**: Approximately $0.02-0.03 per call minute

For a 1-minute call every hour, monthly cost is ~$15-20.

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## License

MIT License - feel free to modify and use for your own projects!
