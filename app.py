from flask import Flask, send_file, request, Response, abort, flash, redirect, g
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect, CSRFError
from werkzeug.middleware.proxy_fix import ProxyFix
from models import db, User, SiteSettings
from helpers import replacement_value_basis
from dotenv import load_dotenv
from markupsafe import Markup, escape
import os
import io
import hashlib
import secrets
import mimetypes
import requests as http_requests
import cssmin
import rjsmin

load_dotenv()

app = Flask(__name__, static_folder=None)

# ── Minifying static file server ──────────────────────────────────────
_static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')
_static_cache = {}  # (filepath, mtime) -> (data, etag, mimetype)


def _compute_asset_version():
    """Cache-busting token for static URLs: newest mtime below static/."""
    newest = 0
    for dirpath, _, filenames in os.walk(_static_dir):
        for name in filenames:
            newest = max(newest, os.path.getmtime(os.path.join(dirpath, name)))
    return format(int(newest), 'x')


_asset_version = _compute_asset_version()

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
_secret_key = (os.getenv('SECRET_KEY') or '').strip()
if len(_secret_key) < 32 or 'change' in _secret_key.lower():  # rejects the example placeholders
    raise RuntimeError(
        'SECRET_KEY must be set to a random value of at least 32 characters, e.g. '
        'python -c "import secrets; print(secrets.token_hex(32))"'
    )
app.config['SECRET_KEY'] = _secret_key
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///erp_rent.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max upload

# Cookies: HTTPS-only by default; set COOKIE_SECURE=0 for plain-http local development
_cookie_secure = os.getenv('COOKIE_SECURE', '1').strip().lower() not in ('0', 'false', 'no')
app.config.update(
    SESSION_COOKIE_SECURE=_cookie_secure,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    REMEMBER_COOKIE_SECURE=_cookie_secure,
    REMEMBER_COOKIE_HTTPONLY=True,
    REMEMBER_COOKIE_SAMESITE='Lax',
    WTF_CSRF_TIME_LIMIT=None,  # token lives as long as the session (long-open admin tabs)
)

# Behind the TLS-terminating reverse proxy: trust one hop of X-Forwarded-* headers.
# Only safe while gunicorn is reachable by the proxy alone (see docker-compose ports).
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

csrf = CSRFProtect(app)


@app.errorhandler(CSRFError)
def handle_csrf_error(e):
    """Expired/missing CSRF token: friendly message instead of a bare 400."""
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.is_json \
            or 'application/json' in request.headers.get('Accept', ''):
        return {'ok': False, 'error': 'Sitzung abgelaufen. Bitte Seite neu laden.'}, 400
    if request.referrer and request.referrer.startswith(request.host_url):
        flash('Sitzung abgelaufen oder ungültige Anfrage. Bitte erneut versuchen.', 'error')
        return redirect(request.referrer, code=303)
    return 'Ungültige Anfrage (CSRF-Token fehlt oder ist abgelaufen).', 400


def _csp_nonce():
    """Per-request nonce for the inline <script nonce="{{ csp_nonce }}"> blocks."""
    if 'csp_nonce' not in g:
        g.csp_nonce = secrets.token_urlsafe(16)
    return g.csp_nonce


@app.context_processor
def inject_csp_nonce():
    return {'csp_nonce': _csp_nonce()}


@app.after_request
def security_headers(resp):
    h = resp.headers
    if request.is_secure:
        h.setdefault('Strict-Transport-Security', 'max-age=31536000')
    h.setdefault('X-Content-Type-Options', 'nosniff')
    h.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    h.setdefault('X-Frame-Options', 'DENY')
    h.setdefault('Permissions-Policy', 'camera=(self), microphone=(), geolocation=()')
    # OAuth consent redirects to the client's callback after the POST, which form-action also covers
    form_action = "'self' https:" if request.path == '/oauth/authorize' else "'self'"
    h.setdefault('Content-Security-Policy',
                 f"default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                 f"script-src 'self' 'nonce-{_csp_nonce()}'; object-src 'none'; base-uri 'self'; "
                 f"form-action {form_action}; frame-ancestors 'none'")
    # Never cache personal / staff pages (shared computers, back button after logout)
    path = request.path
    if path in ('/login', '/cart', '/profile') or path.startswith(('/admin', '/oauth')):
        h['Cache-Control'] = 'no-store'
    return resp

# Favicon cache (per worker; refetched when the configured URL changes)
_favicon_data = None
_favicon_mimetype = 'image/x-icon'
_favicon_source = None  # URL the cache was filled from ('' = none configured)

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

def _load_favicon(settings=None):
    """Favicon from the settings URL (FAVICON_URL env as fallback), fetched once per URL."""
    global _favicon_data, _favicon_mimetype, _favicon_source
    favicon_url = ((settings.favicon_url if settings else None) or os.getenv('FAVICON_URL', '')).strip()
    if favicon_url == _favicon_source:
        return
    _favicon_source = favicon_url
    _favicon_data = None
    _favicon_mimetype = 'image/x-icon'
    if not favicon_url:
        return
    try:
        resp = http_requests.get(favicon_url, timeout=5)
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
    _load_favicon(settings)
    show_netto = request.cookies.get('price_mode') == 'netto'
    local_mode = (settings.tax_mode or 'kleinunternehmer') if settings else 'kleinunternehmer'
    local_rate = (settings.tax_rate if settings and settings.tax_rate else 19.0)
    eff_mode, eff_rate = local_mode, local_rate
    return dict(
        site_settings=settings,
        brand_name=(settings.display_name or settings.business_name or 'Verleih') if settings else 'Verleih',
        has_favicon=_favicon_data is not None,
        asset_v=_asset_version,
        favicon_mimetype=_favicon_mimetype,
        show_netto=show_netto,
        tax_rate=eff_rate,
        effective_tax_mode=eff_mode,
        effective_tax_rate=eff_rate,
        value_basis=replacement_value_basis(eff_mode),
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
    _load_favicon(SiteSettings.query.first())
    if _favicon_data:
        return send_file(io.BytesIO(_favicon_data), mimetype=_favicon_mimetype)
    return '', 204


# Register blueprints
from blueprints.auth import auth_bp
from blueprints.public import public_bp
from blueprints.admin import admin_bp
from blueprints.api import api_bp
from blueprints.oauth import oauth_bp
from blueprints.mcp import mcp_bp

app.register_blueprint(auth_bp)
app.register_blueprint(public_bp)
app.register_blueprint(admin_bp, url_prefix='/admin')
app.register_blueprint(api_bp, url_prefix='/api/v1')
app.register_blueprint(oauth_bp)
app.register_blueprint(mcp_bp)

# Bearer-token endpoints (no cookies involved) don't take CSRF tokens
from blueprints.oauth import register as _oauth_register, token as _oauth_token, revoke as _oauth_revoke
csrf.exempt(api_bp)
csrf.exempt(mcp_bp)
for _view in (_oauth_register, _oauth_token, _oauth_revoke):
    csrf.exempt(_view)


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
    _add_column_if_missing('item', 'accessories_note', 'TEXT')
    # Single-company stock model
    _add_column_if_missing('item', 'stock_quantity', 'INTEGER NOT NULL DEFAULT 0')
    # Quote editor v2: line ordering, headings, optional positions
    _needs_position_backfill = 'position' not in {c['name'] for c in _sa_inspect(db.engine).get_columns('quote_item')}
    _add_column_if_missing('quote_item', 'position', 'INTEGER NOT NULL DEFAULT 0')
    _add_column_if_missing('quote_item', 'is_heading', 'BOOLEAN DEFAULT 0')
    _add_column_if_missing('quote_item', 'is_optional', 'BOOLEAN DEFAULT 0')
    if _needs_position_backfill:
        # Preserve existing display order (id order keeps package components together)
        db.session.execute(_sql_text('UPDATE quote_item SET position = id'))
        db.session.commit()
        print('  migrated: quote_item.position backfilled from id order')

    # ── Quote finalization removed: single quote type (Entwurf/draft) ──
    # Reset any legacy non-draft quotes to draft and drop the workflow columns.
    def _drop_column_if_exists(table, column):
        """Drop a column from an existing SQLite table if it still exists."""
        from sqlalchemy import text, inspect as sa_inspect
        insp = sa_inspect(db.engine)
        existing = {c['name'] for c in insp.get_columns(table)}
        if column in existing:
            db.session.execute(text(f'ALTER TABLE {table} DROP COLUMN {column}'))
            db.session.commit()
            print(f"  migrated: dropped {table}.{column}")

    _quote_cols = {c['name'] for c in _sa_inspect(db.engine).get_columns('quote')}
    if 'status' in _quote_cols:
        _reset = db.session.execute(
            _sql_text("UPDATE quote SET status = 'draft' WHERE status != 'draft'")
        )
        if _reset.rowcount:
            print(f"  migrated: reset {_reset.rowcount} quote(s) to 'draft'")
        db.session.commit()
    for _col in ('finalized_at', 'performed_at', 'paid_at', 'payment_method'):
        _drop_column_if_exists('quote', _col)

    # ── Customer management removed: quotes keep only the customer name ──
    _drop_column_if_exists('quote', 'recipient_lines')
    if 'customer' in _sa_inspect(db.engine).get_table_names():
        db.session.execute(_sql_text('DROP TABLE customer'))
        db.session.commit()
        print('  migrated: dropped table customer')

    # ── Invoicing moved to external tool: drop invoice-only settings ──
    _drop_column_if_exists('site_settings', 'bank_lines')
    _drop_column_if_exists('site_settings', 'payment_terms_days')
    # Extra text printed on unit labels
    _add_column_if_missing('site_settings', 'label_extra_text', 'TEXT')
    # MCP server (Claude connector)
    _add_column_if_missing('site_settings', 'mcp_enabled', 'BOOLEAN DEFAULT 0')
    _add_column_if_missing('site_settings', 'mcp_allow_delete', 'BOOLEAN DEFAULT 0')
    # Optional extra button in the shop header (e.g. back to the main website)
    _add_column_if_missing('site_settings', 'header_link_text', 'VARCHAR(100)')
    _add_column_if_missing('site_settings', 'header_link_url', 'VARCHAR(500)')
    # Favicon source moved from FAVICON_URL env to settings (env stays as fallback)
    _add_column_if_missing('site_settings', 'favicon_url', 'VARCHAR(500)')
    # Optional TOTP two-factor login
    _add_column_if_missing('user', 'totp_secret', 'VARCHAR(64)')
    _add_column_if_missing('user', 'totp_last_step', 'INTEGER')

    # ── Category names no longer unique (slug URLs disambiguate via path) ──
    # SQLite can't drop a UNIQUE constraint, so rebuild the table without it.
    _cat_indexes = db.session.execute(_sql_text("PRAGMA index_list('category')")).fetchall()
    if any(row[2] for row in _cat_indexes):  # row: (seq, name, unique, origin, partial)
        db.session.execute(_sql_text(
            'CREATE TABLE _category_new ('
            'id INTEGER NOT NULL, '
            'name VARCHAR(100) NOT NULL, '
            'display_order INTEGER, '
            'parent_id INTEGER REFERENCES category(id), '
            'image_filename VARCHAR(300), '
            'PRIMARY KEY (id))'
        ))
        db.session.execute(_sql_text(
            'INSERT INTO _category_new (id, name, display_order, parent_id, image_filename) '
            'SELECT id, name, display_order, parent_id, image_filename FROM category'
        ))
        db.session.execute(_sql_text('DROP TABLE category'))
        db.session.execute(_sql_text('ALTER TABLE _category_new RENAME TO category'))
        db.session.commit()
        print('  migrated: removed UNIQUE constraint from category.name')

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
    _load_favicon(SiteSettings.query.first())


if __name__ == '__main__':
    app.run(port=5000, debug=False)
