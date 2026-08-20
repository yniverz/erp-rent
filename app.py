from flask import Flask, send_file, request, Response, abort
from flask_login import LoginManager
from models import db, User, SiteSettings
from dotenv import load_dotenv
from markupsafe import Markup, escape
import os
import io
import hashlib
import mimetypes
import requests as http_requests
import cssmin
import rjsmin

load_dotenv()

app = Flask(__name__, static_folder=None)

# ── Minifying static file server ──────────────────────────────────────
_static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')
_static_cache = {}  # (filepath, mtime) -> (data, etag, mimetype)

@app.route('/static/<path:filename>', endpoint='static')
def serve_static(filename):
    filepath = os.path.realpath(os.path.join(_static_dir, filename))
    # Prevent path traversal
    if not filepath.startswith(os.path.realpath(_static_dir)):
        abort(404)
    if not os.path.isfile(filepath):
        abort(404)

    mtime = os.path.getmtime(filepath)
    cache_key = (filepath, mtime)

    if cache_key not in _static_cache:
        # Evict stale entries for the same file
        _static_cache.pop(
            next((k for k in _static_cache if k[0] == filepath), None), None
        )
        mime = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
        with open(filepath, 'rb') as f:
            data = f.read()

        if mime == 'text/css':
            try:
                data = cssmin.cssmin(data.decode('utf-8')).encode('utf-8')
            except Exception:
                pass  # serve original on error
        elif mime in ('application/javascript', 'text/javascript'):
            try:
                data = rjsmin.jsmin(data.decode('utf-8')).encode('utf-8')
            except Exception:
                pass

        etag = hashlib.md5(data).hexdigest()
        _static_cache[cache_key] = (data, etag, mime)

    data, etag, mime = _static_cache[cache_key]

    # Handle conditional requests (304 Not Modified)
    if request.headers.get('If-None-Match') == etag:
        return Response(status=304)

    resp = Response(data, mimetype=mime)
    resp.headers['ETag'] = etag
    resp.headers['Cache-Control'] = 'public, max-age=3600'
    return resp

@app.template_filter('nl2br')
def nl2br_filter(value):
    if not value:
        return value
    return Markup(escape(value).replace('\n', Markup('<br>')))
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-secret-key-change-in-production')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///erp_rent.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max upload

# Favicon cache
_favicon_data = None
_favicon_mimetype = 'image/x-icon'

# Map file extensions to MIME types for favicons
_FAVICON_EXT_MAP = {
    '.ico': 'image/x-icon',
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.gif': 'image/gif',
    '.svg': 'image/svg+xml',
    '.webp': 'image/webp',
    '.bmp': 'image/bmp',
}

def _detect_mimetype(url, content_type_header):
    """Detect favicon MIME type from Content-Type header or URL extension."""
    # Try Content-Type header first (ignore generic octet-stream)
    if content_type_header:
        ct = content_type_header.split(';')[0].strip().lower()
        if ct and ct != 'application/octet-stream' and ct.startswith('image/'):
            return ct
    # Fall back to URL extension
    from urllib.parse import urlparse
    path = urlparse(url).path.lower()
    for ext, mime in _FAVICON_EXT_MAP.items():
        if path.endswith(ext):
            return mime
    return 'image/x-icon'

def _load_favicon():
    global _favicon_data, _favicon_mimetype
    favicon_url = os.getenv('FAVICON_URL', '').strip()
    if not favicon_url:
        return
    try:
        resp = http_requests.get(favicon_url, timeout=10)
        resp.raise_for_status()
        _favicon_data = resp.content
        _favicon_mimetype = _detect_mimetype(favicon_url, resp.headers.get('Content-Type', ''))
        print(f"Favicon loaded from {favicon_url} ({len(_favicon_data)} bytes, {_favicon_mimetype})")
    except Exception as e:
        print(f"Warning: Could not load favicon from {favicon_url}: {e}")

db.init_app(app)

# Flask-Login setup
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'auth.login'
login_manager.login_message = 'Bitte melden Sie sich an, um auf diese Seite zuzugreifen.'
login_manager.login_message_category = 'error'


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# Context processor to inject settings into all templates
@app.context_processor
def inject_site_settings():
    settings = SiteSettings.query.first()
    show_netto = request.cookies.get('price_mode') == 'netto'
    local_mode = (settings.tax_mode or 'kleinunternehmer') if settings else 'kleinunternehmer'
    local_rate = (settings.tax_rate if settings and settings.tax_rate else 19.0)
    eff_mode, eff_rate = local_mode, local_rate
    return dict(
        site_settings=settings,
        brand_name=(settings.display_name or settings.business_name or 'Verleih') if settings else 'Verleih',
        has_favicon=_favicon_data is not None,
        favicon_mimetype=_favicon_mimetype,
        show_netto=show_netto,
        tax_rate=eff_rate,
        effective_tax_mode=eff_mode,
        effective_tax_rate=eff_rate,
    )


@app.template_filter('netto')
def netto_filter(value):
    """Convert brutto price to netto if price_mode cookie is set to netto.
    Uses the configured tax rate from SiteSettings."""
    if request.cookies.get('price_mode') == 'netto':
        settings = SiteSettings.query.first()
        rate = (settings.tax_rate if settings and settings.tax_rate else 19.0)
        return round(value / (1 + rate / 100), 2)
    return value


@app.route('/favicon.ico')
def favicon():
    if _favicon_data:
        return send_file(io.BytesIO(_favicon_data), mimetype=_favicon_mimetype)
    return '', 204


# Register blueprints
from blueprints.auth import auth_bp
from blueprints.public import public_bp
from blueprints.admin import admin_bp

app.register_blueprint(auth_bp)
app.register_blueprint(public_bp)
app.register_blueprint(admin_bp, url_prefix='/admin')


# Initialize database and create default admin
with app.app_context():
    # Detect whether the one-time ownership→supplier conversion is needed
    # BEFORE create_all / column migration (presence of stock_quantity = already done).
    from sqlalchemy import text as _sql_text, inspect as _sa_inspect
    _insp = _sa_inspect(db.engine)
    _needs_ownership_conversion = (
        _insp.has_table('item')
        and 'stock_quantity' not in {c['name'] for c in _insp.get_columns('item')}
        and _insp.has_table('item_ownership')
    )

    db.create_all()

    # ── Auto-migrate helper: add missing columns to existing tables ───
    # Kept for convenience when introducing new columns later.
    def _add_column_if_missing(table, column, col_type='TEXT'):
        """Add a column to an existing SQLite table if it doesn't exist yet."""
        from sqlalchemy import text, inspect as sa_inspect
        insp = sa_inspect(db.engine)
        existing = {c['name'] for c in insp.get_columns(table)}
        if column not in existing:
            db.session.execute(text(f'ALTER TABLE {table} ADD COLUMN {column} {col_type}'))
            db.session.commit()
            print(f"  migrated: {table}.{column} ({col_type})")

    # Item technical specs / logistics fields (additive, safe on live data)
    _add_column_if_missing('item', 'manufacturer', 'TEXT')
    _add_column_if_missing('item', 'model_name', 'TEXT')
    _add_column_if_missing('item', 'weight_kg', 'FLOAT')
    _add_column_if_missing('item', 'power_watts', 'FLOAT')
    _add_column_if_missing('item', 'dimensions', 'TEXT')
    _add_column_if_missing('item', 'storage_location', 'TEXT')
    _add_column_if_missing('item', 'replacement_value', 'FLOAT')
    # Single-company stock model
    _add_column_if_missing('item', 'stock_quantity', 'INTEGER NOT NULL DEFAULT 0')

    # ── One-time conversion: ItemOwnership → Item.stock_quantity + Supplier/ItemSupply ──
    if _needs_ownership_conversion:
        from models import Supplier, ItemSupply, Item
        rows = db.session.execute(_sql_text(
            'SELECT io.item_id, io.quantity, io.external_price_per_day, io.external_price_is_brutto, '
            'u.display_name, u.username '
            'FROM item_ownership io JOIN user u ON u.id = io.user_id'
        )).fetchall()

        stock = {}
        suppliers_by_name = {}
        supply_count = 0
        for r in rows:
            if r.external_price_per_day is None:
                # Internal ownership → company stock
                if r.quantity == -1:
                    stock[r.item_id] = -1
                elif stock.get(r.item_id) != -1:
                    stock[r.item_id] = stock.get(r.item_id, 0) + max(0, r.quantity)
            else:
                # External ownership → supplier + supply row
                name = (r.display_name or r.username or 'Unbekannt').strip()
                supplier = suppliers_by_name.get(name)
                if supplier is None:
                    supplier = Supplier.query.filter_by(name=name).first()
                    if supplier is None:
                        supplier = Supplier(name=name)
                        db.session.add(supplier)
                        db.session.flush()
                    suppliers_by_name[name] = supplier
                db.session.add(ItemSupply(
                    item_id=r.item_id,
                    supplier_id=supplier.id,
                    quantity=r.quantity,
                    price_per_day=r.external_price_per_day or 0,
                    price_is_brutto=bool(r.external_price_is_brutto) if r.external_price_is_brutto is not None else True,
                ))
                supply_count += 1

        for item in Item.query.all():
            item.stock_quantity = stock.get(item.id, 0)

        db.session.commit()
        print(f"  converted ownerships: {len(suppliers_by_name)} Lieferanten, "
              f"{supply_count} Angebote, Bestand für {len(stock)} Artikel übernommen")

    # Create uploads directory
    uploads_dir = os.path.join(os.path.dirname(__file__), 'instance', 'uploads')
    os.makedirs(uploads_dir, exist_ok=True)

    # Create default admin user if no users exist
    if User.query.count() == 0:
        admin_username = os.getenv('ADMIN_USERNAME', 'admin')
        admin_password = os.getenv('ADMIN_PASSWORD', 'password123')
        admin = User(
            username=admin_username,
            display_name='Administrator',
            is_admin=True
        )
        admin.set_password(admin_password)
        db.session.add(admin)
        db.session.commit()
        print(f"Created default admin user: {admin_username}")

    # Create default site settings if none exist
    if SiteSettings.query.count() == 0:
        settings = SiteSettings(business_name='Mein Verleih')
        db.session.add(settings)
        db.session.commit()
        print("Created default site settings")

    # Load favicon from URL
    _load_favicon()


if __name__ == '__main__':
    app.run(port=5000, debug=False)
