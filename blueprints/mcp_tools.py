"""Tool definitions for the MCP server (see blueprints/mcp.py).

Each tool is a plain function registered with @tool(...). Arguments arrive as
keyword arguments matching the JSON schema; the return value is serialized as
JSON text. Raise ToolError for user-facing errors (rolls back the session).
Tools marked deletes=True are only listed/callable when "Löschen erlauben" is
enabled in the MCP settings.
"""
import io
import os
from datetime import datetime, date, timedelta

import requests as http_requests
from flask import g, url_for
from urllib.parse import urlparse

from blueprints import api as quote_ops
from blueprints.api import ApiError, _serialize_quote
from blueprints.admin import convert_inquiry_to_quote
from helpers import (get_available_quantity, get_package_available_quantity,
                     get_upload_path, public_base_url, replacement_value_basis,
                     save_clean_image, InvalidImage)
from models import (db, Category, Inquiry, Item, ItemAccessory, ItemSupply, ItemUnit, PackageComponent,
                    Quote, QuoteItem, SiteSettings, Supplier)

ToolError = ApiError

TOOLS = {}


class Tool:
    def __init__(self, fn, name, title, description, properties, required,
                 read_only, destructive, idempotent, deletes):
        self.fn = fn
        self.name = name
        self.title = title
        self.description = description
        self.deletes = deletes
        self.input_schema = {'type': 'object', 'properties': properties or {},
                             'additionalProperties': False}
        if required:
            self.input_schema['required'] = list(required)
        self.annotations = {
            'title': title,
            'readOnlyHint': read_only,
            'destructiveHint': destructive or deletes,
            'idempotentHint': idempotent or read_only,
            'openWorldHint': False,
        }

    def descriptor(self):
        return {'name': self.name, 'title': self.title, 'description': self.description,
                'inputSchema': self.input_schema, 'annotations': self.annotations}


def tool(name, title, description, properties=None, required=(), *,
         read_only=False, destructive=False, idempotent=False, deletes=False):
    def deco(fn):
        TOOLS[name] = Tool(fn, name, title, description, properties, required,
                           read_only, destructive, idempotent, deletes)
        return fn
    return deco


def delete_allowed():
    settings = SiteSettings.query.first()
    return bool(settings and settings.mcp_allow_delete)


# ── Schema shorthands ─────────────────────────────────────────────────

def _s(type_, description, nullable=False, **extra):
    t = [type_, 'null'] if nullable else type_
    return {'type': t, 'description': description, **extra}

ID = lambda what: _s('integer', f'ID {what}')
DATE = lambda desc, nullable=True: _s('string', f'{desc} (YYYY-MM-DD)', nullable=nullable)

ITEM_FIELD_PROPS = {
    'name': _s('string', 'Artikelname'),
    'description': _s('string', 'Beschreibung für den Shop (Klartext, Zeilenumbrüche erlaubt). null löscht.', True),
    'category_id': _s('integer', 'Hauptkategorie (siehe list_categories). null = ohne Kategorie.', True),
    'subcategory_ids': {'type': 'array', 'items': {'type': 'integer'},
                        'description': 'Zusätzliche Kategorien (ersetzt die bisherige Liste)'},
    'price_per_day': _s('number', 'Standard-Mietpreis pro Einheit und Tag in EUR', minimum=0),
    'show_price_publicly': _s('boolean', 'Preis im Shop anzeigen (false = "Preis auf Anfrage")'),
    'visible_in_shop': _s('boolean', 'Im öffentlichen Shop sichtbar'),
    'stock_quantity': _s('integer', 'Eigener Bestand (-1 = unbegrenzt). Nicht für Pakete.', minimum=-1),
    'manufacturer': _s('string', 'Hersteller', True),
    'model_name': _s('string', 'Modellbezeichnung', True),
    'weight_kg': _s('number', 'Gewicht in kg', True),
    'power_watts': _s('number', 'Leistungsaufnahme in Watt', True),
    'dimensions': _s('string', 'Maße als Freitext, z.B. "60 × 40 × 30 cm"', True),
    'storage_location': _s('string', 'Lagerort / Case (intern)', True),
    'replacement_value': _s('number', 'Gerätewert ohne Zubehör pro Einheit in EUR (intern, heutiger Wiederbeschaffungswert; '
                             'netto bei Regelbesteuerung, brutto bei Kleinunternehmer – siehe replacement_value_basis in get_item; '
                             'Zubehör wird über set_item_accessories bewertet)', True),
    'accessories_note': _s('string', 'Hinweis zum Lieferumfang (öffentlich: Shop + Lieferschein), z.B. "inkl. Safety-Seil"', True),
    'show_bundle_discount': _s('boolean', 'Nur Pakete: Paketpreis im Shop als Rabatt gegenüber Einzelpreisen zeigen'),
}

UNIT_FIELD_PROPS = {
    'serial_number': _s('string', 'Seriennummer', True),
    'status': _s('string', 'Status', enum=list(ItemUnit.STATUS_LABELS)),
    'purchase_date': DATE('Kaufdatum'),
    'last_inspection_date': DATE('Letzte Prüfung (z.B. DGUV V3)'),
    'next_inspection_date': DATE('Nächste fällige Prüfung'),
    'notes': _s('string', 'Notizen', True),
}

QUOTE_FIELD_PROPS = {
    'customer_name': _s('string', 'Kundenname'),
    'start_date': DATE('Mietbeginn'),
    'end_date': DATE('Mietende (>= Mietbeginn)'),
    'rental_days_override': _s('integer', 'Abweichende Anzahl berechneter Miettage. null = aus Datum.', True, minimum=1),
    'notes': _s('string', 'Interne Notizen (nicht auf PDFs)', True),
    'public_notes': _s('string', 'Hinweise, die auf den PDFs gedruckt werden', True),
    'prices_are_net': _s('boolean', 'Gespeicherte Preise als Netto behandeln (Standard: brutto)'),
    'discount_percent': _s('number', 'Rabatt in Prozent (0–100)', minimum=0, maximum=100),
    'discount_label': _s('string', 'Bezeichnung des Rabatts', True),
}


# ── Generic helpers ───────────────────────────────────────────────────

def _url(endpoint, **values):
    return public_base_url() + url_for(endpoint, **values)


def _iso(d):
    return d.strftime('%Y-%m-%d') if d else None


def _parse_day(value, field):
    """YYYY-MM-DD -> date; empty -> None."""
    if value in (None, ''):
        return None
    try:
        return datetime.strptime(str(value).strip(), '%Y-%m-%d').date()
    except ValueError:
        raise ToolError(f'{field}: ungültiges Datum (erwartet YYYY-MM-DD).')


def _text(value, max_len=None):
    value = (str(value).strip() if value is not None else '') or None
    if value and max_len and len(value) > max_len:
        raise ToolError(f'Text zu lang (max. {max_len} Zeichen).')
    return value


def _num(value, field, minimum=None):
    if value in (None, ''):
        return None
    try:
        num = float(str(value).replace(',', '.'))
    except ValueError:
        raise ToolError(f'{field}: keine gültige Zahl.')
    if minimum is not None and num < minimum:
        raise ToolError(f'{field}: muss >= {minimum} sein.')
    return num


def _get(model, obj_id, label):
    obj = db.session.get(model, int(obj_id)) if obj_id is not None else None
    if not obj:
        raise ToolError(f'{label} {obj_id} nicht gefunden.', 404)
    return obj


def _category_path(cat):
    return ' > '.join(c.name for c in cat.ancestors + [cat]) if cat else None


def _paginate(rows, limit, offset, default=100):
    limit = max(1, min(int(limit or default), 500))
    offset = max(0, int(offset or 0))
    return rows[offset:offset + limit]


MAX_IMAGE_BYTES = 10 * 1024 * 1024


def _download_image(image_url):
    """Download an image from a public http(s) URL into uploads/. Returns the filename."""
    from blueprints.oauth import _is_public_host
    parts = urlparse(image_url or '')
    if parts.scheme not in ('http', 'https') or not parts.hostname:
        raise ToolError('image_url muss eine http(s)-URL sein.')
    if not _is_public_host(parts.hostname):
        raise ToolError('image_url zeigt auf keine öffentliche Adresse.')
    try:
        resp = http_requests.get(image_url, timeout=15, stream=True,
                                 headers={'User-Agent': 'ERP-Rent image fetcher'})
        resp.raise_for_status()
        data = resp.raw.read(MAX_IMAGE_BYTES + 1, decode_content=True)
    except http_requests.RequestException as e:
        raise ToolError(f'Bild konnte nicht geladen werden: {e}')
    if len(data) > MAX_IMAGE_BYTES:
        raise ToolError('Bild ist größer als 10 MB.')
    try:
        return save_clean_image(data)
    except InvalidImage as e:
        raise ToolError(str(e))


def _remove_upload(filename):
    if filename:
        path = os.path.join(get_upload_path(), filename)
        if os.path.exists(path):
            os.remove(path)


# ── Serializers ───────────────────────────────────────────────────────

def _unit_dict(u, with_item=False):
    d = {
        'id': u.id,
        'asset_tag': u.asset_tag,
        'serial_number': u.serial_number,
        'status': u.status,
        'status_label': u.status_label,
        'purchase_date': _iso(u.purchase_date),
        'last_inspection_date': _iso(u.last_inspection_date),
        'next_inspection_date': _iso(u.next_inspection_date),
        'inspection_overdue': u.inspection_overdue,
        'notes': u.notes,
    }
    if with_item:
        d['item_id'] = u.item_id
        d['item_name'] = u.item.name if u.item else None
    return d


def _item_summary(item):
    desc = item.description or ''
    return {
        'id': item.id,
        'name': item.name,
        'category': _category_path(item.category),
        'price_per_day': round(item.default_rental_price_per_day or 0, 2),
        'stock_quantity': None if item.is_package else item.stock_quantity,
        'available_total': item.operational_quantity if not item.is_package else None,
        'is_package': item.is_package,
        'visible_in_shop': item.visible_in_shop,
        'manufacturer': item.manufacturer,
        'model_name': item.model_name,
        'description_preview': (desc[:100] + '…') if len(desc) > 100 else (desc or None),
        'has_image': bool(item.image_filename),
        'unit_count': len(item.units),
    }


def _accessory_dict(a):
    return {
        'id': a.id, 'name': a.name, 'quantity': a.quantity, 'per_units': a.per_units,
        'value': a.value, 'effective_value': a.effective_value(),
        'linked_item_id': a.linked_item_id, 'show_in_shop': a.show_in_shop,
    }


def _value_basis():
    """'netto' or 'brutto' – how replacement values are entered, from the tax mode in the settings."""
    settings = SiteSettings.query.first()
    return replacement_value_basis((settings.tax_mode if settings else None) or 'kleinunternehmer')


def _item_detail(item):
    today = datetime.combine(date.today(), datetime.min.time())
    bookings = []
    if not item.is_package:
        rows = db.session.query(QuoteItem, Quote).join(Quote).filter(
            QuoteItem.item_id == item.id, QuoteItem.is_custom == False,  # noqa: E712
            Quote.start_date.isnot(None), Quote.end_date.isnot(None),
            Quote.end_date >= today,
        ).order_by(Quote.start_date).all()
        bookings = [{'quote_id': q.id, 'reference_number': q.reference_number,
                     'customer_name': q.customer_name, 'start_date': _iso(q.start_date),
                     'end_date': _iso(q.end_date), 'quantity': qi.quantity,
                     'optional': bool(qi.is_optional)} for qi, q in rows]
    part_of = PackageComponent.query.filter_by(component_item_id=item.id).all()
    return {
        'id': item.id,
        'name': item.name,
        'description': item.description,
        'category': {'id': item.category.id, 'path': _category_path(item.category)} if item.category else None,
        'subcategories': [{'id': c.id, 'path': _category_path(c)} for c in item.subcategories],
        'price_per_day': round(item.default_rental_price_per_day or 0, 2),
        'show_price_publicly': item.show_price_publicly,
        'visible_in_shop': item.visible_in_shop,
        'is_package': item.is_package,
        'show_bundle_discount': item.show_bundle_discount,
        'stock_quantity': item.stock_quantity,
        'operational_quantity': item.operational_quantity,
        'manufacturer': item.manufacturer,
        'model_name': item.model_name,
        'weight_kg': item.weight_kg,
        'power_watts': item.power_watts,
        'dimensions': item.dimensions,
        'storage_location': item.storage_location,
        'replacement_value': item.replacement_value,
        'replacement_value_total': item.replacement_value_total,
        'replacement_value_basis': _value_basis(),
        'accessories': [_accessory_dict(a) for a in item.accessories],
        'accessories_note': item.accessories_note,
        'image_url': _url('public.uploaded_file', filename=item.image_filename) if item.image_filename else None,
        'shop_url': _url('public.item_detail', item_id=item.id),
        'admin_url': _url('admin.inventory_edit', item_id=item.id),
        'supplier_offers': [{'supplier_id': s.supplier_id, 'supplier_name': s.supplier.name,
                             'quantity': s.quantity, 'price_per_day': s.price_per_day,
                             'price_is_brutto': s.price_is_brutto} for s in item.supplies_sorted],
        'package_components': [{'item_id': pc.component_item_id, 'name': pc.component_item.name,
                                'quantity': pc.quantity} for pc in item.package_components],
        'part_of_packages': [{'id': pc.package_id, 'name': pc.package.name} for pc in part_of],
        'units': [_unit_dict(u) for u in item.units],
        'upcoming_bookings': bookings,
    }


def _quote_dict(quote, warnings=None):
    data = _serialize_quote(quote, include_lines=True)
    data['admin_url'] = _url('admin.quote_edit', quote_id=quote.id)
    data['preisuebersicht_pdf_url'] = _url('admin.preisuebersicht_pdf', quote_id=quote.id)
    data['lieferschein_pdf_url'] = _url('admin.lieferschein_pdf', quote_id=quote.id)
    data['wertaufstellung_pdf_url'] = _url('admin.wertaufstellung_pdf', quote_id=quote.id)
    if warnings is not None:
        data['warnings'] = warnings
    return data


def _inquiry_dict(inq, detail=False):
    d = {
        'id': inq.id,
        'customer_name': inq.customer_name,
        'customer_email': inq.customer_email,
        'customer_phone': inq.customer_phone,
        'status': inq.status,
        'desired_start_date': _iso(inq.desired_start_date),
        'desired_end_date': _iso(inq.desired_end_date),
        'created_at': inq.created_at.strftime('%Y-%m-%d %H:%M') if inq.created_at else None,
        'converted_quote_id': inq.converted_quote.id if inq.converted_quote else None,
    }
    if detail:
        d['message'] = inq.message
        d['items'] = [{'item_id': ii.item_id, 'name': ii.item_name_snapshot,
                       'quantity': ii.quantity, 'price_snapshot': ii.price_snapshot}
                      for ii in inq.items]
        d['admin_url'] = _url('admin.inquiry_view', inquiry_id=inq.id)
    return d


def _supplier_dict(s):
    return {
        'id': s.id, 'name': s.name, 'email': s.email, 'phone': s.phone, 'notes': s.notes,
        'offers': [{'item_id': o.item_id, 'item_name': o.item.name, 'quantity': o.quantity,
                    'price_per_day': o.price_per_day, 'price_is_brutto': o.price_is_brutto}
                   for o in s.supplies],
    }


# ════════════════════════════ INVENTORY ═══════════════════════════════

MISSING_FIELDS = {
    'description': lambda i: not (i.description or '').strip(),
    'image': lambda i: not i.image_filename,
    'category': lambda i: not i.category_id,
    'manufacturer': lambda i: not i.manufacturer,
    'model_name': lambda i: not i.model_name,
    'weight_kg': lambda i: i.weight_kg is None,
    'power_watts': lambda i: i.power_watts is None,
    'dimensions': lambda i: not i.dimensions,
    'replacement_value': lambda i: i.replacement_value is None,
    'accessories': lambda i: not i.is_package and not i.accessories,
    'price': lambda i: not i.default_rental_price_per_day,
}


@tool('list_items', 'Artikel auflisten',
      'Listet Inventar-Artikel (kompakt). Filter: Suchtext, Kategorie (inkl. Unterkategorien), '
      'fehlende Felder (z.B. ["description"] für Artikel ohne Beschreibung), Pakete, Shop-Sichtbarkeit. '
      'Mit start_date/end_date wird zusätzlich die Verfügbarkeit im Zeitraum berechnet.',
      {
          'query': _s('string', 'Suchbegriffe (Name, Hersteller, Modell, Kategorie; alle Wörter müssen vorkommen)'),
          'category_id': _s('integer', 'Nur Artikel dieser Kategorie inkl. Unterkategorien'),
          'missing': {'type': 'array', 'items': {'type': 'string', 'enum': list(MISSING_FIELDS)},
                      'description': 'Nur Artikel, bei denen mindestens eines dieser Felder leer ist'},
          'is_package': _s('boolean', 'true = nur Pakete, false = nur Einzelartikel'),
          'visible_in_shop': _s('boolean', 'Nach Shop-Sichtbarkeit filtern'),
          'start_date': DATE('Verfügbarkeit ab', False),
          'end_date': DATE('Verfügbarkeit bis', False),
          'limit': _s('integer', 'Max. Anzahl (Standard 100, max 500)'),
          'offset': _s('integer', 'Offset für Paginierung'),
      }, read_only=True)
def list_items(query=None, category_id=None, missing=None, is_package=None, visible_in_shop=None,
               start_date=None, end_date=None, limit=None, offset=None):
    items = Item.query.order_by(Item.name).all()
    if query:
        tokens = query.lower().split()
        def hay(i):
            return ' '.join(filter(None, [i.name, i.manufacturer, i.model_name,
                                          _category_path(i.category)])).lower()
        items = [i for i in items if all(t in hay(i) for t in tokens)]
    if category_id is not None:
        cat_ids = _get(Category, category_id, 'Kategorie').all_descendant_ids()
        items = [i for i in items if i.category_id in cat_ids
                 or any(c.id in cat_ids for c in i.subcategories)]
    if missing:
        unknown = set(missing) - set(MISSING_FIELDS)
        if unknown:
            raise ToolError(f'Unbekannte Felder in missing: {", ".join(sorted(unknown))}')
        items = [i for i in items if any(MISSING_FIELDS[f](i) for f in missing)]
    if is_package is not None:
        items = [i for i in items if bool(i.is_package) == bool(is_package)]
    if visible_in_shop is not None:
        items = [i for i in items if bool(i.visible_in_shop) == bool(visible_in_shop)]
    start, end = _parse_day(start_date, 'start_date'), _parse_day(end_date, 'end_date')
    total = len(items)
    result = []
    for i in _paginate(items, limit, offset):
        entry = _item_summary(i)
        if start and end:
            s, e = datetime.combine(start, datetime.min.time()), datetime.combine(end, datetime.min.time())
            entry['available_in_period'] = (get_package_available_quantity(i.id, s, e) if i.is_package
                                            else get_available_quantity(i.id, s, e))
        result.append(entry)
    return {'total': total, 'items': result, 'note': 'available: -1 = unbegrenzt'}


@tool('get_item', 'Artikel abrufen',
      'Alle Details eines Artikels: Beschreibung, technische Daten, Kategorien, Bild, Lieferanten-Angebote, '
      'Paketinhalt, Einheiten (Seriennummern) und kommende Buchungen.',
      {'item_id': ID('des Artikels')}, ['item_id'], read_only=True)
def get_item(item_id):
    return _item_detail(_get(Item, item_id, 'Artikel'))


def _apply_item_fields(item, data):
    if 'name' in data:
        name = _text(data['name'], 200)
        if not name:
            raise ToolError('name darf nicht leer sein.')
        item.name = name
    if 'description' in data:
        item.description = _text(data['description'])
    if 'accessories_note' in data:
        item.accessories_note = _text(data['accessories_note'])
    if 'category_id' in data:
        item.category_id = _get(Category, data['category_id'], 'Kategorie').id if data['category_id'] else None
    if 'subcategory_ids' in data:
        ids = [int(x) for x in data['subcategory_ids'] or []]
        cats = Category.query.filter(Category.id.in_(ids)).all() if ids else []
        if len(cats) != len(set(ids)):
            raise ToolError('Mindestens eine subcategory_id existiert nicht.')
        item.subcategories = cats
    if 'price_per_day' in data:
        price = _num(data['price_per_day'], 'price_per_day', 0)
        item.default_rental_price_per_day = round(price or 0, 2)
    for flag in ('show_price_publicly', 'visible_in_shop', 'show_bundle_discount'):
        if flag in data and data[flag] is not None:
            setattr(item, flag, bool(data[flag]))
    if 'stock_quantity' in data and data['stock_quantity'] is not None:
        if item.is_package:
            raise ToolError('Pakete haben keinen eigenen Bestand.')
        qty = int(data['stock_quantity'])
        if qty < -1:
            raise ToolError('stock_quantity muss >= -1 sein.')
        item.stock_quantity = qty
    for field in ('manufacturer', 'model_name', 'dimensions', 'storage_location'):
        if field in data:
            setattr(item, field, _text(data[field], 200))
    for field in ('weight_kg', 'power_watts', 'replacement_value'):
        if field in data:
            setattr(item, field, _num(data[field], field, 0))


@tool('create_item', 'Artikel anlegen',
      'Legt einen neuen Inventar-Artikel an. Für Pakete is_package=true und components angeben.',
      {**ITEM_FIELD_PROPS,
       'is_package': _s('boolean', 'Artikel ist ein Paket/Bundle aus anderen Artikeln'),
       'components': {'type': 'array', 'description': 'Nur Pakete: enthaltene Artikel',
                      'items': {'type': 'object', 'required': ['item_id', 'quantity'],
                                'properties': {'item_id': {'type': 'integer'},
                                               'quantity': {'type': 'integer', 'minimum': 1}}}},
       'image_url': _s('string', 'Optional: Bild von dieser URL herunterladen und setzen')},
      ['name', 'price_per_day'])
def create_item(is_package=False, components=None, image_url=None, **fields):
    item = Item(name='', default_rental_price_per_day=0, is_package=bool(is_package),
                stock_quantity=0, show_price_publicly=True, visible_in_shop=True,
                show_bundle_discount=False)
    _apply_item_fields(item, fields)
    db.session.add(item)
    db.session.flush()
    if is_package:
        _set_components(item, components or [])
    if image_url:
        item.image_filename = _download_image(image_url)
    db.session.commit()
    return _item_detail(item)


@tool('update_items', 'Artikel bearbeiten',
      'Ändert einen oder mehrere Artikel in einem Schritt (z.B. Beschreibungen oder technische Daten für viele '
      'Artikel ausfüllen). Nur die angegebenen Felder werden geändert; null leert ein Feld. '
      'Alles oder nichts: bei einem Fehler wird keine Änderung gespeichert.',
      {'updates': {'type': 'array', 'minItems': 1, 'maxItems': 200,
                   'description': 'Liste von Änderungen, je Artikel ein Objekt mit item_id und den zu ändernden Feldern',
                   'items': {'type': 'object', 'required': ['item_id'], 'additionalProperties': False,
                             'properties': {'item_id': ID('des Artikels'), **ITEM_FIELD_PROPS}}}},
      ['updates'], idempotent=True)
def update_items(updates):
    changed = []
    for upd in updates:
        upd = dict(upd)
        item = _get(Item, upd.pop('item_id', None), 'Artikel')
        unknown = set(upd) - set(ITEM_FIELD_PROPS)
        if unknown:
            raise ToolError(f'Artikel {item.id}: unbekannte Felder {", ".join(sorted(unknown))}')
        try:
            _apply_item_fields(item, upd)
        except ToolError as e:
            raise ToolError(f'Artikel {item.id} ({item.name}): {e.message}', e.code)
        changed.append({'id': item.id, 'name': item.name, 'fields': sorted(upd)})
    db.session.commit()
    return {'updated': len(changed), 'items': changed}


@tool('set_item_image', 'Artikelbild setzen',
      'Lädt ein Bild (JPEG/PNG/GIF/WebP, max. 10 MB) von einer öffentlichen URL herunter und setzt es als '
      'Artikelbild (ersetzt das bisherige). Ohne image_url wird das Bild entfernt.',
      {'item_id': ID('des Artikels'), 'image_url': _s('string', 'Direkte Bild-URL', True)},
      ['item_id'], idempotent=True)
def set_item_image(item_id, image_url=None):
    item = _get(Item, item_id, 'Artikel')
    new_file = _download_image(image_url) if image_url else None
    old_file = item.image_filename
    item.image_filename = new_file
    db.session.commit()
    _remove_upload(old_file)
    return {'item_id': item.id,
            'image_url': _url('public.uploaded_file', filename=new_file) if new_file else None}


def _set_components(item, components):
    PackageComponent.query.filter_by(package_id=item.id).delete()
    seen = set()
    for comp in components:
        comp_item = _get(Item, comp.get('item_id'), 'Artikel')
        qty = int(comp.get('quantity') or 0)
        if comp_item.id == item.id or comp_item.is_package:
            raise ToolError(f'{comp_item.name}: Pakete können nur Einzelartikel enthalten.')
        if qty < 1:
            raise ToolError(f'{comp_item.name}: quantity muss >= 1 sein.')
        if comp_item.id in seen:
            raise ToolError(f'{comp_item.name} ist doppelt angegeben.')
        seen.add(comp_item.id)
        db.session.add(PackageComponent(package_id=item.id, component_item_id=comp_item.id, quantity=qty))


@tool('set_package_components', 'Paketinhalt festlegen',
      'Ersetzt die enthaltenen Artikel eines Pakets/Bundles.',
      {'item_id': ID('des Pakets'),
       'components': {'type': 'array', 'minItems': 1,
                      'items': {'type': 'object', 'required': ['item_id', 'quantity'],
                                'properties': {'item_id': {'type': 'integer'},
                                               'quantity': {'type': 'integer', 'minimum': 1}}}}},
      ['item_id', 'components'], idempotent=True)
def set_package_components(item_id, components):
    item = _get(Item, item_id, 'Artikel')
    if not item.is_package:
        raise ToolError(f'{item.name} ist kein Paket.')
    _set_components(item, components)
    db.session.commit()
    return _item_detail(item)


@tool('set_item_supplier_offers', 'Lieferanten-Angebote eines Artikels festlegen',
      'Ersetzt die Liste, welche Lieferanten diesen Artikel wie oft und zu welchem Tagespreis zumieten '
      '(Einkaufspreis). Leere Liste entfernt alle Angebote.',
      {'item_id': ID('des Artikels'),
       'offers': {'type': 'array',
                  'items': {'type': 'object', 'required': ['supplier_id', 'quantity', 'price_per_day'],
                            'properties': {
                                'supplier_id': {'type': 'integer'},
                                'quantity': {'type': 'integer', 'minimum': -1, 'description': '-1 = unbegrenzt'},
                                'price_per_day': {'type': 'number', 'minimum': 0},
                                'price_is_brutto': {'type': 'boolean', 'description': 'Standard true'}}}}},
      ['item_id', 'offers'], idempotent=True)
def set_item_supplier_offers(item_id, offers):
    item = _get(Item, item_id, 'Artikel')
    if item.is_package:
        raise ToolError('Pakete haben keine Lieferanten-Angebote.')
    ItemSupply.query.filter_by(item_id=item.id).delete()
    seen = set()
    for o in offers:
        supplier = _get(Supplier, o.get('supplier_id'), 'Lieferant')
        if supplier.id in seen:
            raise ToolError(f'Lieferant {supplier.name} ist doppelt angegeben.')
        seen.add(supplier.id)
        qty = int(o.get('quantity') or 0)
        if qty < -1:
            raise ToolError('quantity muss >= -1 sein.')
        db.session.add(ItemSupply(
            item_id=item.id, supplier_id=supplier.id, quantity=qty,
            price_per_day=round(_num(o.get('price_per_day'), 'price_per_day', 0) or 0, 2),
            price_is_brutto=bool(o.get('price_is_brutto', True))))
    db.session.commit()
    db.session.refresh(item)
    return _item_detail(item)


@tool('set_item_accessories', 'Lieferumfang festlegen',
      'Ersetzt den Lieferumfang (Zubehör) eines Artikels. quantity gilt je per_units Geräte: '
      '2 Superclamps pro Lampe = {quantity: 2, per_units: 1}; 1 Case für 2 Lampen = {quantity: 1, per_units: 2}. '
      'value = Wert pro Stück (intern, für die Wertaufstellung). Optional linked_item_id verweist auf einen '
      'Inventar-Artikel (Name/Wert werden übernommen, wenn leer; Verfügbarkeit bleibt unberührt). '
      'Leere Liste entfernt den Lieferumfang.',
      {'item_id': ID('des Artikels'),
       'accessories': {'type': 'array', 'maxItems': 50,
                       'items': {'type': 'object', 'additionalProperties': False, 'properties': {
                           'name': {'type': 'string', 'description': 'Bezeichnung (optional bei linked_item_id)'},
                           'quantity': {'type': 'integer', 'minimum': 1, 'description': 'Standard 1'},
                           'per_units': {'type': 'integer', 'minimum': 1, 'description': 'Menge gilt je so viele Geräte (Standard 1)'},
                           'value': {'type': ['number', 'null'], 'minimum': 0, 'description': 'Wert pro Stück in EUR (netto/brutto wie replacement_value_basis)'},
                           'linked_item_id': {'type': ['integer', 'null'], 'description': 'Optional: verknüpfter Artikel'},
                           'show_in_shop': {'type': 'boolean', 'description': 'Im Shop anzeigen (Standard true)'}}}}},
      ['item_id', 'accessories'], idempotent=True)
def set_item_accessories(item_id, accessories):
    item = _get(Item, item_id, 'Artikel')
    if item.is_package:
        raise ToolError('Pakete haben keinen eigenen Lieferumfang (ergibt sich aus den Komponenten).')
    rows = []
    for i, acc in enumerate(accessories, start=1):
        linked = None
        if acc.get('linked_item_id'):
            linked = _get(Item, acc['linked_item_id'], 'Artikel')
            if linked.id == item.id:
                raise ToolError('Ein Artikel kann nicht sein eigenes Zubehör sein.')
        name = _text(acc.get('name'), 200) or (linked.name if linked else None)
        if not name:
            raise ToolError(f'Zeile {i}: name oder linked_item_id ist erforderlich.')
        qty, per = int(acc.get('quantity') or 1), int(acc.get('per_units') or 1)
        if qty < 1 or per < 1:
            raise ToolError(f'{name}: quantity und per_units müssen >= 1 sein.')
        value = _num(acc.get('value'), 'value', 0)
        rows.append(ItemAccessory(
            name=name, linked_item_id=linked.id if linked else None, quantity=qty, per_units=per,
            value=round(value, 2) if value is not None else None,
            show_in_shop=bool(acc.get('show_in_shop', True)), position=i))
    item.accessories = rows
    db.session.commit()
    return _item_detail(item)


@tool('delete_item', 'Artikel löschen',
      'Löscht einen Artikel inkl. Einheiten, Lieferanten-Angeboten und Angebotspositionen. Nicht umkehrbar.',
      {'item_id': ID('des Artikels')}, ['item_id'], deletes=True)
def delete_item(item_id):
    item = _get(Item, item_id, 'Artikel')
    name, image = item.name, item.image_filename
    PackageComponent.query.filter_by(component_item_id=item.id).delete()
    db.session.delete(item)
    db.session.commit()
    _remove_upload(image)
    return f'Artikel "{name}" gelöscht.'


# ═════════════════════════════ UNITS ══════════════════════════════════

@tool('list_units', 'Einheiten auflisten',
      'Listet physische Einheiten (Seriennummern/Inventarnummern). Filter: Artikel, Status, '
      'fällige Prüfungen, Suche nach Inventarnummer/Seriennummer.',
      {'item_id': _s('integer', 'Nur Einheiten dieses Artikels'),
       'status': _s('string', 'Status-Filter', enum=list(ItemUnit.STATUS_LABELS)),
       'inspection_due_within_days': _s('integer', 'Nur Einheiten, deren Prüfung innerhalb N Tagen fällig/überfällig ist'),
       'query': _s('string', 'Teil der Inventarnummer (z.B. U00012) oder Seriennummer'),
       'limit': _s('integer', 'Max. Anzahl (Standard 200)'),
       'offset': _s('integer', 'Offset')},
      read_only=True)
def list_units(item_id=None, status=None, inspection_due_within_days=None, query=None, limit=None, offset=None):
    q = ItemUnit.query
    if item_id is not None:
        q = q.filter(ItemUnit.item_id == int(item_id))
    if status:
        q = q.filter(ItemUnit.status == status)
    if inspection_due_within_days is not None:
        cutoff = date.today() + timedelta(days=int(inspection_due_within_days))
        q = q.filter(ItemUnit.next_inspection_date.isnot(None), ItemUnit.next_inspection_date <= cutoff,
                     ItemUnit.status != ItemUnit.STATUS_RETIRED)
    if query:
        like = f'%{query.strip()}%'
        q = q.filter(db.or_(ItemUnit.asset_tag.ilike(like), ItemUnit.serial_number.ilike(like)))
    units = q.order_by(ItemUnit.item_id, ItemUnit.id).all()
    return {'total': len(units), 'units': [_unit_dict(u, True) for u in _paginate(units, limit, offset, 200)]}


def _apply_unit_fields(unit, data):
    if 'serial_number' in data:
        unit.serial_number = _text(data['serial_number'], 200)
    if data.get('status') is not None:
        if data['status'] not in ItemUnit.STATUS_LABELS:
            raise ToolError(f'Ungültiger Status {data["status"]!r}.')
        unit.status = data['status']
    for field in ('purchase_date', 'last_inspection_date', 'next_inspection_date'):
        if field in data:
            setattr(unit, field, _parse_day(data[field], field))
    if 'notes' in data:
        unit.notes = _text(data['notes'])


@tool('add_units', 'Einheiten hinzufügen',
      'Fügt einem Artikel physische Einheiten hinzu (bekommen automatisch eine Inventarnummer U#####). '
      'Entweder units mit Details je Einheit angeben oder count für leere Einheiten.',
      {'item_id': ID('des Artikels'),
       'units': {'type': 'array', 'maxItems': 100,
                 'items': {'type': 'object', 'properties': UNIT_FIELD_PROPS, 'additionalProperties': False}},
       'count': _s('integer', 'Anzahl leerer Einheiten (wenn units nicht angegeben)', minimum=1, maximum=100)},
      ['item_id'])
def add_units(item_id, units=None, count=None):
    item = _get(Item, item_id, 'Artikel')
    if item.is_package:
        raise ToolError('Pakete haben keine eigenen Einheiten.')
    specs = units or [{} for _ in range(max(1, min(int(count or 1), 100)))]
    created = []
    for spec in specs:
        unit = ItemUnit(item_id=item.id, status=ItemUnit.STATUS_AVAILABLE)
        _apply_unit_fields(unit, spec)
        db.session.add(unit)
        db.session.flush()
        unit.generate_asset_tag()
        created.append(unit)
    db.session.commit()
    return {'created': [_unit_dict(u) for u in created]}


@tool('update_units', 'Einheiten bearbeiten',
      'Ändert eine oder mehrere Einheiten (Seriennummer, Status, Kauf-/Prüfdaten, Notizen). '
      'Einheit per unit_id oder asset_tag angeben. Alles oder nichts.',
      {'updates': {'type': 'array', 'minItems': 1, 'maxItems': 200,
                   'items': {'type': 'object', 'additionalProperties': False,
                             'properties': {'unit_id': _s('integer', 'ID der Einheit'),
                                            'asset_tag': _s('string', 'Alternativ: Inventarnummer, z.B. U00012'),
                                            **UNIT_FIELD_PROPS}}}},
      ['updates'], idempotent=True)
def update_units(updates):
    changed = []
    for upd in updates:
        upd = dict(upd)
        unit_id, tag = upd.pop('unit_id', None), upd.pop('asset_tag', None)
        if unit_id is not None:
            unit = _get(ItemUnit, unit_id, 'Einheit')
        elif tag:
            unit = ItemUnit.query.filter_by(asset_tag=tag.strip()).first()
            if not unit:
                raise ToolError(f'Einheit {tag} nicht gefunden.', 404)
        else:
            raise ToolError('Jede Änderung braucht unit_id oder asset_tag.')
        _apply_unit_fields(unit, upd)
        changed.append(_unit_dict(unit, True))
    db.session.commit()
    return {'updated': len(changed), 'units': changed}


@tool('delete_unit', 'Einheit löschen', 'Löscht eine Einheit. Tipp: ausgemusterte Geräte besser auf status=retired setzen.',
      {'unit_id': ID('der Einheit')}, ['unit_id'], deletes=True)
def delete_unit(unit_id):
    unit = _get(ItemUnit, unit_id, 'Einheit')
    label = unit.asset_tag or unit.id
    db.session.delete(unit)
    db.session.commit()
    return f'Einheit {label} gelöscht.'


# ═══════════════════════════ CATEGORIES ═══════════════════════════════

@tool('list_categories', 'Kategorien auflisten',
      'Kategoriebaum (flach, in Anzeigereihenfolge) mit ID, Pfad, Tiefe und Anzahl direkter Artikel.',
      read_only=True)
def list_categories():
    result = []
    for cat, depth in Category.get_tree():
        result.append({
            'id': cat.id, 'name': cat.name, 'path': _category_path(cat), 'parent_id': cat.parent_id,
            'depth': depth, 'display_order': cat.display_order, 'item_count': cat.items.count(),
            'image_url': _url('public.uploaded_file', filename=cat.image_filename) if cat.image_filename else None,
        })
    return {'categories': result}


def _apply_category_fields(cat, data):
    if 'name' in data:
        name = _text(data['name'], 100)
        if not name:
            raise ToolError('name darf nicht leer sein.')
        cat.name = name
    if 'display_order' in data and data['display_order'] is not None:
        cat.display_order = int(data['display_order'])
    if 'parent_id' in data:
        parent_id = data['parent_id']
        if parent_id:
            _get(Category, parent_id, 'Kategorie')
            if cat.id and int(parent_id) in cat.all_descendant_ids():
                raise ToolError('Kann keine Unterkategorie von sich selbst sein.')
        cat.parent_id = int(parent_id) if parent_id else None


CATEGORY_PROPS = {
    'name': _s('string', 'Name'),
    'parent_id': _s('integer', 'Übergeordnete Kategorie (null = oberste Ebene)', True),
    'display_order': _s('integer', 'Sortierung (kleiner = weiter vorne)'),
    'image_url': _s('string', 'Optional: Kategoriebild von URL laden'),
}


@tool('create_category', 'Kategorie anlegen', 'Legt eine Kategorie an (beliebig verschachtelbar).',
      CATEGORY_PROPS, ['name'])
def create_category(image_url=None, **fields):
    cat = Category(name='', display_order=0)
    _apply_category_fields(cat, fields)
    if image_url:
        cat.image_filename = _download_image(image_url)
    db.session.add(cat)
    db.session.commit()
    return {'id': cat.id, 'path': _category_path(cat)}


@tool('update_category', 'Kategorie bearbeiten', 'Ändert Name, Elternkategorie, Sortierung oder Bild einer Kategorie.',
      {'category_id': ID('der Kategorie'), **CATEGORY_PROPS}, ['category_id'], idempotent=True)
def update_category(category_id, image_url=None, **fields):
    cat = _get(Category, category_id, 'Kategorie')
    _apply_category_fields(cat, fields)
    old_image = None
    if image_url:
        old_image, cat.image_filename = cat.image_filename, _download_image(image_url)
    db.session.commit()
    _remove_upload(old_image)
    return {'id': cat.id, 'path': _category_path(cat)}


@tool('delete_category', 'Kategorie löschen',
      'Löscht eine Kategorie. Unterkategorien rücken eine Ebene nach oben, Artikel werden ohne Kategorie.',
      {'category_id': ID('der Kategorie')}, ['category_id'], deletes=True)
def delete_category(category_id):
    cat = _get(Category, category_id, 'Kategorie')
    name, image = cat.name, cat.image_filename
    for child in cat.children:
        child.parent_id = cat.parent_id
    Item.query.filter_by(category_id=cat.id).update({'category_id': None})
    db.session.delete(cat)
    db.session.commit()
    _remove_upload(image)
    return f'Kategorie "{name}" gelöscht.'


# ═══════════════════════════ SUPPLIERS ════════════════════════════════

SUPPLIER_PROPS = {
    'name': _s('string', 'Name (eindeutig)'),
    'email': _s('string', 'E-Mail', True),
    'phone': _s('string', 'Telefon', True),
    'notes': _s('string', 'Notizen', True),
}


@tool('list_suppliers', 'Lieferanten auflisten',
      'Alle Lieferanten (von denen zugemietet wird) mit ihren Artikel-Angeboten.', read_only=True)
def list_suppliers():
    return {'suppliers': [_supplier_dict(s) for s in Supplier.query.order_by(Supplier.name).all()]}


def _apply_supplier_fields(supplier, data):
    if 'name' in data:
        name = _text(data['name'], 200)
        if not name:
            raise ToolError('name darf nicht leer sein.')
        clash = Supplier.query.filter(Supplier.name == name, Supplier.id != supplier.id).first()
        if clash:
            raise ToolError(f'Lieferant "{name}" existiert bereits (ID {clash.id}).', 409)
        supplier.name = name
    for field in ('email', 'phone'):
        if field in data:
            setattr(supplier, field, _text(data[field], 200))
    if 'notes' in data:
        supplier.notes = _text(data['notes'])


@tool('create_supplier', 'Lieferant anlegen', 'Legt einen Lieferanten an.', SUPPLIER_PROPS, ['name'])
def create_supplier(**fields):
    supplier = Supplier(name='')
    _apply_supplier_fields(supplier, fields)
    db.session.add(supplier)
    db.session.commit()
    return _supplier_dict(supplier)


@tool('update_supplier', 'Lieferant bearbeiten', 'Ändert Stammdaten eines Lieferanten.',
      {'supplier_id': ID('des Lieferanten'), **SUPPLIER_PROPS}, ['supplier_id'], idempotent=True)
def update_supplier(supplier_id, **fields):
    supplier = _get(Supplier, supplier_id, 'Lieferant')
    _apply_supplier_fields(supplier, fields)
    db.session.commit()
    return _supplier_dict(supplier)


@tool('delete_supplier', 'Lieferant löschen',
      'Löscht einen Lieferanten und seine Artikel-Angebote (Angebote behalten den Namen als Historie).',
      {'supplier_id': ID('des Lieferanten')}, ['supplier_id'], deletes=True)
def delete_supplier(supplier_id):
    supplier = _get(Supplier, supplier_id, 'Lieferant')
    name = supplier.name
    db.session.delete(supplier)
    db.session.commit()
    return f'Lieferant "{name}" gelöscht.'


# ═════════════════════════════ QUOTES ═════════════════════════════════

def _actor_id():
    user = getattr(g, 'mcp_user', None)
    return user.id if user else None


@tool('list_quotes', 'Angebote auflisten', 'Angebote, neueste zuerst (ohne Positionen).',
      {'customer': _s('string', 'Teil des Kundennamens'),
       'limit': _s('integer', 'Max. Anzahl (Standard 50)'),
       'offset': _s('integer', 'Offset')}, read_only=True)
def list_quotes(customer=None, limit=None, offset=None):
    total, quotes = quote_ops.list_quotes(customer=(customer or '').strip(),
                                          limit=limit or 50, offset=offset or 0)
    return {'total': total, 'quotes': [_serialize_quote(q) for q in quotes]}


@tool('get_quote', 'Angebot abrufen', 'Vollständiges Angebot inkl. Positionen, Summen und Links (Admin, PDFs).',
      {'quote_id': ID('des Angebots')}, ['quote_id'], read_only=True)
def get_quote(quote_id):
    return _quote_dict(quote_ops.get_quote(int(quote_id)))


@tool('create_quote', 'Angebot anlegen',
      'Legt ein neues Angebot (Entwurf) an. Für Artikelpositionen müssen start_date und end_date gesetzt sein.',
      QUOTE_FIELD_PROPS, ['customer_name'])
def create_quote(**fields):
    return _quote_dict(quote_ops.create_quote(fields, created_by_id=_actor_id()))


@tool('update_quote', 'Angebot bearbeiten',
      'Ändert Kopfdaten eines Angebots (Kunde, Zeitraum, Notizen, Rabatt, Netto/Brutto).',
      {'quote_id': ID('des Angebots'), **QUOTE_FIELD_PROPS}, ['quote_id'], idempotent=True)
def update_quote(quote_id, **fields):
    return _quote_dict(quote_ops.update_quote(int(quote_id), fields))


@tool('add_quote_line', 'Angebotsposition hinzufügen',
      'Fügt eine Position hinzu. type=item: Inventar-Artikel oder Paket (Pakete werden in Einzelpositionen '
      'aufgelöst; existiert der Artikel schon, erhöht sich die Menge). type=custom: freie Position mit name/price_per_day (z.B. Lieferung; wird einmalig berechnet, nicht pro Miettag). '
      'type=heading: Zwischenüberschrift. Antwort enthält Verfügbarkeitswarnungen.',
      {'quote_id': ID('des Angebots'),
       'type': _s('string', 'Art der Position', enum=['item', 'custom', 'heading']),
       'item_id': _s('integer', 'Bei type=item: Artikel-ID'),
       'quantity': _s('integer', 'Menge (Standard 1)', minimum=1),
       'name': _s('string', 'Bei custom/heading: Text'),
       'price_per_day': _s('number', 'Optional: abweichender Preis pro Einheit und Tag (bei custom: einmaliger Preis pro Einheit)', minimum=0)},
      ['quote_id', 'type'])
def add_quote_line(quote_id, **data):
    data = {k: v for k, v in data.items() if v is not None}
    quote, warnings = quote_ops.add_line(int(quote_id), data)
    return _quote_dict(quote, warnings)


@tool('update_quote_line', 'Angebotsposition bearbeiten',
      'Ändert Menge, Preis, Einkaufskosten, Name (nur custom/heading), Rabattausnahme oder Optional-Status einer Position.',
      {'quote_id': ID('des Angebots'), 'line_id': ID('der Position'),
       'quantity': _s('integer', 'Menge', minimum=1),
       'price_per_day': _s('number', 'Preis pro Einheit und Tag (bei eigenen Positionen: einmaliger Preis pro Einheit)', minimum=0),
       'cost_per_day': _s('number', 'Einkaufskosten pro Einheit/Tag (nur ohne Lieferanten-Beschaffung)', minimum=0),
       'name': _s('string', 'Text (nur custom/heading)'),
       'discount_exempt': _s('boolean', 'Vom Rabatt ausgenommen'),
       'is_optional': _s('boolean', 'Optionale Position (nicht in Summe)'),
       'auto_sources': _s('boolean', 'Lieferanten-Beschaffung automatisch neu verteilen')},
      ['quote_id', 'line_id'], idempotent=True)
def update_quote_line(quote_id, line_id, **data):
    quote, warnings = quote_ops.update_line(int(quote_id), int(line_id), data)
    return _quote_dict(quote, warnings)


@tool('delete_quote_line', 'Angebotsposition entfernen',
      'Entfernt eine Position aus einem Angebot. whole_package=true entfernt alle Positionen desselben Pakets.',
      {'quote_id': ID('des Angebots'), 'line_id': ID('der Position'),
       'whole_package': _s('boolean', 'Ganzes Paket entfernen')},
      ['quote_id', 'line_id'], destructive=True)
def delete_quote_line(quote_id, line_id, whole_package=False):
    return _quote_dict(quote_ops.delete_line(int(quote_id), int(line_id), bool(whole_package)))


@tool('reorder_quote_lines', 'Angebotspositionen sortieren',
      'Setzt die Reihenfolge der Positionen. Paketpositionen bleiben als Block zusammen.',
      {'quote_id': ID('des Angebots'),
       'order': {'type': 'array', 'items': {'type': 'integer'}, 'description': 'Positions-IDs in neuer Reihenfolge'}},
      ['quote_id', 'order'], idempotent=True)
def reorder_quote_lines(quote_id, order):
    return _quote_dict(quote_ops.reorder_lines(int(quote_id), order))


@tool('delete_quote', 'Angebot löschen', 'Löscht ein Angebot inkl. aller Positionen.',
      {'quote_id': ID('des Angebots')}, ['quote_id'], deletes=True)
def delete_quote(quote_id):
    quote = quote_ops.get_quote(int(quote_id))
    ref = quote.reference_number
    db.session.delete(quote)
    db.session.commit()
    return f'Angebot {ref} gelöscht.'


# ════════════════════════════ INQUIRIES ═══════════════════════════════

INQUIRY_STATUSES = ['new', 'contacted', 'converted', 'closed']


@tool('list_inquiries', 'Anfragen auflisten', 'Kundenanfragen aus dem Shop, neueste zuerst.',
      {'status': _s('string', 'Status-Filter', enum=INQUIRY_STATUSES),
       'limit': _s('integer', 'Max. Anzahl (Standard 50)'),
       'offset': _s('integer', 'Offset')}, read_only=True)
def list_inquiries(status=None, limit=None, offset=None):
    q = Inquiry.query.order_by(Inquiry.created_at.desc())
    if status:
        q = q.filter(Inquiry.status == status)
    rows = q.all()
    return {'total': len(rows), 'inquiries': [_inquiry_dict(i) for i in _paginate(rows, limit, offset, 50)]}


@tool('get_inquiry', 'Anfrage abrufen', 'Details einer Kundenanfrage inkl. Nachricht und angefragter Artikel.',
      {'inquiry_id': ID('der Anfrage')}, ['inquiry_id'], read_only=True)
def get_inquiry(inquiry_id):
    return _inquiry_dict(_get(Inquiry, inquiry_id, 'Anfrage'), detail=True)


@tool('update_inquiry_status', 'Anfragestatus ändern', 'Setzt den Status einer Anfrage.',
      {'inquiry_id': ID('der Anfrage'), 'status': _s('string', 'Neuer Status', enum=INQUIRY_STATUSES)},
      ['inquiry_id', 'status'], idempotent=True)
def update_inquiry_status(inquiry_id, status):
    inquiry = _get(Inquiry, inquiry_id, 'Anfrage')
    if status not in INQUIRY_STATUSES:
        raise ToolError(f'Ungültiger Status {status!r}.')
    inquiry.status = status
    db.session.commit()
    return _inquiry_dict(inquiry)


@tool('convert_inquiry_to_quote', 'Anfrage in Angebot umwandeln',
      'Erstellt aus einer Anfrage ein Angebot (Kunde, Zeitraum, Artikel, Kontakt in Notizen) und setzt die Anfrage auf converted.',
      {'inquiry_id': ID('der Anfrage')}, ['inquiry_id'])
def convert_inquiry_to_quote_tool(inquiry_id):
    inquiry = _get(Inquiry, inquiry_id, 'Anfrage')
    if inquiry.converted_quote:
        raise ToolError(f'Anfrage wurde bereits in Angebot {inquiry.converted_quote.id} umgewandelt.', 409)
    return _quote_dict(convert_inquiry_to_quote(inquiry, created_by_id=_actor_id()))


@tool('delete_inquiry', 'Anfrage löschen', 'Löscht eine Kundenanfrage.',
      {'inquiry_id': ID('der Anfrage')}, ['inquiry_id'], deletes=True)
def delete_inquiry(inquiry_id):
    inquiry = _get(Inquiry, inquiry_id, 'Anfrage')
    db.session.delete(inquiry)
    db.session.commit()
    return f'Anfrage {inquiry_id} gelöscht.'


# ═════════════════════════ SCHEDULE / AVAILABILITY ════════════════════

@tool('get_schedule', 'Terminplan abrufen',
      'Alle Angebote und offenen Anfragen, deren Zeitraum sich mit dem angegebenen Zeitraum überschneidet.',
      {'start_date': DATE('Von', False), 'end_date': DATE('Bis', False)},
      ['start_date', 'end_date'], read_only=True)
def get_schedule(start_date, end_date):
    start, end = _parse_day(start_date, 'start_date'), _parse_day(end_date, 'end_date')
    if end < start:
        raise ToolError('end_date muss >= start_date sein.')
    s, e = datetime.combine(start, datetime.min.time()), datetime.combine(end, datetime.max.time())
    quotes = Quote.query.filter(Quote.start_date.isnot(None), Quote.end_date.isnot(None),
                                Quote.start_date <= e, Quote.end_date >= s).order_by(Quote.start_date).all()
    inquiries = Inquiry.query.filter(Inquiry.desired_start_date.isnot(None), Inquiry.desired_end_date.isnot(None),
                                     Inquiry.desired_start_date <= e, Inquiry.desired_end_date >= s,
                                     Inquiry.status.in_(['new', 'contacted'])).order_by(Inquiry.desired_start_date).all()
    return {'quotes': [_serialize_quote(q) for q in quotes],
            'open_inquiries': [_inquiry_dict(i) for i in inquiries]}


@tool('check_availability', 'Verfügbarkeit prüfen',
      'Wie viele Einheiten der Artikel im Zeitraum verfügbar sind (eigener Bestand abzüglich Defekte und '
      'Buchungen, plus Lieferanten). -1 = unbegrenzt.',
      {'item_ids': {'type': 'array', 'items': {'type': 'integer'}, 'minItems': 1, 'description': 'Artikel-IDs'},
       'start_date': DATE('Von', False), 'end_date': DATE('Bis', False),
       'exclude_quote_id': _s('integer', 'Buchungen dieses Angebots nicht mitzählen')},
      ['item_ids', 'start_date', 'end_date'], read_only=True)
def check_availability(item_ids, start_date, end_date, exclude_quote_id=None):
    start, end = _parse_day(start_date, 'start_date'), _parse_day(end_date, 'end_date')
    s, e = datetime.combine(start, datetime.min.time()), datetime.combine(end, datetime.min.time())
    result = []
    for item_id in item_ids:
        item = _get(Item, item_id, 'Artikel')
        fn = get_package_available_quantity if item.is_package else get_available_quantity
        result.append({'item_id': item.id, 'name': item.name,
                       'available': fn(item.id, s, e, exclude_quote_id)})
    return {'start_date': _iso(start), 'end_date': _iso(end), 'items': result}
