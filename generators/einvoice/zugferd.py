"""
ZUGFeRD 2.x / Factur-X implementation.

Generates UN/CEFACT Cross Industry Invoice (CII) XML
conforming to the Factur-X / ZUGFeRD BASIC profile.

The BASIC profile is the minimum profile that:
- Is umsatzsteuerlich valid in Germany
- Includes line-item details
- Fulfils EN 16931 core requirements

References:
- ZUGFeRD Spec: https://www.ferd-net.de/standards/zugferd
- Factur-X: https://fnfe-mpe.org/factur-x/
- CII D16B/D22B schema: UN/CEFACT CrossIndustryInvoice
"""
from __future__ import annotations

from datetime import date
from lxml import etree

from generators.einvoice.base import EInvoiceData, EInvoiceStandard

# ── XML Namespaces (CII D16B, compatible with D22B) ─────────────
NS = {
    "rsm": "urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100",
    "ram": "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100",
    "qdt": "urn:un:unece:uncefact:data:standard:QualifiedDataType:100",
    "udt": "urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100",
    "xsi": "http://www.w3.org/2001/XMLSchema-instance",
}

# Guideline ID per profile
PROFILE_IDS = {
    "minimum": "urn:factur-x.eu:1p0:minimum",
    "basicwl": "urn:factur-x.eu:1p0:basicwl",
    "basic": "urn:cen.eu:en16931:2017#compliant#urn:factur-x.eu:1p0:basic",
    "en16931": "urn:cen.eu:en16931:2017",
    "extended": "urn:cen.eu:en16931:2017#conformant#urn:factur-x.eu:1p0:extended",
}


def _el(parent: etree._Element, tag: str, text: str | None = None, **attribs) -> etree._Element:
    """Create a sub-element with optional text and attributes."""
    ns_prefix, local = tag.split(":", 1) if ":" in tag else ("ram", tag)
    ns_uri = NS[ns_prefix]
    elem = etree.SubElement(parent, f"{{{ns_uri}}}{local}")
    if text is not None:
        elem.text = str(text)
    for k, v in attribs.items():
        elem.set(k, str(v))
    return elem


def _fmt_date(d: date) -> str:
    """Format date as YYYYMMDD (format 102)."""
    return d.strftime("%Y%m%d")


def _fmt_amount(value: float) -> str:
    """Format monetary amount: 2 decimals, no thousands separator."""
    return f"{value:.2f}"


def _fmt_quantity(value: float) -> str:
    """Format quantity with up to 4 decimals."""
    return f"{value:.4f}".rstrip("0").rstrip(".")


class ZUGFeRDStandard(EInvoiceStandard):
    """ZUGFeRD 2.x / Factur-X – BASIC profile."""

    def __init__(self, profile: str = "basic"):
        if profile not in PROFILE_IDS:
            raise ValueError(f"Unknown ZUGFeRD profile '{profile}'. Options: {list(PROFILE_IDS)}")
        self._profile = profile

    @property
    def standard_name(self) -> str:
        return "ZUGFeRD 2.x / Factur-X"

    @property
    def xml_filename(self) -> str:
        return "factur-x.xml"

    @property
    def profile_name(self) -> str:
        return self._profile.upper()

    # ── Public API ──────────────────────────────────────────────
    def generate_xml(self, data: EInvoiceData) -> bytes:
        root = self._build_root(data)
        return etree.tostring(
            root, pretty_print=True, xml_declaration=True, encoding="UTF-8"
        )

    def validate_data(self, data: EInvoiceData) -> list[str]:
        warnings = super().validate_data(data)
        if data.tax_mode == "regular" and not data.seller_tax_number and not data.seller_vat_id:
            warnings.append("Tax number or VAT-ID required for regular tax mode.")
        if not data.line_items and self._profile in ("basic", "en16931", "extended"):
            warnings.append(f"Profile {self._profile} requires at least one line item.")
        return warnings

    # ── XML tree builders ───────────────────────────────────────
    def _build_root(self, d: EInvoiceData) -> etree._Element:
        nsmap = {k: v for k, v in NS.items()}
        root = etree.Element(f"{{{NS['rsm']}}}CrossIndustryInvoice", nsmap=nsmap)

        self._add_context(root, d)
        self._add_document(root, d)
        self._add_transaction(root, d)

        return root

    def _add_context(self, root: etree._Element, d: EInvoiceData) -> None:
        ctx = _el(root, "rsm:ExchangedDocumentContext")
        param = _el(ctx, "ram:GuidelineSpecifiedDocumentContextParameter")
        _el(param, "ram:ID", PROFILE_IDS[self._profile])

    def _add_document(self, root: etree._Element, d: EInvoiceData) -> None:
        doc = _el(root, "rsm:ExchangedDocument")
        _el(doc, "ram:ID", d.invoice_number)
        _el(doc, "ram:TypeCode", d.type_code)

        dt = _el(doc, "ram:IssueDateTime")
        _el(dt, "udt:DateTimeString", _fmt_date(d.invoice_date or date.today()), format="102")

        # Include notes
        if d.notes:
            note = _el(doc, "ram:IncludedNote")
            _el(note, "ram:Content", d.notes)

        # Kleinunternehmer note
        if d.tax_mode == "kleinunternehmer":
            note = _el(doc, "ram:IncludedNote")
            _el(note, "ram:Content", "Gemäß § 19 UStG wird keine Umsatzsteuer berechnet.")
            _el(note, "ram:SubjectCode", "REG")  # REG = Regulatory information

    def _add_transaction(self, root: etree._Element, d: EInvoiceData) -> None:
        txn = _el(root, "rsm:SupplyChainTradeTransaction")

        # Line items (required for BASIC profile)
        if self._profile in ("basic", "en16931", "extended"):
            for item in d.line_items:
                self._add_line_item(txn, item, d)

        self._add_agreement(txn, d)
        self._add_delivery(txn, d)
        self._add_settlement(txn, d)

    def _add_line_item(self, txn: etree._Element, item, d: EInvoiceData) -> None:
        li = _el(txn, "ram:IncludedSupplyChainTradeLineItem")

        # Line document
        line_doc = _el(li, "ram:AssociatedDocumentLineDocument")
        _el(line_doc, "ram:LineID", str(item.position_number))

        # Product
        product = _el(li, "ram:SpecifiedTradeProduct")
        _el(product, "ram:Name", item.name)

        # Line agreement (price)
        agreement = _el(li, "ram:SpecifiedLineTradeAgreement")
        net_price = _el(agreement, "ram:NetPriceProductTradePrice")
        _el(net_price, "ram:ChargeAmount", _fmt_amount(item.unit_price_net))

        # Line delivery (quantity)
        delivery = _el(li, "ram:SpecifiedLineTradeDelivery")
        _el(delivery, "ram:BilledQuantity", _fmt_quantity(item.quantity), unitCode=item.unit_code)

        # Line settlement (tax + total)
        settlement = _el(li, "ram:SpecifiedLineTradeSettlement")
        tax = _el(settlement, "ram:ApplicableTradeTax")
        _el(tax, "ram:TypeCode", "VAT")

        if d.tax_mode == "kleinunternehmer":
            _el(tax, "ram:CategoryCode", "E")  # Exempt
            _el(tax, "ram:RateApplicablePercent", "0.00")
        else:
            _el(tax, "ram:CategoryCode", item.tax_category)
            _el(tax, "ram:RateApplicablePercent", _fmt_amount(item.tax_rate))

        monetary = _el(settlement, "ram:SpecifiedTradeSettlementLineMonetarySummation")
        _el(monetary, "ram:LineTotalAmount", _fmt_amount(item.line_total_net))

    def _add_agreement(self, txn: etree._Element, d: EInvoiceData) -> None:
        agreement = _el(txn, "ram:ApplicableHeaderTradeAgreement")

        # Seller
        seller = _el(agreement, "ram:SellerTradeParty")
        _el(seller, "ram:Name", d.seller_name)

        if d.seller_address_lines or d.seller_postcode or d.seller_city:
            addr = _el(seller, "ram:PostalTradeAddress")
            if d.seller_postcode:
                _el(addr, "ram:PostcodeCode", d.seller_postcode)
            # Use first address line as line1, second as line2
            for i, line in enumerate(d.seller_address_lines[:2]):
                _el(addr, "ram:LineOne" if i == 0 else "ram:LineTwo", line)
            if d.seller_city:
                _el(addr, "ram:CityName", d.seller_city)
            _el(addr, "ram:CountryID", d.seller_country)

        if d.seller_email:
            uri = _el(seller, "ram:URIUniversalCommunication")
            _el(uri, "ram:URIID", d.seller_email, schemeID="EM")

        # Tax registration
        if d.seller_tax_number:
            tax_reg = _el(seller, "ram:SpecifiedTaxRegistration")
            _el(tax_reg, "ram:ID", d.seller_tax_number, schemeID="FC")  # FC = Steuernummer
        if d.seller_vat_id:
            tax_reg = _el(seller, "ram:SpecifiedTaxRegistration")
            _el(tax_reg, "ram:ID", d.seller_vat_id, schemeID="VA")  # VA = USt-IdNr

        # Buyer
        buyer = _el(agreement, "ram:BuyerTradeParty")
        _el(buyer, "ram:Name", d.buyer_name)

        if d.buyer_address_lines or d.buyer_postcode or d.buyer_city:
            addr = _el(buyer, "ram:PostalTradeAddress")
            if d.buyer_postcode:
                _el(addr, "ram:PostcodeCode", d.buyer_postcode)
            for i, line in enumerate(d.buyer_address_lines[:2]):
                _el(addr, "ram:LineOne" if i == 0 else "ram:LineTwo", line)
            if d.buyer_city:
                _el(addr, "ram:CityName", d.buyer_city)
            _el(addr, "ram:CountryID", d.buyer_country)

    def _add_delivery(self, txn: etree._Element, d: EInvoiceData) -> None:
        delivery = _el(txn, "ram:ApplicableHeaderTradeDelivery")

        # Actual delivery / service date
        if d.delivery_date or d.service_start_date:
            event = _el(delivery, "ram:ActualDeliverySupplyChainEvent")
            dt = _el(event, "ram:OccurrenceDateTime")
            actual_date = d.delivery_date or d.service_start_date
            _el(dt, "udt:DateTimeString", _fmt_date(actual_date), format="102")

    def _add_settlement(self, txn: etree._Element, d: EInvoiceData) -> None:
        settlement = _el(txn, "ram:ApplicableHeaderTradeSettlement")

        # Payment reference
        if d.payment_reference:
            _el(settlement, "ram:PaymentReference", d.payment_reference)

        _el(settlement, "ram:InvoiceCurrencyCode", d.currency_code)

        # Payment means (bank transfer)
        if d.bank_iban:
            pmeans = _el(settlement, "ram:SpecifiedTradeSettlementPaymentMeans")
            _el(pmeans, "ram:TypeCode", "58")  # 58 = SEPA credit transfer
            account = _el(pmeans, "ram:PayeePartyCreditorFinancialAccount")
            _el(account, "ram:IBANID", d.bank_iban)
            # Note: BIC (PayeeSpecifiedCreditorFinancialInstitution) is only
            # allowed in EN16931 and EXTENDED profiles, not in BASIC.

        # Tax breakdown
        tax = _el(settlement, "ram:ApplicableTradeTax")
        _el(tax, "ram:CalculatedAmount", _fmt_amount(d.tax_amount))
        _el(tax, "ram:TypeCode", "VAT")

        if d.tax_mode == "kleinunternehmer":
            # Kleinunternehmer: exempt from VAT, use reason code
            _el(tax, "ram:ExemptionReason", "Gemäß § 19 UStG wird keine Umsatzsteuer berechnet.")
            _el(tax, "ram:BasisAmount", _fmt_amount(d.total_net))
            _el(tax, "ram:CategoryCode", "E")  # Exempt
            _el(tax, "ram:RateApplicablePercent", "0.00")
        else:
            _el(tax, "ram:BasisAmount", _fmt_amount(d.total_net))
            _el(tax, "ram:CategoryCode", "S")  # Standard
            _el(tax, "ram:RateApplicablePercent", _fmt_amount(d.tax_rate))

        # Billing period (service dates)
        if d.service_start_date and d.service_end_date:
            period = _el(settlement, "ram:BillingSpecifiedPeriod")
            start = _el(period, "ram:StartDateTime")
            _el(start, "udt:DateTimeString", _fmt_date(d.service_start_date), format="102")
            end = _el(period, "ram:EndDateTime")
            _el(end, "udt:DateTimeString", _fmt_date(d.service_end_date), format="102")

        # Payment terms
        terms = _el(settlement, "ram:SpecifiedTradePaymentTerms")
        _el(terms, "ram:Description",
            f"Zahlbar innerhalb von {d.payment_terms_days} Tagen ohne Abzug.")
        if d.invoice_date:
            from datetime import timedelta
            due = d.invoice_date + timedelta(days=d.payment_terms_days)
            due_dt = _el(terms, "ram:DueDateDateTime")
            _el(due_dt, "udt:DateTimeString", _fmt_date(due), format="102")

        # Monetary summation
        summary = _el(settlement, "ram:SpecifiedTradeSettlementHeaderMonetarySummation")
        _el(summary, "ram:LineTotalAmount", _fmt_amount(d.line_total_net))

        # Allowance (discount)
        if d.discount_amount_net > 0:
            _el(summary, "ram:AllowanceTotalAmount", _fmt_amount(d.discount_amount_net))
        else:
            _el(summary, "ram:AllowanceTotalAmount", "0.00")

        # Note: ChargeTotalAmount is only in EN16931/EXTENDED, not in BASIC.
        _el(summary, "ram:TaxBasisTotalAmount", _fmt_amount(d.total_net))

        tax_total = _el(summary, "ram:TaxTotalAmount", _fmt_amount(d.tax_amount), currencyID=d.currency_code)
        _el(summary, "ram:GrandTotalAmount", _fmt_amount(d.total_gross))
        _el(summary, "ram:DuePayableAmount", _fmt_amount(d.total_gross - d.prepaid_amount))

        # Discount allowance at document level
        if d.discount_amount_net > 0:
            allowance = _el(settlement, "ram:SpecifiedTradeAllowanceCharge")
            _el(allowance, "ram:ChargeIndicator")
            ind = allowance.find(f"{{{NS['ram']}}}ChargeIndicator")
            _el(ind, "udt:Indicator", "false")
            _el(allowance, "ram:ActualAmount", _fmt_amount(d.discount_amount_net))
            _el(allowance, "ram:Reason", "Rabatt")
            allowance_tax = _el(allowance, "ram:CategoryTradeTax")
            _el(allowance_tax, "ram:TypeCode", "VAT")
            if d.tax_mode == "kleinunternehmer":
                _el(allowance_tax, "ram:CategoryCode", "E")
                _el(allowance_tax, "ram:RateApplicablePercent", "0.00")
            else:
                _el(allowance_tax, "ram:CategoryCode", "S")
                _el(allowance_tax, "ram:RateApplicablePercent", _fmt_amount(d.tax_rate))
