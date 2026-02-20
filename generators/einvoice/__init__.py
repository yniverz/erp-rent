"""
Modular e-invoice generation package.

Supports pluggable standards (ZUGFeRD/Factur-X, XRechnung, etc.)
with a common interface for XML generation and PDF embedding.
"""
from generators.einvoice.base import EInvoiceData, EInvoiceLineItem, EInvoiceStandard
from generators.einvoice.zugferd import ZUGFeRDStandard
from generators.einvoice.embed import embed_xml_in_pdf

# Registry of available e-invoice standards
STANDARDS: dict[str, type[EInvoiceStandard]] = {
    "zugferd": ZUGFeRDStandard,
}

# Default standard for Germany
DEFAULT_STANDARD = "zugferd"


def get_standard(name: str | None = None) -> EInvoiceStandard:
    """Get an e-invoice standard instance by name.

    Args:
        name: Standard identifier (e.g. 'zugferd'). Uses DEFAULT_STANDARD if None.

    Returns:
        Instantiated standard object.

    Raises:
        ValueError: If the standard name is not registered.
    """
    name = name or DEFAULT_STANDARD
    cls = STANDARDS.get(name)
    if cls is None:
        raise ValueError(
            f"Unknown e-invoice standard '{name}'. "
            f"Available: {', '.join(STANDARDS.keys())}"
        )
    return cls()
