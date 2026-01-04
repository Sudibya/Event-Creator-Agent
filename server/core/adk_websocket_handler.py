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
WebSocket handler with ADK Live Agent integration
OPTIMIZED: Binary frames, audio deduplication, 1-second response time
"""

import logging
import json
import asyncio
import base64
import traceback
import hashlib
import time
from typing import Any, Set

from core.adk_agent import get_adk_agent

logger = logging.getLogger(__name__)

# Audio sample rate (24kHz for native Gemini format)
VOICE_SAMPLE_RATE = 24000


class SessionState:
    """Holds mutable state for a voice session (from Bondly pattern)"""
    def __init__(self):
        self.sent_audio_hashes: Set[str] = set()
        self.is_agent_speaking: bool = False
        self.last_audio_send_time: float = 0.0
        self.audio_sequence: int = 0
        self.max_hash_cache_size: int = 100


async def send_error_message(websocket: Any, error_data: dict) -> None:
    """Send formatted error message to client."""
    try:
        await websocket.send(json.dumps({
            "type": "error",
            "data": error_data
        }))
    except Exception as e:
        logger.error(f"Failed to send error message: {e}")


async def handle_adk_client(websocket: Any) -> None:
    """Handles a new client connection with ADK Live Agent."""
    session_id = str(id(websocket))
    logger.info(f"connection open - session: {session_id}")

    # Get ADK agent instance
    adk_agent = get_adk_agent()

    # ===== AUDIO OVERLAP PREVENTION (from Bondly) =====
    state = SessionState()  # Mutable state container

    try:
        # Initialize agent if needed
        if not adk_agent.initialized:
            await adk_agent.initialize()

        # Start live stream
        event_stream = await adk_agent.run_live_stream(session_id)
        logger.info(f"Event stream obtained from ADK agent for session: {session_id}")

        # Send ready message to client
        await websocket.send(json.dumps({"ready": True}))
        logger.info(f"New ADK session started: {session_id}")

        # Create task group for bidirectional communication
        async with asyncio.TaskGroup() as tg:
            # Task 1: Handle incoming messages from client
            tg.create_task(handle_client_messages(websocket, adk_agent, session_id))

            # Task 2: Handle events from ADK agent with audio deduplication
            tg.create_task(handle_agent_events(websocket, event_stream, adk_agent, state))

    except* Exception as eg:
        for exc in eg.exceptions:
            if "connection closed" in str(exc).lower():
                logger.info(f"WebSocket connection closed for session {session_id}")
            else:
                logger.error(f"Error in handle_adk_client: {exc}")
                logger.error(f"Full traceback:\n{traceback.format_exc()}")
    finally:
        # Cleanup
        await adk_agent.cleanup(session_id)
        logger.info(f"Session {session_id} cleaned up and ended")


async def handle_client_messages(websocket: Any, adk_agent: Any, session_id: str) -> None:
    """Handle incoming messages from the client."""
    try:
        async for message in websocket:
            try:
                data = json.loads(message)

                if "type" not in data:
                    continue

                msg_type = data["type"]

                if msg_type == "audio":
                    # Decode base64 audio and send to agent
                    logger.debug("Sending audio to ADK agent...")
                    audio_data = base64.b64decode(data.get("data", ""))
                    adk_agent.send_audio(audio_data, sample_rate=24000)  # ← Changed to 24kHz

                elif msg_type == "text":
                    # Send text message to agent
                    logger.info(f"Sending text to ADK agent: {data.get('data')}")
                    adk_agent.send_text(data.get("data"))
                    adk_agent.send_end_of_turn()

                elif msg_type == "end":
                    # Signal end of turn (user stopped speaking)
                    logger.info("Received end signal - marking end of turn")
                    adk_agent.send_end_of_turn()

                else:
                    logger.warning(f"Unsupported message type: {msg_type}")

            except Exception as e:
                logger.error(f"Error handling client message: {e}")
                logger.error(f"Full traceback:\n{traceback.format_exc()}")

    except Exception as e:
        if "connection closed" not in str(e).lower():
            logger.error(f"WebSocket connection error: {e}")
        raise


async def handle_agent_events(
    websocket: Any,
    event_stream: Any,
    adk_agent: Any,
    state: SessionState
) -> None:
    """
    Handle events from ADK agent and forward to client.
    ENHANCED with Bondly's audio deduplication and binary frames.
    """
    logger.info("Starting to listen for events from ADK agent...")
    try:
        async for event in event_stream:
            try:
                logger.info(f"Received event from ADK: {type(event)}")

                # Handle audio transcription (text version of audio output)
                if hasattr(event, 'output_transcription') and event.output_transcription:
                    # Extract text from Transcription object
                    transcription_text = event.output_transcription.text if hasattr(event.output_transcription, 'text') else str(event.output_transcription)
                    logger.info(f"Audio transcription: {transcription_text}")
                    await websocket.send(json.dumps({
                        "type": "transcription",
                        "data": transcription_text
                    }))

                # Handle model response with audio or text
                if event.content and event.content.parts:
                    for part in event.content.parts:
                        # Audio response with BONDLY'S OVERLAP PREVENTION
                        if hasattr(part, 'inline_data') and part.inline_data:
                            audio_data = part.inline_data.data
                            chunk_size = len(audio_data)

                            # Skip empty audio chunks
                            if chunk_size == 0:
                                logger.warning("Skipping empty audio chunk")
                                continue

                            # Calculate hash of audio chunk to detect duplicates
                            audio_hash = hashlib.md5(audio_data).hexdigest()

                            # Skip if we've already sent this exact audio chunk
                            if audio_hash in state.sent_audio_hashes:
                                logger.warning(f"⚠️ Skipping duplicate audio chunk (hash: {audio_hash[:8]}...)")
                                continue

                            # Throttle audio sends to prevent flooding (max 100 chunks/sec)
                            current_time = time.time()
                            time_since_last = current_time - state.last_audio_send_time
                            if time_since_last < 0.01:  # 10ms minimum between chunks (faster!)
                                await asyncio.sleep(0.01 - time_since_last)

                            # Mark agent as speaking
                            if not state.is_agent_speaking:
                                state.is_agent_speaking = True
                                logger.info("🎙️ Agent started speaking")

                            logger.info(f"📤 Sending audio chunk #{state.audio_sequence}: {chunk_size} bytes")

                            # OPTION 1: Binary WebSocket frame (more efficient - 33% smaller)
                            try:
                                await websocket.send(audio_data)
                                logger.debug(f"✅ Sent as BINARY frame")
                            except Exception as e:
                                # FALLBACK: JSON with base64 (for compatibility)
                                logger.warning(f"Binary send failed ({e}), falling back to base64")
                                audio_base64 = base64.b64encode(audio_data).decode('utf-8')
                                await websocket.send(json.dumps({
                                    "type": "audio",
                                    "data": audio_base64
                                }))

                            # Track this audio chunk
                            state.sent_audio_hashes.add(audio_hash)
                            state.last_audio_send_time = time.time()
                            state.audio_sequence += 1

                            # Limit hash cache size to prevent memory growth
                            if len(state.sent_audio_hashes) > state.max_hash_cache_size:
                                # Remove oldest hashes (first 20)
                                hashes_to_remove = list(state.sent_audio_hashes)[:20]
                                for h in hashes_to_remove:
                                    state.sent_audio_hashes.discard(h)

                        # Text response (when response_modalities includes TEXT)
                        if hasattr(part, 'text') and part.text:
                            logger.info(f"Model text: {part.text}")
                            await websocket.send(json.dumps({
                                "type": "text",
                                "data": part.text
                            }))

                # Handle function calls
                if hasattr(event, 'tool_call') and event.tool_call:
                    for function_call in event.tool_call.function_calls:
                        logger.info(f"Function call: {function_call.name}")
                        await websocket.send(json.dumps({
                            "type": "function_call",
                            "data": {
                                "name": function_call.name,
                                "args": function_call.args
                            }
                        }))

                # Handle function responses - DON'T forward them back
                # ADK executes MCP tools automatically and includes results in events
                # Live API will receive the results through the normal event stream
                if hasattr(event, 'get_function_responses'):
                    function_responses = event.get_function_responses()
                    if function_responses:
                        logger.info(f"Detected {len(function_responses)} tool responses (auto-executed by ADK)")
                        for fr in function_responses:
                            logger.info(f"  Tool: {fr.name}, response type: {type(fr.response)}")
                        # Don't forward - ADK already handled it internally
                        logger.info(f"Tool responses handled by ADK - agent should continue...")

                # Check for turn completion in actions state delta
                if hasattr(event, 'actions') and event.actions:
                    state_delta = event.actions.state_delta if hasattr(event.actions, 'state_delta') else {}

                    if state_delta.get("turn_complete", False):
                        logger.info("🏁 Turn complete")
                        await websocket.send(json.dumps({
                            "type": "turn_complete"
                        }))

                        # Reset speaking state and clear audio cache on turn completion (from Bondly)
                        if state.is_agent_speaking:
                            state.is_agent_speaking = False
                            logger.info("🎙️ Agent stopped speaking")

                        # Clear audio hash cache to free memory
                        state.sent_audio_hashes.clear()
                        state.audio_sequence = 0
                        logger.debug(f"🧹 Cleared audio cache (turn complete)")

                    # Check for interruption - clear audio buffers immediately (from Bondly)
                    if state_delta.get("interrupted", False):
                        logger.info("⚠️ Interruption detected")

                        # Clear audio state immediately on interruption
                        if state.is_agent_speaking:
                            state.is_agent_speaking = False
                            logger.info("🎙️ Agent speaking interrupted")

                        # Clear audio cache to prevent stale audio from playing
                        state.sent_audio_hashes.clear()
                        state.audio_sequence = 0
                        logger.debug(f"🧹 Cleared audio cache (interrupted)")

                        await websocket.send(json.dumps({
                            "type": "interrupted",
                            "data": {
                                "message": "Response interrupted by user input"
                            }
                        }))

            except Exception as e:
                logger.error(f"Error handling agent event: {e}")
                logger.error(f"Full traceback:\n{traceback.format_exc()}")

    except Exception as e:
        if "connection closed" not in str(e).lower():
            logger.error(f"Error in event stream: {e}")
        raise

