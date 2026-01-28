# Electronics Rental ERP System

A Flask-based ERP system for managing electronics rental inventory, creating quotes, and tracking revenue.

## Features

### Inventory Management
- Add items with purchase details (total cost, quantity, set sizes)
- Set default rental prices per unit per day
- Define rental steps (e.g., can only rent in multiples of 2)
- Track total revenue per item type
- View payoff status (which items have earned back their purchase cost)

### Quote Creation & Management
- Create customer quotes with multiple items
- Dynamically see default rental prices but adjust them per customer
- Apply discounts (percentage-based)
- Save quotes as drafts or finalize them
- Mark quotes as paid (automatically updates item revenue)
- Generate printable receipts for insurance purposes

### Financial Tracking
- See which items/item types have been paid off
- Track revenue vs. purchase cost per item
- Payoff report showing status of all inventory

## Installation

1. **Clone or navigate to the project directory:**
   ```bash
   cd /Users/######/Documents/Privat/Scripts/github/yniverz/erp-rent
   ```

2. **Create a virtual environment (recommended):**
   ```bash
   python3 -m venv venv
   source venv/bin/activate  # On macOS/Linux
   ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

## Running the Application

1. **Start the Flask development server:**
   ```bash
   python app.py
   ```

2. **Open your browser and navigate to:**
   ```
   http://127.0.0.1:5000
   ```

## Usage Guide

### 1. Add Inventory Items

Example: You bought 4 lamps for 1000€ total, they come in pairs of 2.

1. Go to **Inventory** → **Add New Item**
2. Fill in:
   - **Item Name:** "LED Lamps"
   - **Total Quantity:** 4
   - **Set Size:** 2 (they come in pairs)
   - **Rental Step:** 2 (can only rent in multiples of 2)
   - **Total Purchase Cost:** 1000
   - **Default Rental Price per Day:** 50 (or whatever you want to charge)
3. Click **Add Item**

The system will calculate the unit purchase cost (250€ each in this example).

### 2. Create a Quote

1. Go to **Quotes** → **Create New Quote**
2. Enter:
   - **Customer Name:** Customer's name
   - **Rental Days:** How many days they're renting
3. Click **Create Quote**

### 3. Add Items to the Quote

1. You'll be redirected to the quote editing page
2. Select an item from the dropdown (you'll see the default price)
3. Enter the quantity (respecting rental steps if set)
4. Adjust the price per day if needed (default is pre-filled)
5. Click **Add Item**
6. Repeat for all items in the quote
7. You can adjust the discount percentage
8. The system automatically calculates subtotals and total

### 4. Finalize or Save Quote

- **Save Draft & Return:** Saves the quote as a draft to continue later
- **Finalize Quote:** Locks the quote and makes it ready for the customer

### 5. Mark as Paid

1. Go to **Quotes** list
2. For finalized quotes, click **Mark Paid**
3. This will:
   - Update the quote status to "Paid"
   - Add the quote total to the respective items' revenue
   - Update the payoff status

### 6. Generate Receipt

1. View any quote
2. Click **Receipt** or **View Receipt**
3. Print the receipt for insurance documentation
4. The receipt shows individual item values and rental details

### 7. Check Payoff Status

1. Go to **Payoff Report**
2. See which items have been paid off
3. View remaining amounts to payoff
4. Track overall financial status

## Database

The system uses SQLite (`erp_rent.db`) which is created automatically when you first run the app. The database includes:

- **Items:** Inventory items with pricing and revenue tracking
- **Quotes:** Customer quotes with status tracking
- **QuoteItems:** Line items in quotes linking items to quotes

## Project Structure

```
erp-rent/
├── app.py                 # Main Flask application
├── models.py              # Database models
├── requirements.txt       # Python dependencies
├── templates/
│   ├── base.html         # Base template with navigation
│   ├── index.html        # Home page
│   ├── inventory/
│   │   ├── list.html     # Inventory list
│   │   ├── add.html      # Add inventory item
│   │   └── edit.html     # Edit inventory item
│   ├── quotes/
│   │   ├── list.html     # Quotes list
│   │   ├── create.html   # Create quote
│   │   ├── edit.html     # Edit quote
│   │   ├── view.html     # View quote
│   │   └── receipt.html  # Printable receipt
│   └── reports/
│       └── payoff.html   # Payoff status report
└── erp_rent.db           # SQLite database (created on first run)
```

## Notes

- The system uses basic CSS for simplicity
- All prices are in Euros (€)
- The database is created automatically on first run
- Quotes can be saved as drafts and continued later
- Deleting items or quotes will cascade delete related data
- Revenue is only updated when quotes are marked as paid

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
