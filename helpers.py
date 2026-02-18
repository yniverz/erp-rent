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

    business_name = settings.business_name if settings and settings.business_name else 'ERP Rent'

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


# ============= DEPRECIATION (AfA) CALCULATIONS =============

def _months_between(d1, d2):
    """Return the number of months between two dates (inclusive of both months).
    d1 and d2 should be first-of-month dates. Result is >= 1 if d1 <= d2.
    """
    if d1 > d2:
        return 0
    return (d2.year - d1.year) * 12 + (d2.month - d1.month) + 1


def _first_of_month(d):
    """Return the first day of the month for a given date."""
    from datetime import date
    if hasattr(d, 'date'):
        d = d.date()
    return date(d.year, d.month, 1)


def _add_months(d, months):
    """Add months to a date, returning a date on the 1st of the resulting month."""
    from datetime import date
    total_months = d.year * 12 + (d.month - 1) + months
    year = total_months // 12
    month = total_months % 12 + 1
    return date(year, month, 1)


def calculate_depreciation_for_period(purchase_cost, purchase_date, category, period_from, period_to):
    """Calculate the depreciation amount for a given reporting period.

    Args:
        purchase_cost: Total purchase cost (brutto or netto as stored)
        purchase_date: Date of purchase (datetime or date)
        category: DepreciationCategory model instance
        period_from: Start of reporting period (datetime or date)
        period_to: End of reporting period (datetime or date)

    Returns:
        float: Depreciation amount attributable to the reporting period
    """
    from datetime import date

    if not purchase_cost or purchase_cost <= 0 or not purchase_date or not category:
        return 0.0

    # Normalize to date objects
    if hasattr(purchase_date, 'date'):
        purchase_date = purchase_date.date()
    if hasattr(period_from, 'date'):
        period_from = period_from.date()
    if hasattr(period_to, 'date'):
        period_to = period_to.date()

    if category.method == 'sofort':
        return _calc_sofort(purchase_cost, purchase_date, period_from, period_to)
    elif category.method == 'linear':
        return _calc_linear(purchase_cost, purchase_date, category.duration_months, period_from, period_to)
    elif category.method == 'degressive':
        return _calc_degressive(purchase_cost, purchase_date, category.duration_months,
                                category.degressive_rate or 25.0, period_from, period_to)
    return 0.0


def _calc_sofort(purchase_cost, purchase_date, period_from, period_to):
    """Sofortabschreibung: Full amount in the purchase month."""
    purchase_month = _first_of_month(purchase_date)
    period_first = _first_of_month(period_from)
    period_last = _first_of_month(period_to)
    if period_first <= purchase_month <= period_last:
        return round(purchase_cost, 2)
    return 0.0


def _calc_linear(purchase_cost, purchase_date, duration_months, period_from, period_to):
    """Lineare AfA: Equal monthly amounts over the duration.

    AfA starts in the purchase month and runs for duration_months months.
    For the reporting period, we calculate how many months overlap.
    """
    monthly_rate = purchase_cost / duration_months

    # AfA period: [purchase_month, purchase_month + duration_months - 1]
    afa_start = _first_of_month(purchase_date)
    afa_end = _add_months(afa_start, duration_months - 1)  # Last month of depreciation

    # Reporting period months
    period_first = _first_of_month(period_from)
    period_last = _first_of_month(period_to)

    # Overlap
    overlap_start = max(afa_start, period_first)
    overlap_end = min(afa_end, period_last)

    if overlap_start > overlap_end:
        return 0.0

    months = _months_between(overlap_start, overlap_end)
    return round(monthly_rate * months, 2)


def _calc_degressive(purchase_cost, purchase_date, duration_months, rate_pct, period_from, period_to):
    """Degressive AfA: Fixed percentage of remaining book value each year.

    - In the first year: pro-rata from purchase month to Dec (or end of duration).
    - Subsequent years: full annual rate on remaining book value.
    - Automatic switch to linear when linear becomes more favorable.
    - Duration limits the total depreciation period.
    """
    from datetime import date

    afa_start = _first_of_month(purchase_date)
    afa_end = _add_months(afa_start, duration_months - 1)

    period_first = _first_of_month(period_from)
    period_last = _first_of_month(period_to)

    if period_first > afa_end or period_last < afa_start:
        return 0.0

    rate = rate_pct / 100.0
    book_value = purchase_cost
    total_depr_in_period = 0.0

    # Process year by year from purchase date
    current_year_start = afa_start
    while book_value > 0.01 and current_year_start <= afa_end and current_year_start <= period_last:
        # Determine the year's AfA window
        if current_year_start == afa_start:
            # First year: from purchase month to end of calendar year (or afa_end)
            year_end = date(current_year_start.year, 12, 1)
        else:
            year_end = date(current_year_start.year, 12, 1)

        year_end = min(year_end, afa_end)
        year_months = _months_between(current_year_start, year_end)

        if year_months <= 0:
            break

        # Calculate remaining useful life in months from current_year_start
        remaining_months = _months_between(current_year_start, afa_end)

        # Degressive annual amount (pro-rated for partial year)
        degressive_annual = book_value * rate
        degressive_amount = degressive_annual * (year_months / 12.0)

        # Linear annual amount based on remaining book value and remaining months
        if remaining_months > 0:
            linear_monthly = book_value / remaining_months
            linear_amount = linear_monthly * year_months
        else:
            linear_amount = book_value

        # Use the HIGHER of degressive or linear (switch to linear when favorable)
        year_depr = max(degressive_amount, linear_amount)
        year_depr = min(year_depr, book_value)  # Don't exceed remaining book value

        # Calculate monthly rate for this year
        monthly_depr = year_depr / year_months if year_months > 0 else 0

        # How much of this year overlaps with the reporting period?
        overlap_start = max(current_year_start, period_first)
        overlap_end = min(year_end, period_last)

        if overlap_start <= overlap_end:
            overlap_months = _months_between(overlap_start, overlap_end)
            total_depr_in_period += monthly_depr * overlap_months

        book_value -= year_depr

        # Move to next year (Jan 1st)
        current_year_start = date(current_year_start.year + 1, 1, 1)

    return round(total_depr_in_period, 2)


def calculate_book_value_at_date(purchase_cost, purchase_date, category, at_date):
    """Calculate the remaining book value (Restwert) at a specific date.

    This is purchase_cost minus all depreciation up to and including at_date's month.
    """
    from datetime import date

    if not purchase_cost or purchase_cost <= 0 or not purchase_date or not category:
        return round(purchase_cost or 0, 2)

    if hasattr(purchase_date, 'date'):
        purchase_date = purchase_date.date()
    if hasattr(at_date, 'date'):
        at_date = at_date.date()

    # Calculate total depreciation from purchase to at_date
    total_depr = calculate_depreciation_for_period(
        purchase_cost, purchase_date, category,
        purchase_date, at_date
    )
    return round(max(0, purchase_cost - total_depr), 2)
