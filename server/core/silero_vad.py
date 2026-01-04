"""
Silero VAD implementation for ultra-low latency voice activity detection
This replaces Gemini's built-in VAD for faster turn detection
"""

import numpy as np
import logging
from collections import deque

logger = logging.getLogger(__name__)

class SileroVAD:
    """
    Ultra-fast voice activity detection using energy-based approach
    Optimized for 1-second latency in phone calls
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        frame_duration_ms: int = 20,  # Process every 20ms (Twilio chunk size)
        speech_threshold: float = 0.5,  # Energy threshold for speech detection
        silence_duration_ms: int = 200,  # Only 200ms silence triggers turn end (aggressive)
        min_speech_duration_ms: int = 100  # Minimum speech to consider valid
    ):
        self.sample_rate = sample_rate
        self.frame_duration_ms = frame_duration_ms
        self.frame_size = int(sample_rate * frame_duration_ms / 1000)
        self.speech_threshold = speech_threshold
        self.silence_duration_ms = silence_duration_ms
        self.min_speech_duration_ms = min_speech_duration_ms

        # State tracking
        self.is_speaking = False
        self.speech_start_time = None
        self.silence_start_time = None
        self.speech_frames = 0
        self.silence_frames = 0

        # Energy history for adaptive threshold
        self.energy_history = deque(maxlen=50)  # Last 1 second of energy values
        self.noise_floor = 0.1

        logger.info(f"SileroVAD initialized: silence_duration={silence_duration_ms}ms for 1-second latency")

    def process_audio(self, audio_data: bytes) -> dict:
        """
        Process audio chunk and detect speech/silence
        Returns: dict with 'is_speech', 'speech_started', 'speech_ended', 'confidence'
        """
        # Convert bytes to numpy array
        audio = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32)

        # Calculate energy (RMS)
        energy = np.sqrt(np.mean(audio ** 2)) / 32768.0  # Normalize to 0-1

        # Add to history for adaptive threshold
        self.energy_history.append(energy)

        # Adaptive threshold based on noise floor
        if len(self.energy_history) > 10:
            sorted_energy = sorted(self.energy_history)
            self.noise_floor = sorted_energy[len(sorted_energy) // 4]  # 25th percentile

        # Dynamic threshold
        dynamic_threshold = max(self.speech_threshold, self.noise_floor * 3)

        # Detect speech
        is_speech = energy > dynamic_threshold

        result = {
            'is_speech': is_speech,
            'speech_started': False,
            'speech_ended': False,
            'energy': energy,
            'threshold': dynamic_threshold,
            'confidence': min(1.0, energy / dynamic_threshold) if is_speech else 0.0
        }

        # State machine for speech detection
        if is_speech:
            self.silence_frames = 0

            if not self.is_speaking:
                # Speech started
                self.is_speaking = True
                self.speech_start_time = self.speech_frames * self.frame_duration_ms
                result['speech_started'] = True
                logger.info(f"🎤 SPEECH STARTED (energy: {energy:.3f}, threshold: {dynamic_threshold:.3f})")

            self.speech_frames += 1

        else:
            # Silence detected
            if self.is_speaking:
                self.silence_frames += 1
                silence_duration = self.silence_frames * self.frame_duration_ms

                # Check if silence is long enough to end turn
                if silence_duration >= self.silence_duration_ms:
                    speech_duration = self.speech_frames * self.frame_duration_ms

                    # Only end if speech was long enough
                    if speech_duration >= self.min_speech_duration_ms:
                        self.is_speaking = False
                        self.speech_frames = 0
                        self.silence_frames = 0
                        result['speech_ended'] = True
                        logger.info(f"🔇 SPEECH ENDED after {silence_duration}ms silence (speech duration: {speech_duration}ms)")
                    else:
                        # Too short, probably noise
                        logger.debug(f"Ignoring short speech burst ({speech_duration}ms)")
                        self.is_speaking = False
                        self.speech_frames = 0
                        self.silence_frames = 0

        return result

    def reset(self):
        """Reset VAD state"""
        self.is_speaking = False
        self.speech_frames = 0
        self.silence_frames = 0
        self.energy_history.clear()
        logger.debug("VAD state reset")


class WebRTCVAD:
    """
    Alternative: WebRTC VAD for even lower latency
    Uses Google's WebRTC VAD which is battle-tested for real-time communication
    """

    def __init__(self, aggressiveness: int = 3, silence_duration_ms: int = 150):
        """
        aggressiveness: 0-3, where 3 is most aggressive (fastest but more false positives)
        silence_duration_ms: How long silence before turn ends (150ms for ultra-low latency)
        """
        try:
            import webrtcvad
            self.vad = webrtcvad.Vad(aggressiveness)
            self.available = True
            logger.info(f"WebRTC VAD initialized: aggressiveness={aggressiveness}, silence={silence_duration_ms}ms")
        except ImportError:
            logger.warning("WebRTC VAD not available. Install with: pip install webrtcvad")
            self.available = False
            self.vad = None

        self.silence_duration_ms = silence_duration_ms
        self.frame_duration_ms = 20  # WebRTC VAD works with 10, 20, or 30ms frames
        self.is_speaking = False
        self.silence_frames = 0

    def process_audio(self, audio_data: bytes, sample_rate: int = 16000) -> dict:
        """Process audio with WebRTC VAD"""
        if not self.available:
            return {'is_speech': True, 'speech_started': False, 'speech_ended': False}

        # WebRTC VAD requires specific frame sizes
        is_speech = self.vad.is_speech(audio_data, sample_rate)

        result = {
            'is_speech': is_speech,
            'speech_started': False,
            'speech_ended': False,
            'confidence': 1.0 if is_speech else 0.0
        }

        if is_speech:
            self.silence_frames = 0
            if not self.is_speaking:
                self.is_speaking = True
                result['speech_started'] = True
                logger.info("🎤 WebRTC: SPEECH STARTED")
        else:
            if self.is_speaking:
                self.silence_frames += 1
                silence_duration = self.silence_frames * self.frame_duration_ms

                if silence_duration >= self.silence_duration_ms:
                    self.is_speaking = False
                    self.silence_frames = 0
                    result['speech_ended'] = True
                    logger.info(f"🔇 WebRTC: SPEECH ENDED after {silence_duration}ms")

        return result


def get_vad(vad_type: str = "silero", **kwargs) -> object:
    """
    Factory function to get VAD instance

    Options:
    - 'silero': Energy-based VAD optimized for low latency
    - 'webrtc': Google's WebRTC VAD (requires webrtcvad package)
    - 'gemini': Use Gemini's built-in VAD (not recommended for low latency)
    """
    if vad_type == "silero":
        return SileroVAD(**kwargs)
    elif vad_type == "webrtc":
        return WebRTCVAD(**kwargs)
    else:
        raise ValueError(f"Unknown VAD type: {vad_type}")