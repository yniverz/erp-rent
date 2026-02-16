from flask import Flask, send_file
from flask_login import LoginManager
from models import db, User, SiteSettings, PackageComponent, ItemOwnership, OwnershipDocument
from dotenv import load_dotenv
from markupsafe import Markup, escape
import os
import io
import requests as http_requests

load_dotenv()

app = Flask(__name__)

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
    return dict(site_settings=settings, has_favicon=_favicon_data is not None, favicon_mimetype=_favicon_mimetype)


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
    db.create_all()

    # Migrate: add new columns/tables if they don't exist (for existing databases)
    import sqlite3
    db_path = os.path.join(os.path.dirname(__file__), 'instance', 'erp_rent.db')
    if os.path.exists(db_path):
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        def column_exists(table, column):
            cursor.execute(f"PRAGMA table_info({table})")
            return any(row[1] == column for row in cursor.fetchall())

        def table_exists(name):
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,))
            return cursor.fetchone() is not None

        # QuoteItem table migrations
        if not column_exists('quote_item', 'rental_cost_per_day'):
            cursor.execute("ALTER TABLE quote_item ADD COLUMN rental_cost_per_day FLOAT DEFAULT 0")
        if not column_exists('quote_item', 'discount_exempt'):
            cursor.execute("ALTER TABLE quote_item ADD COLUMN discount_exempt BOOLEAN DEFAULT 0")

        # Quote table migrations
        if not column_exists('quote', 'rental_days_override'):
            cursor.execute("ALTER TABLE quote ADD COLUMN rental_days_override INTEGER")
        if not column_exists('quote', 'discount_label'):
            cursor.execute("ALTER TABLE quote ADD COLUMN discount_label VARCHAR(200)")

        # Customer table migration
        if not table_exists('customer'):
            cursor.execute("""
                CREATE TABLE customer (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name VARCHAR(200) NOT NULL UNIQUE,
                    recipient_lines TEXT,
                    updated_at DATETIME
                )
            """)

        # Item subcategories association table migration
        if not table_exists('item_subcategories'):
            cursor.execute("""
                CREATE TABLE item_subcategories (
                    item_id INTEGER NOT NULL,
                    category_id INTEGER NOT NULL,
                    PRIMARY KEY (item_id, category_id),
                    FOREIGN KEY (item_id) REFERENCES item(id),
                    FOREIGN KEY (category_id) REFERENCES category(id)
                )
            """)

        # Category hierarchy migration
        if not column_exists('category', 'parent_id'):
            cursor.execute("ALTER TABLE category ADD COLUMN parent_id INTEGER REFERENCES category(id)")
        if not column_exists('category', 'image_filename'):
            cursor.execute("ALTER TABLE category ADD COLUMN image_filename VARCHAR(300)")

        # Item table migrations (needed before ownership migration reads these columns)
        if not column_exists('item', 'is_external'):
            cursor.execute("ALTER TABLE item ADD COLUMN is_external BOOLEAN DEFAULT 0")
        if not column_exists('item', 'default_rental_cost_per_day'):
            cursor.execute("ALTER TABLE item ADD COLUMN default_rental_cost_per_day FLOAT DEFAULT 0")
        if not column_exists('item', 'total_cost'):
            cursor.execute("ALTER TABLE item ADD COLUMN total_cost FLOAT DEFAULT 0")

        # Package support migrations
        if not column_exists('item', 'is_package'):
            cursor.execute("ALTER TABLE item ADD COLUMN is_package BOOLEAN DEFAULT 0")

        # Bundle discount display migration
        if not column_exists('item', 'show_bundle_discount'):
            cursor.execute("ALTER TABLE item ADD COLUMN show_bundle_discount BOOLEAN DEFAULT 0")

        if not column_exists('quote_item', 'package_id'):
            cursor.execute("ALTER TABLE quote_item ADD COLUMN package_id INTEGER REFERENCES item(id)")

        # PackageComponent table migration
        if not table_exists('package_component'):
            cursor.execute("""
                CREATE TABLE package_component (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    package_id INTEGER NOT NULL,
                    component_item_id INTEGER NOT NULL,
                    quantity INTEGER NOT NULL DEFAULT 1,
                    FOREIGN KEY (package_id) REFERENCES item(id),
                    FOREIGN KEY (component_item_id) REFERENCES item(id)
                )
            """)

        # ItemOwnership table migration
        if not table_exists('item_ownership'):
            cursor.execute("""
                CREATE TABLE item_ownership (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    quantity INTEGER NOT NULL DEFAULT 0,
                    external_price_per_day FLOAT,
                    purchase_cost FLOAT DEFAULT 0,
                    FOREIGN KEY (item_id) REFERENCES item(id),
                    FOREIGN KEY (user_id) REFERENCES user(id)
                )
            """)

            # Migrate existing items: create ownership entries from old owner_id + total_quantity
            if column_exists('item', 'owner_id'):
                cursor.execute("""
                    SELECT id, owner_id, total_quantity, is_external,
                           default_rental_cost_per_day, unit_purchase_cost
                    FROM item WHERE owner_id IS NOT NULL
                """)
                for row in cursor.fetchall():
                    item_id, owner_id, total_qty, is_ext, rental_cost, purchase_cost = row
                    ext_price = rental_cost if is_ext else None
                    p_cost = 0 if is_ext else ((purchase_cost or 0) * (total_qty or 0))
                    cursor.execute("""
                        INSERT OR IGNORE INTO item_ownership
                            (item_id, user_id, quantity, external_price_per_day, purchase_cost)
                        VALUES (?, ?, ?, ?, ?)
                    """, (item_id, owner_id, total_qty or 0, ext_price, p_cost))
                print("Migrated existing items to ItemOwnership table")

        # ItemOwnership: add purchase_date column
        if table_exists('item_ownership') and not column_exists('item_ownership', 'purchase_date'):
            cursor.execute("ALTER TABLE item_ownership ADD COLUMN purchase_date DATETIME")
            # Set current date for existing rows with purchase_cost > 0
            cursor.execute("""
                UPDATE item_ownership SET purchase_date = datetime('now')
                WHERE purchase_cost > 0 AND purchase_date IS NULL
            """)
            print("Added purchase_date column to item_ownership table")

        # Drop UNIQUE(item_id, user_id) constraint to allow multiple ownership rows per user/item.
        # SQLite doesn't support DROP CONSTRAINT, so we recreate the table.
        # Detect by checking if the unique index still exists.
        cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='item_ownership'")
        create_sql = cursor.fetchone()
        if create_sql and 'UNIQUE' in (create_sql[0] or ''):
            cursor.execute("PRAGMA table_info(item_ownership)")
            cols_info = cursor.fetchall()
            col_names = [c[1] for c in cols_info]
            col_defs = []
            for c in cols_info:
                name, ctype, notnull, dflt, pk = c[1], c[2], c[3], c[4], c[5]
                parts = [name, ctype or 'TEXT']
                if pk:
                    parts.append('PRIMARY KEY')
                    if name == 'id':
                        parts.append('AUTOINCREMENT')
                if notnull and not pk:
                    parts.append('NOT NULL')
                if dflt is not None:
                    parts.append(f'DEFAULT {dflt}')
                col_defs.append(' '.join(parts))
            defs_joined = ', '.join(col_defs)
            cols_joined = ', '.join(col_names)
            cursor.execute(f"CREATE TABLE item_ownership_new ({defs_joined}, FOREIGN KEY (item_id) REFERENCES item(id), FOREIGN KEY (user_id) REFERENCES user(id))")
            cursor.execute(f"INSERT INTO item_ownership_new ({cols_joined}) SELECT {cols_joined} FROM item_ownership")
            cursor.execute("DROP TABLE item_ownership")
            cursor.execute("ALTER TABLE item_ownership_new RENAME TO item_ownership")
            print("Dropped UNIQUE(item_id, user_id) constraint from item_ownership table")

        # OwnershipDocument table
        if not table_exists('ownership_document'):
            cursor.execute("""
                CREATE TABLE ownership_document (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ownership_id INTEGER NOT NULL,
                    filename VARCHAR(300) NOT NULL,
                    original_name VARCHAR(300) NOT NULL,
                    uploaded_at DATETIME,
                    FOREIGN KEY (ownership_id) REFERENCES item_ownership(id)
                )
            """)
            print("Created ownership_document table")

        # Drop legacy columns from item table that moved to item_ownership.
        # SQLite doesn't support DROP COLUMN on older versions, so we recreate.
        if column_exists('item', 'owner_id'):
            # Read current column info to build the new schema dynamically
            cursor.execute("PRAGMA table_info(item)")
            columns_info = cursor.fetchall()
            drop_cols = {'owner_id', 'total_quantity', 'is_external',
                         'default_rental_cost_per_day', 'unit_purchase_cost'}
            keep_cols = [c for c in columns_info if c[1] not in drop_cols]
            col_names = [c[1] for c in keep_cols]

            # Build column definitions for the new table
            type_map = {c[1]: c for c in keep_cols}
            col_defs = []
            for c in keep_cols:
                name, ctype, notnull, dflt, pk = c[1], c[2], c[3], c[4], c[5]
                parts = [name, ctype or 'TEXT']
                if pk:
                    parts.append('PRIMARY KEY')
                if notnull and not pk:
                    parts.append('NOT NULL')
                if dflt is not None:
                    parts.append(f'DEFAULT {dflt}')
                col_defs.append(' '.join(parts))

            cols_joined = ', '.join(col_names)
            defs_joined = ', '.join(col_defs)

            cursor.execute(f"CREATE TABLE item_new ({defs_joined}, FOREIGN KEY (category_id) REFERENCES category(id))")
            cursor.execute(f"INSERT INTO item_new ({cols_joined}) SELECT {cols_joined} FROM item")
            cursor.execute("DROP TABLE item")
            cursor.execute("ALTER TABLE item_new RENAME TO item")
            print("Dropped legacy owner columns from item table")

        conn.commit()
        conn.close()

    # Migrate SiteSettings new columns
    if os.path.exists(db_path):
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        def column_exists2(table, column):
            cursor.execute(f"PRAGMA table_info({table})")
            return any(row[1] == column for row in cursor.fetchall())

        if not column_exists2('site_settings', 'tax_number'):
            cursor.execute("ALTER TABLE site_settings ADD COLUMN tax_number VARCHAR(100)")

        # User table migration: add is_external_user column
        if not column_exists2('user', 'is_external_user'):
            cursor.execute("ALTER TABLE user ADD COLUMN is_external_user BOOLEAN DEFAULT 0")
        if not column_exists2('site_settings', 'tax_mode'):
            cursor.execute("ALTER TABLE site_settings ADD COLUMN tax_mode VARCHAR(20) DEFAULT 'kleinunternehmer'")
        if not column_exists2('site_settings', 'payment_terms_days'):
            cursor.execute("ALTER TABLE site_settings ADD COLUMN payment_terms_days INTEGER DEFAULT 14")
        if not column_exists2('site_settings', 'quote_validity_days'):
            cursor.execute("ALTER TABLE site_settings ADD COLUMN quote_validity_days INTEGER DEFAULT 14")
        if not column_exists2('site_settings', 'logo_filename'):
            cursor.execute("ALTER TABLE site_settings ADD COLUMN logo_filename VARCHAR(300)")
        if not column_exists2('site_settings', 'terms_and_conditions_text'):
            cursor.execute("ALTER TABLE site_settings ADD COLUMN terms_and_conditions_text TEXT")

        conn.commit()
        conn.close()

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
            is_admin=True,
            can_edit_all=True
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
