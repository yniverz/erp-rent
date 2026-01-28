from io import BytesIO
from flask import Flask, render_template, request, redirect, send_file, url_for, flash, session
from models import db, Item, Quote, QuoteItem, Settings
from datetime import datetime
from sqlalchemy import and_, or_
from functools import wraps
from dotenv import load_dotenv
import os

# Load environment variables
load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-secret-key-change-in-production')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///erp_rent.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)


def login_required(f):
    """Decorator to require login for routes"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session:
            flash('Please log in to access this page.', 'error')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function


def get_available_quantity(item_id, start_date, end_date, exclude_quote_id=None):
    """
    Calculate available quantity for an item during a specific date range.
    Considers overlapping quotes that are finalized or paid.
    """
    item = Item.query.get(item_id)
    if not item:
        return 0
    
    # Find all overlapping quotes (finalized or paid)
    overlapping_quotes = Quote.query.filter(
        Quote.status.in_(['finalized', 'paid']),
        Quote.start_date.isnot(None),
        Quote.end_date.isnot(None),
        or_(
            # Quote starts during our period
            and_(Quote.start_date <= end_date, Quote.start_date >= start_date),
            # Quote ends during our period
            and_(Quote.end_date <= end_date, Quote.end_date >= start_date),
            # Quote encompasses our entire period
            and_(Quote.start_date <= start_date, Quote.end_date >= end_date)
        )
    )
    
    # Exclude current quote being edited
    if exclude_quote_id:
        overlapping_quotes = overlapping_quotes.filter(Quote.id != exclude_quote_id)
    
    overlapping_quotes = overlapping_quotes.all()
    
    # Calculate total quantity already booked
    booked_quantity = 0
    for quote in overlapping_quotes:
        for quote_item in quote.quote_items:
            if quote_item.item_id == item_id and not quote_item.is_custom:
                booked_quantity += quote_item.quantity
    
    # Return available quantity
    available = item.total_quantity - booked_quantity
    return max(0, available)


# Initialize database
with app.app_context():
    db.create_all()
    
    # Run migrations automatically
    import sqlite3
    db_path = os.path.join(os.path.dirname(__file__), 'instance', 'erp_rent.db')
    
    if os.path.exists(db_path):
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Check and add new settings fields
        cursor.execute("PRAGMA table_info(settings)")
        columns = [col[1] for col in cursor.fetchall()]
        
        if 'business_name' not in columns:
            print("Running migration: Adding new settings fields...")
            cursor.execute("ALTER TABLE settings ADD COLUMN business_name VARCHAR(200)")
            cursor.execute("ALTER TABLE settings ADD COLUMN address_lines TEXT")
            cursor.execute("ALTER TABLE settings ADD COLUMN contact_lines TEXT")
            cursor.execute("ALTER TABLE settings ADD COLUMN bank_lines TEXT")
            conn.commit()
            print("Migration completed: Settings fields added")
        
        # Check and add quote fields
        cursor.execute("PRAGMA table_info(quote)")
        quote_columns = [col[1] for col in cursor.fetchall()]
        
        if 'recipient_lines' not in quote_columns:
            print("Running migration: Adding recipient_lines to quote...")
            cursor.execute("ALTER TABLE quote ADD COLUMN recipient_lines TEXT")
            conn.commit()
            print("Migration completed: recipient_lines added")
        
        if 'reference_number' not in quote_columns:
            print("Running migration: Adding reference_number to quote...")
            cursor.execute("ALTER TABLE quote ADD COLUMN reference_number VARCHAR(50)")
            conn.commit()
            
            # Generate reference numbers for existing quotes
            cursor.execute("SELECT id, created_at FROM quote WHERE reference_number IS NULL")
            quotes = cursor.fetchall()
            for quote_id, created_at in quotes:
                # Parse created_at (format: YYYY-MM-DD HH:MM:SS.mmmmmm)
                date_part = created_at[:10].replace('-', '')
                ref_no = f"RE{date_part}{quote_id:04d}"
                cursor.execute("UPDATE quote SET reference_number = ? WHERE id = ?", (ref_no, quote_id))
            conn.commit()
            print(f"Migration completed: Generated reference numbers for {len(quotes)} existing quotes")
        
        conn.close()


# Authentication routes
@app.route('/login', methods=['GET', 'POST'])
def login():
    """Login page"""
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        admin_username = os.getenv('ADMIN_USERNAME')
        admin_password = os.getenv('ADMIN_PASSWORD')
        
        if username == admin_username and password == admin_password:
            session['logged_in'] = True
            session['username'] = username
            flash('Login successful!', 'success')
            return redirect(url_for('index'))
        else:
            flash('Invalid credentials. Please try again.', 'error')
    
    return render_template('login.html')


@app.route('/logout')
def logout():
    """Logout user"""
    session.clear()
    flash('You have been logged out.', 'success')
    return redirect(url_for('login'))


@app.route('/')
@login_required
def index():
    """Home page"""
    return render_template('index.html')


# ============= INVENTORY MANAGEMENT =============

@app.route('/inventory')
@login_required
def inventory_list():
    """List all inventory items"""
    items = Item.query.order_by(Item.name).all()
    return render_template('inventory/list.html', items=items)


@app.route('/inventory/add', methods=['GET', 'POST'])
@login_required
def inventory_add():
    """Add new inventory item"""
    if request.method == 'POST':
        try:
            # Get form data
            name = request.form.get('name')
            total_quantity = int(request.form.get('total_quantity'))
            set_size = int(request.form.get('set_size', 1))
            rental_step = int(request.form.get('rental_step', 1))
            total_cost = float(request.form.get('total_cost'))
            default_rental_price = float(request.form.get('default_rental_price'))
            
            # Calculate unit purchase cost
            unit_purchase_cost = total_cost / total_quantity
            
            # Create new item
            item = Item(
                name=name,
                total_quantity=total_quantity,
                set_size=set_size,
                rental_step=rental_step,
                unit_purchase_cost=unit_purchase_cost,
                default_rental_price_per_day=default_rental_price
            )
            
            db.session.add(item)
            db.session.commit()
            
            flash(f'Successfully added {name} to inventory!', 'success')
            return redirect(url_for('inventory_list'))
            
        except Exception as e:
            flash(f'Error adding item: {str(e)}', 'error')
            db.session.rollback()
    
    return render_template('inventory/add.html')


@app.route('/inventory/<int:item_id>/edit', methods=['GET', 'POST'])
@login_required
def inventory_edit(item_id):
    """Edit inventory item"""
    item = Item.query.get_or_404(item_id)
    
    if request.method == 'POST':
        try:
            item.name = request.form.get('name')
            item.total_quantity = int(request.form.get('total_quantity'))
            item.set_size = int(request.form.get('set_size', 1))
            item.rental_step = int(request.form.get('rental_step', 1))
            item.default_rental_price_per_day = float(request.form.get('default_rental_price'))
            
            # Recalculate unit cost if total cost is provided
            if 'total_cost' in request.form and request.form.get('total_cost'):
                total_cost = float(request.form.get('total_cost'))
                item.unit_purchase_cost = total_cost / item.total_quantity
            
            db.session.commit()
            flash(f'Successfully updated {item.name}!', 'success')
            return redirect(url_for('inventory_list'))
            
        except Exception as e:
            flash(f'Error updating item: {str(e)}', 'error')
            db.session.rollback()
    
    return render_template('inventory/edit.html', item=item)


@app.route('/inventory/<int:item_id>/delete', methods=['POST'])
@login_required
def inventory_delete(item_id):
    """Delete inventory item"""
    item = Item.query.get_or_404(item_id)
    
    try:
        name = item.name
        db.session.delete(item)
        db.session.commit()
        flash(f'Successfully deleted {name}!', 'success')
    except Exception as e:
        flash(f'Error deleting item: {str(e)}', 'error')
        db.session.rollback()
    
    return redirect(url_for('inventory_list'))


# ============= QUOTE MANAGEMENT =============

@app.route('/quotes')
@login_required
def quote_list():
    """List all quotes"""
    quotes = Quote.query.order_by(Quote.created_at.desc()).all()
    return render_template('quotes/list.html', quotes=quotes)


@app.route('/quotes/create', methods=['GET', 'POST'])
@login_required
def quote_create():
    """Create new quote"""
    if request.method == 'POST':
        try:
            customer_name = request.form.get('customer_name')
            start_date_str = request.form.get('start_date')
            end_date_str = request.form.get('end_date')
            
            # Parse dates
            start_date = datetime.strptime(start_date_str, '%Y-%m-%d') if start_date_str else None
            end_date = datetime.strptime(end_date_str, '%Y-%m-%d') if end_date_str else None
            
            # Validate dates
            if start_date and end_date and start_date > end_date:
                flash('End date must be after or equal to start date!', 'error')
                return render_template('quotes/create.html')
            
            # Calculate rental days
            rental_days = 1
            if start_date and end_date:
                delta = end_date - start_date
                rental_days = max(1, delta.days + 1)
            
            # Create new quote
            quote = Quote(
                customer_name=customer_name,
                start_date=start_date,
                end_date=end_date,
                rental_days=rental_days,
                status='draft'
            )
            
            db.session.add(quote)
            db.session.commit()
            
            # Generate reference number after commit (needs ID)
            quote.generate_reference_number()
            db.session.commit()
            
            flash(f'Quote created for {customer_name}!', 'success')
            return redirect(url_for('quote_edit', quote_id=quote.id))
            
        except Exception as e:
            flash(f'Error creating quote: {str(e)}', 'error')
            db.session.rollback()
    
    return render_template('quotes/create.html')


@app.route('/quotes/<int:quote_id>/edit', methods=['GET', 'POST'])
@login_required
def quote_edit(quote_id):
    """Edit quote and add items"""
    quote = Quote.query.get_or_404(quote_id)
    items = Item.query.order_by(Item.name).all()
    
    if request.method == 'POST':
        action = request.form.get('action')
        
        try:
            if action == 'update_quote':
                quote.customer_name = request.form.get('customer_name')
                start_date_str = request.form.get('start_date')
                end_date_str = request.form.get('end_date')
                
                # Parse dates
                start_date = datetime.strptime(start_date_str, '%Y-%m-%d') if start_date_str else None
                end_date = datetime.strptime(end_date_str, '%Y-%m-%d') if end_date_str else None
                
                # Validate dates
                if start_date and end_date and start_date > end_date:
                    flash('End date must be after or equal to start date!', 'error')
                    return render_template('quotes/edit.html', quote=quote, items=items)
                
                quote.start_date = start_date
                quote.end_date = end_date
                
                # Calculate rental days
                if start_date and end_date:
                    delta = end_date - start_date
                    quote.rental_days = max(1, delta.days + 1)
                else:
                    quote.rental_days = int(request.form.get('rental_days', 1))
                
                quote.discount_percent = float(request.form.get('discount_percent', 0))
                quote.recipient_lines = request.form.get('recipient_lines', '')
                quote.notes = request.form.get('notes', '')
                db.session.commit()
                flash('Quote updated!', 'success')
                
            elif action == 'update_items':
                # Check if dates are set
                if not quote.start_date or not quote.end_date:
                    flash('Please set start and end dates before adding items!', 'error')
                    return render_template('quotes/edit.html', quote=quote, items=items)
                
                # Update all inventory items
                errors = []
                for item in items:
                    quantity_key = f'quantity_{item.id}'
                    price_key = f'price_{item.id}'
                    
                    if quantity_key in request.form:
                        quantity = int(request.form.get(quantity_key, 0))
                        price = round(float(request.form.get(price_key, item.default_rental_price_per_day)), 2)
                        
                        if quantity > 0:
                            # Get available quantity for this date range
                            available = get_available_quantity(
                                item.id, 
                                quote.start_date, 
                                quote.end_date,
                                exclude_quote_id=quote.id
                            )
                            
                            # Validate quantity against available stock
                            if quantity > available:
                                errors.append(f'{item.name}: Only {available} available during this period (total: {item.total_quantity})')
                                continue
                            
                            # Validate rental step
                            if item.rental_step > 1 and quantity % item.rental_step != 0:
                                errors.append(f'{item.name}: Quantity must be a multiple of {item.rental_step}')
                                continue
                        
                        # Find existing quote item
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
                            # Remove if quantity is 0
                            if existing:
                                db.session.delete(existing)
                
                if errors:
                    flash('Errors: ' + '; '.join(errors), 'error')
                    db.session.rollback()
                else:
                    db.session.commit()
                    flash('Items updated!', 'success')
                
            elif action == 'add_custom':
                custom_name = request.form.get('custom_name')
                custom_quantity = int(request.form.get('custom_quantity', 1))
                custom_price = round(float(request.form.get('custom_price')), 2)
                
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
                    flash(f'Custom item "{custom_name}" added!', 'success')
                    
            elif action == 'remove_item':
                quote_item_id = int(request.form.get('quote_item_id'))
                quote_item = QuoteItem.query.get(quote_item_id)
                if quote_item and quote_item.quote_id == quote.id:
                    db.session.delete(quote_item)
                    db.session.commit()
                    flash('Item removed from quote!', 'success')
                    
            elif action == 'update_discount':
                discount_percent = float(request.form.get('final_discount_percent', 0))
                quote.discount_percent = discount_percent
                db.session.commit()
                flash(f'Pricing updated! Discount: {discount_percent:.4f}%', 'success')
                
            elif action == 'finalize':
                # Validate all items are still available before finalizing
                if not quote.start_date or not quote.end_date:
                    flash('Cannot finalize quote: Start and end dates must be set!', 'error')
                    # Recalculate availability for display
                    item_availability = {}
                    for item in items:
                        item_availability[item.id] = item.total_quantity
                    return render_template('quotes/edit.html', quote=quote, items=items, item_availability=item_availability)
                
                validation_errors = []
                for quote_item in quote.quote_items:
                    if not quote_item.is_custom and quote_item.item:
                        available = get_available_quantity(
                            quote_item.item_id,
                            quote.start_date,
                            quote.end_date,
                            exclude_quote_id=quote.id
                        )
                        
                        if quote_item.quantity > available:
                            validation_errors.append(
                                f'{quote_item.item.name}: Only {available} available during this period (quote has {quote_item.quantity})'
                            )
                
                if validation_errors:
                    flash('Cannot finalize quote due to availability issues: ' + '; '.join(validation_errors), 'error')
                    # Recalculate availability for all items to show proper greying out
                    item_availability = {}
                    for item in items:
                        item_availability[item.id] = get_available_quantity(
                            item.id,
                            quote.start_date,
                            quote.end_date,
                            exclude_quote_id=quote.id
                        )
                    return render_template('quotes/edit.html', quote=quote, items=items, item_availability=item_availability)
                
                quote.status = 'finalized'
                quote.finalized_at = datetime.utcnow()
                db.session.commit()
                flash('Quote finalized!', 'success')
                return redirect(url_for('quote_view', quote_id=quote.id))
                
        except Exception as e:
            flash(f'Error: {str(e)}', 'error')
            db.session.rollback()
    
    # Calculate available quantities for each item
    item_availability = {}
    if quote.start_date and quote.end_date:
        for item in items:
            item_availability[item.id] = get_available_quantity(
                item.id,
                quote.start_date,
                quote.end_date,
                exclude_quote_id=quote.id
            )
    else:
        # No dates set, show total quantity
        for item in items:
            item_availability[item.id] = item.total_quantity
    
    return render_template('quotes/edit.html', quote=quote, items=items, item_availability=item_availability)


@app.route('/quotes/<int:quote_id>')
@login_required
def quote_view(quote_id):
    """View quote details"""
    quote = Quote.query.get_or_404(quote_id)
    return render_template('quotes/view.html', quote=quote)


@app.route('/quotes/<int:quote_id>/unfinalize', methods=['POST'])
@login_required
def quote_unfinalize(quote_id):
    """Undo finalization of a quote"""
    quote = Quote.query.get_or_404(quote_id)
    
    try:
        if quote.status == 'finalized':
            quote.status = 'draft'
            quote.finalized_at = None
            db.session.commit()
            flash('Quote returned to draft status!', 'success')
        else:
            flash('Quote is not finalized.', 'info')
            
    except Exception as e:
        flash(f'Error unfinalizing quote: {str(e)}', 'error')
        db.session.rollback()
    
    return redirect(url_for('quote_edit', quote_id=quote_id))


@app.route('/quotes/<int:quote_id>/mark_paid', methods=['POST'])
@login_required
def quote_mark_paid(quote_id):
    """Mark quote as paid and update item revenue"""
    quote = Quote.query.get_or_404(quote_id)
    
    try:
        if quote.status != 'paid':
            quote.status = 'paid'
            quote.paid_at = datetime.utcnow()
            
            # Calculate the actual revenue after discount
            # The discount is distributed proportionally across all items
            subtotal = quote.subtotal
            discount_multiplier = (100 - quote.discount_percent) / 100
            
            # Update revenue for each item (only non-custom items)
            for quote_item in quote.quote_items:
                if not quote_item.is_custom and quote_item.item:
                    # Calculate this item's share of the discounted total
                    item_revenue = round(quote_item.total_price * discount_multiplier, 2)
                    quote_item.item.total_revenue = round(quote_item.item.total_revenue + item_revenue, 2)
            
            db.session.commit()
            flash('Quote marked as paid and revenue updated!', 'success')
        else:
            flash('Quote is already marked as paid.', 'info')
            
    except Exception as e:
        flash(f'Error marking quote as paid: {str(e)}', 'error')
        db.session.rollback()
    
    return redirect(url_for('quote_view', quote_id=quote_id))


@app.route('/quotes/<int:quote_id>/receipt')
@login_required
def quote_receipt(quote_id):
    """Generate receipt/insurance document"""
    quote = Quote.query.get_or_404(quote_id)
    settings = Settings.query.first()
    return render_template('quotes/receipt.html', quote=quote, settings=settings)


@app.get("/quotes/<int:quote_id>/ueberlassungsbestaetigung.pdf")
@login_required
def ueberlassungsbestaetigung_pdf(quote_id, with_total=False):
    from generators.ueberlassungsbestaetigung import _build_pdf_bytes
    quote = Quote.query.get_or_404(quote_id)
    timeframe_str = ""
    if quote.start_date and quote.end_date:
        f = quote.start_date.strftime("%d.%m.%Y")
        t = quote.end_date.strftime("%d.%m.%Y")
        if f == t:
            timeframe_str = f
        else:
            timeframe_str = f"{f} - {t}"
    else:
        timeframe_str = "#Datum nicht festgelegt#"
    f = timeframe_str

    settings = Settings.query.first()
    # Build consignor info from structured settings fields
    consignor_info = []
    if settings:
        if settings.business_name:
            consignor_info.append(settings.business_name)
        if settings.address_lines:
            consignor_info.extend([line for line in settings.address_lines.split("\n") if line.strip()])
    pdf_bytes = _build_pdf_bytes(consignor_info=consignor_info, timeframe_str=timeframe_str, items=[q.display_name for q in quote.quote_items], total_sum=float(quote.total) if with_total else "__________")
    
    response = send_file(
        BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=False,
        download_name="ueberlassungsbestaetigung.pdf",
        max_age=0,
    )
    
    # Prevent caching
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    
    return response

@app.route('/quotes/<int:quote_id>/unpay', methods=['POST'])
@login_required
def quote_unpay(quote_id):
    """Unpay quote and revert revenue"""
    quote = Quote.query.get_or_404(quote_id)
    
    try:
        if quote.status == 'paid':
            # Revert the revenue from items
            discount_multiplier = (100 - quote.discount_percent) / 100
            for quote_item in quote.quote_items:
                if not quote_item.is_custom and quote_item.item:
                    item_revenue = round(quote_item.total_price * discount_multiplier, 2)
                    quote_item.item.total_revenue = round(quote_item.item.total_revenue - item_revenue, 2)
            
            quote.status = 'finalized'
            quote.paid_at = None
            db.session.commit()
            flash('Quote unpaid and revenue reverted!', 'success')
        else:
            flash('Quote is not marked as paid.', 'info')
            
    except Exception as e:
        flash(f'Error unpaying quote: {str(e)}', 'error')
        db.session.rollback()
    
    return redirect(url_for('quote_view', quote_id=quote_id))


@app.route('/quotes/<int:quote_id>/delete', methods=['POST'])
@login_required
def quote_delete(quote_id):
    """Delete quote and revert revenue if paid"""
    quote = Quote.query.get_or_404(quote_id)
    
    try:
        # If quote was paid, subtract the revenue from items
        if quote.status == 'paid':
            discount_multiplier = (100 - quote.discount_percent) / 100
            for quote_item in quote.quote_items:
                if not quote_item.is_custom and quote_item.item:
                    item_revenue = round(quote_item.total_price * discount_multiplier, 2)
                    quote_item.item.total_revenue = round(quote_item.item.total_revenue - item_revenue, 2)
        
        db.session.delete(quote)
        db.session.commit()
        flash('Quote deleted!', 'success')
    except Exception as e:
        flash(f'Error deleting quote: {str(e)}', 'error')
        db.session.rollback()
    
    return redirect(url_for('quote_list'))


# ============= REPORTS =============

@app.route('/reports/payoff')
@login_required
def report_payoff():
    """Show payoff status for all items"""
    items = Item.query.order_by(Item.name).all()
    
    # Calculate miscellaneous revenue from custom items in paid quotes
    misc_revenue = db.session.query(db.func.sum(
        QuoteItem.quantity * QuoteItem.rental_price_per_day * Quote.rental_days
    )).join(Quote).filter(
        QuoteItem.is_custom == True,
        Quote.status == 'paid'
    ).scalar() or 0.0
    
    return render_template('reports/payoff.html', items=items, misc_revenue=misc_revenue)


@app.route('/schedule')
@login_required
def schedule():
    """View rental schedule/calendar"""
    from datetime import timedelta
    
    # Get all quotes with dates (excluding drafts)
    quotes = Quote.query.filter(
        Quote.status.in_(['finalized', 'paid']),
        Quote.start_date.isnot(None),
        Quote.end_date.isnot(None)
    ).order_by(Quote.start_date).all()
    
    return render_template('schedule.html', quotes=quotes, timedelta=timedelta)


@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    """Application settings"""
    # Get or create settings record
    settings_record = Settings.query.first()
    if not settings_record:
        settings_record = Settings()
        db.session.add(settings_record)
        db.session.commit()
    
    if request.method == 'POST':
        try:
            settings_record.business_name = request.form.get('business_name', '')
            settings_record.address_lines = request.form.get('address_lines', '')
            settings_record.contact_lines = request.form.get('contact_lines', '')
            settings_record.bank_lines = request.form.get('bank_lines', '')
            settings_record.updated_at = datetime.utcnow()
            db.session.commit()
            flash('Settings saved successfully!', 'success')
        except Exception as e:
            flash(f'Error saving settings: {str(e)}', 'error')
            db.session.rollback()
    
    return render_template('settings.html', settings=settings_record)


@app.route('/quotes/<int:quote_id>/kostenbeteiligung.pdf')
@login_required
def kostenbeteiligung_pdf(quote_id):
    """Generate Kostenbeteiligung/Rechnung PDF"""
    from generators.kostenbeteiligung import build_rechnung_pdf_bytes
    
    quote = Quote.query.get_or_404(quote_id)
    settings_record = Settings.query.first()
    
    # Prepare date range
    if quote.start_date and quote.end_date:
        start_str = quote.start_date.strftime("%d.%m.%Y")
        end_str = quote.end_date.strftime("%d.%m.%Y")
        bereitstellungszeitraum = (start_str, end_str)
    else:
        bereitstellungszeitraum = ("XX.XX.20XX", "XX.XX.20XX")
    
    # Get settings or use defaults
    issuer_name = settings_record.business_name if settings_record and settings_record.business_name else "Your Business"
    address_lines = settings_record.address_lines.split('\n') if settings_record and settings_record.address_lines else []
    contact_lines = settings_record.contact_lines.split('\n') if settings_record and settings_record.contact_lines else []
    bank_lines = settings_record.bank_lines.split('\n') if settings_record and settings_record.bank_lines else []
    
    # Get recipient lines from quote
    recipient_lines = quote.recipient_lines.split('\n') if quote.recipient_lines else [quote.customer_name]
    
    # Generate PDF
    pdf_bytes = build_rechnung_pdf_bytes(
        issuer_name=issuer_name,
        issuer_address_lines=[line.strip() for line in address_lines if line.strip()],
        issuer_contact_lines=[line.strip() for line in contact_lines if line.strip()],
        bank_lines=[line.strip() for line in bank_lines if line.strip()],
        recipient_lines=[line.strip() for line in recipient_lines if line.strip()],
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
    
    # Prevent caching
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    
    return response


if __name__ == '__main__':
    app.run(debug=False)
