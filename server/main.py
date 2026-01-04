#!/usr/bin/env python3
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
Main server - runs both WebSocket server (for client) and FastAPI server (for Twilio)
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

# Add server directory to path
sys.path.insert(0, str(Path(__file__).parent))

import websockets
import uvicorn
from dotenv import load_dotenv
from core.adk_websocket_handler import handle_adk_client
from core.twilio_webhook import app as fastapi_app

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Server configuration
WEBSOCKET_HOST = os.getenv("WEBSOCKET_HOST", "localhost")
# WEBSOCKET_PORT = int(os.getenv("WEBSOCKET_PORT", "8003"))
WEBSOCKET_PORT = 8006
FASTAPI_HOST = os.getenv("FASTAPI_HOST", "0.0.0.0")
# FASTAPI_PORT = int(os.getenv("MEDIA_STREAMS_PORT", "8004"))
FASTAPI_PORT = 8005


async def run_websocket_server():
    """Run WebSocket server for client connections"""
    logger.info(f"Starting WebSocket server on ws://{WEBSOCKET_HOST}:{WEBSOCKET_PORT}")

    async with websockets.serve(
        handle_adk_client,
        WEBSOCKET_HOST,
        WEBSOCKET_PORT,
        ping_interval=20,
        ping_timeout=10,
        max_size=10 * 1024 * 1024,  # 10MB max message size
    ):
        logger.info(f"✓ WebSocket server running on ws://{WEBSOCKET_HOST}:{WEBSOCKET_PORT}")
        await asyncio.Future()  # Run forever


async def run_fastapi_server():
    """Run FastAPI server for Twilio webhooks"""
    logger.info(f"Starting FastAPI server on http://{FASTAPI_HOST}:{FASTAPI_PORT}")

    config = uvicorn.Config(
        app=fastapi_app,
        host=FASTAPI_HOST,
        port=FASTAPI_PORT,
        log_level="info",
    )
    server = uvicorn.Server(config)
    logger.info(f"✓ FastAPI server running on http://{FASTAPI_HOST}:{FASTAPI_PORT}")
    await server.serve()


async def main():
    """Start both WebSocket and FastAPI servers concurrently"""
    logger.info("=" * 60)
    logger.info("Starting ADK Live Agent Server")
    logger.info("=" * 60)

    # Run both servers concurrently
    await asyncio.gather(
        run_websocket_server(),
        run_fastapi_server(),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("\nServer shutdown requested")
    except Exception as e:
        logger.error(f"Server error: {e}")
        sys.exit(1)

