from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime, timedelta
import os

DATABASE_URL = "sqlite:///./voice_accountability.db"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True, index=True)
    phone_number = Column(String, unique=True, index=True)
    interval_minutes = Column(Integer)
    personality = Column(String)  # "strict", "sarcastic", "supportive"
    is_active = Column(Boolean, default=False)
    next_call_time = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)

# Create tables
Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def create_user(db, phone_number: str, interval_minutes: int, personality: str):
    """Create a new user and return the user object"""
    user = User(
        phone_number=phone_number,
        interval_minutes=interval_minutes,
        personality=personality,
        is_active=False,
        next_call_time=datetime.utcnow() + timedelta(minutes=interval_minutes)
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user

def get_user(db, user_id: int):
    """Get user by ID"""
    return db.query(User).filter(User.id == user_id).first()

def get_user_by_phone(db, phone_number: str):
    """Get user by phone number"""
    return db.query(User).filter(User.phone_number == phone_number).first()

def update_user(db, user_id: int, **kwargs):
    """Update user fields"""
    user = db.query(User).filter(User.id == user_id).first()
    if user:
        for key, value in kwargs.items():
            setattr(user, key, value)
        db.commit()
        db.refresh(user)
    return user

def get_active_users(db):
    """Get all active users"""
    return db.query(User).filter(User.is_active == True).all()

def get_users_due_for_call(db):
    """Get users who are due for a call"""
    now = datetime.utcnow()
    return db.query(User).filter(
        User.is_active == True,
        User.next_call_time <= now
    ).all()
