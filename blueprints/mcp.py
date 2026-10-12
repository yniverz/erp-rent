"""MCP server endpoint (Streamable HTTP transport, stateless, JSON responses).

Mounted at /mcp. Authentication: OAuth access token (see blueprints/oauth.py)
or one of the static API_TOKENS (handy for Claude Code). Tools live in
blueprints/mcp_tools.py.
"""
import hmac
import json

from flask import Blueprint, Response, g, jsonify, request

from blueprints.api import ApiError, _configured_tokens
from blueprints.mcp_tools import TOOLS, ToolError, delete_allowed
from blueprints.oauth import mcp_enabled, protected_resource_metadata_url, validate_access_token
from models import db, SiteSettings

mcp_bp = Blueprint('mcp', __name__)

SUPPORTED_PROTOCOL_VERSIONS = ('2025-11-25', '2025-06-18', '2025-03-26', '2024-11-05')
SERVER_VERSION = '1.0.0'

INSTRUCTIONS = """ERP-Rent: Verleih-Verwaltung für Veranstaltungstechnik (Inventar, Einheiten mit
Seriennummern, Kategorien, Lieferanten, Angebote, Kundenanfragen). Alle Inhalte sind auf Deutsch.

Hinweise:
- Artikelbeschreibungen erscheinen im öffentlichen Shop (Klartext, Zeilenumbrüche erlaubt, kein
  Markdown/HTML; die ersten ~120 Zeichen werden in der Katalogübersicht angezeigt).
- Preise sind Mietpreise in EUR pro Einheit und Tag (brutto, außer prices_are_net ist gesetzt).
- Bestand -1 bedeutet unbegrenzt. Daten im Format YYYY-MM-DD.
- Lieferumfang (Zubehör wie Superclamps, Cases, Kabel) über set_item_accessories pflegen, nicht in
  die Beschreibung schreiben. replacement_value ist der Gerätewert ohne Zubehör; der Gesamtwert
  (replacement_value_total) wird aus Gerätewert + Zubehör berechnet.
- Vor Massenänderungen erst mit list_items/get_item den aktuellen Stand lesen und nur Felder
  senden, die sich ändern sollen. Technische Daten nur eintragen, wenn sie sicher bekannt sind.
- Änderungen wirken sofort im Live-System."""


def _rpc_result(msg_id, result):
    return {'jsonrpc': '2.0', 'id': msg_id, 'result': result}


def _rpc_error(msg_id, code, message):
    return {'jsonrpc': '2.0', 'id': msg_id, 'error': {'code': code, 'message': message}}


def _unauthorized(description):
    resp = jsonify({'error': 'invalid_token', 'error_description': description})
    resp.status_code = 401
    resp.headers['WWW-Authenticate'] = (
        f'Bearer resource_metadata="{protected_resource_metadata_url()}", '
        f'error="invalid_token", error_description="{description}"'
    )
    return resp


def _authenticate():
    """Set g.mcp_user (None for static API tokens). Returns an error response or None."""
    auth = request.headers.get('Authorization', '')
    supplied = auth[7:].strip() if auth.startswith('Bearer ') else ''
    if not supplied:
        return _unauthorized('Anmeldung erforderlich.')
    if any(hmac.compare_digest(supplied, t) for t in _configured_tokens()):
        g.mcp_user = None
        g.mcp_actor = 'API-Token'
        return None
    tok = validate_access_token(supplied)
    if not tok:
        return _unauthorized('Token ungültig oder abgelaufen.')
    g.mcp_user = tok.user
    g.mcp_actor = tok.user.username
    return None


def _visible_tools():
    allow_delete = delete_allowed()
    return [t for t in TOOLS.values() if allow_delete or not t.deletes]


def _call_tool(params):
    name = params.get('name')
    tool = TOOLS.get(name)
    if not tool:
        return None, f'Unbekanntes Tool: {name}'
    args = params.get('arguments') or {}
    if not isinstance(args, dict):
        return None, 'arguments muss ein Objekt sein.'
    for req in tool.input_schema.get('required', []):
        if args.get(req) is None:
            return _tool_error(f'Pflichtfeld fehlt: {req}'), None
    try:
        if tool.deletes and not delete_allowed():
            raise ToolError('Löschen über Claude ist in den Einstellungen deaktiviert '
                            '(Einstellungen → Claude / MCP → „Löschen erlauben“).')
        result = tool.fn(**args)
    except TypeError as e:
        db.session.rollback()
        return _tool_error(f'Ungültige Argumente: {e}'), None
    except (ApiError, ToolError) as e:
        db.session.rollback()
        return _tool_error(e.message), None
    except Exception as e:  # report instead of failing the whole RPC call
        db.session.rollback()
        return _tool_error(f'Interner Fehler: {e}'), None
    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, indent=1, default=str)
    return {'content': [{'type': 'text', 'text': text}], 'isError': False}, None


def _tool_error(message):
    return {'content': [{'type': 'text', 'text': message}], 'isError': True}


def _handle(msg):
    """Handle one JSON-RPC message. Returns a response dict or None (notifications)."""
    if not isinstance(msg, dict) or msg.get('jsonrpc') != '2.0':
        return _rpc_error(None, -32600, 'Invalid Request')
    method = msg.get('method')
    msg_id = msg.get('id')
    if method is None or msg_id is None:
        return None  # notification or client response: nothing to answer
    params = msg.get('params') or {}

    if method == 'initialize':
        requested = params.get('protocolVersion')
        version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else SUPPORTED_PROTOCOL_VERSIONS[0]
        settings = SiteSettings.query.first()
        brand = (settings.display_name or settings.business_name) if settings else None
        return _rpc_result(msg_id, {
            'protocolVersion': version,
            'capabilities': {'tools': {'listChanged': False}},
            'serverInfo': {'name': 'erp-rent', 'title': f'{brand or "ERP-Rent"} Verwaltung',
                           'version': SERVER_VERSION},
            'instructions': INSTRUCTIONS,
        })
    if method == 'ping':
        return _rpc_result(msg_id, {})
    if method == 'tools/list':
        return _rpc_result(msg_id, {'tools': [t.descriptor() for t in _visible_tools()]})
    if method == 'tools/call':
        result, error = _call_tool(params)
        if error:
            return _rpc_error(msg_id, -32602, error)
        return _rpc_result(msg_id, result)
    if method in ('resources/list', 'resources/templates/list', 'prompts/list'):
        key = {'resources/list': 'resources', 'resources/templates/list': 'resourceTemplates',
               'prompts/list': 'prompts'}[method]
        return _rpc_result(msg_id, {key: []})
    return _rpc_error(msg_id, -32601, f'Method not found: {method}')


@mcp_bp.after_request
def _cors(resp):
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Access-Control-Allow-Headers'] = 'Authorization, Content-Type, MCP-Protocol-Version, Mcp-Session-Id'
    resp.headers['Access-Control-Allow-Methods'] = 'POST, GET, DELETE, OPTIONS'
    resp.headers['Access-Control-Expose-Headers'] = 'WWW-Authenticate, Mcp-Session-Id'
    return resp


@mcp_bp.route('/mcp', methods=['GET', 'POST', 'DELETE'])
def mcp_endpoint():
    if not mcp_enabled():
        return jsonify({'error': 'MCP ist in den Einstellungen deaktiviert.'}), 404
    err = _authenticate()
    if err:
        return err
    if request.method != 'POST':
        # Stateless server: no server-initiated SSE stream, no sessions to delete.
        return Response(status=405, headers={'Allow': 'POST'})

    try:
        payload = json.loads(request.get_data(as_text=True) or 'null')
    except ValueError:
        return jsonify(_rpc_error(None, -32700, 'Parse error')), 400

    if isinstance(payload, list):
        responses = [r for r in (_handle(m) for m in payload) if r is not None]
        return (jsonify(responses), 200) if responses else Response(status=202)
    response = _handle(payload)
    if response is None:
        return Response(status=202)
    return jsonify(response)
