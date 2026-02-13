from io import BytesIO
from flask import Blueprint, render_template, request, redirect, url_for, flash, send_file, jsonify, abort
from flask_login import login_required, current_user
from models import db, User, Item, Category, Quote, QuoteItem, Inquiry, InquiryItem, SiteSettings, Customer, PackageComponent, ItemOwnership
from helpers import get_available_quantity, get_package_available_quantity, get_upload_path, allowed_image_file
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
    items = Item.query.outerjoin(Category).order_by(
        db.case((Item.category_id.is_(None), 1), else_=0),
        Category.display_order,
        Category.name,
        Item.name
    ).all()
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
            default_rental_price = float(request.form.get('default_rental_price', 0))
            description = request.form.get('description', '').strip()
            category_id = request.form.get('category_id', type=int) or None
            show_price = request.form.get('show_price_publicly') == 'on'
            visible = request.form.get('visible_in_shop') == 'on'
            is_package = request.form.get('is_package') == 'on'

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
                category_id=category_id,
                description=description or None,
                default_rental_price_per_day=default_rental_price,
                show_price_publicly=show_price,
                visible_in_shop=visible,
                image_filename=image_filename,
                is_package=is_package
            )

            # Handle subcategories
            subcategory_ids = request.form.getlist('subcategory_ids', type=int)
            item.subcategories = Category.query.filter(Category.id.in_(subcategory_ids)).all() if subcategory_ids else []

            db.session.add(item)
            db.session.flush()  # Get the item.id

            # Handle package components
            if is_package:
                comp_item_ids = request.form.getlist('component_item_ids', type=int)
                comp_quantities = request.form.getlist('component_quantities', type=int)
                for comp_id, comp_qty in zip(comp_item_ids, comp_quantities):
                    if comp_id and comp_qty and comp_qty > 0:
                        pc = PackageComponent(
                            package_id=item.id,
                            component_item_id=comp_id,
                            quantity=comp_qty
                        )
                        db.session.add(pc)
            else:
                # Handle ownership entries
                ownership_user_ids = request.form.getlist('ownership_user_ids', type=int)
                ownership_quantities = request.form.getlist('ownership_quantities', type=int)
                ownership_ext_prices = request.form.getlist('ownership_ext_prices')
                ownership_purchase_costs = request.form.getlist('ownership_purchase_costs')

                for i, uid in enumerate(ownership_user_ids):
                    if not uid:
                        continue
                    qty = ownership_quantities[i] if i < len(ownership_quantities) else 0
                    ext_price_str = ownership_ext_prices[i] if i < len(ownership_ext_prices) else ''
                    purchase_cost_str = ownership_purchase_costs[i] if i < len(ownership_purchase_costs) else ''
                    ext_price = float(ext_price_str) if ext_price_str.strip() else None
                    purchase_cost = float(purchase_cost_str) if purchase_cost_str.strip() else 0

                    ownership = ItemOwnership(
                        item_id=item.id,
                        user_id=uid,
                        quantity=qty,
                        external_price_per_day=ext_price,
                        purchase_cost=purchase_cost
                    )
                    db.session.add(ownership)

            db.session.commit()
            flash(f'{name} erfolgreich hinzugefügt!', 'success')
            return redirect(url_for('admin.inventory_list'))

        except Exception as e:
            db.session.rollback()
            flash(f'Fehler beim Hinzufügen des Artikels: {str(e)}', 'error')

    return render_template('admin/inventory_form.html',
                           item=None,
                           categories=categories,
                           users=users,
                           all_items=Item.query.filter_by(is_package=False).order_by(Item.name).all())


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
            item.default_rental_price_per_day = float(request.form.get('default_rental_price', 0))
            item.description = request.form.get('description', '').strip() or None
            item.category_id = request.form.get('category_id', type=int) or None
            item.show_price_publicly = request.form.get('show_price_publicly') == 'on'
            item.visible_in_shop = request.form.get('visible_in_shop') == 'on'
            item.is_package = request.form.get('is_package') == 'on'

            if item.is_package:
                # Clear ownerships for packages
                ItemOwnership.query.filter_by(item_id=item.id).delete()

                # Update package components
                PackageComponent.query.filter_by(package_id=item.id).delete()
                comp_item_ids = request.form.getlist('component_item_ids', type=int)
                comp_quantities = request.form.getlist('component_quantities', type=int)
                for comp_id, comp_qty in zip(comp_item_ids, comp_quantities):
                    if comp_id and comp_qty and comp_qty > 0:
                        pc = PackageComponent(
                            package_id=item.id,
                            component_item_id=comp_id,
                            quantity=comp_qty
                        )
                        db.session.add(pc)
            else:
                # Update ownership entries
                ItemOwnership.query.filter_by(item_id=item.id).delete()
                ownership_user_ids = request.form.getlist('ownership_user_ids', type=int)
                ownership_quantities = request.form.getlist('ownership_quantities', type=int)
                ownership_ext_prices = request.form.getlist('ownership_ext_prices')
                ownership_purchase_costs = request.form.getlist('ownership_purchase_costs')

                for i, uid in enumerate(ownership_user_ids):
                    if not uid:
                        continue
                    qty = ownership_quantities[i] if i < len(ownership_quantities) else 0
                    ext_price_str = ownership_ext_prices[i] if i < len(ownership_ext_prices) else ''
                    purchase_cost_str = ownership_purchase_costs[i] if i < len(ownership_purchase_costs) else ''
                    ext_price = float(ext_price_str) if ext_price_str.strip() else None
                    purchase_cost = float(purchase_cost_str) if purchase_cost_str.strip() else 0

                    ownership = ItemOwnership(
                        item_id=item.id,
                        user_id=uid,
                        quantity=qty,
                        external_price_per_day=ext_price,
                        purchase_cost=purchase_cost
                    )
                    db.session.add(ownership)

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

            # Handle subcategories
            subcategory_ids = request.form.getlist('subcategory_ids', type=int)
            item.subcategories = Category.query.filter(Category.id.in_(subcategory_ids)).all() if subcategory_ids else []

            db.session.commit()
            flash(f'{item.name} erfolgreich aktualisiert!', 'success')
            return redirect(url_for('admin.inventory_list'))

        except Exception as e:
            db.session.rollback()
            flash(f'Fehler beim Aktualisieren des Artikels: {str(e)}', 'error')

    return render_template('admin/inventory_form.html',
                           item=item,
                           categories=categories,
                           users=users,
                           all_items=Item.query.filter(Item.is_package == False, Item.id != item.id).order_by(Item.name).all())


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
        # Remove this item from any packages it's a component of
        PackageComponent.query.filter_by(component_item_id=item.id).delete()
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
                status='draft',
                recipient_lines=request.form.get('recipient_lines', '').strip()
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
    categories = Category.query.order_by(Category.display_order, Category.name).all()

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
                    return render_template('admin/quote_edit.html', quote=quote, items=items, categories=categories, item_availability=item_availability)

                quote.start_date = start_date
                quote.end_date = end_date

                if start_date and end_date:
                    delta = end_date - start_date
                    quote.rental_days = max(1, delta.days + 1)
                else:
                    quote.rental_days = int(request.form.get('rental_days', 1))

                # Manual rental days override
                # Only update if the form explicitly includes the field
                if 'rental_days_override' in request.form:
                    override_str = request.form.get('rental_days_override', '').strip()
                    quote.rental_days_override = int(override_str) if override_str else None

                quote.recipient_lines = request.form.get('recipient_lines', '')
                quote.notes = request.form.get('notes', '')
                db.session.commit()
                flash('Angebot aktualisiert!', 'success')

            elif action == 'add_item':
                if not quote.start_date or not quote.end_date:
                    flash('Bitte setzen Sie Start- und Enddatum, bevor Sie Artikel hinzufügen!', 'error')
                else:
                    item_id = request.form.get('item_id', type=int)
                    if item_id:
                        item = Item.query.get(item_id)
                        if item:
                            if item.is_package:
                                # Check if package already added
                                existing_pkg = QuoteItem.query.filter_by(
                                    quote_id=quote.id, package_id=item.id
                                ).first()
                                if existing_pkg:
                                    flash(f'{item.name} ist bereits im Angebot.', 'info')
                                else:
                                    # Calculate proportional prices based on package price
                                    component_price_sum = item.component_price_sum
                                    for pc in item.package_components:
                                        if component_price_sum > 0:
                                            # Proportional share of package price
                                            comp_share = (pc.component_item.default_rental_price_per_day * pc.quantity) / component_price_sum
                                            adjusted_price = round((item.default_rental_price_per_day * comp_share) / pc.quantity, 2)
                                        else:
                                            adjusted_price = 0
                                        # Calculate blended external cost
                                        ext_cost_total, _ = pc.component_item.calculate_external_cost(pc.quantity)
                                        ext_cost_per_unit = round(ext_cost_total / pc.quantity, 2) if pc.quantity > 0 else 0
                                        qi = QuoteItem(
                                            quote_id=quote.id,
                                            item_id=pc.component_item_id,
                                            quantity=pc.quantity,
                                            rental_price_per_day=adjusted_price,
                                            rental_cost_per_day=ext_cost_per_unit,
                                            is_custom=False,
                                            package_id=item.id
                                        )
                                        db.session.add(qi)
                                    db.session.commit()
                                    flash(f'Paket {item.name} mit {len(item.package_components)} Komponenten hinzugefügt!', 'success')
                            else:
                                existing = QuoteItem.query.filter_by(
                                    quote_id=quote.id, item_id=item.id, is_custom=False, package_id=None
                                ).first()
                                if existing:
                                    flash(f'{item.name} ist bereits im Angebot.', 'info')
                                else:
                                    # Calculate blended external cost for initial quantity
                                    initial_qty = 1
                                    ext_cost_total, _ = item.calculate_external_cost(initial_qty)
                                    ext_cost_per_unit = round(ext_cost_total / initial_qty, 2) if initial_qty > 0 else 0
                                    qi = QuoteItem(
                                        quote_id=quote.id,
                                        item_id=item.id,
                                        quantity=initial_qty,
                                        rental_price_per_day=item.default_rental_price_per_day,
                                        rental_cost_per_day=ext_cost_per_unit,
                                        is_custom=False
                                    )
                                    db.session.add(qi)
                                    db.session.commit()
                                    flash(f'{item.name} hinzugefügt!', 'success')

            elif action == 'update_items':
                if not quote.start_date or not quote.end_date:
                    flash('Bitte setzen Sie Start- und Enddatum, bevor Sie Artikel bearbeiten!', 'error')
                    item_availability = {item.id: item.total_quantity for item in items}
                    return render_template('admin/quote_edit.html', quote=quote, items=items, categories=categories, item_availability=item_availability)

                errors = []
                for qi in quote.quote_items:
                    if qi.is_custom:
                        continue
                    if not qi.item:
                        continue

                    # Use qi.id as unique key for package components
                    if qi.package_id:
                        quantity_key = f'quantity_pkg_{qi.id}'
                        price_key = f'price_pkg_{qi.id}'
                        cost_key = f'cost_pkg_{qi.id}'
                        exempt_key = f'discount_exempt_pkg_{qi.id}'
                    else:
                        quantity_key = f'quantity_{qi.item_id}'
                        price_key = f'price_{qi.item_id}'
                        cost_key = f'cost_{qi.item_id}'
                        exempt_key = f'discount_exempt_{qi.item_id}'

                    if quantity_key in request.form:
                        quantity = int(request.form.get(quantity_key, 0))
                        price = round(float(request.form.get(price_key, qi.rental_price_per_day)), 2)
                        cost = round(float(request.form.get(cost_key, qi.rental_cost_per_day)), 2)
                        exempt = request.form.get(exempt_key) == 'on'

                        if quantity > 0:
                            available = get_available_quantity(
                                qi.item_id,
                                quote.start_date,
                                quote.end_date,
                                exclude_quote_id=quote.id
                            )

                            if available != -1 and quantity > available:
                                errors.append(f'{qi.item.name}: Nur {available} verfügbar (gesamt: {qi.item.total_quantity}), aber {quantity} zugewiesen')

                            qi.quantity = quantity
                            qi.rental_price_per_day = price
                            qi.rental_cost_per_day = cost
                            qi.discount_exempt = exempt
                        else:
                            db.session.delete(qi)

                # Also update custom items discount_exempt
                for qi in quote.quote_items:
                    if qi.is_custom:
                        exempt_key = f'discount_exempt_custom_{qi.id}'
                        qi.discount_exempt = request.form.get(exempt_key) == 'on'

                if errors:
                    flash('⚠ Bestandswarnung: ' + '; '.join(errors), 'warning')
                db.session.commit()
                flash('Artikel aktualisiert!', 'success')

            elif action == 'remove_item':
                quote_item_id = int(request.form.get('quote_item_id'))
                quote_item = QuoteItem.query.get(quote_item_id)
                if quote_item and quote_item.quote_id == quote.id:
                    db.session.delete(quote_item)
                    db.session.commit()
                    flash('Artikel aus Angebot entfernt!', 'success')

            elif action == 'remove_package':
                package_id = int(request.form.get('package_id'))
                pkg_items = QuoteItem.query.filter_by(quote_id=quote.id, package_id=package_id).all()
                for qi in pkg_items:
                    db.session.delete(qi)
                db.session.commit()
                flash('Paket aus Angebot entfernt!', 'success')

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

            elif action == 'update_discount':
                target_total_str = request.form.get('target_total', '').strip()
                if target_total_str:
                    # Calculate discount percent from target total
                    target_total = float(target_total_str)
                    discountable = quote.discountable_subtotal
                    if discountable > 0:
                        needed_discount = quote.subtotal - target_total
                        discount_percent = max(0, min(100, (needed_discount / discountable) * 100))
                    else:
                        discount_percent = 0
                else:
                    discount_percent = float(request.form.get('final_discount_percent', 0))
                quote.discount_percent = discount_percent
                quote.discount_label = request.form.get('discount_label', '').strip() or None
                db.session.commit()
                flash(f'Rabatt auf {discount_percent:.4f}% aktualisiert (Gesamt: €{quote.total:.2f})', 'success')

            elif action == 'finalize':
                if not quote.start_date or not quote.end_date:
                    flash('Kann nicht finalisiert werden: Start- und Enddatum müssen gesetzt sein!', 'error')
                    item_availability = {item.id: item.total_quantity for item in items}
                    return render_template('admin/quote_edit.html', quote=quote, items=items, categories=categories, item_availability=item_availability)

                validation_warnings = []
                for quote_item in quote.quote_items:
                    if not quote_item.is_custom and quote_item.item:
                        available = get_available_quantity(
                            quote_item.item_id,
                            quote.start_date,
                            quote.end_date,
                            exclude_quote_id=quote.id
                        )
                        if available != -1 and quote_item.quantity > available:
                            pkg_note = f' (Paket: {quote_item.package.name})' if quote_item.package_id else ''
                            validation_warnings.append(
                                f'{quote_item.item.name}{pkg_note}: Nur {available} verfügbar (Angebot hat {quote_item.quantity})'
                            )

                if validation_warnings:
                    flash('⚠ Bestandswarnung: ' + '; '.join(validation_warnings), 'warning')

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
            if item.is_package:
                item_availability[item.id] = get_package_available_quantity(
                    item.id, quote.start_date, quote.end_date, exclude_quote_id=quote.id)
            else:
                item_availability[item.id] = get_available_quantity(
                    item.id, quote.start_date, quote.end_date, exclude_quote_id=quote.id)
    else:
        for item in items:
            item_availability[item.id] = item.total_quantity

    return render_template('admin/quote_edit.html', quote=quote, items=items, categories=categories, item_availability=item_availability)


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
                    multiplier = 1.0 if quote_item.discount_exempt else discount_multiplier
                    item_revenue = round(quote_item.total_price * multiplier, 2)
                    quote_item.item.total_revenue = round(quote_item.item.total_revenue + item_revenue, 2)
                    # Accumulate external rental costs
                    if quote_item.rental_cost_per_day:
                        item_cost = quote_item.total_external_cost
                        quote_item.item.total_cost = round(quote_item.item.total_cost + item_cost, 2)

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
                    multiplier = 1.0 if quote_item.discount_exempt else discount_multiplier
                    item_revenue = round(quote_item.total_price * multiplier, 2)
                    quote_item.item.total_revenue = round(quote_item.item.total_revenue - item_revenue, 2)
                    # Reverse external rental costs
                    if quote_item.rental_cost_per_day:
                        item_cost = quote_item.total_external_cost
                        quote_item.item.total_cost = round(quote_item.item.total_cost - item_cost, 2)

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
                    multiplier = 1.0 if quote_item.discount_exempt else discount_multiplier
                    item_revenue = round(quote_item.total_price * multiplier, 2)
                    quote_item.item.total_revenue = round(quote_item.item.total_revenue - item_revenue, 2)
                    # Reverse external rental costs
                    if quote_item.rental_cost_per_day:
                        item_cost = quote_item.total_external_cost
                        quote_item.item.total_cost = round(quote_item.item.total_cost - item_cost, 2)

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
                if item.is_package:
                    # Expand package into components
                    component_price_sum = item.component_price_sum
                    for pc in item.package_components:
                        if component_price_sum > 0:
                            comp_share = (pc.component_item.default_rental_price_per_day * pc.quantity) / component_price_sum
                            adjusted_price = round((item.default_rental_price_per_day * comp_share) / pc.quantity, 2)
                        else:
                            adjusted_price = 0
                        for _ in range(inq_item.quantity):
                            ext_cost_total, _ = pc.component_item.calculate_external_cost(pc.quantity)
                            ext_cost_per_unit = round(ext_cost_total / pc.quantity, 2) if pc.quantity > 0 else 0
                            qi = QuoteItem(
                                quote_id=quote.id,
                                item_id=pc.component_item_id,
                                quantity=pc.quantity,
                                rental_price_per_day=adjusted_price,
                                rental_cost_per_day=ext_cost_per_unit,
                                is_custom=False,
                                package_id=item.id
                            )
                            db.session.add(qi)
                else:
                    ext_cost_total, _ = item.calculate_external_cost(inq_item.quantity)
                    ext_cost_per_unit = round(ext_cost_total / inq_item.quantity, 2) if inq_item.quantity > 0 else 0
                    qi = QuoteItem(
                        quote_id=quote.id,
                        item_id=item.id,
                        quantity=inq_item.quantity,
                        rental_price_per_day=item.default_rental_price_per_day,
                        rental_cost_per_day=ext_cost_per_unit,
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
        # Delete ownership entries for this user
        ItemOwnership.query.filter_by(user_id=user.id).delete()
        db.session.delete(user)
        db.session.commit()
        flash(f'Benutzer "{user.username}" gelöscht. Artikelzuordnungen wurden entfernt.', 'success')
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
            settings_record.tax_number = request.form.get('tax_number', '').strip()
            settings_record.tax_mode = request.form.get('tax_mode', 'kleinunternehmer').strip()
            settings_record.payment_terms_days = int(request.form.get('payment_terms_days', '14') or 14)
            settings_record.quote_validity_days = int(request.form.get('quote_validity_days', '14') or 14)
            settings_record.shop_description = request.form.get('shop_description', '')
            settings_record.imprint_url = request.form.get('imprint_url', '').strip()
            settings_record.privacy_url = request.form.get('privacy_url', '').strip()
            settings_record.terms_and_conditions_text = request.form.get('terms_and_conditions_text', '').strip() or None
            settings_record.notification_email = request.form.get('notification_email', '').strip()
            settings_record.updated_at = datetime.utcnow()

            # Handle logo upload
            if request.form.get('remove_logo'):
                if settings_record.logo_filename:
                    old_path = os.path.join(get_upload_path(), settings_record.logo_filename)
                    if os.path.exists(old_path):
                        os.remove(old_path)
                    settings_record.logo_filename = None
            logo_file = request.files.get('logo')
            if logo_file and logo_file.filename:
                from werkzeug.utils import secure_filename as sf
                ext = os.path.splitext(logo_file.filename)[1].lower()
                if ext in ('.png', '.jpg', '.jpeg', '.svg', '.webp', '.gif'):
                    # Remove old logo
                    if settings_record.logo_filename:
                        old_path = os.path.join(get_upload_path(), settings_record.logo_filename)
                        if os.path.exists(old_path):
                            os.remove(old_path)
                    filename = f'company_logo{ext}'
                    logo_file.save(os.path.join(get_upload_path(), filename))
                    settings_record.logo_filename = filename
                else:
                    flash('Ung\u00fcltiges Logo-Format. Erlaubt: PNG, JPEG, SVG, WebP, GIF', 'error')

            db.session.commit()
            flash('Einstellungen gespeichert!', 'success')
        except Exception as e:
            db.session.rollback()
            flash(f'Fehler: {str(e)}', 'error')

    return render_template('admin/settings.html', settings=settings_record)


@admin_bp.route('/logo')
@login_required
def serve_logo():
    """Serve the uploaded company logo"""
    site_settings = SiteSettings.query.first()
    if not site_settings or not site_settings.logo_filename:
        abort(404)
    logo_path = os.path.join(get_upload_path(), site_settings.logo_filename)
    if not os.path.exists(logo_path):
        abort(404)
    return send_file(logo_path)


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

    # Separate owned and external items
    owned_items = [i for i in items if not i.is_external]
    external_items = [i for i in items if i.is_external]

    # Get all ownerships for the report
    all_ownerships = ItemOwnership.query.all()

    return render_template('admin/payoff_report.html',
                           items=items,
                           owned_items=owned_items,
                           external_items=external_items,
                           users=users,
                           all_ownerships=all_ownerships,
                           misc_revenue=misc_revenue)


@admin_bp.route('/schedule')
@login_required
def schedule():
    """Rental schedule / calendar"""
    from datetime import timedelta, date
    import calendar as cal_mod

    quotes = Quote.query.filter(
        Quote.start_date.isnot(None),
        Quote.end_date.isnot(None)
    ).order_by(Quote.start_date).all()

    # Inquiries with date ranges (not yet converted to quotes)
    inquiries = Inquiry.query.filter(
        Inquiry.desired_start_date.isnot(None),
        Inquiry.desired_end_date.isnot(None),
        Inquiry.status.in_(['new', 'contacted'])
    ).order_by(Inquiry.desired_start_date).all()

    # Calendar month from query params, default to current month
    try:
        cal_year = int(request.args.get('year', date.today().year))
        cal_month = int(request.args.get('month', date.today().month))
    except (ValueError, TypeError):
        cal_year, cal_month = date.today().year, date.today().month

    # Build calendar weeks
    first_weekday, num_days = cal_mod.monthrange(cal_year, cal_month)
    # Monday=0 … Sunday=6
    month_start = date(cal_year, cal_month, 1)
    month_end = date(cal_year, cal_month, num_days)

    # Previous / next month
    if cal_month == 1:
        prev_year, prev_month = cal_year - 1, 12
    else:
        prev_year, prev_month = cal_year, cal_month - 1
    if cal_month == 12:
        next_year, next_month = cal_year + 1, 1
    else:
        next_year, next_month = cal_year, cal_month + 1

    # Build calendar events from quotes
    cal_events = []
    for q in quotes:
        cal_events.append({
            'label': q.customer_name,
            'customer': q.customer_name,
            'start': q.start_date.date() if hasattr(q.start_date, 'date') else q.start_date,
            'end': q.end_date.date() if hasattr(q.end_date, 'date') else q.end_date,
            'status': q.status,
            'type': 'quote',
            'id': q.id,
        })
    for inq in inquiries:
        cal_events.append({
            'label': inq.customer_name,
            'customer': inq.customer_name,
            'start': inq.desired_start_date.date() if hasattr(inq.desired_start_date, 'date') else inq.desired_start_date,
            'end': inq.desired_end_date.date() if hasattr(inq.desired_end_date, 'date') else inq.desired_end_date,
            'status': 'inquiry',
            'type': 'inquiry',
            'id': inq.id,
        })

    # Build weeks grid (list of lists of 7 day-cells)
    # Each cell: {'day': int|None, 'date': date|None, 'events': [...]}
    weeks = []
    current_week = [None] * first_weekday  # padding before 1st
    for day_num in range(1, num_days + 1):
        d = date(cal_year, cal_month, day_num)
        day_events = [e for e in cal_events if e['start'] <= d <= e['end']]
        current_week.append({'day': day_num, 'date': d, 'events': day_events})
        if len(current_week) == 7:
            weeks.append(current_week)
            current_week = []
    if current_week:
        while len(current_week) < 7:
            current_week.append(None)
        weeks.append(current_week)

    return render_template('admin/schedule.html',
                           quotes=quotes, timedelta=timedelta,
                           inquiries=inquiries,
                           cal_year=cal_year, cal_month=cal_month,
                           prev_year=prev_year, prev_month=prev_month,
                           next_year=next_year, next_month=next_month,
                           weeks=weeks, today=date.today())


# ============= PDF GENERATORS =============

def _extract_common_pdf_data(quote, site_settings):
    """Extract common data used across all PDF generators."""
    issuer_name = site_settings.business_name if site_settings and site_settings.business_name else "Ihr Unternehmen"
    address_lines = [l.strip() for l in (site_settings.address_lines or '').split('\n') if l.strip()] if site_settings else []
    contact_lines_list = [l.strip() for l in (site_settings.contact_lines or '').split('\n') if l.strip()] if site_settings else []
    bank_lines_list = [l.strip() for l in (site_settings.bank_lines or '').split('\n') if l.strip()] if site_settings else []
    recipient = [l.strip() for l in (quote.recipient_lines or quote.customer_name).split('\n') if l.strip()]
    tax_number = site_settings.tax_number if site_settings else None
    tax_mode = (site_settings.tax_mode or 'kleinunternehmer') if site_settings else 'kleinunternehmer'
    payment_terms_days = (site_settings.payment_terms_days or 14) if site_settings else 14
    quote_validity_days = (site_settings.quote_validity_days or 14) if site_settings else 14

    # Logo path
    logo_path = None
    if site_settings and site_settings.logo_filename:
        lp = os.path.join(get_upload_path(), site_settings.logo_filename)
        if os.path.exists(lp):
            logo_path = lp

    # Date strings
    start_str = quote.start_date.strftime("%d.%m.%Y") if quote.start_date else None
    end_str = quote.end_date.strftime("%d.%m.%Y") if quote.end_date else None
    rental_days = quote.calculate_rental_days()

    return {
        'issuer_name': issuer_name,
        'issuer_address': address_lines,
        'contact_lines': contact_lines_list,
        'bank_lines': bank_lines_list,
        'recipient_lines': recipient,
        'tax_number': tax_number,
        'tax_mode': tax_mode,
        'payment_terms_days': payment_terms_days,
        'quote_validity_days': quote_validity_days,
        'logo_path': logo_path,
        'start_date_str': start_str,
        'end_date_str': end_str,
        'rental_days': rental_days,
    }


def _extract_positions(quote):
    """Extract positions from a quote, grouping bundle components under their package.

    Returns a list of dicts:
    - Regular item: { 'name', 'quantity', 'price_per_day', 'total', 'is_bundle': False }
    - Bundle: { 'name', 'quantity', 'price_per_day': 0, 'total', 'is_bundle': True,
                'bundle_components': [{'name', 'quantity'}] }
    """
    positions = []
    seen_package_ids = set()

    for qi in quote.quote_items:
        if qi.package_id:
            if qi.package_id in seen_package_ids:
                continue
            seen_package_ids.add(qi.package_id)
            # Gather all components for this package
            components = [q for q in quote.quote_items if q.package_id == qi.package_id]
            bundle_total = sum(c.total_price for c in components)
            bundle_qty = 1  # Packages are listed once
            # Determine package name
            pkg_name = qi.package.name if qi.package else "Paket"
            positions.append({
                'name': pkg_name,
                'quantity': bundle_qty,
                'price_per_day': 0,
                'total': bundle_total,
                'is_bundle': True,
                'bundle_components': [
                    {'name': c.display_name, 'quantity': c.quantity}
                    for c in components
                ],
            })
        else:
            positions.append({
                'name': qi.display_name,
                'quantity': qi.quantity,
                'price_per_day': qi.rental_price_per_day,
                'total': qi.total_price,
                'is_bundle': False,
            })

    return positions


def _extract_items_for_lieferschein(quote):
    """Extract items for the Lieferschein (no prices, just names and quantities)."""
    items = []
    seen_package_ids = set()

    for qi in quote.quote_items:
        if qi.package_id:
            if qi.package_id in seen_package_ids:
                continue
            seen_package_ids.add(qi.package_id)
            components = [q for q in quote.quote_items if q.package_id == qi.package_id]
            pkg_name = qi.package.name if qi.package else "Paket"
            items.append({
                'name': pkg_name,
                'quantity': 1,
                'is_bundle': True,
                'bundle_components': [
                    {'name': c.display_name, 'quantity': c.quantity}
                    for c in components
                ],
            })
        else:
            items.append({
                'name': qi.display_name,
                'quantity': qi.quantity,
                'is_bundle': False,
            })

    return items


def _send_pdf_response(pdf_bytes, filename):
    """Send a PDF response with no-cache headers."""
    response = send_file(
        BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=False,
        download_name=filename,
        max_age=0,
    )
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


# ── Angebot PDF ──

@admin_bp.route('/quotes/<int:quote_id>/angebot.pdf')
@login_required
def angebot_pdf(quote_id):
    """Generate Angebot (Quote) PDF"""
    from generators.angebot import build_angebot_pdf

    quote = Quote.query.get_or_404(quote_id)
    site_settings = SiteSettings.query.first()
    data = _extract_common_pdf_data(quote, site_settings)
    positions = _extract_positions(quote)

    pdf_bytes = build_angebot_pdf(
        issuer_name=data['issuer_name'],
        issuer_address=data['issuer_address'],
        contact_lines=data['contact_lines'],
        bank_lines=data['bank_lines'],
        tax_number=data['tax_number'],
        tax_mode=data['tax_mode'],
        logo_path=data['logo_path'],
        recipient_lines=data['recipient_lines'],
        reference_number=quote.reference_number or f"AN-{quote.id:04d}",
        start_date_str=data['start_date_str'],
        end_date_str=data['end_date_str'],
        rental_days=data['rental_days'],
        positions=positions,
        discount_percent=quote.discount_percent or 0,
        discount_label=quote.discount_label,
        discount_amount=quote.discount_amount,
        subtotal=quote.subtotal,
        total=quote.total,
        payment_terms_days=data['payment_terms_days'],
        quote_validity_days=data['quote_validity_days'],
        notes=quote.notes,
        terms_and_conditions_text=site_settings.terms_and_conditions_text if site_settings else None,
    )
    return _send_pdf_response(pdf_bytes, f"angebot_{quote.reference_number}.pdf")


# ── Rechnung PDF ──

@admin_bp.route('/quotes/<int:quote_id>/rechnung.pdf')
@login_required
def rechnung_pdf(quote_id):
    """Generate Rechnung (Invoice) PDF"""
    from generators.rechnung import build_rechnung_pdf

    quote = Quote.query.get_or_404(quote_id)
    site_settings = SiteSettings.query.first()
    data = _extract_common_pdf_data(quote, site_settings)
    positions = _extract_positions(quote)

    rechnungs_datum = quote.finalized_at.strftime("%d.%m.%Y") if quote.finalized_at else datetime.now().strftime("%d.%m.%Y")

    pdf_bytes = build_rechnung_pdf(
        issuer_name=data['issuer_name'],
        issuer_address=data['issuer_address'],
        contact_lines=data['contact_lines'],
        bank_lines=data['bank_lines'],
        tax_number=data['tax_number'],
        tax_mode=data['tax_mode'],
        logo_path=data['logo_path'],
        recipient_lines=data['recipient_lines'],
        reference_number=quote.reference_number or f"RE-{quote.id:04d}",
        rechnungs_datum=rechnungs_datum,
        start_date_str=data['start_date_str'],
        end_date_str=data['end_date_str'],
        rental_days=data['rental_days'],
        positions=positions,
        discount_percent=quote.discount_percent or 0,
        discount_label=quote.discount_label,
        discount_amount=quote.discount_amount,
        subtotal=quote.subtotal,
        total=quote.total,
        payment_terms_days=data['payment_terms_days'],
        notes=quote.notes,
    )
    return _send_pdf_response(pdf_bytes, f"rechnung_{quote.reference_number}.pdf")


# ── Lieferschein PDF ──

@admin_bp.route('/quotes/<int:quote_id>/lieferschein.pdf')
@login_required
def lieferschein_pdf(quote_id):
    """Generate Lieferschein (Delivery Note / Handover Protocol) PDF"""
    from generators.lieferschein import build_lieferschein_pdf

    quote = Quote.query.get_or_404(quote_id)
    site_settings = SiteSettings.query.first()
    data = _extract_common_pdf_data(quote, site_settings)
    items = _extract_items_for_lieferschein(quote)

    # Kaution from query param (optional)
    kaution = request.args.get('kaution', None, type=float)

    pdf_bytes = build_lieferschein_pdf(
        issuer_name=data['issuer_name'],
        issuer_address=data['issuer_address'],
        contact_lines=data['contact_lines'],
        bank_lines=data['bank_lines'],
        tax_number=data['tax_number'],
        logo_path=data['logo_path'],
        recipient_lines=data['recipient_lines'],
        reference_number=quote.reference_number or f"LS-{quote.id:04d}",
        start_date_str=data['start_date_str'],
        end_date_str=data['end_date_str'],
        items=items,
        kaution=kaution,
        notes=quote.notes,
    )
    return _send_pdf_response(pdf_bytes, f"lieferschein_{quote.reference_number}.pdf")


# ── Legacy PDF generators (kept for backwards compatibility) ──

# ============= CUSTOMER DATABASE =============

@admin_bp.route('/api/customers/search')
@login_required
def customer_search():
    """Search saved customers by name (for autocomplete)"""
    q = request.args.get('q', '').strip()
    if len(q) < 1:
        return jsonify([])
    customers = Customer.query.filter(Customer.name.ilike(f'%{q}%')).order_by(Customer.name).limit(10).all()
    return jsonify([{'name': c.name, 'recipient_lines': c.recipient_lines or ''} for c in customers])


@admin_bp.route('/api/customers/save', methods=['POST'])
@login_required
def customer_save():
    """Save or update a customer entry (identified by name)"""
    data = request.get_json()
    name = (data.get('name') or '').strip()
    recipient_lines = (data.get('recipient_lines') or '').strip()

    if not name:
        return jsonify({'error': 'Name ist erforderlich.'}), 400

    customer = Customer.query.filter(Customer.name.ilike(name)).first()
    if customer:
        customer.recipient_lines = recipient_lines
        customer.name = name  # preserve exact casing from latest save
        action = 'updated'
    else:
        customer = Customer(name=name, recipient_lines=recipient_lines)
        db.session.add(customer)
        action = 'created'

    db.session.commit()
    return jsonify({'status': 'ok', 'action': action, 'name': customer.name})


@admin_bp.route('/api/customers/delete', methods=['POST'])
@login_required
def customer_delete():
    """Delete a saved customer by name"""
    data = request.get_json()
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'Name ist erforderlich.'}), 400
    customer = Customer.query.filter(Customer.name.ilike(name)).first()
    if not customer:
        return jsonify({'error': 'Kunde nicht gefunden.'}), 404
    db.session.delete(customer)
    db.session.commit()
    return jsonify({'status': 'ok', 'name': name})
