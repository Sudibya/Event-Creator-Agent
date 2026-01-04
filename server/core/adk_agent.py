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
import asyncio
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

# MCP Toolset imports
from google.adk.tools.mcp_tool import MCPToolset, StreamableHTTPConnectionParams, StdioConnectionParams
from mcp import StdioServerParameters

logger = logging.getLogger(__name__)

# Configuration from environment
PROJECT_ID = os.getenv("PROJECT_ID", "vertex-adk")
LOCATION = os.getenv("VERTEX_LOCATION", "us-central1")
USE_VERTEX = os.getenv("VERTEX_API", "false").lower() == "true"
MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"
VOICE_NAME = "Aoede"  # Matching reference implementation default voice

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


# Load system instructions
def load_system_instructions():
    """Load optimized system instructions from file"""
    try:
        # Try to load optimized instructions first (better for proactive behavior control)
        with open('config/system-instructions-optimized.txt', 'r') as f:
            instructions = f.read()
            logger.info("Loaded optimized system instructions (anti-interruption)")
            return instructions
    except Exception as e:
        logger.warning(f"Failed to load optimized instructions, trying default: {e}")
        try:
            with open('config/system-instructions.txt', 'r') as f:
                return f.read()
        except Exception as e2:
            logger.warning(f"Failed to load system instructions: {e2}")
            return """You are a helpful AI assistant.

**CRITICAL RULES TO PREVENT INTERRUPTION:**
- ALWAYS wait for the user to COMPLETELY finish speaking before responding
- DO NOT interrupt the user mid-sentence
- If you hear a pause, wait an extra moment - they may still be thinking
- Only respond when you're certain the user has finished their complete thought
- Be patient and listen carefully
- ONLY respond in English unless explicitly asked to use another language
"""


# Example tool function
def get_weather(city: str) -> dict:
    """Get weather information for a location.

    Args:
        city: The city or location to get weather for

    Returns:
        Dictionary with weather information
    """
    # Mock implementation - replace with actual API call
    return {
        "city": city,
        "temperature": "72°F",
        "condition": "Sunny",
        "humidity": "45%"
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

            # Load system instructions
            system_instructions = load_system_instructions()

            # Create ADK agent with tools
            self.agent = Agent(
                name="sonu",
                model=MODEL,  # Use MODEL variable from config (line 44)
                instruction=system_instructions,
               
            )

            logger.info(f"Created agent with model: {MODEL}")

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
        if not self.initialized:
            await self.initialize()

        # Create session if not exists
        if not self.session or self.session.id != session_id:
            await self.create_session(session_id)

        # Create live request queue
        self.create_live_request_queue()

        # Get run config with latency optimization
        run_config = self.get_run_config(low_latency=low_latency)

        logger.info(f"Starting live stream for session: {session_id} (low_latency={low_latency})")

        # Start the event stream
        event_stream = self.runner.run_live(
            session=self.session,
            live_request_queue=self.live_request_queue,
            run_config=run_config,
        )

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
        if self.live_request_queue and function_responses:
            tool_response = types.LiveClientToolResponse(function_responses=function_responses)
            message = types.LiveClientMessage(tool_response=tool_response)
            self.live_request_queue.send(message)
            logger.info(f"Sent tool responses: {[fr.name for fr in function_responses]}")

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

