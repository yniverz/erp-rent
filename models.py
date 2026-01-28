from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()


class Settings(db.Model):
    """Application settings"""
    id = db.Column(db.Integer, primary_key=True)
    business_details = db.Column(db.Text, nullable=True)  # Free text field for business details
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


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
        return round(self.total_quantity * self.unit_purchase_cost, 2)
    
    @property
    def is_paid_off(self):
        """Check if item has been paid off"""
        return self.total_revenue >= self.total_purchase_cost
    
    @property
    def remaining_to_payoff(self):
        """Amount remaining until item is paid off"""
        return round(max(0, self.total_purchase_cost - self.total_revenue), 2)


class Quote(db.Model):
    """Customer quote/rental order"""
    id = db.Column(db.Integer, primary_key=True)
    customer_name = db.Column(db.String(200), nullable=False)
    discount_percent = db.Column(db.Float, default=0.0)  # Discount percentage
    rental_days = db.Column(db.Integer, default=1)  # Number of rental days (calculated)
    start_date = db.Column(db.DateTime, nullable=True)  # Rental start date
    end_date = db.Column(db.DateTime, nullable=True)  # Rental end date
    status = db.Column(db.String(50), default='draft')  # draft, finalized, paid
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    finalized_at = db.Column(db.DateTime, nullable=True)
    paid_at = db.Column(db.DateTime, nullable=True)
    notes = db.Column(db.Text, nullable=True)
    
    # Relationships
    quote_items = db.relationship('QuoteItem', back_populates='quote', cascade='all, delete-orphan')
    
    def calculate_rental_days(self):
        """Calculate rental days from start and end dates"""
        if self.start_date and self.end_date:
            delta = self.end_date - self.start_date
            # Same day counts as 1 day
            return max(1, delta.days + 1)
        return self.rental_days or 1
    
    @property
    def subtotal(self):
        """Calculate subtotal before discount"""
        return round(sum(qi.total_price for qi in self.quote_items), 2)
    
    @property
    def discount_amount(self):
        """Calculate discount amount"""
        return round(self.subtotal * (self.discount_percent / 100), 2)
    
    @property
    def total(self):
        """Calculate total after discount"""
        return round(self.subtotal - self.discount_amount, 2)


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
        days = self.quote.calculate_rental_days()
        return round(self.quantity * self.rental_price_per_day * days, 2)
