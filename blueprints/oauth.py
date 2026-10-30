"""OAuth 2.1 authorization server for the MCP endpoint (/mcp).

Implements what MCP clients such as claude.ai custom connectors need:
- Protected resource metadata (RFC 9728) and authorization server metadata (RFC 8414)
- Dynamic client registration (RFC 7591) and Client ID Metadata Documents
- Authorization code grant with mandatory PKCE (S256) and rotating refresh tokens
- Token revocation (RFC 7009)

Users authorize with their normal staff login; the issued token acts as that user.
Everything is disabled unless "MCP aktivieren" is checked in the admin settings.
"""
import base64
import hashlib
import hmac
import ipaddress
import json
import secrets
import socket
from datetime import datetime, timedelta
from urllib.parse import urlencode, urlparse, urlsplit, urlunsplit, parse_qsl, unquote

import requests as http_requests
from flask import Blueprint, jsonify, request, redirect, render_template, session, url_for
from flask_login import current_user

from helpers import public_base_url, throttle_count, throttle_record
from models import db, User, SiteSettings, OAuthClient, OAuthAuthorizationCode, OAuthToken

oauth_bp = Blueprint('oauth', __name__)

SCOPE = 'mcp'
CODE_TTL = timedelta(minutes=10)
ACCESS_TOKEN_TTL = timedelta(hours=1)
REFRESH_TOKEN_TTL = timedelta(days=90)
CLIENT_METADATA_TTL = timedelta(hours=24)
# Abuse limits per client IP
REGISTER_WINDOW = timedelta(hours=1)
MAX_REGISTRATIONS_PER_IP = 10
TOKEN_FAIL_WINDOW = timedelta(minutes=15)
MAX_TOKEN_FAILS_PER_IP = 20


# ── Helpers ───────────────────────────────────────────────────────────

def mcp_enabled():
    settings = SiteSettings.query.first()
    return bool(settings and settings.mcp_enabled)


def _hash(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def mcp_resource_url():
    return public_base_url() + '/mcp'


def protected_resource_metadata_url():
    return public_base_url() + '/.well-known/oauth-protected-resource/mcp'


def _oauth_error(error, description, code=400):
    resp = jsonify({'error': error, 'error_description': description})
    resp.status_code = code
    resp.headers['Cache-Control'] = 'no-store'
    return resp


def _disabled():
    return _oauth_error('access_denied', 'MCP ist in den Einstellungen deaktiviert.', 404)


@oauth_bp.after_request
def _cors(resp):
    """Metadata, registration and token endpoints are called cross-origin by
    browser-based MCP clients. They never rely on cookies, so * is safe here."""
    if request.endpoint and request.endpoint != 'oauth.authorize':
        resp.headers['Access-Control-Allow-Origin'] = '*'
        resp.headers['Access-Control-Allow-Headers'] = 'Authorization, Content-Type, MCP-Protocol-Version'
        resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    return resp


def _valid_redirect_uri(uri):
    """https anywhere, http only for loopback (native/CLI clients)."""
    try:
        parts = urlparse(uri)
    except ValueError:
        return False
    if parts.fragment or not parts.netloc:
        return False
    if parts.scheme == 'https':
        return True
    return parts.scheme == 'http' and parts.hostname in ('localhost', '127.0.0.1', '::1')


def _is_public_host(hostname):
    """Reject hosts resolving to private/loopback addresses (SSRF guard)."""
    try:
        infos = socket.getaddrinfo(hostname, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return True


def _fetch_client_metadata(client_id):
    """Fetch and validate a Client ID Metadata Document. Returns dict or None."""
    parts = urlparse(client_id)
    if parts.scheme != 'https' or not parts.hostname or parts.path in ('', '/'):
        return None
    if not _is_public_host(parts.hostname):
        return None
    try:
        resp = http_requests.get(client_id, timeout=5, allow_redirects=False, stream=True,
                                 headers={'Accept': 'application/json'})
        if resp.status_code != 200:
            return None
        raw = resp.raw.read(64 * 1024 + 1, decode_content=True)
        if len(raw) > 64 * 1024:
            return None
        doc = json.loads(raw)
    except (http_requests.RequestException, ValueError):
        return None
    if not isinstance(doc, dict) or doc.get('client_id') != client_id:
        return None
    uris = doc.get('redirect_uris')
    if not isinstance(uris, list) or not uris or not all(isinstance(u, str) and _valid_redirect_uri(u) for u in uris):
        return None
    return doc


def resolve_client(client_id):
    """Look up a registered client; URL client ids are fetched (and cached)."""
    if not client_id:
        return None
    client = OAuthClient.query.filter_by(client_id=client_id).first()
    is_url = client_id.startswith('https://')
    if client and not (is_url and datetime.utcnow() - client.created_at > CLIENT_METADATA_TTL):
        return client
    if not is_url:
        return None
    doc = _fetch_client_metadata(client_id)
    if not doc:
        return client  # keep using a stale cached copy if the fetch fails
    if not client:
        client = OAuthClient(client_id=client_id)
        db.session.add(client)
    client.client_name = str(doc.get('client_name') or '')[:200] or None
    client.redirect_uris = json.dumps(doc['redirect_uris'])
    client.created_at = datetime.utcnow()
    db.session.commit()
    return client


def _authenticate_client():
    """Client authentication at the token/revocation endpoints.
    Returns (client, error_response)."""
    client_id = request.form.get('client_id')
    client_secret = request.form.get('client_secret')
    auth = request.headers.get('Authorization', '')
    if auth.startswith('Basic '):
        try:
            decoded = base64.b64decode(auth[6:]).decode('utf-8')
            basic_id, _, basic_secret = decoded.partition(':')
            client_id, client_secret = unquote(basic_id), unquote(basic_secret)
        except (ValueError, UnicodeDecodeError):
            return None, _oauth_error('invalid_client', 'Ungültige Client-Authentifizierung.', 401)
    client = resolve_client(client_id)
    if not client:
        return None, _oauth_error('invalid_client', 'Unbekannter Client.', 401)
    if client.client_secret_hash:
        if not client_secret or not hmac.compare_digest(_hash(client_secret), client.client_secret_hash):
            return None, _oauth_error('invalid_client', 'Client-Secret ungültig.', 401)
    return client, None


def _issue_token(client_id, user_id, scope):
    access = 'erp_at_' + secrets.token_urlsafe(32)
    refresh = 'erp_rt_' + secrets.token_urlsafe(32)
    now = datetime.utcnow()
    db.session.add(OAuthToken(
        client_id=client_id, user_id=user_id, scope=scope,
        access_token_hash=_hash(access), refresh_token_hash=_hash(refresh),
        access_expires_at=now + ACCESS_TOKEN_TTL,
        refresh_expires_at=now + REFRESH_TOKEN_TTL,
    ))
    # Housekeeping: drop expired codes and dead tokens
    OAuthAuthorizationCode.query.filter(OAuthAuthorizationCode.expires_at < now).delete()
    OAuthToken.query.filter(OAuthToken.refresh_expires_at < now - timedelta(days=1)).delete()
    db.session.commit()
    resp = jsonify({
        'access_token': access,
        'token_type': 'Bearer',
        'expires_in': int(ACCESS_TOKEN_TTL.total_seconds()),
        'refresh_token': refresh,
        'scope': scope,
    })
    resp.headers['Cache-Control'] = 'no-store'
    return resp


def validate_access_token(raw_token):
    """Return the active OAuthToken for a bearer token, or None."""
    if not raw_token or not raw_token.startswith('erp_at_'):
        return None
    tok = OAuthToken.query.filter_by(access_token_hash=_hash(raw_token)).first()
    now = datetime.utcnow()
    if not tok or tok.revoked or tok.access_expires_at < now:
        return None
    if not tok.user or not tok.user.active:
        return None
    if not tok.last_used_at or now - tok.last_used_at > timedelta(minutes=5):
        tok.last_used_at = now
        db.session.commit()
    return tok


def _client_ip():
    return request.remote_addr or 'unknown'


def _too_many(description):
    resp = _oauth_error('slow_down', description, 429)
    resp.headers['Retry-After'] = '900'
    return resp


def _with_query(uri, params):
    parts = urlsplit(uri)
    query = parse_qsl(parts.query, keep_blank_values=True) + list(params.items())
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ''))


# ── Metadata ──────────────────────────────────────────────────────────

@oauth_bp.route('/.well-known/oauth-protected-resource')
@oauth_bp.route('/.well-known/oauth-protected-resource/mcp')
def protected_resource_metadata():
    if not mcp_enabled():
        return _disabled()
    return jsonify({
        'resource': mcp_resource_url(),
        'authorization_servers': [public_base_url()],
        'scopes_supported': [SCOPE],
        'bearer_methods_supported': ['header'],
        'resource_name': 'ERP-Rent MCP',
    })


@oauth_bp.route('/.well-known/oauth-authorization-server')
@oauth_bp.route('/.well-known/oauth-authorization-server/mcp')
@oauth_bp.route('/.well-known/openid-configuration')
def authorization_server_metadata():
    if not mcp_enabled():
        return _disabled()
    base = public_base_url()
    return jsonify({
        'issuer': base,
        'authorization_endpoint': base + url_for('oauth.authorize'),
        'token_endpoint': base + url_for('oauth.token'),
        'registration_endpoint': base + url_for('oauth.register'),
        'revocation_endpoint': base + url_for('oauth.revoke'),
        'scopes_supported': [SCOPE],
        'response_types_supported': ['code'],
        'response_modes_supported': ['query'],
        'grant_types_supported': ['authorization_code', 'refresh_token'],
        'code_challenge_methods_supported': ['S256'],
        'token_endpoint_auth_methods_supported': ['none', 'client_secret_post', 'client_secret_basic'],
        'revocation_endpoint_auth_methods_supported': ['none', 'client_secret_post', 'client_secret_basic'],
        'client_id_metadata_document_supported': True,
        'authorization_response_iss_parameter_supported': True,
    })


# ── Dynamic client registration ───────────────────────────────────────

@oauth_bp.route('/oauth/register', methods=['POST'])
def register():
    if not mcp_enabled():
        return _disabled()
    if throttle_count('oauth_register', REGISTER_WINDOW, ip=_client_ip()) >= MAX_REGISTRATIONS_PER_IP:
        return _too_many('Zu viele Client-Registrierungen. Bitte später erneut versuchen.')
    data = request.get_json(silent=True) or {}
    uris = data.get('redirect_uris')
    if not isinstance(uris, list) or not uris or len(uris) > 10:
        return _oauth_error('invalid_redirect_uri', 'redirect_uris (1–10 URIs) ist erforderlich.')
    if not all(isinstance(u, str) and len(u) <= 500 and _valid_redirect_uri(u) for u in uris):
        return _oauth_error('invalid_redirect_uri', 'Redirect-URIs müssen https (oder http://localhost) sein.')
    auth_method = data.get('token_endpoint_auth_method') or 'client_secret_basic'
    if auth_method not in ('none', 'client_secret_post', 'client_secret_basic'):
        return _oauth_error('invalid_client_metadata', f'token_endpoint_auth_method {auth_method} wird nicht unterstützt.')

    client_id = 'erp_' + secrets.token_urlsafe(16)
    client_secret = None if auth_method == 'none' else secrets.token_urlsafe(32)
    client = OAuthClient(
        client_id=client_id,
        client_secret_hash=_hash(client_secret) if client_secret else None,
        client_name=str(data.get('client_name') or '')[:200] or None,
        redirect_uris=json.dumps(uris),
    )
    db.session.add(client)
    db.session.commit()
    throttle_record('oauth_register', _client_ip())

    body = {
        'client_id': client_id,
        'client_id_issued_at': int(client.created_at.timestamp()),
        'client_name': client.client_name,
        'redirect_uris': uris,
        'token_endpoint_auth_method': auth_method,
        'grant_types': ['authorization_code', 'refresh_token'],
        'response_types': ['code'],
        'scope': SCOPE,
    }
    if client_secret:
        body['client_secret'] = client_secret
        body['client_secret_expires_at'] = 0
    resp = jsonify(body)
    resp.status_code = 201
    resp.headers['Cache-Control'] = 'no-store'
    return resp


# ── Authorization endpoint (consent screen) ───────────────────────────

_AUTHZ_PARAMS = ('response_type', 'client_id', 'redirect_uri', 'state', 'scope',
                 'code_challenge', 'code_challenge_method', 'resource')


@oauth_bp.route('/oauth/authorize', methods=['GET', 'POST'])
def authorize():
    params = {k: (request.values.get(k) or '') for k in _AUTHZ_PARAMS}

    def _page_error(message):
        return render_template('auth/oauth_authorize.html', error=message), 400

    if not mcp_enabled():
        return _page_error('Die Claude-/MCP-Schnittstelle ist in den Einstellungen deaktiviert.')

    # Client and redirect URI must be valid before anything is sent back to it.
    client = resolve_client(params['client_id'])
    if not client:
        return _page_error('Unbekannter Client.')
    redirect_uri = params['redirect_uri']
    registered = client.redirect_uri_list
    if not redirect_uri and len(registered) == 1:
        redirect_uri = registered[0]
    if redirect_uri not in registered:
        return _page_error('Die Redirect-URI ist für diesen Client nicht registriert.')

    def _redirect_error(error, description):
        return redirect(_with_query(redirect_uri, {
            'error': error, 'error_description': description,
            'state': params['state'], 'iss': public_base_url(),
        }))

    if params['response_type'] != 'code':
        return _redirect_error('unsupported_response_type', 'Nur response_type=code wird unterstützt.')
    if not params['code_challenge'] or params['code_challenge_method'] != 'S256':
        return _redirect_error('invalid_request', 'PKCE mit code_challenge_method=S256 ist erforderlich.')

    if not current_user.is_authenticated:
        return redirect(url_for('auth.login', next=url_for('oauth.authorize', **params)))

    if request.method == 'POST':
        expected = session.pop('oauth_csrf', None)
        if not expected or not hmac.compare_digest(expected, request.form.get('consent_token', '')):
            return _page_error('Sitzung abgelaufen. Bitte die Verbindung in Claude erneut starten.')
        if request.form.get('decision') != 'allow':
            return _redirect_error('access_denied', 'Zugriff wurde abgelehnt.')
        code = secrets.token_urlsafe(32)
        db.session.add(OAuthAuthorizationCode(
            code_hash=_hash(code),
            client_id=client.client_id,
            user_id=current_user.id,
            redirect_uri=redirect_uri,
            code_challenge=params['code_challenge'],
            scope=SCOPE,
            resource=params['resource'] or None,
            expires_at=datetime.utcnow() + CODE_TTL,
        ))
        db.session.commit()
        return redirect(_with_query(redirect_uri, {
            'code': code, 'state': params['state'], 'iss': public_base_url(),
        }))

    session['oauth_csrf'] = secrets.token_urlsafe(24)
    settings = SiteSettings.query.first()
    return render_template('auth/oauth_authorize.html',
                           client=client,
                           redirect_host=urlparse(redirect_uri).netloc,
                           params=params,
                           consent_token=session['oauth_csrf'],
                           allow_delete=bool(settings and settings.mcp_allow_delete))


# ── Token endpoint ────────────────────────────────────────────────────

@oauth_bp.route('/oauth/token', methods=['POST'])
def token():
    if not mcp_enabled():
        return _disabled()
    ip = _client_ip()
    if throttle_count('oauth_token', TOKEN_FAIL_WINDOW, ip=ip) >= MAX_TOKEN_FAILS_PER_IP:
        return _too_many('Zu viele fehlgeschlagene Token-Anfragen. Bitte in 15 Minuten erneut versuchen.')
    resp = _token_grant()
    if resp.status_code in (400, 401):
        throttle_record('oauth_token', ip, request.form.get('client_id') or '')
    return resp


def _token_grant():
    client, err = _authenticate_client()
    if err:
        return err
    grant_type = request.form.get('grant_type')

    if grant_type == 'authorization_code':
        code = OAuthAuthorizationCode.query.filter_by(
            code_hash=_hash(request.form.get('code') or '')).first()
        if not code:
            return _oauth_error('invalid_grant', 'Autorisierungscode ungültig.')
        # Codes are single use, also on failure
        db.session.delete(code)
        db.session.commit()
        if code.expires_at < datetime.utcnow() or code.client_id != client.client_id:
            return _oauth_error('invalid_grant', 'Autorisierungscode abgelaufen oder für anderen Client.')
        if (request.form.get('redirect_uri') or code.redirect_uri) != code.redirect_uri:
            return _oauth_error('invalid_grant', 'redirect_uri stimmt nicht überein.')
        verifier = request.form.get('code_verifier') or ''
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode('ascii', 'ignore')).digest()).rstrip(b'=').decode()
        if not verifier or not hmac.compare_digest(challenge, code.code_challenge):
            return _oauth_error('invalid_grant', 'PKCE-Prüfung fehlgeschlagen.')
        user = db.session.get(User, code.user_id)
        if not user or not user.active:
            return _oauth_error('invalid_grant', 'Benutzer ist deaktiviert.')
        return _issue_token(client.client_id, user.id, code.scope or SCOPE)

    if grant_type == 'refresh_token':
        old = OAuthToken.query.filter_by(
            refresh_token_hash=_hash(request.form.get('refresh_token') or '')).first()
        if not old or old.revoked or old.client_id != client.client_id:
            return _oauth_error('invalid_grant', 'Refresh-Token ungültig.')
        if old.refresh_expires_at and old.refresh_expires_at < datetime.utcnow():
            return _oauth_error('invalid_grant', 'Refresh-Token abgelaufen.')
        if not old.user or not old.user.active:
            return _oauth_error('invalid_grant', 'Benutzer ist deaktiviert.')
        old.revoked = True
        return _issue_token(client.client_id, old.user_id, old.scope or SCOPE)

    return _oauth_error('unsupported_grant_type', f'grant_type {grant_type!r} wird nicht unterstützt.')


@oauth_bp.route('/oauth/revoke', methods=['POST'])
def revoke():
    client, err = _authenticate_client()
    if err:
        return err
    raw = request.form.get('token') or ''
    tok = (OAuthToken.query.filter_by(access_token_hash=_hash(raw)).first()
           or OAuthToken.query.filter_by(refresh_token_hash=_hash(raw)).first())
    if tok and tok.client_id == client.client_id:
        tok.revoked = True
        db.session.commit()
    return '', 200
