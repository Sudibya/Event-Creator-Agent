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
Twilio Media Streams WebSocket handler
Handles bidirectional audio streaming between Twilio and ADK agent
"""

import logging
import json
import asyncio
import traceback
import os
import time
from typing import Any
from dotenv import load_dotenv

from core.adk_agent import get_adk_agent
from core.audio_converter import twilio_to_adk, adk_to_twilio
from core.audio_batcher import TwilioAudioBatcher

# Load environment variables
load_dotenv()

logger = logging.getLogger(__name__)

# Keepalive configuration
KEEPALIVE_INTERVAL = 20  # Send mark event every 20 seconds
KEEPALIVE_MARK_NAME = "keepalive"

# Latency optimization configuration
LOW_LATENCY_MODE = os.getenv("LOW_LATENCY_MODE", "true").lower() == "true"


async def send_keepalive_marks(websocket: Any, stream_sid: str) -> None:
    """
    Send periodic mark events to Twilio to keep the connection alive.
    This prevents connection timeouts during long conversations.
    """
    try:
        while True:
            await asyncio.sleep(KEEPALIVE_INTERVAL)

            # Send mark event to Twilio
            mark_event = {
                "event": "mark",
                "streamSid": stream_sid,
                "mark": {
                    "name": KEEPALIVE_MARK_NAME
                }
            }

            try:
                await websocket.send_text(json.dumps(mark_event))
                logger.debug(f"Sent keepalive mark for stream {stream_sid}")
            except Exception as e:
                logger.warning(f"Failed to send keepalive mark: {e}")
                break

    except asyncio.CancelledError:
        logger.info("Keepalive task cancelled")
        raise
    except Exception as e:
        logger.error(f"Error in keepalive task: {e}")


async def handle_twilio_call(websocket: Any) -> None:
    """
    Handles a Twilio Media Streams WebSocket connection for phone calls.
    Similar to handle_adk_client but for Twilio protocol.
    """
    call_sid = None
    session_id = None
    stream_sid = None

    logger.info("New Twilio Media Streams connection")

    # Get ADK agent instance
    adk_agent = get_adk_agent()

    try:
        # Initialize agent if needed
        if not adk_agent.initialized:
            await adk_agent.initialize()

        # Wait for 'start' event to get call_sid and stream_sid
        # Then create session and event stream
        call_sid, stream_sid, session_id, event_stream = await initialize_twilio_session(websocket, adk_agent)

        logger.info(f"Twilio call session started: {session_id} (Call SID: {call_sid}, Stream SID: {stream_sid})")

        # 🔥 Create shared state for tracking latency AND silence detection across handlers
        shared_state = {
            'last_audio_sent_to_gemini_time': None,
            'last_audio_activity_time': None,
            'turn_ended': False,
            'is_agent_speaking': False  # Track when AI is speaking for interruption support
        }

        # Create task group for bidirectional communication
        async with asyncio.TaskGroup() as tg:
            # Task 1: Handle incoming audio from Twilio
            tg.create_task(handle_twilio_audio_input(websocket, adk_agent, call_sid, stream_sid, shared_state))

            # Task 2: Handle events from ADK agent and send audio to Twilio
            tg.create_task(handle_adk_audio_output(websocket, event_stream, adk_agent, stream_sid, shared_state))

            # Task 3: Send periodic keepalive marks to prevent connection timeout
            tg.create_task(send_keepalive_marks(websocket, stream_sid))

            # Task 4: REMOVED - Automatic VAD is now enabled, no need for manual silence detector

    except* Exception as eg:
        for exc in eg.exceptions:
            error_str = str(exc).lower()
            # Gracefully handle expected disconnections
            if any(keyword in error_str for keyword in ["connection closed", "disconnect", "timeout", "keepalive", "1006", "1011"]):
                logger.info(f"Call ended gracefully for {call_sid}: {type(exc).__name__}")
            else:
                logger.error(f"Error in handle_twilio_call: {exc}")
                logger.error(f"Full traceback:\n{traceback.format_exc()}")
    finally:
        # Cleanup
        if session_id:
            await adk_agent.cleanup(session_id)
            logger.info(f"Twilio session {session_id} cleaned up")


async def initialize_twilio_session(websocket: Any, adk_agent: Any):
    """
    Wait for Twilio 'start' event and initialize ADK session.

    Returns:
        Tuple of (call_sid, stream_sid, session_id, event_stream)
    """
    while True:
        try:
            # Receive message from FastAPI WebSocket
            message = await websocket.receive_text()
            data = json.loads(message)
            event_type = data.get("event")

            if event_type == "connected":
                logger.info("Twilio Media Streams connected")
                continue

            elif event_type == "start":
                # Extract call metadata
                start_data = data.get("start", {})
                call_sid = start_data.get("callSid")
                stream_sid = start_data.get("streamSid")

                logger.info(f"Call started - SID: {call_sid}, Stream: {stream_sid}")

                # Create session ID
                session_id = f"twilio_{call_sid}"

                # Start ADK live stream with latency optimization
                event_stream = await adk_agent.run_live_stream(session_id, low_latency=LOW_LATENCY_MODE)
                logger.info(f"ADK event stream started for session: {session_id} (low_latency={LOW_LATENCY_MODE})")

                return call_sid, stream_sid, session_id, event_stream

        except Exception as e:
            logger.error(f"Error during Twilio session initialization: {e}")
            raise


async def silence_detector(adk_agent: Any, shared_state: dict, call_sid: str, silence_threshold_sec: float = 2.0) -> None:
    """
    Background task that monitors for silence and sends end-of-turn signal to Gemini.

    With automatic VAD disabled, we must manually tell Gemini when the user has finished speaking.
    This detector waits for {silence_threshold_sec} seconds of silence before signaling turn completion.
    """
    logger.info(f"🔇 Silence detector started (threshold: {silence_threshold_sec}s)")

    try:
        while True:
            await asyncio.sleep(0.5)  # Check every 500ms

            # Check if we have audio activity
            last_activity = shared_state.get('last_audio_activity_time')
            turn_ended = shared_state.get('turn_ended', False)

            if last_activity and not turn_ended:
                silence_duration = time.time() - last_activity

                # If we've been silent for the threshold duration, signal end of turn
                if silence_duration >= silence_threshold_sec:
                    logger.warning(f"🔇 SILENCE DETECTED: {silence_duration:.1f}s - Signaling end of turn to Gemini")
                    adk_agent.send_end_of_turn()

                    # Track when we sent the signal
                    shared_state['last_audio_sent_to_gemini_time'] = time.time()
                    shared_state['turn_ended'] = True  # Prevent multiple signals

    except asyncio.CancelledError:
        logger.info(f"Silence detector cancelled for call {call_sid}")
        raise
    except Exception as e:
        logger.error(f"Error in silence detector: {e}")


async def handle_twilio_audio_input(websocket: Any, adk_agent: Any, call_sid: str, stream_sid: str, shared_state: dict) -> None:
    """
    Handle incoming audio from Twilio Media Streams.
    Uses custom Silero VAD for ultra-low latency (1 second response time).
    """
    # 🔥 ULTRA-FAST VAD: Custom VAD for 1-second latency
    from core.silero_vad import SileroVAD

    vad = SileroVAD(
        sample_rate=24000,  # Match Gemini's native sample rate
        frame_duration_ms=20,
        speech_threshold=0.3,  # Lower threshold for phone audio (was 0.5 default)
        silence_duration_ms=300,  # 300ms silence triggers turn end (balanced for phone)
        min_speech_duration_ms=200  # Minimum 200ms speech to avoid false triggers from noise
    )

    # Audio batcher for efficiency (processes Twilio's 8kHz audio BEFORE conversion)
    audio_batcher = TwilioAudioBatcher(target_duration_ms=20, sample_rate=8000)  # Twilio's native sample rate

    # 📊 LATENCY TRACKING: Track timing for diagnostics
    last_audio_receive_time = None
    first_audio_receive_time = None
    chunks_received = 0

    try:
        while True:
            try:
                # Receive message from FastAPI WebSocket
                message = await websocket.receive_text()
                data = json.loads(message)
                event_type = data.get("event")

                if event_type == "media":
                    # 📊 Track when audio arrives
                    current_time = time.time()
                    if first_audio_receive_time is None:
                        first_audio_receive_time = current_time
                        logger.info(f"📥 First audio chunk received for call {call_sid}")

                    # Extract audio payload
                    media_data = data.get("media", {})
                    audio_payload = media_data.get("payload", "")

                    if audio_payload:
                        chunks_received += 1

                        # 🔥 Update activity timestamp for silence detection
                        shared_state['last_audio_activity_time'] = current_time
                        # Automatic VAD handles turn detection

                        # Convert Twilio audio (base64 μ-law 8kHz) to ADK format (PCM16 16kHz)
                        conversion_start = time.time()
                        adk_audio = twilio_to_adk(audio_payload)
                        conversion_time = (time.time() - conversion_start) * 1000

                        # 🔥 ULTRA-FAST VAD: Process audio for speech detection
                        vad_result = vad.process_audio(adk_audio)

                        # Debug: Log VAD processing every 50 chunks
                        if chunks_received % 50 == 1:
                            logger.info(f"🎙️ VAD Processing: energy={vad_result['energy']:.3f}, threshold={vad_result['threshold']:.3f}, is_speech={vad_result['is_speech']}")

                        # Always send audio to Gemini (continuous stream) at native 24kHz
                        adk_agent.send_audio(adk_audio, sample_rate=24000)

                        # Handle VAD state changes
                        if vad_result['speech_started']:
                            logger.info("🎤 VAD: Speech STARTED")
                            shared_state['speech_active'] = True

                            # 🔥 INTERRUPTION: If AI is speaking, interrupt it immediately
                            if shared_state.get('is_agent_speaking', False):
                                logger.warning("⚠️ USER INTERRUPTED AI - Clearing Twilio audio buffer")

                                # Send "clear" command to Twilio to stop playback immediately
                                await websocket.send_text(json.dumps({
                                    "event": "clear",
                                    "streamSid": stream_sid
                                }))

                                # Mark agent as no longer speaking
                                shared_state['is_agent_speaking'] = False

                                # Notify ADK agent about interruption (optional - Gemini will detect it anyway)
                                logger.info("🎙️ Interruption sent to Twilio - audio cleared")

                        if vad_result['speech_ended']:
                            # CRITICAL: Send end_of_turn IMMEDIATELY for 1-second latency
                            logger.warning(f"🔇 VAD: Speech ENDED! Energy: {vad_result['energy']:.3f}")
                            logger.warning("⚡ Sending end_of_turn NOW for 1-second response")
                            adk_agent.send_end_of_turn()
                            shared_state['speech_active'] = False
                            shared_state['last_audio_sent_to_gemini_time'] = current_time

                        # Debug logging (reduced frequency)
                        if chunks_received % 25 == 1:
                            logger.debug(f"VAD: Energy={vad_result.get('energy', 0):.3f}, Speech={vad_result['is_speech']}")

                        last_audio_receive_time = current_time

                elif event_type == "mark":
                    # Handle Twilio mark events (keepalive)
                    mark_name = data.get("mark", {}).get("name", "unknown")
                    logger.debug(f"Received mark event: {mark_name}")

                elif event_type == "stop":
                    logger.info(f"Twilio stream stopped for call {call_sid}")
                    # 🔥 NEW: Flush any remaining buffered audio before stopping
                    remaining_audio = audio_batcher.flush()
                    if remaining_audio:
                        adk_agent.send_audio(remaining_audio, sample_rate=24000)
                        logger.debug(f"Flushed {len(remaining_audio)} bytes on stream stop")
                    break

            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse Twilio message: {e}")
            except Exception as e:
                # Handle WebSocket disconnection gracefully
                if "disconnect" in str(e).lower() or "1006" in str(e):
                    logger.info(f"Twilio WebSocket disconnected (call ended): {call_sid}")
                    break
                else:
                    logger.error(f"Error handling Twilio audio input: {e}")
                    logger.error(f"Full traceback:\n{traceback.format_exc()}")
                    break

    except asyncio.CancelledError:
        logger.info(f"Twilio audio input handler cancelled for call {call_sid}")
        raise
    except Exception as e:
        if "disconnect" not in str(e).lower() and "connection closed" not in str(e).lower():
            logger.error(f"Twilio WebSocket error: {e}")
        raise


async def handle_adk_audio_output(websocket: Any, event_stream: Any, adk_agent: Any, stream_sid: str, shared_state: dict) -> None:
    """
    Handle events from ADK agent.
    Converts audio and sends to Twilio Media Streams.
    """
    logger.info("Listening for ADK events (Twilio handler)...")
    sequence_number = 0  # Track sequence for audio chunks

    # 📊 LATENCY TRACKING: Track response timing
    first_audio_received_time = None
    last_send_time = None
    session_start_time = time.time()

    try:
        async for event in event_stream:
            try:
                # Handle audio responses
                if event.content and event.content.parts:
                    for part in event.content.parts:
                        # Audio response from ADK
                        if hasattr(part, 'inline_data') and part.inline_data:
                            audio_data = part.inline_data.data

                            # Skip empty audio chunks
                            if len(audio_data) == 0:
                                continue

                            # 📊 Track first audio response
                            current_response_time = time.time()
                            if first_audio_received_time is None:
                                first_audio_received_time = current_response_time
                                time_to_first_audio = (first_audio_received_time - session_start_time) * 1000
                                logger.info(f"🎵 FIRST AUDIO CHUNK from Gemini! Time since session start: {time_to_first_audio:.1f}ms")

                            # 🎙️ Mark agent as speaking (for interruption detection)
                            if not shared_state.get('is_agent_speaking', False):
                                shared_state['is_agent_speaking'] = True
                                logger.info("🎙️ Agent started speaking")

                            # 🔥 NEW: Measure turn-around time (question → answer)
                            if shared_state and 'last_audio_sent_to_gemini_time' in shared_state:
                                last_sent_time = shared_state['last_audio_sent_to_gemini_time']
                                if last_sent_time is not None:
                                    response_latency = (current_response_time - last_sent_time) * 1000
                                    logger.warning(f"⏱️  RESPONSE LATENCY: {response_latency:.1f}ms (from last audio sent to first response)")
                                    logger.warning(f"🔴 USER PERCEIVES: ~{response_latency:.0f}ms delay from finish speaking to hearing response")
                                    # Clear the timer after first response to prepare for next question
                                    shared_state['last_audio_sent_to_gemini_time'] = None

                            sequence_number += 1

                            # Convert ADK audio (PCM16 24kHz) to Twilio format (base64 μ-law 8kHz)
                            conversion_start = time.time()
                            twilio_audio = adk_to_twilio(audio_data)
                            conversion_time = (time.time() - conversion_start) * 1000

                            # 📊 Log every 10th chunk with timing
                            if sequence_number % 10 == 0:
                                logger.info(f"📡 Audio chunk #{sequence_number}: {len(audio_data)} bytes → Twilio (conversion: {conversion_time:.2f}ms)")

                            # Send to Twilio immediately (non-blocking)
                            send_start = time.time()
                            await websocket.send_text(json.dumps({
                                "event": "media",
                                "streamSid": stream_sid,
                                "media": {
                                    "payload": twilio_audio
                                }
                            }))
                            send_time = (time.time() - send_start) * 1000

                            # 📊 Track send timing for first few chunks
                            if sequence_number <= 5:
                                logger.info(f"📤 Sent chunk #{sequence_number} to Twilio in {send_time:.2f}ms")

                            last_send_time = time.time()

                # Log transcriptions
                if hasattr(event, 'output_transcription') and event.output_transcription:
                    transcription_text = event.output_transcription.text if hasattr(
                        event.output_transcription, 'text') else str(event.output_transcription)
                    logger.info(f"ADK output: {transcription_text}")

                # Handle turn completion
                if hasattr(event, 'actions') and event.actions:
                    state_delta = event.actions.state_delta if hasattr(event.actions, 'state_delta') else {}

                    if state_delta.get("turn_complete", False):
                        logger.info("🏁 Turn complete")
                        # Mark agent as no longer speaking
                        if shared_state.get('is_agent_speaking', False):
                            shared_state['is_agent_speaking'] = False
                            logger.info("🎙️ Agent stopped speaking")

                    if state_delta.get("interrupted", False):
                        logger.info("⚠️ Interrupted")
                        # Mark agent as no longer speaking
                        if shared_state.get('is_agent_speaking', False):
                            shared_state['is_agent_speaking'] = False
                            logger.info("🎙️ Agent speaking interrupted")

            except Exception as e:
                logger.error(f"Error handling ADK event: {e}")
                logger.error(f"Full traceback:\n{traceback.format_exc()}")

    except asyncio.CancelledError:
        logger.info("ADK audio output handler cancelled")
        raise
    except Exception as e:
        error_str = str(e).lower()
        # Gracefully handle expected connection closures
        if any(keyword in error_str for keyword in ["connection closed", "keepalive", "timeout", "1011"]):
            logger.info(f"ADK connection closed gracefully: {e}")
        else:
            logger.error(f"Error in ADK event stream: {e}")
        raise
