"""
Accounting REST API integration.

Connects to an external accounting service to book income/expense transactions
when quotes are marked as paid or external expenses are settled.

Configuration via environment variables:
  ACCOUNTING_API_URL  – Base URL (e.g. https://accounting.example.com/api/v1)
  ACCOUNTING_API_KEY  – Bearer token for Authorization header
"""

import os
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
