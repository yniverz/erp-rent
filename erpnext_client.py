"""
ERPNext API client for Journal Entry creation and Customer lookup.
Activated via ENV variables: ERPNEXT_ENABLED, ERPNEXT_URL, ERPNEXT_USER, ERPNEXT_PASSWORD
"""

import os
import requests


def is_erpnext_enabled():
    """Check if ERPNext integration is enabled via env."""
    return os.getenv('ERPNEXT_ENABLED', '').lower() in ('1', 'true', 'yes')


def _get_config():
    """Return ERPNext connection config from env."""
    url = os.getenv('ERPNEXT_URL', '').rstrip('/')
    user = os.getenv('ERPNEXT_USER', '')
    password = os.getenv('ERPNEXT_PASSWORD', '')
    if not all([url, user, password]):
        raise RuntimeError('ERPNext ist aktiviert, aber ERPNEXT_URL, ERPNEXT_USER oder ERPNEXT_PASSWORD fehlen.')
    return url, user, password


def _get_session():
    """Create an authenticated requests session for ERPNext."""
    url, user, password = _get_config()
    session = requests.Session()
    # Login via /api/method/login
    resp = session.post(f'{url}/api/method/login', data={
        'usr': user,
        'pwd': password,
    }, timeout=15)
    if resp.status_code != 200:
        raise RuntimeError(f'ERPNext Login fehlgeschlagen: {resp.status_code} – {resp.text[:300]}')
    return session, url


def _api_get(endpoint, params=None):
    """Authenticated GET request to ERPNext API."""
    session, url = _get_session()
    resp = session.get(f'{url}{endpoint}', params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def _api_post(endpoint, data=None, json_data=None):
    """Authenticated POST request to ERPNext API."""
    session, url = _get_session()
    resp = session.post(f'{url}{endpoint}', data=data, json=json_data, timeout=15)
    if resp.status_code >= 400:
        raise RuntimeError(f'ERPNext API Fehler ({resp.status_code}): {resp.text[:500]}')
    return resp.json()


def _api_put(endpoint, json_data=None):
    """Authenticated PUT request to ERPNext API."""
    session, url = _get_session()
    resp = session.put(f'{url}{endpoint}', json=json_data, timeout=15)
    if resp.status_code >= 400:
        raise RuntimeError(f'ERPNext API Fehler ({resp.status_code}): {resp.text[:500]}')
    return resp.json()


# ============= COMPANY & ACCOUNTS =============

def get_companies():
    """Fetch list of companies from ERPNext."""
    data = _api_get('/api/resource/Company', params={
        'fields': '["name","company_name"]',
        'limit_page_length': 0,
        'order_by': 'name asc',
    })
    return [c['name'] for c in data.get('data', [])]


def get_accounts(company, account_type=None, root_type=None):
    """Fetch chart of accounts from ERPNext, optionally filtered.

    Args:
        company: Company name
        account_type: e.g. 'Receivable', 'Income Account', 'Tax', 'Bank'
        root_type: e.g. 'Asset', 'Liability', 'Income', 'Expense'
    """
    filters = [['company', '=', company], ['is_group', '=', 0]]
    if account_type:
        filters.append(['account_type', '=', account_type])
    if root_type:
        filters.append(['root_type', '=', root_type])

    data = _api_get('/api/resource/Account', params={
        'filters': str(filters),
        'fields': '["name","account_name","account_number","account_type","root_type"]',
        'limit_page_length': 0,
        'order_by': 'name asc',
    })
    return data.get('data', [])


def get_receivable_accounts(company):
    """Get accounts suitable for receivables (Forderungen)."""
    return get_accounts(company, account_type='Receivable')


def get_revenue_accounts(company):
    """Get income accounts (Erlöse)."""
    return get_accounts(company, root_type='Income')


def get_tax_accounts(company):
    """Get tax/liability accounts (Umsatzsteuer)."""
    return get_accounts(company, root_type='Liability')


def get_bank_accounts(company):
    """Get bank & cash accounts."""
    return get_accounts(company, account_type='Bank') + get_accounts(company, account_type='Cash')


# ============= CUSTOMERS =============

def search_customers(query, limit=10):
    """Search customers in ERPNext by name."""
    filters = [['customer_name', 'like', f'%{query}%']]
    data = _api_get('/api/resource/Customer', params={
        'filters': str(filters),
        'fields': '["name","customer_name"]',
        'limit_page_length': limit,
        'order_by': 'customer_name asc',
    })
    results = []
    for c in data.get('data', []):
        # Fetch address for this customer
        address_lines = _get_customer_address(c['name'])
        results.append({
            'name': c['customer_name'],
            'erpnext_id': c['name'],
            'recipient_lines': address_lines,
        })
    return results


def get_customer(customer_name):
    """Get a single customer by name from ERPNext."""
    filters = [['customer_name', '=', customer_name]]
    data = _api_get('/api/resource/Customer', params={
        'filters': str(filters),
        'fields': '["name","customer_name"]',
        'limit_page_length': 1,
    })
    customers = data.get('data', [])
    if not customers:
        return None
    c = customers[0]
    address_lines = _get_customer_address(c['name'])
    return {
        'name': c['customer_name'],
        'erpnext_id': c['name'],
        'recipient_lines': address_lines,
    }


def _get_customer_address(customer_id):
    """Fetch the primary address for a customer from ERPNext."""
    try:
        # Get linked addresses via Dynamic Link
        links = _api_get('/api/resource/Dynamic Link', params={
            'filters': str([
                ['link_doctype', '=', 'Customer'],
                ['link_name', '=', customer_id],
                ['parenttype', '=', 'Address'],
            ]),
            'fields': '["parent"]',
            'limit_page_length': 5,
        })
        address_names = [l['parent'] for l in links.get('data', [])]
        if not address_names:
            return ''

        # Get the first address details
        addr_data = _api_get(f'/api/resource/Address/{address_names[0]}')
        addr = addr_data.get('data', {})
        parts = []
        if addr.get('address_line1'):
            parts.append(addr['address_line1'])
        if addr.get('address_line2'):
            parts.append(addr['address_line2'])
        city_line = ''
        if addr.get('pincode'):
            city_line += addr['pincode'] + ' '
        if addr.get('city'):
            city_line += addr['city']
        if city_line:
            parts.append(city_line.strip())
        if addr.get('country') and addr['country'] != 'Germany':
            parts.append(addr['country'])
        return '\n'.join(parts)
    except Exception:
        return ''


# ============= JOURNAL ENTRIES =============

def create_journal_entry(company, posting_date, accounts, user_remark='', reference_number='', bill_no=''):
    """Create a Journal Entry in ERPNext.

    Args:
        company: Company name
        posting_date: Date string 'YYYY-MM-DD'
        accounts: List of dicts with keys:
            - account: Account name
            - debit_in_account_currency: float (0 if credit)
            - credit_in_account_currency: float (0 if debit)
            - party_type: str (optional, e.g. 'Customer')
            - party: str (optional, customer name)
        user_remark: Description/comment
        reference_number: e.g. invoice reference
        bill_no: Bill number for reference

    Returns:
        Journal Entry name (e.g. 'ACC-JV-2024-00001')
    """
    je_data = {
        'doctype': 'Journal Entry',
        'company': company,
        'posting_date': posting_date,
        'user_remark': user_remark,
        'bill_no': bill_no,
        'cheque_no': reference_number,
        'cheque_date': posting_date,
        'accounts': accounts,
    }

    result = _api_post('/api/resource/Journal Entry', json_data=je_data)
    je_name = result.get('data', {}).get('name')
    if not je_name:
        raise RuntimeError(f'Journal Entry konnte nicht erstellt werden: {result}')

    # Submit the Journal Entry (docstatus=1)
    _api_put(f'/api/resource/Journal Entry/{je_name}', json_data={
        'docstatus': 1,
    })

    return je_name


def cancel_journal_entry(je_name):
    """Cancel (stornieren) a submitted Journal Entry in ERPNext.

    Uses the amend/cancel workflow: PUT with docstatus=2.
    """
    if not je_name:
        return
    try:
        _api_put(f'/api/resource/Journal Entry/{je_name}', json_data={
            'docstatus': 2,
        })
    except Exception as e:
        raise RuntimeError(f'Journal Entry {je_name} konnte nicht storniert werden: {e}')


# ============= HIGH-LEVEL BOOKING FUNCTIONS =============

def book_receivable(quote, settings):
    """Create Journal Entry for 'Durchgeführt': Forderungen an Erlöse (+USt).

    Debit: Forderungskonto (Brutto)
    Credit: Erlöskonto (Netto or Brutto if Kleinunternehmer)
    Credit: USt-Konto (MwSt, only if tax_mode='regular')

    Returns the Journal Entry name.
    """
    if not is_erpnext_enabled():
        return None
    _validate_settings(settings)

    brutto = quote.total
    if brutto <= 0:
        return None

    posting_date = (quote.performed_at or quote.finalized_at or quote.created_at).strftime('%Y-%m-%d')

    accounts = []

    # Find customer party name in ERPNext
    customer_party = _resolve_customer_party(quote.customer_name)

    if settings.tax_mode == 'regular':
        netto = round(brutto / 1.19, 2)
        mwst = round(brutto - netto, 2)

        # Debit: Forderungen (Brutto)
        debit_entry = {
            'account': settings.erpnext_account_receivable,
            'debit_in_account_currency': brutto,
            'credit_in_account_currency': 0,
        }
        if customer_party:
            debit_entry['party_type'] = 'Customer'
            debit_entry['party'] = customer_party
        accounts.append(debit_entry)

        # Credit: Erlöse (Netto)
        accounts.append({
            'account': settings.erpnext_account_revenue,
            'debit_in_account_currency': 0,
            'credit_in_account_currency': netto,
        })

        # Credit: Umsatzsteuer (MwSt)
        accounts.append({
            'account': settings.erpnext_account_vat,
            'debit_in_account_currency': 0,
            'credit_in_account_currency': mwst,
        })
    else:
        # Kleinunternehmer: no VAT split
        debit_entry = {
            'account': settings.erpnext_account_receivable,
            'debit_in_account_currency': brutto,
            'credit_in_account_currency': 0,
        }
        if customer_party:
            debit_entry['party_type'] = 'Customer'
            debit_entry['party'] = customer_party
        accounts.append(debit_entry)

        accounts.append({
            'account': settings.erpnext_account_revenue,
            'debit_in_account_currency': 0,
            'credit_in_account_currency': brutto,
        })

    remark = f'Rechnung {quote.reference_number} – {quote.customer_name}'
    return create_journal_entry(
        company=settings.erpnext_company,
        posting_date=posting_date,
        accounts=accounts,
        user_remark=remark,
        reference_number=quote.reference_number,
        bill_no=quote.reference_number,
    )


def book_payment(quote, settings):
    """Create Journal Entry for 'Bezahlt': Bank an Forderungen.

    Debit: Bankkonto (Brutto)
    Credit: Forderungskonto (Brutto)

    Returns the Journal Entry name.
    """
    if not is_erpnext_enabled():
        return None
    _validate_settings(settings)

    brutto = quote.total
    if brutto <= 0:
        return None

    posting_date = (quote.paid_at or quote.performed_at or quote.created_at).strftime('%Y-%m-%d')

    customer_party = _resolve_customer_party(quote.customer_name)

    credit_entry = {
        'account': settings.erpnext_account_receivable,
        'debit_in_account_currency': 0,
        'credit_in_account_currency': brutto,
    }
    if customer_party:
        credit_entry['party_type'] = 'Customer'
        credit_entry['party'] = customer_party

    accounts = [
        # Debit: Bank
        {
            'account': settings.erpnext_account_bank,
            'debit_in_account_currency': brutto,
            'credit_in_account_currency': 0,
        },
        # Credit: Forderungen
        credit_entry,
    ]

    remark = f'Zahlung Rechnung {quote.reference_number} – {quote.customer_name}'
    return create_journal_entry(
        company=settings.erpnext_company,
        posting_date=posting_date,
        accounts=accounts,
        user_remark=remark,
        reference_number=quote.reference_number,
        bill_no=quote.reference_number,
    )


def cancel_receivable(quote):
    """Cancel the receivable Journal Entry for a quote."""
    if not is_erpnext_enabled():
        return
    if quote.erpnext_je_receivable:
        cancel_journal_entry(quote.erpnext_je_receivable)


def cancel_payment(quote):
    """Cancel the payment Journal Entry for a quote."""
    if not is_erpnext_enabled():
        return
    if quote.erpnext_je_payment:
        cancel_journal_entry(quote.erpnext_je_payment)


def _validate_settings(settings):
    """Validate that required ERPNext account settings are configured."""
    missing = []
    if not settings.erpnext_company:
        missing.append('Firma')
    if not settings.erpnext_account_receivable:
        missing.append('Forderungskonto')
    if not settings.erpnext_account_revenue:
        missing.append('Erlöskonto')
    if not settings.erpnext_account_bank:
        missing.append('Bankkonto')
    if settings.tax_mode == 'regular' and not settings.erpnext_account_vat:
        missing.append('Umsatzsteuerkonto')
    if missing:
        raise RuntimeError(f'ERPNext-Einstellungen unvollständig. Fehlend: {", ".join(missing)}. '
                           f'Bitte unter Einstellungen konfigurieren.')


def _resolve_customer_party(customer_name):
    """Try to find the customer's ERPNext Customer name (party).
    Returns the ERPNext Customer ID if found, else None.
    """
    try:
        c = get_customer(customer_name)
        return c['erpnext_id'] if c else None
    except Exception:
        return None


def test_connection():
    """Test the ERPNext connection. Returns (success: bool, message: str)."""
    if not is_erpnext_enabled():
        return False, 'ERPNext ist nicht aktiviert (ERPNEXT_ENABLED).'
    try:
        companies = get_companies()
        return True, f'Verbindung erfolgreich. {len(companies)} Firma(en) gefunden.'
    except Exception as e:
        return False, f'Verbindungsfehler: {e}'
