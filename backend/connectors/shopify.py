"""Shopify Admin REST API connector for B2B customer data retrieval."""

import logging
from typing import Optional, List, Dict, Any
from backend.connectors.base import BaseConnector
from backend.config import config
from backend.shopify_token import get_shopify_token

logger = logging.getLogger(__name__)


class ShopifyConnector(BaseConnector):
    """Connector for Shopify Admin REST API focusing on B2B customer data."""

    def __init__(self):
        """Initialize Shopify connector with store URL and access token."""
        has_credentials = (
            config.SHOPIFY_CLIENT_ID and config.SHOPIFY_CLIENT_SECRET
        )
        if not config.SHOPIFY_STORE_URL or (
            not config.SHOPIFY_ACCESS_TOKEN and not has_credentials
        ):
            raise ValueError(
                "SHOPIFY_STORE_URL and either SHOPIFY_ACCESS_TOKEN "
                "or SHOPIFY_CLIENT_ID+SECRET must be configured"
            )

        base_url = config.shopify_api_base()
        super().__init__(base_url=base_url, timeout=30, max_retries=3)

        self.store_url = config.SHOPIFY_STORE_URL
        self.api_version = config.SHOPIFY_API_VERSION

    def _get_headers(self) -> Dict[str, str]:
        """Return headers for Shopify API requests (token auto-refreshes)."""
        return {
            "X-Shopify-Access-Token": get_shopify_token(),
            "Content-Type": "application/json",
        }

    def fetch_b2b_customers(self) -> List[Dict[str, Any]]:
        """
        Fetch all B2B customers from Shopify using cursor pagination.

        Uses the "B2B" tag to identify B2B customers and implements cursor-based
        pagination via Link headers (rel="next").

        Returns:
            List of customer dicts with keys:
                - shopify_id: Customer ID
                - ragione_sociale: Company name
                - partita_iva: P.IVA (VAT number)
                - codice_fiscale: Tax code (CF)
                - codice_sdi: SDI code
                - phone: Phone number
                - email: Email address
                - tags: Comma-separated tags
        """
        customers = []
        cursor = None
        page_count = 0

        while True:
            page_count += 1
            logger.info(f"Fetching B2B customers page {page_count}")

            # Build query for B2B tagged customers
            params = {
                "query": 'tag:"B2B"',
                "limit": 250,  # Max allowed by Shopify
                "fields": "id,email,phone,tags,addresses,createdAt",
            }

            if cursor:
                params["cursor"] = cursor

            try:
                response = self.get("customers/search.json", headers=self._get_headers(), params=params)
            except Exception as e:
                logger.error(f"Error fetching B2B customers page {page_count}: {e}")
                raise

            if "customers" not in response:
                logger.warning(f"No 'customers' key in response: {response}")
                break

            page_customers = response.get("customers", [])
            logger.info(f"Retrieved {len(page_customers)} customers on page {page_count}")

            # Parse each customer
            for customer in page_customers:
                parsed = self._parse_customer(customer)
                if parsed:
                    customers.append(parsed)

            # Check for next page using Link header
            cursor = self._extract_next_cursor(response)
            if not cursor:
                logger.info(f"Completed pagination - fetched {len(customers)} B2B customers total")
                break

        return customers

    def get_customer_by_id(self, shopify_id: str) -> Optional[Dict[str, Any]]:
        """
        Fetch a single customer by Shopify ID.

        Args:
            shopify_id: The Shopify customer ID (numeric string or with "gid://...")

        Returns:
            Parsed customer dict or None if not found
        """
        # Clean up ID if it's in gid format
        customer_id = self._extract_id_from_gid(shopify_id)

        try:
            response = self.get(
                f"customers/{customer_id}.json",
                headers=self._get_headers(),
            )
        except Exception as e:
            logger.error(f"Error fetching customer {shopify_id}: {e}")
            return None

        if "customer" not in response:
            logger.warning(f"No customer found with ID {shopify_id}")
            return None

        return self._parse_customer(response["customer"])

    def search_customers(self, query: str) -> List[Dict[str, Any]]:
        """
        Search customers by query.

        Supports Shopify search syntax like:
        - email: 'email@example.com'
        - phone: '+39123456789'
        - company: 'ACME Corp'

        Args:
            query: Shopify search query string

        Returns:
            List of matching customers
        """
        customers = []
        cursor = None
        page_count = 0

        while True:
            page_count += 1
            logger.info(f"Searching customers with query: {query} (page {page_count})")

            params = {
                "query": query,
                "limit": 250,
                "fields": "id,email,phone,tags,addresses,createdAt",
            }

            if cursor:
                params["cursor"] = cursor

            try:
                response = self.get(
                    "customers/search.json",
                    headers=self._get_headers(),
                    params=params,
                )
            except Exception as e:
                logger.error(f"Error searching customers: {e}")
                raise

            page_customers = response.get("customers", [])
            logger.info(f"Retrieved {len(page_customers)} results on page {page_count}")

            for customer in page_customers:
                parsed = self._parse_customer(customer)
                if parsed:
                    customers.append(parsed)

            cursor = self._extract_next_cursor(response)
            if not cursor:
                logger.info(f"Completed search - found {len(customers)} customers total")
                break

        return customers

    @staticmethod
    def parse_piva_from_address2(address2_value: Optional[str]) -> Dict[str, Optional[str]]:
        """
        Extract P.IVA, Codice Fiscale, and SDI from address2 field.

        Expected format: "PIVA-CF-SDI"
        Example: "04627230271-04627230271-M5UXCR1"

        Args:
            address2_value: The address2 field value

        Returns:
            Dict with keys: piva, codice_fiscale, codice_sdi
            Returns empty strings if parsing fails
        """
        result = {"piva": None, "codice_fiscale": None, "codice_sdi": None}

        if not address2_value or not isinstance(address2_value, str):
            return result

        address2_value = address2_value.strip()
        if not address2_value:
            return result

        # Split by hyphen
        parts = address2_value.split("-")

        if len(parts) >= 1 and parts[0]:
            result["piva"] = parts[0].strip()

        if len(parts) >= 2 and parts[1]:
            result["codice_fiscale"] = parts[1].strip()

        if len(parts) >= 3 and parts[2]:
            result["codice_sdi"] = parts[2].strip()

        return result

    def _parse_customer(self, customer: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Parse raw Shopify customer data into internal format.

        Args:
            customer: Raw Shopify customer object

        Returns:
            Parsed customer dict or None if required fields are missing
        """
        shopify_id = customer.get("id")
        email = customer.get("email")
        phone = customer.get("phone")
        tags = customer.get("tags", "")

        # Ensure we have at least ID and email
        if not shopify_id:
            logger.warning("Customer missing Shopify ID")
            return None

        # Search ALL addresses for B2B data (company name + P.IVA)
        # B2B data is typically in the billing address, which may NOT be the default
        addresses = customer.get("addresses", [])

        # Find address with company name and P.IVA data
        billing_address = None
        for addr in addresses:
            company = (addr.get("company") or "").strip()
            address2 = (addr.get("address2") or "").strip()
            if company and address2:
                billing_address = addr
                break
        # Fallback: address with just company
        if not billing_address:
            for addr in addresses:
                company = (addr.get("company") or "").strip()
                if company:
                    billing_address = addr
                    break
        # Fallback: default address
        default_address = next((addr for addr in addresses if addr.get("default")), None)
        if not billing_address:
            billing_address = default_address

        # Extract company name (ragione sociale)
        ragione_sociale = ""
        if billing_address:
            ragione_sociale = (billing_address.get("company") or "").strip()
        # Fallback: use first_name + last_name
        if not ragione_sociale:
            first = (customer.get("first_name") or "").strip()
            last = (customer.get("last_name") or "").strip()
            ragione_sociale = f"{first} {last}".strip()
            if ragione_sociale:
                logger.debug(f"Customer {shopify_id}: using name as ragione_sociale: {ragione_sociale}")

        # Extract P.IVA data from address2 of billing address
        piva_data = {}
        if billing_address:
            address2 = billing_address.get("address2")
            piva_data = self.parse_piva_from_address2(address2)
        else:
            logger.debug(f"Customer {shopify_id} has no addresses")

        # Collect ALL phone numbers with source labels
        phones = []
        seen_numbers = set()

        def _add_phone(num, source, label):
            if not num:
                return
            clean = num.strip()
            if clean and clean not in seen_numbers:
                seen_numbers.add(clean)
                phones.append({
                    "number": clean,
                    "source": source,
                    "label": label,
                })

        # Main Shopify customer phone
        _add_phone(phone, "shopify_customer", "Shopify")

        # Phones from addresses
        for addr in addresses:
            addr_phone = (addr.get("phone") or "").strip()
            if not addr_phone:
                continue
            is_default = addr.get("default", False)
            company = (addr.get("company") or "").strip()
            # Determine label
            if addr == billing_address:
                lbl = "Fatturazione"
                src = "shopify_billing"
            elif is_default:
                lbl = "Consegna"
                src = "shopify_shipping"
            else:
                city = (addr.get("city") or "").strip()
                lbl = f"Indirizzo {city}" if city else "Altro"
                src = "shopify_address"
            _add_phone(addr_phone, src, lbl)

        # Primary phone = first available
        customer_phone = phones[0]["number"] if phones else None

        parsed = {
            "shopify_id": str(shopify_id),
            "ragione_sociale": ragione_sociale,
            "partita_iva": piva_data.get("piva"),
            "codice_fiscale": piva_data.get("codice_fiscale"),
            "codice_sdi": piva_data.get("codice_sdi"),
            "phone": customer_phone,
            "phones": phones,
            "email": email,
            "tags": tags,
        }

        logger.debug(f"Parsed customer {shopify_id}: {ragione_sociale}")
        return parsed

    def update_customer_phone(
        self, shopify_id: str, phone: str
    ) -> bool:
        """Update customer phone number on Shopify.

        Args:
            shopify_id: Shopify customer ID (numeric or gid)
            phone: New phone number

        Returns:
            True if updated successfully
        """
        try:
            numeric_id = self._extract_id_from_gid(shopify_id)
            url = f"customers/{numeric_id}.json"
            response = self.put(
                url,
                json_data={
                    "customer": {"id": int(numeric_id), "phone": phone}
                },
                headers=self._get_headers(),
            )
            if response and isinstance(response, dict):
                logger.info(
                    f"Shopify customer {numeric_id} phone "
                    f"updated to {phone}"
                )
                return True
            return False
        except Exception as e:
            logger.error(
                f"Error updating Shopify customer phone: {e}"
            )
            return False

    @staticmethod
    def _extract_id_from_gid(shopify_id: str) -> str:
        """
        Extract numeric ID from GraphQL GID format.

        Converts "gid://shopify/Customer/123456789" -> "123456789"
        Or returns the input unchanged if already numeric.

        Args:
            shopify_id: Shopify ID (gid or numeric)

        Returns:
            Numeric ID string
        """
        if shopify_id.startswith("gid://"):
            # Extract ID from GraphQL format
            parts = shopify_id.split("/")
            return parts[-1] if parts else shopify_id

        return shopify_id

    @staticmethod
    def _extract_next_cursor(response: Dict[str, Any]) -> Optional[str]:
        """
        Extract cursor for next page from response.

        Shopify API returns pagination info in the Link header,
        but the response dict may contain pagination metadata.

        Args:
            response: API response dict

        Returns:
            Next cursor string or None if no next page
        """
        # Check for pageInfo in response (if returned by Shopify)
        if "pageInfo" in response:
            page_info = response["pageInfo"]
            if page_info.get("hasNextPage"):
                return page_info.get("endCursor")

        return None

    def close(self):
        """Close the HTTP client."""
        super().close()
        logger.info("Shopify connector closed")
