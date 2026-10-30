from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from flask_login import login_user, logout_user, login_required, current_user
from models import db, User, SiteSettings, MIN_PASSWORD_LENGTH
from helpers import throttle_count, throttle_record, totp_new_secret, totp_verify, totp_uri
from datetime import timedelta
from urllib.parse import urlsplit
import time

auth_bp = Blueprint('auth', __name__)

# Brute-force lockout: failed attempts within the window, per client IP and per username
LOCKOUT_WINDOW = timedelta(minutes=15)
MAX_FAILS_PER_IP = 5
MAX_FAILS_PER_USERNAME = 10
# Password step passed, waiting for the TOTP code: valid this long
TOTP_PENDING_SECONDS = 300


def _safe_next(target):
    """Only allow same-site relative redirects after login (no //host or scheme)."""
    if not target:
        return None
    parts = urlsplit(target.replace('\\', '/'))
    if parts.scheme or parts.netloc or not target.startswith('/') or target.startswith('//'):
        return None
    return target


def _locked_out(ip, username):
    return (throttle_count('login', LOCKOUT_WINDOW, ip=ip) >= MAX_FAILS_PER_IP
            or throttle_count('login', LOCKOUT_WINDOW, subject=username) >= MAX_FAILS_PER_USERNAME)


def _record_failure(ip, username):
    throttle_record('login', ip, username)
    print(f"Failed login for user {username[:80]!r} from {ip}")


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    """Login page"""
    if current_user.is_authenticated:
        return redirect(url_for('admin.inventory_list'))
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        ip = request.remote_addr or 'unknown'

        if _locked_out(ip, username):
            print(f"Login blocked (lockout) for user {username[:80]!r} from {ip}")
            flash('Zu viele fehlgeschlagene Anmeldeversuche. Bitte in 15 Minuten erneut versuchen.', 'error')
            return render_template('auth/login.html'), 429

        user = User.query.filter_by(username=username).first()
        if user and user.check_password(password) and user.active:
            next_page = _safe_next(request.args.get('next'))
            if user.has_totp:
                session['totp_pending'] = {'uid': user.id, 'next': next_page, 'ts': time.time()}
                return redirect(url_for('auth.login_totp'))
            return _finish_login(user, next_page)
        else:
            _record_failure(ip, username)
            flash('Ungültige Anmeldedaten oder Konto deaktiviert.', 'error')

    return render_template('auth/login.html')


def _finish_login(user, next_page):
    session.pop('totp_pending', None)
    login_user(user)
    flash('Anmeldung erfolgreich!', 'success')
    return redirect(next_page or url_for('admin.inventory_list'))


@auth_bp.route('/login/2fa', methods=['GET', 'POST'])
def login_totp():
    """Second login step for accounts with TOTP enabled"""
    pending = session.get('totp_pending')
    user = User.query.get(pending['uid']) if pending else None
    if not user or not user.active or not user.has_totp \
            or time.time() - pending.get('ts', 0) > TOTP_PENDING_SECONDS:
        session.pop('totp_pending', None)
        if pending:
            flash('Die Anmeldung ist abgelaufen. Bitte erneut anmelden.', 'error')
        return redirect(url_for('auth.login'))

    if request.method == 'POST':
        ip = request.remote_addr or 'unknown'
        if _locked_out(ip, user.username):
            session.pop('totp_pending', None)
            print(f"2FA blocked (lockout) for user {user.username!r} from {ip}")
            flash('Zu viele fehlgeschlagene Anmeldeversuche. Bitte in 15 Minuten erneut versuchen.', 'error')
            return redirect(url_for('auth.login'))
        step = totp_verify(user.totp_secret, request.form.get('code'), user.totp_last_step)
        if step is not None:
            user.totp_last_step = step
            db.session.commit()
            return _finish_login(user, pending.get('next'))
        _record_failure(ip, user.username)
        flash('Ungültiger oder bereits verwendeter Code.', 'error')

    return render_template('auth/login.html', totp_step=True)


@auth_bp.route('/logout', methods=['POST'])
@login_required
def logout():
    """Logout user"""
    logout_user()
    flash('Sie wurden abgemeldet.', 'success')
    return redirect(url_for('public.catalog'))


@auth_bp.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    """User profile – change display name, email, or password"""
    user = current_user
    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'update_profile':
            display_name = request.form.get('display_name', '').strip()
            email = request.form.get('email', '').strip()
            user.display_name = display_name or None
            user.email = email or None
            db.session.commit()
            flash('Profil aktualisiert.', 'success')

        elif action == 'totp_start':
            if not user.has_totp:
                session['totp_setup'] = totp_new_secret()

        elif action == 'totp_cancel':
            session.pop('totp_setup', None)

        elif action == 'totp_confirm':
            secret = session.get('totp_setup')
            step = totp_verify(secret, request.form.get('code')) if secret else None
            if step is None:
                flash('Der Code stimmt nicht. Bitte die Uhrzeit des Handys prüfen und erneut versuchen.', 'error')
            else:
                user.totp_secret = secret
                user.totp_last_step = step
                db.session.commit()
                session.pop('totp_setup', None)
                flash('Zwei-Faktor-Anmeldung ist jetzt aktiv.', 'success')

        elif action == 'totp_disable':
            if not user.check_password(request.form.get('current_password', '')):
                flash('Aktuelles Passwort ist falsch.', 'error')
            elif totp_verify(user.totp_secret, request.form.get('code'), user.totp_last_step) is None:
                flash('Ungültiger oder bereits verwendeter Code.', 'error')
            else:
                user.totp_secret = None
                user.totp_last_step = None
                db.session.commit()
                flash('Zwei-Faktor-Anmeldung wurde deaktiviert.', 'success')

        elif action == 'change_password':
            current_pw = request.form.get('current_password', '')
            new_pw = request.form.get('new_password', '')
            confirm_pw = request.form.get('confirm_password', '')

            if not user.check_password(current_pw):
                flash('Aktuelles Passwort ist falsch.', 'error')
            elif len(new_pw) < MIN_PASSWORD_LENGTH:
                flash(f'Neues Passwort muss mindestens {MIN_PASSWORD_LENGTH} Zeichen lang sein.', 'error')
            elif new_pw != confirm_pw:
                flash('Passwörter stimmen nicht überein.', 'error')
            else:
                user.set_password(new_pw)
                db.session.commit()
                flash('Passwort erfolgreich geändert.', 'success')

        return redirect(url_for('auth.profile'))

    totp_setup = None
    secret = session.get('totp_setup')
    if secret and not user.has_totp:
        import segno
        settings = SiteSettings.query.first()
        issuer = ((settings and (settings.display_name or settings.business_name)) or 'ERP Rent').replace(':', '')
        uri = totp_uri(secret, user.username, issuer)
        totp_setup = {
            'secret': ' '.join(secret[i:i + 4] for i in range(0, len(secret), 4)),
            'qr': segno.make(uri, error='m').svg_data_uri(scale=5, border=2, dark='#000', light='#fff'),
        }
    return render_template('auth/profile.html', user=user, totp_setup=totp_setup)
