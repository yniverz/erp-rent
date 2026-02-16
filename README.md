[![License: NCPUL](https://img.shields.io/badge/license-NCPUL-blue.svg)](./LICENSE.md)

# ERP-Rent

An equipment rental management system with a public-facing shop and an admin back-office. Manage your rental inventory, handle customer inquiries, create quotes, generate invoices, and track finances — all in one place.

> The UI is in **German**. PDF documents (quotes, invoices, delivery notes) are generated in German as well.

---

## Quick Start (Docker)

The easiest way to run ERP-Rent is with Docker.

1. **Clone the repository:**
   ```bash
   git clone https://github.com/yniverz/erp-rent.git
   cd erp-rent
   ```

2. **Create a `.env` file** with your configuration (see [Configuration](#configuration) below):
   ```env
   SECRET_KEY=your-random-secret-key
   ADMIN_USERNAME=admin
   ADMIN_PASSWORD=change-me
   ```

3. **Start the application:**
   ```bash
   docker compose up -d
   ```

4. **Open** `http://localhost:5000` in your browser.

The SQLite database and uploaded files are stored in the `instance/` directory, which is mounted as a volume so your data persists across container restarts.

---

## Manual Setup (without Docker)

1. **Clone the repository:**
   ```bash
   git clone https://github.com/yniverz/erp-rent.git
   cd erp-rent
   ```

2. **Create a virtual environment and install dependencies:**
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

3. **Set environment variables** (or create a `.env` file):
   ```env
   SECRET_KEY=your-random-secret-key
   ADMIN_USERNAME=admin
   ADMIN_PASSWORD=change-me
   ```

4. **Run the application:**
   ```bash
   python app.py
   ```

5. **Open** `http://localhost:5000` in your browser.

On first launch, the database is created automatically and a default admin account is set up using the credentials from your environment variables (defaults to `admin` / `password123` if not set).

---

## Configuration

All configuration is done through environment variables (or a `.env` file in the project root).

| Variable | Required | Default | Description |
|---|---|---|---|
| `SECRET_KEY` | **Yes** | `dev-secret-key...` | Flask session secret. Set a long random string in production. |
| `ADMIN_USERNAME` | No | `admin` | Username for the initial admin account (only used on first launch). |
| `ADMIN_PASSWORD` | No | `password123` | Password for the initial admin account (only used on first launch). |
| `SMTP_SERVER` | No | — | SMTP server for sending inquiry notification emails. |
| `SMTP_PORT` | No | `587` | SMTP port. |
| `SMTP_USER` | No | — | SMTP login username. |
| `SMTP_PASSWORD` | No | — | SMTP login password. |
| `SMTP_FROM` | No | Same as `SMTP_USER` | Sender address for notification emails. |
| `FAVICON_URL` | No | — | URL to an image to use as the site favicon. |

> **Email notifications:** When a customer submits an inquiry through the public shop, a notification email is sent to the address configured in Admin → Settings. For this to work, the SMTP variables must be set.

---

## Features

### Public Shop

The public-facing storefront is accessible without login at the root URL (`/`).

- **Catalog** — Browse rental items organized in a hierarchical category tree. Categories can be nested to any depth and can have images.
- **Search** — Full-text search across item names, descriptions, and categories.
- **Item details** — View item description, image, and rental price per day. Prices can be hidden per item ("on request").
- **Cart** — Add items to a session-based cart (no login required), adjust quantities, and review the selection.
- **Inquiry** — Submit a rental inquiry with your contact details, desired rental dates, and a message. The shop owner receives an email notification.

### Admin Panel

Log in at `/admin` with your admin or staff credentials to access the back-office.

#### Dashboard
Overview of total inventory items, quotes, new inquiries, and active quotes.

#### Inventory
- Add, edit, and delete rental items with name, description, price, image, and category.
- **Ownership system** — Each item can have multiple owners (users) who each contribute a certain quantity. This lets you track who owns what and how much stock is available.
- **External providers** — Mark a user as an external provider with a per-day cost. The system tracks what you pay externally vs. what you charge customers.
- **Packages / Bundles** — Create virtual items that are composed of multiple component items. When added to a quote, packages automatically expand into their components with prices distributed proportionally. Bundles can optionally show a discount compared to renting items individually.
- **Visibility controls** — Hide items from the public shop or hide their price ("on request").

#### Categories
- Unlimited nesting depth for organizing items.
- Each category can have a display order and an image.
- Items can belong to one primary category and multiple subcategories.

#### Quotes
Full lifecycle management from draft to paid:

1. **Create** — Enter customer name, recipient address, and rental period (start/end dates). A reference number is auto-generated.
2. **Edit** — Add inventory items (availability is checked against the date range), add packages (auto-expanded into components), or add custom free-text line items. Adjust quantities, prices, and per-item external costs.
3. **Discounts** — Set a discount percentage or enter a target total and let the system calculate the percentage. Individual items can be marked as discount-exempt. You can also add a label to the discount (e.g., "Loyalty discount").
4. **Rental days override** — Optionally bill for a different number of days than the actual date range.
5. **Finalize** — Lock the quote. The system checks inventory availability and warns about overbooking.
6. **Mark as paid** — Records payment and updates cumulative revenue and cost tracking on each item.

Each step can be undone (unfinalize, unpay) and quotes can be deleted at any time.

#### PDF Documents
Generate professional German-language PDF documents directly from any quote:

- **Angebot (Quote)** — Itemized quote with positions, quantities, daily rates, totals, discount, tax handling, payment terms, and validity period. Includes AGB (terms & conditions) as an appendix if configured.
- **Rechnung (Invoice)** — Same layout as the quote but with invoice-specific language, the service period, and payment instructions with bank details.
- **Lieferschein / Übergabeprotokoll (Delivery Note)** — Lists items and quantities without prices. Includes a condition/comment column for handwritten notes, a deposit field, and signature sections for both handover and return.

All PDFs include your company logo (if uploaded), address, contact details, tax number, and bank information as configured in settings.

**Tax modes:**
- **Kleinunternehmer** (§19 UStG) — No VAT is calculated or shown. A notice is printed on the document.
- **Regular** — 19% VAT is calculated and displayed with net, VAT, and gross amounts.

#### Inquiries
- View all customer inquiries from the public shop.
- Update inquiry status: New → Contacted → Converted → Closed.
- **Convert to quote** — Creates a new draft quote pre-filled with the customer's details, desired dates, and requested items.

#### Schedule
Monthly calendar view showing all quotes and open inquiries with their rental periods. Color-coded by status (draft, finalized, paid, inquiry). Navigate between months and click entries to view details.

#### Payoff Report
Financial overview of your inventory:
- **Owned items** — Revenue vs. purchase cost, payoff status, remaining amount to break even, and net profit.
- **External items** — Revenue vs. external rental costs.
- Per-user ownership breakdown.

#### User Management (Admin only)
- Create and manage user accounts with roles:
  - **Admin** — Full access to everything.
  - **Staff** — Can manage inventory they own and handle quotes/inquiries. No access to settings or user management.
  - **External user** — Represents an external equipment provider. Cannot log in. Their items and per-day costs are tracked in the system.
- Activate/deactivate accounts, reset passwords.

#### Settings (Admin only)
- **Business info** — Company name, address, contact details, bank details (used in PDFs and the storefront).
- **Logo** — Upload a company logo displayed on PDF documents.
- **Tax** — Tax number/USt-IdNr and tax mode (Kleinunternehmer or regular 19% VAT).
- **Payment & quote terms** — Configurable payment deadline and quote validity period in days.
- **Shop description** — Text shown on the public storefront.
- **Legal links** — Impressum and Datenschutz (privacy) URLs for the shop footer.
- **AGB (Terms & Conditions)** — Markdown-formatted text that is appended as an annex to quote PDFs. Supports headings (`#`, `##`, `###`), bold (`**text**`), and paragraphs.
- **Notification email** — Address that receives email alerts when customers submit inquiries.

---

## Data Storage

ERP-Rent uses **SQLite** as its database — no external database server needed. The database file and uploaded images are stored in the `instance/` directory. Back up this directory to preserve all your data.

---

## Customer Database

The system maintains a customer database for quick lookup. When creating a quote, you can search for existing customers to auto-fill their name and address. Customer records are saved and updated automatically.

## Future Enhancements (Optional)

- User authentication
- PDF generation for receipts
- Email notifications
- Advanced reporting (revenue by period, etc.)
- Item availability checking
- Multi-currency support
- Barcode/QR code generation

## Support

For issues or questions, check the code comments or modify as needed. The system is designed to be straightforward and easy to customize.
