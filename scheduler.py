from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse
import os
from datetime import datetime, timedelta
from database import SessionLocal, get_users_due_for_call, update_user, User
import logging
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize Twilio client
twilio_client = Client(os.getenv('TWILIO_ACCOUNT_SID'), os.getenv('TWILIO_AUTH_TOKEN'))
twilio_phone_number = os.getenv('TWILIO_PHONE_NUMBER')

# Initialize scheduler
scheduler = BackgroundScheduler()

def call_user(user_id: int):
    """Trigger a call to a specific user"""
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if not user or not user.is_active:
            logger.warning(f"User {user_id} not found or not active")
            return
        
        # IMPORTANT: Only verified number can receive calls
        VERIFIED_NUMBER = os.getenv('VERIFIED_PHONE_NUMBER', '+1234567890')
        if user.phone_number != VERIFIED_NUMBER:
            logger.error(f"❌ Cannot call {user.phone_number} - Only {VERIFIED_NUMBER} is verified!")
            logger.error(f"   Twilio will reject calls to unverified numbers.")
            logger.error(f"   Skipping call for user {user_id}")
            return
        
        # Get the webhook URL
        webhook_url = os.getenv('WEBHOOK_BASE_URL', 'http://localhost:8000')
        full_webhook_url = f"{webhook_url}/twilio-call"
        
        logger.info("=" * 60)
        logger.info(f"INITIATING CALL TO USER {user_id}")
        logger.info("=" * 60)
        logger.info(f"  To: {user.phone_number} ✅ VERIFIED")
        logger.info(f"  From: {twilio_phone_number}")
        logger.info(f"  Webhook URL: {full_webhook_url}")
        logger.info(f"  Personality: {user.personality}")
        
        # Make the call
        call = twilio_client.calls.create(
            to=user.phone_number,
            from_=twilio_phone_number,
            url=full_webhook_url
        )
        
        logger.info(f"✅ Call created successfully!")
        logger.info(f"  Call SID: {call.sid}")
        logger.info(f"  Status: {call.status}")
        logger.info("=" * 60)
        
        # Update next call time
        next_call_time = datetime.utcnow() + timedelta(minutes=user.interval_minutes)
        update_user(db, user_id, next_call_time=next_call_time)
        
    except Exception as e:
        logger.error(f"❌ Error calling user {user_id}: {str(e)}", exc_info=True)
    finally:
        db.close()

def check_and_trigger_calls():
    """Background job that runs every minute to check for due calls"""
    db = SessionLocal()
    try:
        users_due = get_users_due_for_call(db)
        logger.info(f"Found {len(users_due)} users due for calls")
        
        for user in users_due:
            logger.info(f"Triggering call for user {user.id} ({user.phone_number})")
            call_user(user.id)
            
    except Exception as e:
        logger.error(f"Error in check_and_trigger_calls: {str(e)}")
    finally:
        db.close()

def start_scheduler():
    """Start the background scheduler"""
    scheduler.add_job(
        check_and_trigger_calls,
        trigger=IntervalTrigger(minutes=1),
        id='check_calls',
        name='Check for due calls',
        replace_existing=True
    )
    scheduler.start()
    logger.info("Scheduler started")

def stop_scheduler():
    """Stop the background scheduler"""
    scheduler.shutdown()
    logger.info("Scheduler stopped")
