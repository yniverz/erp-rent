"""
PDF/A-3 embedding for e-invoice XML.

Uses the ``factur-x`` Python library to:
- Convert a regular PDF to PDF/A-3
- Embed the e-invoice XML as an attachment
- Set correct XMP metadata (Factur-X / ZUGFeRD conformance)

The ``factur-x`` library handles all the low-level PDF manipulation,
XMP metadata, and PDF/A-3 compliance automatically.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def embed_xml_in_pdf(
    pdf_bytes: bytes,
    xml_bytes: bytes,
    *,
    flavor: str = "factur-x",
    level: str = "basic",
    lang: str = "de",
    pdf_metadata: dict | None = None,
) -> bytes:
    """Embed e-invoice XML into a PDF, producing a PDF/A-3 compliant file.

    Args:
        pdf_bytes: The original PDF as bytes.
        xml_bytes: The e-invoice XML as bytes (UTF-8).
        flavor: 'factur-x' (default) or 'order-x'.
        level: Profile level ('minimum', 'basicwl', 'basic', 'en16931', 'extended').
        lang: PDF language tag (RFC 3066), e.g. 'de' for German.
        pdf_metadata: Optional dict with keys 'author', 'title', 'subject', 'keywords'.

    Returns:
        The Factur-X/ZUGFeRD PDF as bytes (PDF/A-3 with embedded XML).

    Raises:
        ImportError: If the ``factur-x`` library is not installed.
        Exception: If XML validation or PDF generation fails.
    """
    try:
        from facturx import generate_from_binary
    except ImportError:
        raise ImportError(
            "The 'factur-x' library is required for e-invoice PDF generation. "
            "Install it with: pip install factur-x"
        )

    logger.info("Embedding %s XML (level=%s) into PDF/A-3", flavor, level)

    result_pdf = generate_from_binary(
        pdf_bytes,
        xml_bytes,
        flavor=flavor,
        level=level,
        check_xsd=True,
        pdf_metadata=pdf_metadata,
        lang=lang,
    )

    if not result_pdf:
        raise RuntimeError("factur-x library returned empty PDF")

    logger.info("Successfully generated %s PDF/A-3 (%d bytes)", flavor, len(result_pdf))
    return result_pdf
