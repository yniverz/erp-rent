import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from models import db, Item, Quote, QuoteItem, PackageComponent, ThrottleEvent
from sqlalchemy import and_, or_
from datetime import datetime, timedelta

# Longest throttle window in use; older ThrottleEvent rows are deleted
THROTTLE_RETENTION = timedelta(hours=1)


def replacement_value_basis(tax_mode):
    """Replacement values are net when regularly taxed (input VAT is recoverable), otherwise gross."""
    return 'netto' if tax_mode == 'regular' else 'brutto'


def get_upload_path():
    """Get the path for uploaded files"""
    base = os.path.join(os.path.dirname(__file__), 'instance', 'uploads')
    os.makedirs(base, exist_ok=True)
    return base


def throttle_count(kind, window, ip=None, subject=None):
    """Number of `kind` events within `window`, optionally filtered by ip and/or subject."""
    q = ThrottleEvent.query.filter(ThrottleEvent.kind == kind,
                                   ThrottleEvent.created_at >= datetime.utcnow() - window)
    if ip is not None:
        q = q.filter(ThrottleEvent.ip == ip)
    if subject is not None:
        q = q.filter(ThrottleEvent.subject == subject[:80])
    return q.count()


def throttle_record(kind, ip, subject=''):
    """Record one event and prune rows past the retention window."""
    ThrottleEvent.query.filter(ThrottleEvent.created_at < datetime.utcnow() - THROTTLE_RETENTION).delete()
    db.session.add(ThrottleEvent(kind=kind, ip=ip, subject=(subject or '')[:80]))
    db.session.commit()


TOTP_PERIOD = 30
TOTP_DIGITS = 6


def totp_new_secret():
    """Random 160-bit base32 secret (what authenticator apps expect)."""
    import base64
    import secrets
    return base64.b32encode(secrets.token_bytes(20)).decode('ascii').rstrip('=')


def _totp_at(secret, step):
    import base64
    import hashlib
    import hmac
    import struct
    key = base64.b32decode(secret + '=' * (-len(secret) % 8))
    digest = hmac.new(key, struct.pack('>Q', step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = (struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** TOTP_DIGITS)
    return f'{value:0{TOTP_DIGITS}d}'


def totp_verify(secret, code, last_step=None):
    """RFC 6238 check allowing ±1 step of clock drift.
    Returns the matched time step (store it to block replays) or None."""
    import hmac
    import time
    code = ''.join(ch for ch in str(code or '') if ch.isdigit())
    if not secret or len(code) != TOTP_DIGITS:
        return None
    now = int(time.time()) // TOTP_PERIOD
    for step in (now - 1, now, now + 1):
        if last_step is not None and step <= last_step:
            continue
        if hmac.compare_digest(_totp_at(secret, step), code):
            return step
    return None


def totp_uri(secret, account, issuer):
    from urllib.parse import quote
    label = quote(f'{issuer}:{account}')
    return f'otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}&period={TOTP_PERIOD}&digits={TOTP_DIGITS}'


def header_safe(value):
    """Collapse CR/LF and other whitespace so user input can't inject e-mail headers."""
    return ' '.join(str(value or '').split())


def _booked_quantity(item_id, start_date, end_date, exclude_quote_id=None):
    """Total quantity of an item booked in overlapping quotes."""
    overlapping_quotes = Quote.query.filter(
        Quote.status == 'draft',
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

    booked = 0
    for quote in overlapping_quotes.all():
        for quote_item in quote.quote_items:
            if quote_item.is_custom or quote_item.is_heading or quote_item.is_optional:
                continue
            if quote_item.item_id == item_id:
                booked += quote_item.quantity
    return booked


def get_available_quantity(item_id, start_date, end_date, exclude_quote_id=None):
    """
    Calculate available quantity for an item during a specific date range.
    Considers overlapping quotes, subtracts out-of-service units, and includes
    supplier quantities on top of own stock.
    Returns -1 for unlimited items.
    """
    item = Item.query.get(item_id)
    if not item:
        return 0

    total = item.operational_quantity
    if total == -1:
        return -1

    booked = _booked_quantity(item_id, start_date, end_date, exclude_quote_id)
    return max(0, total - booked)


def get_own_stock_available(item_id, start_date, end_date, exclude_quote_id=None):
    """
    Available quantity from OWN stock only (no suppliers) during a date range.
    Bookings are counted against own stock first. Returns -1 for unlimited.
    """
    item = Item.query.get(item_id)
    if not item:
        return 0

    own = item.operational_stock
    if own == -1:
        return -1

    booked = _booked_quantity(item_id, start_date, end_date, exclude_quote_id)
    return max(0, own - booked)


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
    msg['Subject'] = header_safe(f'[{business_name}] Mietanfrage von {inquiry.customer_name}')
    # customer_email is validated on submit (no whitespace); header_safe is belt and braces
    msg['Reply-To'] = header_safe(inquiry.customer_email)
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


IMAGE_FORMATS = {'JPEG': 'jpg', 'PNG': 'png', 'GIF': 'gif', 'WEBP': 'webp'}


class InvalidImage(ValueError):
    pass


def save_clean_image(data, filename_stem=None):
    """Validate raster image bytes, re-encode them without EXIF/XMP/GPS metadata
    and write them to uploads/. Returns the stored filename (<uuid or stem>.<ext>).

    Orientation from EXIF is applied to the pixels first so photos stay upright."""
    import io
    import uuid
    from PIL import Image, ImageOps
    try:
        probe = Image.open(io.BytesIO(data))
        probe.verify()
        img = Image.open(io.BytesIO(data))
        fmt = img.format
    except Exception:
        raise InvalidImage('Die Datei ist kein gültiges Bild.')
    ext = IMAGE_FORMATS.get(fmt)
    if not ext:
        raise InvalidImage(f'Bildformat {fmt} wird nicht unterstützt (JPEG, PNG, GIF, WebP).')

    save_kwargs = {}
    icc = img.info.get('icc_profile')
    if icc:
        save_kwargs['icc_profile'] = icc  # colour profile only, no location data
    if fmt == 'GIF' and getattr(img, 'n_frames', 1) > 1:
        frames_out = img  # keep animation; GIF carries no EXIF
        save_kwargs.update(save_all=True, loop=img.info.get('loop', 0),
                           duration=img.info.get('duration', 100))
    else:
        frames_out = ImageOps.exif_transpose(img)
        if fmt == 'JPEG':
            if frames_out.mode not in ('RGB', 'L', 'CMYK'):
                frames_out = frames_out.convert('RGB')
            save_kwargs.update(quality=90, optimize=True)
        elif fmt == 'WEBP':
            save_kwargs.update(quality=90)

    filename = f'{filename_stem or uuid.uuid4().hex}.{ext}'
    out = io.BytesIO()
    frames_out.save(out, format=fmt, **save_kwargs)
    with open(os.path.join(get_upload_path(), filename), 'wb') as f:
        f.write(out.getvalue())
    return filename


def allowed_image_file(filename):
    """Check if a filename has an allowed image extension"""
    allowed = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in allowed


def allowed_document_file(filename):
    """Check if a filename has an allowed document/image extension"""
    allowed = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'pdf', 'doc', 'docx', 'xls', 'xlsx', 'txt', 'csv'}
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in allowed


def public_base_url():
    """External base URL of this app without trailing slash.
    PUBLIC_URL (e.g. https://verleih.example.com) wins; needed behind a
    reverse proxy so OAuth/MCP metadata advertises the https URL."""
    from flask import request
    configured = (os.getenv('PUBLIC_URL') or '').strip().rstrip('/')
    return configured or request.url_root.rstrip('/')
