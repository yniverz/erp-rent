from io import BytesIO
from flask import Blueprint, render_template, request, redirect, url_for, flash, send_file, jsonify, abort
from flask_login import login_required, current_user
from models import db, User, Item, Category, Quote, QuoteItem, Inquiry, InquiryItem, SiteSettings, Customer, PackageComponent, ItemOwnership, QuoteItemExpense, QuoteItemExpenseDocument
from helpers import get_available_quantity, get_package_available_quantity, get_upload_path, allowed_image_file, allowed_document_file
import accounting
from datetime import datetime
from functools import wraps
from werkzeug.utils import secure_filename
import os
import uuid

admin_bp = Blueprint('admin', __name__)


def _generate_rechnung_pdf_bytes(quote, *, einvoice=True):
    """Generate the Rechnung PDF bytes for a quote.

    When *einvoice* is True (default), the PDF is returned as a ZUGFeRD/Factur-X
    PDF/A-3 with the e-invoice XML embedded.  Falls back to a plain PDF if the
    factur-x library is not installed.
    """
    from generators.rechnung import build_rechnung_pdf
    site_settings = SiteSettings.query.first()
    data = _extract_common_pdf_data(quote, site_settings)
    positions = _extract_positions(quote)
    rechnungs_datum = quote.finalized_at.strftime('%d.%m.%Y') if quote.finalized_at else datetime.now().strftime('%d.%m.%Y')
    pdf_bytes = build_rechnung_pdf(
        issuer_name=data['issuer_name'],
        issuer_address=data['issuer_address'],
        contact_lines=data['contact_lines'],
        bank_lines=data['bank_lines'],
        tax_number=data['tax_number'],
        vat_id=data.get('vat_id'),
        tax_mode=data['tax_mode'],
        tax_rate=data['tax_rate'],
        logo_path=data['logo_path'],
        recipient_lines=data['recipient_lines'],
        reference_number=quote.reference_number or f'RE-{quote.id:04d}',
        rechnungs_datum=rechnungs_datum,
        start_date_str=data['start_date_str'],
        end_date_str=data['end_date_str'],
        rental_days=data['rental_days'],
        is_pauschale=data['is_pauschale'],
        leistungszeitraum=data.get('leistungszeitraum'),
        positions=positions,
        discount_percent=quote.discount_percent or 0,
        discount_label=quote.discount_label,
        discount_amount=quote.discount_amount,
        subtotal=quote.subtotal,
        total=quote.total,
        payment_terms_days=data['payment_terms_days'],
        notes=quote.public_notes,
    )

    if einvoice:
        pdf_bytes = _apply_einvoice(pdf_bytes, quote, data, positions, site_settings)

    return pdf_bytes


def _apply_einvoice(pdf_bytes, quote, data, positions, site_settings):
    """Embed ZUGFeRD / Factur-X e-invoice XML into the PDF.

    Returns the enhanced PDF bytes, or the original pdf_bytes on error.
    """
    try:
        from generators.einvoice import get_standard, EInvoiceData, EInvoiceLineItem
        from generators.einvoice.embed import embed_xml_in_pdf
    except ImportError:
        import logging
        logging.getLogger(__name__).warning(
            'E-invoice libraries not available – returning plain PDF.'
        )
        return pdf_bytes

    try:
        einvoice_data = _build_einvoice_data(quote, data, positions, site_settings)
        standard = get_standard()  # default = ZUGFeRD
        xml_bytes = standard.generate_xml(einvoice_data)

        pdf_metadata = {
            'author': data['issuer_name'],
            'title': f"{data['issuer_name']}: Rechnung {einvoice_data.invoice_number}",
            'subject': f"Rechnung {einvoice_data.invoice_number}",
            'keywords': 'Factur-X, Rechnung, ZUGFeRD',
        }

        pdf_bytes = embed_xml_in_pdf(
            pdf_bytes, xml_bytes,
            flavor='factur-x',
            level='basic',
            lang='de',
            pdf_metadata=pdf_metadata,
        )
    except Exception as exc:
        import logging
        logging.getLogger(__name__).error(
            'E-invoice embedding failed, returning plain PDF: %s', exc
        )

    return pdf_bytes


def _build_einvoice_data(quote, data, positions, site_settings):
    """Convert internal quote/settings data to the standard-agnostic EInvoiceData."""
    from generators.einvoice.base import EInvoiceData, EInvoiceLineItem
    import re, math
    from datetime import date as _date

    # --- Parse dates ---
    def _parse_de_date(s):
        """Parse DD.MM.YYYY string to date."""
        if not s:
            return None
        try:
            parts = s.strip().split('.')
            return _date(int(parts[2]), int(parts[1]), int(parts[0]))
        except (ValueError, IndexError):
            return None

    invoice_date = None
    if quote.finalized_at:
        invoice_date = quote.finalized_at.date() if hasattr(quote.finalized_at, 'date') else quote.finalized_at
    if not invoice_date:
        invoice_date = _date.today()

    start_date = _parse_de_date(data.get('start_date_str'))
    end_date = _parse_de_date(data.get('end_date_str'))

    # --- Parse address info ---
    seller_postcode = ''
    seller_city = ''
    seller_address = []
    for line in data.get('issuer_address', []):
        # Try to detect postcode + city pattern (e.g. "12345 Berlin")
        m = re.match(r'^(\d{4,5})\s+(.+)$', line.strip())
        if m and not seller_postcode:
            seller_postcode = m.group(1)
            seller_city = m.group(2)
        else:
            seller_address.append(line.strip())

    buyer_postcode = ''
    buyer_city = ''
    buyer_address = []
    for line in data.get('recipient_lines', []):
        m = re.match(r'^(\d{4,5})\s+(.+)$', line.strip())
        if m and not buyer_postcode:
            buyer_postcode = m.group(1)
            buyer_city = m.group(2)
        else:
            buyer_address.append(line.strip())

    buyer_name = buyer_address[0] if buyer_address else quote.customer_name
    buyer_address_rest = buyer_address[1:] if len(buyer_address) > 1 else []

    # --- Parse IBAN / BIC from bank_lines ---
    bank_iban = None
    bank_bic = None
    bank_name = None
    for line in data.get('bank_lines', []):
        line_upper = line.upper().replace(' ', '')
        # Look for IBAN
        iban_match = re.search(r'(?:IBAN[:\s]*)?((?:DE|AT|CH|LI|LU|FR|NL|BE|IT|ES)\d{2}\d{4,30})', line_upper)
        if iban_match and not bank_iban:
            bank_iban = iban_match.group(1)
        # Look for BIC
        bic_match = re.search(r'(?:BIC|SWIFT)[:\s]*([A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?)', line_upper)
        if bic_match and not bank_bic:
            bank_bic = bic_match.group(1)
        # Bank name: line that is not IBAN/BIC
        if not iban_match and not bic_match and line.strip() and not bank_name:
            bank_name = line.strip()

    # --- Parse contact info ---
    seller_email = None
    seller_phone = None
    for line in data.get('contact_lines', []):
        if '@' in line and not seller_email:
            seller_email = line.strip()
        elif re.search(r'\+?[\d\s/-]{6,}', line) and not seller_phone:
            seller_phone = line.strip()

    # --- Tax calculations ---
    tax_mode = data.get('tax_mode', 'kleinunternehmer')
    tax_rate = data.get('tax_rate', 19.0)
    is_regular = (tax_mode == 'regular')
    tax_factor = 1 + tax_rate / 100

    rental_days = data.get('rental_days', 1)
    discount_percent = quote.discount_percent or 0

    if is_regular:
        # All stored prices are brutto; derive netto
        brutto_subtotal = quote.subtotal
        brutto_discount = quote.discount_amount
        brutto_total = brutto_subtotal - brutto_discount
        netto_total = round(brutto_total / tax_factor, 2)
        netto_subtotal = round(brutto_subtotal / tax_factor, 2)
        netto_discount = round(netto_subtotal - netto_total, 2) if discount_percent > 0 else 0.0
        mwst = round(brutto_total - netto_total, 2)

        # Distribute netto_subtotal across positions (largest-remainder)
        position_bruttos = [p['total'] for p in positions]
        brutto_sum = sum(position_bruttos) or 1
        raw_nettos = [netto_subtotal * (pb / brutto_sum) for pb in position_bruttos]
        floored = [math.floor(r * 100) / 100 for r in raw_nettos]
        deficit_cents = round((netto_subtotal - sum(floored)) * 100)
        idx_by_remainder = sorted(
            range(len(raw_nettos)),
            key=lambda i: -(raw_nettos[i] * 100 - math.floor(raw_nettos[i] * 100)),
        )
        position_nettos = list(floored)
        for k in range(max(0, deficit_cents)):
            position_nettos[idx_by_remainder[k]] += 0.01
        position_nettos = [round(n, 2) for n in position_nettos]
    else:
        # Kleinunternehmer: brutto = netto (no VAT)
        netto_subtotal = quote.subtotal
        netto_discount = quote.discount_amount if discount_percent > 0 else 0.0
        netto_total = netto_subtotal - netto_discount
        mwst = 0.0
        position_nettos = [p['total'] for p in positions]

    # --- Build line items ---
    line_items = []
    for idx, pos in enumerate(positions):
        line_net = position_nettos[idx] if idx < len(position_nettos) else pos['total']
        ppd_net = round(line_net / max(pos.get('quantity', 1), 1) / max(rental_days, 1), 2) if not pos.get('is_bundle') else 0

        li = EInvoiceLineItem(
            position_number=idx + 1,
            name=pos['name'],
            quantity=pos.get('quantity', 1),
            unit_price_net=ppd_net if not pos.get('is_bundle') else line_net,
            line_total_net=line_net,
            tax_rate=tax_rate if is_regular else 0.0,
            tax_category='S' if is_regular else 'E',
            days=rental_days,
            is_bundle=pos.get('is_bundle', False),
            bundle_components=pos.get('bundle_components'),
        )
        line_items.append(li)

    return EInvoiceData(
        invoice_number=quote.reference_number or f'RE-{quote.id:04d}',
        invoice_date=invoice_date,
        type_code='380',
        currency_code='EUR',
        seller_name=data['issuer_name'],
        seller_address_lines=seller_address,
        seller_postcode=seller_postcode,
        seller_city=seller_city,
        seller_country='DE',
        seller_tax_number=data.get('tax_number'),
        seller_vat_id=data.get('vat_id'),
        seller_email=seller_email,
        seller_phone=seller_phone,
        buyer_name=buyer_name,
        buyer_address_lines=buyer_address_rest,
        buyer_postcode=buyer_postcode,
        buyer_city=buyer_city,
        buyer_country='DE',
        delivery_date=start_date,
        service_start_date=start_date,
        service_end_date=end_date,
        tax_mode=tax_mode,
        tax_rate=tax_rate,
        tax_amount=mwst,
        line_total_net=netto_subtotal,
        discount_amount_net=netto_discount,
        total_net=netto_total,
        total_gross=round(netto_total + mwst, 2),
        payment_terms_days=data.get('payment_terms_days', 14),
        payment_reference=quote.reference_number or f'RE-{quote.id:04d}',
        bank_iban=bank_iban,
        bank_bic=bank_bic,
        bank_name=bank_name,
        notes=quote.public_notes,
        line_items=line_items,
    )


def _book_quote_income(quote, site_settings=None, account_id=None):
    """Book a quote payment as income in the external accounting service.
    Returns (ok, message).  Silently succeeds when accounting is not configured.
    """
    if not accounting.is_configured():
        return True, None
    if not site_settings:
        site_settings = SiteSettings.query.first()
    # Determine tax treatment
    tax_treatment = quote.accounting_tax_treatment or accounting.get_default_tax_treatment(site_settings)
    category_id = site_settings.accounting_income_category_id if site_settings else None
    # Determine account: explicit override > site default
    if not account_id and site_settings:
        account_id = site_settings.accounting_income_account_id
    if not account_id:
        return False, 'Kein Buchhaltungs-Konto ausgewählt. Bitte in den Einstellungen ein Einnahmen-Konto hinterlegen oder im Bezahl-Dialog auswählen.'
    paid_date = quote.paid_at.strftime('%Y-%m-%d') if quote.paid_at else datetime.utcnow().strftime('%Y-%m-%d')
    description = f'{quote.customer_name} – {quote.reference_number or ("RE" + str(quote.id))}'
    ok, result = accounting.create_transaction(
        date=paid_date,
        txn_type='income',
        description=description,
        amount=quote.total,
        account_id=account_id,
        category_id=category_id,
        tax_treatment=tax_treatment,
        notes=f'Angebot {quote.reference_number}',
    )
    if ok:
        quote.accounting_transaction_id = result  # store the returned ID
    return ok, result


def _delete_quote_accounting(quote):
    """Delete the accounting transaction linked to a quote. Returns (ok, msg)."""
    if not accounting.is_configured() or not quote.accounting_transaction_id:
        return True, None
    ok, result = accounting.delete_transaction(quote.accounting_transaction_id)
    if ok:
        quote.accounting_transaction_id = None
    return ok, result


def _book_expense_transaction(expense, quote_item, site_settings=None, account_id=None):
    """Book an external cost expense in the accounting service."""
    if not accounting.is_configured():
        return True, None
    if not site_settings:
        site_settings = SiteSettings.query.first()
    tax_treatment = accounting.get_default_tax_treatment(site_settings)
    category_id = site_settings.accounting_expense_category_id if site_settings else None
    # Determine account: explicit override > site default
    if not account_id and site_settings:
        account_id = site_settings.accounting_expense_account_id
    if not account_id:
        return False, 'Kein Buchhaltungs-Konto ausgewählt. Bitte in den Einstellungen ein Ausgaben-Konto hinterlegen oder im Bezahl-Dialog auswählen.'
    paid_date = expense.paid_at.strftime('%Y-%m-%d') if expense.paid_at else datetime.utcnow().strftime('%Y-%m-%d')
    quote = quote_item.quote
    item_name = quote_item.display_name
    description = f'Extern: {item_name} – {quote.customer_name} ({quote.reference_number or quote.id})'
    ok, result = accounting.create_transaction(
        date=paid_date,
        txn_type='expense',
        description=description,
        amount=expense.amount,
        account_id=account_id,
        category_id=category_id,
        tax_treatment=tax_treatment,
        notes=expense.notes or None,
    )
    if ok:
        expense.accounting_transaction_id = result
    return ok, result


def _delete_expense_accounting(expense):
    """Delete the accounting transaction linked to an expense."""
    if not accounting.is_configured() or not expense.accounting_transaction_id:
        return True, None
    ok, result = accounting.delete_transaction(expense.accounting_transaction_id)
    if ok:
        expense.accounting_transaction_id = None
    return ok, result


# ---------------------------------------------------------------------------
# API Quote / Invoice helpers
# ---------------------------------------------------------------------------

def _build_api_quote_items(quote):
    """Build the items list for an API quote/invoice from local quote items."""
    positions = _extract_positions(quote)
    items = []
    for pos in positions:
        items.append({
            'description': pos['name'],
            'quantity': pos['quantity'],
            'unit': 'Stk.',
            'unit_price': round(pos['total'] / pos['quantity'], 2) if pos['quantity'] else pos['total'],
        })
    return items


def _build_api_notes(quote):
    """Build notes string for the API quote/invoice, including rental period."""
    parts = []
    if quote.start_date and quote.end_date:
        days = quote.calculate_rental_days()
        parts.append(
            f"Mietzeitraum: {quote.start_date.strftime('%d.%m.%Y')} – "
            f"{quote.end_date.strftime('%d.%m.%Y')} ({days} Tag{'e' if days != 1 else ''})"
        )
    if quote.public_notes:
        parts.append(quote.public_notes)
    return '\n'.join(parts) if parts else None


def _sync_create_api_quote(quote, site_settings=None):
    """Create an API quote for a local quote. Returns (ok, error_or_None)."""
    if not accounting.is_configured():
        return True, None
    if quote.api_quote_id:
        return True, None  # already exists
    if not site_settings:
        site_settings = SiteSettings.query.first()

    items = _build_api_quote_items(quote)
    if not items:
        return False, 'Keine Positionen im Angebot.'

    date_str = (quote.created_at or datetime.utcnow()).strftime('%Y-%m-%d')
    valid_until = None
    if site_settings and site_settings.quote_validity_days:
        from datetime import timedelta
        valid_date = (quote.created_at or datetime.utcnow()) + timedelta(days=site_settings.quote_validity_days)
        valid_until = valid_date.strftime('%Y-%m-%d')

    tax_treatment = quote.accounting_tax_treatment or accounting.get_default_tax_treatment(site_settings)
    agb_text = site_settings.terms_and_conditions_text if site_settings else None
    payment_terms = (site_settings.payment_terms_days or 14) if site_settings else 14
    notes = _build_api_notes(quote)

    ok, result = accounting.create_quote(
        date=date_str,
        items=items,
        customer_id=quote.api_customer_id,
        valid_until=valid_until,
        tax_treatment=tax_treatment,
        discount_percent=quote.discount_percent or 0,
        notes=notes,
        agb_text=agb_text,
        payment_terms_days=payment_terms,
    )
    if ok:
        quote.api_quote_id = result.get('id')
        quote.api_quote_number = result.get('quote_number')
    return ok, result if not ok else None


def _sync_update_api_quote(quote, site_settings=None):
    """Update the API quote with current local data."""
    if not accounting.is_configured() or not quote.api_quote_id:
        return True, None
    if not site_settings:
        site_settings = SiteSettings.query.first()

    items = _build_api_quote_items(quote)
    tax_treatment = quote.accounting_tax_treatment or accounting.get_default_tax_treatment(site_settings)
    agb_text = site_settings.terms_and_conditions_text if site_settings else None
    notes = _build_api_notes(quote)

    ok, result = accounting.update_quote(
        quote.api_quote_id,
        items=items,
        customer_id=quote.api_customer_id,
        tax_treatment=tax_treatment,
        discount_percent=quote.discount_percent or 0,
        notes=notes,
        agb_text=agb_text,
    )
    return ok, result if not ok else None


def _sync_delete_api_quote(quote):
    """Delete the API quote for a local quote."""
    if not accounting.is_configured() or not quote.api_quote_id:
        return True, None
    ok, result = accounting.delete_quote(quote.api_quote_id)
    if ok:
        quote.api_quote_id = None
        quote.api_quote_number = None
    return ok, result


def _sync_api_quote_status(quote, status):
    """Sync a status change to the API quote."""
    if not accounting.is_configured() or not quote.api_quote_id:
        return True, None
    ok, result = accounting.set_quote_status(quote.api_quote_id, status)
    return ok, result if not ok else None


def _create_api_invoice_from_quote(quote, date_str=None):
    """Create an API invoice from the API quote. Returns (ok, error_or_None)."""
    if not accounting.is_configured():
        return True, None
    if not quote.api_quote_id:
        return False, 'Kein API-Angebot vorhanden. Bitte zuerst ein API-Angebot erstellen.'
    if quote.api_invoice_id:
        return True, None  # already exists

    ok, result = accounting.create_invoice_from_quote(
        quote.api_quote_id, date=date_str)
    if ok:
        quote.api_invoice_id = result.get('id')
        quote.api_invoice_number = result.get('invoice_number')
    return ok, result if not ok else None


def admin_required(f):
    """Decorator to require admin privileges"""
    @wraps(f)
    @login_required
    def decorated_function(*args, **kwargs):
        if not current_user.is_admin:
            flash('Admin-Zugang erforderlich.', 'error')
            return redirect(url_for('admin.dashboard'))
        return f(*args, **kwargs)
    return decorated_function


def can_edit_or_admin(f):
    """Decorator: user must be admin or have can_edit_all, or own the resource"""
    @wraps(f)
    @login_required
    def decorated_function(*args, **kwargs):
        return f(*args, **kwargs)
    return decorated_function


# ============= DASHBOARD =============

@admin_bp.route('/')
@login_required
def dashboard():
    """Admin dashboard"""
    total_items = Item.query.count()
    total_quotes = Quote.query.count()
    new_inquiries = Inquiry.query.filter_by(status='new').count()
    active_quotes = Quote.query.filter(Quote.status.in_(['draft', 'finalized', 'performed'])).count()
    return render_template('admin/dashboard.html',
                           total_items=total_items,
                           total_quotes=total_quotes,
                           new_inquiries=new_inquiries,
                           active_quotes=active_quotes)


# ============= CATEGORIES =============

@admin_bp.route('/categories', methods=['GET', 'POST'])
@login_required
def categories():
    """Manage categories"""
    if request.method == 'POST':
        action = request.form.get('action')
        try:
            if action == 'add':
                name = request.form.get('name', '').strip()
                order = request.form.get('display_order', 0, type=int)
                parent_id = request.form.get('parent_id', type=int) or None
                if name:
                    # Handle image upload
                    image_filename = None
                    if 'image' in request.files:
                        file = request.files['image']
                        if file and file.filename and allowed_image_file(file.filename):
                            ext = file.filename.rsplit('.', 1)[1].lower()
                            image_filename = f"{uuid.uuid4().hex}.{ext}"
                            file.save(os.path.join(get_upload_path(), image_filename))
                    cat = Category(name=name, display_order=order, parent_id=parent_id, image_filename=image_filename)
                    db.session.add(cat)
                    db.session.commit()
                    flash(f'Kategorie "{name}" erstellt.', 'success')
            elif action == 'edit':
                cat_id = request.form.get('category_id', type=int)
                cat = Category.query.get_or_404(cat_id)
                cat.name = request.form.get('name', '').strip()
                cat.display_order = request.form.get('display_order', 0, type=int)
                new_parent_id = request.form.get('parent_id', type=int) or None
                # Prevent circular references
                if new_parent_id:
                    descendant_ids = cat.all_descendant_ids()
                    if new_parent_id in descendant_ids:
                        flash('Kann keine Unterkategorie von sich selbst sein.', 'error')
                        return redirect(url_for('admin.categories'))
                cat.parent_id = new_parent_id
                # Handle image
                if 'image' in request.files:
                    file = request.files['image']
                    if file and file.filename and allowed_image_file(file.filename):
                        if cat.image_filename:
                            old_path = os.path.join(get_upload_path(), cat.image_filename)
                            if os.path.exists(old_path):
                                os.remove(old_path)
                        ext = file.filename.rsplit('.', 1)[1].lower()
                        cat.image_filename = f"{uuid.uuid4().hex}.{ext}"
                        file.save(os.path.join(get_upload_path(), cat.image_filename))
                if request.form.get('remove_image') == 'on' and cat.image_filename:
                    old_path = os.path.join(get_upload_path(), cat.image_filename)
                    if os.path.exists(old_path):
                        os.remove(old_path)
                    cat.image_filename = None
                db.session.commit()
                flash(f'Kategorie "{cat.name}" aktualisiert.', 'success')
            elif action == 'delete':
                cat_id = request.form.get('category_id', type=int)
                cat = Category.query.get_or_404(cat_id)
                # Re-parent children to this category's parent
                for child in cat.children:
                    child.parent_id = cat.parent_id
                # Unassign items from this category
                Item.query.filter_by(category_id=cat_id).update({'category_id': None})
                # Remove image
                if cat.image_filename:
                    old_path = os.path.join(get_upload_path(), cat.image_filename)
                    if os.path.exists(old_path):
                        os.remove(old_path)
                db.session.delete(cat)
                db.session.commit()
                flash('Kategorie gelöscht.', 'success')
        except Exception as e:
            db.session.rollback()
            flash(f'Fehler: {str(e)}', 'error')

    cats = Category.query.order_by(Category.display_order, Category.name).all()
    category_tree = Category.get_tree(cats)
    return render_template('admin/categories.html', categories=cats, category_tree=category_tree)


# ============= INVENTORY =============

@admin_bp.route('/inventory')
@login_required
def inventory_list():
    """List all inventory items"""
    items = Item.query.all()
    categories = Category.query.order_by(Category.display_order, Category.name).all()
    category_tree = Category.get_tree(categories)
    # Build a mapping from category_id -> tree position for hierarchical sorting
    cat_order = {cat.id: idx for idx, (cat, depth) in enumerate(category_tree)}
    items.sort(key=lambda item: (cat_order.get(item.category_id, len(cat_order)), item.name))
    return render_template('admin/inventory_list.html', items=items, categories=categories, category_tree=category_tree)


@admin_bp.route('/inventory/add', methods=['GET', 'POST'])
@login_required
def inventory_add():
    """Add new inventory item"""
    categories = Category.query.order_by(Category.display_order, Category.name).all()
    category_tree = Category.get_tree(categories)
    users = User.query.filter_by(active=True).order_by(User.username).all()

    if request.method == 'POST':
        try:
            name = request.form.get('name', '').strip()
            default_rental_price = float(request.form.get('default_rental_price', 0))
            description = request.form.get('description', '').strip()
            category_id = request.form.get('category_id', type=int) or None
            show_price = request.form.get('show_price_publicly') == 'on'
            visible = request.form.get('visible_in_shop') == 'on'
            is_package = request.form.get('is_package') == 'on'
            show_bundle_discount = request.form.get('show_bundle_discount') == 'on'

            # Handle image upload
            image_filename = None
            if 'image' in request.files:
                file = request.files['image']
                if file and file.filename and allowed_image_file(file.filename):
                    ext = file.filename.rsplit('.', 1)[1].lower()
                    image_filename = f"{uuid.uuid4().hex}.{ext}"
                    file.save(os.path.join(get_upload_path(), image_filename))

            item = Item(
                name=name,
                category_id=category_id,
                description=description or None,
                default_rental_price_per_day=default_rental_price,
                show_price_publicly=show_price,
                visible_in_shop=visible,
                image_filename=image_filename,
                is_package=is_package,
                show_bundle_discount=show_bundle_discount
            )

            # Handle subcategories
            subcategory_ids = request.form.getlist('subcategory_ids', type=int)
            item.subcategories = Category.query.filter(Category.id.in_(subcategory_ids)).all() if subcategory_ids else []

            db.session.add(item)
            db.session.flush()  # Get the item.id

            # Handle package components
            if is_package:
                comp_item_ids = request.form.getlist('component_item_ids', type=int)
                comp_quantities = request.form.getlist('component_quantities', type=int)
                for comp_id, comp_qty in zip(comp_item_ids, comp_quantities):
                    if comp_id and comp_qty and comp_qty > 0:
                        pc = PackageComponent(
                            package_id=item.id,
                            component_item_id=comp_id,
                            quantity=comp_qty
                        )
                        db.session.add(pc)
            else:
                # Handle ownership entries
                ownership_user_ids = request.form.getlist('ownership_user_ids', type=int)
                ownership_quantities = request.form.getlist('ownership_quantities', type=int)
                ownership_ext_prices = request.form.getlist('ownership_ext_prices')
                ownership_ext_price_is_brutto = request.form.getlist('ownership_ext_price_is_brutto')
                ownership_purchase_costs = request.form.getlist('ownership_purchase_costs')
                ownership_purchase_cost_is_brutto = request.form.getlist('ownership_purchase_cost_is_brutto')

                for i, uid in enumerate(ownership_user_ids):
                    if not uid:
                        continue
                    qty = ownership_quantities[i] if i < len(ownership_quantities) else 0
                    ext_price_str = ownership_ext_prices[i] if i < len(ownership_ext_prices) else ''
                    purchase_cost_str = ownership_purchase_costs[i] if i < len(ownership_purchase_costs) else ''
                    ext_price = float(ext_price_str) if ext_price_str.strip() else None
                    purchase_cost = float(purchase_cost_str) if purchase_cost_str.strip() else 0

                    # Brutto/Netto flags (default to brutto=True)
                    ext_is_brutto_str = ownership_ext_price_is_brutto[i] if i < len(ownership_ext_price_is_brutto) else '1'
                    pc_is_brutto_str = ownership_purchase_cost_is_brutto[i] if i < len(ownership_purchase_cost_is_brutto) else '1'
                    ext_is_brutto = ext_is_brutto_str == '1'
                    pc_is_brutto = pc_is_brutto_str == '1'

                    # External users must always have an external price
                    owner_user = User.query.get(uid)
                    if owner_user and owner_user.is_external_user and ext_price is None:
                        flash(f'Externer Benutzer "{owner_user.display_name or owner_user.username}" erfordert einen externen Preis/Tag.', 'error')
                        db.session.rollback()
                        return render_template('admin/inventory_form.html',
                                               item=None,
                                               categories=categories,
                                               category_tree=category_tree,
                                               users=users,
                                               all_items=Item.query.filter_by(is_package=False).order_by(Item.name).all())

                    ownership = ItemOwnership(
                        item_id=item.id,
                        user_id=uid,
                        quantity=qty,
                        external_price_per_day=ext_price,
                        external_price_is_brutto=ext_is_brutto,
                        purchase_cost=purchase_cost,
                        purchase_cost_is_brutto=pc_is_brutto,
                    )
                    db.session.add(ownership)

            db.session.commit()
            flash(f'{name} erfolgreich hinzugefügt!', 'success')
            return redirect(url_for('admin.inventory_edit', item_id=item.id))

        except Exception as e:
            db.session.rollback()
            flash(f'Fehler beim Hinzufügen des Artikels: {str(e)}', 'error')

    return render_template('admin/inventory_form.html',
                           item=None,
                           categories=categories,
                           category_tree=category_tree,
                           users=users,
                           all_items=Item.query.filter_by(is_package=False).order_by(Item.name).all())


@admin_bp.route('/inventory/<int:item_id>/edit', methods=['GET', 'POST'])
@login_required
def inventory_edit(item_id):
    """Edit inventory item"""
    item = Item.query.get_or_404(item_id)
    categories = Category.query.order_by(Category.display_order, Category.name).all()
    category_tree = Category.get_tree(categories)
    users = User.query.filter_by(active=True).order_by(User.username).all()

    if not current_user.can_edit_item(item):
        flash('Sie haben keine Berechtigung, diesen Artikel zu bearbeiten.', 'error')
        return redirect(url_for('admin.inventory_list'))

    if request.method == 'POST':
        try:
            item.name = request.form.get('name', '').strip()
            item.default_rental_price_per_day = float(request.form.get('default_rental_price', 0))
            item.description = request.form.get('description', '').strip() or None
            item.category_id = request.form.get('category_id', type=int) or None
            item.show_price_publicly = request.form.get('show_price_publicly') == 'on'
            item.visible_in_shop = request.form.get('visible_in_shop') == 'on'
            item.is_package = request.form.get('is_package') == 'on'
            item.show_bundle_discount = request.form.get('show_bundle_discount') == 'on'

            if item.is_package:
                # Clear ownerships for packages
                for old_o in ItemOwnership.query.filter_by(item_id=item.id).all():
                    db.session.delete(old_o)

                # Update package components
                PackageComponent.query.filter_by(package_id=item.id).delete()
                comp_item_ids = request.form.getlist('component_item_ids', type=int)
                comp_quantities = request.form.getlist('component_quantities', type=int)
                for comp_id, comp_qty in zip(comp_item_ids, comp_quantities):
                    if comp_id and comp_qty and comp_qty > 0:
                        pc = PackageComponent(
                            package_id=item.id,
                            component_item_id=comp_id,
                            quantity=comp_qty
                        )
                        db.session.add(pc)
            else:
                # Update ownership entries
                ownership_ids = request.form.getlist('ownership_ids')
                ownership_user_ids = request.form.getlist('ownership_user_ids', type=int)
                ownership_quantities = request.form.getlist('ownership_quantities', type=int)
                ownership_ext_prices = request.form.getlist('ownership_ext_prices')
                ownership_ext_price_is_brutto = request.form.getlist('ownership_ext_price_is_brutto')
                ownership_purchase_costs = request.form.getlist('ownership_purchase_costs')
                ownership_purchase_cost_is_brutto = request.form.getlist('ownership_purchase_cost_is_brutto')

                # Collect existing ownership IDs BEFORE processing
                existing_ownership_ids = {o.id for o in ItemOwnership.query.filter_by(item_id=item.id).all()}
                submitted_ids = set()
                for i, uid in enumerate(ownership_user_ids):
                    if not uid:
                        continue
                    qty = ownership_quantities[i] if i < len(ownership_quantities) else 0
                    ext_price_str = ownership_ext_prices[i] if i < len(ownership_ext_prices) else ''
                    purchase_cost_str = ownership_purchase_costs[i] if i < len(ownership_purchase_costs) else ''
                    ext_price = float(ext_price_str) if ext_price_str.strip() else None
                    purchase_cost = float(purchase_cost_str) if purchase_cost_str.strip() else 0

                    # Brutto/Netto flags (default to brutto=True)
                    ext_is_brutto_str = ownership_ext_price_is_brutto[i] if i < len(ownership_ext_price_is_brutto) else '1'
                    pc_is_brutto_str = ownership_purchase_cost_is_brutto[i] if i < len(ownership_purchase_cost_is_brutto) else '1'
                    ext_is_brutto = ext_is_brutto_str == '1'
                    pc_is_brutto = pc_is_brutto_str == '1'

                    # External users must always have an external price
                    owner_user = User.query.get(uid)
                    if owner_user and owner_user.is_external_user and ext_price is None:
                        flash(f'Externer Benutzer "{owner_user.display_name or owner_user.username}" erfordert einen externen Preis/Tag.', 'error')
                        db.session.rollback()
                        return render_template('admin/inventory_form.html',
                                               item=item,
                                               categories=categories,
                                               category_tree=category_tree,
                                               users=users,
                                               all_items=Item.query.filter_by(is_package=False).order_by(Item.name).all())

                    oid_str = ownership_ids[i] if i < len(ownership_ids) else ''
                    oid = int(oid_str) if oid_str.strip() else None

                    if oid:
                        # Update existing ownership row
                        ownership = ItemOwnership.query.get(oid)
                        if ownership and ownership.item_id == item.id:
                            ownership.user_id = uid
                            ownership.quantity = qty
                            ownership.external_price_per_day = ext_price
                            ownership.external_price_is_brutto = ext_is_brutto
                            ownership.purchase_cost = purchase_cost
                            ownership.purchase_cost_is_brutto = pc_is_brutto
                            submitted_ids.add(oid)
                        else:
                            # ID invalid, create new
                            ownership = ItemOwnership(
                                item_id=item.id, user_id=uid, quantity=qty,
                                external_price_per_day=ext_price,
                                external_price_is_brutto=ext_is_brutto,
                                purchase_cost=purchase_cost,
                                purchase_cost_is_brutto=pc_is_brutto,
                            )
                            db.session.add(ownership)
                    else:
                        ownership = ItemOwnership(
                            item_id=item.id, user_id=uid, quantity=qty,
                            external_price_per_day=ext_price,
                            external_price_is_brutto=ext_is_brutto,
                            purchase_cost=purchase_cost,
                            purchase_cost_is_brutto=pc_is_brutto,
                        )
                        db.session.add(ownership)

                    db.session.flush()

                # Delete removed ownership rows
                for removed_id in existing_ownership_ids - submitted_ids:
                    old_o = ItemOwnership.query.get(removed_id)
                    if old_o:
                        db.session.delete(old_o)

            # Handle image upload
            if 'image' in request.files:
                file = request.files['image']
                if file and file.filename and allowed_image_file(file.filename):
                    # Delete old image
                    if item.image_filename:
                        old_path = os.path.join(get_upload_path(), item.image_filename)
                        if os.path.exists(old_path):
                            os.remove(old_path)
                    ext = file.filename.rsplit('.', 1)[1].lower()
                    item.image_filename = f"{uuid.uuid4().hex}.{ext}"
                    file.save(os.path.join(get_upload_path(), item.image_filename))

            # Remove image if requested
            if request.form.get('remove_image') == 'on' and item.image_filename:
                old_path = os.path.join(get_upload_path(), item.image_filename)
                if os.path.exists(old_path):
                    os.remove(old_path)
                item.image_filename = None

            # Handle subcategories
            subcategory_ids = request.form.getlist('subcategory_ids', type=int)
            item.subcategories = Category.query.filter(Category.id.in_(subcategory_ids)).all() if subcategory_ids else []

            db.session.commit()
            flash(f'{item.name} erfolgreich aktualisiert!', 'success')
            return redirect(url_for('admin.inventory_edit', item_id=item.id))

        except Exception as e:
            db.session.rollback()
            flash(f'Fehler beim Aktualisieren des Artikels: {str(e)}', 'error')

    return render_template('admin/inventory_form.html',
                           item=item,
                           categories=categories,
                           category_tree=category_tree,
                           users=users,
                           all_items=Item.query.filter(Item.is_package == False, Item.id != item.id).order_by(Item.name).all())


@admin_bp.route('/inventory/<int:item_id>/delete', methods=['POST'])
@login_required
def inventory_delete(item_id):
    """Delete inventory item"""
    item = Item.query.get_or_404(item_id)

    if not current_user.can_edit_item(item):
        flash('Sie haben keine Berechtigung, diesen Artikel zu löschen.', 'error')
        return redirect(url_for('admin.inventory_list'))

    try:
        # Delete image file
        if item.image_filename:
            old_path = os.path.join(get_upload_path(), item.image_filename)
            if os.path.exists(old_path):
                os.remove(old_path)
        # Remove this item from any packages it's a component of
        PackageComponent.query.filter_by(component_item_id=item.id).delete()
        name = item.name
        db.session.delete(item)
        db.session.commit()
        flash(f'{name} erfolgreich gelöscht!', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Fehler beim Löschen des Artikels: {str(e)}', 'error')

    return redirect(url_for('admin.inventory_list'))


# ============= QUOTE ITEM EXPENSES =============

@admin_bp.route('/expense/<int:expense_id>/mark_paid', methods=['POST'])
@login_required
def expense_mark_paid(expense_id):
    """Mark an external cost expense as paid"""
    expense = QuoteItemExpense.query.get_or_404(expense_id)
    try:
        paid_date_str = request.form.get('paid_at', '').strip()
        if paid_date_str:
            expense.paid_at = datetime.strptime(paid_date_str, '%Y-%m-%d')
        else:
            expense.paid_at = datetime.utcnow()
        expense.paid = True
        notes = request.form.get('notes', '').strip()
        if notes:
            expense.notes = notes

        # Book expense in accounting service BEFORE committing
        acct_account_id_str = request.form.get('accounting_account_id', '').strip()
        acct_account_id = int(acct_account_id_str) if acct_account_id_str else None
        if accounting.is_configured():
            ok, acct_msg = _book_expense_transaction(expense, expense.quote_item, account_id=acct_account_id)
            if not ok:
                db.session.rollback()
                flash(f'Buchhaltung fehlgeschlagen: {acct_msg}', 'error')
                return redirect(url_for('admin.quote_view', quote_id=expense.quote_item.quote_id))

        db.session.commit()

        # Upload all attached expense documents to the accounting transaction
        if accounting.is_configured() and expense.accounting_transaction_id and expense.documents:
            import mimetypes
            doc_files = []
            for doc in expense.documents:
                doc_path = os.path.join(get_upload_path(), doc.filename)
                if os.path.exists(doc_path):
                    with open(doc_path, 'rb') as f:
                        file_bytes = f.read()
                    ct = mimetypes.guess_type(doc.original_name)[0] or 'application/octet-stream'
                    doc_files.append((doc.original_name, file_bytes, ct))
            if doc_files:
                try:
                    dok_ok, dok_msg = accounting.upload_transaction_documents(
                        expense.accounting_transaction_id, doc_files)
                    if not dok_ok:
                        flash(f'Buchhaltung Dokument: {dok_msg}', 'warning')
                except Exception as doc_err:
                    flash(f'Dokument-Upload fehlgeschlagen: {doc_err}', 'warning')

        flash('Ausgabe als bezahlt markiert.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Fehler: {str(e)}', 'error')
    return redirect(url_for('admin.quote_view', quote_id=expense.quote_item.quote_id))


@admin_bp.route('/expense/<int:expense_id>/mark_unpaid', methods=['POST'])
@login_required
def expense_mark_unpaid(expense_id):
    """Mark an external cost expense as unpaid"""
    expense = QuoteItemExpense.query.get_or_404(expense_id)
    try:
        # Delete accounting transaction for this expense
        ok, acct_msg = _delete_expense_accounting(expense)
        if not ok:
            flash(f'Buchhaltung: {acct_msg}', 'warning')

        expense.paid = False
        # Keep paid_at so it can be prefilled when re-marking as paid
        db.session.commit()
        flash('Ausgabe als unbezahlt markiert.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Fehler: {str(e)}', 'error')
    return redirect(url_for('admin.quote_view', quote_id=expense.quote_item.quote_id))


@admin_bp.route('/expense/<int:expense_id>/upload-document', methods=['POST'])
@login_required
def expense_upload_document(expense_id):
    """Upload a document (invoice, receipt) to an expense entry (AJAX)"""
    expense = QuoteItemExpense.query.get_or_404(expense_id)

    if 'document' not in request.files:
        return jsonify({'error': 'Keine Datei ausgewählt'}), 400

    file = request.files['document']
    if not file or not file.filename:
        return jsonify({'error': 'Keine Datei ausgewählt'}), 400

    if not allowed_document_file(file.filename):
        return jsonify({'error': 'Dateityp nicht erlaubt.'}), 400

    original_name = secure_filename(file.filename)
    ext = file.filename.rsplit('.', 1)[1].lower()
    stored_name = f"{uuid.uuid4().hex}.{ext}"
    file.save(os.path.join(get_upload_path(), stored_name))

    doc = QuoteItemExpenseDocument(
        expense_id=expense.id,
        filename=stored_name,
        original_name=original_name
    )
    db.session.add(doc)
    db.session.commit()

    return jsonify({
        'id': doc.id,
        'original_name': doc.original_name,
        'download_url': url_for('admin.expense_download_document', doc_id=doc.id),
        'delete_url': url_for('admin.expense_delete_document', doc_id=doc.id)
    })


@admin_bp.route('/expense/document/<int:doc_id>/download')
@login_required
def expense_download_document(doc_id):
    """Download an expense document"""
    doc = QuoteItemExpenseDocument.query.get_or_404(doc_id)
    from flask import send_from_directory
    return send_from_directory(get_upload_path(), doc.filename,
                               download_name=doc.original_name, as_attachment=True)


@admin_bp.route('/expense/document/<int:doc_id>/delete', methods=['POST'])
@login_required
def expense_delete_document(doc_id):
    """Delete an expense document (AJAX)"""
    doc = QuoteItemExpenseDocument.query.get_or_404(doc_id)

    file_path = os.path.join(get_upload_path(), doc.filename)
    if os.path.exists(file_path):
        os.remove(file_path)

    db.session.delete(doc)
    db.session.commit()
    return jsonify({'success': True})


# ============= QUOTES =============

@admin_bp.route('/quotes')
@login_required
def quote_list():
    """List all quotes"""
    quotes = Quote.query.order_by(Quote.created_at.desc()).all()
    return render_template('admin/quote_list.html', quotes=quotes)


@admin_bp.route('/quotes/create', methods=['GET', 'POST'])
@login_required
def quote_create():
    """Create new quote"""
    if request.method == 'POST':
        try:
            customer_name = request.form.get('customer_name', '').strip()
            start_date_str = request.form.get('start_date')
            end_date_str = request.form.get('end_date')

            start_date = datetime.strptime(start_date_str, '%Y-%m-%d') if start_date_str else None
            end_date = datetime.strptime(end_date_str, '%Y-%m-%d') if end_date_str else None

            if start_date and end_date and start_date > end_date:
                flash('Enddatum muss nach oder gleich dem Startdatum sein!', 'error')
                return render_template('admin/quote_create.html',
                                       accounting_configured=accounting.is_configured())

            rental_days = 1
            if start_date and end_date:
                delta = end_date - start_date
                rental_days = max(1, delta.days + 1)

            # API customer ID (when accounting API is configured)
            api_customer_id_str = request.form.get('api_customer_id', '').strip()
            api_customer_id = int(api_customer_id_str) if api_customer_id_str else None

            quote = Quote(
                customer_name=customer_name,
                created_by_id=current_user.id,
                start_date=start_date,
                end_date=end_date,
                rental_days=rental_days,
                status='draft',
                recipient_lines=request.form.get('recipient_lines', '').strip(),
                api_customer_id=api_customer_id,
            )
            db.session.add(quote)
            db.session.commit()

            quote.generate_reference_number()
            db.session.commit()

            flash(f'Angebot für {customer_name} erstellt!', 'success')
            return redirect(url_for('admin.quote_edit', quote_id=quote.id))

        except Exception as e:
            db.session.rollback()
            flash(f'Fehler beim Erstellen des Angebots: {str(e)}', 'error')

    return render_template('admin/quote_create.html',
                           accounting_configured=accounting.is_configured())


@admin_bp.route('/quotes/<int:quote_id>/edit', methods=['GET', 'POST'])
@login_required
def quote_edit(quote_id):
    """Edit quote and add items"""
    quote = Quote.query.get_or_404(quote_id)
    items = Item.query.order_by(Item.name).all()
    categories = Category.query.order_by(Category.display_order, Category.name).all()
    category_tree = Category.get_tree(categories)

    if request.method == 'POST':
        action = request.form.get('action')

        try:
            if action == 'update_quote':
                quote.customer_name = request.form.get('customer_name', '').strip()
                # API customer ID (when accounting API is configured)
                api_cid_str = request.form.get('api_customer_id', '').strip()
                quote.api_customer_id = int(api_cid_str) if api_cid_str else None
                start_date_str = request.form.get('start_date')
                end_date_str = request.form.get('end_date')

                start_date = datetime.strptime(start_date_str, '%Y-%m-%d') if start_date_str else None
                end_date = datetime.strptime(end_date_str, '%Y-%m-%d') if end_date_str else None

                if start_date and end_date and start_date > end_date:
                    flash('Enddatum muss nach oder gleich dem Startdatum sein!', 'error')
                    item_availability = {item.id: item.total_quantity for item in items}
                    _ss = SiteSettings.query.first()
                    return render_template('admin/quote_edit.html', quote=quote, items=items, categories=categories, category_tree=category_tree, item_availability=item_availability, accounting_configured=accounting.is_configured(), site_settings=_ss, tax_rate=(_ss.tax_rate if _ss and _ss.tax_rate else 19.0))

                quote.start_date = start_date
                quote.end_date = end_date

                if start_date and end_date:
                    delta = end_date - start_date
                    quote.rental_days = max(1, delta.days + 1)
                else:
                    quote.rental_days = int(request.form.get('rental_days', 1))

                # Manual rental days override
                # Only update if the form explicitly includes the field
                if 'rental_days_override' in request.form:
                    override_str = request.form.get('rental_days_override', '').strip()
                    quote.rental_days_override = int(override_str) if override_str else None

                quote.recipient_lines = request.form.get('recipient_lines', '')
                quote.notes = request.form.get('notes', '')
                quote.public_notes = request.form.get('public_notes', '')
                db.session.commit()
                flash('Angebot aktualisiert!', 'success')

            elif action == 'add_item':
                if not quote.start_date or not quote.end_date:
                    flash('Bitte setzen Sie Start- und Enddatum, bevor Sie Artikel hinzufügen!', 'error')
                else:
                    item_id = request.form.get('item_id', type=int)
                    if item_id:
                        item = Item.query.get(item_id)
                        if item:
                            if item.is_package:
                                # Check if package already added
                                existing_pkg = QuoteItem.query.filter_by(
                                    quote_id=quote.id, package_id=item.id
                                ).first()
                                if existing_pkg:
                                    flash(f'{item.name} ist bereits im Angebot.', 'info')
                                else:
                                    # Calculate proportional prices based on package price
                                    component_price_sum = item.component_price_sum
                                    for pc in item.package_components:
                                        if component_price_sum > 0:
                                            # Proportional share of package price
                                            comp_share = (pc.component_item.default_rental_price_per_day * pc.quantity) / component_price_sum
                                            adjusted_price = round((item.default_rental_price_per_day * comp_share) / pc.quantity, 2)
                                        else:
                                            adjusted_price = 0
                                        # Calculate blended external cost
                                        ext_cost_total, _ = pc.component_item.calculate_external_cost(pc.quantity)
                                        ext_cost_per_unit = round(ext_cost_total / pc.quantity, 2) if pc.quantity > 0 else 0
                                        qi = QuoteItem(
                                            quote_id=quote.id,
                                            item_id=pc.component_item_id,
                                            quantity=pc.quantity,
                                            rental_price_per_day=adjusted_price,
                                            rental_cost_per_day=ext_cost_per_unit,
                                            is_custom=False,
                                            package_id=item.id
                                        )
                                        db.session.add(qi)
                                    db.session.commit()
                                    flash(f'Paket {item.name} mit {len(item.package_components)} Komponenten hinzugefügt!', 'success')
                            else:
                                existing = QuoteItem.query.filter_by(
                                    quote_id=quote.id, item_id=item.id, is_custom=False, package_id=None
                                ).first()
                                if existing:
                                    flash(f'{item.name} ist bereits im Angebot.', 'info')
                                else:
                                    # Calculate blended external cost for initial quantity
                                    initial_qty = 1
                                    ext_cost_total, _ = item.calculate_external_cost(initial_qty)
                                    ext_cost_per_unit = round(ext_cost_total / initial_qty, 2) if initial_qty > 0 else 0
                                    qi = QuoteItem(
                                        quote_id=quote.id,
                                        item_id=item.id,
                                        quantity=initial_qty,
                                        rental_price_per_day=item.default_rental_price_per_day,
                                        rental_cost_per_day=ext_cost_per_unit,
                                        is_custom=False
                                    )
                                    db.session.add(qi)
                                    db.session.commit()
                                    flash(f'{item.name} hinzugefügt!', 'success')

            elif action == 'update_items':
                if not quote.start_date or not quote.end_date:
                    flash('Bitte setzen Sie Start- und Enddatum, bevor Sie Artikel bearbeiten!', 'error')
                    item_availability = {item.id: item.total_quantity for item in items}
                    _ss = SiteSettings.query.first()
                    return render_template('admin/quote_edit.html', quote=quote, items=items, categories=categories, category_tree=category_tree, item_availability=item_availability, accounting_configured=accounting.is_configured(), site_settings=_ss, tax_rate=(_ss.tax_rate if _ss and _ss.tax_rate else 19.0))

                errors = []
                for qi in quote.quote_items:
                    if qi.is_custom:
                        continue
                    if not qi.item:
                        continue

                    # Use qi.id as unique key for package components
                    if qi.package_id:
                        quantity_key = f'quantity_pkg_{qi.id}'
                        price_key = f'price_pkg_{qi.id}'
                        cost_key = f'cost_pkg_{qi.id}'
                        exempt_key = f'discount_exempt_pkg_{qi.id}'
                    else:
                        quantity_key = f'quantity_{qi.item_id}'
                        price_key = f'price_{qi.item_id}'
                        cost_key = f'cost_{qi.item_id}'
                        exempt_key = f'discount_exempt_{qi.item_id}'

                    if quantity_key in request.form:
                        quantity = int(request.form.get(quantity_key, 0))
                        price = round(float(request.form.get(price_key, qi.rental_price_per_day)), 2)
                        cost = round(float(request.form.get(cost_key, qi.rental_cost_per_day)), 2)
                        exempt = request.form.get(exempt_key) == 'on'

                        if quantity > 0:
                            available = get_available_quantity(
                                qi.item_id,
                                quote.start_date,
                                quote.end_date,
                                exclude_quote_id=quote.id
                            )

                            if available != -1 and quantity > available:
                                errors.append(f'{qi.item.name}: Nur {available} verfügbar (gesamt: {qi.item.total_quantity}), aber {quantity} zugewiesen')

                            qi.quantity = quantity
                            qi.rental_price_per_day = price
                            qi.rental_cost_per_day = cost
                            qi.discount_exempt = exempt
                        else:
                            db.session.delete(qi)

                # Also update custom items discount_exempt
                for qi in quote.quote_items:
                    if qi.is_custom:
                        exempt_key = f'discount_exempt_custom_{qi.id}'
                        qi.discount_exempt = request.form.get(exempt_key) == 'on'

                if errors:
                    flash('⚠ Bestandswarnung: ' + '; '.join(errors), 'warning')
                db.session.commit()
                flash('Artikel aktualisiert!', 'success')

            elif action == 'remove_item':
                quote_item_id = int(request.form.get('quote_item_id'))
                quote_item = QuoteItem.query.get(quote_item_id)
                if quote_item and quote_item.quote_id == quote.id:
                    db.session.delete(quote_item)
                    db.session.commit()
                    flash('Artikel aus Angebot entfernt!', 'success')

            elif action == 'remove_package':
                package_id = int(request.form.get('package_id'))
                pkg_items = QuoteItem.query.filter_by(quote_id=quote.id, package_id=package_id).all()
                for qi in pkg_items:
                    db.session.delete(qi)
                db.session.commit()
                flash('Paket aus Angebot entfernt!', 'success')

            elif action == 'add_custom':
                custom_name = request.form.get('custom_name', '').strip()
                custom_quantity = int(request.form.get('custom_quantity', 1))
                custom_price = round(float(request.form.get('custom_price', 0)), 2)

                if custom_name and custom_price > 0:
                    quote_item = QuoteItem(
                        quote_id=quote.id,
                        item_id=None,
                        quantity=custom_quantity,
                        rental_price_per_day=custom_price,
                        custom_item_name=custom_name,
                        is_custom=True
                    )
                    db.session.add(quote_item)
                    db.session.commit()
                    flash(f'Eigene Position "{custom_name}" hinzugefügt!', 'success')

            elif action == 'update_discount':
                target_total_str = request.form.get('target_total', '').strip()
                if target_total_str:
                    # Calculate discount percent from target total
                    target_total = float(target_total_str)
                    discountable = quote.discountable_subtotal
                    if discountable > 0:
                        needed_discount = quote.subtotal - target_total
                        discount_percent = max(0, min(100, (needed_discount / discountable) * 100))
                    else:
                        discount_percent = 0
                else:
                    discount_percent = float(request.form.get('final_discount_percent', 0))
                quote.discount_percent = discount_percent
                quote.discount_label = request.form.get('discount_label', '').strip() or None
                db.session.commit()
                flash(f'Rabatt auf {discount_percent:.4f}% aktualisiert (Gesamt: €{quote.total:.2f})', 'success')

            elif action == 'finalize':
                if not quote.start_date or not quote.end_date:
                    flash('Kann nicht finalisiert werden: Start- und Enddatum müssen gesetzt sein!', 'error')
                    item_availability = {item.id: item.total_quantity for item in items}
                    _ss = SiteSettings.query.first()
                    return render_template('admin/quote_edit.html', quote=quote, items=items, categories=categories, category_tree=category_tree, item_availability=item_availability, accounting_configured=accounting.is_configured(), site_settings=_ss, tax_rate=(_ss.tax_rate if _ss and _ss.tax_rate else 19.0))

                validation_warnings = []
                for quote_item in quote.quote_items:
                    if not quote_item.is_custom and quote_item.item:
                        available = get_available_quantity(
                            quote_item.item_id,
                            quote.start_date,
                            quote.end_date,
                            exclude_quote_id=quote.id
                        )
                        if available != -1 and quote_item.quantity > available:
                            pkg_note = f' (Paket: {quote_item.package.name})' if quote_item.package_id else ''
                            validation_warnings.append(
                                f'{quote_item.item.name}{pkg_note}: Nur {available} verfügbar (Angebot hat {quote_item.quantity})'
                            )

                if validation_warnings:
                    flash('⚠ Bestandswarnung: ' + '; '.join(validation_warnings), 'warning')

                quote.status = 'finalized'
                # Use provided date (from re-finalize dialog) or current time
                finalized_date_str = request.form.get('finalized_at', '').strip()
                if finalized_date_str:
                    quote.finalized_at = datetime.strptime(finalized_date_str, '%Y-%m-%d')
                else:
                    quote.finalized_at = datetime.utcnow()

                # Create QuoteItemExpense entries for items with external costs
                for qi in quote.quote_items:
                    if qi.rental_cost_per_day and qi.rental_cost_per_day > 0 and not qi.expense:
                        expense = QuoteItemExpense(
                            quote_item_id=qi.id,
                            amount=qi.total_external_cost,
                        )
                        db.session.add(expense)

                # Sync API quote (create if not exists, update if exists)
                if accounting.is_configured():
                    if not quote.api_quote_id:
                        ok, err = _sync_create_api_quote(quote)
                        if not ok:
                            flash(f'API-Angebot erstellen fehlgeschlagen: {err}', 'warning')
                    else:
                        ok, err = _sync_update_api_quote(quote)
                        if not ok:
                            flash(f'API-Angebot aktualisieren fehlgeschlagen: {err}', 'warning')
                    # Generate API quote PDF
                    if quote.api_quote_id:
                        accounting.generate_quote_pdf(quote.api_quote_id)
                    # Set API quote status to sent
                    if quote.api_quote_id:
                        _sync_api_quote_status(quote, 'sent')

                db.session.commit()
                flash('Angebot finalisiert!', 'success')
                return redirect(url_for('admin.quote_view', quote_id=quote.id))

        except Exception as e:
            db.session.rollback()
            flash(f'Fehler: {str(e)}', 'error')

    # Calculate availability
    item_availability = {}
    if quote.start_date and quote.end_date:
        for item in items:
            if item.is_package:
                item_availability[item.id] = get_package_available_quantity(
                    item.id, quote.start_date, quote.end_date, exclude_quote_id=quote.id)
            else:
                item_availability[item.id] = get_available_quantity(
                    item.id, quote.start_date, quote.end_date, exclude_quote_id=quote.id)
    else:
        for item in items:
            item_availability[item.id] = item.total_quantity

    _ss = SiteSettings.query.first()
    return render_template('admin/quote_edit.html', quote=quote, items=items, categories=categories, category_tree=category_tree, item_availability=item_availability, accounting_configured=accounting.is_configured(), site_settings=_ss, tax_rate=(_ss.tax_rate if _ss and _ss.tax_rate else 19.0))


@admin_bp.route('/quotes/<int:quote_id>')
@login_required
def quote_view(quote_id):
    """View quote details"""
    quote = Quote.query.get_or_404(quote_id)
    from datetime import date as date_cls
    site_settings = SiteSettings.query.first()
    return render_template('admin/quote_view.html', quote=quote, today=date_cls.today().isoformat(),
                           accounting_configured=accounting.is_configured(),
                           site_settings=site_settings)


@admin_bp.route('/quotes/<int:quote_id>/unfinalize', methods=['POST'])
@login_required
def quote_unfinalize(quote_id):
    quote = Quote.query.get_or_404(quote_id)
    try:
        if quote.status == 'finalized':
            quote.status = 'draft'
            # Keep finalized_at so we can offer it when re-finalizing
            # Sync API quote status back to draft
            _sync_api_quote_status(quote, 'draft')
            db.session.commit()
            flash('Angebot zurück in den Entwurf versetzt!', 'success')
        elif quote.status == 'performed':
            flash('Bitte zuerst die Durchführung aufheben.', 'info')
        else:
            flash('Angebot ist nicht finalisiert.', 'info')
    except Exception as e:
        db.session.rollback()
        flash(f'Fehler: {str(e)}', 'error')
    return redirect(url_for('admin.quote_edit', quote_id=quote_id))


@admin_bp.route('/quotes/<int:quote_id>/mark_performed', methods=['POST'])
@login_required
def quote_mark_performed(quote_id):
    """Mark quote as performed (Durchgeführt)."""
    quote = Quote.query.get_or_404(quote_id)
    try:
        if quote.status == 'finalized':
            performed_date_str = request.form.get('performed_at', '').strip()
            if performed_date_str:
                quote.performed_at = datetime.strptime(performed_date_str, '%Y-%m-%d')
            else:
                quote.performed_at = datetime.utcnow()

            quote.status = 'performed'

            db.session.commit()
            flash('Angebot als durchgeführt markiert!', 'success')
        else:
            flash('Angebot muss finalisiert sein.', 'info')
    except Exception as e:
        db.session.rollback()
        flash(f'Fehler: {str(e)}', 'error')
    return redirect(url_for('admin.quote_view', quote_id=quote_id))


@admin_bp.route('/quotes/<int:quote_id>/unperform', methods=['POST'])
@login_required
def quote_unperform(quote_id):
    """Revert performed status back to finalized. Cancels receivable Journal Entry."""
    quote = Quote.query.get_or_404(quote_id)
    try:
        if quote.status == 'performed':
            quote.status = 'finalized'
            # Keep performed_at for re-performing
            db.session.commit()
            flash('Durchführung aufgehoben!', 'success')
        else:
            flash('Angebot ist nicht als durchgeführt markiert.', 'info')
    except Exception as e:
        db.session.rollback()
        flash(f'Fehler: {str(e)}', 'error')
    return redirect(url_for('admin.quote_view', quote_id=quote_id))


@admin_bp.route('/quotes/<int:quote_id>/mark_paid', methods=['POST'])
@login_required
def quote_mark_paid(quote_id):
    """Mark quote as paid and update item revenue"""
    quote = Quote.query.get_or_404(quote_id)
    try:
        if quote.status != 'paid':
            # Check if a custom paid_at date was provided
            paid_date_str = request.form.get('paid_at', '').strip()
            if paid_date_str:
                quote.paid_at = datetime.strptime(paid_date_str, '%Y-%m-%d')
            else:
                quote.paid_at = datetime.utcnow()

            paid_date_iso = quote.paid_at.strftime('%Y-%m-%d')

            # Accounting tax treatment override (from pay dialog)
            acct_tax = request.form.get('accounting_tax_treatment', '').strip()
            if acct_tax:
                quote.accounting_tax_treatment = acct_tax

            # Accounting account override (from pay dialog)
            acct_account_id_str = request.form.get('accounting_account_id', '').strip()
            acct_account_id = int(acct_account_id_str) if acct_account_id_str else None

            # Accounting category override (from pay dialog)
            acct_category_id_str = request.form.get('accounting_category_id', '').strip()
            acct_category_id = int(acct_category_id_str) if acct_category_id_str else None

            # If API invoice exists, use mark-paid on it
            if accounting.is_configured() and quote.api_invoice_id:
                site_settings = SiteSettings.query.first()
                if not acct_account_id and site_settings:
                    acct_account_id = site_settings.accounting_income_account_id
                if not acct_category_id and site_settings:
                    acct_category_id = site_settings.accounting_income_category_id
                if not acct_account_id:
                    flash('Kein Buchhaltungs-Konto ausgewählt.', 'error')
                    return redirect(url_for('admin.quote_view', quote_id=quote_id))
                ok, result = accounting.mark_invoice_paid(
                    quote.api_invoice_id,
                    account_id=acct_account_id,
                    category_id=acct_category_id,
                    payment_date=paid_date_iso,
                )
                if not ok:
                    db.session.rollback()
                    flash(f'API-Rechnung bezahlen fehlgeschlagen: {result}', 'error')
                    return redirect(url_for('admin.quote_view', quote_id=quote_id))
                # Store linked transaction ID from API response
                if isinstance(result, dict):
                    txn = result.get('transaction', {})
                    if isinstance(txn, dict) and txn.get('id'):
                        quote.accounting_transaction_id = txn['id']

            elif accounting.is_configured():
                # Fallback: book directly as transaction (no API invoice)
                ok, acct_msg = _book_quote_income(quote, account_id=acct_account_id)
                if not ok:
                    db.session.rollback()
                    flash(f'Buchhaltung fehlgeschlagen: {acct_msg}', 'error')
                    return redirect(url_for('admin.quote_view', quote_id=quote_id))

            quote.status = 'paid'

            discount_multiplier = (100 - quote.discount_percent) / 100
            for quote_item in quote.quote_items:
                if not quote_item.is_custom and quote_item.item:
                    multiplier = 1.0 if quote_item.discount_exempt else discount_multiplier
                    item_revenue = round(quote_item.total_price * multiplier, 2)
                    quote_item.item.total_revenue = round(quote_item.item.total_revenue + item_revenue, 2)
                    # Accumulate external rental costs
                    if quote_item.rental_cost_per_day:
                        item_cost = quote_item.total_external_cost
                        quote_item.item.total_cost = round(quote_item.item.total_cost + item_cost, 2)

            db.session.commit()

            # Upload Rechnung PDF as document to the accounting transaction
            # Only when there's NO API invoice (legacy flow)
            if accounting.is_configured() and quote.accounting_transaction_id and not quote.api_invoice_id:
                try:
                    pdf_bytes = _generate_rechnung_pdf_bytes(quote)
                    filename = f'rechnung_{quote.reference_number or quote.id}.pdf'
                    dok_ok, dok_msg = accounting.upload_transaction_document(
                        quote.accounting_transaction_id, pdf_bytes, filename)
                    if not dok_ok:
                        flash(f'Buchhaltung Dokument: {dok_msg}', 'warning')
                except Exception as doc_err:
                    flash(f'Rechnung-Upload fehlgeschlagen: {doc_err}', 'warning')

            flash('Angebot als bezahlt markiert und Umsatz aktualisiert!', 'success')
        else:
            flash('Angebot ist bereits als bezahlt markiert.', 'info')
    except Exception as e:
        db.session.rollback()
        flash(f'Fehler: {str(e)}', 'error')
    return redirect(url_for('admin.quote_view', quote_id=quote_id))


@admin_bp.route('/quotes/<int:quote_id>/update_paid_date', methods=['POST'])
@login_required
def quote_update_paid_date(quote_id):
    """Update the paid_at date of a paid quote and re-book payment JE if needed."""
    quote = Quote.query.get_or_404(quote_id)
    try:
        if quote.status == 'paid':
            paid_date_str = request.form.get('paid_at', '').strip()
            if paid_date_str:
                quote.paid_at = datetime.strptime(paid_date_str, '%Y-%m-%d')
                # Update date in accounting service
                if accounting.is_configured() and quote.accounting_transaction_id:
                    ok, msg = accounting.update_transaction(
                        quote.accounting_transaction_id, date=paid_date_str)
                    if not ok:
                        flash(f'Buchhaltung: {msg}', 'warning')
                db.session.commit()
                flash('Bezahldatum aktualisiert!', 'success')
            else:
                flash('Kein Datum angegeben.', 'error')
        else:
            flash('Angebot ist nicht als bezahlt markiert.', 'info')
    except Exception as e:
        db.session.rollback()
        flash(f'Fehler: {str(e)}', 'error')
    return redirect(url_for('admin.quote_view', quote_id=quote_id))


@admin_bp.route('/quotes/<int:quote_id>/update_finalized_date', methods=['POST'])
@login_required
def quote_update_finalized_date(quote_id):
    """Update the finalized_at date of a finalized/paid quote"""
    quote = Quote.query.get_or_404(quote_id)
    try:
        if quote.status in ('finalized', 'performed', 'paid'):
            finalized_date_str = request.form.get('finalized_at', '').strip()
            if finalized_date_str:
                quote.finalized_at = datetime.strptime(finalized_date_str, '%Y-%m-%d')
                db.session.commit()
                flash('Finalisierungsdatum aktualisiert!', 'success')
            else:
                flash('Kein Datum angegeben.', 'error')
        else:
            flash('Angebot ist nicht finalisiert.', 'info')
    except Exception as e:
        db.session.rollback()
        flash(f'Fehler: {str(e)}', 'error')
    return redirect(url_for('admin.quote_view', quote_id=quote_id))


@admin_bp.route('/quotes/<int:quote_id>/update_performed_date', methods=['POST'])
@login_required
def quote_update_performed_date(quote_id):
    """Update the performed_at date and re-book receivable JE if needed."""
    quote = Quote.query.get_or_404(quote_id)
    try:
        if quote.status in ('performed', 'paid'):
            performed_date_str = request.form.get('performed_at', '').strip()
            if performed_date_str:
                quote.performed_at = datetime.strptime(performed_date_str, '%Y-%m-%d')
                db.session.commit()
                flash('Durchführungsdatum aktualisiert!', 'success')
            else:
                flash('Kein Datum angegeben.', 'error')
        else:
            flash('Angebot ist nicht als durchgeführt markiert.', 'info')
    except Exception as e:
        db.session.rollback()
        flash(f'Fehler: {str(e)}', 'error')
    return redirect(url_for('admin.quote_view', quote_id=quote_id))


@admin_bp.route('/quotes/<int:quote_id>/unpay', methods=['POST'])
@login_required
def quote_unpay(quote_id):
    """Unpay quote and revert revenue"""
    quote = Quote.query.get_or_404(quote_id)
    try:
        if quote.status == 'paid':
            discount_multiplier = (100 - quote.discount_percent) / 100
            for quote_item in quote.quote_items:
                if not quote_item.is_custom and quote_item.item:
                    multiplier = 1.0 if quote_item.discount_exempt else discount_multiplier
                    item_revenue = round(quote_item.total_price * multiplier, 2)
                    quote_item.item.total_revenue = round(quote_item.item.total_revenue - item_revenue, 2)
                    # Reverse external rental costs
                    if quote_item.rental_cost_per_day:
                        item_cost = quote_item.total_external_cost
                        quote_item.item.total_cost = round(quote_item.item.total_cost - item_cost, 2)

            # Unmark API invoice paid if applicable
            if accounting.is_configured() and quote.api_invoice_id:
                ok, result = accounting.unmark_invoice_paid(quote.api_invoice_id)
                if not ok:
                    flash(f'API-Rechnung Zahlung aufheben fehlgeschlagen: {result}', 'warning')
                else:
                    quote.accounting_transaction_id = None
            else:
                # Legacy: delete accounting transaction directly
                ok, acct_msg = _delete_quote_accounting(quote)
                if not ok:
                    flash(f'Buchhaltung: {acct_msg}', 'warning')

            # Revert to performed if it was performed, otherwise to finalized
            if quote.performed_at:
                quote.status = 'performed'
            else:
                quote.status = 'finalized'
            # Keep paid_at so we can offer it when re-marking as paid
            db.session.commit()
            flash('Zahlung aufgehoben und Umsatz zurückerstattet!', 'success')
        else:
            flash('Angebot ist nicht als bezahlt markiert.', 'info')
    except Exception as e:
        db.session.rollback()
        flash(f'Fehler: {str(e)}', 'error')
    return redirect(url_for('admin.quote_view', quote_id=quote_id))


@admin_bp.route('/quotes/<int:quote_id>/delete', methods=['POST'])
@login_required
def quote_delete(quote_id):
    """Delete quote (only allowed in draft status)"""
    quote = Quote.query.get_or_404(quote_id)
    if quote.status != 'draft':
        flash('Nur Entwürfe können gelöscht werden.', 'error')
        return redirect(url_for('admin.quote_view', quote_id=quote_id))
    try:
        # Delete API quote if exists
        _sync_delete_api_quote(quote)
        db.session.delete(quote)
        db.session.commit()
        flash('Angebot gelöscht!', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Fehler: {str(e)}', 'error')
    return redirect(url_for('admin.quote_list'))


# ============= INQUIRIES =============

@admin_bp.route('/inquiries')
@login_required
def inquiry_list():
    """List all customer inquiries"""
    inquiries = Inquiry.query.order_by(Inquiry.created_at.desc()).all()
    return render_template('admin/inquiry_list.html', inquiries=inquiries)


@admin_bp.route('/inquiries/<int:inquiry_id>')
@login_required
def inquiry_view(inquiry_id):
    """View inquiry details"""
    inquiry = Inquiry.query.get_or_404(inquiry_id)
    return render_template('admin/inquiry_view.html', inquiry=inquiry,
                           accounting_configured=accounting.is_configured())


@admin_bp.route('/inquiries/<int:inquiry_id>/status', methods=['POST'])
@login_required
def inquiry_update_status(inquiry_id):
    """Update inquiry status"""
    inquiry = Inquiry.query.get_or_404(inquiry_id)
    new_status = request.form.get('status')
    if new_status in ['new', 'contacted', 'converted', 'closed']:
        inquiry.status = new_status
        db.session.commit()
        flash(f'Anfragestatus auf {new_status} aktualisiert.', 'success')
    return redirect(url_for('admin.inquiry_view', inquiry_id=inquiry_id))


@admin_bp.route('/inquiries/<int:inquiry_id>/convert', methods=['POST'])
@login_required
def inquiry_convert(inquiry_id):
    """Convert inquiry to a quote"""
    inquiry = Inquiry.query.get_or_404(inquiry_id)

    try:
        # API customer ID (optional, when accounting API is configured)
        api_cid_str = request.form.get('api_customer_id', '').strip()
        api_customer_id = int(api_cid_str) if api_cid_str else None

        quote = Quote(
            customer_name=inquiry.customer_name,
            created_by_id=current_user.id,
            start_date=inquiry.desired_start_date,
            end_date=inquiry.desired_end_date,
            rental_days=1,
            status='draft',
            inquiry_id=inquiry.id,
            api_customer_id=api_customer_id,
            notes=f"Aus Anfrage umgewandelt. E-Mail: {inquiry.customer_email}"
                  + (f", Telefon: {inquiry.customer_phone}" if inquiry.customer_phone else "")
                  + (f"\n{inquiry.message}" if inquiry.message else "")
        )
        if quote.start_date and quote.end_date:
            delta = quote.end_date - quote.start_date
            quote.rental_days = max(1, delta.days + 1)

        db.session.add(quote)
        db.session.commit()

        quote.generate_reference_number()

        # Add inquiry items to the quote
        for inq_item in inquiry.items:
            item = Item.query.get(inq_item.item_id)
            if item:
                if item.is_package:
                    # Expand package into components
                    component_price_sum = item.component_price_sum
                    for pc in item.package_components:
                        if component_price_sum > 0:
                            comp_share = (pc.component_item.default_rental_price_per_day * pc.quantity) / component_price_sum
                            adjusted_price = round((item.default_rental_price_per_day * comp_share) / pc.quantity, 2)
                        else:
                            adjusted_price = 0
                        for _ in range(inq_item.quantity):
                            ext_cost_total, _ = pc.component_item.calculate_external_cost(pc.quantity)
                            ext_cost_per_unit = round(ext_cost_total / pc.quantity, 2) if pc.quantity > 0 else 0
                            qi = QuoteItem(
                                quote_id=quote.id,
                                item_id=pc.component_item_id,
                                quantity=pc.quantity,
                                rental_price_per_day=adjusted_price,
                                rental_cost_per_day=ext_cost_per_unit,
                                is_custom=False,
                                package_id=item.id
                            )
                            db.session.add(qi)
                else:
                    ext_cost_total, _ = item.calculate_external_cost(inq_item.quantity)
                    ext_cost_per_unit = round(ext_cost_total / inq_item.quantity, 2) if inq_item.quantity > 0 else 0
                    qi = QuoteItem(
                        quote_id=quote.id,
                        item_id=item.id,
                        quantity=inq_item.quantity,
                        rental_price_per_day=item.default_rental_price_per_day,
                        rental_cost_per_day=ext_cost_per_unit,
                        is_custom=False
                    )
                    db.session.add(qi)

        inquiry.status = 'converted'
        db.session.commit()

        flash(f'Angebot aus Anfrage erstellt!', 'success')
        return redirect(url_for('admin.quote_edit', quote_id=quote.id))

    except Exception as e:
        db.session.rollback()
        flash(f'Fehler beim Umwandeln der Anfrage: {str(e)}', 'error')
        return redirect(url_for('admin.inquiry_view', inquiry_id=inquiry_id))


@admin_bp.route('/inquiries/<int:inquiry_id>/delete', methods=['POST'])
@login_required
def inquiry_delete(inquiry_id):
    """Delete an inquiry"""
    inquiry = Inquiry.query.get_or_404(inquiry_id)
    try:
        db.session.delete(inquiry)
        db.session.commit()
        flash('Anfrage gelöscht.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Fehler: {str(e)}', 'error')
    return redirect(url_for('admin.inquiry_list'))


# ============= USER MANAGEMENT =============

@admin_bp.route('/users')
@admin_required
def user_list():
    """List all users (admin only)"""
    users = User.query.order_by(User.username).all()
    return render_template('admin/user_list.html', users=users)


@admin_bp.route('/users/add', methods=['GET', 'POST'])
@admin_required
def user_add():
    """Add new user (admin only)"""
    if request.method == 'POST':
        try:
            username = request.form.get('username', '').strip()
            password = request.form.get('password', '')
            display_name = request.form.get('display_name', '').strip()
            email = request.form.get('email', '').strip()
            is_admin = request.form.get('is_admin') == 'on'
            can_edit_all = request.form.get('can_edit_all') == 'on'
            is_external_user = request.form.get('is_external_user') == 'on'

            if not username or not password:
                flash('Benutzername und Passwort sind erforderlich.', 'error')
                return render_template('admin/user_form.html', user=None)

            if User.query.filter_by(username=username).first():
                flash('Benutzername existiert bereits.', 'error')
                return render_template('admin/user_form.html', user=None)

            user = User(
                username=username,
                display_name=display_name or None,
                email=email or None,
                is_admin=is_admin if not is_external_user else False,
                can_edit_all=can_edit_all if not is_external_user else False,
                is_external_user=is_external_user
            )
            user.set_password(password)
            db.session.add(user)
            db.session.commit()

            flash(f'Benutzer "{username}" erstellt.', 'success')
            return redirect(url_for('admin.user_list'))

        except Exception as e:
            db.session.rollback()
            flash(f'Fehler: {str(e)}', 'error')

    return render_template('admin/user_form.html', user=None)


@admin_bp.route('/users/<int:user_id>/edit', methods=['GET', 'POST'])
@admin_required
def user_edit(user_id):
    """Edit user (admin only)"""
    user = User.query.get_or_404(user_id)

    if request.method == 'POST':
        try:
            user.display_name = request.form.get('display_name', '').strip() or None
            user.email = request.form.get('email', '').strip() or None
            user.is_external_user = request.form.get('is_external_user') == 'on'
            user.is_admin = request.form.get('is_admin') == 'on' if not user.is_external_user else False
            user.can_edit_all = request.form.get('can_edit_all') == 'on' if not user.is_external_user else False
            user.active = request.form.get('active') == 'on'

            new_password = request.form.get('password', '').strip()
            if new_password:
                user.set_password(new_password)

            # Prevent removing own admin status
            if user.id == 1 and not user.is_admin:
                user.is_admin = True
                flash('Admin-Status des primären Admin-Kontos kann nicht entfernt werden.', 'info')

            db.session.commit()
            flash(f'Benutzer "{user.username}" aktualisiert.', 'success')
            return redirect(url_for('admin.user_list'))

        except Exception as e:
            db.session.rollback()
            flash(f'Fehler: {str(e)}', 'error')

    return render_template('admin/user_form.html', user=user)


@admin_bp.route('/users/<int:user_id>/delete', methods=['POST'])
@admin_required
def user_delete(user_id):
    """Delete user (admin only)"""
    if user_id == current_user.id:
        flash('Eigenes Konto kann nicht gelöscht werden.', 'error')
        return redirect(url_for('admin.user_list'))

    user = User.query.get_or_404(user_id)
    try:
        # Delete ownership entries for this user
        ItemOwnership.query.filter_by(user_id=user.id).delete()
        db.session.delete(user)
        db.session.commit()
        flash(f'Benutzer "{user.username}" gelöscht. Artikelzuordnungen wurden entfernt.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Fehler: {str(e)}', 'error')
    return redirect(url_for('admin.user_list'))


# ============= SETTINGS =============

@admin_bp.route('/settings', methods=['GET', 'POST'])
@admin_required
def settings():
    """Site settings (admin only)"""
    settings_record = SiteSettings.query.first()
    if not settings_record:
        settings_record = SiteSettings()
        db.session.add(settings_record)
        db.session.commit()

    if request.method == 'POST':
        try:
            settings_record.business_name = request.form.get('business_name', '').strip()
            settings_record.display_name = request.form.get('display_name', '').strip() or None
            settings_record.address_lines = request.form.get('address_lines', '')
            settings_record.contact_lines = request.form.get('contact_lines', '')
            settings_record.bank_lines = request.form.get('bank_lines', '')
            settings_record.tax_number = request.form.get('tax_number', '').strip()
            settings_record.vat_id = request.form.get('vat_id', '').strip()
            settings_record.tax_mode = request.form.get('tax_mode', 'kleinunternehmer').strip()
            settings_record.tax_rate = float(request.form.get('tax_rate', '19.0') or 19.0)
            settings_record.payment_terms_days = int(request.form.get('payment_terms_days', '14') or 14)
            settings_record.quote_validity_days = int(request.form.get('quote_validity_days', '14') or 14)
            settings_record.shop_description = request.form.get('shop_description', '')
            settings_record.imprint_url = request.form.get('imprint_url', '').strip()
            settings_record.privacy_url = request.form.get('privacy_url', '').strip()
            settings_record.terms_and_conditions_text = request.form.get('terms_and_conditions_text', '').strip() or None
            settings_record.notification_email = request.form.get('notification_email', '').strip()

            # Accounting API category IDs
            acct_income_cat = request.form.get('accounting_income_category_id', '').strip()
            settings_record.accounting_income_category_id = int(acct_income_cat) if acct_income_cat else None
            acct_expense_cat = request.form.get('accounting_expense_category_id', '').strip()
            settings_record.accounting_expense_category_id = int(acct_expense_cat) if acct_expense_cat else None

            # Accounting API account IDs
            acct_income_acc = request.form.get('accounting_income_account_id', '').strip()
            settings_record.accounting_income_account_id = int(acct_income_acc) if acct_income_acc else None
            acct_expense_acc = request.form.get('accounting_expense_account_id', '').strip()
            settings_record.accounting_expense_account_id = int(acct_expense_acc) if acct_expense_acc else None

            settings_record.updated_at = datetime.utcnow()

            # Handle logo upload
            if request.form.get('remove_logo'):
                if settings_record.logo_filename:
                    old_path = os.path.join(get_upload_path(), settings_record.logo_filename)
                    if os.path.exists(old_path):
                        os.remove(old_path)
                    settings_record.logo_filename = None
            logo_file = request.files.get('logo')
            if logo_file and logo_file.filename:
                from werkzeug.utils import secure_filename as sf
                ext = os.path.splitext(logo_file.filename)[1].lower()
                if ext in ('.png', '.jpg', '.jpeg', '.svg', '.webp', '.gif'):
                    # Remove old logo
                    if settings_record.logo_filename:
                        old_path = os.path.join(get_upload_path(), settings_record.logo_filename)
                        if os.path.exists(old_path):
                            os.remove(old_path)
                    filename = f'company_logo{ext}'
                    logo_file.save(os.path.join(get_upload_path(), filename))
                    settings_record.logo_filename = filename
                else:
                    flash('Ung\u00fcltiges Logo-Format. Erlaubt: PNG, JPEG, SVG, WebP, GIF', 'error')

            db.session.commit()
            flash('Einstellungen gespeichert!', 'success')
        except Exception as e:
            db.session.rollback()
            flash(f'Fehler: {str(e)}', 'error')

    return render_template('admin/settings.html', settings=settings_record,
                           accounting_configured=accounting.is_configured())


@admin_bp.route('/api/accounting/categories')
@login_required
def accounting_categories():
    """Proxy: fetch categories from the accounting service (for settings UI)."""
    if not accounting.is_configured():
        return jsonify({'error': 'Accounting API not configured'}), 503
    type_filter = request.args.get('type')
    cats = accounting.get_categories(type_filter=type_filter)
    return jsonify({'categories': cats})


@admin_bp.route('/api/accounting/accounts')
@login_required
def accounting_accounts():
    """Proxy: fetch accounts from the accounting service."""
    if not accounting.is_configured():
        return jsonify({'error': 'Accounting API not configured'}), 503
    accounts = accounting.get_accounts()
    return jsonify({'accounts': accounts})


@admin_bp.route('/api/accounting/tax-treatments')
@login_required
def accounting_tax_treatments():
    """Proxy: fetch tax treatments from the accounting service."""
    if not accounting.is_configured():
        return jsonify({'error': 'Accounting API not configured'}), 503
    treatments = accounting.get_tax_treatments()
    return jsonify({'tax_treatments': treatments})


@admin_bp.route('/logo')
@login_required
def serve_logo():
    """Serve the uploaded company logo"""
    site_settings = SiteSettings.query.first()
    if not site_settings or not site_settings.logo_filename:
        abort(404)
    logo_path = os.path.join(get_upload_path(), site_settings.logo_filename)
    if not os.path.exists(logo_path):
        abort(404)
    return send_file(logo_path)



# ============= REPORTS =============

@admin_bp.route('/reports/payoff')
@login_required
def report_payoff():
    """Payoff status report"""
    items = Item.query.order_by(Item.name).all()
    users = User.query.filter_by(active=True).order_by(User.username).all()

    misc_revenue = db.session.query(db.func.sum(
        QuoteItem.quantity * QuoteItem.rental_price_per_day * Quote.rental_days
    )).join(Quote).filter(
        QuoteItem.is_custom == True,
        Quote.status == 'paid'
    ).scalar() or 0.0

    # Separate owned and external items
    owned_items = [i for i in items if not i.is_external]
    external_items = [i for i in items if i.is_external]

    # Get all ownerships for the report
    all_ownerships = ItemOwnership.query.all()

    return render_template('admin/payoff_report.html',
                           items=items,
                           owned_items=owned_items,
                           external_items=external_items,
                           users=users,
                           all_ownerships=all_ownerships,
                           misc_revenue=misc_revenue)


@admin_bp.route('/schedule')
@login_required
def schedule():
    """Rental schedule / calendar"""
    from datetime import timedelta, date
    import calendar as cal_mod

    quotes = Quote.query.filter(
        Quote.start_date.isnot(None),
        Quote.end_date.isnot(None)
    ).order_by(Quote.start_date).all()

    # Inquiries with date ranges (not yet converted to quotes)
    inquiries = Inquiry.query.filter(
        Inquiry.desired_start_date.isnot(None),
        Inquiry.desired_end_date.isnot(None),
        Inquiry.status.in_(['new', 'contacted'])
    ).order_by(Inquiry.desired_start_date).all()

    # Calendar month from query params, default to current month
    try:
        cal_year = int(request.args.get('year', date.today().year))
        cal_month = int(request.args.get('month', date.today().month))
    except (ValueError, TypeError):
        cal_year, cal_month = date.today().year, date.today().month

    # Build calendar weeks
    first_weekday, num_days = cal_mod.monthrange(cal_year, cal_month)
    # Monday=0 … Sunday=6
    month_start = date(cal_year, cal_month, 1)
    month_end = date(cal_year, cal_month, num_days)

    # Previous / next month
    if cal_month == 1:
        prev_year, prev_month = cal_year - 1, 12
    else:
        prev_year, prev_month = cal_year, cal_month - 1
    if cal_month == 12:
        next_year, next_month = cal_year + 1, 1
    else:
        next_year, next_month = cal_year, cal_month + 1

    # Build calendar events from quotes
    cal_events = []
    for q in quotes:
        cal_events.append({
            'label': q.customer_name,
            'customer': q.customer_name,
            'notes': q.notes or '',
            'start': q.start_date.date() if hasattr(q.start_date, 'date') else q.start_date,
            'end': q.end_date.date() if hasattr(q.end_date, 'date') else q.end_date,
            'status': q.status,
            'type': 'quote',
            'id': q.id,
        })
    for inq in inquiries:
        cal_events.append({
            'label': inq.customer_name,
            'customer': inq.customer_name,
            'notes': inq.message or '',
            'start': inq.desired_start_date.date() if hasattr(inq.desired_start_date, 'date') else inq.desired_start_date,
            'end': inq.desired_end_date.date() if hasattr(inq.desired_end_date, 'date') else inq.desired_end_date,
            'status': 'inquiry',
            'type': 'inquiry',
            'id': inq.id,
        })

    # Build weeks grid (list of lists of 7 day-cells)
    # Each cell: {'day': int|None, 'date': date|None, 'events': [...]}
    weeks = []
    current_week = [None] * first_weekday  # padding before 1st
    for day_num in range(1, num_days + 1):
        d = date(cal_year, cal_month, day_num)
        day_events = [e for e in cal_events if e['start'] <= d <= e['end']]
        current_week.append({'day': day_num, 'date': d, 'events': day_events})
        if len(current_week) == 7:
            weeks.append(current_week)
            current_week = []
    if current_week:
        while len(current_week) < 7:
            current_week.append(None)
        weeks.append(current_week)

    return render_template('admin/schedule.html',
                           quotes=quotes, timedelta=timedelta,
                           inquiries=inquiries,
                           cal_year=cal_year, cal_month=cal_month,
                           prev_year=prev_year, prev_month=prev_month,
                           next_year=next_year, next_month=next_month,
                           weeks=weeks, today=date.today())


# ============= PDF GENERATORS =============

def _extract_common_pdf_data(quote, site_settings):
    """Extract common data used across all PDF generators."""
    issuer_name = site_settings.business_name if site_settings and site_settings.business_name else "Ihr Unternehmen"
    address_lines = [l.strip() for l in (site_settings.address_lines or '').split('\n') if l.strip()] if site_settings else []
    contact_lines_list = [l.strip() for l in (site_settings.contact_lines or '').split('\n') if l.strip()] if site_settings else []
    bank_lines_list = [l.strip() for l in (site_settings.bank_lines or '').split('\n') if l.strip()] if site_settings else []
    recipient = [l.strip() for l in (quote.recipient_lines or '').split('\n') if l.strip()]
    # Prepend customer name above address lines
    if quote.customer_name and quote.customer_name.strip():
        customer_name = quote.customer_name.strip()
        if not recipient or recipient[0] != customer_name:
            recipient.insert(0, customer_name)
    tax_number = site_settings.tax_number if site_settings else None
    vat_id = site_settings.vat_id if site_settings else None
    tax_mode = (site_settings.tax_mode or 'kleinunternehmer') if site_settings else 'kleinunternehmer'
    tax_rate = (site_settings.tax_rate if site_settings and site_settings.tax_rate else 19.0)
    payment_terms_days = (site_settings.payment_terms_days or 14) if site_settings else 14
    quote_validity_days = (site_settings.quote_validity_days or 14) if site_settings else 14

    # Logo path
    logo_path = None
    if site_settings and site_settings.logo_filename:
        lp = os.path.join(get_upload_path(), site_settings.logo_filename)
        if os.path.exists(lp):
            logo_path = lp

    # Date strings
    start_str = quote.start_date.strftime("%d.%m.%Y") if quote.start_date else None
    end_str = quote.end_date.strftime("%d.%m.%Y") if quote.end_date else None
    rental_days = quote.calculate_rental_days()
    is_pauschale = bool(quote.rental_days_override)

    # Build a compact period label for Pauschale mode
    leistungszeitraum = None
    if start_str and end_str:
        if start_str == end_str:
            leistungszeitraum = start_str
        else:
            leistungszeitraum = f"{start_str} – {end_str}"

    return {
        'issuer_name': issuer_name,
        'issuer_address': address_lines,
        'contact_lines': contact_lines_list,
        'bank_lines': bank_lines_list,
        'recipient_lines': recipient,
        'tax_number': tax_number,
        'vat_id': vat_id,
        'tax_mode': tax_mode,
        'tax_rate': tax_rate,
        'payment_terms_days': payment_terms_days,
        'quote_validity_days': quote_validity_days,
        'logo_path': logo_path,
        'start_date_str': start_str,
        'end_date_str': end_str,
        'rental_days': rental_days,
        'is_pauschale': is_pauschale,
        'leistungszeitraum': leistungszeitraum,
    }


def _extract_positions(quote):
    """Extract positions from a quote, grouping bundle components under their package.

    Returns a list of dicts:
    - Regular item: { 'name', 'quantity', 'price_per_day', 'total', 'is_bundle': False }
    - Bundle: { 'name', 'quantity', 'price_per_day': 0, 'total', 'is_bundle': True,
                'bundle_components': [{'name', 'quantity'}] }
    """
    positions = []
    seen_package_ids = set()

    for qi in quote.quote_items:
        if qi.package_id:
            if qi.package_id in seen_package_ids:
                continue
            seen_package_ids.add(qi.package_id)
            # Gather all components for this package
            components = [q for q in quote.quote_items if q.package_id == qi.package_id]
            bundle_total = sum(c.total_price for c in components)
            bundle_qty = 1  # Packages are listed once
            # Determine package name
            pkg_name = qi.package.name if qi.package else "Paket"
            positions.append({
                'name': pkg_name,
                'quantity': bundle_qty,
                'price_per_day': 0,
                'total': bundle_total,
                'is_bundle': True,
                'bundle_components': [
                    {'name': c.display_name, 'quantity': c.quantity}
                    for c in components
                ],
            })
        else:
            positions.append({
                'name': qi.display_name,
                'quantity': qi.quantity,
                'price_per_day': qi.rental_price_per_day,
                'total': qi.total_price,
                'is_bundle': False,
            })

    return positions


def _extract_items_for_lieferschein(quote):
    """Extract items for the Lieferschein (no prices, just names and quantities)."""
    items = []
    seen_package_ids = set()

    for qi in quote.quote_items:
        if qi.package_id:
            if qi.package_id in seen_package_ids:
                continue
            seen_package_ids.add(qi.package_id)
            components = [q for q in quote.quote_items if q.package_id == qi.package_id]
            pkg_name = qi.package.name if qi.package else "Paket"
            items.append({
                'name': pkg_name,
                'quantity': 1,
                'is_bundle': True,
                'bundle_components': [
                    {'name': c.display_name, 'quantity': c.quantity}
                    for c in components
                ],
            })
        else:
            items.append({
                'name': qi.display_name,
                'quantity': qi.quantity,
                'is_bundle': False,
            })

    return items


def _send_pdf_response(pdf_bytes, filename):
    """Send a PDF response with no-cache headers."""
    response = send_file(
        BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=False,
        download_name=filename,
        max_age=0,
    )
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


# ── Angebot PDF ──

@admin_bp.route('/quotes/<int:quote_id>/angebot.pdf')
@login_required
def angebot_pdf(quote_id):
    """Generate Angebot (Quote) PDF"""
    from generators.angebot import build_angebot_pdf

    quote = Quote.query.get_or_404(quote_id)
    site_settings = SiteSettings.query.first()
    data = _extract_common_pdf_data(quote, site_settings)
    positions = _extract_positions(quote)

    pdf_bytes = build_angebot_pdf(
        issuer_name=data['issuer_name'],
        issuer_address=data['issuer_address'],
        contact_lines=data['contact_lines'],
        bank_lines=data['bank_lines'],
        tax_number=data['tax_number'],
        vat_id=data.get('vat_id'),
        tax_mode=data['tax_mode'],
        tax_rate=data['tax_rate'],
        logo_path=data['logo_path'],
        recipient_lines=data['recipient_lines'],
        reference_number=quote.reference_number or f"AN-{quote.id:04d}",
        start_date_str=data['start_date_str'],
        end_date_str=data['end_date_str'],
        rental_days=data['rental_days'],
        is_pauschale=data['is_pauschale'],
        leistungszeitraum=data.get('leistungszeitraum'),
        positions=positions,
        discount_percent=quote.discount_percent or 0,
        discount_label=quote.discount_label,
        discount_amount=quote.discount_amount,
        subtotal=quote.subtotal,
        total=quote.total,
        payment_terms_days=data['payment_terms_days'],
        quote_validity_days=data['quote_validity_days'],
        notes=quote.public_notes,
        terms_and_conditions_text=site_settings.terms_and_conditions_text if site_settings else None,
    )
    return _send_pdf_response(pdf_bytes, f"angebot_{quote.reference_number}.pdf")


# ── Rechnung PDF ──

@admin_bp.route('/quotes/<int:quote_id>/rechnung.pdf')
@login_required
def rechnung_pdf(quote_id):
    """Generate Rechnung (Invoice) PDF – ZUGFeRD/Factur-X e-invoice"""
    quote = Quote.query.get_or_404(quote_id)
    pdf_bytes = _generate_rechnung_pdf_bytes(quote, einvoice=True)
    return _send_pdf_response(pdf_bytes, f"rechnung_{quote.reference_number}.pdf")


# ── Lieferschein PDF ──

@admin_bp.route('/quotes/<int:quote_id>/lieferschein.pdf')
@login_required
def lieferschein_pdf(quote_id):
    """Generate Lieferschein (Delivery Note / Handover Protocol) PDF"""
    from generators.lieferschein import build_lieferschein_pdf

    quote = Quote.query.get_or_404(quote_id)
    site_settings = SiteSettings.query.first()
    data = _extract_common_pdf_data(quote, site_settings)
    items = _extract_items_for_lieferschein(quote)

    # Kaution from query param (optional)
    kaution = request.args.get('kaution', None, type=float)

    pdf_bytes = build_lieferschein_pdf(
        issuer_name=data['issuer_name'],
        issuer_address=data['issuer_address'],
        contact_lines=data['contact_lines'],
        bank_lines=data['bank_lines'],
        tax_number=data['tax_number'],
        vat_id=data.get('vat_id'),
        logo_path=data['logo_path'],
        recipient_lines=data['recipient_lines'],
        reference_number=quote.reference_number or f"LS-{quote.id:04d}",
        start_date_str=data['start_date_str'],
        end_date_str=data['end_date_str'],
        items=items,
        kaution=kaution,
        notes=quote.public_notes,
    )
    return _send_pdf_response(pdf_bytes, f"lieferschein_{quote.reference_number}.pdf")


# ── Legacy PDF generators (kept for backwards compatibility) ──

# ============= CUSTOMER DATABASE =============

# ── API Customer endpoints (when accounting API is configured) ──

@admin_bp.route('/api/customers/api/search')
@login_required
def api_customer_search():
    """Search customers via accounting API."""
    if not accounting.is_configured():
        return jsonify([])
    q = request.args.get('q', '').strip()
    customers = accounting.get_customers(q=q if q else None)
    return jsonify(customers)


@admin_bp.route('/api/customers/api/<int:customer_id>')
@login_required
def api_customer_get(customer_id):
    """Get a single customer from the accounting API."""
    if not accounting.is_configured():
        return jsonify({'error': 'API not configured'}), 503
    ok, data = accounting.get_customer(customer_id)
    if ok:
        return jsonify(data)
    return jsonify({'error': str(data)}), 404


@admin_bp.route('/api/customers/api/create', methods=['POST'])
@login_required
def api_customer_create():
    """Create a customer via accounting API."""
    if not accounting.is_configured():
        return jsonify({'error': 'API not configured'}), 503
    data = request.get_json()
    if not data or not data.get('name', '').strip():
        return jsonify({'error': 'Name ist erforderlich.'}), 400
    ok, result = accounting.create_customer(
        name=data['name'].strip(),
        company=data.get('company', '').strip() or None,
        address=data.get('address', '').strip() or None,
        email=data.get('email', '').strip() or None,
        phone=data.get('phone', '').strip() or None,
        notes=data.get('notes', '').strip() or None,
    )
    if ok:
        return jsonify(result), 201
    return jsonify({'error': str(result)}), 400


@admin_bp.route('/api/customers/api/<int:customer_id>/update', methods=['POST'])
@login_required
def api_customer_update(customer_id):
    """Update a customer via accounting API."""
    if not accounting.is_configured():
        return jsonify({'error': 'API not configured'}), 503
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided.'}), 400
    fields = {}
    for key in ('name', 'company', 'address', 'email', 'phone', 'notes'):
        if key in data:
            fields[key] = data[key]
    ok, result = accounting.update_customer(customer_id, **fields)
    if ok:
        return jsonify(result)
    return jsonify({'error': str(result)}), 400


@admin_bp.route('/api/customers/api/<int:customer_id>/delete', methods=['POST'])
@login_required
def api_customer_delete(customer_id):
    """Delete a customer via accounting API."""
    if not accounting.is_configured():
        return jsonify({'error': 'API not configured'}), 503
    ok, result = accounting.delete_customer(customer_id)
    if ok:
        return jsonify({'deleted': True, 'id': customer_id})
    return jsonify({'error': str(result)}), 400


# ── API Quote/Invoice actions ──

@admin_bp.route('/quotes/<int:quote_id>/create_api_quote', methods=['POST'])
@login_required
def quote_create_api_quote(quote_id):
    """Create or update the API quote for a local quote."""
    quote = Quote.query.get_or_404(quote_id)
    try:
        if quote.api_quote_id:
            ok, err = _sync_update_api_quote(quote)
            if not ok:
                flash(f'API-Angebot aktualisieren fehlgeschlagen: {err}', 'error')
            else:
                accounting.generate_quote_pdf(quote.api_quote_id)
                db.session.commit()
                flash('API-Angebot aktualisiert!', 'success')
        else:
            ok, err = _sync_create_api_quote(quote)
            if not ok:
                flash(f'API-Angebot erstellen fehlgeschlagen: {err}', 'error')
            else:
                accounting.generate_quote_pdf(quote.api_quote_id)
                db.session.commit()
                flash(f'API-Angebot {quote.api_quote_number} erstellt!', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Fehler: {str(e)}', 'error')
    return redirect(url_for('admin.quote_view', quote_id=quote_id))


@admin_bp.route('/quotes/<int:quote_id>/create_api_invoice', methods=['POST'])
@login_required
def quote_create_api_invoice(quote_id):
    """Create an API invoice from the quote's API quote."""
    quote = Quote.query.get_or_404(quote_id)
    if quote.api_invoice_id:
        flash('API-Rechnung existiert bereits.', 'info')
        return redirect(url_for('admin.quote_view', quote_id=quote_id))
    try:
        date_str = request.form.get('invoice_date', '').strip()
        if not date_str:
            date_str = (quote.finalized_at or datetime.utcnow()).strftime('%Y-%m-%d')

        # Create API quote first if not exists
        if not quote.api_quote_id:
            ok, err = _sync_create_api_quote(quote)
            if not ok:
                flash(f'API-Angebot erstellen fehlgeschlagen: {err}', 'error')
                return redirect(url_for('admin.quote_view', quote_id=quote_id))
            accounting.generate_quote_pdf(quote.api_quote_id)
            _sync_api_quote_status(quote, 'sent')
            _sync_api_quote_status(quote, 'accepted')

        ok, err = _create_api_invoice_from_quote(quote, date_str=date_str)
        if not ok:
            flash(f'API-Rechnung erstellen fehlgeschlagen: {err}', 'error')
        else:
            # Generate invoice PDF
            if quote.api_invoice_id:
                accounting.generate_invoice_pdf(quote.api_invoice_id)
                # Set invoice status to sent
                accounting.set_invoice_status(quote.api_invoice_id, 'sent')
            db.session.commit()
            flash(f'API-Rechnung {quote.api_invoice_number} erstellt!', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Fehler: {str(e)}', 'error')
    return redirect(url_for('admin.quote_view', quote_id=quote_id))


# ── API PDF proxy downloads ──

@admin_bp.route('/quotes/<int:quote_id>/api_angebot.pdf')
@login_required
def api_angebot_pdf(quote_id):
    """Download the Angebot PDF from the accounting API."""
    quote = Quote.query.get_or_404(quote_id)
    if not quote.api_quote_id:
        flash('Kein API-Angebot vorhanden.', 'error')
        return redirect(url_for('admin.quote_view', quote_id=quote_id))
    ok, result = accounting.download_quote_pdf(quote.api_quote_id)
    if ok:
        pdf_bytes, content_type, filename = result
        return _send_pdf_response(pdf_bytes, filename or f'Angebot_{quote.api_quote_number}.pdf')
    flash(f'PDF-Download fehlgeschlagen: {result}', 'error')
    return redirect(url_for('admin.quote_view', quote_id=quote_id))


@admin_bp.route('/quotes/<int:quote_id>/api_rechnung.pdf')
@login_required
def api_rechnung_pdf(quote_id):
    """Download the Rechnung PDF from the accounting API."""
    quote = Quote.query.get_or_404(quote_id)
    if not quote.api_invoice_id:
        flash('Keine API-Rechnung vorhanden.', 'error')
        return redirect(url_for('admin.quote_view', quote_id=quote_id))
    ok, result = accounting.download_invoice_pdf(quote.api_invoice_id)
    if ok:
        pdf_bytes, content_type, filename = result
        return _send_pdf_response(pdf_bytes, filename or f'Rechnung_{quote.api_invoice_number}.pdf')
    flash(f'PDF-Download fehlgeschlagen: {result}', 'error')
    return redirect(url_for('admin.quote_view', quote_id=quote_id))


# ── Local customer database (fallback when API not configured) ──

@admin_bp.route('/api/customers/search')
@login_required
def customer_search():
    """Search saved customers by name (for autocomplete)."""
    q = request.args.get('q', '').strip()
    if len(q) < 1:
        return jsonify([])

    customers = Customer.query.filter(Customer.name.ilike(f'%{q}%')).order_by(Customer.name).limit(10).all()
    return jsonify([{'name': c.name, 'recipient_lines': c.recipient_lines or ''} for c in customers])


@admin_bp.route('/api/customers/save', methods=['POST'])
@login_required
def customer_save():
    """Save or update a customer entry (identified by name)."""
    data = request.get_json()
    name = (data.get('name') or '').strip()
    recipient_lines = (data.get('recipient_lines') or '').strip()

    if not name:
        return jsonify({'error': 'Name ist erforderlich.'}), 400

    customer = Customer.query.filter(Customer.name.ilike(name)).first()
    if customer:
        customer.recipient_lines = recipient_lines
        customer.name = name  # preserve exact casing from latest save
        action = 'updated'
    else:
        customer = Customer(name=name, recipient_lines=recipient_lines)
        db.session.add(customer)
        action = 'created'

    db.session.commit()
    return jsonify({'status': 'ok', 'action': action, 'name': customer.name})


@admin_bp.route('/api/customers/delete', methods=['POST'])
@login_required
def customer_delete():
    """Delete a saved customer by name."""
    data = request.get_json()
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'Name ist erforderlich.'}), 400
    customer = Customer.query.filter(Customer.name.ilike(name)).first()
    if not customer:
        return jsonify({'error': 'Kunde nicht gefunden.'}), 404
    db.session.delete(customer)
    db.session.commit()
    return jsonify({'status': 'ok', 'name': name})
