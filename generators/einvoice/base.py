"""
Abstract base classes and data structures for e-invoice generation.

To add a new e-invoice standard:
1. Subclass ``EInvoiceStandard``
2. Implement ``generate_xml()`` and ``xml_filename`` / ``profile_name``
3. Register it in ``generators/einvoice/__init__.py`` STANDARDS dict
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date


@dataclass
class EInvoiceLineItem:
    """A single invoice line item (position)."""
    position_number: int
    name: str
    quantity: float = 1
    unit_code: str = "C62"  # UN/ECE Rec 20: "one" (piece/unit)
    unit_price_net: float = 0.0
    line_total_net: float = 0.0
    tax_rate: float = 0.0  # VAT % (e.g. 19.0)
    tax_category: str = "S"  # S=Standard, E=Exempt, Z=Zero, etc.
    days: int = 1  # rental days (informational)
    is_bundle: bool = False
    bundle_components: list[dict] | None = None


@dataclass
class EInvoiceData:
    """All data needed to generate an e-invoice XML.

    This is a standard-agnostic structure; each standard implementation
    maps it to the appropriate XML schema.
    """
    # Document metadata
    invoice_number: str = ""
    invoice_date: date | None = None
    type_code: str = "380"  # 380 = commercial invoice, 381 = credit note
    currency_code: str = "EUR"

    # Seller (issuer)
    seller_name: str = ""
    seller_address_lines: list[str] = field(default_factory=list)
    seller_postcode: str = ""
    seller_city: str = ""
    seller_country: str = "DE"
    seller_tax_number: str | None = None
    seller_vat_id: str | None = None  # USt-IdNr (e.g. DE123456789)
    seller_email: str | None = None
    seller_phone: str | None = None

    # Buyer (recipient)
    buyer_name: str = ""
    buyer_address_lines: list[str] = field(default_factory=list)
    buyer_postcode: str = ""
    buyer_city: str = ""
    buyer_country: str = "DE"

    # Delivery / service period
    delivery_date: date | None = None
    service_start_date: date | None = None
    service_end_date: date | None = None

    # Tax
    tax_mode: str = "kleinunternehmer"  # 'kleinunternehmer' or 'regular'
    tax_rate: float = 19.0
    tax_amount: float = 0.0  # total MwSt

    # Totals (all in EUR)
    line_total_net: float = 0.0  # sum of line net amounts
    discount_amount_net: float = 0.0
    total_net: float = 0.0  # after discount, before tax
    total_gross: float = 0.0  # final payable amount
    prepaid_amount: float = 0.0

    # Payment
    payment_terms_days: int = 14
    payment_reference: str = ""  # = invoice number typically
    bank_iban: str | None = None
    bank_bic: str | None = None
    bank_name: str | None = None

    # Notes
    notes: str | None = None

    # Line items
    line_items: list[EInvoiceLineItem] = field(default_factory=list)


class EInvoiceStandard(ABC):
    """Abstract base for an e-invoice standard (ZUGFeRD, XRechnung, …)."""

    @property
    @abstractmethod
    def standard_name(self) -> str:
        """Human-readable name, e.g. 'ZUGFeRD 2.x / Factur-X'."""

    @property
    @abstractmethod
    def xml_filename(self) -> str:
        """Filename of the embedded XML (e.g. 'factur-x.xml')."""

    @property
    @abstractmethod
    def profile_name(self) -> str:
        """Profile/level name (e.g. 'BASIC', 'EN 16931')."""

    @abstractmethod
    def generate_xml(self, data: EInvoiceData) -> bytes:
        """Generate the standards-compliant XML from invoice data.

        Returns:
            UTF-8 encoded XML bytes.
        """

    def validate_data(self, data: EInvoiceData) -> list[str]:
        """Optional: validate data before XML generation.

        Returns:
            List of warning/error messages (empty = OK).
        """
        warnings = []
        if not data.invoice_number:
            warnings.append("Invoice number is missing.")
        if not data.seller_name:
            warnings.append("Seller name is missing.")
        if not data.buyer_name:
            warnings.append("Buyer name is missing.")
        return warnings
