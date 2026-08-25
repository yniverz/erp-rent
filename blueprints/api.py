"""External REST API (v1) for quote management.

Authentication: static token(s) via `Authorization: Bearer <token>`.
Tokens are configured through the API_TOKENS env var (comma-separated;
API_TOKEN works as well). If no token is configured the API is disabled.

Only quotes in status 'draft' can be modified (same rule as the UI).
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

def _ensure_draft(quote):
    """Return an error response if the quote is not editable, else None."""
    if quote.status != 'draft':
        return _error(f"Angebot ist '{quote.status}' und kann nicht mehr bearbeitet werden.", 409)
    return None

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
    """Apply quote master data from a JSON payload. Returns error response or None."""
    if 'customer_name' in data:
        name = (data.get('customer_name') or '').strip()
        if not name:
            return _error('customer_name darf nicht leer sein.')
        quote.customer_name = name
    if 'start_date' in data or 'end_date' in data:
        start = _parse_date(data.get('start_date', quote.start_date and quote.start_date.strftime('%Y-%m-%d')), 'start_date')
        end = _parse_date(data.get('end_date', quote.end_date and quote.end_date.strftime('%Y-%m-%d')), 'end_date')
        if start and end and start > end:
            return _error('Enddatum muss nach oder gleich dem Startdatum sein.')
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
    return None


# ── Quotes ────────────────────────────────────────────────────────────

@api_bp.route('/quotes')
@require_api_token
def quotes_list():
    """List quotes. Filters: ?status=draft&customer=<substring>&limit=&offset="""
    query = Quote.query.order_by(Quote.created_at.desc())
    status = (request.args.get('status') or '').strip()
    if status:
        query = query.filter(Quote.status == status)
    customer = (request.args.get('customer') or '').strip()
    if customer:
        query = query.filter(Quote.customer_name.ilike(f'%{customer}%'))
    total = query.count()
    limit = min(request.args.get('limit', default=100, type=int) or 100, 500)
    offset = max(request.args.get('offset', default=0, type=int) or 0, 0)
    quotes = query.limit(limit).offset(offset).all()
    return jsonify({'ok': True, 'total': total,
                    'quotes': [_serialize_quote(q) for q in quotes]})


@api_bp.route('/quotes/<int:quote_id>')
@require_api_token
def quote_get(quote_id):
    quote = _get_quote_or_none(quote_id)
    if not quote:
        return _error('Angebot nicht gefunden.', 404)
    return _quote_response(quote)


@api_bp.route('/quotes', methods=['POST'])
@require_api_token
def quote_create():
    """Create a new draft quote. Required: customer_name."""
    data = request.get_json(silent=True) or {}
    if not (data.get('customer_name') or '').strip():
        return _error('customer_name ist erforderlich.')
    try:
        quote = Quote(customer_name='', status='draft')
        err = _apply_details(quote, data)
        if err:
            return err
        db.session.add(quote)
        db.session.commit()
        quote.generate_reference_number()
        db.session.commit()
        return _quote_response(quote, code=201)
    except ValueError as e:
        db.session.rollback()
        return _error(str(e))
    except Exception as e:
        db.session.rollback()
        return _error(str(e), 500)


@api_bp.route('/quotes/<int:quote_id>', methods=['PATCH'])
@require_api_token
def quote_update(quote_id):
    """Update quote details (customer, dates, notes, discount, prices_are_net)."""
    quote = _get_quote_or_none(quote_id)
    if not quote:
        return _error('Angebot nicht gefunden.', 404)
    err = _ensure_draft(quote)
    if err:
        return err
    data = request.get_json(silent=True) or {}
    try:
        err = _apply_details(quote, data)
        if err:
            db.session.rollback()
            return err
        db.session.commit()
        return _quote_response(quote)
    except ValueError as e:
        db.session.rollback()
        return _error(str(e))
    except Exception as e:
        db.session.rollback()
        return _error(str(e), 500)


# ── Quote lines ───────────────────────────────────────────────────────

@api_bp.route('/quotes/<int:quote_id>/lines', methods=['POST'])
@require_api_token
def line_add(quote_id):
    """Add a line. Payload:
    {type: 'item'|'custom'|'heading', item_id, quantity, name, price_per_day}
    - 'item': item_id required (inventory item or package; packages expand
      into component lines). Quote must have start/end dates.
    - 'custom': name required; quantity/price_per_day optional.
    - 'heading': name required.
    """
    quote = _get_quote_or_none(quote_id)
    if not quote:
        return _error('Angebot nicht gefunden.', 404)
    err = _ensure_draft(quote)
    if err:
        return err
    data = request.get_json(silent=True) or {}
    ltype = data.get('type', 'item')
    warnings = []
    try:
        pos = _next_line_position(quote)
        if ltype == 'heading':
            text = (data.get('name') or '').strip()
            if not text:
                return _error('name ist erforderlich.')
            db.session.add(QuoteItem(
                quote_id=quote.id, is_custom=True, is_heading=True,
                custom_item_name=text, quantity=0, rental_price_per_day=0, position=pos))
        elif ltype == 'custom':
            name = (data.get('name') or '').strip()
            if not name:
                return _error('name ist erforderlich.')
            db.session.add(QuoteItem(
                quote_id=quote.id, is_custom=True, custom_item_name=name,
                quantity=max(1, int(data.get('quantity') or 1)),
                rental_price_per_day=round(float(data.get('price_per_day') or 0), 2),
                position=pos))
        elif ltype == 'item':
            if not quote.start_date or not quote.end_date:
                return _error('Angebot benötigt Start- und Enddatum, bevor Artikel hinzugefügt werden können.')
            item = db.session.get(Item, int(data.get('item_id') or 0))
            if not item:
                return _error('Artikel nicht gefunden.', 404)
            qty = max(1, int(data.get('quantity') or 1))
            if item.is_package:
                if any(qi.package_id == item.id for qi in quote.quote_items):
                    return _error(f'{item.name} ist bereits im Angebot.', 409)
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
            return _error(f"Unbekannter Zeilentyp '{ltype}'.")
        db.session.commit()
        return _quote_response(quote, warnings, code=201)
    except (TypeError, ValueError) as e:
        db.session.rollback()
        return _error(f'Ungültige Eingabe: {e}')
    except Exception as e:
        db.session.rollback()
        return _error(str(e), 500)


@api_bp.route('/quotes/<int:quote_id>/lines/<int:line_id>', methods=['PATCH'])
@require_api_token
def line_update(quote_id, line_id):
    """Update a line: name (custom/heading), quantity, price_per_day,
    cost_per_day, discount_exempt, is_optional, auto_sources."""
    quote = _get_quote_or_none(quote_id)
    if not quote:
        return _error('Angebot nicht gefunden.', 404)
    err = _ensure_draft(quote)
    if err:
        return err
    qi = db.session.get(QuoteItem, line_id)
    if not qi or qi.quote_id != quote.id:
        return _error('Position nicht gefunden.', 404)
    data = request.get_json(silent=True) or {}
    warnings = []
    try:
        if 'name' in data:
            if not (qi.is_custom or qi.is_heading):
                return _error('name kann nur bei eigenen Positionen/Überschriften geändert werden.')
            new_name = (data.get('name') or '').strip()
            if not new_name:
                return _error('name darf nicht leer sein.')
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
                return _error('cost_per_day wird bei Artikeln mit Lieferanten aus der Beschaffung berechnet.')
            qi.rental_cost_per_day = max(0.0, round(float(data.get('cost_per_day') or 0), 2))

        if not qi.is_custom and not qi.is_heading and qi.item and qi.item.supplies:
            if data.get('auto_sources'):
                _apply_default_sources(qi, qi.item, quote)
            qi.recalc_cost_from_sources()

        warnings += _availability_warnings(quote, qi)
        db.session.commit()
        return _quote_response(quote, warnings)
    except (TypeError, ValueError) as e:
        db.session.rollback()
        return _error(f'Ungültige Eingabe: {e}')
    except Exception as e:
        db.session.rollback()
        return _error(str(e), 500)


@api_bp.route('/quotes/<int:quote_id>/lines/<int:line_id>', methods=['DELETE'])
@require_api_token
def line_delete(quote_id, line_id):
    """Delete a line. ?whole_package=1 deletes all lines of the same package."""
    quote = _get_quote_or_none(quote_id)
    if not quote:
        return _error('Angebot nicht gefunden.', 404)
    err = _ensure_draft(quote)
    if err:
        return err
    qi = db.session.get(QuoteItem, line_id)
    if not qi or qi.quote_id != quote.id:
        return _error('Position nicht gefunden.', 404)
    try:
        if request.args.get('whole_package') in ('1', 'true') and qi.package_id:
            for comp in [c for c in quote.quote_items if c.package_id == qi.package_id]:
                db.session.delete(comp)
        else:
            db.session.delete(qi)
        db.session.commit()
        return _quote_response(quote)
    except Exception as e:
        db.session.rollback()
        return _error(str(e), 500)


@api_bp.route('/quotes/<int:quote_id>/lines/reorder', methods=['POST'])
@require_api_token
def lines_reorder(quote_id):
    """Set manual line order. Payload: {order: [line_id, ...]}.
    Package components move as a block (any component id moves the block)."""
    quote = _get_quote_or_none(quote_id)
    if not quote:
        return _error('Angebot nicht gefunden.', 404)
    err = _ensure_draft(quote)
    if err:
        return err
    data = request.get_json(silent=True) or {}
    order = data.get('order') or []
    try:
        lines_by_id = {qi.id: qi for qi in quote.quote_items}
        pos = 1
        seen_packages = set()
        for line_id in order:
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
        db.session.commit()
        return _quote_response(quote)
    except (TypeError, ValueError) as e:
        db.session.rollback()
        return _error(f'Ungültige Eingabe: {e}')
    except Exception as e:
        db.session.rollback()
        return _error(str(e), 500)


# ── Items (lookup for adding lines) ───────────────────────────────────

@api_bp.route('/items')
@require_api_token
def items_list():
    """Item lookup. Filters: ?q=<search>. Optional ?start=&end= (YYYY-MM-DD)
    to include availability for that period."""
    q = (request.args.get('q') or '').strip().lower()
    try:
        start = _parse_date(request.args.get('start'), 'start')
        end = _parse_date(request.args.get('end'), 'end')
    except ValueError as e:
        return _error(str(e))
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
            from helpers import get_package_available_quantity
            if item.is_package:
                entry['available'] = get_package_available_quantity(item.id, start, end)
            else:
                entry['available'] = get_available_quantity(item.id, start, end)
        else:
            entry['available'] = item.operational_quantity
        results.append(entry)
    return jsonify({'ok': True, 'items': results})
