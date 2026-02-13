from flask import Flask, send_file
from flask_login import LoginManager
from models import db, User, SiteSettings, PackageComponent, ItemOwnership
from dotenv import load_dotenv
import os
import io
import requests as http_requests

load_dotenv()

app = Flask(__name__)
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
                    UNIQUE(item_id, user_id),
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
