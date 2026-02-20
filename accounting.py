"""
Accounting REST API integration.

Connects to an external accounting service for:
  - Booking income / expense transactions
  - Customer management (CRUD)
  - Quote management (CRUD, status, PDF)
  - Invoice management (CRUD, status, PDF, payment)
  - Account & category management
  - Transfers between accounts
  - Financial summaries

Configuration via environment variables:
  ACCOUNTING_API_URL  – Base URL (e.g. https://accounting.example.com/api/v1)
  ACCOUNTING_API_KEY  – Bearer token for Authorization header
"""

from __future__ import annotations

import os
from typing import Any

import requests

# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------

def _base_url():
    """Return the configured API base URL (without trailing slash)."""
    url = os.getenv('ACCOUNTING_API_URL', '').strip().rstrip('/')
    return url or None


def _api_key():
    return os.getenv('ACCOUNTING_API_KEY', '').strip() or None


def is_configured():
    """Return True when both URL and API key are present."""
    return bool(_base_url() and _api_key())


def _headers():
    return {
        'Authorization': f'Bearer {_api_key()}',
        'Content-Type': 'application/json',
    }


def _request(method, path, **kwargs):
    """Low-level request wrapper.  Returns (ok, data_or_error)."""
    base = _base_url()
    if not base:
        return False, 'ACCOUNTING_API_URL not configured'
    url = f'{base}{path}'
    try:
        resp = requests.request(method, url, headers=_headers(), timeout=15, **kwargs)
        if resp.status_code in (200, 201):
            return True, resp.json()
        else:
            try:
                body = resp.json()
            except Exception:
                body = resp.text
            return False, f'HTTP {resp.status_code}: {body}'
    except requests.RequestException as exc:
        return False, f'Request failed: {exc}'


def _request_raw(method, path, **kwargs):
    """Low-level request returning raw Response object (for binary downloads).

    Returns (True, response) or (False, error_string).
    """
    base = _base_url()
    if not base:
        return False, 'ACCOUNTING_API_URL not configured'
    url = f'{base}{path}'
    try:
        resp = requests.request(method, url, headers=_headers(), timeout=30,
                                stream=True, **kwargs)
        if resp.status_code in (200, 201):
            return True, resp
        else:
            try:
                body = resp.json()
            except Exception:
                body = resp.text
            return False, f'HTTP {resp.status_code}: {body}'
    except requests.RequestException as exc:
        return False, f'Request failed: {exc}'


# ---------------------------------------------------------------------------
# Read-only helpers (settings, categories, tax treatments)
# ---------------------------------------------------------------------------

def get_settings():
    """GET /settings – returns accounting service settings dict or None."""
    ok, data = _request('GET', '/settings')
    return data if ok else None


def get_categories(type_filter=None):
    """GET /categories – returns list of category dicts or empty list."""
    params = {}
    if type_filter:
        params['type'] = type_filter
    ok, data = _request('GET', '/categories', params=params)
    if ok and isinstance(data, dict):
        return data.get('categories', [])
    return []


def get_tax_treatments():
    """GET /tax-treatments – returns list of {value, label} dicts."""
    ok, data = _request('GET', '/tax-treatments')
    if ok and isinstance(data, dict):
        return data.get('tax_treatments', [])
    return []


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

def get_accounts():
    """GET /accounts – returns list of account dicts with current balances."""
    ok, data = _request('GET', '/accounts')
    if ok and isinstance(data, dict):
        return data.get('accounts', [])
    return []


# ---------------------------------------------------------------------------
# Tax treatment mapping
# ---------------------------------------------------------------------------

def get_default_tax_treatment(site_settings):
    """Derive the default accounting tax_treatment from site settings.

    * kleinunternehmer → 'none'
    * regular          → 'standard'
    """
    if not site_settings:
        return 'none'
    mode = (site_settings.tax_mode or 'kleinunternehmer').strip().lower()
    if mode == 'regular':
        return 'standard'
    return 'none'


# ---------------------------------------------------------------------------
# Transaction CRUD
# ---------------------------------------------------------------------------

def create_transaction(*, date, txn_type, description, amount,
                       account_id=None, category_id=None,
                       tax_treatment=None, notes=None):
    """POST /transactions – create a single transaction.

    Parameters
    ----------
    date : str  – ISO 8601 date (YYYY-MM-DD)
    txn_type : str – 'income' or 'expense'
    description : str
    amount : float – gross (brutto) amount, must be > 0
    account_id : int | None – account to book to (required by API)
    category_id : int | None
    tax_treatment : str | None – e.g. 'none', 'standard', …
    notes : str | None

    Returns
    -------
    (True, transaction_id) on success, (False, error_message) on failure.
    """
    payload = {
        'date': date,
        'type': txn_type,
        'description': description,
        'amount': round(amount, 2),
    }
    if account_id is not None:
        payload['account_id'] = int(account_id)
    if category_id is not None:
        payload['category_id'] = int(category_id)
    if tax_treatment:
        payload['tax_treatment'] = tax_treatment
    if notes:
        payload['notes'] = notes

    ok, data = _request('POST', '/transactions', json=payload)
    if ok and isinstance(data, dict):
        txn = data.get('transaction', {})
        return True, txn.get('id')
    return False, data


def update_transaction(transaction_id, **fields):
    """PUT /transactions/:id – update selected fields.

    Accepted keyword arguments: date, amount, description, tax_treatment,
    category_id, notes.

    Returns (True, updated_transaction_dict) or (False, error).
    """
    if not transaction_id:
        return False, 'No transaction_id'
    payload = {}
    for key in ('date', 'amount', 'description', 'tax_treatment',
                'category_id', 'account_id', 'notes', 'type'):
        if key in fields and fields[key] is not None:
            payload[key] = fields[key]
    if not payload:
        return True, {}  # nothing to update
    ok, data = _request('PUT', f'/transactions/{transaction_id}', json=payload)
    if ok and isinstance(data, dict):
        return True, data.get('transaction', data)
    return False, data


def delete_transaction(transaction_id):
    """DELETE /transactions/:id.

    Returns (True, info) or (False, error).
    """
    if not transaction_id:
        return False, 'No transaction_id'
    ok, data = _request('DELETE', f'/transactions/{transaction_id}')
    return ok, data


# ---------------------------------------------------------------------------
# Transaction documents
# ---------------------------------------------------------------------------

def upload_transaction_documents(transaction_id, files):
    """POST /transactions/:id/documents – upload one or more document attachments.

    Parameters
    ----------
    transaction_id : int
    files : list of (filename, file_bytes, content_type) tuples
        e.g. [('rechnung.pdf', b'...', 'application/pdf')]

    Returns
    -------
    (True, response_dict) on success, (False, error_message) on failure.
    """
    if not transaction_id:
        return False, 'No transaction_id'
    if not files:
        return True, {}  # nothing to upload
    base = _base_url()
    if not base:
        return False, 'ACCOUNTING_API_URL not configured'
    url = f'{base}/transactions/{transaction_id}/documents'
    headers = {'Authorization': f'Bearer {_api_key()}'}
    multipart = [('documents', (fn, data, ct)) for fn, data, ct in files]
    try:
        resp = requests.post(
            url, headers=headers, timeout=30,
            files=multipart,
        )
        if resp.status_code in (200, 201):
            return True, resp.json()
        else:
            try:
                body = resp.json()
            except Exception:
                body = resp.text
            return False, f'HTTP {resp.status_code}: {body}'
    except requests.RequestException as exc:
        return False, f'Request failed: {exc}'


# Convenience wrapper for single-file uploads
def upload_transaction_document(transaction_id, file_bytes, filename,
                                content_type='application/pdf'):
    """Upload a single document to a transaction (convenience wrapper)."""
    return upload_transaction_documents(
        transaction_id, [(filename, file_bytes, content_type)])


def get_transaction_documents(transaction_id):
    """GET /transactions/:id/documents – list attached documents.

    Returns list of document dicts or empty list.
    """
    if not transaction_id:
        return []
    ok, data = _request('GET', f'/transactions/{transaction_id}/documents')
    if ok and isinstance(data, dict):
        return data.get('documents', [])
    return []


def download_transaction_document(transaction_id, doc_id):
    """GET /transactions/:id/documents/:doc_id – download a document.

    Returns (True, (content_bytes, content_type, filename)) or (False, error).
    """
    if not transaction_id or not doc_id:
        return False, 'Missing transaction_id or doc_id'
    ok, resp_or_err = _request_raw('GET',
                                   f'/transactions/{transaction_id}/documents/{doc_id}')
    if ok:
        ct = resp_or_err.headers.get('Content-Type', 'application/octet-stream')
        cd = resp_or_err.headers.get('Content-Disposition', '')
        fn = ''
        if 'filename=' in cd:
            fn = cd.split('filename=')[-1].strip('" ')
        return True, (resp_or_err.content, ct, fn)
    return False, resp_or_err


def delete_transaction_document(transaction_id, doc_id):
    """DELETE /transactions/:id/documents/:doc_id.

    Returns (True, data) or (False, error).
    """
    if not transaction_id or not doc_id:
        return False, 'Missing transaction_id or doc_id'
    return _request('DELETE', f'/transactions/{transaction_id}/documents/{doc_id}')


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------

def get_customers(q: str | None = None):
    """GET /customers – list customers, optionally filtered by search query.

    Returns list of customer dicts.
    """
    params = {}
    if q:
        params['q'] = q
    ok, data = _request('GET', '/customers', params=params)
    if ok and isinstance(data, dict):
        return data.get('customers', [])
    return []


def get_customer(customer_id: int):
    """GET /customers/:id – get a single customer.

    Returns (True, customer_dict) or (False, error).
    """
    ok, data = _request('GET', f'/customers/{customer_id}')
    if ok and isinstance(data, dict):
        return True, data.get('customer', data)
    return False, data


def create_customer(*, name: str, company: str | None = None,
                    address: str | None = None, email: str | None = None,
                    phone: str | None = None, notes: str | None = None):
    """POST /customers – create a new customer.

    Returns (True, customer_dict) or (False, error).
    """
    payload: dict[str, Any] = {'name': name}
    if company:
        payload['company'] = company
    if address:
        payload['address'] = address
    if email:
        payload['email'] = email
    if phone:
        payload['phone'] = phone
    if notes:
        payload['notes'] = notes
    ok, data = _request('POST', '/customers', json=payload)
    if ok and isinstance(data, dict):
        return True, data.get('customer', data)
    return False, data


def update_customer(customer_id: int, **fields):
    """PUT /customers/:id – update customer fields.

    Accepted fields: name, company, address, email, phone, notes.
    Returns (True, customer_dict) or (False, error).
    """
    payload = {}
    for key in ('name', 'company', 'address', 'email', 'phone', 'notes'):
        if key in fields:
            payload[key] = fields[key]
    if not payload:
        return True, {}
    ok, data = _request('PUT', f'/customers/{customer_id}', json=payload)
    if ok and isinstance(data, dict):
        return True, data.get('customer', data)
    return False, data


def delete_customer(customer_id: int):
    """DELETE /customers/:id.

    Returns (True, data) or (False, error).
    """
    return _request('DELETE', f'/customers/{customer_id}')


# ---------------------------------------------------------------------------
# Accounts (CRUD)
# ---------------------------------------------------------------------------

def get_account(account_id: int):
    """GET /accounts/:id – get a single account with current balance.

    Returns (True, account_dict) or (False, error).
    """
    ok, data = _request('GET', f'/accounts/{account_id}')
    if ok and isinstance(data, dict):
        return True, data.get('account', data)
    return False, data


def create_account(*, name: str, description: str | None = None,
                   initial_balance: float = 0.0, sort_order: int = 0):
    """POST /accounts – create a new account.

    Returns (True, account_dict) or (False, error).
    """
    payload: dict[str, Any] = {'name': name}
    if description:
        payload['description'] = description
    if initial_balance:
        payload['initial_balance'] = round(initial_balance, 2)
    if sort_order:
        payload['sort_order'] = sort_order
    ok, data = _request('POST', '/accounts', json=payload)
    if ok and isinstance(data, dict):
        return True, data.get('account', data)
    return False, data


def update_account(account_id: int, **fields):
    """PUT /accounts/:id – update account fields.

    Returns (True, account_dict) or (False, error).
    """
    payload = {}
    for key in ('name', 'description', 'initial_balance', 'sort_order'):
        if key in fields:
            payload[key] = fields[key]
    if not payload:
        return True, {}
    ok, data = _request('PUT', f'/accounts/{account_id}', json=payload)
    if ok and isinstance(data, dict):
        return True, data.get('account', data)
    return False, data


def delete_account(account_id: int):
    """DELETE /accounts/:id.

    Returns (True, data) or (False, error).  409 if transactions exist.
    """
    return _request('DELETE', f'/accounts/{account_id}')


# ---------------------------------------------------------------------------
# Transfers
# ---------------------------------------------------------------------------

def create_transfer(*, date: str, amount: float, from_account_id: int,
                    to_account_id: int, description: str | None = None,
                    notes: str | None = None):
    """POST /transfers – move money between accounts.

    Returns (True, transaction_dict) or (False, error).
    """
    payload: dict[str, Any] = {
        'date': date,
        'amount': round(amount, 2),
        'from_account_id': from_account_id,
        'to_account_id': to_account_id,
    }
    if description:
        payload['description'] = description
    if notes:
        payload['notes'] = notes
    ok, data = _request('POST', '/transfers', json=payload)
    if ok and isinstance(data, dict):
        return True, data.get('transaction', data)
    return False, data


# ---------------------------------------------------------------------------
# Categories (CRUD)
# ---------------------------------------------------------------------------

def get_category(category_id: int):
    """GET /categories/:id – get a single category.

    Returns (True, category_dict) or (False, error).
    """
    ok, data = _request('GET', f'/categories/{category_id}')
    if ok and isinstance(data, dict):
        return True, data.get('category', data)
    return False, data


def create_category(*, name: str, type: str,
                    description: str | None = None, sort_order: int = 0):
    """POST /categories – create a new category.

    Returns (True, category_dict) or (False, error).
    """
    payload: dict[str, Any] = {'name': name, 'type': type}
    if description:
        payload['description'] = description
    if sort_order:
        payload['sort_order'] = sort_order
    ok, data = _request('POST', '/categories', json=payload)
    if ok and isinstance(data, dict):
        return True, data.get('category', data)
    return False, data


def update_category(category_id: int, **fields):
    """PUT /categories/:id – update category fields.

    Returns (True, category_dict) or (False, error).
    """
    payload = {}
    for key in ('name', 'type', 'description', 'sort_order'):
        if key in fields:
            payload[key] = fields[key]
    if not payload:
        return True, {}
    ok, data = _request('PUT', f'/categories/{category_id}', json=payload)
    if ok and isinstance(data, dict):
        return True, data.get('category', data)
    return False, data


def delete_category(category_id: int):
    """DELETE /categories/:id.

    Returns (True, data) or (False, error).
    """
    return _request('DELETE', f'/categories/{category_id}')


# ---------------------------------------------------------------------------
# Transactions – bulk & get
# ---------------------------------------------------------------------------

def get_transactions(*, year: int | None = None, month: int | None = None,
                     type: str | None = None, category_id: int | None = None,
                     account_id: int | None = None, search: str | None = None,
                     sort: str = 'date_desc', limit: int = 100,
                     offset: int = 0):
    """GET /transactions – list transactions with filters & pagination.

    Returns (list_of_transactions, total_count) tuple.
    """
    params: dict[str, Any] = {}
    if year is not None:
        params['year'] = year
    if month is not None:
        params['month'] = month
    if type:
        params['type'] = type
    if category_id is not None:
        params['category_id'] = category_id
    if account_id is not None:
        params['account_id'] = account_id
    if search:
        params['search'] = search
    params['sort'] = sort
    params['limit'] = min(limit, 1000)
    params['offset'] = offset

    ok, data = _request('GET', '/transactions', params=params)
    if ok and isinstance(data, dict):
        return data.get('transactions', []), data.get('total', 0)
    return [], 0


def get_transaction(transaction_id: int):
    """GET /transactions/:id – get a single transaction.

    Returns (True, transaction_dict) or (False, error).
    """
    ok, data = _request('GET', f'/transactions/{transaction_id}')
    if ok and isinstance(data, dict):
        return True, data.get('transaction', data)
    return False, data


def create_transactions_bulk(transactions: list[dict]):
    """POST /transactions/bulk – create up to 500 transactions at once.

    Each item follows the same schema as create_transaction.
    Returns (True, {created: [...], errors: [...], count: N}) or (False, error).
    """
    if not transactions:
        return True, {'created': [], 'errors': [], 'count': 0}
    ok, data = _request('POST', '/transactions/bulk',
                        json={'transactions': transactions})
    return ok, data


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def get_summary(year: int | None = None):
    """GET /summary – financial summary for a given year.

    Returns (True, summary_dict) or (False, error).
    """
    params = {}
    if year is not None:
        params['year'] = year
    ok, data = _request('GET', '/summary', params=params)
    return (True, data) if ok else (False, data)


# ---------------------------------------------------------------------------
# Quotes (Angebote)
# ---------------------------------------------------------------------------

def get_quotes(*, status: str | None = None, year: int | None = None,
               customer_id: int | None = None, limit: int = 100,
               offset: int = 0):
    """GET /quotes – list quotes with optional filters.

    Returns (list_of_quotes, total_count) tuple.
    """
    params: dict[str, Any] = {'limit': min(limit, 1000), 'offset': offset}
    if status:
        params['status'] = status
    if year is not None:
        params['year'] = year
    if customer_id is not None:
        params['customer_id'] = customer_id
    ok, data = _request('GET', '/quotes', params=params)
    if ok and isinstance(data, dict):
        return data.get('quotes', []), data.get('total', 0)
    return [], 0


def get_quote(quote_id: int):
    """GET /quotes/:id – get a single quote with items.

    Returns (True, quote_dict) or (False, error).
    """
    ok, data = _request('GET', f'/quotes/{quote_id}')
    if ok and isinstance(data, dict):
        return True, data.get('quote', data)
    return False, data


def create_quote(*, date: str, items: list[dict],
                 customer_id: int | None = None,
                 valid_until: str | None = None,
                 tax_treatment: str | None = None,
                 custom_tax_rate: float | None = None,
                 discount_percent: float = 0,
                 notes: str | None = None,
                 agb_text: str | None = None,
                 payment_terms_days: int = 14,
                 linked_asset_id: int | None = None):
    """POST /quotes – create a new quote.

    Returns (True, quote_dict) or (False, error).
    """
    payload: dict[str, Any] = {
        'date': date,
        'items': items,
        'payment_terms_days': payment_terms_days,
    }
    if customer_id is not None:
        payload['customer_id'] = customer_id
    if valid_until:
        payload['valid_until'] = valid_until
    if tax_treatment:
        payload['tax_treatment'] = tax_treatment
    if custom_tax_rate is not None:
        payload['custom_tax_rate'] = custom_tax_rate
    if discount_percent:
        payload['discount_percent'] = discount_percent
    if notes:
        payload['notes'] = notes
    if agb_text:
        payload['agb_text'] = agb_text
    if linked_asset_id is not None:
        payload['linked_asset_id'] = linked_asset_id
    ok, data = _request('POST', '/quotes', json=payload)
    if ok and isinstance(data, dict):
        return True, data.get('quote', data)
    return False, data


def update_quote(quote_id: int, **fields):
    """PUT /quotes/:id – update quote fields.

    If 'items' is provided, existing items are replaced entirely.
    Returns (True, quote_dict) or (False, error).
    """
    allowed = ('date', 'customer_id', 'valid_until', 'tax_treatment',
               'custom_tax_rate', 'discount_percent', 'notes', 'agb_text',
               'payment_terms_days', 'linked_asset_id', 'items')
    payload = {k: v for k, v in fields.items() if k in allowed}
    if not payload:
        return True, {}
    ok, data = _request('PUT', f'/quotes/{quote_id}', json=payload)
    if ok and isinstance(data, dict):
        return True, data.get('quote', data)
    return False, data


def delete_quote(quote_id: int):
    """DELETE /quotes/:id.

    Returns (True, data) or (False, error).  409 if invoices reference it.
    """
    return _request('DELETE', f'/quotes/{quote_id}')


def set_quote_status(quote_id: int, status: str):
    """POST /quotes/:id/status – change quote status.

    Valid statuses: draft, sent, accepted, rejected.
    ('invoiced' is set automatically when creating an invoice from the quote.)
    Returns (True, quote_dict) or (False, error).
    """
    ok, data = _request('POST', f'/quotes/{quote_id}/status',
                        json={'status': status})
    if ok and isinstance(data, dict):
        return True, data.get('quote', data)
    return False, data


def generate_quote_pdf(quote_id: int):
    """POST /quotes/:id/generate-pdf – generate or regenerate PDF.

    Returns (True, response_dict) or (False, error).
    """
    return _request('POST', f'/quotes/{quote_id}/generate-pdf')


def download_quote_pdf(quote_id: int):
    """GET /quotes/:id/pdf – download the quote PDF.

    Returns (True, (pdf_bytes, content_type, filename)) or (False, error).
    """
    ok, resp_or_err = _request_raw('GET', f'/quotes/{quote_id}/pdf')
    if ok:
        ct = resp_or_err.headers.get('Content-Type', 'application/pdf')
        cd = resp_or_err.headers.get('Content-Disposition', '')
        fn = f'Angebot_{quote_id}.pdf'
        if 'filename=' in cd:
            fn = cd.split('filename=')[-1].strip('" ')
        return True, (resp_or_err.content, ct, fn)
    return False, resp_or_err


def create_invoice_from_quote(quote_id: int, date: str | None = None):
    """POST /quotes/:id/create-invoice – create an invoice from a quote.

    Copies all items. Marks quote as 'invoiced'. Auto-generates invoice PDF.
    Returns (True, invoice_dict) or (False, error).
    """
    payload: dict[str, Any] = {}
    if date:
        payload['date'] = date
    ok, data = _request('POST', f'/quotes/{quote_id}/create-invoice',
                        json=payload)
    if ok and isinstance(data, dict):
        return True, data.get('invoice', data)
    return False, data


# ---------------------------------------------------------------------------
# Invoices (Rechnungen)
# ---------------------------------------------------------------------------

def get_invoices(*, status: str | None = None, year: int | None = None,
                 customer_id: int | None = None, limit: int = 100,
                 offset: int = 0):
    """GET /invoices – list invoices with optional filters.

    Returns (list_of_invoices, total_count, total_amount, paid_amount, open_amount).
    """
    params: dict[str, Any] = {'limit': min(limit, 1000), 'offset': offset}
    if status:
        params['status'] = status
    if year is not None:
        params['year'] = year
    if customer_id is not None:
        params['customer_id'] = customer_id
    ok, data = _request('GET', '/invoices', params=params)
    if ok and isinstance(data, dict):
        return (data.get('invoices', []),
                data.get('total', 0),
                data.get('total_amount', 0),
                data.get('paid_amount', 0),
                data.get('open_amount', 0))
    return [], 0, 0, 0, 0


def get_invoice(invoice_id: int):
    """GET /invoices/:id – get a single invoice with items.

    Returns (True, invoice_dict) or (False, error).
    """
    ok, data = _request('GET', f'/invoices/{invoice_id}')
    if ok and isinstance(data, dict):
        return True, data.get('invoice', data)
    return False, data


def create_invoice(*, date: str, customer_id: int, items: list[dict],
                   tax_treatment: str | None = None,
                   custom_tax_rate: float | None = None,
                   discount_percent: float = 0,
                   notes: str | None = None,
                   payment_terms_days: int = 14,
                   linked_asset_id: int | None = None):
    """POST /invoices – create a new invoice directly (without a quote).

    Returns (True, invoice_dict) or (False, error).
    """
    payload: dict[str, Any] = {
        'date': date,
        'customer_id': customer_id,
        'items': items,
        'payment_terms_days': payment_terms_days,
    }
    if tax_treatment:
        payload['tax_treatment'] = tax_treatment
    if custom_tax_rate is not None:
        payload['custom_tax_rate'] = custom_tax_rate
    if discount_percent:
        payload['discount_percent'] = discount_percent
    if notes:
        payload['notes'] = notes
    if linked_asset_id is not None:
        payload['linked_asset_id'] = linked_asset_id
    ok, data = _request('POST', '/invoices', json=payload)
    if ok and isinstance(data, dict):
        return True, data.get('invoice', data)
    return False, data


def update_invoice(invoice_id: int, **fields):
    """PUT /invoices/:id – update invoice fields.

    If 'items' is provided, existing items are replaced entirely.
    Returns (True, invoice_dict) or (False, error).
    """
    allowed = ('date', 'customer_id', 'tax_treatment', 'custom_tax_rate',
               'discount_percent', 'notes', 'payment_terms_days',
               'linked_asset_id', 'items')
    payload = {k: v for k, v in fields.items() if k in allowed}
    if not payload:
        return True, {}
    ok, data = _request('PUT', f'/invoices/{invoice_id}', json=payload)
    if ok and isinstance(data, dict):
        return True, data.get('invoice', data)
    return False, data


def delete_invoice(invoice_id: int):
    """DELETE /invoices/:id.

    Returns (True, data) or (False, error).  409 if payment is linked.
    """
    return _request('DELETE', f'/invoices/{invoice_id}')


def set_invoice_status(invoice_id: int, status: str):
    """POST /invoices/:id/status – change invoice status.

    Valid statuses: draft, sent, cancelled.
    (For 'paid', use mark_invoice_paid instead.)
    Returns (True, invoice_dict) or (False, error).
    """
    ok, data = _request('POST', f'/invoices/{invoice_id}/status',
                        json={'status': status})
    if ok and isinstance(data, dict):
        return True, data.get('invoice', data)
    return False, data


def generate_invoice_pdf(invoice_id: int):
    """POST /invoices/:id/generate-pdf – generate or regenerate PDF.

    Returns (True, response_dict) or (False, error).
    """
    return _request('POST', f'/invoices/{invoice_id}/generate-pdf')


def download_invoice_pdf(invoice_id: int):
    """GET /invoices/:id/pdf – download the invoice PDF.

    Returns (True, (pdf_bytes, content_type, filename)) or (False, error).
    """
    ok, resp_or_err = _request_raw('GET', f'/invoices/{invoice_id}/pdf')
    if ok:
        ct = resp_or_err.headers.get('Content-Type', 'application/pdf')
        cd = resp_or_err.headers.get('Content-Disposition', '')
        fn = f'Rechnung_{invoice_id}.pdf'
        if 'filename=' in cd:
            fn = cd.split('filename=')[-1].strip('" ')
        return True, (resp_or_err.content, ct, fn)
    return False, resp_or_err


def mark_invoice_paid(invoice_id: int, *, account_id: int,
                      category_id: int | None = None,
                      payment_date: str | None = None):
    """POST /invoices/:id/mark-paid – mark as paid and create accounting txn.

    Returns (True, {invoice: ..., transaction: ...}) or (False, error).
    """
    payload: dict[str, Any] = {'account_id': account_id}
    if category_id is not None:
        payload['category_id'] = category_id
    if payment_date:
        payload['payment_date'] = payment_date
    ok, data = _request('POST', f'/invoices/{invoice_id}/mark-paid',
                        json=payload)
    return ok, data


def unmark_invoice_paid(invoice_id: int):
    """POST /invoices/:id/unmark-paid – reverse payment, delete transaction.

    Returns (True, {invoice: ...}) or (False, error).
    """
    ok, data = _request('POST', f'/invoices/{invoice_id}/unmark-paid')
    return ok, data
