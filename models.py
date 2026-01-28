from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()


class Item(db.Model):
    """Inventory item model"""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    total_quantity = db.Column(db.Integer, nullable=False)  # Total units owned
    set_size = db.Column(db.Integer, default=1)  # How many come in a set
    rental_step = db.Column(db.Integer, default=1)  # Minimum rental increment
    unit_purchase_cost = db.Column(db.Float, nullable=False)  # Cost per unit
    default_rental_price_per_day = db.Column(db.Float, nullable=False)  # Default rental price per unit per day
    total_revenue = db.Column(db.Float, default=0.0)  # Total revenue earned from this item type
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Relationships
    quote_items = db.relationship('QuoteItem', back_populates='item', cascade='all, delete-orphan')
    
    @property
    def total_purchase_cost(self):
        """Total amount spent purchasing all units"""
        return self.total_quantity * self.unit_purchase_cost
    
    @property
    def is_paid_off(self):
        """Check if item has been paid off"""
        return self.total_revenue >= self.total_purchase_cost
    
    @property
    def remaining_to_payoff(self):
        """Amount remaining until item is paid off"""
        return max(0, self.total_purchase_cost - self.total_revenue)


class Quote(db.Model):
    """Customer quote/rental order"""
    id = db.Column(db.Integer, primary_key=True)
    customer_name = db.Column(db.String(200), nullable=False)
    discount_percent = db.Column(db.Float, default=0.0)  # Discount percentage
    rental_days = db.Column(db.Integer, default=1)  # Number of rental days
    status = db.Column(db.String(50), default='draft')  # draft, finalized, paid
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    finalized_at = db.Column(db.DateTime, nullable=True)
    paid_at = db.Column(db.DateTime, nullable=True)
    notes = db.Column(db.Text, nullable=True)
    
    # Relationships
    quote_items = db.relationship('QuoteItem', back_populates='quote', cascade='all, delete-orphan')
    
    @property
    def subtotal(self):
        """Calculate subtotal before discount"""
        return sum(qi.total_price for qi in self.quote_items)
    
    @property
    def discount_amount(self):
        """Calculate discount amount"""
        return self.subtotal * (self.discount_percent / 100)
    
    @property
    def total(self):
        """Calculate total after discount"""
        return self.subtotal - self.discount_amount


class QuoteItem(db.Model):
    """Individual item in a quote"""
    id = db.Column(db.Integer, primary_key=True)
    quote_id = db.Column(db.Integer, db.ForeignKey('quote.id'), nullable=False)
    item_id = db.Column(db.Integer, db.ForeignKey('item.id'), nullable=True)  # Nullable for custom items
    quantity = db.Column(db.Integer, nullable=False)  # Number of units
    rental_price_per_day = db.Column(db.Float, nullable=False)  # Custom price for this quote
    custom_item_name = db.Column(db.String(200), nullable=True)  # For custom items like "Time"
    is_custom = db.Column(db.Boolean, default=False)  # Flag for custom/miscellaneous items
    
    # Relationships
    quote = db.relationship('Quote', back_populates='quote_items')
    item = db.relationship('Item', back_populates='quote_items')
    
    @property
    def display_name(self):
        """Get the display name for this item"""
        if self.is_custom:
            return self.custom_item_name or "Custom Item"
        return self.item.name if self.item else "Unknown Item"
    
    @property
    def total_price(self):
        """Calculate total price for this line item"""
        return self.quantity * self.rental_price_per_day * self.quote.rental_days
