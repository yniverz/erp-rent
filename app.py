from flask import Flask, send_file
from flask_login import LoginManager
from models import db, User, SiteSettings
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

def _load_favicon():
    global _favicon_data, _favicon_mimetype
    favicon_url = os.getenv('FAVICON_URL', '').strip()
    if not favicon_url:
        return
    try:
        resp = http_requests.get(favicon_url, timeout=10)
        resp.raise_for_status()
        _favicon_data = resp.content
        ct = resp.headers.get('Content-Type', 'image/x-icon').split(';')[0].strip()
        if ct:
            _favicon_mimetype = ct
        print(f"Favicon loaded from {favicon_url} ({len(_favicon_data)} bytes)")
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
    return dict(site_settings=settings, has_favicon=_favicon_data is not None)


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
