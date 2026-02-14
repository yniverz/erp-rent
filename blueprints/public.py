from flask import Blueprint, render_template, request, redirect, url_for, flash, session, jsonify, send_from_directory
from models import db, Item, Category, Inquiry, InquiryItem, SiteSettings, item_subcategories
from helpers import send_inquiry_notification, get_upload_path
from datetime import datetime, date
import os
import re

public_bp = Blueprint('public', __name__)


@public_bp.route('/')
def catalog():
    """Public storefront catalog"""
    selected_category = request.args.get('category', type=int)
    misc = request.args.get('misc', type=int, default=0)
    search_query = request.args.get('q', '').strip()

    # Build full category tree for sidebar
    all_categories = Category.query.order_by(Category.display_order, Category.name).all()
    category_tree = Category.get_tree(all_categories)

    # Top-level categories (for main page cards)
    top_level_categories = [c for c in all_categories if c.parent_id is None]

    # Determine which parent categories have direct visible items (for virtual "Sonstiges")
    parent_cats = [c for c in all_categories if c.children]
    if parent_cats:
        parent_cat_ids = [c.id for c in parent_cats]
        # Find which of these parent categories have at least one visible item directly assigned
        from sqlalchemy import func
        direct_item_cats = db.session.query(Item.category_id).filter(
            Item.visible_in_shop == True,
            Item.category_id.in_(parent_cat_ids)
        ).distinct().all()
        misc_category_ids = {row[0] for row in direct_item_cats}
    else:
        misc_category_ids = set()

    # Get cart from session
    cart = session.get('cart', {})
    cart_count = sum(cart.values())

    if search_query:
        # Search across all visible items by name, description, and category
        terms = search_query.lower().split()
        query = Item.query.filter_by(visible_in_shop=True)
        for term in terms:
            term_filter = db.or_(
                Item.name.ilike(f'%{term}%'),
                Item.description.ilike(f'%{term}%'),
                Item.category.has(Category.name.ilike(f'%{term}%')),
                Item.subcategories.any(Category.name.ilike(f'%{term}%'))
            )
            query = query.filter(term_filter)
        items = query.order_by(Item.name).all()

        return render_template('public/catalog.html',
                               items=items,
                               categories=all_categories,
                               category_tree=category_tree,
                               top_level_categories=top_level_categories,
                               selected_category=selected_category,
                               selected_cat=None,
                               cart=cart,
                               cart_count=cart_count,
                               search_query=search_query,
                               show_items=True,
                               misc=False,
                               has_direct_items=False,
                               misc_category_ids=misc_category_ids)

    if selected_category:
        # Find the selected category and all its descendants
        cat = Category.query.get(selected_category)
        if cat:
            if misc and cat.children:
                # "Sonstiges" virtual category: only items directly in this category
                query = Item.query.filter_by(visible_in_shop=True).filter(
                    db.or_(
                        Item.category_id == cat.id,
                        Item.subcategories.any(Category.id == cat.id)
                    )
                )
            else:
                descendant_ids = cat.all_descendant_ids()
                query = Item.query.filter_by(visible_in_shop=True).filter(
                    db.or_(
                        Item.category_id.in_(descendant_ids),
                        Item.subcategories.any(Category.id.in_(descendant_ids))
                    )
                )
        else:
            query = Item.query.filter_by(visible_in_shop=True)
        items = query.order_by(Item.name).all()

        # Check if the category has items directly assigned (not only via children)
        has_direct_items = False
        if cat and cat.children:
            has_direct_items = Item.query.filter_by(visible_in_shop=True).filter(
                db.or_(
                    Item.category_id == cat.id,
                    Item.subcategories.any(Category.id == cat.id)
                )
            ).first() is not None

        return render_template('public/catalog.html',
                               items=items,
                               categories=all_categories,
                               category_tree=category_tree,
                               top_level_categories=top_level_categories,
                               selected_category=selected_category,
                               selected_cat=cat,
                               cart=cart,
                               cart_count=cart_count,
                               search_query='',
                               show_items=True,
                               misc=misc,
                               has_direct_items=has_direct_items,
                               misc_category_ids=misc_category_ids)
    else:
        # Main page: show top-level category cards
        return render_template('public/catalog.html',
                               items=[],
                               categories=all_categories,
                               category_tree=category_tree,
                               top_level_categories=top_level_categories,
                               selected_category=None,
                               selected_cat=None,
                               cart=cart,
                               cart_count=cart_count,
                               search_query='',
                               show_items=False,
                               misc=False,
                               has_direct_items=False,
                               misc_category_ids=misc_category_ids)


@public_bp.route('/item/<int:item_id>')
def item_detail(item_id):
    """Public item detail page"""
    item = Item.query.get_or_404(item_id)
    if not item.visible_in_shop:
        return redirect(url_for('public.catalog'))

    cart = session.get('cart', {})
    cart_count = sum(cart.values())

    return render_template('public/item_detail.html',
                           item=item,
                           cart=cart,
                           cart_count=cart_count)


@public_bp.route('/cart')
def cart():
    """View shopping cart"""
    cart_data = session.get('cart', {})
    cart_items = []
    subtotal = 0
    has_on_request = False

    for item_id_str, quantity in cart_data.items():
        item = Item.query.get(int(item_id_str))
        if item:
            if item.show_price_publicly:
                line_total = item.default_rental_price_per_day * quantity
                subtotal += line_total
            else:
                line_total = None
                has_on_request = True
            cart_items.append({
                'item': item,
                'quantity': quantity,
                'line_total': line_total
            })

    return render_template('public/cart.html',
                           cart_items=cart_items,
                           subtotal=subtotal,
                           has_on_request=has_on_request,
                           cart_count=sum(cart_data.values()))


@public_bp.route('/cart/add', methods=['POST'])
def cart_add():
    """Add item to cart (AJAX or form)"""
    item_id = request.form.get('item_id', type=int)
    quantity = request.form.get('quantity', 1, type=int)

    if not item_id or quantity < 1:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'error': 'Invalid request'}), 400
        flash('Ungültige Anfrage.', 'error')
        return redirect(url_for('public.catalog'))

    item = Item.query.get(item_id)
    if not item or not item.visible_in_shop:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'error': 'Item not found'}), 404
        flash('Artikel nicht gefunden.', 'error')
        return redirect(url_for('public.catalog'))

    cart = session.get('cart', {})
    key = str(item_id)
    cart[key] = cart.get(key, 0) + quantity
    session['cart'] = cart
    session.modified = True

    cart_count = sum(cart.values())

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'success': True, 'cart_count': cart_count})

    flash(f'{item.name} zum Warenkorb hinzugefügt!', 'success')
    return redirect(url_for('public.catalog'))


@public_bp.route('/cart/update', methods=['POST'])
def cart_update():
    """Update cart quantities"""
    cart = session.get('cart', {})

    for key in list(cart.keys()):
        qty = request.form.get(f'quantity_{key}', type=int)
        if qty is not None:
            if qty <= 0:
                del cart[key]
            else:
                cart[key] = qty

    session['cart'] = cart
    session.modified = True
    flash('Warenkorb aktualisiert.', 'success')
    return redirect(url_for('public.cart'))


@public_bp.route('/cart/remove/<int:item_id>', methods=['POST'])
def cart_remove(item_id):
    """Remove item from cart"""
    cart = session.get('cart', {})
    key = str(item_id)
    if key in cart:
        del cart[key]
        session['cart'] = cart
        session.modified = True

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'success': True, 'cart_count': sum(cart.values())})

    flash('Artikel aus dem Warenkorb entfernt.', 'success')
    return redirect(url_for('public.cart'))


@public_bp.route('/cart/clear', methods=['POST'])
def cart_clear():
    """Clear entire cart"""
    session.pop('cart', None)
    session.modified = True
    flash('Warenkorb geleert.', 'success')
    return redirect(url_for('public.cart'))


@public_bp.route('/inquiry', methods=['POST'])
def submit_inquiry():
    """Submit cart as customer inquiry"""
    cart_data = session.get('cart', {})
    if not cart_data:
        flash('Ihr Warenkorb ist leer.', 'error')
        return redirect(url_for('public.cart'))

    customer_name = request.form.get('customer_name', '').strip()
    customer_email = request.form.get('customer_email', '').strip()
    customer_phone = request.form.get('customer_phone', '').strip()
    message = request.form.get('message', '').strip()
    start_date_str = request.form.get('start_date', '').strip()
    end_date_str = request.form.get('end_date', '').strip()

    # --- Validation ---
    errors = []

    if not customer_name:
        errors.append('Name ist erforderlich.')
    if not customer_email:
        errors.append('E-Mail-Adresse ist erforderlich.')
    elif not re.match(r'^[^\s@]+@[^\s@]+\.[^\s@]+$', customer_email):
        errors.append('Bitte geben Sie eine gültige E-Mail-Adresse ein.')

    if not start_date_str:
        errors.append('Startdatum ist erforderlich.')
    if not end_date_str:
        errors.append('Enddatum ist erforderlich.')

    start_date = None
    end_date = None
    try:
        if start_date_str:
            start_date = datetime.strptime(start_date_str, '%Y-%m-%d')
        if end_date_str:
            end_date = datetime.strptime(end_date_str, '%Y-%m-%d')
    except ValueError:
        errors.append('Ungültiges Datumsformat.')

    today = datetime.combine(date.today(), datetime.min.time())
    if start_date and start_date < today:
        errors.append('Das Startdatum muss in der Zukunft liegen.')
    if end_date and end_date < today:
        errors.append('Das Enddatum muss in der Zukunft liegen.')
    if start_date and end_date and end_date < start_date:
        errors.append('Das Enddatum muss nach dem Startdatum liegen.')

    # Validate cart item quantities
    for item_id_str, quantity in cart_data.items():
        item = Item.query.get(int(item_id_str))
        if not item:
            errors.append(f'Artikel (ID {item_id_str}) nicht gefunden.')
        elif quantity < 1:
            errors.append(f'Ungültige Menge für {item.name}.')

    if errors:
        for err in errors:
            flash(err, 'error')
        return redirect(url_for('public.cart'))

    try:
        inquiry = Inquiry(
            customer_name=customer_name,
            customer_email=customer_email,
            customer_phone=customer_phone or None,
            message=message or None,
            desired_start_date=start_date,
            desired_end_date=end_date
        )
        db.session.add(inquiry)
        db.session.flush()

        for item_id_str, quantity in cart_data.items():
            item = Item.query.get(int(item_id_str))
            if item:
                inq_item = InquiryItem(
                    inquiry_id=inquiry.id,
                    item_id=item.id,
                    quantity=quantity,
                    price_snapshot=item.default_rental_price_per_day if item.show_price_publicly else None,
                    item_name_snapshot=item.name
                )
                db.session.add(inq_item)

        db.session.commit()

        # Send email notification
        settings = SiteSettings.query.first()
        send_inquiry_notification(inquiry, settings)

        # Clear cart
        session.pop('cart', None)
        session.modified = True

        return render_template('public/inquiry_sent.html', inquiry=inquiry)

    except Exception as e:
        db.session.rollback()
        flash(f'Fehler beim Senden der Anfrage: {str(e)}', 'error')
        return redirect(url_for('public.cart'))


@public_bp.route('/uploads/<path:filename>')
def uploaded_file(filename):
    """Serve uploaded files"""
    return send_from_directory(get_upload_path(), filename)
