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
Twilio Phone Number Management Service
Handles listing available numbers and purchasing new numbers
"""

import logging
from typing import List, Dict, Optional
from twilio.rest import Client
from twilio.base.exceptions import TwilioRestException

from config.twilio_config import twilio_config

logger = logging.getLogger(__name__)


class TwilioPhoneService:
    """Service for managing Twilio phone numbers"""

    def __init__(self):
        """Initialize Twilio client"""
        if not twilio_config.is_configured:
            raise ValueError("Twilio configuration is incomplete. Please set TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN")

        self.client = Client(
            twilio_config.account_sid,
            twilio_config.auth_token
        )
        self._account_info = None

    def search_available_numbers(
        self,
        country_code: str = "US",
        area_code: Optional[str] = None,
        contains: Optional[str] = None,
        limit: int = 20,
        sms_enabled: Optional[bool] = None,
        voice_enabled: Optional[bool] = None,
        mms_enabled: Optional[bool] = None
    ) -> List[Dict]:
        """
        Search for available phone numbers to purchase.

        Args:
            country_code: ISO country code (default: US)
            area_code: Filter by area code (e.g., "415")
            contains: Filter numbers containing this pattern (e.g., "555")
            limit: Maximum number of results (default: 20)
            sms_enabled: Filter for SMS capability (None = don't filter)
            voice_enabled: Filter for voice capability (None = don't filter)
            mms_enabled: Filter for MMS capability (None = don't filter)

        Returns:
            List of available phone numbers with their capabilities
        """
        try:
            logger.info(f"Searching for available numbers in {country_code}, area_code: {area_code}")

            # Build search parameters - only include capability filters if explicitly set
            search_params = {"limit": limit}

            if sms_enabled is not None:
                search_params["sms_enabled"] = sms_enabled
            if voice_enabled is not None:
                search_params["voice_enabled"] = voice_enabled
            if mms_enabled is not None:
                search_params["mms_enabled"] = mms_enabled

            if area_code:
                search_params["area_code"] = area_code
            if contains:
                search_params["contains"] = contains

            # Detect if this is a toll-free area code
            toll_free_codes = ["800", "888", "877", "866", "855", "844", "833"]
            is_toll_free = area_code in toll_free_codes if area_code else False

            # Search for available numbers (toll-free or local)
            if is_toll_free:
                logger.info(f"Searching toll-free numbers for area code {area_code}")
                available_numbers = self.client.available_phone_numbers(country_code).toll_free.list(**search_params)
            else:
                logger.info(f"Searching local numbers")
                available_numbers = self.client.available_phone_numbers(country_code).local.list(**search_params)

            # Format response
            results = []
            for number in available_numbers:
                results.append({
                    "phone_number": number.phone_number,
                    "friendly_name": number.friendly_name,
                    "iso_country": number.iso_country,
                    "region": number.region,
                    "postal_code": number.postal_code,
                    "rate_center": number.rate_center,
                    "capabilities": {
                        "voice": number.capabilities.get("voice", False),
                        "sms": number.capabilities.get("SMS", False),
                        "mms": number.capabilities.get("MMS", False)
                    },
                    "address_requirements": number.address_requirements
                })

            logger.info(f"Found {len(results)} available numbers")
            return results

        except TwilioRestException as e:
            logger.error(f"Twilio API error searching numbers: {e}")
            raise
        except Exception as e:
            logger.error(f"Error searching available numbers: {e}")
            raise

    def purchase_phone_number(
        self,
        phone_number: str,
        voice_url: Optional[str] = None,
        sms_url: Optional[str] = None,
        status_callback: Optional[str] = None,
        friendly_name: Optional[str] = None,
        skip_trial_check: bool = False
    ) -> Dict:
        """
        Purchase a phone number from Twilio.

        Args:
            phone_number: The phone number to purchase (E.164 format, e.g., "+14155551234")
            voice_url: URL for incoming voice calls webhook
            sms_url: URL for incoming SMS webhook
            status_callback: URL for status callbacks
            friendly_name: Friendly name for the number
            skip_trial_check: Skip trial account limitation check (default: False)

        Returns:
            Information about the purchased phone number

        Raises:
            ValueError: If trial account already has a phone number
            TwilioRestException: If Twilio API returns an error
        """
        try:
            logger.info(f"Attempting to purchase number: {phone_number}")

            # Check trial account limitations
            if not skip_trial_check:
                purchase_check = self.can_purchase_more_numbers()
                if not purchase_check["can_purchase"]:
                    error_msg = purchase_check["reason"]
                    logger.warning(f"Cannot purchase number: {error_msg}")
                    raise ValueError(error_msg)

            # Build purchase parameters
            purchase_params = {
                "phone_number": phone_number
            }

            # Use configured webhook URLs if not provided
            if voice_url:
                purchase_params["voice_url"] = voice_url
            elif twilio_config.is_configured:
                purchase_params["voice_url"] = twilio_config.incoming_webhook_url

            if sms_url:
                purchase_params["sms_url"] = sms_url

            if status_callback:
                purchase_params["status_callback"] = status_callback
            elif twilio_config.is_configured:
                purchase_params["status_callback"] = twilio_config.status_webhook_url

            if friendly_name:
                purchase_params["friendly_name"] = friendly_name

            # Purchase the number
            incoming_phone_number = self.client.incoming_phone_numbers.create(**purchase_params)

            # Check if SMS capability needs to be enabled
            sms_capability = incoming_phone_number.capabilities.get("SMS", False)
            if (sms_url or (twilio_config.is_configured and not sms_url)) and not sms_capability:
                logger.info(f"SMS webhook configured for {phone_number} but SMS capability is disabled. Attempting to enable SMS...")
                try:
                    # Re-fetch and update to enable SMS capability
                    incoming_phone_number = self.client.incoming_phone_numbers(incoming_phone_number.sid).update(
                        sms_url=sms_url if sms_url else "https://example.com/sms"
                    )
                    sms_capability = incoming_phone_number.capabilities.get("SMS", False)
                    
                    if sms_capability:
                        logger.info(f"Successfully enabled SMS capability for newly purchased number: {phone_number}")
                    else:
                        logger.warning(f"SMS capability could not be enabled for {phone_number}. The phone number may not support SMS in its region.")
                except Exception as e:
                    logger.warning(f"Could not enable SMS capability for newly purchased number: {e}. SMS webhook will not work.")

            result = {
                "sid": incoming_phone_number.sid,
                "phone_number": incoming_phone_number.phone_number,
                "friendly_name": incoming_phone_number.friendly_name,
                "status": incoming_phone_number.status,
                "capabilities": {
                    "voice": incoming_phone_number.capabilities.get("voice", False),
                    "sms": incoming_phone_number.capabilities.get("SMS", False),
                    "mms": incoming_phone_number.capabilities.get("MMS", False)
                },
                "voice_url": incoming_phone_number.voice_url,
                "sms_url": incoming_phone_number.sms_url,
                "status_callback": incoming_phone_number.status_callback,
                "date_created": str(incoming_phone_number.date_created)
            }

            logger.info(f"Successfully purchased number: {phone_number} (SID: {incoming_phone_number.sid})")
            return result

        except TwilioRestException as e:
            logger.error(f"Twilio API error purchasing number: {e}")
            raise
        except Exception as e:
            logger.error(f"Error purchasing phone number: {e}")
            raise

    def list_owned_numbers(self, limit: int = 50) -> List[Dict]:
        """
        List all phone numbers owned by this Twilio account with complete details.

        Args:
            limit: Maximum number of results (default: 50)

        Returns:
            List of owned phone numbers with their complete details
        """
        try:
            logger.info("Fetching owned phone numbers")

            owned_numbers = self.client.incoming_phone_numbers.list(limit=limit)

            results = []
            for number in owned_numbers:
                results.append({
                    # Basic Information
                    "sid": number.sid,
                    "phone_number": number.phone_number,
                    "friendly_name": number.friendly_name,
                    "status": number.status,
                    "account_sid": number.account_sid,

                    # Capabilities
                    "capabilities": {
                        "voice": number.capabilities.get("voice", False),
                        "sms": number.capabilities.get("SMS", False),
                        "mms": number.capabilities.get("MMS", False)
                    },

                    # Voice Configuration
                    "voice_url": number.voice_url,
                    "voice_method": number.voice_method,
                    "voice_fallback_url": number.voice_fallback_url,
                    "voice_fallback_method": number.voice_fallback_method,
                    "voice_application_sid": number.voice_application_sid,
                    "voice_caller_id_lookup": getattr(number, 'voice_caller_id_lookup', None),

                    # SMS Configuration
                    "sms_url": number.sms_url,
                    "sms_method": number.sms_method,
                    "sms_fallback_url": number.sms_fallback_url,
                    "sms_fallback_method": number.sms_fallback_method,
                    "sms_application_sid": number.sms_application_sid,

                    # Status Callback
                    "status_callback": number.status_callback,
                    "status_callback_method": number.status_callback_method,

                    # Compliance & Identity
                    "address_sid": getattr(number, 'address_sid', None),
                    "address_requirements": getattr(number, 'address_requirements', None),
                    "identity_sid": getattr(number, 'identity_sid', None),
                    "bundle_sid": getattr(number, 'bundle_sid', None),

                    # Emergency Services
                    "emergency_status": getattr(number, 'emergency_status', None),
                    "emergency_address_sid": getattr(number, 'emergency_address_sid', None),

                    # Technical Details
                    "api_version": number.api_version,
                    "beta": getattr(number, 'beta', False),
                    "origin": getattr(number, 'origin', None),
                    "trunk_sid": getattr(number, 'trunk_sid', None),

                    # Timestamps
                    "date_created": str(number.date_created),
                    "date_updated": str(number.date_updated),

                    # Resource URI
                    "uri": number.uri
                })

            logger.info(f"Found {len(results)} owned numbers")
            return results

        except TwilioRestException as e:
            logger.error(f"Twilio API error listing numbers: {e}")
            raise
        except Exception as e:
            logger.error(f"Error listing owned numbers: {e}")
            raise

    def update_phone_number_webhooks(
        self,
        phone_number_sid: str,
        voice_url: Optional[str] = None,
        sms_url: Optional[str] = None,
        status_callback: Optional[str] = None,
        friendly_name: Optional[str] = None,
        voice_fallback_url: Optional[str] = None,
        sms_fallback_url: Optional[str] = None,
        status_callback_method: Optional[str] = None,
        voice_method: Optional[str] = None,
        sms_method: Optional[str] = None
    ) -> Dict:
        """
        Update webhook URLs and other settings for an existing phone number.

        Args:
            phone_number_sid: The SID of the phone number to update
            voice_url: URL for incoming voice calls webhook
            sms_url: URL for incoming SMS webhook
            status_callback: URL for status callbacks
            friendly_name: Friendly name for the number
            voice_fallback_url: Fallback URL if voice_url fails
            sms_fallback_url: Fallback URL if sms_url fails
            status_callback_method: HTTP method for status callback (GET or POST)
            voice_method: HTTP method for voice webhook (GET or POST)
            sms_method: HTTP method for SMS webhook (GET or POST)

        Returns:
            Updated phone number information
        """
        try:
            logger.info(f"Updating webhook configuration for phone number SID: {phone_number_sid}")

            # First, fetch the current phone number to check capabilities
            current_number = self.client.incoming_phone_numbers(phone_number_sid).fetch()
            current_sms_capability = current_number.capabilities.get("SMS", False)
            
            # Check if SMS webhook is being set but SMS capability is not enabled
            if (sms_url is not None or sms_fallback_url is not None) and not current_sms_capability:
                logger.info(f"SMS webhook requested for {phone_number_sid} but SMS capability is disabled. Attempting to enable SMS...")
                try:
                    # Try to enable SMS capability
                    current_number = self.client.incoming_phone_numbers(phone_number_sid).update(
                        sms_url=sms_url if sms_url else "https://example.com/sms"  # Temporary URL to enable capability
                    )
                    current_sms_capability = current_number.capabilities.get("SMS", False)
                    
                    if current_sms_capability:
                        logger.info(f"Successfully enabled SMS capability for {phone_number_sid}")
                    else:
                        logger.warning(f"SMS capability could not be enabled. The phone number may not support SMS in its region.")
                except Exception as e:
                    logger.warning(f"Could not enable SMS capability: {e}. SMS webhook will not work.")

            # Build update parameters (only include provided values)
            update_params = {}

            if voice_url is not None:
                update_params["voice_url"] = voice_url
            if sms_url is not None:
                update_params["sms_url"] = sms_url
            if status_callback is not None:
                update_params["status_callback"] = status_callback
            if friendly_name is not None:
                update_params["friendly_name"] = friendly_name
            if voice_fallback_url is not None:
                update_params["voice_fallback_url"] = voice_fallback_url
            if sms_fallback_url is not None:
                update_params["sms_fallback_url"] = sms_fallback_url
            if status_callback_method is not None:
                update_params["status_callback_method"] = status_callback_method
            if voice_method is not None:
                update_params["voice_method"] = voice_method
            if sms_method is not None:
                update_params["sms_method"] = sms_method

            if not update_params:
                logger.warning("No parameters provided for update")
                raise ValueError("At least one parameter must be provided for update")

            # Update the phone number
            incoming_phone_number = self.client.incoming_phone_numbers(phone_number_sid).update(**update_params)

            result = {
                "sid": incoming_phone_number.sid,
                "phone_number": incoming_phone_number.phone_number,
                "friendly_name": incoming_phone_number.friendly_name,
                "status": incoming_phone_number.status,
                "capabilities": {
                    "voice": incoming_phone_number.capabilities.get("voice", False),
                    "sms": incoming_phone_number.capabilities.get("SMS", False),
                    "mms": incoming_phone_number.capabilities.get("MMS", False)
                },
                "voice_url": incoming_phone_number.voice_url,
                "voice_method": incoming_phone_number.voice_method,
                "voice_fallback_url": incoming_phone_number.voice_fallback_url,
                "voice_fallback_method": incoming_phone_number.voice_fallback_method,
                "sms_url": incoming_phone_number.sms_url,
                "sms_method": incoming_phone_number.sms_method,
                "sms_fallback_url": incoming_phone_number.sms_fallback_url,
                "sms_fallback_method": incoming_phone_number.sms_fallback_method,
                "status_callback": incoming_phone_number.status_callback,
                "status_callback_method": incoming_phone_number.status_callback_method,
                "date_created": str(incoming_phone_number.date_created),
                "date_updated": str(incoming_phone_number.date_updated)
            }

            logger.info(f"Successfully updated phone number: {phone_number_sid}")
            return result

        except TwilioRestException as e:
            logger.error(f"Twilio API error updating phone number: {e}")
            raise
        except Exception as e:
            logger.error(f"Error updating phone number: {e}")
            raise

    def delete_phone_number(self, phone_number_sid: str) -> bool:
        """
        Delete (release) a phone number from Twilio account.

        Args:
            phone_number_sid: The SID of the phone number to delete

        Returns:
            True if successful
        """
        try:
            logger.info(f"Deleting phone number with SID: {phone_number_sid}")

            self.client.incoming_phone_numbers(phone_number_sid).delete()

            logger.info(f"Successfully deleted phone number: {phone_number_sid}")
            return True

        except TwilioRestException as e:
            logger.error(f"Twilio API error deleting number: {e}")
            raise
        except Exception as e:
            logger.error(f"Error deleting phone number: {e}")
            raise

    def get_account_info(self) -> Dict:
        """
        Get information about the Twilio account.

        Returns:
            Account information including status, type, and balance
        """
        try:
            if self._account_info is None:
                account = self.client.api.accounts(twilio_config.account_sid).fetch()
                self._account_info = {
                    "account_sid": account.sid,
                    "friendly_name": account.friendly_name,
                    "status": account.status,
                    "type": account.type,
                    "date_created": str(account.date_created),
                    "date_updated": str(account.date_updated)
                }

            return self._account_info

        except TwilioRestException as e:
            logger.error(f"Twilio API error fetching account info: {e}")
            raise
        except Exception as e:
            logger.error(f"Error fetching account info: {e}")
            raise

    def is_trial_account(self) -> bool:
        """
        Check if this is a trial account.

        Returns:
            True if trial account, False otherwise
        """
        try:
            account_info = self.get_account_info()
            return account_info.get("type") == "Trial"
        except:
            return False

    def can_purchase_more_numbers(self) -> Dict:
        """
        Check if the account can purchase more phone numbers.
        Trial accounts are limited to 1 phone number.

        Returns:
            Dictionary with 'can_purchase' boolean and 'reason' string
        """
        try:
            is_trial = self.is_trial_account()
            owned_numbers = self.list_owned_numbers(limit=5)
            owned_count = len(owned_numbers)

            if is_trial and owned_count >= 1:
                return {
                    "can_purchase": False,
                    "reason": "Trial accounts are limited to 1 phone number. Please upgrade your account to purchase more.",
                    "is_trial": True,
                    "owned_count": owned_count,
                    "owned_numbers": owned_numbers,
                    "upgrade_url": "https://www.twilio.com/console/billing/upgrade"
                }

            return {
                "can_purchase": True,
                "reason": "Account can purchase more numbers",
                "is_trial": is_trial,
                "owned_count": owned_count
            }

        except Exception as e:
            logger.error(f"Error checking purchase capability: {e}")
            return {
                "can_purchase": False,
                "reason": f"Error checking account: {str(e)}",
                "is_trial": None,
                "owned_count": 0
            }

    def list_call_logs(
        self,
        phone_number: Optional[str] = None,
        direction: Optional[str] = None,
        status: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: int = 50
    ) -> List[Dict]:
        """
        List call logs with optional filters.

        Args:
            phone_number: Filter by phone number (searches both to/from)
            direction: Filter by direction (inbound, outbound-api, outbound-dial)
            status: Filter by status (queued, ringing, in-progress, completed, failed, busy, no-answer)
            start_date: Filter calls after this date (ISO 8601 format: YYYY-MM-DD)
            end_date: Filter calls before this date (ISO 8601 format: YYYY-MM-DD)
            limit: Maximum number of results (default: 50)

        Returns:
            List of call records with complete details
        """
        try:
            logger.info(f"Fetching call logs (phone: {phone_number}, status: {status}, limit: {limit})")

            # Build filter parameters
            filter_params = {"limit": limit}

            if phone_number:
                # Try both 'to' and 'from' searches
                to_calls = self.client.calls.list(to=phone_number, limit=limit)
                from_calls = self.client.calls.list(from_=phone_number, limit=limit)

                # Combine and deduplicate by SID
                all_calls = list({call.sid: call for call in (to_calls + from_calls)}.values())

                # Apply additional filters manually
                if status:
                    all_calls = [c for c in all_calls if c.status == status]
                if direction:
                    all_calls = [c for c in all_calls if c.direction == direction]

                calls = sorted(all_calls, key=lambda x: x.date_created, reverse=True)[:limit]
            else:
                # No phone number filter, use API filters
                if status:
                    filter_params["status"] = status
                if start_date:
                    filter_params["start_time_after"] = start_date
                if end_date:
                    filter_params["end_time_before"] = end_date

                calls = self.client.calls.list(**filter_params)

            # Format results
            results = []
            for call in calls:
                results.append({
                    # Core Identifiers
                    "sid": call.sid,
                    "account_sid": call.account_sid,
                    "parent_call_sid": call.parent_call_sid,
                    "phone_number_sid": getattr(call, 'phone_number_sid', None),

                    # Participants
                    "to": call.to,
                    "to_formatted": call.to_formatted,
                    "from": call.from_,
                    "from_formatted": call.from_formatted,
                    "caller_name": getattr(call, 'caller_name', None),
                    "forwarded_from": call.forwarded_from,

                    # Call Status & Timeline
                    "status": call.status,
                    "direction": call.direction,
                    "start_time": str(call.start_time) if call.start_time else None,
                    "end_time": str(call.end_time) if call.end_time else None,
                    "duration": call.duration,
                    "date_created": str(call.date_created),
                    "date_updated": str(call.date_updated),

                    # Call Details
                    "answered_by": call.answered_by,
                    "queue_time": getattr(call, 'queue_time', None),

                    # Financial
                    "price": call.price,
                    "price_unit": call.price_unit,

                    # Technical
                    "api_version": call.api_version,
                    "trunk_sid": getattr(call, 'trunk_sid', None),
                    "group_sid": getattr(call, 'group_sid', None),
                    "uri": call.uri
                })

            logger.info(f"Found {len(results)} call records")
            return results

        except TwilioRestException as e:
            logger.error(f"Twilio API error fetching call logs: {e}")
            raise
        except Exception as e:
            logger.error(f"Error fetching call logs: {e}")
            raise

    def get_call_details(self, call_sid: str) -> Dict:
        """
        Get detailed information about a specific call.

        Args:
            call_sid: The SID of the call to retrieve

        Returns:
            Complete call details
        """
        try:
            logger.info(f"Fetching call details for SID: {call_sid}")

            call = self.client.calls(call_sid).fetch()

            result = {
                # Core Identifiers
                "sid": call.sid,
                "account_sid": call.account_sid,
                "parent_call_sid": call.parent_call_sid,
                "phone_number_sid": getattr(call, 'phone_number_sid', None),

                # Participants
                "to": call.to,
                "to_formatted": call.to_formatted,
                "from": call.from_,
                "from_formatted": call.from_formatted,
                "caller_name": getattr(call, 'caller_name', None),
                "forwarded_from": call.forwarded_from,

                # Call Status & Timeline
                "status": call.status,
                "direction": call.direction,
                "start_time": str(call.start_time) if call.start_time else None,
                "end_time": str(call.end_time) if call.end_time else None,
                "duration": call.duration,
                "date_created": str(call.date_created),
                "date_updated": str(call.date_updated),

                # Call Details
                "answered_by": call.answered_by,
                "queue_time": getattr(call, 'queue_time', None),

                # Financial
                "price": call.price,
                "price_unit": call.price_unit,

                # Technical
                "api_version": call.api_version,
                "trunk_sid": getattr(call, 'trunk_sid', None),
                "group_sid": getattr(call, 'group_sid', None),
                "uri": call.uri,

                # Subresources
                "subresource_uris": call.subresource_uris
            }

            logger.info(f"Successfully retrieved call details for: {call_sid}")
            return result

        except TwilioRestException as e:
            logger.error(f"Twilio API error fetching call details: {e}")
            raise
        except Exception as e:
            logger.error(f"Error fetching call details: {e}")
            raise


# Global instance
phone_service = TwilioPhoneService() if twilio_config.is_configured else None
