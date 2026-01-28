from flask import Flask, render_template, request, redirect, url_for, flash
from models import db, Item, Quote, QuoteItem
from datetime import datetime
import os

app = Flask(__name__)
app.config['SECRET_KEY'] = 'dev-secret-key-change-in-production'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///erp_rent.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)


# Initialize database
with app.app_context():
    db.create_all()


@app.route('/')
def index():
    """Home page"""
    return render_template('index.html')


# ============= INVENTORY MANAGEMENT =============

@app.route('/inventory')
def inventory_list():
    """List all inventory items"""
    items = Item.query.order_by(Item.name).all()
    return render_template('inventory/list.html', items=items)


@app.route('/inventory/add', methods=['GET', 'POST'])
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
def quote_list():
    """List all quotes"""
    quotes = Quote.query.order_by(Quote.created_at.desc()).all()
    return render_template('quotes/list.html', quotes=quotes)


@app.route('/quotes/create', methods=['GET', 'POST'])
def quote_create():
    """Create new quote"""
    if request.method == 'POST':
        try:
            customer_name = request.form.get('customer_name')
            rental_days = int(request.form.get('rental_days', 1))
            
            # Create new quote
            quote = Quote(
                customer_name=customer_name,
                rental_days=rental_days,
                status='draft'
            )
            
            db.session.add(quote)
            db.session.commit()
            
            flash(f'Quote created for {customer_name}!', 'success')
            return redirect(url_for('quote_edit', quote_id=quote.id))
            
        except Exception as e:
            flash(f'Error creating quote: {str(e)}', 'error')
            db.session.rollback()
    
    return render_template('quotes/create.html')


@app.route('/quotes/<int:quote_id>/edit', methods=['GET', 'POST'])
def quote_edit(quote_id):
    """Edit quote and add items"""
    quote = Quote.query.get_or_404(quote_id)
    items = Item.query.order_by(Item.name).all()
    
    if request.method == 'POST':
        action = request.form.get('action')
        
        try:
            if action == 'update_quote':
                quote.customer_name = request.form.get('customer_name')
                quote.rental_days = int(request.form.get('rental_days'))
                quote.discount_percent = float(request.form.get('discount_percent', 0))
                quote.notes = request.form.get('notes', '')
                db.session.commit()
                flash('Quote updated!', 'success')
                
            elif action == 'update_items':
                # Update all inventory items
                for item in items:
                    quantity_key = f'quantity_{item.id}'
                    price_key = f'price_{item.id}'
                    
                    if quantity_key in request.form:
                        quantity = int(request.form.get(quantity_key, 0))
                        price = float(request.form.get(price_key, item.default_rental_price_per_day))
                        
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
                
                db.session.commit()
                flash('Items updated!', 'success')
                
            elif action == 'add_custom':
                custom_name = request.form.get('custom_name')
                custom_quantity = int(request.form.get('custom_quantity', 1))
                custom_price = float(request.form.get('custom_price'))
                
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
                    
            elif action == 'finalize':
                quote.status = 'finalized'
                quote.finalized_at = datetime.utcnow()
                db.session.commit()
                flash('Quote finalized!', 'success')
                return redirect(url_for('quote_view', quote_id=quote.id))
                
        except Exception as e:
            flash(f'Error: {str(e)}', 'error')
            db.session.rollback()
    
    return render_template('quotes/edit.html', quote=quote, items=items)


@app.route('/quotes/<int:quote_id>')
def quote_view(quote_id):
    """View quote details"""
    quote = Quote.query.get_or_404(quote_id)
    return render_template('quotes/view.html', quote=quote)


@app.route('/quotes/<int:quote_id>/unfinalize', methods=['POST'])
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
def quote_mark_paid(quote_id):
    """Mark quote as paid and update item revenue"""
    quote = Quote.query.get_or_404(quote_id)
    
    try:
        if quote.status != 'paid':
            quote.status = 'paid'
            quote.paid_at = datetime.utcnow()
            
            # Update revenue for each item (only non-custom items)
            for quote_item in quote.quote_items:
                if not quote_item.is_custom and quote_item.item:
                    quote_item.item.total_revenue += quote_item.total_price
            
            db.session.commit()
            flash('Quote marked as paid and revenue updated!', 'success')
        else:
            flash('Quote is already marked as paid.', 'info')
            
    except Exception as e:
        flash(f'Error marking quote as paid: {str(e)}', 'error')
        db.session.rollback()
    
    return redirect(url_for('quote_view', quote_id=quote_id))


@app.route('/quotes/<int:quote_id>/receipt')
def quote_receipt(quote_id):
    """Generate receipt/insurance document"""
    quote = Quote.query.get_or_404(quote_id)
    return render_template('quotes/receipt.html', quote=quote)


@app.route('/quotes/<int:quote_id>/german-doc')
def quote_german_doc(quote_id):
    """Generate German Überlassungsbestätigung document"""
    quote = Quote.query.get_or_404(quote_id)
    return render_template('quotes/german_doc.html', quote=quote)


@app.route('/quotes/<int:quote_id>/delete', methods=['POST'])
def quote_delete(quote_id):
    """Delete quote"""
    quote = Quote.query.get_or_404(quote_id)
    
    try:
        db.session.delete(quote)
        db.session.commit()
        flash('Quote deleted!', 'success')
    except Exception as e:
        flash(f'Error deleting quote: {str(e)}', 'error')
        db.session.rollback()
    
    return redirect(url_for('quote_list'))


# ============= REPORTS =============

@app.route('/reports/payoff')
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


if __name__ == '__main__':
    app.run(debug=True)
