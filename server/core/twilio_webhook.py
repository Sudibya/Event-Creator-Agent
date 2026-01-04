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
Twilio webhook endpoints using FastAPI
Handles incoming call webhooks and provides TwiML responses
"""

import logging
from typing import Optional
from pathlib import Path
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException, Query
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from config.twilio_config import twilio_config
from core.twilio_handler import handle_twilio_call
from core.twilio_phone_service import phone_service
from core.adk_websocket_handler import handle_adk_client

logger = logging.getLogger(__name__)


# Pydantic models for request/response
class PurchaseNumberRequest(BaseModel):
    phone_number: str
    voice_url: Optional[str] = None
    sms_url: Optional[str] = None
    status_callback: Optional[str] = None
    friendly_name: Optional[str] = None


class UpdateNumberRequest(BaseModel):
    voice_url: Optional[str] = None
    sms_url: Optional[str] = None
    status_callback: Optional[str] = None
    friendly_name: Optional[str] = None
    voice_fallback_url: Optional[str] = None
    sms_fallback_url: Optional[str] = None
    status_callback_method: Optional[str] = None
    voice_method: Optional[str] = None
    sms_method: Optional[str] = None


# Create FastAPI app
app = FastAPI(title="Twilio Webhook Server")

# Configure CORS for Twilio requests
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ===== STATIC FILES SERVING (from Bondly pattern) =====
# Mount static files for voice chat web UI
static_dir = Path(__file__).parent.parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    logger.info(f"✅ Mounted static files from: {static_dir}")
else:
    logger.warning(f"⚠️ Static directory not found: {static_dir}")


@app.post("/twilio/incoming")
async def incoming_call(request: Request):
    """
    Webhook endpoint for incoming Twilio calls.
    Returns TwiML response to connect call to Media Streams.
    """
    try:
        # Get form data from Twilio
        form_data = await request.form()
        call_sid = form_data.get("CallSid")
        from_number = form_data.get("From")
        to_number = form_data.get("To")

        logger.info(f"Incoming call: {call_sid} from {from_number} to {to_number}")

        # Get Media Streams WebSocket URL
        media_stream_url = twilio_config.get_media_streams_url_for_twiml()

        # Generate TwiML response
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say>Connecting you to the AI assistant.</Say>
    <Connect>
        <Stream url="{media_stream_url}"></Stream>
    </Connect>
</Response>"""

        logger.info(f"Returning TwiML with Media Streams URL: {media_stream_url}")

        return Response(content=twiml, media_type="application/xml")

    except Exception as e:
        logger.error(f"Error handling incoming call: {e}")

        # Return error TwiML
        error_twiml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say>Sorry, there was an error connecting your call. Please try again later.</Say>
    <Hangup/>
</Response>"""
        return Response(content=error_twiml, media_type="application/xml")


class WebSocketWrapper:
    """
    Wrapper to make FastAPI WebSocket compatible with websockets library API.
    The ADK handler was written for websockets library, this makes it work with FastAPI.
    """
    def __init__(self, fastapi_websocket: WebSocket):
        self.ws = fastapi_websocket

    async def send(self, message):
        """Send message - handle both text and binary"""
        if isinstance(message, str):
            await self.ws.send_text(message)
        elif isinstance(message, bytes):
            await self.ws.send_bytes(message)
        else:
            await self.ws.send_text(str(message))

    async def recv(self):
        """Receive message"""
        return await self.ws.receive_text()

    async def close(self):
        """Close connection"""
        await self.ws.close()

    def __aiter__(self):
        """Make it async iterable"""
        return self

    async def __anext__(self):
        """Async iteration"""
        try:
            data = await self.ws.receive_text()
            return data
        except WebSocketDisconnect:
            raise StopAsyncIteration


@app.websocket("/ws")
async def client_websocket(websocket: WebSocket):
    """
    WebSocket endpoint for web client voice chat connections.
    Handles bidirectional audio streaming with ADK agent.
    """
    logger.info("🔌 Client WebSocket connection attempt received")

    try:
        await websocket.accept()
        logger.info("✅ Client WebSocket connection accepted")

        # Wrap FastAPI WebSocket to be compatible with websockets library
        wrapped_ws = WebSocketWrapper(websocket)

        # Handle the client connection using ADK handler
        await handle_adk_client(wrapped_ws)

    except WebSocketDisconnect as e:
        logger.info(f"📴 Client WebSocket disconnected: {e}")
    except Exception as e:
        logger.error(f"❌ Error in Client WebSocket: {e}")
        import traceback
        logger.error(f"Full traceback:\n{traceback.format_exc()}")
    finally:
        try:
            await websocket.close()
        except:
            pass


@app.websocket("/twilio/media-stream")
async def media_stream_websocket(websocket: WebSocket):
    """
    WebSocket endpoint for Twilio Media Streams.
    Handles bidirectional audio streaming with ADK agent.
    """
    print("=" * 60)
    print("🔌 WEBSOCKET CONNECTION ATTEMPT RECEIVED!")
    print("=" * 60)
    logger.info("🔌 WebSocket connection attempt received")

    try:
        await websocket.accept()
        print("✅ WEBSOCKET ACCEPTED!")
        logger.info("✅ Twilio Media Streams WebSocket connection accepted")

        # Handle the Twilio call using our handler
        await handle_twilio_call(websocket)

    except WebSocketDisconnect as e:
        logger.info(f"📴 Twilio Media Streams WebSocket disconnected: {e}")
    except Exception as e:
        logger.error(f"❌ Error in Media Streams WebSocket: {e}")
        import traceback
        logger.error(f"Full traceback:\n{traceback.format_exc()}")
    finally:
        try:
            await websocket.close()
        except:
            pass


@app.post("/twilio/status")
async def call_status(request: Request):
    """
    Optional webhook endpoint for call status updates.
    Receives events like call answered, completed, failed, etc.
    """
    try:
        form_data = await request.form()
        call_sid = form_data.get("CallSid")
        call_status = form_data.get("CallStatus")

        logger.info(f"Call status update: {call_sid} - {call_status}")

        # Handle different call statuses
        if call_status == "completed":
            logger.info(f"Call completed: {call_sid}")
        elif call_status == "failed":
            logger.warning(f"Call failed: {call_sid}")
        elif call_status == "busy":
            logger.info(f"Call busy: {call_sid}")
        elif call_status == "no-answer":
            logger.info(f"Call no answer: {call_sid}")

        # Return 200 OK to Twilio
        return {"status": "ok"}

    except Exception as e:
        logger.error(f"Error handling call status: {e}")
        return {"status": "error", "message": str(e)}


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "twilio_configured": twilio_config.is_configured,
        "media_streams_url": twilio_config.media_streams_url
    }


@app.get("/twilio/account/info")
async def get_account_info():
    """
    Get Twilio account information including account type (Trial/Paid).

    This is useful to check if you have a trial account which has limitations.
    """
    try:
        if not phone_service:
            raise HTTPException(
                status_code=503,
                detail="Twilio phone service not configured. Please set TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN"
            )

        account_info = phone_service.get_account_info()

        return {
            "status": "success",
            "account": account_info
        }

    except Exception as e:
        logger.error(f"Error fetching account info: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/twilio/account/purchase-capability")
async def check_purchase_capability():
    """
    Check if the account can purchase more phone numbers.

    Trial accounts are limited to 1 phone number. This endpoint will tell you:
    - Whether you can purchase more numbers
    - If not, why not (with owned numbers list)
    - Upgrade URL if you're on a trial account
    """
    try:
        if not phone_service:
            raise HTTPException(
                status_code=503,
                detail="Twilio phone service not configured. Please set TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN"
            )

        capability = phone_service.can_purchase_more_numbers()

        return {
            "status": "success",
            "capability": capability
        }

    except Exception as e:
        logger.error(f"Error checking purchase capability: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# Phone number management endpoints
@app.get("/twilio/numbers/available")
async def search_available_numbers(
    country_code: str = Query("US", description="ISO country code (e.g., US, GB, CA)"),
    area_code: Optional[str] = Query(None, description="Filter by area code (e.g., 415)"),
    contains: Optional[str] = Query(None, description="Filter numbers containing this pattern"),
    limit: int = Query(20, ge=1, le=50, description="Maximum number of results"),
    sms_enabled: Optional[bool] = Query(None, description="Filter for SMS capability (true/false, omit for any)"),
    voice_enabled: Optional[bool] = Query(None, description="Filter for voice capability (true/false, omit for any)"),
    mms_enabled: Optional[bool] = Query(None, description="Filter for MMS capability (true/false, omit for any)")
):
    """
    Search for available phone numbers to purchase from Twilio.

    Query parameters:
    - country_code: ISO country code (default: US)
    - area_code: Filter by area code (e.g., "415") or toll-free (e.g., "800")
    - contains: Filter numbers containing this pattern (e.g., "555")
    - limit: Maximum number of results (default: 20, max: 50)
    - sms_enabled: Filter for SMS capability (omit to get any)
    - voice_enabled: Filter for voice capability (omit to get any)
    - mms_enabled: Filter for MMS capability (omit to get any)

    Example: Just call /twilio/numbers/available to get any available numbers
    """
    try:
        if not phone_service:
            raise HTTPException(
                status_code=503,
                detail="Twilio phone service not configured. Please set TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN"
            )

        results = phone_service.search_available_numbers(
            country_code=country_code,
            area_code=area_code,
            contains=contains,
            limit=limit,
            sms_enabled=sms_enabled,
            voice_enabled=voice_enabled,
            mms_enabled=mms_enabled
        )

        return {
            "status": "success",
            "count": len(results),
            "numbers": results
        }

    except Exception as e:
        logger.error(f"Error searching available numbers: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/twilio/numbers/purchase")
async def purchase_number(request: PurchaseNumberRequest):
    """
    Purchase a phone number from Twilio.

    Request body:
    - phone_number: The phone number to purchase (E.164 format, e.g., "+14155551234")
    - voice_url: Optional URL for incoming voice calls webhook
    - sms_url: Optional URL for incoming SMS webhook
    - status_callback: Optional URL for status callbacks
    - friendly_name: Optional friendly name for the number

    If voice_url and status_callback are not provided, the configured webhook URLs will be used.

    NOTE: Trial accounts are limited to 1 phone number. Use GET /twilio/account/purchase-capability
    to check if you can purchase more numbers before calling this endpoint.
    """
    try:
        if not phone_service:
            raise HTTPException(
                status_code=503,
                detail="Twilio phone service not configured. Please set TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN"
            )

        result = phone_service.purchase_phone_number(
            phone_number=request.phone_number,
            voice_url=request.voice_url,
            sms_url=request.sms_url,
            status_callback=request.status_callback,
            friendly_name=request.friendly_name
        )

        return {
            "status": "success",
            "message": f"Successfully purchased {request.phone_number}",
            "number": result
        }

    except ValueError as e:
        # Trial account limitation or other validation errors
        logger.warning(f"Purchase validation failed: {e}")

        # Get additional context
        capability = phone_service.can_purchase_more_numbers()

        raise HTTPException(
            status_code=400,
            detail={
                "error": str(e),
                "capability": capability
            }
        )
    except Exception as e:
        logger.error(f"Error purchasing number: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/twilio/numbers/owned")
async def list_owned_numbers(
    limit: int = Query(50, ge=1, le=100, description="Maximum number of results")
):
    """
    List all phone numbers currently owned by this Twilio account.

    Query parameters:
    - limit: Maximum number of results (default: 50, max: 100)
    """
    try:
        if not phone_service:
            raise HTTPException(
                status_code=503,
                detail="Twilio phone service not configured. Please set TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN"
            )

        results = phone_service.list_owned_numbers(limit=limit)

        return {
            "status": "success",
            "count": len(results),
            "numbers": results
        }

    except Exception as e:
        logger.error(f"Error listing owned numbers: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/twilio/numbers/{phone_number_sid}")
async def delete_number(phone_number_sid: str):
    """
    Delete (release) a phone number from this Twilio account.

    Path parameters:
    - phone_number_sid: The SID of the phone number to delete (e.g., "PN...")
    """
    try:
        if not phone_service:
            raise HTTPException(
                status_code=503,
                detail="Twilio phone service not configured. Please set TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN"
            )

        success = phone_service.delete_phone_number(phone_number_sid)

        return {
            "status": "success",
            "message": f"Successfully deleted phone number {phone_number_sid}"
        }

    except Exception as e:
        logger.error(f"Error deleting number: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/")
async def serve_voice_ui():
    """
    Serve the voice chat web UI (from Bondly pattern)
    """
    from fastapi.responses import FileResponse

    index_path = Path(__file__).parent.parent / "static" / "index.html"
    if index_path.exists():
        return FileResponse(index_path)
    else:
        return {
            "service": "Voice Chat & Twilio Webhook Server",
            "endpoints": {
                "voice_ui": "/ (this page)",
                "client_websocket": "/ws (WebSocket)",
                "incoming_call": "/twilio/incoming",
                "media_stream": "/twilio/media-stream (WebSocket)",
                "call_status": "/twilio/status",
                "health": "/health",
                "account_management": {
                    "account_info": "/twilio/account/info",
                    "purchase_capability": "/twilio/account/purchase-capability"
                },
                "phone_management": {
                    "search_available": "/twilio/numbers/available",
                    "purchase": "/twilio/numbers/purchase",
                    "list_owned": "/twilio/numbers/owned",
                    "update": "/twilio/numbers/update/{phone_number_sid}",
                    "delete": "/twilio/numbers/{phone_number_sid}"
                },
                "call_logs": {
                    "list_calls": "/twilio/calls/logs",
                    "call_details": "/twilio/calls/{call_sid}"
                }
            }
        }



@app.get("/twilio/calls/logs")
async def get_call_logs(
    phone_number: Optional[str] = Query(None, description="Filter by phone number (E.164 format)"),
    direction: Optional[str] = Query(None, description="Filter by direction (inbound, outbound-api, outbound-dial)"),
    status: Optional[str] = Query(None, description="Filter by status (completed, failed, busy, no-answer, etc.)"),
    start_date: Optional[str] = Query(None, description="Filter calls after this date (YYYY-MM-DD)"),
    end_date: Optional[str] = Query(None, description="Filter calls before this date (YYYY-MM-DD)"),
    limit: int = Query(50, ge=1, le=100, description="Maximum number of results")
):
    """
    Retrieve call history/logs with optional filters.

    Query parameters:
    - phone_number: Filter by phone number (searches both to/from)
    - direction: Filter by call direction
    - status: Filter by call status
    - start_date: Filter calls after this date (ISO format: YYYY-MM-DD)
    - end_date: Filter calls before this date (ISO format: YYYY-MM-DD)
    - limit: Maximum number of results (default: 50, max: 100)

    Returns comprehensive call data including duration, price, timestamps, and participants.
    """
    try:
        if not phone_service:
            raise HTTPException(
                status_code=503,
                detail="Twilio phone service not configured. Please set TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN"
            )

        results = phone_service.list_call_logs(
            phone_number=phone_number,
            direction=direction,
            status=status,
            start_date=start_date,
            end_date=end_date,
            limit=limit
        )

        return {
            "status": "success",
            "count": len(results),
            "calls": results
        }

    except Exception as e:
        logger.error(f"Error fetching call logs: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/twilio/calls/{call_sid}")
async def get_call_details(call_sid: str):
    """
    Get detailed information about a specific call.

    Path parameters:
    - call_sid: The SID of the call (e.g., "CA...")

    Returns complete call details including all metadata, pricing, and technical information.
    """
    try:
        if not phone_service:
            raise HTTPException(
                status_code=503,
                detail="Twilio phone service not configured. Please set TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN"
            )

        result = phone_service.get_call_details(call_sid)

        return {
            "status": "success",
            "call": result
        }

    except Exception as e:
        logger.error(f"Error fetching call details: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/twilio/numbers/update/{phone_number_sid}")
async def update_number(phone_number_sid: str, request: UpdateNumberRequest):
    """
    Update the webhook URLs and settings for an existing phone number.

    Path parameters:
    - phone_number_sid: The SID of the phone number to update (e.g., "PN...")

    Request body (all optional):
    - voice_url: URL for incoming voice calls webhook
    - sms_url: URL for incoming SMS webhook
    - status_callback: URL for status callbacks
    - friendly_name: Friendly name for the number
    - voice_fallback_url: Fallback URL if voice_url fails
    - sms_fallback_url: Fallback URL if sms_url fails
    - voice_method: HTTP method for voice webhook (GET or POST)
    - sms_method: HTTP method for SMS webhook (GET or POST)
    - status_callback_method: HTTP method for status callback (GET or POST)
    """
    try:
        if not phone_service:
            raise HTTPException(
                status_code=503,
                detail="Twilio phone service not configured. Please set TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN"
            )

        result = phone_service.update_phone_number_webhooks(
            phone_number_sid=phone_number_sid,
            voice_url=request.voice_url,
            sms_url=request.sms_url,
            status_callback=request.status_callback,
            friendly_name=request.friendly_name,
            voice_fallback_url=request.voice_fallback_url,
            sms_fallback_url=request.sms_fallback_url,
            voice_method=request.voice_method,
            sms_method=request.sms_method,
            status_callback_method=request.status_callback_method
        )

        return {
            "status": "success",
            "message": f"Successfully updated phone number {phone_number_sid}",
            "number": result
        }

    except Exception as e:
        logger.error(f"Error updating number: {e}")
        raise HTTPException(status_code=500, detail=str(e))