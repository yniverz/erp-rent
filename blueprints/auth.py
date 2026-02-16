from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_user, logout_user, login_required, current_user
from models import db, User

auth_bp = Blueprint('auth', __name__)


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    """Login page"""
    if current_user.is_authenticated:
        return redirect(url_for('admin.dashboard'))
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')

        user = User.query.filter_by(username=username).first()
        if user and user.check_password(password) and user.active and not user.is_external_user:
            login_user(user)
            flash('Anmeldung erfolgreich!', 'success')
            next_page = request.args.get('next')
            return redirect(next_page or url_for('admin.dashboard'))
        else:
            flash('Ungültige Anmeldedaten oder Konto deaktiviert.', 'error')

    return render_template('auth/login.html')


@auth_bp.route('/logout')
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

        elif action == 'change_password':
            current_pw = request.form.get('current_password', '')
            new_pw = request.form.get('new_password', '')
            confirm_pw = request.form.get('confirm_password', '')

            if not user.check_password(current_pw):
                flash('Aktuelles Passwort ist falsch.', 'error')
            elif len(new_pw) < 4:
                flash('Neues Passwort muss mindestens 4 Zeichen lang sein.', 'error')
            elif new_pw != confirm_pw:
                flash('Passwörter stimmen nicht überein.', 'error')
            else:
                user.set_password(new_pw)
                db.session.commit()
                flash('Passwort erfolgreich geändert.', 'success')

        return redirect(url_for('auth.profile'))

    return render_template('auth/profile.html', user=user)
