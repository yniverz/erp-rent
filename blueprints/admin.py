from io import BytesIO
from flask import Blueprint, render_template, request, redirect, url_for, flash, send_file
from flask_login import login_required, current_user
from models import db, User, Item, Category, Quote, QuoteItem, Inquiry, InquiryItem, SiteSettings
from helpers import get_available_quantity, get_upload_path, allowed_image_file
from datetime import datetime
from functools import wraps
from werkzeug.utils import secure_filename
import os
import uuid

admin_bp = Blueprint('admin', __name__)


def admin_required(f):
    """Decorator to require admin privileges"""
    @wraps(f)
    @login_required
    def decorated_function(*args, **kwargs):
        if not current_user.is_admin:
            flash('Admin-Zugang erforderlich.', 'error')
            return redirect(url_for('admin.dashboard'))
        return f(*args, **kwargs)
    return decorated_function


def can_edit_or_admin(f):
    """Decorator: user must be admin or have can_edit_all, or own the resource"""
    @wraps(f)
    @login_required
    def decorated_function(*args, **kwargs):
        return f(*args, **kwargs)
    return decorated_function


# ============= DASHBOARD =============

@admin_bp.route('/')
@login_required
def dashboard():
    """Admin dashboard"""
    total_items = Item.query.count()
    total_quotes = Quote.query.count()
    new_inquiries = Inquiry.query.filter_by(status='new').count()
    active_quotes = Quote.query.filter(Quote.status.in_(['draft', 'finalized'])).count()
    return render_template('admin/dashboard.html',
                           total_items=total_items,
                           total_quotes=total_quotes,
                           new_inquiries=new_inquiries,
                           active_quotes=active_quotes)


# ============= CATEGORIES =============

@admin_bp.route('/categories', methods=['GET', 'POST'])
@login_required
def categories():
    """Manage categories"""
    if request.method == 'POST':
        action = request.form.get('action')
        try:
            if action == 'add':
                name = request.form.get('name', '').strip()
                order = request.form.get('display_order', 0, type=int)
                if name:
                    cat = Category(name=name, display_order=order)
                    db.session.add(cat)
                    db.session.commit()
                    flash(f'Kategorie "{name}" erstellt.', 'success')
            elif action == 'edit':
                cat_id = request.form.get('category_id', type=int)
                cat = Category.query.get_or_404(cat_id)
                cat.name = request.form.get('name', '').strip()
                cat.display_order = request.form.get('display_order', 0, type=int)
                db.session.commit()
                flash(f'Kategorie "{cat.name}" aktualisiert.', 'success')
            elif action == 'delete':
                cat_id = request.form.get('category_id', type=int)
                cat = Category.query.get_or_404(cat_id)
                # Unassign items from this category
                Item.query.filter_by(category_id=cat_id).update({'category_id': None})
                db.session.delete(cat)
                db.session.commit()
                flash('Kategorie gelöscht.', 'success')
        except Exception as e:
            db.session.rollback()
            flash(f'Fehler: {str(e)}', 'error')

    cats = Category.query.order_by(Category.display_order, Category.name).all()
    return render_template('admin/categories.html', categories=cats)


# ============= INVENTORY =============

@admin_bp.route('/inventory')
@login_required
def inventory_list():
    """List all inventory items"""
    items = Item.query.order_by(Item.name).all()
    categories = Category.query.order_by(Category.display_order, Category.name).all()
    return render_template('admin/inventory_list.html', items=items, categories=categories)


@admin_bp.route('/inventory/add', methods=['GET', 'POST'])
@login_required
def inventory_add():
    """Add new inventory item"""
    categories = Category.query.order_by(Category.display_order, Category.name).all()
    users = User.query.filter_by(active=True).order_by(User.username).all()

    if request.method == 'POST':
        try:
            name = request.form.get('name', '').strip()
            total_quantity = int(request.form.get('total_quantity', 0))
            set_size = int(request.form.get('set_size', 1))
            rental_step = int(request.form.get('rental_step', 1))
            total_cost = float(request.form.get('total_cost', 0))
            default_rental_price = float(request.form.get('default_rental_price', 0))
            description = request.form.get('description', '').strip()
            category_id = request.form.get('category_id', type=int) or None
            show_price = request.form.get('show_price_publicly') == 'on'
            visible = request.form.get('visible_in_shop') == 'on'

            # Owner: admin can assign to any user, others assign to themselves
            if current_user.is_admin:
                owner_id = request.form.get('owner_id', type=int) or current_user.id
            else:
                owner_id = current_user.id

            unit_purchase_cost = 0 if total_quantity == -1 else (total_cost / total_quantity if total_quantity > 0 else 0)

            # Handle image upload
            image_filename = None
            if 'image' in request.files:
                file = request.files['image']
                if file and file.filename and allowed_image_file(file.filename):
                    ext = file.filename.rsplit('.', 1)[1].lower()
                    image_filename = f"{uuid.uuid4().hex}.{ext}"
                    file.save(os.path.join(get_upload_path(), image_filename))

            item = Item(
                name=name,
                owner_id=owner_id,
                category_id=category_id,
                description=description or None,
                total_quantity=total_quantity,
                set_size=set_size,
                rental_step=rental_step,
                unit_purchase_cost=unit_purchase_cost,
                default_rental_price_per_day=default_rental_price,
                show_price_publicly=show_price,
                visible_in_shop=visible,
                image_filename=image_filename
            )
            db.session.add(item)
            db.session.commit()
            flash(f'{name} erfolgreich hinzugefügt!', 'success')
            return redirect(url_for('admin.inventory_list'))

        except Exception as e:
            db.session.rollback()
            flash(f'Fehler beim Hinzufügen des Artikels: {str(e)}', 'error')

    return render_template('admin/inventory_form.html',
                           item=None,
                           categories=categories,
                           users=users)


@admin_bp.route('/inventory/<int:item_id>/edit', methods=['GET', 'POST'])
@login_required
def inventory_edit(item_id):
    """Edit inventory item"""
    item = Item.query.get_or_404(item_id)
    categories = Category.query.order_by(Category.display_order, Category.name).all()
    users = User.query.filter_by(active=True).order_by(User.username).all()

    if not current_user.can_edit_item(item):
        flash('Sie haben keine Berechtigung, diesen Artikel zu bearbeiten.', 'error')
        return redirect(url_for('admin.inventory_list'))

    if request.method == 'POST':
        try:
            item.name = request.form.get('name', '').strip()
            item.total_quantity = int(request.form.get('total_quantity', 0))
            item.set_size = int(request.form.get('set_size', 1))
            item.rental_step = int(request.form.get('rental_step', 1))
            item.default_rental_price_per_day = float(request.form.get('default_rental_price', 0))
            item.description = request.form.get('description', '').strip() or None
            item.category_id = request.form.get('category_id', type=int) or None
            item.show_price_publicly = request.form.get('show_price_publicly') == 'on'
            item.visible_in_shop = request.form.get('visible_in_shop') == 'on'

            if current_user.is_admin:
                item.owner_id = request.form.get('owner_id', type=int) or item.owner_id

            if 'total_cost' in request.form and request.form.get('total_cost'):
                total_cost = float(request.form.get('total_cost'))
                item.unit_purchase_cost = 0 if item.total_quantity == -1 else (total_cost / item.total_quantity if item.total_quantity > 0 else 0)

            # Handle image upload
            if 'image' in request.files:
                file = request.files['image']
                if file and file.filename and allowed_image_file(file.filename):
                    # Delete old image
                    if item.image_filename:
                        old_path = os.path.join(get_upload_path(), item.image_filename)
                        if os.path.exists(old_path):
                            os.remove(old_path)
                    ext = file.filename.rsplit('.', 1)[1].lower()
                    item.image_filename = f"{uuid.uuid4().hex}.{ext}"
                    file.save(os.path.join(get_upload_path(), item.image_filename))

            # Remove image if requested
            if request.form.get('remove_image') == 'on' and item.image_filename:
                old_path = os.path.join(get_upload_path(), item.image_filename)
                if os.path.exists(old_path):
                    os.remove(old_path)
                item.image_filename = None

            db.session.commit()
            flash(f'{item.name} erfolgreich aktualisiert!', 'success')
            return redirect(url_for('admin.inventory_list'))

        except Exception as e:
            db.session.rollback()
            flash(f'Fehler beim Aktualisieren des Artikels: {str(e)}', 'error')

    return render_template('admin/inventory_form.html',
                           item=item,
                           categories=categories,
                           users=users)


@admin_bp.route('/inventory/<int:item_id>/delete', methods=['POST'])
@login_required
def inventory_delete(item_id):
    """Delete inventory item"""
    item = Item.query.get_or_404(item_id)

    if not current_user.can_edit_item(item):
        flash('Sie haben keine Berechtigung, diesen Artikel zu löschen.', 'error')
        return redirect(url_for('admin.inventory_list'))

    try:
        # Delete image file
        if item.image_filename:
            old_path = os.path.join(get_upload_path(), item.image_filename)
            if os.path.exists(old_path):
                os.remove(old_path)
        name = item.name
        db.session.delete(item)
        db.session.commit()
        flash(f'{name} erfolgreich gelöscht!', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Fehler beim Löschen des Artikels: {str(e)}', 'error')

    return redirect(url_for('admin.inventory_list'))


# ============= QUOTES =============

@admin_bp.route('/quotes')
@login_required
def quote_list():
    """List all quotes"""
    quotes = Quote.query.order_by(Quote.created_at.desc()).all()
    return render_template('admin/quote_list.html', quotes=quotes)


@admin_bp.route('/quotes/create', methods=['GET', 'POST'])
@login_required
def quote_create():
    """Create new quote"""
    if request.method == 'POST':
        try:
            customer_name = request.form.get('customer_name', '').strip()
            start_date_str = request.form.get('start_date')
            end_date_str = request.form.get('end_date')

            start_date = datetime.strptime(start_date_str, '%Y-%m-%d') if start_date_str else None
            end_date = datetime.strptime(end_date_str, '%Y-%m-%d') if end_date_str else None

            if start_date and end_date and start_date > end_date:
                flash('Enddatum muss nach oder gleich dem Startdatum sein!', 'error')
                return render_template('admin/quote_create.html')

            rental_days = 1
            if start_date and end_date:
                delta = end_date - start_date
                rental_days = max(1, delta.days + 1)

            quote = Quote(
                customer_name=customer_name,
                created_by_id=current_user.id,
                start_date=start_date,
                end_date=end_date,
                rental_days=rental_days,
                status='draft'
            )
            db.session.add(quote)
            db.session.commit()

            quote.generate_reference_number()
            db.session.commit()

            flash(f'Angebot für {customer_name} erstellt!', 'success')
            return redirect(url_for('admin.quote_edit', quote_id=quote.id))

        except Exception as e:
            db.session.rollback()
            flash(f'Fehler beim Erstellen des Angebots: {str(e)}', 'error')

    return render_template('admin/quote_create.html')


@admin_bp.route('/quotes/<int:quote_id>/edit', methods=['GET', 'POST'])
@login_required
def quote_edit(quote_id):
    """Edit quote and add items"""
    quote = Quote.query.get_or_404(quote_id)
    items = Item.query.order_by(Item.name).all()

    if request.method == 'POST':
        action = request.form.get('action')

        try:
            if action == 'update_quote':
                quote.customer_name = request.form.get('customer_name', '').strip()
                start_date_str = request.form.get('start_date')
                end_date_str = request.form.get('end_date')

                start_date = datetime.strptime(start_date_str, '%Y-%m-%d') if start_date_str else None
                end_date = datetime.strptime(end_date_str, '%Y-%m-%d') if end_date_str else None

                if start_date and end_date and start_date > end_date:
                    flash('Enddatum muss nach oder gleich dem Startdatum sein!', 'error')
                    item_availability = {item.id: item.total_quantity for item in items}
                    return render_template('admin/quote_edit.html', quote=quote, items=items, item_availability=item_availability)

                quote.start_date = start_date
                quote.end_date = end_date

                if start_date and end_date:
                    delta = end_date - start_date
                    quote.rental_days = max(1, delta.days + 1)
                else:
                    quote.rental_days = int(request.form.get('rental_days', 1))

                quote.recipient_lines = request.form.get('recipient_lines', '')
                quote.notes = request.form.get('notes', '')
                db.session.commit()
                flash('Angebot aktualisiert!', 'success')

            elif action == 'update_items':
                if not quote.start_date or not quote.end_date:
                    flash('Bitte setzen Sie Start- und Enddatum, bevor Sie Artikel hinzufügen!', 'error')
                    item_availability = {item.id: item.total_quantity for item in items}
                    return render_template('admin/quote_edit.html', quote=quote, items=items, item_availability=item_availability)

                errors = []
                for item in items:
                    quantity_key = f'quantity_{item.id}'
                    price_key = f'price_{item.id}'

                    if quantity_key in request.form:
                        quantity = int(request.form.get(quantity_key, 0))
                        price = round(float(request.form.get(price_key, item.default_rental_price_per_day)), 2)

                        if quantity > 0:
                            available = get_available_quantity(
                                item.id,
                                quote.start_date,
                                quote.end_date,
                                exclude_quote_id=quote.id
                            )

                            if available != -1 and quantity > available:
                                errors.append(f'{item.name}: Nur {available} verfügbar (gesamt: {item.total_quantity})')
                                continue

                            if item.rental_step > 1 and quantity % item.rental_step != 0:
                                errors.append(f'{item.name}: Menge muss ein Vielfaches von {item.rental_step} sein')
                                continue

                        existing = QuoteItem.query.filter_by(
                            quote_id=quote.id,
                            item_id=item.id,
                            is_custom=False
                        ).first()

                        if quantity > 0:
                            if existing:
                                existing.quantity = quantity
                                existing.rental_price_per_day = price
                            else:
                                quote_item = QuoteItem(
                                    quote_id=quote.id,
                                    item_id=item.id,
                                    quantity=quantity,
                                    rental_price_per_day=price,
                                    is_custom=False
                                )
                                db.session.add(quote_item)
                        else:
                            if existing:
                                db.session.delete(existing)

                if errors:
                    flash('Fehler: ' + '; '.join(errors), 'error')
                    db.session.rollback()
                else:
                    db.session.commit()
                    flash('Artikel aktualisiert!', 'success')

            elif action == 'add_custom':
                custom_name = request.form.get('custom_name', '').strip()
                custom_quantity = int(request.form.get('custom_quantity', 1))
                custom_price = round(float(request.form.get('custom_price', 0)), 2)

                if custom_name and custom_price > 0:
                    quote_item = QuoteItem(
                        quote_id=quote.id,
                        item_id=None,
                        quantity=custom_quantity,
                        rental_price_per_day=custom_price,
                        custom_item_name=custom_name,
                        is_custom=True
                    )
                    db.session.add(quote_item)
                    db.session.commit()
                    flash(f'Eigene Position "{custom_name}" hinzugefügt!', 'success')

            elif action == 'remove_item':
                quote_item_id = int(request.form.get('quote_item_id'))
                quote_item = QuoteItem.query.get(quote_item_id)
                if quote_item and quote_item.quote_id == quote.id:
                    db.session.delete(quote_item)
                    db.session.commit()
                    flash('Artikel aus Angebot entfernt!', 'success')

            elif action == 'update_discount':
                discount_percent = float(request.form.get('final_discount_percent', 0))
                quote.discount_percent = discount_percent
                db.session.commit()
                flash(f'Rabatt auf {discount_percent:.4f}% aktualisiert', 'success')

            elif action == 'finalize':
                if not quote.start_date or not quote.end_date:
                    flash('Kann nicht finalisiert werden: Start- und Enddatum müssen gesetzt sein!', 'error')
                    item_availability = {item.id: item.total_quantity for item in items}
                    return render_template('admin/quote_edit.html', quote=quote, items=items, item_availability=item_availability)

                validation_errors = []
                for quote_item in quote.quote_items:
                    if not quote_item.is_custom and quote_item.item:
                        available = get_available_quantity(
                            quote_item.item_id,
                            quote.start_date,
                            quote.end_date,
                            exclude_quote_id=quote.id
                        )
                        if available != -1 and quote_item.quantity > available:
                            validation_errors.append(
                                f'{quote_item.item.name}: Nur {available} verfügbar (Angebot hat {quote_item.quantity})'
                            )

                if validation_errors:
                    flash('Kann nicht finalisiert werden: ' + '; '.join(validation_errors), 'error')
                    item_availability = {}
                    for item in items:
                        item_availability[item.id] = get_available_quantity(
                            item.id, quote.start_date, quote.end_date, exclude_quote_id=quote.id)
                    return render_template('admin/quote_edit.html', quote=quote, items=items, item_availability=item_availability)

                quote.status = 'finalized'
                quote.finalized_at = datetime.utcnow()
                db.session.commit()
                flash('Angebot finalisiert!', 'success')
                return redirect(url_for('admin.quote_view', quote_id=quote.id))

        except Exception as e:
            db.session.rollback()
            flash(f'Fehler: {str(e)}', 'error')

    # Calculate availability
    item_availability = {}
    if quote.start_date and quote.end_date:
        for item in items:
            item_availability[item.id] = get_available_quantity(
                item.id, quote.start_date, quote.end_date, exclude_quote_id=quote.id)
    else:
        for item in items:
            item_availability[item.id] = item.total_quantity

    return render_template('admin/quote_edit.html', quote=quote, items=items, item_availability=item_availability)


@admin_bp.route('/quotes/<int:quote_id>')
@login_required
def quote_view(quote_id):
    """View quote details"""
    quote = Quote.query.get_or_404(quote_id)
    return render_template('admin/quote_view.html', quote=quote)


@admin_bp.route('/quotes/<int:quote_id>/unfinalize', methods=['POST'])
@login_required
def quote_unfinalize(quote_id):
    quote = Quote.query.get_or_404(quote_id)
    try:
        if quote.status == 'finalized':
            quote.status = 'draft'
            quote.finalized_at = None
            db.session.commit()
            flash('Angebot zurück in den Entwurf versetzt!', 'success')
        else:
            flash('Angebot ist nicht finalisiert.', 'info')
    except Exception as e:
        db.session.rollback()
        flash(f'Fehler: {str(e)}', 'error')
    return redirect(url_for('admin.quote_edit', quote_id=quote_id))


@admin_bp.route('/quotes/<int:quote_id>/mark_paid', methods=['POST'])
@login_required
def quote_mark_paid(quote_id):
    """Mark quote as paid and update item revenue"""
    quote = Quote.query.get_or_404(quote_id)
    try:
        if quote.status != 'paid':
            quote.status = 'paid'
            quote.paid_at = datetime.utcnow()

            discount_multiplier = (100 - quote.discount_percent) / 100
            for quote_item in quote.quote_items:
                if not quote_item.is_custom and quote_item.item:
                    item_revenue = round(quote_item.total_price * discount_multiplier, 2)
                    quote_item.item.total_revenue = round(quote_item.item.total_revenue + item_revenue, 2)

            db.session.commit()
            flash('Angebot als bezahlt markiert und Umsatz aktualisiert!', 'success')
        else:
            flash('Angebot ist bereits als bezahlt markiert.', 'info')
    except Exception as e:
        db.session.rollback()
        flash(f'Fehler: {str(e)}', 'error')
    return redirect(url_for('admin.quote_view', quote_id=quote_id))


@admin_bp.route('/quotes/<int:quote_id>/unpay', methods=['POST'])
@login_required
def quote_unpay(quote_id):
    """Unpay quote and revert revenue"""
    quote = Quote.query.get_or_404(quote_id)
    try:
        if quote.status == 'paid':
            discount_multiplier = (100 - quote.discount_percent) / 100
            for quote_item in quote.quote_items:
                if not quote_item.is_custom and quote_item.item:
                    item_revenue = round(quote_item.total_price * discount_multiplier, 2)
                    quote_item.item.total_revenue = round(quote_item.item.total_revenue - item_revenue, 2)

            quote.status = 'finalized'
            quote.paid_at = None
            db.session.commit()
            flash('Zahlung aufgehoben und Umsatz zurückerstattet!', 'success')
        else:
            flash('Angebot ist nicht als bezahlt markiert.', 'info')
    except Exception as e:
        db.session.rollback()
        flash(f'Fehler: {str(e)}', 'error')
    return redirect(url_for('admin.quote_view', quote_id=quote_id))


@admin_bp.route('/quotes/<int:quote_id>/delete', methods=['POST'])
@login_required
def quote_delete(quote_id):
    """Delete quote and revert revenue if paid"""
    quote = Quote.query.get_or_404(quote_id)
    try:
        if quote.status == 'paid':
            discount_multiplier = (100 - quote.discount_percent) / 100
            for quote_item in quote.quote_items:
                if not quote_item.is_custom and quote_item.item:
                    item_revenue = round(quote_item.total_price * discount_multiplier, 2)
                    quote_item.item.total_revenue = round(quote_item.item.total_revenue - item_revenue, 2)

        db.session.delete(quote)
        db.session.commit()
        flash('Angebot gelöscht!', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Fehler: {str(e)}', 'error')
    return redirect(url_for('admin.quote_list'))


# ============= INQUIRIES =============

@admin_bp.route('/inquiries')
@login_required
def inquiry_list():
    """List all customer inquiries"""
    inquiries = Inquiry.query.order_by(Inquiry.created_at.desc()).all()
    return render_template('admin/inquiry_list.html', inquiries=inquiries)


@admin_bp.route('/inquiries/<int:inquiry_id>')
@login_required
def inquiry_view(inquiry_id):
    """View inquiry details"""
    inquiry = Inquiry.query.get_or_404(inquiry_id)
    return render_template('admin/inquiry_view.html', inquiry=inquiry)


@admin_bp.route('/inquiries/<int:inquiry_id>/status', methods=['POST'])
@login_required
def inquiry_update_status(inquiry_id):
    """Update inquiry status"""
    inquiry = Inquiry.query.get_or_404(inquiry_id)
    new_status = request.form.get('status')
    if new_status in ['new', 'contacted', 'converted', 'closed']:
        inquiry.status = new_status
        db.session.commit()
        flash(f'Anfragestatus auf {new_status} aktualisiert.', 'success')
    return redirect(url_for('admin.inquiry_view', inquiry_id=inquiry_id))


@admin_bp.route('/inquiries/<int:inquiry_id>/convert', methods=['POST'])
@login_required
def inquiry_convert(inquiry_id):
    """Convert inquiry to a quote"""
    inquiry = Inquiry.query.get_or_404(inquiry_id)

    try:
        quote = Quote(
            customer_name=inquiry.customer_name,
            created_by_id=current_user.id,
            start_date=inquiry.desired_start_date,
            end_date=inquiry.desired_end_date,
            rental_days=1,
            status='draft',
            inquiry_id=inquiry.id,
            notes=f"Aus Anfrage umgewandelt. E-Mail: {inquiry.customer_email}"
                  + (f", Telefon: {inquiry.customer_phone}" if inquiry.customer_phone else "")
                  + (f"\n{inquiry.message}" if inquiry.message else "")
        )
        if quote.start_date and quote.end_date:
            delta = quote.end_date - quote.start_date
            quote.rental_days = max(1, delta.days + 1)

        db.session.add(quote)
        db.session.commit()

        quote.generate_reference_number()

        # Add inquiry items to the quote
        for inq_item in inquiry.items:
            item = Item.query.get(inq_item.item_id)
            if item:
                qi = QuoteItem(
                    quote_id=quote.id,
                    item_id=item.id,
                    quantity=inq_item.quantity,
                    rental_price_per_day=item.default_rental_price_per_day,
                    is_custom=False
                )
                db.session.add(qi)

        inquiry.status = 'converted'
        db.session.commit()

        flash(f'Angebot aus Anfrage erstellt!', 'success')
        return redirect(url_for('admin.quote_edit', quote_id=quote.id))

    except Exception as e:
        db.session.rollback()
        flash(f'Fehler beim Umwandeln der Anfrage: {str(e)}', 'error')
        return redirect(url_for('admin.inquiry_view', inquiry_id=inquiry_id))


@admin_bp.route('/inquiries/<int:inquiry_id>/delete', methods=['POST'])
@login_required
def inquiry_delete(inquiry_id):
    """Delete an inquiry"""
    inquiry = Inquiry.query.get_or_404(inquiry_id)
    try:
        db.session.delete(inquiry)
        db.session.commit()
        flash('Anfrage gelöscht.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Fehler: {str(e)}', 'error')
    return redirect(url_for('admin.inquiry_list'))


# ============= USER MANAGEMENT =============

@admin_bp.route('/users')
@admin_required
def user_list():
    """List all users (admin only)"""
    users = User.query.order_by(User.username).all()
    return render_template('admin/user_list.html', users=users)


@admin_bp.route('/users/add', methods=['GET', 'POST'])
@admin_required
def user_add():
    """Add new user (admin only)"""
    if request.method == 'POST':
        try:
            username = request.form.get('username', '').strip()
            password = request.form.get('password', '')
            display_name = request.form.get('display_name', '').strip()
            email = request.form.get('email', '').strip()
            is_admin = request.form.get('is_admin') == 'on'
            can_edit_all = request.form.get('can_edit_all') == 'on'

            if not username or not password:
                flash('Benutzername und Passwort sind erforderlich.', 'error')
                return render_template('admin/user_form.html', user=None)

            if User.query.filter_by(username=username).first():
                flash('Benutzername existiert bereits.', 'error')
                return render_template('admin/user_form.html', user=None)

            user = User(
                username=username,
                display_name=display_name or None,
                email=email or None,
                is_admin=is_admin,
                can_edit_all=can_edit_all
            )
            user.set_password(password)
            db.session.add(user)
            db.session.commit()

            flash(f'Benutzer "{username}" erstellt.', 'success')
            return redirect(url_for('admin.user_list'))

        except Exception as e:
            db.session.rollback()
            flash(f'Fehler: {str(e)}', 'error')

    return render_template('admin/user_form.html', user=None)


@admin_bp.route('/users/<int:user_id>/edit', methods=['GET', 'POST'])
@admin_required
def user_edit(user_id):
    """Edit user (admin only)"""
    user = User.query.get_or_404(user_id)

    if request.method == 'POST':
        try:
            user.display_name = request.form.get('display_name', '').strip() or None
            user.email = request.form.get('email', '').strip() or None
            user.is_admin = request.form.get('is_admin') == 'on'
            user.can_edit_all = request.form.get('can_edit_all') == 'on'
            user.active = request.form.get('active') == 'on'

            new_password = request.form.get('password', '').strip()
            if new_password:
                user.set_password(new_password)

            # Prevent removing own admin status
            if user.id == 1 and not user.is_admin:
                user.is_admin = True
                flash('Admin-Status des primären Admin-Kontos kann nicht entfernt werden.', 'info')

            db.session.commit()
            flash(f'Benutzer "{user.username}" aktualisiert.', 'success')
            return redirect(url_for('admin.user_list'))

        except Exception as e:
            db.session.rollback()
            flash(f'Fehler: {str(e)}', 'error')

    return render_template('admin/user_form.html', user=user)


@admin_bp.route('/users/<int:user_id>/delete', methods=['POST'])
@admin_required
def user_delete(user_id):
    """Delete user (admin only)"""
    if user_id == current_user.id:
        flash('Eigenes Konto kann nicht gelöscht werden.', 'error')
        return redirect(url_for('admin.user_list'))

    user = User.query.get_or_404(user_id)
    try:
        # Reassign items to current admin
        Item.query.filter_by(owner_id=user.id).update({'owner_id': current_user.id})
        db.session.delete(user)
        db.session.commit()
        flash(f'Benutzer "{user.username}" gelöscht. Artikel wurden Ihnen zugewiesen.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Fehler: {str(e)}', 'error')
    return redirect(url_for('admin.user_list'))


# ============= SETTINGS =============

@admin_bp.route('/settings', methods=['GET', 'POST'])
@admin_required
def settings():
    """Site settings (admin only)"""
    settings_record = SiteSettings.query.first()
    if not settings_record:
        settings_record = SiteSettings()
        db.session.add(settings_record)
        db.session.commit()

    if request.method == 'POST':
        try:
            settings_record.business_name = request.form.get('business_name', '').strip()
            settings_record.address_lines = request.form.get('address_lines', '')
            settings_record.contact_lines = request.form.get('contact_lines', '')
            settings_record.bank_lines = request.form.get('bank_lines', '')
            settings_record.shop_description = request.form.get('shop_description', '')
            settings_record.imprint_url = request.form.get('imprint_url', '').strip()
            settings_record.privacy_url = request.form.get('privacy_url', '').strip()
            settings_record.notification_email = request.form.get('notification_email', '').strip()
            settings_record.updated_at = datetime.utcnow()
            db.session.commit()
            flash('Einstellungen gespeichert!', 'success')
        except Exception as e:
            db.session.rollback()
            flash(f'Fehler: {str(e)}', 'error')

    return render_template('admin/settings.html', settings=settings_record)


# ============= REPORTS =============

@admin_bp.route('/reports/payoff')
@login_required
def report_payoff():
    """Payoff status report"""
    items = Item.query.order_by(Item.name).all()
    users = User.query.filter_by(active=True).order_by(User.username).all()

    misc_revenue = db.session.query(db.func.sum(
        QuoteItem.quantity * QuoteItem.rental_price_per_day * Quote.rental_days
    )).join(Quote).filter(
        QuoteItem.is_custom == True,
        Quote.status == 'paid'
    ).scalar() or 0.0

    return render_template('admin/payoff_report.html', items=items, users=users, misc_revenue=misc_revenue)


@admin_bp.route('/schedule')
@login_required
def schedule():
    """Rental schedule / calendar"""
    from datetime import timedelta
    quotes = Quote.query.filter(
        Quote.start_date.isnot(None),
        Quote.end_date.isnot(None)
    ).order_by(Quote.start_date).all()
    return render_template('admin/schedule.html', quotes=quotes, timedelta=timedelta)


# ============= PDF GENERATORS =============

@admin_bp.route('/quotes/<int:quote_id>/ueberlassungsbestaetigung.pdf')
@login_required
def ueberlassungsbestaetigung_pdf(quote_id):
    with_quote_total = request.args.get("with_quote_total", "false").lower() == "true"

    from generators.ueberlassungsbestaetigung import _build_pdf_bytes
    quote = Quote.query.get_or_404(quote_id)

    if quote.start_date and quote.end_date:
        f = quote.start_date.strftime("%d.%m.%Y")
        t = quote.end_date.strftime("%d.%m.%Y")
        timeframe_str = f if f == t else f"{f} - {t}"
    else:
        timeframe_str = "#Datum nicht festgelegt#"

    site_settings = SiteSettings.query.first()
    consignor_info = []
    if site_settings:
        if site_settings.business_name:
            consignor_info.append(site_settings.business_name)
        if site_settings.address_lines:
            consignor_info.extend([line for line in site_settings.address_lines.split("\n") if line.strip()])

    kwargs = {}
    if with_quote_total:
        kwargs["total_sum"] = quote.total

    pdf_bytes = _build_pdf_bytes(
        consignor_info=consignor_info,
        recipient_info=[line for line in (quote.recipient_lines or quote.customer_name).split("\n") if line.strip()],
        timeframe_str=timeframe_str,
        items=[(q.quantity, q.display_name) for q in quote.quote_items],
        **kwargs)

    response = send_file(
        BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=False,
        download_name="ueberlassungsbestaetigung.pdf",
        max_age=0,
    )
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


@admin_bp.route('/quotes/<int:quote_id>/kostenbeteiligung.pdf')
@login_required
def quote_receipt(quote_id):
    """Generate Kostenbeteiligung/Rechnung PDF"""
    from generators.kostenbeteiligung import build_rechnung_pdf_bytes

    quote = Quote.query.get_or_404(quote_id)
    site_settings = SiteSettings.query.first()

    if quote.start_date and quote.end_date:
        bereitstellungszeitraum = (
            quote.start_date.strftime("%d.%m.%Y"),
            quote.end_date.strftime("%d.%m.%Y")
        )
    else:
        bereitstellungszeitraum = ("XX.XX.20XX", "XX.XX.20XX")

    issuer_name = site_settings.business_name if site_settings and site_settings.business_name else "Ihr Unternehmen"
    address_lines = site_settings.address_lines.split('\n') if site_settings and site_settings.address_lines else []
    contact_lines = site_settings.contact_lines.split('\n') if site_settings and site_settings.contact_lines else []
    bank_lines = site_settings.bank_lines.split('\n') if site_settings and site_settings.bank_lines else []
    recipient_lines = quote.recipient_lines.split('\n') if quote.recipient_lines else [quote.customer_name]

    pdf_bytes = build_rechnung_pdf_bytes(
        issuer_name=issuer_name,
        issuer_address_lines=[l.strip() for l in address_lines if l.strip()],
        issuer_contact_lines=[l.strip() for l in contact_lines if l.strip()],
        bank_lines=[l.strip() for l in bank_lines if l.strip()],
        recipient_lines=[l.strip() for l in recipient_lines if l.strip()],
        reference_no=quote.reference_number or f"RE{quote.id:04d}",
        bereitstellungszeitraum=bereitstellungszeitraum,
        rechnungsbetrag_eur=float(quote.total),
        rechnungsdatum=quote.finalized_at.strftime("%d.%m.%Y") if quote.finalized_at else datetime.now().strftime("%d.%m.%Y")
    )

    response = send_file(
        BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=False,
        download_name=f"rechnung_{quote.reference_number}.pdf",
        max_age=0,
    )
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response
