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
    event_count = 0
    try:
        async for event in event_stream:
            event_count += 1
            try:
                # Detailed event logging for debugging
                logger.info(f"📩 [EVENT #{event_count}] Type: {type(event).__name__}")

                # Log all event attributes for debugging
                event_attrs = [attr for attr in dir(event) if not attr.startswith('_')]
                significant_attrs = []
                for attr in event_attrs:
                    try:
                        value = getattr(event, attr)
                        if value is not None and not callable(value):
                            # Only log non-None, non-callable attributes
                            if attr in ['tool_call', 'content', 'actions', 'output_transcription', 'get_function_responses']:
                                significant_attrs.append(f"{attr}={type(value).__name__}")
                    except Exception:
                        pass

                if significant_attrs:
                    logger.info(f"📩 [EVENT #{event_count}] Significant attrs: {', '.join(significant_attrs)}")

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

                # Handle function calls - CRITICAL: This is where tool calls are initiated
                if hasattr(event, 'tool_call') and event.tool_call:
                    logger.info("=" * 60)
                    logger.info("🔧 [WS-HANDLER] TOOL CALL EVENT RECEIVED")
                    logger.info("=" * 60)
                    logger.info(f"🔧 [WS-HANDLER] tool_call object: {event.tool_call}")
                    logger.info(f"🔧 [WS-HANDLER] Number of function_calls: {len(event.tool_call.function_calls)}")

                    for i, function_call in enumerate(event.tool_call.function_calls):
                        logger.info(f"🔧 [WS-HANDLER] Function Call #{i+1}:")
                        logger.info(f"    - name: {function_call.name}")
                        logger.info(f"    - id: {getattr(function_call, 'id', 'N/A')}")
                        logger.info(f"    - args: {function_call.args}")
                        logger.info(f"    - args type: {type(function_call.args)}")

                        # Notify client of function call
                        await websocket.send(json.dumps({
                            "type": "function_call",
                            "data": {
                                "name": function_call.name,
                                "args": function_call.args
                            }
                        }))
                        logger.info(f"🔧 [WS-HANDLER] Sent function_call notification to client")

                    logger.info("=" * 60)

                # Handle function responses - DON'T forward them back
                # ADK executes MCP tools automatically and includes results in events
                # Live API will receive the results through the normal event stream
                if hasattr(event, 'get_function_responses'):
                    function_responses = event.get_function_responses()
                    if function_responses:
                        logger.info("=" * 60)
                        logger.info("📤 [WS-HANDLER] TOOL RESPONSE EVENT RECEIVED")
                        logger.info("=" * 60)
                        logger.info(f"📤 [WS-HANDLER] Number of responses: {len(function_responses)}")

                        for i, fr in enumerate(function_responses):
                            logger.info(f"📤 [WS-HANDLER] Response #{i+1}:")
                            logger.info(f"    - name: {fr.name}")
                            logger.info(f"    - id: {getattr(fr, 'id', 'N/A')}")
                            logger.info(f"    - response type: {type(fr.response)}")
                            logger.info(f"    - response value: {fr.response}")

                        # Don't forward - ADK already handled it internally
                        logger.info(f"📤 [WS-HANDLER] Tool responses handled by ADK - agent should continue...")
                        logger.info("=" * 60)

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
                logger.error(f"❌ [EVENT #{event_count}] Error handling agent event: {e}")
                logger.error(f"❌ [EVENT #{event_count}] Exception type: {type(e).__name__}")
                logger.error(f"❌ [EVENT #{event_count}] Full traceback:\n{traceback.format_exc()}")

    except Exception as e:
        error_str = str(e).lower()
        logger.error("=" * 60)
        logger.error("❌ [EVENT-STREAM] EVENT STREAM ERROR")
        logger.error("=" * 60)
        logger.error(f"❌ [EVENT-STREAM] Exception: {e}")
        logger.error(f"❌ [EVENT-STREAM] Exception type: {type(e).__name__}")
        logger.error(f"❌ [EVENT-STREAM] Total events processed before error: {event_count}")

        if "1011" in error_str:
            logger.error("❌ [EVENT-STREAM] ERROR CODE 1011 DETECTED - Internal server error from Gemini Live API")
            logger.error("❌ [EVENT-STREAM] This typically occurs during or after tool execution")
        elif "connection closed" in error_str:
            logger.info(f"📵 [EVENT-STREAM] Connection closed normally after {event_count} events")
        else:
            logger.error(f"❌ [EVENT-STREAM] Full traceback:\n{traceback.format_exc()}")

        logger.error("=" * 60)
        raise

