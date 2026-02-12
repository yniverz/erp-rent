import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from models import db, Item, Quote, QuoteItem
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
    Returns -1 for unlimited items (items with total_quantity = -1).
    """
    item = Item.query.get(item_id)
    if not item:
        return 0

    if item.total_quantity == -1:
        return -1

    overlapping_quotes = Quote.query.filter(
        Quote.status.in_(['finalized', 'paid']),
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
            if quote_item.item_id == item_id and not quote_item.is_custom:
                booked_quantity += quote_item.quantity

    available = item.total_quantity - booked_quantity
    return max(0, available)


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

    business_name = settings.business_name if settings and settings.business_name else 'ERP Rent'

    # Build item list
    item_lines = []
    for inq_item in inquiry.items:
        if inq_item.price_snapshot is not None:
            item_lines.append(f"  - {inq_item.quantity}x {inq_item.item_name_snapshot} @ €{inq_item.price_snapshot:.2f}/day")
        else:
            item_lines.append(f"  - {inq_item.quantity}x {inq_item.item_name_snapshot} (price on request)")
    items_text = '\n'.join(item_lines) if item_lines else '  (no items)'

    dates_text = ''
    if inquiry.desired_start_date and inquiry.desired_end_date:
        dates_text = f"{inquiry.desired_start_date.strftime('%d.%m.%Y')} - {inquiry.desired_end_date.strftime('%d.%m.%Y')}"
    else:
        dates_text = 'Not specified'

    body = f"""New rental inquiry received!

Customer: {inquiry.customer_name}
Email: {inquiry.customer_email}
Phone: {inquiry.customer_phone or 'Not provided'}

Desired Period: {dates_text}

Items:
{items_text}

Message:
{inquiry.message or '(no message)'}

---
View this inquiry in the admin panel.
"""

    msg = MIMEMultipart()
    msg['From'] = smtp_from
    msg['To'] = recipient
    msg['Subject'] = f'[{business_name}] New Rental Inquiry from {inquiry.customer_name}'
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
