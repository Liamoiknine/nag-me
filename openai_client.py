from openai import OpenAI
import json
import os
from typing import List, Dict
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Initialize OpenAI client
client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))

def generate_response(user_input: str, personality: str, conversation_history: List[Dict] = None) -> Dict:
    """
    Generate a response from OpenAI based on user input and personality.
    Returns: {"response": str, "should_end": bool}
    """
    
    # System prompts for each personality (optimized for speed & cost)
    system_prompts = {
        "strict": """You are a strict accountability coach. Reply ONLY with valid JSON (no other text). Format: {"response": "your 30-word message", "should_end": true/false}. Be direct and demanding.""",
        
        "sarcastic": """You are a sarcastic accountability coach. Reply ONLY with valid JSON (no other text). Format: {"response": "your 30-word message", "should_end": true/false}. Use wit to challenge excuses.""",
        
        "supportive": """You are a supportive accountability coach. Reply ONLY with valid JSON (no other text). Format: {"response": "your 30-word message", "should_end": true/false}. Encourage but hold accountable."""
    }
    
    # Build conversation context
    messages = [{"role": "system", "content": system_prompts.get(personality, system_prompts["supportive"])}]
    
    # Add conversation history if available (limit to 3 exchanges for speed)
    if conversation_history:
        for msg in conversation_history[-3:]:  # Keep last 3 exchanges only
            messages.append(msg)
    
    # Add current user input
    messages.append({"role": "user", "content": user_input})
    
    try:
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",  # Cheapest model at $0.0015/1k input, $0.002/1k output
            messages=messages,
            max_tokens=80,  # Reduced from 150 - faster response, lower cost
            temperature=0.7,
            timeout=10,  # 10 second timeout for faster failure
            response_format={"type": "json_object"}  # Force JSON-only output
        )
        
        # Extract the response text (guaranteed to be JSON due to response_format)
        response_text = response.choices[0].message.content.strip()
        
        # Parse the JSON
        try:
            parsed_response = json.loads(response_text)
            # Extract ONLY the "response" field for speaking
            speech_text = parsed_response.get("response", "Let's stay focused on your goals.")
            should_end = parsed_response.get("should_end", False)
            
            return {
                "response": speech_text,  # ONLY the speech text
                "should_end": should_end
            }
        except json.JSONDecodeError as e:
            # Fallback if JSON parsing fails (shouldn't happen with json_object mode)
            return {
                "response": "Let's stay focused on your productivity goals.",
                "should_end": False
            }
            
    except Exception as e:
        # Fallback response if OpenAI fails
        fallback_responses = {
            "strict": "I need you to be more specific about your productivity. What exactly have you accomplished?",
            "sarcastic": "Oh, that's... interesting. Care to elaborate on that excuse?",
            "supportive": "I understand it's challenging. Let's focus on what you can do next."
        }
        
        return {
            "response": fallback_responses.get(personality, "Let's talk about your productivity goals."),
            "should_end": False
        }
