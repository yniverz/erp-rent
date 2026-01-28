# German Document Template Customization Guide

## Überlassungsbestätigung Template

The German document template (`templates/quotes/german_doc.html`) uses placeholders that you can customize.

### Placeholders to Replace:

1. **Company Information:**
   - `{{COMPANY_NAME}}` - Your company name
   - `{{COMPANY_ADDRESS}}` - Your full address
   - `{{COMPANY_PHONE}}` - Your phone number
   - `{{COMPANY_EMAIL}}` - Your email address

2. **Rental Dates:**
   - `{{START_DATE}}` - Start date of rental (format: DD.MM.YYYY)
   - `{{END_DATE}}` - End date of rental (format: DD.MM.YYYY)

3. **Legal Text:**
   - `{{SPECIAL_NOTES}}` - Special agreements or notes
   - `{{TERMS_AND_CONDITIONS}}` - Your general terms and conditions
   - `{{LIABILITY_INSURANCE_TEXT}}` - Liability and insurance information

### How to Customize:

1. Open `templates/quotes/german_doc.html`
2. Search for the placeholders (they have yellow highlighting in the browser)
3. Replace them with your actual information

### Example:

**Before:**
```html
<span class="field-value placeholder">{{COMPANY_NAME}}</span>
```

**After:**
```html
<span class="field-value">Meine Vermietungsfirma GmbH</span>
```

**Note:** Remove the `placeholder` class after replacing to remove the yellow highlighting.

### Dynamic Fields (Automatically Filled):

These are filled automatically from your quote data:
- Customer name: `{{ quote.customer_name }}`
- Quote number: `#{{ quote.id }}`
- Creation date: `{{ quote.created_at.strftime('%d.%m.%Y') }}`
- Rental days: `{{ quote.rental_days }}`
- Total amount: `{{ quote.total }}`
- Item list with quantities

### Features:

- **No individual prices shown** - Only the total amount is displayed
- **Items listed in table** - All rented equipment is listed by name and quantity
- **German formatting** - Dates in DD.MM.YYYY format, € currency
- **Professional layout** - Clean, printable document
- **Signature sections** - For both lessor and lessee
