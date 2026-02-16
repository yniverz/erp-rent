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
    is_external_user = db.Column(db.Boolean, default=False)
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    ownerships = db.relationship('ItemOwnership', back_populates='user', lazy='dynamic')

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
        # Can edit if user has an ownership entry for this item
        from models import ItemOwnership
        return ItemOwnership.query.filter_by(item_id=item.id, user_id=self.id).first() is not None


# Association table for item subcategories (many-to-many)
item_subcategories = db.Table('item_subcategories',
    db.Column('item_id', db.Integer, db.ForeignKey('item.id'), primary_key=True),
    db.Column('category_id', db.Integer, db.ForeignKey('category.id'), primary_key=True)
)


class PackageComponent(db.Model):
    """Component item within a package/bundle"""
    id = db.Column(db.Integer, primary_key=True)
    package_id = db.Column(db.Integer, db.ForeignKey('item.id'), nullable=False)
    component_item_id = db.Column(db.Integer, db.ForeignKey('item.id'), nullable=False)
    quantity = db.Column(db.Integer, nullable=False, default=1)

    package = db.relationship('Item', foreign_keys=[package_id], back_populates='package_components')
    component_item = db.relationship('Item', foreign_keys=[component_item_id])


class Category(db.Model):
    """Category for organizing inventory items – supports unlimited nesting"""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    display_order = db.Column(db.Integer, default=0)
    parent_id = db.Column(db.Integer, db.ForeignKey('category.id'), nullable=True)
    image_filename = db.Column(db.String(300), nullable=True)

    parent = db.relationship('Category', remote_side=[id], backref=db.backref('children', lazy='selectin', order_by='Category.display_order, Category.name'))
    items = db.relationship('Item', back_populates='category', lazy='dynamic')

    @property
    def ancestors(self):
        """Return list of ancestors from root down to (not including) self."""
        result = []
        current = self.parent
        while current:
            result.append(current)
            current = current.parent
        result.reverse()
        return result

    @property
    def depth(self):
        """Return nesting depth (0 = top-level)."""
        d = 0
        current = self.parent
        while current:
            d += 1
            current = current.parent
        return d

    def all_descendant_ids(self):
        """Return set of all descendant category ids (including self)."""
        ids = {self.id}
        for child in self.children:
            ids |= child.all_descendant_ids()
        return ids

    @staticmethod
    def get_tree(categories=None):
        """Return categories as a flat list with depth info, suitable for <select> rendering.
        Each entry is (category, depth).
        """
        if categories is None:
            categories = Category.query.order_by(Category.display_order, Category.name).all()
        root_cats = [c for c in categories if c.parent_id is None]
        result = []

        def _walk(cat, depth):
            result.append((cat, depth))
            for child in sorted(cat.children, key=lambda c: (c.display_order, c.name)):
                _walk(child, depth + 1)

        for cat in sorted(root_cats, key=lambda c: (c.display_order, c.name)):
            _walk(cat, 0)
        return result


class ItemOwnership(db.Model):
    """Per-user ownership/supply of an item.
    If external_price_per_day is set, this user is an external provider for this item.
    """
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey('item.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    quantity = db.Column(db.Integer, nullable=False, default=0)  # -1 for unlimited
    external_price_per_day = db.Column(db.Float, nullable=True)  # If set, this user is external provider
    purchase_cost = db.Column(db.Float, default=0.0)  # Total purchase cost (sum) for this owner's stock
    purchase_date = db.Column(db.DateTime, nullable=True)  # Date of purchase (required if purchase_cost > 0)

    __table_args__ = (db.UniqueConstraint('item_id', 'user_id', name='uq_item_user'),)

    item = db.relationship('Item', back_populates='ownerships')
    user = db.relationship('User')

    @property
    def is_external(self):
        return self.external_price_per_day is not None

    @property
    def total_purchase_cost(self):
        if self.is_external:
            return 0
        return round(self.purchase_cost or 0, 2)


class Item(db.Model):
    """Inventory item model"""
    id = db.Column(db.Integer, primary_key=True)
    category_id = db.Column(db.Integer, db.ForeignKey('category.id'), nullable=True)
    name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=True)
    default_rental_price_per_day = db.Column(db.Float, nullable=False, default=0)
    show_price_publicly = db.Column(db.Boolean, default=True)  # False = "on request"
    visible_in_shop = db.Column(db.Boolean, default=True)
    image_filename = db.Column(db.String(300), nullable=True)
    total_revenue = db.Column(db.Float, default=0.0)
    total_cost = db.Column(db.Float, default=0.0)  # Accumulated external rental costs
    is_package = db.Column(db.Boolean, default=False)
    show_bundle_discount = db.Column(db.Boolean, default=False)  # Show bundle price as discount in shop
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    category = db.relationship('Category', back_populates='items')
    subcategories = db.relationship('Category', secondary=item_subcategories, lazy='selectin')
    ownerships = db.relationship('ItemOwnership', back_populates='item',
                                  cascade='all, delete-orphan', lazy='selectin')
    quote_items = db.relationship('QuoteItem', back_populates='item',
                                   foreign_keys='QuoteItem.item_id',
                                   cascade='all, delete-orphan')
    package_components = db.relationship('PackageComponent', back_populates='package',
                                         foreign_keys='PackageComponent.package_id',
                                         cascade='all, delete-orphan', lazy='selectin')

    @property
    def total_quantity(self):
        """Sum of all ownership quantities.
        If any external has -1 and no internal owners have quantity, return -1.
        Otherwise sum all finite quantities (ignoring external -1s).
        """
        if not self.ownerships:
            return 0
        internal_qty = 0
        has_internal = False
        has_infinite_external = False
        finite_total = 0
        for o in self.ownerships:
            if o.is_external:
                if o.quantity == -1:
                    has_infinite_external = True
                else:
                    finite_total += o.quantity
            else:
                has_internal = True
                if o.quantity == -1:
                    return -1  # Internal unlimited = item is unlimited
                internal_qty += o.quantity
                finite_total += o.quantity
        if not has_internal and has_infinite_external:
            return -1
        return finite_total

    @property
    def internal_quantity(self):
        """Sum of quantities from non-external owners."""
        total = 0
        for o in self.ownerships:
            if not o.is_external:
                if o.quantity == -1:
                    return -1
                total += o.quantity
        return total

    @property
    def is_external(self):
        """True if there are ONLY external providers (no internal owners with quantity)."""
        if not self.ownerships:
            return False
        for o in self.ownerships:
            if not o.is_external and o.quantity > 0:
                return False
        return any(o.is_external and o.quantity != 0 for o in self.ownerships)

    @property
    def external_ownerships_sorted(self):
        """External ownerships sorted by price (cheapest first)."""
        return sorted(
            [o for o in self.ownerships if o.is_external],
            key=lambda o: o.external_price_per_day or 0
        )

    @property
    def total_purchase_cost(self):
        return round(sum(o.total_purchase_cost for o in self.ownerships), 2)

    @property
    def is_paid_off(self):
        if self.is_external:
            return False
        return self.total_revenue >= self.total_purchase_cost

    @property
    def remaining_to_payoff(self):
        if self.is_external:
            return 0
        remaining = self.total_purchase_cost - self.total_revenue
        return round(max(0, remaining), 2)

    @property
    def total_profit(self):
        """Net profit: revenue minus costs (purchase cost + external rental cost)"""
        return round(self.total_revenue - self.total_purchase_cost - self.total_cost, 2)

    @property
    def component_price_sum(self):
        """Sum of default rental prices of all components (for proportional splitting)"""
        if not self.is_package:
            return 0
        return sum(pc.component_item.default_rental_price_per_day * pc.quantity
                   for pc in self.package_components)

    def calculate_external_cost(self, quantity_needed):
        """Calculate the blended external cost per day for a given quantity.
        Uses internal stock first, then cheapest external sources.
        Returns (external_cost_per_day_total, breakdown) where breakdown
        is a list of (ownership, qty_used) tuples.
        """
        internal_qty = self.internal_quantity
        if internal_qty == -1:
            # Unlimited internal, no external cost
            return 0, []

        remaining = max(0, quantity_needed - internal_qty)
        if remaining == 0:
            return 0, []

        total_cost = 0
        breakdown = []
        for o in self.external_ownerships_sorted:
            if remaining <= 0:
                break
            if o.quantity == -1:
                # Unlimited external
                total_cost += remaining * (o.external_price_per_day or 0)
                breakdown.append((o, remaining))
                remaining = 0
            else:
                use = min(remaining, o.quantity)
                total_cost += use * (o.external_price_per_day or 0)
                breakdown.append((o, use))
                remaining -= use

        return round(total_cost, 2), breakdown


class Quote(db.Model):
    """Quote / rental agreement model"""
    id = db.Column(db.Integer, primary_key=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    customer_name = db.Column(db.String(200), nullable=False)
    recipient_lines = db.Column(db.Text, nullable=True)
    reference_number = db.Column(db.String(50), nullable=True)
    discount_percent = db.Column(db.Float, default=0.0)
    discount_label = db.Column(db.String(200), nullable=True)
    rental_days = db.Column(db.Integer, default=1)
    rental_days_override = db.Column(db.Integer, nullable=True)
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
        if self.rental_days_override:
            return self.rental_days_override
        if self.start_date and self.end_date:
            delta = self.end_date - self.start_date
            return max(1, delta.days + 1)
        return self.rental_days or 1

    def date_based_rental_days(self):
        """Always returns date-based calculation, ignoring override"""
        if self.start_date and self.end_date:
            delta = self.end_date - self.start_date
            return max(1, delta.days + 1)
        return self.rental_days or 1

    @property
    def subtotal(self):
        return round(sum(qi.total_price for qi in self.quote_items), 2)

    @property
    def discountable_subtotal(self):
        """Sum of line totals for items that are NOT exempt from discount"""
        return round(sum(qi.total_price for qi in self.quote_items if not qi.discount_exempt), 2)

    @property
    def discount_amount(self):
        return round(self.discountable_subtotal * (self.discount_percent / 100), 2)

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
    rental_cost_per_day = db.Column(db.Float, default=0)  # What we pay externally per day per item
    discount_exempt = db.Column(db.Boolean, default=False)  # If True, discount is not applied to this item
    custom_item_name = db.Column(db.String(200), nullable=True)
    is_custom = db.Column(db.Boolean, default=False)
    package_id = db.Column(db.Integer, db.ForeignKey('item.id'), nullable=True)  # If this is a component expanded from a package

    quote = db.relationship('Quote', back_populates='quote_items')
    item = db.relationship('Item', foreign_keys=[item_id], back_populates='quote_items')
    package = db.relationship('Item', foreign_keys=[package_id])  # The package this component belongs to

    @property
    def display_name(self):
        if self.is_custom:
            return self.custom_item_name or "Custom Item"
        return self.item.name if self.item else "Unknown Item"

    @property
    def total_price(self):
        days = self.quote.calculate_rental_days()
        return round(self.quantity * self.rental_price_per_day * days, 2)

    @property
    def total_external_cost(self):
        """Total cost we pay externally for this quote item"""
        days = self.quote.calculate_rental_days()
        return round(self.quantity * (self.rental_cost_per_day or 0) * days, 2)


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


class Customer(db.Model):
    """Saved customer for quick lookup by name"""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False, unique=True)
    recipient_lines = db.Column(db.Text, nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class SiteSettings(db.Model):
    """Global site settings"""
    id = db.Column(db.Integer, primary_key=True)
    # Business info (used in PDFs)
    business_name = db.Column(db.String(200), nullable=True)
    address_lines = db.Column(db.Text, nullable=True)
    contact_lines = db.Column(db.Text, nullable=True)
    bank_lines = db.Column(db.Text, nullable=True)
    # Tax / invoicing
    tax_number = db.Column(db.String(100), nullable=True)  # Steuernummer or USt-IdNr
    tax_mode = db.Column(db.String(20), default='kleinunternehmer')  # 'kleinunternehmer' or 'regular'
    payment_terms_days = db.Column(db.Integer, default=14)
    quote_validity_days = db.Column(db.Integer, default=14)
    logo_filename = db.Column(db.String(300), nullable=True)  # Uploaded logo file
    # Public storefront
    shop_description = db.Column(db.Text, nullable=True)
    # Legal links
    imprint_url = db.Column(db.String(500), nullable=True)
    privacy_url = db.Column(db.String(500), nullable=True)
    # AGB / Terms & Conditions (basic markdown)
    terms_and_conditions_text = db.Column(db.Text, nullable=True)
    # Notification
    notification_email = db.Column(db.String(200), nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
