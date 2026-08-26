from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
import re
import unicodedata

db = SQLAlchemy()


def slugify(value):
    """Turn a name into a URL slug: lowercase, umlaut transliteration, hyphens."""
    value = (value or '').strip().lower()
    for src, repl in (('ä', 'ae'), ('ö', 'oe'), ('ü', 'ue'), ('ß', 'ss')):
        value = value.replace(src, repl)
    value = unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode('ascii')
    return re.sub(r'[^a-z0-9]+', '-', value).strip('-')


class User(UserMixin, db.Model):
    """User model for company members (staff logins)"""
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    display_name = db.Column(db.String(200), nullable=True)
    email = db.Column(db.String(200), nullable=True)
    is_admin = db.Column(db.Boolean, default=False)
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def is_active(self):
        return self.active

    def can_edit_item(self, item):
        """All active staff can edit all inventory (single-company model)."""
        return True


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
    name = db.Column(db.String(100), nullable=False)
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
    def slug(self):
        """URL slug derived from the name (falls back to id for exotic names)."""
        return slugify(self.name) or f'kat-{self.id}'

    @property
    def url_path(self):
        """Full slug path from root, e.g. 'beschallung/kabel'."""
        return '/'.join(c.slug for c in self.ancestors + [self])

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
    """LEGACY – superseded by Item.stock_quantity + ItemSupply.
    Kept only so existing DB rows remain readable during migration; not used by the app.
    """
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey('item.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    quantity = db.Column(db.Integer, nullable=False, default=0)
    external_price_per_day = db.Column(db.Float, nullable=True)
    external_price_is_brutto = db.Column(db.Boolean, default=True)
    purchase_cost = db.Column(db.Float, default=0.0)
    purchase_cost_is_brutto = db.Column(db.Boolean, default=True)


class Supplier(db.Model):
    """External supplier (Lieferant) – someone the company rents equipment FROM.
    Pure data record, cannot log in.
    """
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False, unique=True)
    email = db.Column(db.String(200), nullable=True)
    phone = db.Column(db.String(100), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    supplies = db.relationship('ItemSupply', back_populates='supplier',
                               cascade='all, delete-orphan', lazy='selectin')


class ItemSupply(db.Model):
    """An external supplier's offer for an item: how many they can provide
    and what they charge per unit per day.
    """
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey('item.id'), nullable=False)
    supplier_id = db.Column(db.Integer, db.ForeignKey('supplier.id'), nullable=False)
    quantity = db.Column(db.Integer, nullable=False, default=0)  # -1 = unlimited
    price_per_day = db.Column(db.Float, nullable=False, default=0.0)  # what supplier charges us
    price_is_brutto = db.Column(db.Boolean, default=True)

    item = db.relationship('Item', back_populates='supplies')
    supplier = db.relationship('Supplier', back_populates='supplies')


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
    # Own company stock (single-company model). -1 = unlimited.
    stock_quantity = db.Column(db.Integer, nullable=False, default=0)
    # Technical specs (Veranstaltungstechnik)
    manufacturer = db.Column(db.String(200), nullable=True)
    model_name = db.Column(db.String(200), nullable=True)
    weight_kg = db.Column(db.Float, nullable=True)
    power_watts = db.Column(db.Float, nullable=True)
    dimensions = db.Column(db.String(200), nullable=True)  # free text, e.g. "60 × 40 × 30 cm"
    storage_location = db.Column(db.String(200), nullable=True)  # Lagerort / Case
    replacement_value = db.Column(db.Float, nullable=True)  # Wiederbeschaffungswert (per unit)

    category = db.relationship('Category', back_populates='items')
    subcategories = db.relationship('Category', secondary=item_subcategories, lazy='selectin')
    supplies = db.relationship('ItemSupply', back_populates='item',
                               cascade='all, delete-orphan', lazy='selectin')
    quote_items = db.relationship('QuoteItem', back_populates='item',
                                   foreign_keys='QuoteItem.item_id',
                                   cascade='all, delete-orphan')
    package_components = db.relationship('PackageComponent', back_populates='package',
                                         foreign_keys='PackageComponent.package_id',
                                         cascade='all, delete-orphan', lazy='selectin')
    units = db.relationship('ItemUnit', back_populates='item',
                            cascade='all, delete-orphan', lazy='selectin',
                            order_by='ItemUnit.id')

    @property
    def total_quantity(self):
        """Own stock plus all supplier quantities. -1 = unlimited."""
        if self.stock_quantity == -1:
            return -1
        total = self.stock_quantity or 0
        has_infinite_supply = False
        for s in self.supplies:
            if s.quantity == -1:
                has_infinite_supply = True
            else:
                total += s.quantity
        if has_infinite_supply:
            return -1
        return total

    @property
    def is_external(self):
        """True if the item is ONLY available via suppliers (no own stock)."""
        if (self.stock_quantity or 0) != 0:
            return False
        return any(s.quantity != 0 for s in self.supplies)

    @property
    def supplies_sorted(self):
        """Supplier offers sorted by price (cheapest first)."""
        return sorted(self.supplies, key=lambda s: s.price_per_day or 0)

    @property
    def component_price_sum(self):
        """Sum of default rental prices of all components (for proportional splitting)"""
        if not self.is_package:
            return 0
        return sum(pc.component_item.default_rental_price_per_day * pc.quantity
                   for pc in self.package_components)

    @property
    def has_specs(self):
        """True if any public technical spec is set."""
        return any([self.manufacturer, self.model_name, self.weight_kg,
                    self.power_watts, self.dimensions])

    @property
    def out_of_service_count(self):
        """Number of tracked units that are not operational (defect/repair/retired)."""
        return sum(1 for u in self.units if u.status != ItemUnit.STATUS_AVAILABLE)

    @property
    def defect_count(self):
        """Number of tracked units that are defect or in repair (excludes retired)."""
        return sum(1 for u in self.units if u.status in (ItemUnit.STATUS_DEFECT, ItemUnit.STATUS_REPAIR))

    @property
    def operational_stock(self):
        """Own stock minus out-of-service tracked units. -1 = unlimited."""
        if self.stock_quantity == -1:
            return -1
        return max(0, (self.stock_quantity or 0) - self.out_of_service_count)

    @property
    def operational_quantity(self):
        """Operational own stock plus supplier quantities. -1 = unlimited."""
        own = self.operational_stock
        if own == -1:
            return -1
        total = own
        for s in self.supplies:
            if s.quantity == -1:
                return -1
            total += s.quantity
        return total

    def inspection_due_units(self, within_days=30):
        """Units whose next inspection (e.g. DGUV V3) is due within N days or overdue."""
        from datetime import date, timedelta
        cutoff = date.today() + timedelta(days=within_days)
        return [u for u in self.units
                if u.next_inspection_date and u.next_inspection_date <= cutoff
                and u.status != ItemUnit.STATUS_RETIRED]

    def calculate_external_cost(self, quantity_needed, own_available=None):
        """Calculate default external sourcing for a given quantity.
        Uses own stock first (or *own_available* if given), then cheapest suppliers.
        Returns (external_cost_per_day_total, breakdown) where breakdown
        is a list of (supply, qty_used) tuples.
        """
        own = self.operational_stock if own_available is None else own_available
        if own == -1:
            return 0, []

        remaining = max(0, quantity_needed - own)
        if remaining == 0:
            return 0, []

        total_cost = 0
        breakdown = []
        for s in self.supplies_sorted:
            if remaining <= 0:
                break
            if s.quantity == -1:
                total_cost += remaining * (s.price_per_day or 0)
                breakdown.append((s, remaining))
                remaining = 0
            else:
                use = min(remaining, s.quantity)
                if use > 0:
                    total_cost += use * (s.price_per_day or 0)
                    breakdown.append((s, use))
                    remaining -= use

        return round(total_cost, 2), breakdown


class ItemUnit(db.Model):
    """Individual physical unit of an inventory item (serial number tracking).
    Optional per item: items without units keep pure quantity-based tracking.
    Units with a non-available status reduce the item's operational quantity.
    """
    STATUS_AVAILABLE = 'available'
    STATUS_DEFECT = 'defect'
    STATUS_REPAIR = 'repair'
    STATUS_RETIRED = 'retired'
    STATUS_LABELS = {
        STATUS_AVAILABLE: 'Einsatzbereit',
        STATUS_DEFECT: 'Defekt',
        STATUS_REPAIR: 'In Reparatur',
        STATUS_RETIRED: 'Ausgemustert',
    }

    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey('item.id'), nullable=False)
    asset_tag = db.Column(db.String(50), unique=True, nullable=True)  # printed on QR label
    serial_number = db.Column(db.String(200), nullable=True)
    status = db.Column(db.String(20), nullable=False, default=STATUS_AVAILABLE)
    notes = db.Column(db.Text, nullable=True)
    purchase_date = db.Column(db.Date, nullable=True)
    last_inspection_date = db.Column(db.Date, nullable=True)  # letzte Prüfung (z.B. DGUV V3)
    next_inspection_date = db.Column(db.Date, nullable=True)  # nächste fällige Prüfung
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    item = db.relationship('Item', back_populates='units')

    @property
    def status_label(self):
        return self.STATUS_LABELS.get(self.status, self.status)

    @property
    def inspection_overdue(self):
        from datetime import date
        return bool(self.next_inspection_date and self.next_inspection_date < date.today())

    @property
    def inspection_due_soon(self):
        from datetime import date, timedelta
        return bool(self.next_inspection_date
                    and date.today() <= self.next_inspection_date <= date.today() + timedelta(days=30))

    def generate_asset_tag(self):
        """Generate a stable, human-readable asset tag from the unit id."""
        if not self.asset_tag and self.id:
            self.asset_tag = f"U{self.id:05d}"


class Quote(db.Model):
    """Quote / rental agreement model"""
    id = db.Column(db.Integer, primary_key=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    customer_name = db.Column(db.String(200), nullable=False)
    reference_number = db.Column(db.String(50), nullable=True)
    discount_percent = db.Column(db.Float, default=0.0)
    discount_label = db.Column(db.String(200), nullable=True)
    rental_days = db.Column(db.Integer, default=1)
    rental_days_override = db.Column(db.Integer, nullable=True)
    start_date = db.Column(db.DateTime, nullable=True)
    end_date = db.Column(db.DateTime, nullable=True)
    status = db.Column(db.String(50), default='draft')  # only 'draft' (Entwurf); finalization happens in an external tool
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    notes = db.Column(db.Text, nullable=True)  # Internal notes (not on PDF)
    public_notes = db.Column(db.Text, nullable=True)  # Shown on Angebot/Rechnung/Lieferschein
    inquiry_id = db.Column(db.Integer, db.ForeignKey('inquiry.id'), nullable=True)
    # Pricing
    # When True (and global tax_mode='regular'), all stored item prices are
    # treated as NET values and VAT is added on top in PDFs / API exports.
    # When False (default), stored prices are GROSS (brutto) – legacy behaviour.
    prices_are_net = db.Column(db.Boolean, default=False, nullable=False)

    created_by = db.relationship('User', foreign_keys=[created_by_id])
    quote_items = db.relationship('QuoteItem', back_populates='quote', cascade='all, delete-orphan',
                                  order_by='QuoteItem.position, QuoteItem.id')
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
    def billable_items(self):
        """Quote lines that count towards totals (no headings, no optional lines)."""
        return [qi for qi in self.quote_items if not qi.is_heading and not qi.is_optional]

    @property
    def subtotal(self):
        return round(sum(qi.total_price for qi in self.billable_items), 2)

    @property
    def optional_total(self):
        """Sum of optional line totals (shown on Angebot, not part of the total)."""
        return round(sum(qi.total_price for qi in self.quote_items
                         if qi.is_optional and not qi.is_heading), 2)

    @property
    def discountable_subtotal(self):
        """Sum of line totals for items that are NOT exempt from discount"""
        return round(sum(qi.total_price for qi in self.billable_items if not qi.discount_exempt), 2)

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
    # Ordering & presentation
    position = db.Column(db.Integer, nullable=False, default=0)  # manual sort order within the quote
    is_heading = db.Column(db.Boolean, default=False)  # free-text section heading (uses custom_item_name)
    is_optional = db.Column(db.Boolean, default=False)  # shown on Angebot, excluded from totals

    quote = db.relationship('Quote', back_populates='quote_items')
    item = db.relationship('Item', foreign_keys=[item_id], back_populates='quote_items')
    package = db.relationship('Item', foreign_keys=[package_id])  # The package this component belongs to
    sources = db.relationship('QuoteItemSource', back_populates='quote_item',
                              cascade='all, delete-orphan', lazy='selectin')
    assigned_units = db.relationship('QuoteItemUnit', back_populates='quote_item',
                                     cascade='all, delete-orphan', lazy='selectin')

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

    @property
    def sourced_quantity(self):
        """Units of this line covered by supplier sourcing."""
        return sum(s.quantity for s in self.sources)

    def recalc_cost_from_sources(self):
        """Derive blended rental_cost_per_day from sourcing rows."""
        total = sum(s.quantity * (s.price_per_day or 0) for s in self.sources)
        self.rental_cost_per_day = round(total / self.quantity, 4) if self.quantity else 0


class QuoteItemUnit(db.Model):
    """A specific physical unit (serial number) assigned to a quote line
    during picking (Packliste). asset_tag/serial_number are snapshotted so
    delivery history survives unit deletion.
    """
    id = db.Column(db.Integer, primary_key=True)
    quote_item_id = db.Column(db.Integer, db.ForeignKey('quote_item.id'), nullable=False)
    item_unit_id = db.Column(db.Integer, db.ForeignKey('item_unit.id'), nullable=True)
    asset_tag = db.Column(db.String(50), nullable=True)
    serial_number = db.Column(db.String(200), nullable=True)

    quote_item = db.relationship('QuoteItem', back_populates='assigned_units')
    unit = db.relationship('ItemUnit')

    @property
    def label(self):
        parts = [p for p in [self.asset_tag, self.serial_number] if p]
        return ' / '.join(parts) if parts else f'#{self.item_unit_id or self.id}'


class QuoteItemSource(db.Model):
    """Which supplier provides how many units for a quote line (manual sourcing).
    supplier_name is snapshotted so quote history survives supplier deletion.
    """
    id = db.Column(db.Integer, primary_key=True)
    quote_item_id = db.Column(db.Integer, db.ForeignKey('quote_item.id'), nullable=False)
    supplier_id = db.Column(db.Integer, db.ForeignKey('supplier.id'), nullable=True)
    supplier_name = db.Column(db.String(200), nullable=False)
    quantity = db.Column(db.Integer, nullable=False, default=0)
    price_per_day = db.Column(db.Float, nullable=False, default=0.0)  # per unit per day

    quote_item = db.relationship('QuoteItem', back_populates='sources')
    supplier = db.relationship('Supplier')


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
    display_name = db.Column(db.String(200), nullable=True)  # Alias for website / storefront
    address_lines = db.Column(db.Text, nullable=True)
    contact_lines = db.Column(db.Text, nullable=True)
    bank_lines = db.Column(db.Text, nullable=True)
    # Tax / invoicing
    tax_number = db.Column(db.String(100), nullable=True)  # Steuernummer (z.B. 12/345/67890)
    vat_id = db.Column(db.String(100), nullable=True)  # USt-IdNr (z.B. DE123456789)
    tax_mode = db.Column(db.String(20), default='kleinunternehmer')  # 'kleinunternehmer' or 'regular'
    tax_rate = db.Column(db.Float, default=19.0)  # MwSt-Satz in %, configurable
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
