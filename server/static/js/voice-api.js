/**
 * Voice Chat WebSocket API Client
 * From Bondly - Handles binary and JSON WebSocket messages
 */

export class VoiceAPI {
    constructor(wsUrl) {
        this.wsUrl = wsUrl;
        this.ws = null;
        this.connected = false;

        // Event callbacks
        this.onReady = () => {};
        this.onAudio = () => {};
        this.onText = () => {};
        this.onTranscription = () => {};
        this.onTurnComplete = () => {};
        this.onInterrupted = () => {};
        this.onFunctionCall = () => {};
        this.onError = () => {};
        this.onConnectionClosed = () => {};
    }

    /**
     * Connect to WebSocket
     */
    connect() {
        return new Promise((resolve, reject) => {
            try {
                console.log('Connecting to:', this.wsUrl);

                this.ws = new WebSocket(this.wsUrl);

                this.ws.onopen = () => {
                    console.log('WebSocket connection opened');
                };

                this.ws.onmessage = async (event) => {
                    try {
                        // BONDLY PATTERN: Handle both binary and JSON messages
                        if (event.data instanceof Blob) {
                            // Binary audio frame (efficient - from backend enhancement)
                            console.log('📦 Received binary audio (Blob)');

                            const arrayBuffer = await event.data.arrayBuffer();
                            const bytes = new Uint8Array(arrayBuffer);
                            const base64Audio = this._arrayBufferToBase64(bytes);

                            this.onAudio(base64Audio);

                        } else if (event.data instanceof ArrayBuffer) {
                            // Binary audio frame (ArrayBuffer)
                            console.log('📦 Received binary audio (ArrayBuffer)');

                            const bytes = new Uint8Array(event.data);
                            const base64Audio = this._arrayBufferToBase64(bytes);

                            this.onAudio(base64Audio);

                        } else if (typeof event.data === 'string') {
                            // JSON control message
                            try {
                                const message = JSON.parse(event.data);
                                console.log('📨 Received message:', message.type || 'ready');
                                this.handleMessage(message);
                            } catch (parseError) {
                                // Not valid JSON - might be raw text, log and ignore
                                console.warn('⚠️ Received non-JSON string:', event.data.substring(0, 100));
                            }
                        } else {
                            console.warn('⚠️ Unknown data type:', typeof event.data);
                        }
                    } catch (error) {
                        console.error('❌ Error processing message:', error);
                        // Don't show error to user for parse issues - just log
                        console.warn('Raw data:', event.data);
                    }
                };

                this.ws.onerror = (error) => {
                    console.error('WebSocket error:', error);
                    this.onError({ message: 'WebSocket connection error', error });
                    reject(error);
                };

                this.ws.onclose = (event) => {
                    console.log('WebSocket closed:', event.code, event.reason);
                    this.connected = false;
                    this.onConnectionClosed(event);

                    if (!event.wasClean) {
                        this.onError({
                            message: 'Connection closed unexpectedly',
                            code: event.code,
                            reason: event.reason
                        });
                    }
                };

                // Resolve when ready
                const originalOnReady = this.onReady;
                this.onReady = () => {
                    this.connected = true;
                    originalOnReady();
                    resolve();
                };

            } catch (error) {
                console.error('Error creating WebSocket:', error);
                reject(error);
            }
        });
    }

    /**
     * Handle incoming WebSocket messages (from Bondly)
     */
    handleMessage(message) {
        // Handle ready message
        if (message.ready === true) {
            console.log('Service ready');
            this.onReady();
            return;
        }

        // Handle typed messages
        switch (message.type) {
            case 'ready':
                console.log('Service ready');
                this.onReady();
                break;

            case 'audio':
                this.onAudio(message.data);
                break;

            case 'text':
                this.onText(message.data);
                break;

            case 'transcription':
                this.onTranscription(message.data);
                break;

            case 'turn_complete':
                this.onTurnComplete();
                break;

            case 'interrupted':
                this.onInterrupted(message.data);
                break;

            case 'function_call':
                this.onFunctionCall(message.data);
                break;

            case 'error':
                this.onError(message.data);
                break;

            default:
                console.warn('Unknown message type:', message.type);
        }
    }

    /**
     * Send audio data to the server
     */
    sendAudio(base64Audio) {
        if (!this.connected || !this.ws || this.ws.readyState !== WebSocket.OPEN) {
            console.error('Cannot send audio: WebSocket not connected');
            return;
        }

        this.ws.send(JSON.stringify({
            type: 'audio',
            data: base64Audio
        }));
    }

    /**
     * Send text message to the server
     */
    sendText(text) {
        if (!this.connected || !this.ws || this.ws.readyState !== WebSocket.OPEN) {
            console.error('Cannot send text: WebSocket not connected');
            return;
        }

        this.ws.send(JSON.stringify({
            type: 'text',
            data: text
        }));
    }

    /**
     * Send end of turn signal
     */
    sendEndOfTurn() {
        if (!this.connected || !this.ws || this.ws.readyState !== WebSocket.OPEN) {
            console.error('Cannot send end of turn: WebSocket not connected');
            return;
        }

        this.ws.send(JSON.stringify({
            type: 'end'
        }));
    }

    /**
     * Disconnect from WebSocket
     */
    disconnect() {
        if (this.ws) {
            this.ws.close();
            this.ws = null;
        }
        this.connected = false;
    }

    /**
     * Convert ArrayBuffer/Uint8Array to base64 (from Bondly)
     */
    _arrayBufferToBase64(bytes) {
        const CHUNK_SIZE = 0x8000; // 32KB chunks
        let binary = '';

        if (bytes.length < CHUNK_SIZE) {
            binary = String.fromCharCode(...bytes);
        } else {
            for (let i = 0; i < bytes.length; i += CHUNK_SIZE) {
                const chunk = bytes.subarray(i, i + CHUNK_SIZE);
                binary += String.fromCharCode(...chunk);
            }
        }

        return btoa(binary);
    }
}
