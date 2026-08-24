from io import BytesIO
from flask import Blueprint, render_template, request, redirect, url_for, flash, send_file, jsonify, abort
from flask_login import login_required, current_user
from models import db, User, Item, Category, Quote, QuoteItem, Inquiry, InquiryItem, SiteSettings, Customer, PackageComponent, ItemUnit, Supplier, ItemSupply, QuoteItemSource, QuoteItemUnit
from helpers import get_available_quantity, get_package_available_quantity, get_own_stock_available, get_upload_path, allowed_image_file
from datetime import datetime
from functools import wraps
import os
import re
import uuid
import zipfile

admin_bp = Blueprint('admin', __name__)


def _effective_tax_mode_and_rate(site_settings):
    """Return (tax_mode, tax_rate) from local SiteSettings."""
    local_mode = (site_settings.tax_mode or 'kleinunternehmer') if site_settings else 'kleinunternehmer'
    local_rate = (site_settings.tax_rate if site_settings and site_settings.tax_rate else 19.0)
    return local_mode, float(local_rate)


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
    """Decorator: any logged-in staff user"""
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
    active_quotes = Quote.query.filter(Quote.status == 'draft').count()
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
                parent_id = request.form.get('parent_id', type=int) or None
                if name:
                    # Handle image upload
                    image_filename = None
                    if 'image' in request.files:
                        file = request.files['image']
                        if file and file.filename and allowed_image_file(file.filename):
                            ext = file.filename.rsplit('.', 1)[1].lower()
                            image_filename = f"{uuid.uuid4().hex}.{ext}"
                            file.save(os.path.join(get_upload_path(), image_filename))
                    cat = Category(name=name, display_order=order, parent_id=parent_id, image_filename=image_filename)
                    db.session.add(cat)
                    db.session.commit()
                    flash(f'Kategorie "{name}" erstellt.', 'success')
            elif action == 'edit':
                cat_id = request.form.get('category_id', type=int)
                cat = Category.query.get_or_404(cat_id)
                cat.name = request.form.get('name', '').strip()
                cat.display_order = request.form.get('display_order', 0, type=int)
                new_parent_id = request.form.get('parent_id', type=int) or None
                # Prevent circular references
                if new_parent_id:
                    descendant_ids = cat.all_descendant_ids()
                    if new_parent_id in descendant_ids:
                        flash('Kann keine Unterkategorie von sich selbst sein.', 'error')
                        return redirect(url_for('admin.categories'))
                cat.parent_id = new_parent_id
                # Handle image
                if 'image' in request.files:
                    file = request.files['image']
                    if file and file.filename and allowed_image_file(file.filename):
                        if cat.image_filename:
                            old_path = os.path.join(get_upload_path(), cat.image_filename)
                            if os.path.exists(old_path):
                                os.remove(old_path)
                        ext = file.filename.rsplit('.', 1)[1].lower()
                        cat.image_filename = f"{uuid.uuid4().hex}.{ext}"
                        file.save(os.path.join(get_upload_path(), cat.image_filename))
                if request.form.get('remove_image') == 'on' and cat.image_filename:
                    old_path = os.path.join(get_upload_path(), cat.image_filename)
                    if os.path.exists(old_path):
                        os.remove(old_path)
                    cat.image_filename = None
                db.session.commit()
                flash(f'Kategorie "{cat.name}" aktualisiert.', 'success')
            elif action == 'delete':
                cat_id = request.form.get('category_id', type=int)
                cat = Category.query.get_or_404(cat_id)
                # Re-parent children to this category's parent
                for child in cat.children:
                    child.parent_id = cat.parent_id
                # Unassign items from this category
                Item.query.filter_by(category_id=cat_id).update({'category_id': None})
                # Remove image
                if cat.image_filename:
                    old_path = os.path.join(get_upload_path(), cat.image_filename)
                    if os.path.exists(old_path):
                        os.remove(old_path)
                db.session.delete(cat)
                db.session.commit()
                flash('Kategorie gelöscht.', 'success')
        except Exception as e:
            db.session.rollback()
            flash(f'Fehler: {str(e)}', 'error')

    cats = Category.query.order_by(Category.display_order, Category.name).all()
    category_tree = Category.get_tree(cats)
    return render_template('admin/categories.html', categories=cats, category_tree=category_tree)


# ============= INVENTORY =============

@admin_bp.route('/inventory')
@login_required
def inventory_list():
    """List all inventory items with filters, stats and today's availability"""
    from datetime import date as _date
    from sqlalchemy import and_, or_

    filter_category = request.args.get('category', type=int)
    filter_supplier = request.args.get('supplier', type=int)
    filter_status = request.args.get('status', '').strip()

    items = Item.query.all()
    categories = Category.query.order_by(Category.display_order, Category.name).all()
    category_tree = Category.get_tree(categories)
    suppliers = Supplier.query.order_by(Supplier.name).all()

    # ── Stats (computed over ALL items, before filtering) ──
    total_units = sum(i.total_quantity for i in items if not i.is_package and i.total_quantity > 0)
    defect_units = sum(i.defect_count for i in items)
    inspection_due = sum(len(i.inspection_due_units()) for i in items)
    hidden_items = sum(1 for i in items if not i.visible_in_shop)
    stats = {
        'total_items': len(items),
        'total_units': total_units,
        'defect_units': defect_units,
        'inspection_due': inspection_due,
        'hidden_items': hidden_items,
    }

    # ── Booked quantities for today (single query pass) ──
    today = datetime.combine(_date.today(), datetime.min.time())
    overlapping_quotes = Quote.query.filter(
        Quote.status == 'draft',
        Quote.start_date.isnot(None),
        Quote.end_date.isnot(None),
        Quote.start_date <= today,
        Quote.end_date >= today,
    ).all()
    booked_today = {}
    for q in overlapping_quotes:
        for qi in q.quote_items:
            if qi.is_custom or qi.is_heading or qi.is_optional or not qi.item_id:
                continue
            booked_today[qi.item_id] = booked_today.get(qi.item_id, 0) + qi.quantity

    available_today = {}
    for item in items:
        if item.is_package:
            continue
        op = item.operational_quantity
        if op == -1:
            available_today[item.id] = -1
        else:
            available_today[item.id] = max(0, op - booked_today.get(item.id, 0))

    # ── Filters ──
    if filter_category:
        cat = next((c for c in categories if c.id == filter_category), None)
        if cat:
            cat_ids = cat.all_descendant_ids()
            items = [i for i in items
                     if i.category_id in cat_ids
                     or any(sc.id in cat_ids for sc in i.subcategories)]
    if filter_supplier:
        items = [i for i in items if any(s.supplier_id == filter_supplier for s in i.supplies)]
    if filter_status == 'defect':
        items = [i for i in items if i.defect_count > 0]
    elif filter_status == 'inspection_due':
        items = [i for i in items if i.inspection_due_units()]
    elif filter_status == 'hidden':
        items = [i for i in items if not i.visible_in_shop]
    elif filter_status == 'external':
        items = [i for i in items if i.is_external]
    elif filter_status == 'packages':
        items = [i for i in items if i.is_package]
    elif filter_status == 'unavailable':
        items = [i for i in items if not i.is_package and available_today.get(i.id) == 0]

    # ── Hierarchical category sort ──
    cat_order = {cat.id: idx for idx, (cat, depth) in enumerate(category_tree)}
    items.sort(key=lambda item: (cat_order.get(item.category_id, len(cat_order)), item.name))

    return render_template('admin/inventory_list.html',
                           items=items,
                           categories=categories,
                           category_tree=category_tree,
                           suppliers=suppliers,
                           stats=stats,
                           available_today=available_today,
                           filter_category=filter_category,
                           filter_supplier=filter_supplier,
                           filter_status=filter_status)


# ============= INVENTORY =============

def _parse_float_or_none(val):
    """Parse a form value to float, accepting comma decimals. Empty -> None."""
    s = (val or '').strip().replace(',', '.')
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _apply_item_specs(item, form):
    """Apply technical spec / logistics fields from a form to an item."""
    item.manufacturer = form.get('manufacturer', '').strip() or None
    item.model_name = form.get('model_name', '').strip() or None
    item.weight_kg = _parse_float_or_none(form.get('weight_kg'))
    item.power_watts = _parse_float_or_none(form.get('power_watts'))
    item.dimensions = form.get('dimensions', '').strip() or None
    item.storage_location = form.get('storage_location', '').strip() or None
    item.replacement_value = _parse_float_or_none(form.get('replacement_value'))


def _parse_supplies_form(form):
    """Parse supplier offer rows from the inventory form.
    Returns list of dicts. Raises ValueError on invalid input."""
    supplier_ids = form.getlist('supply_supplier_ids', type=int)
    quantities = form.getlist('supply_quantities', type=int)
    prices = form.getlist('supply_prices')
    brutto_flags = form.getlist('supply_price_is_brutto')
    result = []
    seen = set()
    for i, sid in enumerate(supplier_ids):
        if not sid or sid in seen:
            continue
        seen.add(sid)
        qty = quantities[i] if i < len(quantities) else 0
        price = _parse_float_or_none(prices[i] if i < len(prices) else '')
        if price is None:
            raise ValueError('Jeder Lieferanten-Eintrag benötigt einen Preis/Tag.')
        is_brutto = (brutto_flags[i] == '1') if i < len(brutto_flags) else True
        result.append({'supplier_id': sid, 'quantity': qty,
                       'price_per_day': round(price, 2), 'price_is_brutto': is_brutto})
    return result


@admin_bp.route('/inventory/add', methods=['GET', 'POST'])
@login_required
def inventory_add():
    """Add new inventory item"""
    categories = Category.query.order_by(Category.display_order, Category.name).all()
    category_tree = Category.get_tree(categories)
    suppliers = Supplier.query.order_by(Supplier.name).all()

    if request.method == 'POST':
        try:
            name = request.form.get('name', '').strip()
            default_rental_price = float(request.form.get('default_rental_price', 0))
            description = request.form.get('description', '').strip()
            category_id = request.form.get('category_id', type=int) or None
            show_price = request.form.get('show_price_publicly') == 'on'
            visible = request.form.get('visible_in_shop') == 'on'
            is_package = request.form.get('is_package') == 'on'
            show_bundle_discount = request.form.get('show_bundle_discount') == 'on'
            stock_quantity = request.form.get('stock_quantity', type=int)
            if stock_quantity is None or stock_quantity < -1:
                stock_quantity = 0

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
                is_package=is_package,
                show_bundle_discount=show_bundle_discount,
                stock_quantity=0 if is_package else stock_quantity,
            )
            _apply_item_specs(item, request.form)

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
                # Supplier offers
                for s in _parse_supplies_form(request.form):
                    db.session.add(ItemSupply(item_id=item.id, **s))

            db.session.commit()
            flash(f'{name} erfolgreich hinzugefügt!', 'success')
            return redirect(url_for('admin.inventory_edit', item_id=item.id))

        except Exception as e:
            db.session.rollback()
            flash(f'Fehler beim Hinzufügen des Artikels: {str(e)}', 'error')

    return render_template('admin/inventory_form.html',
                           item=None,
                           categories=categories,
                           category_tree=category_tree,
                           suppliers=suppliers,
                           all_items=Item.query.filter_by(is_package=False).order_by(Item.name).all())


@admin_bp.route('/inventory/<int:item_id>/edit', methods=['GET', 'POST'])
@login_required
def inventory_edit(item_id):
    """Edit inventory item"""
    item = Item.query.get_or_404(item_id)
    categories = Category.query.order_by(Category.display_order, Category.name).all()
    category_tree = Category.get_tree(categories)
    suppliers = Supplier.query.order_by(Supplier.name).all()

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
            item.show_bundle_discount = request.form.get('show_bundle_discount') == 'on'
            _apply_item_specs(item, request.form)

            if item.is_package:
                # Packages carry no stock or supplier offers
                item.stock_quantity = 0
                ItemSupply.query.filter_by(item_id=item.id).delete()

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
                # Own stock
                stock_quantity = request.form.get('stock_quantity', type=int)
                if stock_quantity is None or stock_quantity < -1:
                    stock_quantity = 0
                item.stock_quantity = stock_quantity

                # Supplier offers: rebuild from form
                ItemSupply.query.filter_by(item_id=item.id).delete()
                for s in _parse_supplies_form(request.form):
                    db.session.add(ItemSupply(item_id=item.id, **s))

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
            return redirect(url_for('admin.inventory_edit', item_id=item.id))

        except Exception as e:
            db.session.rollback()
            flash(f'Fehler beim Aktualisieren des Artikels: {str(e)}', 'error')

    # Upcoming / current bookings for this item (availability overview)
    from datetime import date as _date
    _today = datetime.combine(_date.today(), datetime.min.time())
    upcoming_bookings = []
    if not item.is_package:
        booking_rows = db.session.query(QuoteItem, Quote).join(Quote).filter(
            QuoteItem.item_id == item.id,
            QuoteItem.is_custom == False,
            Quote.start_date.isnot(None),
            Quote.end_date.isnot(None),
            Quote.end_date >= _today,
            Quote.status == 'draft'
        ).order_by(Quote.start_date).all()
        upcoming_bookings = [{'quote': q, 'quantity': qi.quantity} for qi, q in booking_rows]

    return render_template('admin/inventory_form.html',
                           item=item,
                           categories=categories,
                           category_tree=category_tree,
                           suppliers=suppliers,
                           upcoming_bookings=upcoming_bookings,
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


@admin_bp.route('/inventory/bulk', methods=['POST'])
@login_required
def inventory_bulk():
    """Apply a bulk action to multiple inventory items"""
    def _back():
        ref = request.referrer
        if ref and ref.startswith(request.host_url):
            return redirect(ref)
        return redirect(url_for('admin.inventory_list'))

    action = request.form.get('action', '').strip()
    item_ids = request.form.getlist('item_ids', type=int)

    if not item_ids:
        flash('Keine Artikel ausgewählt.', 'error')
        return _back()

    items = Item.query.filter(Item.id.in_(item_ids)).all()
    editable = [i for i in items if current_user.can_edit_item(i)]
    skipped = len(items) - len(editable)

    try:
        count = len(editable)
        if action == 'show_in_shop':
            for i in editable:
                i.visible_in_shop = True
            msg = f'{count} Artikel im Shop sichtbar gemacht.'
        elif action == 'hide_from_shop':
            for i in editable:
                i.visible_in_shop = False
            msg = f'{count} Artikel im Shop ausgeblendet.'
        elif action == 'show_price':
            for i in editable:
                i.show_price_publicly = True
            msg = f'Preis wird bei {count} Artikeln öffentlich angezeigt.'
        elif action == 'hide_price':
            for i in editable:
                i.show_price_publicly = False
            msg = f'{count} Artikel auf „Preis auf Anfrage" gesetzt.'
        elif action == 'set_category':
            category_id = request.form.get('bulk_category_id', type=int) or None
            if category_id and not Category.query.get(category_id):
                raise ValueError('Kategorie nicht gefunden.')
            for i in editable:
                i.category_id = category_id
            cat_name = Category.query.get(category_id).name if category_id else 'Ohne Kategorie'
            msg = f'{count} Artikel nach „{cat_name}" verschoben.'
        elif action == 'set_storage':
            location = request.form.get('bulk_storage_location', '').strip() or None
            for i in editable:
                i.storage_location = location
            msg = f'Lagerort für {count} Artikel {"gesetzt" if location else "entfernt"}.'
        elif action == 'adjust_price':
            mode = request.form.get('bulk_price_mode', 'percent')
            value = _parse_float_or_none(request.form.get('bulk_price_value'))
            if value is None:
                raise ValueError('Bitte einen Wert für die Preisanpassung angeben.')
            for i in editable:
                if mode == 'set':
                    i.default_rental_price_per_day = round(max(0, value), 2)
                else:  # percent
                    i.default_rental_price_per_day = round(
                        max(0, i.default_rental_price_per_day * (1 + value / 100)), 2)
            msg = (f'Preis bei {count} Artikeln auf €{value:.2f} gesetzt.' if mode == 'set'
                   else f'Preise bei {count} Artikeln um {value:+g}% angepasst.')
        elif action == 'delete':
            for i in editable:
                if i.image_filename:
                    old_path = os.path.join(get_upload_path(), i.image_filename)
                    if os.path.exists(old_path):
                        os.remove(old_path)
                PackageComponent.query.filter_by(component_item_id=i.id).delete()
                db.session.delete(i)
            msg = f'{count} Artikel gelöscht.'
        else:
            flash('Unbekannte Aktion.', 'error')
            return _back()

        db.session.commit()
        if skipped:
            msg += f' ({skipped} ohne Berechtigung übersprungen.)'
        flash(msg, 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Fehler bei der Massenaktion: {str(e)}', 'error')

    return _back()


# ============= ITEM UNITS (serial number / asset tracking) =============

def _parse_date_or_none(val):
    """Parse a YYYY-MM-DD form value to a date. Empty/invalid -> None."""
    s = (val or '').strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, '%Y-%m-%d').date()
    except ValueError:
        return None


@admin_bp.route('/inventory/<int:item_id>/units/add', methods=['POST'])
@login_required
def unit_add(item_id):
    """Add one or more physical units to an item"""
    item = Item.query.get_or_404(item_id)
    if not current_user.can_edit_item(item):
        flash('Sie haben keine Berechtigung, diesen Artikel zu bearbeiten.', 'error')
        return redirect(url_for('admin.inventory_list'))

    try:
        count = max(1, min(request.form.get('count', 1, type=int) or 1, 100))
        serial = request.form.get('serial_number', '').strip() or None
        for _ in range(count):
            unit = ItemUnit(
                item_id=item.id,
                serial_number=serial if count == 1 else None,
                status=ItemUnit.STATUS_AVAILABLE,
            )
            db.session.add(unit)
            db.session.flush()
            unit.generate_asset_tag()
        db.session.commit()
        flash(f'{count} Einheit(en) hinzugefügt.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Fehler beim Hinzufügen der Einheit: {str(e)}', 'error')

    return redirect(url_for('admin.inventory_edit', item_id=item.id) + '#units')


@admin_bp.route('/inventory/units/<int:unit_id>/update', methods=['POST'])
@login_required
def unit_update(unit_id):
    """Update a single unit (serial, status, dates, notes)"""
    unit = ItemUnit.query.get_or_404(unit_id)
    item = unit.item
    if not current_user.can_edit_item(item):
        flash('Sie haben keine Berechtigung, diesen Artikel zu bearbeiten.', 'error')
        return redirect(url_for('admin.inventory_list'))

    try:
        unit.serial_number = request.form.get('serial_number', '').strip() or None
        status = request.form.get('status', '').strip()
        if status in ItemUnit.STATUS_LABELS:
            unit.status = status
        unit.purchase_date = _parse_date_or_none(request.form.get('purchase_date'))
        unit.last_inspection_date = _parse_date_or_none(request.form.get('last_inspection_date'))
        unit.next_inspection_date = _parse_date_or_none(request.form.get('next_inspection_date'))
        unit.notes = request.form.get('notes', '').strip() or None
        db.session.commit()
        flash(f'Einheit {unit.asset_tag or unit.id} aktualisiert.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Fehler beim Aktualisieren der Einheit: {str(e)}', 'error')

    return redirect(url_for('admin.inventory_edit', item_id=item.id) + '#units')


@admin_bp.route('/inventory/units/<int:unit_id>/delete', methods=['POST'])
@login_required
def unit_delete(unit_id):
    """Delete a unit"""
    unit = ItemUnit.query.get_or_404(unit_id)
    item = unit.item
    if not current_user.can_edit_item(item):
        flash('Sie haben keine Berechtigung, diesen Artikel zu bearbeiten.', 'error')
        return redirect(url_for('admin.inventory_list'))

    try:
        db.session.delete(unit)
        db.session.commit()
        flash('Einheit gelöscht.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Fehler beim Löschen der Einheit: {str(e)}', 'error')

    return redirect(url_for('admin.inventory_edit', item_id=item.id) + '#units')


def _unit_qr_data_uri(unit):
    """Generate an SVG data URI QR code linking to the unit lookup URL."""
    try:
        import segno
    except ImportError:
        return None
    lookup_url = url_for('admin.unit_lookup', asset_tag=unit.asset_tag or str(unit.id), _external=True)
    qr = segno.make(lookup_url, error='m')
    return qr.svg_data_uri(scale=4, border=1)


@admin_bp.route('/inventory/units/<int:unit_id>/label')
@login_required
def unit_label(unit_id):
    """Printable QR label for a single unit"""
    unit = ItemUnit.query.get_or_404(unit_id)
    labels = [{'unit': unit, 'qr': _unit_qr_data_uri(unit)}]
    return render_template('admin/unit_labels.html', item=unit.item, labels=labels, single_unit=unit)


@admin_bp.route('/inventory/<int:item_id>/labels')
@login_required
def unit_labels(item_id):
    """Printable QR label sheet for all units of an item"""
    item = Item.query.get_or_404(item_id)
    labels = [{'unit': u, 'qr': _unit_qr_data_uri(u)} for u in item.units
              if u.status != ItemUnit.STATUS_RETIRED]
    return render_template('admin/unit_labels.html', item=item, labels=labels)


def _label_font(size, bold=False):
    """Best available TTF font (macOS / Linux), falling back to Pillow's built-in."""
    from PIL import ImageFont
    candidates = [
        '/System/Library/Fonts/Supplemental/Arial Bold.ttf' if bold else '/System/Library/Fonts/Supplemental/Arial.ttf',
        '/System/Library/Fonts/Helvetica.ttc',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf' if bold else '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
        '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf' if bold else '/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf',
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _unit_label_png_bytes(unit):
    """High-res label PNG (QR + item name + asset tag + serial), proportioned
    for 24mm P-touch tape. Import in P-touch Editor via Einfügen → Bild."""
    import segno
    from PIL import Image, ImageDraw

    H = 600  # image height; scaled down by P-touch Editor to tape height
    MARGIN = 24

    lookup_url = url_for('admin.unit_lookup', asset_tag=unit.asset_tag or str(unit.id), _external=True)
    qr = segno.make(lookup_url, error='m')
    n = qr.symbol_size(scale=1, border=2)[0]   # modules incl. quiet zone
    scale = max(1, H // n)
    qr_buf = BytesIO()
    qr.save(qr_buf, kind='png', scale=scale, border=2)
    qr_buf.seek(0)
    qr_img = Image.open(qr_buf).convert('1')

    name_font = _label_font(64, bold=True)
    tag_font = _label_font(120, bold=True)
    sn_font = _label_font(52)

    name = unit.item.name if unit.item else ''
    tag = unit.asset_tag or f'#{unit.id}'
    sn = f'SN: {unit.serial_number}' if unit.serial_number else None

    # Measure text to size the canvas
    probe = ImageDraw.Draw(Image.new('1', (1, 1)))
    lines = [(name, name_font), (tag, tag_font)] + ([(sn, sn_font)] if sn else [])
    text_w = max(int(probe.textbbox((0, 0), t, font=f)[2]) for t, f in lines)
    width = H + MARGIN + text_w + MARGIN * 2

    img = Image.new('1', (width, H), 1)  # 1-bit, white — ideal for thermal tape
    img.paste(qr_img, ((H - qr_img.width) // 2, (H - qr_img.height) // 2))
    draw = ImageDraw.Draw(img)

    x = H + MARGIN
    total_text_h = sum(int(probe.textbbox((0, 0), t, font=f)[3]) for t, f in lines) + (len(lines) - 1) * 20
    y = max(MARGIN, (H - total_text_h) // 2)
    for text, font in lines:
        draw.text((x, y), text, font=font, fill=0)
        y += int(probe.textbbox((0, 0), text, font=font)[3]) + 20

    out = BytesIO()
    img.save(out, format='PNG', dpi=(360, 360))
    return out.getvalue()


def _safe_filename(base):
    return re.sub(r'[^A-Za-z0-9_\-]+', '_', base).strip('_') or 'label'


@admin_bp.route('/inventory/units/<int:unit_id>/label.png')
@login_required
def unit_label_png(unit_id):
    """Download a single unit label as PNG (for P-touch Editor import)."""
    unit = ItemUnit.query.get_or_404(unit_id)
    png = _unit_label_png_bytes(unit)
    fname = _safe_filename(unit.asset_tag or f'unit-{unit.id}') + '.png'
    return send_file(BytesIO(png), mimetype='image/png', as_attachment=True, download_name=fname)


@admin_bp.route('/inventory/<int:item_id>/labels.zip')
@login_required
def unit_labels_zip(item_id):
    """Download all unit labels of an item as a ZIP of PNGs."""
    item = Item.query.get_or_404(item_id)
    units = [u for u in item.units if u.status != ItemUnit.STATUS_RETIRED]
    if not units:
        flash('Keine Einheiten vorhanden.', 'error')
        return redirect(url_for('admin.inventory_edit', item_id=item.id) + '#units')
    buf = BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for u in units:
            fname = _safe_filename(u.asset_tag or f'unit-{u.id}') + '.png'
            zf.writestr(fname, _unit_label_png_bytes(u))
    buf.seek(0)
    zip_name = 'etiketten-' + _safe_filename(item.name) + '.zip'
    return send_file(buf, mimetype='application/zip', as_attachment=True, download_name=zip_name)


# ── Brother P-touch .lbx export (experimental) ─────────────────────────
# Format reverse-engineered from P-touch Editor output; references:
# github.com/orochi235/bil-lbx (MIT) and github.com/Sibyx/griminventory-api.
# An .lbx is a ZIP (STORE) of label.xml + prop.xml. Sized for 24mm TZe tape
# (68pt across, format code 261). Objects are native (editable in the editor).

def _lbx_esc(s):
    from xml.sax.saxutils import escape
    return escape(str(s), {'"': '&quot;'})


def _lbx_object_style(x, y, w, h, pen_style='NULL', name='Object'):
    return (
        f'<pt:objectStyle x="{x:g}pt" y="{y:g}pt" width="{w:g}pt" height="{h:g}pt" '
        f'backColor="#FFFFFF" backPrintColorNumber="0" ropMode="COPYPEN" angle="0" anchor="TOPLEFT" flip="NONE">'
        f'<pt:pen style="{pen_style}" widthX="0.5pt" widthY="0.5pt" color="#000000" printColorNumber="1"/>'
        f'<pt:brush style="NULL" color="#000000" printColorNumber="1" id="0"/>'
        f'<pt:expanded objectName="{name}" ID="0" lock="0" templateMergeTarget="LABELLIST" '
        f'templateMergeType="NONE" templateMergeID="0" linkStatus="NONE" linkID="0"/>'
        f'</pt:objectStyle>'
    )


def _lbx_text(name, text, x, y, w, h, size, weight=400):
    font_info = (
        f'<text:ptFontInfo>'
        f'<text:logFont name="Helsinki" width="0" italic="false" weight="{weight}" charSet="0" pitchAndFamily="2"/>'
        f'<text:fontExt effect="NOEFFECT" underline="0" strikeout="0" size="{size:g}pt" orgSize="28.8pt" '
        f'textColor="#000000" textPrintColorNumber="1"/>'
        f'</text:ptFontInfo>'
    )
    return (
        f'<text:text>'
        + _lbx_object_style(x, y, w, h, name=name)
        + font_info
        + '<text:textControl control="AUTOLEN" clipFrame="false" aspectNormal="true" shrink="true" autoLF="false" avoidImage="false"/>'
        + '<text:textAlign horizontalAlignment="LEFT" verticalAlignment="CENTER" inLineAlignment="BASELINE"/>'
        + f'<text:textStyle vertical="false" nullBlock="false" charSpace="0" lineSpace="0" orgPoint="{size:g}pt" combinedChars="false"/>'
        + f'<pt:data>{_lbx_esc(text)}</pt:data>'
        + f'<text:stringItem charLen="{len(text)}">{font_info}</text:stringItem>'
        + '</text:text>'
    )


def _lbx_qrcode(data, x, y, size_pt, cell_size=1.4):
    return (
        f'<barcode:barcode>'
        + _lbx_object_style(x, y, size_pt, size_pt, pen_style='INSIDEFRAME', name='Barcode1')
        + '<barcode:barcodeStyle protocol="QRCODE" lengths="0" zeroFill="false" barWidth="0.8pt" barRatio="1:3" '
          'humanReadable="false" humanReadableAlignment="LEFT" checkDigit="false" autoLengths="true" '
          'margin="true" sameLengthBar="false" bearerBar="false"/>'
        + f'<barcode:qrcodeStyle model="2" eccLevel="15%" cellSize="{cell_size:g}pt" mbcs="65001" '
          'removeCharKind="0" removeCharString="" joint="1" jointSpace="8" jointVertically="false" '
          'version="auto" changeVersionDrag="false"/>'
        + f'<pt:data>{_lbx_esc(data)}</pt:data>'
        + '</barcode:barcode>'
    )


def _unit_label_lbx_bytes(unit):
    """Native P-touch Editor .lbx file for one unit: QR + name + tag + serial,
    24mm tape, auto length. Objects stay editable in the editor."""
    TAPE_W = 68          # 24mm tape in pt
    FORMAT = 261         # Brother format code for 24mm
    END_MARGIN = 5.6     # unprintable 2mm leader/trailer
    SIDE_MARGIN = 2.8

    lookup_url = url_for('admin.unit_lookup', asset_tag=unit.asset_tag or str(unit.id), _external=True)
    name = unit.item.name if unit.item else ''
    tag = unit.asset_tag or f'#{unit.id}'
    sn = f'SN: {unit.serial_number}' if unit.serial_number else None

    qr_size = 59.0
    text_x = END_MARGIN + qr_size + 6
    objects = [_lbx_qrcode(lookup_url, END_MARGIN, 4.4, qr_size)]
    text_widths = []

    def add_text(obj_name, text, y, h, size, weight=400):
        w = max(30.0, len(text) * size * 0.62)
        text_widths.append(w)
        objects.append(_lbx_text(obj_name, text, text_x, y, w, h, size, weight))

    add_text('Text1', name, 6, 11, 8)
    add_text('Text2', tag, 19, 28, 24, weight=700)
    if sn:
        add_text('Text3', sn, 50, 10, 7)

    length = text_x + max(text_widths) + 8
    bg_w = length - 2 * END_MARGIN
    bg_h = TAPE_W - 2 * SIDE_MARGIN

    label_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<pt:document xmlns:pt="http://schemas.brother.info/ptouch/2007/lbx/main" '
        'xmlns:style="http://schemas.brother.info/ptouch/2007/lbx/style" '
        'xmlns:text="http://schemas.brother.info/ptouch/2007/lbx/text" '
        'xmlns:draw="http://schemas.brother.info/ptouch/2007/lbx/draw" '
        'xmlns:image="http://schemas.brother.info/ptouch/2007/lbx/image" '
        'xmlns:barcode="http://schemas.brother.info/ptouch/2007/lbx/barcode" '
        'xmlns:database="http://schemas.brother.info/ptouch/2007/lbx/database" '
        'xmlns:table="http://schemas.brother.info/ptouch/2007/lbx/table" '
        'xmlns:cable="http://schemas.brother.info/ptouch/2007/lbx/cable" '
        'version="1.10" generator="erp-rent">'
        '<pt:body currentSheet="Sheet 1" direction="LTR">'
        '<style:sheet name="Sheet 1">'
        f'<style:paper media="0" width="{TAPE_W}pt" height="2834.4pt" '
        f'marginLeft="{SIDE_MARGIN}pt" marginTop="{END_MARGIN}pt" marginRight="{SIDE_MARGIN}pt" marginBottom="{END_MARGIN}pt" '
        f'orientation="landscape" autoLength="true" monochromeDisplay="true" printColorDisplay="false" '
        f'printColorsID="0" paperColor="#FFFFFF" paperInk="#000000" split="1" format="{FORMAT}" '
        f'backgroundTheme="0" printerID="30256" printerName="Brother PT-P700"/>'
        '<style:cutLine regularCut="0pt" freeCut=""/>'
        f'<style:backGround x="{END_MARGIN}pt" y="{SIDE_MARGIN}pt" width="{bg_w:g}pt" height="{bg_h:g}pt" '
        'brushStyle="NULL" brushId="0" userPattern="NONE" userPatternId="0" color="#000000" '
        'printColorNumber="1" backColor="#FFFFFF" backPrintColorNumber="0"/>'
        '<pt:objects>' + ''.join(objects) + '</pt:objects>'
        '</style:sheet>'
        '</pt:body>'
        '</pt:document>'
    )

    now = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
    prop_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<meta:properties xmlns:meta="http://schemas.brother.info/ptouch/2007/lbx/meta" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/">'
        '<meta:appName>P-touch Editor</meta:appName>'
        f'<dc:title>{_lbx_esc(tag)}</dc:title>'
        '<dc:subject></dc:subject>'
        '<dc:creator>erp-rent</dc:creator>'
        '<meta:keyword></meta:keyword>'
        '<dc:description></dc:description>'
        '<meta:template></meta:template>'
        f'<dcterms:created>{now}</dcterms:created>'
        f'<dcterms:modified>{now}</dcterms:modified>'
        '<meta:lastPrinted></meta:lastPrinted>'
        '<meta:modifiedBy></meta:modifiedBy>'
        '<meta:revision>1</meta:revision>'
        '<meta:editTime>0</meta:editTime>'
        '<meta:numPages>1</meta:numPages>'
        '<meta:numWords>0</meta:numWords>'
        '<meta:numChars>0</meta:numChars>'
        '<meta:security>0</meta:security>'
        '</meta:properties>'
    )

    buf = BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_STORED) as zf:
        zf.writestr('label.xml', label_xml)
        zf.writestr('prop.xml', prop_xml)
    return buf.getvalue()


@admin_bp.route('/inventory/units/<int:unit_id>/label.lbx')
@login_required
def unit_label_lbx(unit_id):
    """Download a single unit label as P-touch Editor .lbx file (experimental)."""
    unit = ItemUnit.query.get_or_404(unit_id)
    data = _unit_label_lbx_bytes(unit)
    fname = _safe_filename(unit.asset_tag or f'unit-{unit.id}') + '.lbx'
    return send_file(BytesIO(data), mimetype='application/octet-stream', as_attachment=True, download_name=fname)


@admin_bp.route('/inventory/<int:item_id>/labels-lbx.zip')
@login_required
def unit_labels_lbx_zip(item_id):
    """Download all unit labels of an item as a ZIP of .lbx files."""
    item = Item.query.get_or_404(item_id)
    units = [u for u in item.units if u.status != ItemUnit.STATUS_RETIRED]
    if not units:
        flash('Keine Einheiten vorhanden.', 'error')
        return redirect(url_for('admin.inventory_edit', item_id=item.id) + '#units')
    buf = BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for u in units:
            fname = _safe_filename(u.asset_tag or f'unit-{u.id}') + '.lbx'
            zf.writestr(fname, _unit_label_lbx_bytes(u))
    buf.seek(0)
    zip_name = 'etiketten-lbx-' + _safe_filename(item.name) + '.zip'
    return send_file(buf, mimetype='application/zip', as_attachment=True, download_name=zip_name)


@admin_bp.route('/u/<asset_tag>')
@login_required
def unit_lookup(asset_tag):
    """QR code target: look up a unit by asset tag and jump to its item"""
    unit = ItemUnit.query.filter_by(asset_tag=asset_tag).first()
    if not unit:
        flash(f'Keine Einheit mit Kennung "{asset_tag}" gefunden.', 'error')
        return redirect(url_for('admin.inventory_list'))
    return redirect(url_for('admin.inventory_edit', item_id=unit.item_id) + f'#unit-{unit.id}')


@admin_bp.route('/api/items/<int:item_id>/availability')
@login_required
def item_availability_api(item_id):
    """JSON availability check for an item in a date range"""
    item = Item.query.get_or_404(item_id)
    start = _parse_date_or_none(request.args.get('start'))
    end = _parse_date_or_none(request.args.get('end'))
    if not start or not end or end < start:
        return jsonify({'error': 'Ungültiger Zeitraum'}), 400
    start_dt = datetime.combine(start, datetime.min.time())
    end_dt = datetime.combine(end, datetime.min.time())
    if item.is_package:
        available = get_package_available_quantity(item.id, start_dt, end_dt)
    else:
        available = get_available_quantity(item.id, start_dt, end_dt)
    return jsonify({
        'item_id': item.id,
        'start': start.isoformat(),
        'end': end.isoformat(),
        'available': available,
        'total': item.operational_quantity,
    })


# ============= SUPPLIERS (Lieferanten) =============

@admin_bp.route('/suppliers', methods=['GET', 'POST'])
@login_required
def suppliers():
    """List and add suppliers"""
    if request.method == 'POST':
        try:
            name = request.form.get('name', '').strip()
            if not name:
                flash('Name ist erforderlich.', 'error')
            elif Supplier.query.filter_by(name=name).first():
                flash(f'Lieferant "{name}" existiert bereits.', 'error')
            else:
                supplier = Supplier(
                    name=name,
                    email=request.form.get('email', '').strip() or None,
                    phone=request.form.get('phone', '').strip() or None,
                    notes=request.form.get('notes', '').strip() or None,
                )
                db.session.add(supplier)
                db.session.commit()
                flash(f'Lieferant "{name}" angelegt.', 'success')
        except Exception as e:
            db.session.rollback()
            flash(f'Fehler: {str(e)}', 'error')
        return redirect(url_for('admin.suppliers'))

    all_suppliers = Supplier.query.order_by(Supplier.name).all()
    return render_template('admin/suppliers.html', suppliers=all_suppliers)


@admin_bp.route('/suppliers/<int:supplier_id>/update', methods=['POST'])
@login_required
def supplier_update(supplier_id):
    """Update a supplier"""
    supplier = Supplier.query.get_or_404(supplier_id)
    try:
        name = request.form.get('name', '').strip()
        if not name:
            flash('Name ist erforderlich.', 'error')
            return redirect(url_for('admin.suppliers'))
        existing = Supplier.query.filter(Supplier.name == name, Supplier.id != supplier.id).first()
        if existing:
            flash(f'Lieferant "{name}" existiert bereits.', 'error')
            return redirect(url_for('admin.suppliers'))
        supplier.name = name
        supplier.email = request.form.get('email', '').strip() or None
        supplier.phone = request.form.get('phone', '').strip() or None
        supplier.notes = request.form.get('notes', '').strip() or None
        db.session.commit()
        flash(f'Lieferant "{name}" aktualisiert.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Fehler: {str(e)}', 'error')
    return redirect(url_for('admin.suppliers'))


@admin_bp.route('/suppliers/<int:supplier_id>/delete', methods=['POST'])
@login_required
def supplier_delete(supplier_id):
    """Delete a supplier (its item offers are removed; quote history keeps the name snapshot)"""
    supplier = Supplier.query.get_or_404(supplier_id)
    try:
        name = supplier.name
        db.session.delete(supplier)
        db.session.commit()
        flash(f'Lieferant "{name}" gelöscht.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Fehler: {str(e)}', 'error')
    return redirect(url_for('admin.suppliers'))


# ============= QUOTES =============

def _apply_default_sources(qi, item, quote):
    """Prefill supplier sourcing for a quote line: own stock first, then cheapest suppliers.
    The user can redistribute manually in the quote editor."""
    qi.sources = []
    if not item.supplies:
        qi.rental_cost_per_day = 0
        return
    own_avail = get_own_stock_available(item.id, quote.start_date, quote.end_date,
                                        exclude_quote_id=quote.id)
    _, breakdown = item.calculate_external_cost(qi.quantity, own_available=own_avail)
    qi.sources = [QuoteItemSource(
        supplier_id=s.supplier_id,
        supplier_name=s.supplier.name,
        quantity=q,
        price_per_day=s.price_per_day or 0,
    ) for s, q in breakdown]
    qi.recalc_cost_from_sources()


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
                recipient_lines=request.form.get('recipient_lines', '').strip(),
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


@admin_bp.route('/quotes/<int:quote_id>/edit')
@login_required
def quote_edit(quote_id):
    """Quote editor (single-page; all mutations go through the JSON API below)"""
    quote = Quote.query.get_or_404(quote_id)
    categories = Category.query.order_by(Category.display_order, Category.name).all()
    category_tree = Category.get_tree(categories)
    _ss = SiteSettings.query.first()
    _eff_mode, _eff_rate = _effective_tax_mode_and_rate(_ss)
    return render_template('admin/quote_edit.html', quote=quote,
                           category_tree=category_tree,
                           site_settings=_ss, tax_rate=_eff_rate, tax_mode=_eff_mode)


# ── Quote editor JSON API ──

def _next_line_position(quote):
    return max([qi.position or 0 for qi in quote.quote_items], default=0) + 1


def _serialize_quote_state(quote):
    """Full editor state as a JSON-serializable dict."""
    site_settings = SiteSettings.query.first()
    tax_mode, tax_rate = _effective_tax_mode_and_rate(site_settings)
    has_dates = bool(quote.start_date and quote.end_date)

    lines = []
    for qi in quote.quote_items:
        if qi.is_heading:
            lines.append({
                'id': qi.id, 'type': 'heading',
                'name': qi.custom_item_name or '',
                'position': qi.position or 0,
            })
            continue
        line = {
            'id': qi.id,
            'type': 'custom' if qi.is_custom else 'item',
            'name': qi.display_name,
            'quantity': qi.quantity,
            'price_per_day': round(qi.rental_price_per_day or 0, 2),
            'cost_per_day': round(qi.rental_cost_per_day or 0, 2),
            'discount_exempt': bool(qi.discount_exempt),
            'is_optional': bool(qi.is_optional),
            'position': qi.position or 0,
            'total': qi.total_price,
            'total_cost': qi.total_external_cost,
            'package_id': qi.package_id,
            'package_name': qi.package.name if qi.package_id and qi.package else None,
        }
        if not qi.is_custom and qi.item:
            item = qi.item
            if has_dates:
                avail = get_available_quantity(qi.item_id, quote.start_date, quote.end_date,
                                               exclude_quote_id=quote.id)
            else:
                avail = item.operational_quantity
            own_avail = None
            if item.supplies:
                if has_dates:
                    own_avail = get_own_stock_available(qi.item_id, quote.start_date, quote.end_date,
                                                        exclude_quote_id=quote.id)
                else:
                    own_avail = item.operational_stock
            line.update({
                'item_id': qi.item_id,
                'category': item.category.name if item.category else None,
                'is_external': item.is_external,
                'available': avail,
                'total_quantity': item.total_quantity,
                'own_stock': item.stock_quantity,
                'own_available': own_avail,
                'sourced_quantity': qi.sourced_quantity,
                'supplies': [{
                    'supplier_id': s.supplier_id,
                    'supplier_name': s.supplier.name if s.supplier else '?',
                    'max_quantity': s.quantity,
                    'price_per_day': round(s.price_per_day or 0, 2),
                } for s in item.supplies_sorted],
                'sources': {str(src.supplier_id): src.quantity for src in qi.sources if src.supplier_id},
            })
        lines.append(line)

    return {
        'id': quote.id,
        'status': quote.status,
        'reference_number': quote.reference_number,
        'customer_name': quote.customer_name,
        'recipient_lines': quote.recipient_lines or '',
        'notes': quote.notes or '',
        'public_notes': quote.public_notes or '',
        'start_date': quote.start_date.strftime('%Y-%m-%d') if quote.start_date else '',
        'end_date': quote.end_date.strftime('%Y-%m-%d') if quote.end_date else '',
        'rental_days': quote.calculate_rental_days(),
        'rental_days_override': quote.rental_days_override,
        'date_based_days': quote.date_based_rental_days(),
        'has_dates': has_dates,
        'prices_are_net': bool(quote.prices_are_net),
        'tax_mode': tax_mode,
        'tax_rate': tax_rate,
        'lines': lines,
        'totals': {
            'subtotal': quote.subtotal,
            'discountable': quote.discountable_subtotal,
            'discount_percent': round(quote.discount_percent or 0, 4),
            'discount_label': quote.discount_label or '',
            'discount_amount': quote.discount_amount,
            'optional_total': quote.optional_total,
            'total': quote.total,
        },
    }


def _api_ok(quote, warnings=None):
    return jsonify({'ok': True, 'state': _serialize_quote_state(quote), 'warnings': warnings or []})


def _api_error(message, code=400):
    return jsonify({'ok': False, 'error': message}), code


@admin_bp.route('/quotes/<int:quote_id>/api/state')
@login_required
def quote_api_state(quote_id):
    quote = Quote.query.get_or_404(quote_id)
    return _api_ok(quote)


@admin_bp.route('/quotes/<int:quote_id>/api/details', methods=['POST'])
@login_required
def quote_api_details(quote_id):
    """Update quote master data (customer, dates, notes)."""
    quote = Quote.query.get_or_404(quote_id)
    data = request.get_json(silent=True) or {}
    try:
        if 'customer_name' in data:
            name = (data.get('customer_name') or '').strip()
            if name:
                quote.customer_name = name
        if 'start_date' in data or 'end_date' in data:
            start_str = (data.get('start_date') or '').strip()
            end_str = (data.get('end_date') or '').strip()
            start = datetime.strptime(start_str, '%Y-%m-%d') if start_str else None
            end = datetime.strptime(end_str, '%Y-%m-%d') if end_str else None
            if start and end and start > end:
                return _api_error('Enddatum muss nach oder gleich dem Startdatum sein.')
            quote.start_date = start
            quote.end_date = end
            if start and end:
                quote.rental_days = max(1, (end - start).days + 1)
        if 'rental_days_override' in data:
            override = data.get('rental_days_override')
            quote.rental_days_override = int(override) if override else None
        if 'recipient_lines' in data:
            quote.recipient_lines = data.get('recipient_lines') or ''
        if 'notes' in data:
            quote.notes = data.get('notes') or ''
        if 'public_notes' in data:
            quote.public_notes = data.get('public_notes') or ''
        db.session.commit()
        return _api_ok(quote)
    except Exception as e:
        db.session.rollback()
        return _api_error(str(e), 500)


@admin_bp.route('/quotes/<int:quote_id>/api/picker')
@login_required
def quote_api_picker(quote_id):
    """Item search for the quote editor side panel, with live availability."""
    quote = Quote.query.get_or_404(quote_id)
    q = (request.args.get('q') or '').strip().lower()
    cat_id = request.args.get('cat', type=int)

    cat_ids = None
    if cat_id:
        cat = Category.query.get(cat_id)
        if cat:
            cat_ids = cat.all_descendant_ids()

    existing_item_ids = {qi.item_id for qi in quote.quote_items
                         if qi.item_id and not qi.package_id and not qi.is_custom}
    existing_package_ids = {qi.package_id for qi in quote.quote_items if qi.package_id}

    results = []
    for item in Item.query.order_by(Item.name).all():
        if cat_ids is not None:
            item_cat_ids = set()
            if item.category_id:
                item_cat_ids.add(item.category_id)
            item_cat_ids |= {c.id for c in item.subcategories}
            if not (item_cat_ids & cat_ids):
                continue
        if q:
            hay = ' '.join(filter(None, [
                item.name, item.manufacturer, item.model_name,
                item.category.name if item.category else '',
            ])).lower()
            if not all(tok in hay for tok in q.split()):
                continue
        if quote.start_date and quote.end_date:
            if item.is_package:
                avail = get_package_available_quantity(item.id, quote.start_date, quote.end_date,
                                                       exclude_quote_id=quote.id)
            else:
                avail = get_available_quantity(item.id, quote.start_date, quote.end_date,
                                               exclude_quote_id=quote.id)
        else:
            avail = item.operational_quantity
        results.append({
            'id': item.id,
            'name': item.name,
            'category': item.category.name if item.category else None,
            'price_per_day': round(item.default_rental_price_per_day or 0, 2),
            'is_package': item.is_package,
            'is_external': item.is_external,
            'available': avail,
            'in_quote': (item.id in existing_package_ids) if item.is_package else (item.id in existing_item_ids),
            'image': url_for('public.uploaded_file', filename=item.image_filename) if item.image_filename else None,
        })
    return jsonify({'ok': True, 'items': results})


@admin_bp.route('/quotes/<int:quote_id>/api/lines', methods=['POST'])
@login_required
def quote_api_line_add(quote_id):
    """Add a line: inventory item, package, custom position or heading."""
    quote = Quote.query.get_or_404(quote_id)
    data = request.get_json(silent=True) or {}
    ltype = data.get('type', 'item')
    try:
        pos = _next_line_position(quote)
        if ltype == 'heading':
            text = (data.get('name') or '').strip()
            if not text:
                return _api_error('Text fehlt.')
            db.session.add(QuoteItem(
                quote_id=quote.id, is_custom=True, is_heading=True,
                custom_item_name=text, quantity=0, rental_price_per_day=0, position=pos))
        elif ltype == 'custom':
            name = (data.get('name') or '').strip()
            if not name:
                return _api_error('Name fehlt.')
            db.session.add(QuoteItem(
                quote_id=quote.id, is_custom=True, custom_item_name=name,
                quantity=max(1, int(data.get('quantity') or 1)),
                rental_price_per_day=round(float(data.get('price') or 0), 2),
                position=pos))
        else:
            if not quote.start_date or not quote.end_date:
                return _api_error('Bitte zuerst Start- und Enddatum setzen.')
            item = Item.query.get(data.get('item_id') or 0)
            if not item:
                return _api_error('Artikel nicht gefunden.')
            qty = max(1, int(data.get('quantity') or 1))
            if item.is_package:
                if any(qi.package_id == item.id for qi in quote.quote_items):
                    return _api_error(f'{item.name} ist bereits im Angebot.')
                component_price_sum = item.component_price_sum
                for pc in item.package_components:
                    if component_price_sum > 0:
                        comp_share = (pc.component_item.default_rental_price_per_day * pc.quantity) / component_price_sum
                        adjusted_price = round((item.default_rental_price_per_day * comp_share) / pc.quantity, 2)
                    else:
                        adjusted_price = 0
                    qi = QuoteItem(
                        quote_id=quote.id, item_id=pc.component_item_id,
                        quantity=pc.quantity * qty, rental_price_per_day=adjusted_price,
                        is_custom=False, package_id=item.id, position=pos)
                    pos += 1
                    db.session.add(qi)
                    db.session.flush()
                    _apply_default_sources(qi, pc.component_item, quote)
            else:
                existing = next((qi for qi in quote.quote_items
                                 if qi.item_id == item.id and not qi.is_custom and not qi.package_id), None)
                if existing:
                    existing.quantity += qty
                    if item.supplies:
                        _apply_default_sources(existing, item, quote)
                else:
                    qi = QuoteItem(
                        quote_id=quote.id, item_id=item.id, quantity=qty,
                        rental_price_per_day=item.default_rental_price_per_day,
                        is_custom=False, position=pos)
                    db.session.add(qi)
                    db.session.flush()
                    _apply_default_sources(qi, item, quote)
        db.session.commit()
        return _api_ok(quote)
    except Exception as e:
        db.session.rollback()
        return _api_error(str(e), 500)

@admin_bp.route('/quotes/<int:quote_id>/api/lines/<int:line_id>', methods=['POST'])
@login_required
def quote_api_line_update(quote_id, line_id):
    """Update a single quote line (qty, price, flags, sourcing)."""
    quote = Quote.query.get_or_404(quote_id)
    qi = QuoteItem.query.get_or_404(line_id)
    if qi.quote_id != quote.id:
        return _api_error('Ungültige Position.', 404)
    data = request.get_json(silent=True) or {}
    warnings = []
    try:
        if 'name' in data and (qi.is_custom or qi.is_heading):
            new_name = (data.get('name') or '').strip()
            if new_name:
                qi.custom_item_name = new_name
        if 'quantity' in data and not qi.is_heading:
            qi.quantity = max(1, int(data.get('quantity') or 1))
        if 'price_per_day' in data and not qi.is_heading:
            qi.rental_price_per_day = max(0.0, round(float(data.get('price_per_day') or 0), 2))
        if 'discount_exempt' in data:
            qi.discount_exempt = bool(data.get('discount_exempt'))
        if 'is_optional' in data and not qi.is_heading:
            qi.is_optional = bool(data.get('is_optional'))
        if 'cost_per_day' in data and not qi.is_custom and qi.item and not qi.item.supplies:
            qi.rental_cost_per_day = max(0.0, round(float(data.get('cost_per_day') or 0), 2))

        if not qi.is_custom and not qi.is_heading and qi.item and qi.item.supplies:
            if data.get('auto_sources'):
                _apply_default_sources(qi, qi.item, quote)
            elif 'sources' in data:
                src_map = data.get('sources') or {}
                new_sources = []
                for supply in qi.item.supplies:
                    try:
                        src_qty = int(src_map.get(str(supply.supplier_id), 0) or 0)
                    except (TypeError, ValueError):
                        src_qty = 0
                    if src_qty <= 0:
                        continue
                    if supply.quantity != -1 and src_qty > supply.quantity:
                        warnings.append(f'{qi.item.name}: {supply.supplier.name} kann max. {supply.quantity} liefern ({src_qty} zugewiesen).')
                    new_sources.append(QuoteItemSource(
                        supplier_id=supply.supplier_id,
                        supplier_name=supply.supplier.name,
                        quantity=src_qty,
                        price_per_day=supply.price_per_day or 0,
                    ))
                qi.sources = new_sources
            qi.recalc_cost_from_sources()

        # Availability / coverage warnings
        if not qi.is_custom and not qi.is_heading and qi.item and not qi.is_optional \
                and quote.start_date and quote.end_date:
            if qi.item.supplies:
                own_avail = get_own_stock_available(qi.item_id, quote.start_date, quote.end_date,
                                                    exclude_quote_id=quote.id)
                if own_avail != -1 and own_avail + qi.sourced_quantity < qi.quantity:
                    warnings.append(f'{qi.item.name}: Beschaffung deckt nur {own_avail + qi.sourced_quantity} von {qi.quantity} ab (Eigenbestand verfügbar: {own_avail}).')
            avail = get_available_quantity(qi.item_id, quote.start_date, quote.end_date,
                                           exclude_quote_id=quote.id)
            if avail != -1 and qi.quantity > avail:
                warnings.append(f'{qi.item.name}: Nur {avail} verfügbar, {qi.quantity} eingeplant.')

        db.session.commit()
        return _api_ok(quote, warnings)
    except Exception as e:
        db.session.rollback()
        return _api_error(str(e), 500)


@admin_bp.route('/quotes/<int:quote_id>/api/lines/<int:line_id>/delete', methods=['POST'])
@login_required
def quote_api_line_delete(quote_id, line_id):
    """Delete a line; with whole_package=true, delete all components of its package."""
    quote = Quote.query.get_or_404(quote_id)
    qi = QuoteItem.query.get_or_404(line_id)
    if qi.quote_id != quote.id:
        return _api_error('Ungültige Position.', 404)
    data = request.get_json(silent=True) or {}
    try:
        if data.get('whole_package') and qi.package_id:
            for comp in [c for c in quote.quote_items if c.package_id == qi.package_id]:
                db.session.delete(comp)
        else:
            db.session.delete(qi)
        db.session.commit()
        return _api_ok(quote)
    except Exception as e:
        db.session.rollback()
        return _api_error(str(e), 500)


@admin_bp.route('/quotes/<int:quote_id>/api/reorder', methods=['POST'])
@login_required
def quote_api_reorder(quote_id):
    """Persist manual line order. Keys: 'line-<id>' or 'pkg-<package_id>' (whole block)."""
    quote = Quote.query.get_or_404(quote_id)
    data = request.get_json(silent=True) or {}
    order = data.get('order') or []
    try:
        lines_by_id = {qi.id: qi for qi in quote.quote_items}
        pos = 1
        for key in order:
            if key.startswith('pkg-'):
                pid = int(key[4:])
                comps = sorted([qi for qi in quote.quote_items if qi.package_id == pid],
                               key=lambda x: (x.position or 0, x.id))
                for qi in comps:
                    qi.position = pos
                    pos += 1
            elif key.startswith('line-'):
                qi = lines_by_id.get(int(key[5:]))
                if qi and not qi.package_id:
                    qi.position = pos
                    pos += 1
        db.session.commit()
        return _api_ok(quote)
    except Exception as e:
        db.session.rollback()
        return _api_error(str(e), 500)

@admin_bp.route('/quotes/<int:quote_id>/api/prices_mode', methods=['POST'])
@login_required
def quote_api_prices_mode(quote_id):
    quote = Quote.query.get_or_404(quote_id)
    data = request.get_json(silent=True) or {}
    try:
        quote.prices_are_net = bool(data.get('prices_are_net'))
        db.session.commit()
        return _api_ok(quote)
    except Exception as e:
        db.session.rollback()
        return _api_error(str(e), 500)


@admin_bp.route('/quotes/<int:quote_id>/api/discount', methods=['POST'])
@login_required
def quote_api_discount(quote_id):
    quote = Quote.query.get_or_404(quote_id)
    data = request.get_json(silent=True) or {}
    try:
        target_total = data.get('target_total')
        if target_total is not None and str(target_total).strip() != '':
            target_total = float(target_total)
            discountable = quote.discountable_subtotal
            if discountable > 0:
                needed_discount = quote.subtotal - target_total
                discount_percent = max(0, min(100, (needed_discount / discountable) * 100))
            else:
                discount_percent = 0
        else:
            discount_percent = max(0, min(100, float(data.get('percent') or 0)))
        quote.discount_percent = discount_percent
        quote.discount_label = (data.get('label') or '').strip() or None
        db.session.commit()
        return _api_ok(quote)
    except Exception as e:
        db.session.rollback()
        return _api_error(str(e), 500)


# ── Packliste: assign physical units (serial numbers) to quote lines ──

def _conflicting_unit_ids(quote):
    """ItemUnit ids already assigned to OTHER quotes overlapping this quote's period."""
    if not quote.start_date or not quote.end_date:
        return set()
    overlapping = Quote.query.filter(
        Quote.id != quote.id,
        Quote.status == 'draft',
        Quote.start_date.isnot(None),
        Quote.end_date.isnot(None),
        Quote.start_date <= quote.end_date,
        Quote.end_date >= quote.start_date,
    ).all()
    ids = set()
    for q in overlapping:
        for qi in q.quote_items:
            for au in qi.assigned_units:
                if au.item_unit_id:
                    ids.add(au.item_unit_id)
    return ids


def _packliste_lines(quote):
    """Pickable lines (inventory items, no headings/optional) with unit info."""
    conflict_ids = _conflicting_unit_ids(quote)
    lines = []
    for qi in quote.quote_items:
        if qi.is_custom or qi.is_heading or qi.is_optional or not qi.item:
            continue
        units = qi.item.units
        assigned_unit_ids = {au.item_unit_id for au in qi.assigned_units if au.item_unit_id}
        candidates = [u for u in units
                      if u.status == ItemUnit.STATUS_AVAILABLE
                      and u.id not in conflict_ids
                      and u.id not in assigned_unit_ids]
        lines.append({
            'qi': qi,
            'tracked': bool(units),
            'candidates': candidates,
            'assigned_count': len(qi.assigned_units),
        })
    return lines


@admin_bp.route('/quotes/<int:quote_id>/packliste')
@login_required
def quote_packliste(quote_id):
    """Picking view: assign specific units (serial numbers) to the quote lines."""
    quote = Quote.query.get_or_404(quote_id)
    return render_template('admin/packliste.html', quote=quote, lines=_packliste_lines(quote))


@admin_bp.route('/quotes/<int:quote_id>/packliste/assign', methods=['POST'])
@login_required
def packliste_assign(quote_id):
    """Assign a specific unit to a quote line."""
    quote = Quote.query.get_or_404(quote_id)
    line_id = request.form.get('line_id', type=int)
    unit_id = request.form.get('unit_id', type=int)
    qi = QuoteItem.query.get_or_404(line_id)
    unit = ItemUnit.query.get_or_404(unit_id)
    try:
        if qi.quote_id != quote.id or unit.item_id != qi.item_id:
            flash('Ungültige Zuordnung.', 'error')
        elif len(qi.assigned_units) >= qi.quantity:
            flash(f'{qi.item.name}: Alle {qi.quantity} Einheiten sind bereits zugewiesen.', 'error')
        elif any(au.item_unit_id == unit.id for au in qi.assigned_units):
            flash(f'Einheit {unit.asset_tag or unit.id} ist bereits zugewiesen.', 'info')
        elif unit.id in _conflicting_unit_ids(quote):
            flash(f'Einheit {unit.asset_tag or unit.id} ist im Zeitraum bereits anderweitig eingeplant.', 'error')
        elif unit.status != ItemUnit.STATUS_AVAILABLE:
            flash(f'Einheit {unit.asset_tag or unit.id} ist nicht einsatzbereit ({unit.status_label}).', 'error')
        else:
            db.session.add(QuoteItemUnit(
                quote_item_id=qi.id, item_unit_id=unit.id,
                asset_tag=unit.asset_tag, serial_number=unit.serial_number))
            db.session.commit()
            flash(f'Einheit {unit.asset_tag or unit.id} zugewiesen.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Fehler: {str(e)}', 'error')
    return redirect(url_for('admin.quote_packliste', quote_id=quote.id))


def _packliste_scan_code(quote, code):
    """Try to assign the unit identified by code to a matching line.
    Returns (ok, category, message)."""
    # QR labels contain a URL ending in /u/<asset_tag> – accept those too
    if '/u/' in code:
        code = code.rstrip('/').rsplit('/', 1)[-1]
    unit = ItemUnit.query.filter(
        db.or_(ItemUnit.asset_tag.ilike(code), ItemUnit.serial_number.ilike(code))
    ).first()
    if not unit:
        return False, 'error', f'Keine Einheit mit Kennung "{code}" gefunden.'
    # Find a matching quote line with free capacity
    target = None
    for qi in quote.quote_items:
        if qi.is_custom or qi.is_heading or qi.is_optional or qi.item_id != unit.item_id:
            continue
        if any(au.item_unit_id == unit.id for au in qi.assigned_units):
            return False, 'info', f'Einheit {unit.asset_tag or unit.id} ist bereits zugewiesen.'
        if len(qi.assigned_units) < qi.quantity:
            target = qi
            break
    if not target:
        return False, 'error', f'Kein passender Artikel im Angebot für Einheit {unit.asset_tag or unit.id} ({unit.item.name}) – oder alle Positionen sind voll.'
    if unit.status != ItemUnit.STATUS_AVAILABLE:
        return False, 'error', f'Einheit {unit.asset_tag or unit.id} ist nicht einsatzbereit ({unit.status_label}).'
    if unit.id in _conflicting_unit_ids(quote):
        return False, 'error', f'Einheit {unit.asset_tag or unit.id} ist im Zeitraum bereits anderweitig eingeplant.'
    try:
        db.session.add(QuoteItemUnit(
            quote_item_id=target.id, item_unit_id=unit.id,
            asset_tag=unit.asset_tag, serial_number=unit.serial_number))
        db.session.commit()
        return True, 'success', f'✓ {unit.item.name}: Einheit {unit.asset_tag or unit.id} zugewiesen.'
    except Exception as e:
        db.session.rollback()
        return False, 'error', f'Fehler: {str(e)}'


@admin_bp.route('/quotes/<int:quote_id>/packliste/scan', methods=['POST'])
@login_required
def packliste_scan(quote_id):
    """Scan/type an asset tag or serial number; auto-assign to the matching line.
    Returns JSON for AJAX requests (camera scanner), otherwise flash + redirect."""
    quote = Quote.query.get_or_404(quote_id)
    wants_json = 'application/json' in (request.headers.get('Accept') or '')
    code = (request.form.get('code') or '').strip()
    if not code:
        if wants_json:
            return jsonify(ok=False, category='error', message='Kein Code übermittelt.'), 400
        return redirect(url_for('admin.quote_packliste', quote_id=quote.id))
    ok, category, message = _packliste_scan_code(quote, code)
    if wants_json:
        return jsonify(ok=ok, category=category, message=message)
    flash(message, category)
    return redirect(url_for('admin.quote_packliste', quote_id=quote.id))


@admin_bp.route('/quotes/<int:quote_id>/packliste/unassign', methods=['POST'])
@login_required
def packliste_unassign(quote_id):
    """Remove a unit assignment."""
    quote = Quote.query.get_or_404(quote_id)
    assignment = QuoteItemUnit.query.get_or_404(request.form.get('assignment_id', type=int))
    if assignment.quote_item.quote_id != quote.id:
        flash('Ungültige Zuordnung.', 'error')
    else:
        try:
            db.session.delete(assignment)
            db.session.commit()
            flash('Zuweisung entfernt.', 'success')
        except Exception as e:
            db.session.rollback()
            flash(f'Fehler: {str(e)}', 'error')
    return redirect(url_for('admin.quote_packliste', quote_id=quote.id))


@admin_bp.route('/quotes/<int:quote_id>')
@login_required
def quote_view(quote_id):
    """View quote details"""
    quote = Quote.query.get_or_404(quote_id)
    from datetime import date as date_cls
    site_settings = SiteSettings.query.first()
    _eff_mode, _eff_rate = _effective_tax_mode_and_rate(site_settings)
    return render_template('admin/quote_view.html', quote=quote, today=date_cls.today().isoformat(),
                           site_settings=site_settings,
                           tax_mode=_eff_mode, tax_rate=_eff_rate)


@admin_bp.route('/quotes/<int:quote_id>/update_notes', methods=['POST'])
@login_required
def quote_update_notes(quote_id):
    """Update internal notes of a quote (allowed in any status)."""
    quote = Quote.query.get_or_404(quote_id)
    try:
        quote.notes = request.form.get('notes', '')
        db.session.commit()
        flash('Interne Notizen aktualisiert!', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Fehler: {str(e)}', 'error')
    return redirect(url_for('admin.quote_view', quote_id=quote_id))


@admin_bp.route('/quotes/<int:quote_id>/delete', methods=['POST'])
@login_required
def quote_delete(quote_id):
    """Delete quote (only allowed in draft status)"""
    quote = Quote.query.get_or_404(quote_id)
    if quote.status != 'draft':
        flash('Nur Entwürfe können gelöscht werden.', 'error')
        return redirect(url_for('admin.quote_view', quote_id=quote_id))
    try:
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
        _pos = 1
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
                            qi = QuoteItem(
                                quote_id=quote.id,
                                item_id=pc.component_item_id,
                                quantity=pc.quantity,
                                rental_price_per_day=adjusted_price,
                                is_custom=False,
                                package_id=item.id,
                                position=_pos
                            )
                            _pos += 1
                            db.session.add(qi)
                            db.session.flush()
                            _apply_default_sources(qi, pc.component_item, quote)
                else:
                    qi = QuoteItem(
                        quote_id=quote.id,
                        item_id=item.id,
                        quantity=inq_item.quantity,
                        rental_price_per_day=item.default_rental_price_per_day,
                        is_custom=False,
                        position=_pos
                    )
                    _pos += 1
                    db.session.add(qi)
                    db.session.flush()
                    _apply_default_sources(qi, item, quote)

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
                is_admin=is_admin
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
        db.session.delete(user)
        db.session.commit()
        flash(f'Benutzer "{user.username}" gelöscht.', 'success')
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
            settings_record.display_name = request.form.get('display_name', '').strip() or None
            settings_record.address_lines = request.form.get('address_lines', '')
            settings_record.contact_lines = request.form.get('contact_lines', '')
            settings_record.bank_lines = request.form.get('bank_lines', '')
            settings_record.tax_number = request.form.get('tax_number', '').strip()
            settings_record.vat_id = request.form.get('vat_id', '').strip()
            settings_record.tax_mode = request.form.get('tax_mode', 'kleinunternehmer').strip()
            settings_record.tax_rate = float(request.form.get('tax_rate', '19.0') or 19.0)
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
            'notes': q.notes or '',
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
            'notes': inq.message or '',
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
    recipient = [l.strip() for l in (quote.recipient_lines or '').split('\n') if l.strip()]
    # Prepend customer name above address lines
    if quote.customer_name and quote.customer_name.strip():
        customer_name = quote.customer_name.strip()
        if not recipient or recipient[0] != customer_name:
            recipient.insert(0, customer_name)
    tax_number = site_settings.tax_number if site_settings else None
    vat_id = site_settings.vat_id if site_settings else None
    tax_mode, tax_rate = _effective_tax_mode_and_rate(site_settings)
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
    is_pauschale = bool(quote.rental_days_override)

    # Build a compact period label for Pauschale mode
    leistungszeitraum = None
    if start_str and end_str:
        if start_str == end_str:
            leistungszeitraum = start_str
        else:
            leistungszeitraum = f"{start_str} – {end_str}"

    return {
        'issuer_name': issuer_name,
        'issuer_address': address_lines,
        'contact_lines': contact_lines_list,
        'bank_lines': bank_lines_list,
        'recipient_lines': recipient,
        'tax_number': tax_number,
        'vat_id': vat_id,
        'tax_mode': tax_mode,
        'tax_rate': tax_rate,
        'payment_terms_days': payment_terms_days,
        'quote_validity_days': quote_validity_days,
        'logo_path': logo_path,
        'start_date_str': start_str,
        'end_date_str': end_str,
        'rental_days': rental_days,
        'is_pauschale': is_pauschale,
        'leistungszeitraum': leistungszeitraum,
    }


def _extract_positions(quote, *, include_optional=False, include_headings=False):
    """Extract positions from a quote in manual order, grouping bundle components.

    Returns a list of dicts:
    - Regular item: { 'name', 'quantity', 'price_per_day', 'total', 'is_bundle': False }
    - Bundle: { 'name', 'quantity', 'price_per_day': 0, 'total', 'is_bundle': True,
                'bundle_components': [{'name', 'quantity'}] }
    - Heading (if include_headings): { 'name', 'is_heading': True }
    - Optional items (if include_optional) carry 'is_optional': True and are
      NOT part of the billable totals.
    """
    positions = []
    seen_package_ids = set()

    for qi in quote.quote_items:  # ordered by position
        if qi.is_heading:
            if include_headings:
                positions.append({'name': qi.custom_item_name or '', 'is_heading': True})
            continue
        if qi.is_optional and not include_optional:
            continue
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
                'is_optional': bool(qi.is_optional),
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
                'is_optional': bool(qi.is_optional),
            })

    return positions


def _extract_items_for_lieferschein(quote):
    """Extract items for the Lieferschein (no prices; includes headings, assigned
    serial numbers, excludes optional positions)."""
    items = []
    seen_package_ids = set()

    for qi in quote.quote_items:  # ordered by position
        if qi.is_heading:
            items.append({'name': qi.custom_item_name or '', 'is_heading': True})
            continue
        if qi.is_optional:
            continue
        if qi.package_id:
            if qi.package_id in seen_package_ids:
                continue
            seen_package_ids.add(qi.package_id)
            components = [q for q in quote.quote_items if q.package_id == qi.package_id]
            pkg_name = qi.package.name if qi.package else "Paket"
            pkg_description = qi.package.description if qi.package else None
            items.append({
                'name': pkg_name,
                'quantity': 1,
                'is_bundle': True,
                'description': pkg_description,
                'bundle_components': [
                    {
                        'name': c.display_name,
                        'quantity': c.quantity,
                        'description': c.item.description if c.item else None,
                        'units': [au.label for au in c.assigned_units],
                    }
                    for c in components
                ],
            })
        else:
            items.append({
                'name': qi.display_name,
                'quantity': qi.quantity,
                'is_bundle': False,
                'description': qi.item.description if qi.item else None,
                'units': [au.label for au in qi.assigned_units],
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
    positions = _extract_positions(quote, include_optional=True, include_headings=True)

    pdf_bytes = build_angebot_pdf(
        issuer_name=data['issuer_name'],
        issuer_address=data['issuer_address'],
        contact_lines=data['contact_lines'],
        bank_lines=data['bank_lines'],
        tax_number=data['tax_number'],
        vat_id=data.get('vat_id'),
        tax_mode=data['tax_mode'],
        tax_rate=data['tax_rate'],
        prices_are_net=bool(getattr(quote, 'prices_are_net', False)),
        logo_path=data['logo_path'],
        recipient_lines=data['recipient_lines'],
        reference_number=quote.reference_number or f"AN-{quote.id:04d}",
        start_date_str=data['start_date_str'],
        end_date_str=data['end_date_str'],
        rental_days=data['rental_days'],
        is_pauschale=data['is_pauschale'],
        leistungszeitraum=data.get('leistungszeitraum'),
        positions=positions,
        discount_percent=quote.discount_percent or 0,
        discount_label=quote.discount_label,
        discount_amount=quote.discount_amount,
        subtotal=quote.subtotal,
        total=quote.total,
        payment_terms_days=data['payment_terms_days'],
        quote_validity_days=data['quote_validity_days'],
        notes=quote.public_notes,
        terms_and_conditions_text=site_settings.terms_and_conditions_text if site_settings else None,
    )
    return _send_pdf_response(pdf_bytes, f"angebot_{quote.reference_number}.pdf")


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
        vat_id=data.get('vat_id'),
        logo_path=data['logo_path'],
        recipient_lines=data['recipient_lines'],
        reference_number=quote.reference_number or f"LS-{quote.id:04d}",
        start_date_str=data['start_date_str'],
        end_date_str=data['end_date_str'],
        items=items,
        kaution=kaution,
        notes=quote.public_notes,
    )
    return _send_pdf_response(pdf_bytes, f"lieferschein_{quote.reference_number}.pdf")


# ── Legacy PDF generators (kept for backwards compatibility) ──

# ============= CUSTOMER DATABASE =============

# ── Local customer database ──

@admin_bp.route('/api/customers/search')
@login_required
def customer_search():
    """Search saved customers by name (for autocomplete)."""
    q = request.args.get('q', '').strip()
    if len(q) < 1:
        return jsonify([])

    customers = Customer.query.filter(Customer.name.ilike(f'%{q}%')).order_by(Customer.name).limit(10).all()
    return jsonify([{'name': c.name, 'recipient_lines': c.recipient_lines or ''} for c in customers])


@admin_bp.route('/api/customers/save', methods=['POST'])
@login_required
def customer_save():
    """Save or update a customer entry (identified by name)."""
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
    """Delete a saved customer by name."""
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
