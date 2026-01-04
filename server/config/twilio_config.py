"""
Twilio Configuration Module
Handles Twilio-specific configuration and settings
"""

import os
import logging
from typing import Optional
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()


class TwilioConfig:
    """Twilio configuration handler."""
    
    def __init__(self):
        self.account_sid: Optional[str] = os.getenv('TWILIO_ACCOUNT_SID')
        self.auth_token: Optional[str] = os.getenv('TWILIO_AUTH_TOKEN')
        self.phone_number: Optional[str] = os.getenv('TWILIO_PHONE_NUMBER')
        
        # Webhook base URL (without trailing slash)
        self.webhook_base_url: str = os.getenv(
            'TWILIO_WEBHOOK_BASE_URL', 
            'https://your-server.com'
        ).rstrip('/')
        
        # Media Streams WebSocket configuration
        self.media_streams_host: str = os.getenv('MEDIA_STREAMS_HOST', 'your-server.com')
        self.media_streams_port: int = int(os.getenv('MEDIA_STREAMS_PORT', '8004'))
        
        # Validate required credentials
        self._validate()
    
    def _validate(self) -> None:
        """Validate that required Twilio credentials are present."""
        if not self.account_sid:
            logger.warning("TWILIO_ACCOUNT_SID not set in environment variables")
        if not self.auth_token:
            logger.warning("TWILIO_AUTH_TOKEN not set in environment variables")
        if not self.phone_number:
            logger.warning("TWILIO_PHONE_NUMBER not set in environment variables")
    
    @property
    def is_configured(self) -> bool:
        """Check if Twilio is properly configured."""
        return all([
            self.account_sid,
            self.auth_token,
            self.phone_number
        ])
    
    @property
    def incoming_webhook_url(self) -> str:
        """Get the incoming call webhook URL."""
        return f"{self.webhook_base_url}/twilio/incoming"
    
    @property
    def status_webhook_url(self) -> str:
        """Get the status callback webhook URL."""
        return f"{self.webhook_base_url}/twilio/status"
    
    @property
    def media_streams_url(self) -> str:
        """Get the Media Streams WebSocket URL."""
        protocol = 'wss' if self.webhook_base_url.startswith('https') else 'ws'
        # Don't include port when using ngrok (ngrok handles port forwarding)
        return f"{protocol}://{self.media_streams_host}/twilio/media-stream"
    
    def get_media_streams_url_for_twiml(self) -> str:
        """
        Get Media Streams URL for TwiML response.
        TwiML requires the full WebSocket URL.
        """
        # Use webhook_base_url to determine protocol
        protocol = 'wss' if self.webhook_base_url.startswith('https') else 'ws'
        # Extract host from webhook_base_url or use media_streams_host
        if self.webhook_base_url.startswith('http'):
            from urllib.parse import urlparse
            parsed = urlparse(self.webhook_base_url)
            host = parsed.netloc or self.media_streams_host
        else:
            host = self.media_streams_host
        
        return f"{protocol}://{host}/twilio/media-stream"


# Global Twilio configuration instance
twilio_config = TwilioConfig()

# Log configuration status
if twilio_config.is_configured:
    logger.info("Twilio configuration loaded successfully")
    logger.info(f"Twilio Phone Number: {twilio_config.phone_number}")
    logger.info(f"Incoming Webhook URL: {twilio_config.incoming_webhook_url}")
    logger.info(f"Media Streams URL: {twilio_config.media_streams_url}")
else:
    logger.warning("Twilio configuration incomplete. Please set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, and TWILIO_PHONE_NUMBER")
