/**
 * Audio Utilities for Voice Chat
 * From Bondly - Uses AudioWorklet for modern, efficient audio processing
 */

// AudioWorklet processor code - processes at NATIVE microphone sample rate
const WORKLET_CODE = `
class AudioRecorderWorklet extends AudioWorkletProcessor {
    constructor() {
        super();
        this.buffer = new Int16Array(2048); // ← Reduced from 4096 for lower latency
        this.bufferIndex = 0;
    }

    process(inputs) {
        if (inputs[0] && inputs[0].length > 0) {
            const input = inputs[0][0];
            this.processChunk(input);
        }
        return true;
    }

    processChunk(float32Array) {
        for (let i = 0; i < float32Array.length; i++) {
            // Convert float32 (-1 to 1) to int16 (-32768 to 32767)
            const s = Math.max(-1, Math.min(1, float32Array[i]));
            const int16Value = s < 0 ? s * 0x8000 : s * 0x7FFF;
            this.buffer[this.bufferIndex++] = int16Value;

            if (this.bufferIndex >= this.buffer.length) {
                this.sendBuffer();
            }
        }
    }

    sendBuffer() {
        this.port.postMessage({
            int16Buffer: this.buffer.slice(0, this.bufferIndex).buffer
        });
        this.bufferIndex = 0;
    }
}

registerProcessor('audio-recorder-worklet', AudioRecorderWorklet);
`;

export class AudioRecorder {
    constructor(targetSampleRate = 24000) {  // ← Changed from 16000 to 24000
        this.targetSampleRate = targetSampleRate;
        this.nativeSampleRate = null;
        this.stream = null;
        this.audioContext = null;
        this.source = null;
        this.workletNode = null;
        this.isRecording = false;
        this.onAudioData = null;
    }

    /**
     * Resamples audio using linear interpolation (from Bondly)
     */
    resampleAudio(inputBuffer, inputSampleRate, outputSampleRate) {
        if (inputSampleRate === outputSampleRate) {
            return inputBuffer;
        }

        const ratio = inputSampleRate / outputSampleRate;
        const outputLength = Math.floor(inputBuffer.length / ratio);
        const outputBuffer = new Int16Array(outputLength);

        for (let i = 0; i < outputLength; i++) {
            const srcIndex = i * ratio;
            const srcIndexInt = Math.floor(srcIndex);
            const fraction = srcIndex - srcIndexInt;

            if (srcIndexInt + 1 < inputBuffer.length) {
                outputBuffer[i] = Math.floor(
                    inputBuffer[srcIndexInt] * (1 - fraction) +
                    inputBuffer[srcIndexInt + 1] * fraction
                );
            } else {
                outputBuffer[i] = inputBuffer[srcIndexInt];
            }
        }

        return outputBuffer;
    }

    async start() {
        try {
            // Get microphone stream
            this.stream = await navigator.mediaDevices.getUserMedia({
                audio: {
                    echoCancellation: true,
                    noiseSuppression: true,
                    autoGainControl: true
                }
            });

            // Detect native sample rate
            const audioTrack = this.stream.getAudioTracks()[0];
            const settings = audioTrack.getSettings();
            this.nativeSampleRate = settings.sampleRate || 48000;

            console.log('🎤 Microphone detected:', {
                sampleRate: this.nativeSampleRate,
                channelCount: settings.channelCount,
                label: audioTrack.label
            });

            // Create AudioContext at native rate
            this.audioContext = new (window.AudioContext || window.webkitAudioContext)({
                sampleRate: this.nativeSampleRate
            });

            console.log(`🎧 AudioContext created at ${this.audioContext.sampleRate} Hz`);

            await this.audioContext.resume();

            // Create worklet
            const workletBlob = new Blob([WORKLET_CODE], { type: 'application/javascript' });
            const workletUrl = URL.createObjectURL(workletBlob);
            await this.audioContext.audioWorklet.addModule(workletUrl);
            URL.revokeObjectURL(workletUrl);

            this.workletNode = new AudioWorkletNode(this.audioContext, 'audio-recorder-worklet');

            // Handle audio data
            this.workletNode.port.onmessage = (event) => {
                if (event.data.int16Buffer && this.onAudioData) {
                    const int16Data = new Int16Array(event.data.int16Buffer);

                    // Resample to target rate
                    const resampled = this.resampleAudio(
                        int16Data,
                        this.nativeSampleRate,
                        this.targetSampleRate
                    );

                    // Convert to base64
                    const base64Audio = this.arrayBufferToBase64(resampled.buffer);
                    this.onAudioData(base64Audio);
                }
            };

            // Connect audio graph
            this.source = this.audioContext.createMediaStreamSource(this.stream);
            this.source.connect(this.workletNode);

            this.isRecording = true;
            console.log(`✅ Recording started: ${this.nativeSampleRate}Hz → ${this.targetSampleRate}Hz`);

        } catch (error) {
            console.error('❌ Error starting audio recording:', error);
            if (error.name === 'NotAllowedError') {
                throw new Error('Microphone access denied');
            } else if (error.name === 'NotFoundError') {
                throw new Error('No microphone found');
            } else {
                throw error;
            }
        }
    }

    stop() {
        this.isRecording = false;
        console.log('🛑 Stopping audio recording');

        if (this.source) {
            this.source.disconnect();
            this.source = null;
        }

        if (this.workletNode) {
            this.workletNode.disconnect();
            this.workletNode = null;
        }

        if (this.stream) {
            this.stream.getTracks().forEach(track => track.stop());
            this.stream = null;
        }

        if (this.audioContext && this.audioContext.state !== 'closed') {
            this.audioContext.close();
            this.audioContext = null;
        }

        console.log('✅ Audio recording stopped');
    }

    arrayBufferToBase64(buffer) {
        const bytes = new Uint8Array(buffer);
        let binary = '';
        for (let i = 0; i < bytes.byteLength; i++) {
            binary += String.fromCharCode(bytes[i]);
        }
        return btoa(binary);
    }
}

export class AudioPlayer {
    constructor(sampleRate = 24000) {
        this.sampleRate = sampleRate;
        this.audioContext = null;
        this.nextStartTime = 0; // For gapless playback (from Bondly)
        this.audioSources = new Set();
        this.gainNode = null;
        this.onComplete = () => {};
        this.isPlaying = false;

        console.log(`🎵 AudioPlayer initialized: ${this.sampleRate}Hz`);
    }

    async play(base64Audio) {
        try {
            // Initialize on first play
            if (!this.audioContext) {
                this.audioContext = new (window.AudioContext || window.webkitAudioContext)({
                    sampleRate: this.sampleRate
                });

                this.gainNode = this.audioContext.createGain();
                this.gainNode.connect(this.audioContext.destination);
                this.gainNode.gain.setValueAtTime(1, this.audioContext.currentTime);

                console.log(`🎧 AudioContext created at ${this.audioContext.sampleRate}Hz`);
            }

            if (this.audioContext.state === 'suspended') {
                await this.audioContext.resume();
            }

            if (!base64Audio || base64Audio.length === 0) {
                console.warn('[AudioPlayer] Empty audio chunk');
                return;
            }

            // Convert base64 to PCM16
            const pcmData = this.base64ToInt16Array(base64Audio);

            if (pcmData.length === 0) {
                console.warn('[AudioPlayer] Invalid PCM data');
                return;
            }

            const frameCount = pcmData.length;

            // Create audio buffer
            const audioBuffer = this.audioContext.createBuffer(1, frameCount, this.sampleRate);

            // Normalize int16 to float32
            const channelData = audioBuffer.getChannelData(0);
            for (let i = 0; i < frameCount; i++) {
                channelData[i] = pcmData[i] / 32768.0;
            }

            // GAPLESS SCHEDULING (from Bondly) with INTERRUPTION FIX
            const now = this.audioContext.currentTime;

            // If nextStartTime is in the past (after interruption), reset it
            if (this.nextStartTime < now) {
                console.log(`[AudioPlayer] ⚠️ Detected stale scheduling (${this.nextStartTime.toFixed(3)}s < ${now.toFixed(3)}s), resetting...`);
                this.nextStartTime = now + 0.03;  // Start 30ms from now (reduced for lower latency)
            }

            const scheduledTime = Math.max(this.nextStartTime, now + 0.03);  // 30ms safety buffer

            const source = this.audioContext.createBufferSource();
            source.buffer = audioBuffer;
            source.connect(this.gainNode);

            source.addEventListener('ended', () => {
                this.audioSources.delete(source);
                if (this.audioSources.size === 0) {
                    this.isPlaying = false;
                    this.onComplete();
                }
            });

            source.start(scheduledTime);
            this.audioSources.add(source);
            this.isPlaying = true;

            // Update for next chunk (gapless)
            this.nextStartTime = scheduledTime + audioBuffer.duration;

            console.log(`[AudioPlayer] ✅ Scheduled chunk: ${frameCount} samples, duration: ${audioBuffer.duration.toFixed(3)}s, at ${scheduledTime.toFixed(3)}s`);
        } catch (error) {
            console.error('[AudioPlayer] Error playing audio:', error);
        }
    }

    base64ToInt16Array(base64) {
        const binaryString = atob(base64);
        const bytes = new Uint8Array(binaryString.length);
        for (let i = 0; i < binaryString.length; i++) {
            bytes[i] = binaryString.charCodeAt(i);
        }
        return new Int16Array(bytes.buffer);
    }

    stop() {
        console.log('[AudioPlayer] Stopping all audio');

        this.audioSources.forEach(source => {
            try {
                source.stop();
                source.disconnect();
            } catch (e) {
                // Ignore
            }
        });
        this.audioSources.clear();

        this.nextStartTime = 0;
        this.isPlaying = false;

        if (this.gainNode && this.audioContext) {
            this.gainNode.gain.linearRampToValueAtTime(0, this.audioContext.currentTime + 0.1);
        }
    }

    reset() {
        /**
         * Reset audio scheduling state (CRITICAL for fixing audio after interruption)
         * Call this after interruptions or turn completion
         */
        console.log('[AudioPlayer] Resetting audio scheduling state');

        // Reset scheduling timestamp
        this.nextStartTime = 0;
        this.isPlaying = false;

        // Clear any pending sources (don't stop them, just clear tracking)
        this.audioSources.clear();

        // Reset gain to full volume
        if (this.gainNode && this.audioContext) {
            try {
                this.gainNode.gain.cancelScheduledValues(this.audioContext.currentTime);
                this.gainNode.gain.setValueAtTime(1, this.audioContext.currentTime);
            } catch (e) {
                // Ignore if already reset
            }
        }

        console.log('[AudioPlayer] Audio state reset - ready for new audio');
    }

    cleanup() {
        if (this.audioContext && this.audioContext.state !== 'closed') {
            this.audioContext.close();
            this.audioContext = null;
        }
    }
}
