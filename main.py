from fastapi import FastAPI, Depends, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from pydantic import BaseModel
from twilio.twiml.voice_response import VoiceResponse
from twilio.rest import Client
import os
from datetime import datetime, timedelta
import logging
from dotenv import load_dotenv
import requests
from openai import OpenAI
import tempfile

# Load environment variables
load_dotenv()

from database import (
    get_db, create_user, get_user, get_user_by_phone, 
    update_user, get_active_users, User
)
from scheduler import start_scheduler, stop_scheduler, call_user
from openai_client import generate_response

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Voice Accountability App")

# Mount static files
app.mount("/static", StaticFiles(directory="static"), name="static")

# Voice configuration - change this to use different Polly voices
# Options: 'Polly.Matthew', 'Polly.Matthew-Neural', 'Polly.Joanna', 'Polly.Brian', etc.
VOICE = 'Polly.Matthew-Neural'  # Premium natural voice

# Initialize Twilio client
twilio_client = Client(os.getenv('TWILIO_ACCOUNT_SID'), os.getenv('TWILIO_AUTH_TOKEN'))

# Initialize OpenAI client for Whisper
openai_client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))

# In-memory storage for conversation state (call_sid -> conversation_history)
conversation_states = {}

# Pydantic models
class UserRegistration(BaseModel):
    phone_number: str
    interval_minutes: int
    personality: str

class UserAction(BaseModel):
    user_id: int

# Startup and shutdown events
@app.on_event("startup")
async def startup_event():
    logger.info("=" * 60)
    logger.info("VOICE ACCOUNTABILITY APP - STARTING")
    logger.info("=" * 60)
    logger.info(f"Environment variables loaded:")
    logger.info(f"  TWILIO_ACCOUNT_SID: {os.getenv('TWILIO_ACCOUNT_SID')[:10]}...")
    logger.info(f"  TWILIO_PHONE_NUMBER: {os.getenv('TWILIO_PHONE_NUMBER')}")
    logger.info(f"  WEBHOOK_BASE_URL: {os.getenv('WEBHOOK_BASE_URL')}")
    logger.info(f"  OPENAI_API_KEY: {'SET' if os.getenv('OPENAI_API_KEY') else 'NOT SET'}")
    start_scheduler()
    logger.info("Scheduler started - checking for due calls every minute")
    logger.info("=" * 60)
    logger.info("Voice Accountability App ready!")
    logger.info("  Web UI: http://localhost:8000")
    logger.info("  API Docs: http://localhost:8000/docs")
    logger.info("=" * 60)

@app.on_event("shutdown")
async def shutdown_event():
    stop_scheduler()
    logger.info("Voice Accountability App stopped")

# API Endpoints
@app.post("/register")
async def register_user(user_data: UserRegistration, db: Session = Depends(get_db)):
    """Register a new user and immediately trigger a call"""
    try:
        logger.info(f"Attempting to register user with phone: {user_data.phone_number}, interval: {user_data.interval_minutes}, personality: {user_data.personality}")
        
        # Normalize phone number (ensure it starts with +)
        phone = user_data.phone_number.strip()
        if not phone.startswith('+'):
            phone = '+1' + phone  # Assume US number if no country code
            logger.info(f"Normalized phone number to: {phone}")
        
        # IMPORTANT: Only verified number can receive calls
        VERIFIED_NUMBER = os.getenv('VERIFIED_PHONE_NUMBER', '+1234567890')
        if phone != VERIFIED_NUMBER:
            logger.error(f"Cannot register {phone} - Only {VERIFIED_NUMBER} is verified!")
            raise HTTPException(
                status_code=400, 
                detail=f"Only {VERIFIED_NUMBER} is verified for calls. Other numbers will fail with Twilio."
            )
        
        # Update phone in data
        user_data.phone_number = phone
        
        # Validate personality
        valid_personalities = ["strict", "sarcastic", "supportive"]
        if user_data.personality not in valid_personalities:
            logger.warning(f"Invalid personality type: {user_data.personality}")
            raise HTTPException(status_code=400, detail="Invalid personality type")
        
        # Check if user already exists
        existing_user = get_user_by_phone(db, user_data.phone_number)
        if existing_user:
            logger.warning(f"Phone number already registered: {user_data.phone_number}")
            raise HTTPException(status_code=400, detail="Phone number already registered")
        
        # Create user
        logger.info(f"Creating new user in database...")
        user = create_user(db, user_data.phone_number, user_data.interval_minutes, user_data.personality)
        logger.info(f"User created successfully with ID: {user.id}")
        
        # Activate scheduling immediately
        update_user(db, user.id, is_active=True)
        logger.info(f"Scheduling activated for user {user.id}")
        
        # Trigger immediate call
        try:
            logger.info(f"Triggering immediate welcome call to {user.phone_number}...")
            call_user(user.id)
            logger.info(f"Welcome call initiated successfully for user {user.id}")
            call_status = "Call initiated - you should receive it shortly!"
        except Exception as call_error:
            logger.error(f"Failed to initiate immediate call: {str(call_error)}", exc_info=True)
            call_status = f"Registration successful but call failed: {str(call_error)}"
        
        return {
            "message": "User registered successfully",
            "user_id": user.id,
            "phone_number": user.phone_number,
            "interval_minutes": user.interval_minutes,
            "personality": user.personality,
            "call_status": call_status
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error registering user: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Registration failed: {str(e)}")

@app.post("/start")
async def start_user(user_action: UserAction, db: Session = Depends(get_db)):
    """Activate scheduling for a user"""
    try:
        logger.info(f"Attempting to start scheduling for user ID: {user_action.user_id}")
        user = get_user(db, user_action.user_id)
        if not user:
            logger.warning(f"User not found: {user_action.user_id}")
            raise HTTPException(status_code=404, detail="User not found")
        
        # Set next call time to now + interval
        next_call_time = datetime.utcnow() + timedelta(minutes=user.interval_minutes)
        update_user(db, user_action.user_id, is_active=True, next_call_time=next_call_time)
        logger.info(f"Scheduling activated for user {user_action.user_id}, next call at {next_call_time}")
        
        return {"message": f"Scheduling activated for user {user_action.user_id}"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error starting user: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to start user: {str(e)}")

@app.post("/stop")
async def stop_user(user_action: UserAction, db: Session = Depends(get_db)):
    """Deactivate scheduling for a user"""
    try:
        user = get_user(db, user_action.user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        update_user(db, user_action.user_id, is_active=False)
        logger.info(f"User {user_action.user_id} deactivated")
        
        return {"message": f"Scheduling deactivated for user {user_action.user_id}"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error stopping user: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to stop user: {str(e)}")

@app.get("/users")
async def list_users(db: Session = Depends(get_db)):
    """Get all registered users"""
    try:
        users = get_active_users(db)
        # Get all users, not just active ones
        all_users = db.query(User).all()
        
        return [{
            "id": user.id,
            "phone_number": user.phone_number,
            "interval_minutes": user.interval_minutes,
            "personality": user.personality,
            "is_active": user.is_active,
            "next_call_time": user.next_call_time.isoformat() if user.next_call_time else None,
            "created_at": user.created_at.isoformat() if user.created_at else None
        } for user in all_users]
    except Exception as e:
        logger.error(f"Error listing users: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to list users: {str(e)}")

@app.post("/call-now")
async def call_now(request: Request, db: Session = Depends(get_db)):
    """Manually trigger an immediate call to a user"""
    try:
        # Parse the request body
        body = await request.json()
        logger.info(f"Call-now request body: {body}")
        
        user_id = body.get('user_id')
        if user_id is None:
            raise HTTPException(status_code=400, detail="user_id is required")
        
        user = get_user(db, user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        logger.info(f"Manual call trigger requested for user {user_id}")
        
        # Trigger the call
        call_user(user_id)
        
        return {"message": f"Call initiated to {user.phone_number}. Check your phone!"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error triggering call: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to trigger call: {str(e)}")

@app.post("/delete-user")
async def delete_user_endpoint(request: Request, db: Session = Depends(get_db)):
    """Delete a user from the database"""
    try:
        # Parse the request body
        body = await request.json()
        logger.info(f"Delete-user request body: {body}")
        
        user_id = body.get('user_id')
        if user_id is None:
            raise HTTPException(status_code=400, detail="user_id is required")
        
        user = get_user(db, user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        phone = user.phone_number
        db.delete(user)
        db.commit()
        
        logger.info(f"User {user_id} ({phone}) deleted")
        
        return {"message": f"User {phone} deleted successfully"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting user: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to delete user: {str(e)}")

@app.post("/twilio-call")
async def handle_twilio_call(request: Request):
    """Handle incoming Twilio call - initial greeting"""
    try:
        # Get form data from Twilio
        form_data = await request.form()
        
        # Log all form data for debugging
        logger.info("=" * 60)
        logger.info("TWILIO WEBHOOK - /twilio-call")
        logger.info("=" * 60)
        logger.info(f"Form data received: {dict(form_data)}")
        
        call_sid = form_data.get('CallSid')
        from_number = form_data.get('From')  # Caller (Twilio for outbound)
        to_number = form_data.get('To')      # Recipient (User for outbound)
        direction = form_data.get('Direction')  # 'outbound-api' or 'inbound'
        
        logger.info(f"Call SID: {call_sid}")
        logger.info(f"Direction: {direction}")
        logger.info(f"From: {from_number}")
        logger.info(f"To: {to_number}")
        
        # Determine user's phone number based on call direction
        if direction == 'outbound-api':
            # We called them - user is the 'To' number
            user_phone = to_number
            logger.info(f"Outbound call - User phone: {user_phone}")
        else:
            # They called us - user is the 'From' number
            user_phone = from_number
            logger.info(f"Inbound call - User phone: {user_phone}")
        
        # Initialize conversation state
        conversation_states[call_sid] = {
            'history': [],
            'user_phone': user_phone
        }
        
        # Find user by phone number to get personality
        db = next(get_db())
        user = get_user_by_phone(db, user_phone)
        
        if not user:
            logger.warning(f"No user found for phone number: {user_phone}")
            greeting = "Hello! This is your productivity accountability call. How are you doing with your goals today?"
        else:
            logger.info(f"Found user ID {user.id} with personality: {user.personality}")
            # Customize greeting based on personality
            greetings = {
                "strict": "This is your accountability check. Tell me, what have you accomplished today?",
                "sarcastic": "Well, well, well. Another productivity call. So, how's that to-do list looking?",
                "supportive": "Hi there! It's time for your accountability check-in. How are you feeling about your progress today?"
            }
            greeting = greetings.get(user.personality, "Hello! This is your productivity accountability call. How are you doing with your goals today?")
        
        # Create TwiML response
        response = VoiceResponse()
        response.say(greeting, voice=VOICE)
        
        # Use Gather with speech for real-time transcription
        response.gather(
            input='speech',
            timeout=5,
            speech_timeout=2,  # 2 seconds of silence to end speech
            action='/twilio-response',
            method='POST'
        )
        
        # If no input, hang up
        response.say("I didn't hear anything. Goodbye!", voice=VOICE)
        response.hangup()
        
        twiml_str = str(response)
        logger.info(f"Returning TwiML ({len(twiml_str)} bytes):")
        logger.info(twiml_str)
        logger.info("=" * 60)
        
        return Response(content=twiml_str, media_type='application/xml')
        
    except Exception as e:
        logger.error(f"Error handling Twilio call: {str(e)}", exc_info=True)
        response = VoiceResponse()
        response.say("Sorry, there was an error. Goodbye!", voice=VOICE)
        response.hangup()
        return Response(content=str(response), media_type='application/xml')

@app.post("/twilio-recording")
async def handle_twilio_recording(request: Request):
    """Handle Twilio recording and transcribe with Whisper"""
    try:
        # Get form data from Twilio
        form_data = await request.form()
        
        logger.info("=" * 60)
        logger.info("TWILIO WEBHOOK - /twilio-recording")
        logger.info("=" * 60)
        logger.info(f"Form data received: {dict(form_data)}")
        
        call_sid = form_data.get('CallSid')
        recording_url = form_data.get('RecordingUrl')
        direction = form_data.get('Direction')
        from_number = form_data.get('From')
        to_number = form_data.get('To')
        
        logger.info(f"Call SID: {call_sid}")
        logger.info(f"Recording URL: {recording_url}")
        logger.info(f"Direction: {direction}")
        logger.info(f"From: {from_number}, To: {to_number}")
        
        # Determine user phone number
        if direction == 'outbound-api':
            user_phone = to_number
        else:
            user_phone = from_number
        
        # Get conversation state
        call_state = conversation_states.get(call_sid)
        if not call_state:
            call_state = {'history': [], 'user_phone': user_phone}
            logger.warning(f"No conversation state found, created new state for {user_phone}")
        
        conversation_history = call_state.get('history', [])
        
        # Find user
        db = next(get_db())
        user = get_user_by_phone(db, user_phone)
        
        if not user:
            logger.error(f"User not found for phone number: {user_phone}")
            response = VoiceResponse()
            response.say("Sorry, I couldn't find your account. Goodbye!", voice=VOICE)
            response.hangup()
            return Response(content=str(response), media_type='application/xml')
        
        logger.info(f"Processing recording for user ID {user.id} ({user.personality})")
        
        # Download the audio file from Twilio
        if not recording_url:
            logger.error("No recording URL provided")
            response = VoiceResponse()
            response.say("Sorry, I didn't receive your recording. Goodbye!", voice=VOICE)
            response.hangup()
            return Response(content=str(response), media_type='application/xml')
        
        # Add .wav extension to get audio file
        audio_url = f"{recording_url}.wav"
        logger.info(f"Downloading audio from: {audio_url}")
        
        # Download audio with Twilio authentication (faster timeout)
        auth = (os.getenv('TWILIO_ACCOUNT_SID'), os.getenv('TWILIO_AUTH_TOKEN'))
        audio_response = requests.get(audio_url, auth=auth, timeout=5, stream=True)
        
        if audio_response.status_code != 200:
            logger.error(f"Failed to download audio: {audio_response.status_code}")
            response = VoiceResponse()
            response.say("Sorry, I couldn't process your recording. Goodbye!", voice=VOICE)
            response.hangup()
            return Response(content=str(response), media_type='application/xml')
        
        # Save audio temporarily
        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as temp_audio:
            temp_audio.write(audio_response.content)
            temp_audio_path = temp_audio.name
        
        logger.info(f"Audio saved to: {temp_audio_path}")
        
        # Transcribe with Whisper
        logger.info("Transcribing with OpenAI Whisper...")
        try:
            with open(temp_audio_path, 'rb') as audio_file:
                transcription = openai_client.audio.transcriptions.create(
                    model="whisper-1",
                    file=audio_file,
                    language="en"  # Optimize for English
                )
            
            speech_text = transcription.text
            logger.info(f"Whisper transcription: '{speech_text}'")
            
        except Exception as whisper_error:
            logger.error(f"Whisper transcription failed: {whisper_error}", exc_info=True)
            response = VoiceResponse()
            response.say("Sorry, I couldn't understand you. Goodbye!", voice=VOICE)
            response.hangup()
            # Clean up temp file
            os.remove(temp_audio_path)
            return Response(content=str(response), media_type='application/xml')
        finally:
            # Clean up temp file
            if os.path.exists(temp_audio_path):
                os.remove(temp_audio_path)
        
        # Add user input to conversation history
        conversation_history.append({"role": "user", "content": speech_text})
        
        # Generate LLM response
        logger.info(f"Generating LLM response...")
        llm_response = generate_response(speech_text, user.personality, conversation_history)
        logger.info(f"LLM response: '{llm_response['response']}', should_end: {llm_response['should_end']}")
        
        # Add assistant response to conversation history
        conversation_history.append({"role": "assistant", "content": llm_response["response"]})
        call_state['history'] = conversation_history
        conversation_states[call_sid] = call_state
        
        # Create TwiML response
        response = VoiceResponse()
        response.say(llm_response["response"], voice=VOICE)
        
        if llm_response["should_end"]:
            response.say("That's all for now. Stay productive!", voice=VOICE)
            response.hangup()
            # Clean up conversation state
            if call_sid in conversation_states:
                del conversation_states[call_sid]
        else:
            # Continue conversation - record again
            response.record(
                action='/twilio-recording',
                method='POST',
                max_length=30,
                timeout=3,
                transcribe=False,
                play_beep=True
            )
            # If no input, hang up
            response.say("I didn't hear anything. Goodbye!", voice=VOICE)
            response.hangup()
            # Clean up conversation state
            if call_sid in conversation_states:
                del conversation_states[call_sid]
        
        twiml_str = str(response)
        logger.info(f"Returning TwiML ({len(twiml_str)} bytes):")
        logger.info(twiml_str)
        logger.info("=" * 60)
        
        return Response(content=twiml_str, media_type='application/xml')
        
    except Exception as e:
        logger.error(f"Error handling Twilio recording: {str(e)}", exc_info=True)
        response = VoiceResponse()
        response.say("Sorry, there was an error. Goodbye!", voice=VOICE)
        response.hangup()
        return Response(content=str(response), media_type='application/xml')

@app.post("/twilio-response")
async def handle_twilio_response(request: Request):
    """Handle Twilio speech response and generate LLM reply"""
    try:
        # Get form data from Twilio
        form_data = await request.form()
        
        # Log all form data for debugging
        logger.info("=" * 60)
        logger.info("TWILIO WEBHOOK - /twilio-response")
        logger.info("=" * 60)
        logger.info(f"Form data received: {dict(form_data)}")
        
        call_sid = form_data.get('CallSid')
        speech_result = form_data.get('SpeechResult', '')
        direction = form_data.get('Direction')
        from_number = form_data.get('From')
        to_number = form_data.get('To')
        
        logger.info(f"Call SID: {call_sid}")
        logger.info(f"Speech: '{speech_result}'")
        logger.info(f"Direction: {direction}")
        logger.info(f"From: {from_number}, To: {to_number}")
        
        # Get conversation state (should have user_phone from initial call)
        call_state = conversation_states.get(call_sid)
        if not call_state:
            # Fallback: determine user phone from direction
            if direction == 'outbound-api':
                user_phone = to_number
            else:
                user_phone = from_number
            call_state = {'history': [], 'user_phone': user_phone}
            logger.warning(f"No conversation state found, created new state for {user_phone}")
        
        conversation_history = call_state.get('history', [])
        user_phone = call_state.get('user_phone')
        
        # Find user by phone number to get personality
        db = next(get_db())
        user = get_user_by_phone(db, user_phone)
        
        if not user:
            logger.error(f"User not found for phone number: {user_phone}")
            response = VoiceResponse()
            response.say("Sorry, I couldn't find your account. Goodbye!", voice=VOICE)
            response.hangup()
            return Response(content=str(response), media_type='application/xml')
        
        logger.info(f"Processing response for user ID {user.id} ({user.personality})")
        
        # Add user input to conversation history
        conversation_history.append({"role": "user", "content": speech_result})
        
        # Generate LLM response
        logger.info(f"Generating LLM response...")
        llm_response = generate_response(speech_result, user.personality, conversation_history)
        logger.info(f"LLM response: '{llm_response['response']}', should_end: {llm_response['should_end']}")
        
        # Add assistant response to conversation history
        conversation_history.append({"role": "assistant", "content": llm_response["response"]})
        call_state['history'] = conversation_history
        conversation_states[call_sid] = call_state
        
        # Create TwiML response
        response = VoiceResponse()
        response.say(llm_response["response"], voice=VOICE)
        
        if llm_response["should_end"]:
            response.say("That's all for now. Stay productive!", voice=VOICE)
            response.hangup()
            # Clean up conversation state
            if call_sid in conversation_states:
                del conversation_states[call_sid]
        else:
            # Continue conversation
            response.gather(
                input='speech',
                timeout=5,
                speech_timeout=2,  # 2 seconds of silence to end speech
                action='/twilio-response',
                method='POST'
            )
            # If no input, hang up
            response.say("I didn't hear anything. Goodbye!", voice=VOICE)
            response.hangup()
            # Clean up conversation state
            if call_sid in conversation_states:
                del conversation_states[call_sid]
        
        twiml_str = str(response)
        logger.info(f"Returning TwiML ({len(twiml_str)} bytes):")
        logger.info(twiml_str)
        logger.info("=" * 60)
        
        return Response(content=twiml_str, media_type='application/xml')
        
    except Exception as e:
        logger.error(f"Error handling Twilio response: {str(e)}", exc_info=True)
        response = VoiceResponse()
        response.say("Sorry, there was an error. Goodbye!", voice=VOICE)
        response.hangup()
        return Response(content=str(response), media_type='application/xml')

# Web interface
@app.get("/", response_class=HTMLResponse)
async def get_index():
    """Serve the registration and management interface"""
    html_content = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Nag Me</title>
        <link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=Inter:wght@400;500&display=swap" rel="stylesheet">
        <link href="/static/style.css" rel="stylesheet">
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>Nag Me</h1>
                <p>Get called. Stay accountable. Get things done.</p>
            </div>
            
            <div class="sections-wrapper">
            <div class="section">
                    <h2>New Account</h2>
                <form id="registrationForm">
                    <div class="form-group">
                        <label for="phone">Phone Number</label>
                        <input type="tel" id="phone" name="phone" placeholder="+1 234 567 8900" required>
                    </div>
                    
                    <div class="form-group">
                            <label for="interval">Check-in Frequency (minutes)</label>
                        <input type="number" id="interval" name="interval" min="5" max="1440" value="60" placeholder="60" required>
                    </div>
                    
                    <div class="form-group">
                            <label for="personality">Coach Style</label>
                        <select id="personality" name="personality" required>
                            <option value="supportive">Supportive</option>
                            <option value="strict">Strict</option>
                            <option value="sarcastic">Sarcastic</option>
                        </select>
                    </div>
                    
                        <button type="submit">Start Getting Called</button>
                </form>
                <div id="registerStatus" class="status"></div>
            </div>
            
                <div class="section accounts-section">
                    <h2>Active Accounts</h2>
                    <div class="refresh-btn-wrapper">
                        <button onclick="loadUsers()" class="success">Refresh List</button>
                    </div>
                <div id="usersList"></div>
                </div>
            </div>
        </div>
        
        <script>
            // Load users on page load
            window.addEventListener('load', loadUsers);
            
            // Registration form
            document.getElementById('registrationForm').addEventListener('submit', async function(e) {
                e.preventDefault();
                
                const formData = new FormData(e.target);
                const data = {
                    phone_number: formData.get('phone'),
                    interval_minutes: parseInt(formData.get('interval')),
                    personality: formData.get('personality')
                };
                
                try {
                    const response = await fetch('/register', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                        },
                        body: JSON.stringify(data)
                    });
                    
                    const result = await response.json();
                    
                    if (response.ok) {
                        document.getElementById('registerStatus').innerHTML = 
                            '<div class="success-msg">' +
                            '<strong>Registration successful!</strong><br>' +
                            result.call_status + '<br><br>' +
                            'You will then receive calls every ' + data.interval_minutes + ' minutes.<br>' +
                            'Personality: ' + data.personality +
                            '</div>';
                        loadUsers(); // Refresh user list
                    } else {
                        throw new Error(result.detail || 'Registration failed');
                    }
                } catch (error) {
                    document.getElementById('registerStatus').innerHTML = 
                        '<div class="error-msg">Error: ' + error.message + '</div>';
                }
            });
            
            // Load all users
            async function loadUsers() {
                try {
                    const response = await fetch('/users');
                    const users = await response.json();
                    
                    const usersList = document.getElementById('usersList');
                    
                    if (users.length === 0) {
                        usersList.innerHTML = '<div class="empty-state">No users registered yet</div>';
                        return;
                    }
                    
                    usersList.innerHTML = users.map(user => `
                        <div class="user-card">
                            <h3>
                                ${user.phone_number} 
                                <span class="badge ${user.is_active ? 'badge-active' : 'badge-inactive'}">
                                    ${user.is_active ? 'Active' : 'Inactive'}
                                </span>
                            </h3>
                            <div class="user-info"><strong>Personality:</strong> ${user.personality.charAt(0).toUpperCase() + user.personality.slice(1)}</div>
                            <div class="user-info"><strong>Interval:</strong> ${user.interval_minutes} minutes</div>
                            <div class="user-info"><strong>Next Call:</strong> ${user.next_call_time || 'Not scheduled'}</div>
                            <div class="user-actions">
                                ${user.is_active ? 
                                    `<a onclick="toggleUser(${user.id}, false)">Deactivate</a>` :
                                    `<a onclick="toggleUser(${user.id}, true)">Activate</a>`
                                }
                                <span class="separator">|</span>
                                <a onclick="callNow(${user.id})">Call Now</a>
                                <span class="separator">|</span>
                                <a onclick="deleteUser(${user.id})" style="color: #dc2626;">Delete</a>
                            </div>
                        </div>
                    `).join('');
                } catch (error) {
                    document.getElementById('usersList').innerHTML = 
                        '<div class="error-msg">Error loading users: ' + error.message + '</div>';
                }
            }
            
            // Toggle user active/inactive
            async function toggleUser(userId, activate) {
                try {
                    const endpoint = activate ? '/start' : '/stop';
                    const response = await fetch(endpoint, {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                        },
                        body: JSON.stringify({ user_id: userId })
                    });
                    
                    const result = await response.json();
                    
                    if (response.ok) {
                        alert(result.message);
                        loadUsers(); // Refresh list
                    } else {
                        throw new Error(result.detail || 'Operation failed');
                    }
                } catch (error) {
                    alert('Error: ' + error.message);
                }
            }
            
            // Call user now
            async function callNow(userId) {
                if (!confirm('Trigger a call to this user now?')) return;
                
                try {
                    const response = await fetch('/call-now', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                        },
                        body: JSON.stringify({ user_id: parseInt(userId) })
                    });
                    
                    if (response.ok) {
                        const result = await response.json();
                        alert(result.message);
                    } else {
                        const result = await response.json();
                        throw new Error(result.detail || 'Call failed');
                    }
                } catch (error) {
                    alert('Error: ' + error.message);
                }
            }
            
            // Delete user
            async function deleteUser(userId) {
                if (!confirm('Are you sure you want to delete this user?')) return;
                
                try {
                    const response = await fetch('/delete-user', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                        },
                        body: JSON.stringify({ user_id: parseInt(userId) })
                    });
                    
                    if (response.ok) {
                        const result = await response.json();
                        alert(result.message);
                        loadUsers(); // Refresh list
                    } else {
                        const result = await response.json();
                        throw new Error(result.detail || 'Delete failed');
                    }
                } catch (error) {
                    alert('Error: ' + error.message);
                }
            }
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
