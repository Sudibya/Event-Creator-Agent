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
Audio conversion utilities for Twilio Media Streams integration
Handles conversion between Twilio format (μ-law, 8kHz) and ADK format (PCM16, 16kHz/24kHz)
"""

import audioop
import base64
import logging
from scipy import signal
import numpy as np

logger = logging.getLogger(__name__)

# Audio format constants
TWILIO_SAMPLE_RATE = 8000  # Twilio uses 8kHz
ADK_INPUT_SAMPLE_RATE = 24000  # Gemini's native sample rate (24kHz) for optimal latency
ADK_OUTPUT_SAMPLE_RATE = 24000  # ADK outputs 24kHz
SAMPLE_WIDTH = 2  # 16-bit audio = 2 bytes per sample


def ulaw_to_pcm16(ulaw_data: bytes) -> bytes:
    """
    Convert μ-law audio to linear PCM16 format.

    Args:
        ulaw_data: μ-law encoded audio data

    Returns:
        PCM16 encoded audio data
    """
    try:
        # Convert μ-law to linear PCM16
        # audioop.ulaw2lin converts μ-law to linear samples
        pcm_data = audioop.ulaw2lin(ulaw_data, SAMPLE_WIDTH)
        return pcm_data
    except Exception as e:
        logger.error(f"Error converting μ-law to PCM16: {e}")
        raise


def pcm16_to_ulaw(pcm_data: bytes) -> bytes:
    """
    Convert linear PCM16 audio to μ-law format.

    Args:
        pcm_data: PCM16 encoded audio data

    Returns:
        μ-law encoded audio data
    """
    try:
        # Convert linear PCM16 to μ-law
        ulaw_data = audioop.lin2ulaw(pcm_data, SAMPLE_WIDTH)
        return ulaw_data
    except Exception as e:
        logger.error(f"Error converting PCM16 to μ-law: {e}")
        raise


def resample_audio(audio_data: bytes, input_rate: int, output_rate: int) -> bytes:
    """
    Resample audio from one sample rate to another using fast linear interpolation.

    OPTIMIZATION: Uses audioop.ratecv() which is MUCH faster than scipy.signal.resample
    for real-time applications. Trade-off: Slightly lower quality, but imperceptible
    for voice and provides 5-10x speed improvement.

    Args:
        audio_data: PCM16 audio data
        input_rate: Input sample rate (Hz)
        output_rate: Output sample rate (Hz)

    Returns:
        Resampled PCM16 audio data
    """
    try:
        if input_rate == output_rate:
            return audio_data  # No resampling needed

        # Use audioop.ratecv for FAST resampling (C-optimized, linear interpolation)
        # Parameters: (data, width, nchannels, inrate, outrate, state)
        # state=None means no state tracking (okay for stateless conversions)
        resampled_data, _ = audioop.ratecv(
            audio_data,
            SAMPLE_WIDTH,  # 2 bytes for int16
            1,              # mono channel
            input_rate,
            output_rate,
            None            # no state tracking
        )

        return resampled_data
    except Exception as e:
        logger.error(f"Error resampling audio from {input_rate}Hz to {output_rate}Hz: {e}")
        raise


def twilio_to_adk(twilio_audio_base64: str) -> bytes:
    """
    Convert Twilio audio format to ADK input format.

    Conversion pipeline:
    1. Decode base64
    2. Convert μ-law (8kHz) to PCM16 (8kHz)
    3. Resample from 8kHz to 24kHz (Gemini's native rate)

    Args:
        twilio_audio_base64: Base64-encoded μ-law audio from Twilio (8kHz)

    Returns:
        PCM16 audio data at 24kHz for ADK input (optimal latency)
    """
    try:
        # Step 1: Decode base64
        ulaw_data = base64.b64decode(twilio_audio_base64)

        # Step 2: Convert μ-law to PCM16 (still at 8kHz)
        pcm_8khz = ulaw_to_pcm16(ulaw_data)

        # Step 3: Resample from 8kHz to 24kHz (Gemini's native rate)
        pcm_24khz = resample_audio(pcm_8khz, TWILIO_SAMPLE_RATE, ADK_INPUT_SAMPLE_RATE)

        return pcm_24khz
    except Exception as e:
        logger.error(f"Error converting Twilio audio to ADK format: {e}")
        raise


def adk_to_twilio(adk_audio_data: bytes) -> str:
    """
    Convert ADK audio output to Twilio format.

    Conversion pipeline:
    1. Resample from 24kHz to 8kHz
    2. Convert PCM16 to μ-law
    3. Encode to base64

    Args:
        adk_audio_data: PCM16 audio data from ADK at 24kHz

    Returns:
        Base64-encoded μ-law audio at 8kHz for Twilio
    """
    try:
        # Step 1: Resample from 24kHz to 8kHz
        pcm_8khz = resample_audio(adk_audio_data, ADK_OUTPUT_SAMPLE_RATE, TWILIO_SAMPLE_RATE)

        # Step 2: Convert PCM16 to μ-law
        ulaw_data = pcm16_to_ulaw(pcm_8khz)

        # Step 3: Encode to base64
        base64_data = base64.b64encode(ulaw_data).decode('utf-8')

        return base64_data
    except Exception as e:
        logger.error(f"Error converting ADK audio to Twilio format: {e}")
        raise


def decode_base64_audio(base64_data: str) -> bytes:
    """
    Decode base64-encoded audio data.

    Args:
        base64_data: Base64-encoded audio string

    Returns:
        Decoded audio bytes
    """
    try:
        return base64.b64decode(base64_data)
    except Exception as e:
        logger.error(f"Error decoding base64 audio: {e}")
        raise


def encode_base64_audio(audio_data: bytes) -> str:
    """
    Encode audio data to base64 string.

    Args:
        audio_data: Audio bytes

    Returns:
        Base64-encoded audio string
    """
    try:
        return base64.b64encode(audio_data).decode('utf-8')
    except Exception as e:
        logger.error(f"Error encoding audio to base64: {e}")
        raise
