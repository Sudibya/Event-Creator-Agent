# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
ADK-based Live Agent with WebSocket integration
"""

import logging
import os
import requests
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import datetime, timedelta
from dotenv import load_dotenv
from google.adk.agents import Agent, LiveRequestQueue

# Load environment variables
load_dotenv()
from google.adk.runners import Runner
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.genai import types
from google import genai
import vertexai

# MCP Toolset imports (kept for future use)
# from google.adk.tools.mcp_tool import MCPToolset, StreamableHTTPConnectionParams, StdioConnectionParams
# from mcp import StdioServerParameters

logger = logging.getLogger(__name__)

# Configuration from environment
PROJECT_ID = os.getenv("PROJECT_ID", "vertex-adk")
LOCATION = os.getenv("VERTEX_LOCATION", "us-central1")
USE_VERTEX = os.getenv("VERTEX_API", "false").lower() == "true"
MODEL = "gemini-2.5-flash-native-audio-preview-09-2025"
VOICE_NAME = "Aoede"  # Matching reference implementation default voice

# n8n Webhook URL for scheduling
N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL", "https://sudibyajyoti.app.n8n.cloud/webhook/schedule-meeting")

# Other available voices you can use (subject to model and region support):
# - "Zephyr"         # Natural, expressive default
# - "Aoede"          # Clear, young adult female (used in many Gemini demos)
# - "Charon"         # Calm, neutral male
# - "Puck"           # Friendly, conversational
# - "Orpheus"        # Deeper male, slight classic delivery
# - "Euterpe"        # Upbeat, articulate
# - "Calliope"       # Warm, nurturing
# - "Thalia"         # Youthful, positive
# - "Urania"         # Elegant, slightly formal
# - "Polyhymnia"     # Relaxed, storyteller
# - "Clio"           # Confident, engaging
# 
# These names correspond to voice configs available in Google's Vertex AI, Gemini, or other GenAI services.
# See the documentation or Google Cloud Console for the latest list of supported voices in your region/model.


# System instructions for the voice scheduling agent
SYSTEM_INSTRUCTIONS = """You are Ava, an event scheduling assistant.

START by saying: "Hi! What kind of event would you like to schedule today?"

COLLECT ONE AT A TIME:
1. Event purpose → becomes title
2. Name
3. Email
4. Date
5. Time
6. Duration (default 30 min)

CONFIRM before creating: "So that's [title] for [name], invite to [email], on [date] at [time] for [duration] minutes. Correct?"

AFTER SUCCESS: "Done! Sent confirmation to [email]. Need anything else?"

RULES: Short responses. One question at a time. Always confirm first.

---

**TOOL: schedule_meeting_sync**

Call this function after user confirms. Parameters:

| Parameter | Type | Required | Format | Example |
|-----------|------|----------|--------|---------|
| name | string | Yes | User's name | "John Smith" |
| email | string | Yes | Valid email | "john@example.com" |
| date | string | Yes | YYYY-MM-DD | "2025-01-15" |
| meeting_time | string | Yes | HH:MM (24-hour) | "14:00" |
| title | string | No | Event title | "Project Discussion" |
| duration | integer | No | Minutes (default 30) | 30 |

**DATE CONVERSION:**
- "tomorrow" → add 1 day to today's date, format as YYYY-MM-DD
- "next Monday" → calculate the date, format as YYYY-MM-DD
- "January 15th" → "2025-01-15"
- "the 20th" → 20th of current/next month as YYYY-MM-DD

**TIME CONVERSION:**
- "2pm" or "2 PM" → "14:00"
- "10:30am" → "10:30"
- "3 o'clock" → ask if AM or PM, then convert
- "noon" → "12:00"
- "9 in the morning" → "09:00"

**EXAMPLE TOOL CALL:**
schedule_meeting_sync(
    name="John Smith",
    email="john@example.com",
    date="2025-01-15",
    meeting_time="14:00",
    title="Project Discussion",
    duration=30
)

**ON SUCCESS:** Tell user the meeting is created and confirmation email sent.
**ON ERROR:** Apologize and offer to try again.
"""


# Thread pool for non-blocking HTTP calls
_executor = ThreadPoolExecutor(max_workers=2)


def _call_n8n_webhook(payload: dict) -> dict:
    """Make HTTP call to n8n webhook (runs in separate thread)."""
    thread_start = time.time()
    logger.info(f"🔌 [WEBHOOK-THREAD] Starting HTTP call to n8n webhook")
    logger.info(f"🔌 [WEBHOOK-THREAD] URL: {N8N_WEBHOOK_URL}")
    logger.info(f"🔌 [WEBHOOK-THREAD] Payload: {payload}")

    try:
        response = requests.post(
            N8N_WEBHOOK_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=15
        )
        thread_elapsed = time.time() - thread_start
        logger.info(f"✅ [WEBHOOK-THREAD] HTTP call completed in {thread_elapsed:.2f}s")
        logger.info(f"✅ [WEBHOOK-THREAD] Status: {response.status_code}")
        logger.info(f"✅ [WEBHOOK-THREAD] Response: {response.text[:500]}")
        return {"status_code": response.status_code, "data": response.json()}
    except Exception as e:
        thread_elapsed = time.time() - thread_start
        logger.error(f"❌ [WEBHOOK-THREAD] HTTP call failed after {thread_elapsed:.2f}s: {e}")
        logger.error(f"❌ [WEBHOOK-THREAD] Traceback: {traceback.format_exc()}")
        raise


# Schedule Meeting Tool Function
def schedule_meeting_sync(
    name: str,
    email: str,
    date: str,
    meeting_time: str,
    title: str,
    duration: int = 30
) -> dict:
    """Schedule a calendar meeting with Google Meet link.

    Args:
        name: The name of the person to schedule the meeting with
        email: The email address of the attendee
        date: The date for the meeting (YYYY-MM-DD)
        meeting_time: The time for the meeting (HH:MM in 24-hour format)
        title: Optional title for the meeting
        duration: Duration in minutes (default: 30)

    Returns:
        Dictionary with meeting details or error message
    """
    tool_start = time.time()
    logger.info("=" * 60)
    logger.info("🔧 [TOOL-CALL] schedule_meeting_sync INVOKED")
    logger.info("=" * 60)
    logger.info(f"🔧 [TOOL-CALL] Parameters received:")
    logger.info(f"    - name: {name}")
    logger.info(f"    - email: {email}")
    logger.info(f"    - date: {date}")
    logger.info(f"    - meeting_time: {meeting_time}")
    logger.info(f"    - title: {title}")
    logger.info(f"    - duration: {duration}")

    try:
        # Parse and format datetime
        logger.info(f"🔧 [TOOL-CALL] Parsing datetime: {date} {meeting_time}")
        meeting_datetime = datetime.strptime(f"{date} {meeting_time}", "%Y-%m-%d %H:%M")
        end_datetime = meeting_datetime + timedelta(minutes=duration)

        # Format for Google Calendar API
        start_iso = meeting_datetime.strftime("%Y-%m-%dT%H:%M:%S")
        end_iso = end_datetime.strftime("%Y-%m-%dT%H:%M:%S")
        logger.info(f"🔧 [TOOL-CALL] Parsed datetime: start={start_iso}, end={end_iso}")

        # Prepare request payload
        payload = {
            "name": name,
            "attendee_email": email,
            "title": title or f"Meeting with {name}",
            "start_datetime": start_iso,
            "end_datetime": end_iso,
            "duration": duration
        }

        logger.info(f"🔧 [TOOL-CALL] Prepared payload: {payload}")
        logger.info(f"🔧 [TOOL-CALL] Submitting to ThreadPoolExecutor...")

        # Call n8n webhook in thread pool (non-blocking)
        submit_start = time.time()
        future = _executor.submit(_call_n8n_webhook, payload)
        logger.info(f"🔧 [TOOL-CALL] Future submitted, waiting for result (timeout=20s)...")

        try:
            response_data = future.result(timeout=20)
            wait_elapsed = time.time() - submit_start
            logger.info(f"🔧 [TOOL-CALL] Future completed in {wait_elapsed:.2f}s")
        except FuturesTimeoutError:
            wait_elapsed = time.time() - submit_start
            logger.error(f"❌ [TOOL-CALL] Future TIMEOUT after {wait_elapsed:.2f}s")
            return {
                "success": False,
                "message": "The scheduling service took too long to respond. Please try again.",
                "error": "Timeout waiting for n8n webhook"
            }

        status_code = response_data["status_code"]
        result = response_data["data"]
        logger.info(f"🔧 [TOOL-CALL] Response status: {status_code}")
        logger.info(f"🔧 [TOOL-CALL] Response data: {result}")

        if status_code == 200 and result.get("success"):
            tool_elapsed = time.time() - tool_start
            logger.info(f"✅ [TOOL-CALL] SUCCESS - Total time: {tool_elapsed:.2f}s")
            success_result = {
                "success": True,
                "message": f"Meeting scheduled successfully with {name}!",
                "meeting_title": result.get("event_title") or payload["title"],
                "date": date,
                "meeting_time": meeting_time,
                "duration": f"{duration} minutes",
                "google_meet_link": result.get("meet_link", ""),
                "calendar_link": result.get("calendar_link", ""),
                "confirmation_sent_to": email
            }
            logger.info(f"✅ [TOOL-CALL] Returning: {success_result}")
            logger.info("=" * 60)
            return success_result
        else:
            tool_elapsed = time.time() - tool_start
            logger.error(f"❌ [TOOL-CALL] FAILED - n8n returned error - Total time: {tool_elapsed:.2f}s")
            logger.error(f"❌ [TOOL-CALL] Error details: {result}")
            failure_result = {
                "success": False,
                "message": "Sorry, I couldn't schedule the meeting. Please try again.",
                "error": result.get("error", "Unknown error")
            }
            logger.info(f"❌ [TOOL-CALL] Returning: {failure_result}")
            logger.info("=" * 60)
            return failure_result

    except ValueError as e:
        tool_elapsed = time.time() - tool_start
        logger.error(f"❌ [TOOL-CALL] ValueError after {tool_elapsed:.2f}s: {e}")
        logger.error(f"❌ [TOOL-CALL] Traceback: {traceback.format_exc()}")
        return {
            "success": False,
            "message": "Invalid date or time format. Please use YYYY-MM-DD for date and HH:MM for time.",
            "error": str(e)
        }
    except requests.RequestException as e:
        tool_elapsed = time.time() - tool_start
        logger.error(f"❌ [TOOL-CALL] RequestException after {tool_elapsed:.2f}s: {e}")
        logger.error(f"❌ [TOOL-CALL] Traceback: {traceback.format_exc()}")
        return {
            "success": False,
            "message": "Sorry, I couldn't connect to the scheduling service. Please try again later.",
            "error": str(e)
        }
    except Exception as e:
        tool_elapsed = time.time() - tool_start
        logger.error(f"❌ [TOOL-CALL] UNEXPECTED ERROR after {tool_elapsed:.2f}s: {e}")
        logger.error(f"❌ [TOOL-CALL] Exception type: {type(e).__name__}")
        logger.error(f"❌ [TOOL-CALL] Traceback: {traceback.format_exc()}")
        return {
            "success": False,
            "message": "An unexpected error occurred while scheduling the meeting.",
            "error": str(e)
        }


class ADKLiveAgent:
    """ADK-based Live Agent for WebSocket streaming"""

    def __init__(self):
        self.agent = None
        self.runner = None
        self.session_service = None
        self.session = None
        self.live_request_queue = None
        self.initialized = False

    async def initialize(self):
        """Initialize ADK agent, runner, and session"""
        try:
            # Initialize GenAI client
            if USE_VERTEX:
                logger.info(f"Initializing Vertex AI with project: {PROJECT_ID}, location: {LOCATION}")
                vertexai.init(project=PROJECT_ID, location=LOCATION)
                genai.Client(project=PROJECT_ID, location=LOCATION, vertexai=True)
            else:
                logger.info("Using Google AI API key")
                api_key = os.getenv("GOOGLE_API_KEY")
                if not api_key:
                    raise ValueError("GOOGLE_API_KEY not found in environment")
                genai.Client(api_key=api_key)

            # Create ADK agent with tools
            logger.info("=" * 60)
            logger.info("🤖 [AGENT-INIT] Creating ADK Agent with tools")
            logger.info("=" * 60)
            logger.info(f"🤖 [AGENT-INIT] Model: {MODEL}")
            logger.info(f"🤖 [AGENT-INIT] Tools: [schedule_meeting_sync]")
            logger.info(f"🤖 [AGENT-INIT] Tool function signature: schedule_meeting_sync(name, email, date, meeting_time, title, duration=30)")

            self.agent = Agent(
                name="voice_scheduling_agent",
                model=MODEL,
                instruction=SYSTEM_INSTRUCTIONS,
                tools=[
                    schedule_meeting_sync,
                ],
            )

            logger.info(f"✅ [AGENT-INIT] Agent created successfully")
            logger.info(f"🤖 [AGENT-INIT] Agent name: {self.agent.name}")
            logger.info("=" * 60)

            # Create session service
            self.session_service = InMemorySessionService()

            # Create runner
            self.runner = Runner(
                app_name="livewire_audio_assistant",
                agent=self.agent,
                session_service=self.session_service,
            )

            logger.info("ADK Agent initialized successfully")
            self.initialized = True

        except Exception as e:
            logger.error(f"Failed to initialize ADK agent: {e}")
            raise

    async def create_session(self, session_id: str):
        """Create a new session for a client"""
        try:
            self.session = await self.session_service.create_session(
                app_name="livewire_audio_assistant",
                user_id=f"user_{session_id}",
                session_id=session_id
            )
            logger.info(f"Created session: {session_id}")
            return self.session
        except Exception as e:
            logger.error(f"Failed to create session: {e}")
            raise

    def create_live_request_queue(self):
        """Create a new LiveRequestQueue for streaming"""
        self.live_request_queue = LiveRequestQueue()
        return self.live_request_queue

    def get_run_config(self, sample_rate: int = 16000, low_latency: bool = True):
        """
        Get RunConfig for live streaming with ANTI-INTERRUPTION and LATENCY settings

        Args:
            sample_rate: Audio sample rate (default: 16000 Hz)
            low_latency: If True, optimizes for latency over interruption prevention

        This configuration balances:
        - Lower VAD sensitivity (prevents interruptions)
        - Fast response generation (reduces latency)
        - Streaming audio output (eliminates buffering)
        """
        # Try to import optimized config helper
        try:
            import sys
            from pathlib import Path
            config_path = Path(__file__).parent.parent / "config"
            if str(config_path) not in sys.path:
                sys.path.insert(0, str(config_path))

            from gemini_config import get_optimized_run_config

            # Use HIGH sensitivity for 1-second latency
            sensitivity = "HIGH"  # Always use HIGH for fastest response
            logger.info(f"Using optimized RunConfig with HIGH VAD for 1-second latency")
            return get_optimized_run_config(
                voice_name=VOICE_NAME,
                language_code="en-US",  # Force English
                disable_automatic_vad=False,
                vad_sensitivity=sensitivity,  # MEDIUM = balanced latency/interruption
                for_twilio=True,  # 🔥 NEW: Enable Twilio-specific optimizations
                max_output_tokens=256  # Very short responses for 1-second latency
            )
        except Exception as e:
            logger.warning(f"Could not import optimized config ({e}), using safer default")

            # Fallback with balanced settings
            return RunConfig(
                streaming_mode=StreamingMode.BIDI,
                speech_config=types.SpeechConfig(
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=VOICE_NAME)
                    )
                ),
                response_modalities=[types.Modality.AUDIO],  # ✅ Must use enum, not string
                output_audio_transcription=types.AudioTranscriptionConfig(),
                input_audio_transcription=types.AudioTranscriptionConfig(),
            )

    async def run_live_stream(self, session_id: str, low_latency: bool = True):
        """
        Start live streaming with ADK Runner

        Args:
            session_id: Unique session identifier
            low_latency: If True, optimize for latency over interruption prevention

        Returns:
            AsyncIterator of events from the agent
        """
        logger.info("=" * 60)
        logger.info("🎙️ [LIVE-STREAM] Starting live stream")
        logger.info("=" * 60)
        logger.info(f"🎙️ [LIVE-STREAM] Session ID: {session_id}")
        logger.info(f"🎙️ [LIVE-STREAM] Low latency mode: {low_latency}")

        if not self.initialized:
            logger.info("🎙️ [LIVE-STREAM] Agent not initialized, initializing now...")
            await self.initialize()

        # Create session if not exists
        if not self.session or self.session.id != session_id:
            logger.info(f"🎙️ [LIVE-STREAM] Creating new session: {session_id}")
            await self.create_session(session_id)

        # Create live request queue
        logger.info("🎙️ [LIVE-STREAM] Creating live request queue...")
        self.create_live_request_queue()

        # Get run config with latency optimization
        logger.info("🎙️ [LIVE-STREAM] Getting run config...")
        run_config = self.get_run_config(low_latency=low_latency)

        logger.info(f"🎙️ [LIVE-STREAM] Run config ready")
        logger.info(f"🎙️ [LIVE-STREAM] Starting runner.run_live()...")

        # Start the event stream
        event_stream = self.runner.run_live(
            session=self.session,
            live_request_queue=self.live_request_queue,
            run_config=run_config,
        )

        logger.info(f"✅ [LIVE-STREAM] Event stream created successfully")
        logger.info("=" * 60)

        # Don't send kickstart - let audio trigger the stream
        return event_stream

    def send_audio(self, audio_data: bytes, sample_rate: int = 24000):
        """Send audio data to the agent"""
        if self.live_request_queue:
            # Send audio directly as Blob (original working format)
            self.live_request_queue.send_realtime(
                types.Blob(
                    data=audio_data,
                    mime_type=f"audio/pcm;rate={sample_rate}",
                )
            )

    def send_text(self, text: str):
        """Send text input to the agent"""
        if self.live_request_queue:
            self.live_request_queue.send_realtime(types.LiveClientRealtimeInput(text=text))

    def send_end_of_turn(self):
        """Signal end of turn - CRITICAL for 1-second latency with manual VAD"""
        if self.live_request_queue:
            logger.info("⚡ Sending end_of_turn signal for ultra-fast response")
            self.live_request_queue.send_realtime(types.LiveClientRealtimeInput(audio_stream_end=True))
            logger.info("✅ End of turn sent - expecting response in <1 second")

    def send_tool_response(self, function_responses: list):
        """Send tool responses back to the Live API

        Args:
            function_responses: List of FunctionResponse objects from tool execution
        """
        logger.info("=" * 60)
        logger.info("📤 [TOOL-RESPONSE] Sending tool response back to Live API")
        logger.info("=" * 60)

        if self.live_request_queue and function_responses:
            logger.info(f"📤 [TOOL-RESPONSE] Number of responses: {len(function_responses)}")
            for i, fr in enumerate(function_responses):
                logger.info(f"📤 [TOOL-RESPONSE] Response {i+1}:")
                logger.info(f"    - name: {fr.name}")
                logger.info(f"    - id: {getattr(fr, 'id', 'N/A')}")
                logger.info(f"    - response: {getattr(fr, 'response', 'N/A')}")

            tool_response = types.LiveClientToolResponse(function_responses=function_responses)
            message = types.LiveClientMessage(tool_response=tool_response)
            logger.info(f"📤 [TOOL-RESPONSE] Sending message to live_request_queue...")
            self.live_request_queue.send(message)
            logger.info(f"✅ [TOOL-RESPONSE] Tool responses sent successfully")
            logger.info("=" * 60)
        else:
            logger.warning(f"⚠️ [TOOL-RESPONSE] Cannot send - queue={self.live_request_queue is not None}, responses={len(function_responses) if function_responses else 0}")
            logger.info("=" * 60)

    async def cleanup(self, session_id: str):
        """Clean up session resources"""
        try:
            if self.session and self.session.id == session_id:
                # Cancel any pending operations
                if self.live_request_queue:
                    self.live_request_queue = None

                logger.info(f"Cleaned up session: {session_id}")
        except Exception as e:
            logger.error(f"Error during cleanup: {e}")


# Global ADK agent instance
_adk_agent = None

def get_adk_agent() -> ADKLiveAgent:
    """Get or create the global ADK agent instance"""
    global _adk_agent
    if _adk_agent is None:
        _adk_agent = ADKLiveAgent()
    return _adk_agent

