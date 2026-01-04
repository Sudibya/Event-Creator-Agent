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
Audio Batching Utility for Twilio Media Streams

Twilio sends small audio chunks (~50ms each at 8kHz μ-law).
This module batches them into larger chunks (200ms) before sending to Gemini,
reducing API overhead and improving latency.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


class TwilioAudioBatcher:
    """
    Batches small Twilio audio chunks into larger ones for efficient processing.

    Twilio Media Streams sends ~50ms chunks (400 bytes at 8kHz μ-law).
    Gemini prefers larger chunks (200-500ms) for better processing efficiency.
    This batcher accumulates small chunks until reaching target size.
    """

    def __init__(self, target_duration_ms: int = 200, sample_rate: int = 16000):
        """
        Initialize the audio batcher.

        Args:
            target_duration_ms: Target batch duration in milliseconds (default: 200ms)
            sample_rate: Audio sample rate in Hz (default: 16000 for ADK)
        """
        self.target_duration_ms = target_duration_ms
        self.sample_rate = sample_rate

        # Calculate target size in bytes (PCM16 = 2 bytes per sample)
        # Formula: (sample_rate * duration_ms / 1000) * bytes_per_sample
        self.target_bytes = int((sample_rate * target_duration_ms / 1000) * 2)

        # Buffer to accumulate audio chunks
        self.buffer = bytearray()

        # Statistics for monitoring
        self.chunks_received = 0
        self.chunks_sent = 0
        self.bytes_buffered = 0

        logger.info(
            f"AudioBatcher initialized: target={target_duration_ms}ms, "
            f"sample_rate={sample_rate}Hz, target_size={self.target_bytes}bytes"
        )

    def add_chunk(self, audio_data: bytes) -> Optional[bytes]:
        """
        Add an audio chunk to the buffer.

        Args:
            audio_data: Raw PCM16 audio bytes

        Returns:
            Batched audio if buffer is full, None otherwise
        """
        if not audio_data:
            return None

        # Add to buffer
        self.buffer.extend(audio_data)
        self.chunks_received += 1
        self.bytes_buffered = len(self.buffer)

        # Check if we have enough data
        if len(self.buffer) >= self.target_bytes:
            # Extract exactly target_bytes
            batch = bytes(self.buffer[:self.target_bytes])

            # Keep remainder in buffer
            self.buffer = self.buffer[self.target_bytes:]

            self.chunks_sent += 1

            # Log every 50th batch to avoid spam
            if self.chunks_sent % 50 == 0:
                logger.debug(
                    f"Audio batching stats: received={self.chunks_received}, "
                    f"sent={self.chunks_sent}, buffered={len(self.buffer)}bytes"
                )

            return batch

        return None

    def flush(self) -> Optional[bytes]:
        """
        Flush any remaining audio in the buffer.

        Returns:
            Remaining buffered audio, or None if buffer is empty
        """
        if len(self.buffer) > 0:
            batch = bytes(self.buffer)
            self.buffer = bytearray()
            self.chunks_sent += 1

            logger.debug(f"Flushed {len(batch)} bytes from buffer")
            return batch

        return None

    def reset(self):
        """Reset the batcher state."""
        self.buffer = bytearray()
        self.chunks_received = 0
        self.chunks_sent = 0
        self.bytes_buffered = 0
        logger.debug("AudioBatcher reset")

    def get_stats(self) -> dict:
        """
        Get batching statistics.

        Returns:
            Dictionary with batching stats
        """
        return {
            "chunks_received": self.chunks_received,
            "chunks_sent": self.chunks_sent,
            "bytes_buffered": len(self.buffer),
            "batch_ratio": round(self.chunks_received / max(self.chunks_sent, 1), 2),
            "target_bytes": self.target_bytes,
            "target_duration_ms": self.target_duration_ms
        }


class AdaptiveAudioBatcher(TwilioAudioBatcher):
    """
    Advanced batcher that adapts batch size based on network conditions.

    Monitors latency and adjusts batch size dynamically:
    - Low latency (< 100ms): Use smaller batches (150ms) for responsiveness
    - Normal latency (100-300ms): Use standard batches (200ms)
    - High latency (> 300ms): Use larger batches (300ms) for stability
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        min_duration_ms: int = 150,
        max_duration_ms: int = 300,
        default_duration_ms: int = 200
    ):
        """
        Initialize adaptive batcher.

        Args:
            sample_rate: Audio sample rate
            min_duration_ms: Minimum batch duration for low latency
            max_duration_ms: Maximum batch duration for high latency
            default_duration_ms: Default batch duration
        """
        super().__init__(target_duration_ms=default_duration_ms, sample_rate=sample_rate)

        self.min_duration_ms = min_duration_ms
        self.max_duration_ms = max_duration_ms
        self.default_duration_ms = default_duration_ms

        # Calculate min/max target bytes
        self.min_bytes = int((sample_rate * min_duration_ms / 1000) * 2)
        self.max_bytes = int((sample_rate * max_duration_ms / 1000) * 2)

        # Latency tracking
        self.latency_history = []
        self.max_history_size = 10

        logger.info(
            f"AdaptiveAudioBatcher initialized: "
            f"range={min_duration_ms}-{max_duration_ms}ms, "
            f"default={default_duration_ms}ms"
        )

    def update_latency(self, latency_ms: float):
        """
        Update with measured latency to adjust batch size.

        Args:
            latency_ms: Measured latency in milliseconds
        """
        self.latency_history.append(latency_ms)

        # Keep only recent history
        if len(self.latency_history) > self.max_history_size:
            self.latency_history.pop(0)

        # Calculate average latency
        avg_latency = sum(self.latency_history) / len(self.latency_history)

        # Adjust target based on latency
        if avg_latency < 100:
            # Low latency - use smaller batches for responsiveness
            new_duration = self.min_duration_ms
        elif avg_latency > 300:
            # High latency - use larger batches for stability
            new_duration = self.max_duration_ms
        else:
            # Normal latency - use default
            new_duration = self.default_duration_ms

        # Update target if changed
        if new_duration != self.target_duration_ms:
            old_duration = self.target_duration_ms
            self.target_duration_ms = new_duration
            self.target_bytes = int((self.sample_rate * new_duration / 1000) * 2)

            logger.info(
                f"Adaptive batching: latency={avg_latency:.1f}ms, "
                f"adjusted batch size {old_duration}ms → {new_duration}ms"
            )
