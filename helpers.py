import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from models import db, Item, Quote, QuoteItem, PackageComponent, ItemOwnership
from sqlalchemy import and_, or_


def get_upload_path():
    """Get the path for uploaded files"""
    base = os.path.join(os.path.dirname(__file__), 'instance', 'uploads')
    os.makedirs(base, exist_ok=True)
    return base


def get_available_quantity(item_id, start_date, end_date, exclude_quote_id=None):
    """
    Calculate available quantity for an item during a specific date range.
    Considers overlapping quotes that are finalized or paid.
    Also accounts for items consumed by package rentals (via quote_items with package_id).
    Returns -1 for unlimited items (items with total_quantity = -1).
    """
    item = Item.query.get(item_id)
    if not item:
        return 0

    if item.total_quantity == -1:
        return -1

    overlapping_quotes = Quote.query.filter(
        Quote.status.in_(['draft', 'finalized', 'performed', 'paid']),
        Quote.start_date.isnot(None),
        Quote.end_date.isnot(None),
        or_(
            and_(Quote.start_date <= end_date, Quote.start_date >= start_date),
            and_(Quote.end_date <= end_date, Quote.end_date >= start_date),
            and_(Quote.start_date <= start_date, Quote.end_date >= end_date)
        )
    )

    if exclude_quote_id:
        overlapping_quotes = overlapping_quotes.filter(Quote.id != exclude_quote_id)

    overlapping_quotes = overlapping_quotes.all()

    booked_quantity = 0
    for quote in overlapping_quotes:
        for quote_item in quote.quote_items:
            if quote_item.is_custom:
                continue
            # Direct booking of this item
            if quote_item.item_id == item_id and not quote_item.package_id:
                booked_quantity += quote_item.quantity
            # This item booked as part of a package (expanded component)
            elif quote_item.item_id == item_id and quote_item.package_id:
                booked_quantity += quote_item.quantity

    available = item.total_quantity - booked_quantity
    return max(0, available)


def get_package_available_quantity(package_id, start_date, end_date, exclude_quote_id=None):
    """
    Calculate how many units of a package can be rented based on component availability.
    Returns min(available_component / component_qty) across all components.
    Returns -1 if all components are unlimited.
    """
    package = Item.query.get(package_id)
    if not package or not package.is_package or not package.package_components:
        return 0

    min_available = None
    for pc in package.package_components:
        comp_available = get_available_quantity(
            pc.component_item_id, start_date, end_date, exclude_quote_id
        )
        if comp_available == -1:
            continue  # unlimited component doesn't constrain
        packages_from_this = comp_available // pc.quantity
        if min_available is None or packages_from_this < min_available:
            min_available = packages_from_this

    return min_available if min_available is not None else -1


def send_inquiry_notification(inquiry, settings):
    """Send email notification about a new inquiry"""
    smtp_server = os.getenv('SMTP_SERVER')
    smtp_port = int(os.getenv('SMTP_PORT', '587'))
    smtp_user = os.getenv('SMTP_USER')
    smtp_password = os.getenv('SMTP_PASSWORD')
    smtp_from = os.getenv('SMTP_FROM', smtp_user)

    if not all([smtp_server, smtp_user, smtp_password]):
        print("SMTP not configured, skipping email notification")
        return False

    recipient = settings.notification_email if settings and settings.notification_email else smtp_user
    if not recipient:
        print("No notification email configured")
        return False

    business_name = settings.display_name or settings.business_name if settings and (settings.display_name or settings.business_name) else 'ERP Rent'

    # Build item list
    item_lines = []
    for inq_item in inquiry.items:
        if inq_item.price_snapshot is not None:
            item_lines.append(f"  - {inq_item.quantity}x {inq_item.item_name_snapshot} @ €{inq_item.price_snapshot:.2f}/Tag")
        else:
            item_lines.append(f"  - {inq_item.quantity}x {inq_item.item_name_snapshot} (Preis auf Anfrage)")
    items_text = '\n'.join(item_lines) if item_lines else '  (keine Artikel)'

    dates_text = ''
    if inquiry.desired_start_date and inquiry.desired_end_date:
        dates_text = f"{inquiry.desired_start_date.strftime('%d.%m.%Y')} - {inquiry.desired_end_date.strftime('%d.%m.%Y')}"
    else:
        dates_text = 'Nicht angegeben'

    body = f"""## Mietanfrage ##

Kunde: {inquiry.customer_name}
E-Mail: {inquiry.customer_email}
Telefon: {inquiry.customer_phone or 'Nicht angegeben'}

Gewünschter Zeitraum: {dates_text}

Artikel:
{items_text}

Nachricht:
{inquiry.message or '(keine Nachricht)'}

---

"""

    msg = MIMEMultipart()
    msg['From'] = smtp_from
    msg['To'] = recipient
    msg['Subject'] = f'[{business_name}] Mietanfrage von {inquiry.customer_name}'
    msg.attach(MIMEText(body, 'plain'))

    try:
        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_password)
            server.send_message(msg)
        print(f"Inquiry notification sent to {recipient}")
        return True
    except Exception as e:
        print(f"Failed to send inquiry notification: {e}")
        return False


def allowed_image_file(filename):
    """Check if a filename has an allowed image extension"""
    allowed = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in allowed


def allowed_document_file(filename):
    """Check if a filename has an allowed document/image extension"""
    allowed = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'pdf', 'doc', 'docx', 'xls', 'xlsx', 'txt', 'csv'}
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in allowed
