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
Optimized configuration for Gemini 2.5 Flash Native Audio model
Addresses proactive behavior and language switching issues
"""

import os
from google.genai import types


def get_optimized_run_config(
    voice_name: str = "Aoede",
    language_code: str = "en-US",
    disable_automatic_vad: bool = False,
    vad_sensitivity: str = "MEDIUM",
    for_twilio: bool = False,
    max_output_tokens: int = 512
):
    """
    Get optimized RunConfig for Gemini 2.5 Flash Native Audio

    Args:
        voice_name: Voice to use (Aoede, Puck, Charon, Kore, Fenrir, etc.)
        language_code: Language code (en-US, es-ES, fr-FR, etc.)
        disable_automatic_vad: If True, disables automatic Voice Activity Detection
        vad_sensitivity: VAD sensitivity level ("LOW", "MEDIUM", "HIGH")
        for_twilio: If True, optimizes for Twilio phone calls (higher thresholds, longer timeouts)
        max_output_tokens: Maximum tokens for voice responses (default: 512 for concise answers)

    Returns:
        RunConfig optimized for controlled behavior and low latency
    """
    from google.adk.agents.run_config import RunConfig, StreamingMode

    # Build RealtimeInputConfig for advanced VAD control
    realtime_config = None
    if not disable_automatic_vad:
        # Map sensitivity to start/end sensitivity enums
        # For phone calls, we balance noise rejection with responsiveness
        if for_twilio:
            # 🔥 DISABLE Gemini VAD - We use custom Silero VAD for 1-second latency
            realtime_config = types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    disabled=True  # DISABLED - Using custom VAD for ultra-low latency
                ),
                # Only include activity, not silence periods
                turn_coverage="TURN_INCLUDES_ONLY_ACTIVITY"
            )
        else:
            # UI-optimized: more responsive
            # Always use HIGH sensitivity for UI mode too
            realtime_config = types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    disabled=False,
                    start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_HIGH,
                    end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_HIGH,
                    prefix_padding_ms=0,
                    silence_duration_ms=300
                ),
                # Only include activity, not silence periods
                turn_coverage="TURN_INCLUDES_ONLY_ACTIVITY",
                # What happens when activity is detected
                activity_handling="ACTIVITY_HANDLING_UNSPECIFIED"  # Use default behavior
            )
    else:
        # Manual VAD control - client must send activity signals
        realtime_config = types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(
                disabled=True
            )
        )

    # 🔥 ULTRA-FAST: Generation config optimized for 1-second latency
    generation_config = types.GenerationConfig(
        temperature=0.5,  # Lower for faster generation
        top_p=0.8,  # More focused responses
        top_k=20,  # Limit choices for speed
        max_output_tokens=min(max_output_tokens, 256),  # Very short responses for phone
        candidate_count=1
    )

    config = RunConfig(
        streaming_mode=StreamingMode.BIDI,

        # Speech configuration with language control
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(
                    voice_name=voice_name
                )
            ),
            language_code=language_code
        ),

        # Audio modality (you can add TEXT if needed)
        response_modalities=[types.Modality.AUDIO],

        # Enable transcription for both input and output
        output_audio_transcription=types.AudioTranscriptionConfig(),
        input_audio_transcription=types.AudioTranscriptionConfig(),

        # 🔥 NEW: Advanced VAD control for phone calls
        realtime_input_config=realtime_config,

        # 🔥 NEW: Session resumption for better stability
        session_resumption=types.SessionResumptionConfig(),

        # 🔥 NEW: Context window compression for long calls
        context_window_compression=types.ContextWindowCompressionConfig(),

        # 🔥 NEW: Proactivity control to prevent unwanted interruptions
        proactivity=types.ProactivityConfig(),

        # 🔥 NEW: Generation config for faster responses
        generation_config=generation_config
    )

    return config


def get_realtime_input_config_with_vad_control(
    disable_automatic: bool = False,
    sensitivity: str = "MEDIUM",
    speech_start_timeout_ms: int = 3000,
    speech_end_timeout_ms: int = 1500
):
    """
    Get realtime input configuration with VAD control

    This helps prevent the model from being too proactive by:
    1. Increasing timeout values to wait longer before responding
    2. Allowing manual control of turn-taking
    3. Adjusting sensitivity to avoid false triggers

    Args:
        disable_automatic: If True, requires manual activityStart/activityEnd signals
        sensitivity: "LOW", "MEDIUM", or "HIGH" - controls how easily VAD triggers
        speech_start_timeout_ms: How long to wait before considering speech started
        speech_end_timeout_ms: How long to wait after speech stops before processing

    Returns:
        Dictionary with realtime input configuration
    """
    config = {
        "realtime_input_config": {
            "automatic_activity_detection": {
                "disabled": disable_automatic,
            }
        }
    }

    # Add sensitivity settings if automatic VAD is enabled
    if not disable_automatic:
        # Map sensitivity to threshold values
        sensitivity_map = {
            "LOW": 0.7,      # Less sensitive - requires clearer speech
            "MEDIUM": 0.5,   # Balanced
            "HIGH": 0.3      # More sensitive - triggers easily
        }

        config["realtime_input_config"]["automatic_activity_detection"]["sensitivity"] = (
            sensitivity_map.get(sensitivity, 0.5)
        )

        # Add timeout configurations
        config["realtime_input_config"]["speech_start_timeout_ms"] = speech_start_timeout_ms
        config["realtime_input_config"]["speech_end_timeout_ms"] = speech_end_timeout_ms

    return config


def get_language_specific_instructions(language_code: str = "en-US") -> str:
    """
    Get language-specific system instruction additions

    Args:
        language_code: Language code (e.g., "en-US", "es-ES")

    Returns:
        Additional instructions to enforce language consistency
    """
    language_map = {
        "en-US": "English (US)",
        "en-GB": "English (UK)",
        "es-ES": "Spanish (Spain)",
        "es-MX": "Spanish (Mexico)",
        "fr-FR": "French",
        "de-DE": "German",
        "it-IT": "Italian",
        "pt-BR": "Portuguese (Brazil)",
        "ja-JP": "Japanese",
        "ko-KR": "Korean",
        "zh-CN": "Chinese (Simplified)",
        "zh-TW": "Chinese (Traditional)",
        "hi-IN": "Hindi",
        "ar-SA": "Arabic",
    }

    language_name = language_map.get(language_code, "English (US)")

    return f"""

**STRICT LANGUAGE ENFORCEMENT:**
- You MUST respond ONLY in {language_name} unless explicitly instructed otherwise
- DO NOT mix languages in your responses
- DO NOT automatically switch to another language even if the user speaks it
- If the user speaks in a different language, respond in {language_name} and ask: "I noticed you're speaking in [language]. Would you like me to switch to that language, or continue in {language_name}?"
- Maintain consistent language throughout the entire conversation
- This is a voice interface - clarity and consistency are critical
"""


def get_optimized_generation_config():
    """
    Get generation config optimized for less proactive behavior

    Returns:
        GenerationConfig with conservative settings
    """
    return types.GenerationConfig(
        temperature=0.7,        # Balanced creativity
        top_p=0.9,             # Slightly conservative
        top_k=40,              # Limit randomness
        max_output_tokens=512, # Shorter responses for voice
        candidate_count=1,
    )


# Preset configurations for common scenarios
PRESETS = {
    "strict_english": {
        "voice_name": "Aoede",
        "language_code": "en-US",
        "disable_automatic_vad": False,
        "vad_sensitivity": "MEDIUM",
        "speech_end_timeout_ms": 2000,  # Wait 2 seconds after speech
    },

    "patient_listener": {
        "voice_name": "Puck",
        "language_code": "en-US",
        "disable_automatic_vad": False,
        "vad_sensitivity": "LOW",  # Less sensitive
        "speech_end_timeout_ms": 2500,  # Wait even longer
    },

    "manual_control": {
        "voice_name": "Aoede",
        "language_code": "en-US",
        "disable_automatic_vad": True,  # Manual turn control
        "vad_sensitivity": "MEDIUM",
    },

    "multilingual": {
        "voice_name": "Charon",
        "language_code": "en-US",
        "disable_automatic_vad": False,
        "vad_sensitivity": "MEDIUM",
        "speech_end_timeout_ms": 1500,
        "allow_language_switching": True,  # Allow natural switching
    }
}


def load_preset(preset_name: str = "strict_english"):
    """
    Load a preset configuration

    Args:
        preset_name: Name of the preset to load

    Returns:
        Dictionary with preset configuration
    """
    if preset_name not in PRESETS:
        raise ValueError(f"Unknown preset: {preset_name}. Available: {list(PRESETS.keys())}")

    return PRESETS[preset_name].copy()


# Example usage function
def create_optimized_agent_config(preset: str = "strict_english"):
    """
    Create a complete optimized configuration for ADK agent

    Args:
        preset: Preset name to use

    Returns:
        Dictionary with complete configuration
    """
    preset_config = load_preset(preset)

    # Load system instructions
    instructions_file = "system-instructions-optimized.txt" if preset != "multilingual" else "system-instructions.txt"

    try:
        with open(f'config/{instructions_file}', 'r') as f:
            base_instructions = f.read()
    except:
        base_instructions = "You are a helpful AI assistant."

    # Add language-specific instructions
    language_instructions = get_language_specific_instructions(
        preset_config.get("language_code", "en-US")
    )

    full_instructions = base_instructions + language_instructions

    return {
        "run_config": get_optimized_run_config(
            voice_name=preset_config["voice_name"],
            language_code=preset_config["language_code"],
            disable_automatic_vad=preset_config["disable_automatic_vad"],
            vad_sensitivity=preset_config["vad_sensitivity"]
        ),
        "generation_config": get_optimized_generation_config(),
        "system_instructions": full_instructions,
        "realtime_input_config": get_realtime_input_config_with_vad_control(
            disable_automatic=preset_config["disable_automatic_vad"],
            sensitivity=preset_config["vad_sensitivity"],
            speech_end_timeout_ms=preset_config.get("speech_end_timeout_ms", 1500)
        )
    }
