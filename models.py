from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime

db = SQLAlchemy()


class User(UserMixin, db.Model):
    """User model for company members"""
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    display_name = db.Column(db.String(200), nullable=True)
    email = db.Column(db.String(200), nullable=True)
    is_admin = db.Column(db.Boolean, default=False)
    can_edit_all = db.Column(db.Boolean, default=False)
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    items = db.relationship('Item', back_populates='owner', lazy='dynamic')

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def is_active(self):
        return self.active

    def can_edit_item(self, item):
        """Check if this user can edit a given item"""
        if self.is_admin or self.can_edit_all:
            return True
        return item.owner_id == self.id


class Category(db.Model):
    """Category for organizing inventory items"""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    display_order = db.Column(db.Integer, default=0)

    items = db.relationship('Item', back_populates='category', lazy='dynamic')


class Item(db.Model):
    """Inventory item model"""
    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey('category.id'), nullable=True)
    name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=True)
    total_quantity = db.Column(db.Integer, nullable=False)  # -1 for unlimited
    set_size = db.Column(db.Integer, default=1)
    rental_step = db.Column(db.Integer, default=1)
    unit_purchase_cost = db.Column(db.Float, nullable=False, default=0)
    default_rental_price_per_day = db.Column(db.Float, nullable=False, default=0)
    show_price_publicly = db.Column(db.Boolean, default=True)  # False = "on request"
    visible_in_shop = db.Column(db.Boolean, default=True)
    image_filename = db.Column(db.String(300), nullable=True)
    total_revenue = db.Column(db.Float, default=0.0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    owner = db.relationship('User', back_populates='items')
    category = db.relationship('Category', back_populates='items')
    quote_items = db.relationship('QuoteItem', back_populates='item', cascade='all, delete-orphan')

    @property
    def total_purchase_cost(self):
        if self.total_quantity == -1:
            return 0
        return round(self.total_quantity * self.unit_purchase_cost, 2)

    @property
    def is_paid_off(self):
        return self.total_revenue >= self.total_purchase_cost

    @property
    def remaining_to_payoff(self):
        remaining = self.total_purchase_cost - self.total_revenue
        return round(max(0, remaining), 2)


class Quote(db.Model):
    """Quote / rental agreement model"""
    id = db.Column(db.Integer, primary_key=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    customer_name = db.Column(db.String(200), nullable=False)
    recipient_lines = db.Column(db.Text, nullable=True)
    reference_number = db.Column(db.String(50), nullable=True)
    discount_percent = db.Column(db.Float, default=0.0)
    rental_days = db.Column(db.Integer, default=1)
    start_date = db.Column(db.DateTime, nullable=True)
    end_date = db.Column(db.DateTime, nullable=True)
    status = db.Column(db.String(50), default='draft')  # draft, finalized, paid
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    finalized_at = db.Column(db.DateTime, nullable=True)
    paid_at = db.Column(db.DateTime, nullable=True)
    notes = db.Column(db.Text, nullable=True)
    inquiry_id = db.Column(db.Integer, db.ForeignKey('inquiry.id'), nullable=True)

    created_by = db.relationship('User', foreign_keys=[created_by_id])
    quote_items = db.relationship('QuoteItem', back_populates='quote', cascade='all, delete-orphan')
    inquiry = db.relationship('Inquiry', foreign_keys=[inquiry_id], back_populates='converted_quote')

    def generate_reference_number(self):
        if not self.reference_number:
            date_part = self.created_at.strftime('%Y%m%d')
            self.reference_number = f"RE{date_part}{self.id:04d}"

    def calculate_rental_days(self):
        if self.start_date and self.end_date:
            delta = self.end_date - self.start_date
            return max(1, delta.days + 1)
        return self.rental_days or 1

    @property
    def subtotal(self):
        return round(sum(qi.total_price for qi in self.quote_items), 2)

    @property
    def discount_amount(self):
        return round(self.subtotal * (self.discount_percent / 100), 2)

    @property
    def total(self):
        return round(self.subtotal - self.discount_amount, 2)


class QuoteItem(db.Model):
    """Individual item in a quote"""
    id = db.Column(db.Integer, primary_key=True)
    quote_id = db.Column(db.Integer, db.ForeignKey('quote.id'), nullable=False)
    item_id = db.Column(db.Integer, db.ForeignKey('item.id'), nullable=True)
    quantity = db.Column(db.Integer, nullable=False)
    rental_price_per_day = db.Column(db.Float, nullable=False)
    custom_item_name = db.Column(db.String(200), nullable=True)
    is_custom = db.Column(db.Boolean, default=False)

    quote = db.relationship('Quote', back_populates='quote_items')
    item = db.relationship('Item', back_populates='quote_items')

    @property
    def display_name(self):
        if self.is_custom:
            return self.custom_item_name or "Custom Item"
        return self.item.name if self.item else "Unknown Item"

    @property
    def total_price(self):
        days = self.quote.calculate_rental_days()
        return round(self.quantity * self.rental_price_per_day * days, 2)


class Inquiry(db.Model):
    """Customer inquiry from public storefront"""
    id = db.Column(db.Integer, primary_key=True)
    customer_name = db.Column(db.String(200), nullable=False)
    customer_email = db.Column(db.String(200), nullable=False)
    customer_phone = db.Column(db.String(50), nullable=True)
    message = db.Column(db.Text, nullable=True)
    desired_start_date = db.Column(db.DateTime, nullable=True)
    desired_end_date = db.Column(db.DateTime, nullable=True)
    status = db.Column(db.String(50), default='new')  # new, contacted, converted, closed
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    items = db.relationship('InquiryItem', back_populates='inquiry', cascade='all, delete-orphan')
    converted_quote = db.relationship('Quote', back_populates='inquiry', uselist=False,
                                       foreign_keys='Quote.inquiry_id')


class InquiryItem(db.Model):
    """Individual item in a customer inquiry"""
    id = db.Column(db.Integer, primary_key=True)
    inquiry_id = db.Column(db.Integer, db.ForeignKey('inquiry.id'), nullable=False)
    item_id = db.Column(db.Integer, db.ForeignKey('item.id'), nullable=False)
    quantity = db.Column(db.Integer, nullable=False)
    price_snapshot = db.Column(db.Float, nullable=True)  # null if item is "on request"
    item_name_snapshot = db.Column(db.String(200), nullable=False)

    inquiry = db.relationship('Inquiry', back_populates='items')
    item = db.relationship('Item')


class SiteSettings(db.Model):
    """Global site settings"""
    id = db.Column(db.Integer, primary_key=True)
    # Business info (used in PDFs)
    business_name = db.Column(db.String(200), nullable=True)
    address_lines = db.Column(db.Text, nullable=True)
    contact_lines = db.Column(db.Text, nullable=True)
    bank_lines = db.Column(db.Text, nullable=True)
    # Public storefront
    shop_description = db.Column(db.Text, nullable=True)
    # Legal links
    imprint_url = db.Column(db.String(500), nullable=True)
    privacy_url = db.Column(db.String(500), nullable=True)
    # Notification
    notification_email = db.Column(db.String(200), nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
