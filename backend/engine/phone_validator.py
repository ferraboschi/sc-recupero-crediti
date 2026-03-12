"""Phone validator module - validates and normalizes Italian phone numbers."""

import logging
from typing import Optional, Dict, Any

import phonenumbers
from phonenumbers import NumberParseException, PhoneNumberType, carrier

logger = logging.getLogger(__name__)


def normalize_phone(phone: str, default_country: str = "IT") -> Optional[str]:
    """
    Normalize a phone number to E.164 format (+39...).

    Handles Italian phone numbers with various formats:
    - +39 123 456 7890
    - +39123456789
    - 0039123456789
    - 0123456789 (assumes Italy)
    - 3123456789 (mobile without country code)

    Args:
        phone: The phone number to normalize
        default_country: ISO 3166-1 alpha-2 country code (default: "IT")

    Returns:
        Phone number in E.164 format (+39...) if valid, None if invalid

    Examples:
        >>> normalize_phone("+39 333 123 4567")
        "+39333123467"
        >>> normalize_phone("0333 123 4567")
        "+39333123467"
        >>> normalize_phone("invalid")
        None
    """
    if not phone:
        return None

    try:
        # Parse the phone number
        parsed = phonenumbers.parse(phone, default_country)

        # Validate the number
        if not phonenumbers.is_valid_number(parsed):
            logger.debug(f"Invalid phone number: {phone}")
            return None

        # Format in E.164 format
        normalized = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
        return normalized

    except NumberParseException as e:
        logger.debug(f"Failed to parse phone number '{phone}': {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error normalizing phone '{phone}': {e}")
        return None


def is_mobile(phone: str) -> bool:
    """
    Check if a phone number is a mobile number.

    Italian mobile prefixes: 3, 4, 6, 8, 9 (for companies)

    Args:
        phone: Phone number in any format or E.164 format

    Returns:
        True if the number is a mobile number, False otherwise

    Examples:
        >>> is_mobile("+39333123456")  # Vodafone mobile
        True
        >>> is_mobile("+39251234567")  # Fixed line
        False
    """
    # First normalize the number
    normalized = normalize_phone(phone)
    if not normalized:
        return False

    try:
        parsed = phonenumbers.parse(normalized, "IT")

        # Get the number type
        number_type = phonenumbers.number_type(parsed)

        # Check if it's a mobile type
        # MOBILE, FIXED_LINE_OR_MOBILE (sometimes returned for Italian numbers)
        return number_type in (
            PhoneNumberType.MOBILE,
            PhoneNumberType.FIXED_LINE_OR_MOBILE,
        )

    except Exception as e:
        logger.debug(f"Error checking if number is mobile: {e}")
        return False


def validate_for_whatsapp(phone: str) -> Dict[str, Any]:
    """
    Validate a phone number for WhatsApp messaging.

    WhatsApp can only be sent to mobile numbers (not landlines).

    Args:
        phone: Phone number in any format

    Returns:
        Dictionary with keys:
        - valid: bool - Whether the number is valid and suitable for WhatsApp
        - normalized: Optional[str] - E.164 normalized number, or None if invalid
        - is_mobile: bool - Whether the number is a mobile number
        - error: Optional[str] - Error message if validation failed, None if valid

    Examples:
        >>> result = validate_for_whatsapp("+39 333 123 4567")
        >>> result['valid']
        True
        >>> result['normalized']
        "+39333123467"

        >>> result = validate_for_whatsapp("+39 02 1234567")  # Fixed line
        >>> result['valid']
        False
        >>> result['error']
        "WhatsApp requires a mobile number (found: fixed line)"
    """
    # Normalize the phone number
    normalized = normalize_phone(phone)

    if not normalized:
        return {
            'valid': False,
            'normalized': None,
            'is_mobile': False,
            'error': f"Invalid phone number format: {phone}"
        }

    # Check if it's a mobile number
    mobile = is_mobile(normalized)

    if not mobile:
        return {
            'valid': False,
            'normalized': normalized,
            'is_mobile': False,
            'error': "WhatsApp requires a mobile number (found: fixed line or unknown type)"
        }

    return {
        'valid': True,
        'normalized': normalized,
        'is_mobile': True,
        'error': None
    }
