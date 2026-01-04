/**
 * Project Livewire Voice Chat Application
 * Adapted from Bondly - Main application logic with VAD support
 */

import { AudioRecorder, AudioPlayer } from '/static/js/audio-utils.js';
import { VoiceAPI } from '/static/js/voice-api.js';

class VoiceChatApp {
    constructor() {
        // Configuration - Dynamically determine WebSocket URL based on current host
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const host = window.location.host; // includes port if present

        // Use /ws endpoint on the same server (works for both local and Cloudflare tunnel)
        this.wsUrl = `${protocol}//${host}/ws`;

        console.log('WebSocket URL:', this.wsUrl);

        // Components
        this.audioRecorder = new AudioRecorder(24000);  // ← Changed from 16000 to 24000
        this.audioPlayer = new AudioPlayer(24000);
        this.voiceAPI = null;
        this.vad = null;

        // State
        this.isRecording = false;
        this.isConnected = false;
        this.isAISpeaking = false;

        // DOM Elements
        this.elements = {
            status: document.getElementById('status'),
            statusText: document.getElementById('statusText'),
            connectBtn: document.getElementById('connectBtn'),
            chatOutput: document.getElementById('chatOutput'),
            recordingIndicator: document.getElementById('recordingIndicator')
        };

        this.init();
    }

    init() {
        // Connect button
        this.elements.connectBtn.addEventListener('click', () => {
            if (this.isConnected) {
                this.disconnect();
            } else {
                this.connect();
            }
        });

        // Setup audio recorder callback
        this.audioRecorder.onAudioData = (base64Audio) => {
            if (this.voiceAPI && this.voiceAPI.connected) {
                this.voiceAPI.sendAudio(base64Audio);
            }
        };

        console.log('Voice Chat App initialized');
    }

    async connect() {
        try {
            this.updateStatus('connecting', '🔄 Connecting to voice service...');

            // Create new VoiceAPI instance
            this.voiceAPI = new VoiceAPI(this.wsUrl);

            // Setup callbacks
            this.setupVoiceAPICallbacks();

            // Connect
            await this.voiceAPI.connect();

            // Setup VAD after successful connection (from Bondly)
            await this.setupVAD();

        } catch (error) {
            console.error('Connection failed:', error);
            this.updateStatus('disconnected', `❌ Connection failed: ${error.message}`);
            this.addMessage('system', `Failed to connect: ${error.message}`);
        }
    }

    async setupVAD() {
        try {
            console.log('🎤 Initializing Voice Activity Detection...');

            // Check if VAD library is loaded
            if (typeof vad === 'undefined') {
                throw new Error('VAD library not loaded. Please refresh the page.');
            }

            this.vad = await vad.MicVAD.new({
                onSpeechStart: () => {
                    console.log('🎤 User started speaking');

                    // BONDLY PATTERN: Interrupt AI if it's speaking
                    if (this.audioPlayer.isPlaying) {
                        console.log('⏹️ Interrupting AI audio playback');
                        this.audioPlayer.stop();   // Stop all audio
                        this.audioPlayer.reset();  // Reset scheduling state
                        this.isAISpeaking = false;

                        // Notify backend about interruption
                        if (this.voiceAPI && this.voiceAPI.connected) {
                            this.voiceAPI.ws.send(JSON.stringify({
                                type: 'interruption'
                            }));
                        }

                        this.addMessage('system', 'Interrupted AI - listening to you now');
                    }
                },
                onSpeechEnd: () => {
                    console.log('🛑 User stopped speaking');
                },
                onVADMisfire: () => {
                    console.log('⚠️ VAD false positive detected');
                },
                // CDN configuration
                onnxWASMBasePath: 'https://cdn.jsdelivr.net/npm/onnxruntime-web@1.22.0/dist/',
                baseAssetPath: 'https://cdn.jsdelivr.net/npm/@ricky0123/vad-web@0.0.29/dist/'
            });

            // Start VAD monitoring
            this.vad.start();
            console.log('✅ Voice Activity Detection enabled');
            this.addMessage('system', 'Voice interruption detection enabled');

        } catch (error) {
            console.error('Failed to setup VAD:', error);
            this.addMessage('system', `VAD setup failed: ${error.message}. Manual interruption still available.`);
        }
    }

    setupVoiceAPICallbacks() {
        this.voiceAPI.onReady = async () => {
            this.isConnected = true;
            this.updateStatus('connected', '✅ Connected - Listening...');
            this.elements.connectBtn.textContent = '🔌 Disconnect';
            this.elements.connectBtn.classList.remove('btn-connect');
            this.elements.connectBtn.classList.add('btn-danger');
            this.addMessage('system', 'Connected! Start speaking...');

            // Auto-start recording when connected
            await this.startRecording();
        };

        this.voiceAPI.onAudio = async (base64Audio) => {
            this.isAISpeaking = true;
            await this.audioPlayer.play(base64Audio);
        };

        this.voiceAPI.onText = (text) => {
            this.addMessage('assistant', text);
        };

        this.voiceAPI.onTranscription = (text) => {
            this.addMessage('transcription', text, '🗣️ AI is saying');
        };

        this.voiceAPI.onTurnComplete = () => {
            this.isAISpeaking = false;
            this.audioPlayer.reset();  // ← Reset audio scheduling
            this.addMessage('system', 'AI finished speaking');
        };

        this.voiceAPI.onInterrupted = (data) => {
            this.isAISpeaking = false;
            this.audioPlayer.stop();   // ← Stop playback
            this.audioPlayer.reset();  // ← Reset audio scheduling
            this.addMessage('system', 'Response interrupted - ready for new audio');
        };

        this.voiceAPI.onFunctionCall = (data) => {
            this.addMessage('system', `Function called: ${data.name}`);
        };

        this.voiceAPI.onError = (error) => {
            console.error('Voice API error:', error);
            this.addMessage('system', `Error: ${error.message || JSON.stringify(error)}`);
        };

        this.voiceAPI.onConnectionClosed = (event) => {
            this.disconnect();
        };
    }

    disconnect() {
        // Stop and cleanup VAD (from Bondly)
        if (this.vad) {
            try {
                this.vad.pause();
                this.vad.destroy();
                this.vad = null;
                console.log('✅ VAD cleaned up');
            } catch (error) {
                console.error('Error cleaning up VAD:', error);
            }
        }

        if (this.voiceAPI) {
            this.voiceAPI.disconnect();
            this.voiceAPI = null;
        }

        if (this.isRecording) {
            this.stopRecording();
        }

        // Stop any playing audio
        if (this.audioPlayer.isPlaying) {
            this.audioPlayer.stop();
        }

        this.isConnected = false;
        this.isAISpeaking = false;
        this.updateStatus('disconnected', '⭕ Disconnected');
        this.elements.connectBtn.textContent = '🎙️ Start Conversation';
        this.elements.connectBtn.classList.remove('btn-danger');
        this.elements.connectBtn.classList.add('btn-connect');

        this.addMessage('system', 'Disconnected from voice service');
    }

    async startRecording() {
        try {
            await this.audioRecorder.start();
            this.isRecording = true;

            if (this.elements.recordingIndicator) {
                this.elements.recordingIndicator.classList.add('active');
            }
        } catch (error) {
            console.error('Failed to start recording:', error);
            this.addMessage('system', `Microphone error: ${error.message}`);
        }
    }

    stopRecording() {
        this.audioRecorder.stop();
        this.isRecording = false;

        if (this.elements.recordingIndicator) {
            this.elements.recordingIndicator.classList.remove('active');
        }

        // Send end-of-turn signal
        if (this.voiceAPI && this.voiceAPI.connected) {
            this.voiceAPI.sendEndOfTurn();
        }
    }

    updateStatus(state, message) {
        this.elements.status.className = `status ${state}`;
        this.elements.statusText.textContent = message;
    }

    addMessage(type, text, customLabel = null) {
        // Clear empty state on first message
        const emptyState = this.elements.chatOutput.querySelector('.empty-state');
        if (emptyState) {
            emptyState.remove();
        }

        const messageDiv = document.createElement('div');

        let className = 'chat-message ';
        let displayLabel = customLabel;

        if (!displayLabel) {
            if (type === 'user') {
                className += 'user-message';
                displayLabel = '👤 You';
            } else if (type === 'assistant') {
                className += 'assistant-message';
                displayLabel = '🤖 AI Assistant';
            } else if (type === 'system') {
                className += 'system-message';
                displayLabel = '💡 System';
            } else if (type === 'transcription') {
                className += 'transcription';
                displayLabel = '🗣️ AI Speaking';
            }
        } else {
            if (type === 'transcription') {
                className += 'transcription';
            }
        }

        messageDiv.className = className;
        messageDiv.innerHTML = `
            <div class="message-label">${displayLabel}</div>
            <div class="message-text">${this.escapeHtml(text)}</div>
        `;

        this.elements.chatOutput.appendChild(messageDiv);
        this.elements.chatOutput.scrollTop = this.elements.chatOutput.scrollHeight;
    }

    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }
}

// Initialize app when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
    window.voiceChatApp = new VoiceChatApp();
});
