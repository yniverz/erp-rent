"""External REST API (v1) for quote management.

Authentication: static token(s) via `Authorization: Bearer <token>`.
Tokens are configured through the API_TOKENS env var (comma-separated;
API_TOKEN works as well). If no token is configured the API is disabled.

Core operations (list_quotes, create_quote, add_line, ...) raise ApiError and
are reused by the MCP server (blueprints/mcp_tools.py).
"""
import hmac
import os
from datetime import datetime
from functools import wraps

from flask import Blueprint, jsonify, request
from models import db, Item, Quote, QuoteItem
from helpers import get_available_quantity
from blueprints.admin import _apply_default_sources

api_bp = Blueprint('api', __name__)


# ── Auth ──────────────────────────────────────────────────────────────

def _configured_tokens():
    raw = os.getenv('API_TOKENS') or os.getenv('API_TOKEN') or ''
    return [t.strip() for t in raw.split(',') if t.strip()]

def require_api_token(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        tokens = _configured_tokens()
        if not tokens:
            return _error('API ist deaktiviert (kein API_TOKENS konfiguriert).', 503)
        auth = request.headers.get('Authorization', '')
        supplied = auth[7:].strip() if auth.startswith('Bearer ') else ''
        if not supplied or not any(hmac.compare_digest(supplied, t) for t in tokens):
            return _error('Ungültiger oder fehlender API-Token.', 401)
        return f(*args, **kwargs)
    return wrapper


# ── Helpers ───────────────────────────────────────────────────────────

def _error(message, code=400):
    return jsonify({'ok': False, 'error': message}), code

def _parse_date(value, field):
    """Parse YYYY-MM-DD; empty/None -> None. Raises ValueError with field name."""
    value = (value or '').strip() if isinstance(value, str) else value
    if not value:
        return None
    try:
        return datetime.strptime(value, '%Y-%m-%d')
    except (TypeError, ValueError):
        raise ValueError(f"{field}: ungültiges Datum (erwartet YYYY-MM-DD).")

def _get_quote_or_none(quote_id):
    return db.session.get(Quote, quote_id)

class ApiError(Exception):
    """Operation error with an HTTP status code. Shared by the REST API and MCP."""
    def __init__(self, message, code=400):
        super().__init__(message)
        self.message = message
        self.code = code

def _ensure_draft(quote):
    """Raise ApiError if the quote is not editable."""
    if quote.status != 'draft':
        raise ApiError(f"Angebot ist '{quote.status}' und kann nicht mehr bearbeitet werden.", 409)

def _editable_quote(quote_id):
    quote = _get_quote_or_none(quote_id)
    if not quote:
        raise ApiError('Angebot nicht gefunden.', 404)
    _ensure_draft(quote)
    return quote

def _next_line_position(quote):
    return max([qi.position or 0 for qi in quote.quote_items], default=0) + 1

def _serialize_line(qi):
    if qi.is_heading:
        return {
            'id': qi.id,
            'type': 'heading',
            'name': qi.custom_item_name or '',
            'position': qi.position or 0,
        }
    return {
        'id': qi.id,
        'type': 'custom' if qi.is_custom else 'item',
        'name': qi.display_name,
        'item_id': qi.item_id,
        'quantity': qi.quantity,
        'price_per_day': round(qi.rental_price_per_day or 0, 2),
        'cost_per_day': round(qi.rental_cost_per_day or 0, 2),
        'discount_exempt': bool(qi.discount_exempt),
        'is_optional': bool(qi.is_optional),
        'position': qi.position or 0,
        'package_id': qi.package_id,
        'package_name': qi.package.name if qi.package_id and qi.package else None,
        'total': qi.total_price,
        'assigned_units': [
            {
                'id': u.id,
                'item_unit_id': u.item_unit_id,
                'asset_tag': u.asset_tag,
                'serial_number': u.serial_number,
            }
            for u in qi.assigned_units
        ],
    }

def _serialize_quote(quote, include_lines=False):
    data = {
        'id': quote.id,
        'reference_number': quote.reference_number,
        'status': quote.status,
        'customer_name': quote.customer_name,
        'start_date': quote.start_date.strftime('%Y-%m-%d') if quote.start_date else None,
        'end_date': quote.end_date.strftime('%Y-%m-%d') if quote.end_date else None,
        'rental_days': quote.calculate_rental_days(),
        'rental_days_override': quote.rental_days_override,
        'created_at': quote.created_at.strftime('%Y-%m-%d') if quote.created_at else None,
        'prices_are_net': bool(quote.prices_are_net),
        'subtotal': quote.subtotal,
        'discount_percent': round(quote.discount_percent or 0, 4),
        'discount_label': quote.discount_label,
        'discount_amount': quote.discount_amount,
        'optional_total': quote.optional_total,
        'total': quote.total,
    }
    if include_lines:
        data.update({
            'notes': quote.notes or '',
            'public_notes': quote.public_notes or '',
            'lines': [_serialize_line(qi) for qi in quote.quote_items],
        })
    return data

def _quote_response(quote, warnings=None, code=200):
    return jsonify({'ok': True, 'quote': _serialize_quote(quote, include_lines=True),
                    'warnings': warnings or []}), code

def _availability_warnings(quote, qi):
    """Availability warning for a single (non-optional, inventory) line."""
    warnings = []
    if qi.is_custom or qi.is_heading or qi.is_optional or not qi.item:
        return warnings
    if not (quote.start_date and quote.end_date):
        return warnings
    avail = get_available_quantity(qi.item_id, quote.start_date, quote.end_date,
                                   exclude_quote_id=quote.id)
    if avail != -1 and qi.quantity > avail:
        warnings.append(f'{qi.item.name}: Nur {avail} verfügbar, {qi.quantity} eingeplant.')
    return warnings

def _apply_details(quote, data):
    """Apply quote master data from a JSON payload. Raises ApiError/ValueError."""
    if 'customer_name' in data:
        name = (data.get('customer_name') or '').strip()
        if not name:
            raise ApiError('customer_name darf nicht leer sein.')
        quote.customer_name = name
    if 'start_date' in data or 'end_date' in data:
        start = _parse_date(data.get('start_date', quote.start_date and quote.start_date.strftime('%Y-%m-%d')), 'start_date')
        end = _parse_date(data.get('end_date', quote.end_date and quote.end_date.strftime('%Y-%m-%d')), 'end_date')
        if start and end and start > end:
            raise ApiError('Enddatum muss nach oder gleich dem Startdatum sein.')
        quote.start_date = start
        quote.end_date = end
        if start and end:
            quote.rental_days = max(1, (end - start).days + 1)
    if 'rental_days_override' in data:
        override = data.get('rental_days_override')
        quote.rental_days_override = max(1, int(override)) if override else None
    if 'notes' in data:
        quote.notes = data.get('notes') or ''
    if 'public_notes' in data:
        quote.public_notes = data.get('public_notes') or ''
    if 'prices_are_net' in data:
        quote.prices_are_net = bool(data.get('prices_are_net'))
    if 'discount_percent' in data:
        quote.discount_percent = max(0.0, min(100.0, float(data.get('discount_percent') or 0)))
    if 'discount_label' in data:
        quote.discount_label = (data.get('discount_label') or '').strip() or None


def transactional(fn):
    """Commit on success, roll back on any error. Bad input becomes ApiError(400)."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            result = fn(*args, **kwargs)
            db.session.commit()
            return result
        except ApiError:
            db.session.rollback()
            raise
        except (TypeError, ValueError) as e:
            db.session.rollback()
            raise ApiError(f'Ungültige Eingabe: {e}')
        except Exception:
            db.session.rollback()
            raise
    return wrapper


# ── Operations (shared by REST views and the MCP server) ──────────────

def list_quotes(status='', customer='', limit=100, offset=0):
    """Return (total, quotes) ordered newest first."""
    query = Quote.query.order_by(Quote.created_at.desc())
    if status:
        query = query.filter(Quote.status == status)
    if customer:
        query = query.filter(Quote.customer_name.ilike(f'%{customer}%'))
    total = query.count()
    limit = min(limit or 100, 500)
    offset = max(offset or 0, 0)
    return total, query.limit(limit).offset(offset).all()


def get_quote(quote_id):
    quote = _get_quote_or_none(quote_id)
    if not quote:
        raise ApiError('Angebot nicht gefunden.', 404)
    return quote


@transactional
def create_quote(data, created_by_id=None):
    """Create a new draft quote. Required: customer_name."""
    if not (data.get('customer_name') or '').strip():
        raise ApiError('customer_name ist erforderlich.')
    quote = Quote(customer_name='', status='draft', created_by_id=created_by_id)
    _apply_details(quote, data)
    db.session.add(quote)
    db.session.flush()
    quote.generate_reference_number()
    return quote


@transactional
def update_quote(quote_id, data):
    """Update quote details (customer, dates, notes, discount, prices_are_net)."""
    quote = _editable_quote(quote_id)
    _apply_details(quote, data)
    return quote


@transactional
def add_line(quote_id, data):
    """Add a line. Returns (quote, warnings). Payload:
    {type: 'item'|'custom'|'heading', item_id, quantity, name, price_per_day}
    - 'item': item_id required (inventory item or package; packages expand
      into component lines). Quote must have start/end dates.
    - 'custom': name required; quantity/price_per_day optional.
    - 'heading': name required.
    """
    quote = _editable_quote(quote_id)
    ltype = data.get('type', 'item')
    warnings = []
    pos = _next_line_position(quote)
    if ltype == 'heading':
        text = (data.get('name') or '').strip()
        if not text:
            raise ApiError('name ist erforderlich.')
        db.session.add(QuoteItem(
            quote_id=quote.id, is_custom=True, is_heading=True,
            custom_item_name=text, quantity=0, rental_price_per_day=0, position=pos))
    elif ltype == 'custom':
        name = (data.get('name') or '').strip()
        if not name:
            raise ApiError('name ist erforderlich.')
        db.session.add(QuoteItem(
            quote_id=quote.id, is_custom=True, custom_item_name=name,
            quantity=max(1, int(data.get('quantity') or 1)),
            rental_price_per_day=round(float(data.get('price_per_day') or 0), 2),
            position=pos))
    elif ltype == 'item':
        if not quote.start_date or not quote.end_date:
            raise ApiError('Angebot benötigt Start- und Enddatum, bevor Artikel hinzugefügt werden können.')
        item = db.session.get(Item, int(data.get('item_id') or 0))
        if not item:
            raise ApiError('Artikel nicht gefunden.', 404)
        qty = max(1, int(data.get('quantity') or 1))
        if item.is_package:
            if any(qi.package_id == item.id for qi in quote.quote_items):
                raise ApiError(f'{item.name} ist bereits im Angebot.', 409)
            component_price_sum = item.component_price_sum
            for pc in item.package_components:
                if component_price_sum > 0:
                    comp_share = (pc.component_item.default_rental_price_per_day * pc.quantity) / component_price_sum
                    adjusted_price = round((item.default_rental_price_per_day * comp_share) / pc.quantity, 2)
                else:
                    adjusted_price = 0
                qi = QuoteItem(
                    quote_id=quote.id, item_id=pc.component_item_id,
                    quantity=pc.quantity * qty, rental_price_per_day=adjusted_price,
                    is_custom=False, package_id=item.id, position=pos)
                pos += 1
                db.session.add(qi)
                db.session.flush()
                _apply_default_sources(qi, pc.component_item, quote)
                warnings += _availability_warnings(quote, qi)
        else:
            existing = next((qi for qi in quote.quote_items
                             if qi.item_id == item.id and not qi.is_custom and not qi.package_id), None)
            if existing:
                existing.quantity += qty
                if item.supplies:
                    _apply_default_sources(existing, item, quote)
                qi = existing
            else:
                qi = QuoteItem(
                    quote_id=quote.id, item_id=item.id, quantity=qty,
                    rental_price_per_day=item.default_rental_price_per_day,
                    is_custom=False, position=pos)
                db.session.add(qi)
                db.session.flush()
                _apply_default_sources(qi, item, quote)
            if 'price_per_day' in data:
                qi.rental_price_per_day = max(0.0, round(float(data.get('price_per_day') or 0), 2))
            warnings += _availability_warnings(quote, qi)
    else:
        raise ApiError(f"Unbekannter Zeilentyp '{ltype}'.")
    return quote, warnings


@transactional
def update_line(quote_id, line_id, data):
    """Update a line: name (custom/heading), quantity, price_per_day,
    cost_per_day, discount_exempt, is_optional, auto_sources. Returns (quote, warnings)."""
    quote = _editable_quote(quote_id)
    qi = db.session.get(QuoteItem, line_id)
    if not qi or qi.quote_id != quote.id:
        raise ApiError('Position nicht gefunden.', 404)
    if 'name' in data:
        if not (qi.is_custom or qi.is_heading):
            raise ApiError('name kann nur bei eigenen Positionen/Überschriften geändert werden.')
        new_name = (data.get('name') or '').strip()
        if not new_name:
            raise ApiError('name darf nicht leer sein.')
        qi.custom_item_name = new_name
    if 'quantity' in data and not qi.is_heading:
        qi.quantity = max(1, int(data.get('quantity') or 1))
    if 'price_per_day' in data and not qi.is_heading:
        qi.rental_price_per_day = max(0.0, round(float(data.get('price_per_day') or 0), 2))
    if 'discount_exempt' in data:
        qi.discount_exempt = bool(data.get('discount_exempt'))
    if 'is_optional' in data and not qi.is_heading:
        qi.is_optional = bool(data.get('is_optional'))
    if 'cost_per_day' in data:
        if not qi.is_custom and qi.item and qi.item.supplies:
            raise ApiError('cost_per_day wird bei Artikeln mit Lieferanten aus der Beschaffung berechnet.')
        qi.rental_cost_per_day = max(0.0, round(float(data.get('cost_per_day') or 0), 2))

    if not qi.is_custom and not qi.is_heading and qi.item and qi.item.supplies:
        if data.get('auto_sources'):
            _apply_default_sources(qi, qi.item, quote)
        qi.recalc_cost_from_sources()

    return quote, _availability_warnings(quote, qi)


@transactional
def delete_line(quote_id, line_id, whole_package=False):
    """Delete a line. whole_package=True deletes all lines of the same package."""
    quote = _editable_quote(quote_id)
    qi = db.session.get(QuoteItem, line_id)
    if not qi or qi.quote_id != quote.id:
        raise ApiError('Position nicht gefunden.', 404)
    if whole_package and qi.package_id:
        for comp in [c for c in quote.quote_items if c.package_id == qi.package_id]:
            db.session.delete(comp)
    else:
        db.session.delete(qi)
    return quote


@transactional
def reorder_lines(quote_id, order):
    """Set manual line order from a list of line ids.
    Package components move as a block (any component id moves the block)."""
    quote = _editable_quote(quote_id)
    lines_by_id = {qi.id: qi for qi in quote.quote_items}
    pos = 1
    seen_packages = set()
    for line_id in order or []:
        qi = lines_by_id.get(int(line_id))
        if not qi:
            continue
        if qi.package_id:
            if qi.package_id in seen_packages:
                continue
            seen_packages.add(qi.package_id)
            comps = sorted([c for c in quote.quote_items if c.package_id == qi.package_id],
                           key=lambda x: (x.position or 0, x.id))
            for comp in comps:
                comp.position = pos
                pos += 1
        else:
            qi.position = pos
            pos += 1
    return quote


def lookup_items(q='', start=None, end=None):
    """Item lookup for adding lines, optionally with availability for a period."""
    from helpers import get_package_available_quantity
    q = (q or '').strip().lower()
    results = []
    for item in Item.query.order_by(Item.name).all():
        if q:
            hay = ' '.join(filter(None, [
                item.name, item.manufacturer, item.model_name,
                item.category.name if item.category else '',
            ])).lower()
            if not all(tok in hay for tok in q.split()):
                continue
        entry = {
            'id': item.id,
            'name': item.name,
            'category': item.category.name if item.category else None,
            'price_per_day': round(item.default_rental_price_per_day or 0, 2),
            'is_package': item.is_package,
            'is_external': item.is_external,
        }
        if start and end:
            if item.is_package:
                entry['available'] = get_package_available_quantity(item.id, start, end)
            else:
                entry['available'] = get_available_quantity(item.id, start, end)
        else:
            entry['available'] = item.operational_quantity
        results.append(entry)
    return results


# ── REST views ────────────────────────────────────────────────────────

def api_view(f):
    """Token auth + uniform error handling for REST views."""
    @wraps(f)
    @require_api_token
    def wrapper(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except ApiError as e:
            return _error(e.message, e.code)
        except Exception as e:
            db.session.rollback()
            return _error(str(e), 500)
    return wrapper


@api_bp.route('/quotes')
@api_view
def quotes_list():
    """List quotes. Filters: ?status=draft&customer=<substring>&limit=&offset="""
    total, quotes = list_quotes(
        status=(request.args.get('status') or '').strip(),
        customer=(request.args.get('customer') or '').strip(),
        limit=request.args.get('limit', default=100, type=int),
        offset=request.args.get('offset', default=0, type=int),
    )
    return jsonify({'ok': True, 'total': total,
                    'quotes': [_serialize_quote(q) for q in quotes]})


@api_bp.route('/quotes/<int:quote_id>')
@api_view
def quote_get(quote_id):
    return _quote_response(get_quote(quote_id))


@api_bp.route('/quotes', methods=['POST'])
@api_view
def quote_create():
    """Create a new draft quote. Required: customer_name."""
    quote = create_quote(request.get_json(silent=True) or {})
    return _quote_response(quote, code=201)


@api_bp.route('/quotes/<int:quote_id>', methods=['PATCH'])
@api_view
def quote_update(quote_id):
    """Update quote details (customer, dates, notes, discount, prices_are_net)."""
    quote = update_quote(quote_id, request.get_json(silent=True) or {})
    return _quote_response(quote)


@api_bp.route('/quotes/<int:quote_id>/lines', methods=['POST'])
@api_view
def line_add(quote_id):
    """Add a line (see add_line)."""
    quote, warnings = add_line(quote_id, request.get_json(silent=True) or {})
    return _quote_response(quote, warnings, code=201)


@api_bp.route('/quotes/<int:quote_id>/lines/<int:line_id>', methods=['PATCH'])
@api_view
def line_update(quote_id, line_id):
    """Update a line (see update_line)."""
    quote, warnings = update_line(quote_id, line_id, request.get_json(silent=True) or {})
    return _quote_response(quote, warnings)


@api_bp.route('/quotes/<int:quote_id>/lines/<int:line_id>', methods=['DELETE'])
@api_view
def line_delete(quote_id, line_id):
    """Delete a line. ?whole_package=1 deletes all lines of the same package."""
    quote = delete_line(quote_id, line_id,
                        whole_package=request.args.get('whole_package') in ('1', 'true'))
    return _quote_response(quote)


@api_bp.route('/quotes/<int:quote_id>/lines/reorder', methods=['POST'])
@api_view
def lines_reorder(quote_id):
    """Set manual line order. Payload: {order: [line_id, ...]}."""
    data = request.get_json(silent=True) or {}
    return _quote_response(reorder_lines(quote_id, data.get('order') or []))


@api_bp.route('/items')
@api_view
def items_list():
    """Item lookup. Filters: ?q=<search>. Optional ?start=&end= (YYYY-MM-DD)
    to include availability for that period."""
    try:
        start = _parse_date(request.args.get('start'), 'start')
        end = _parse_date(request.args.get('end'), 'end')
    except ValueError as e:
        return _error(str(e))
    return jsonify({'ok': True, 'items': lookup_items(request.args.get('q'), start, end)})
