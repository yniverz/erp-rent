"""Gunicorn settings (loaded automatically from the working directory)."""
import gunicorn.http.wsgi as _wsgi

# Don't advertise the server software: gunicorn always writes "Server: gunicorn"
# itself, so a header set by the app can't replace it. Drop the line instead.
# Relies on gunicorn internals (version pinned in requirements.txt).
_default_headers = getattr(_wsgi.Response, 'default_headers', None)
if _default_headers:
    def _without_server_header(self):
        return [h for h in _default_headers(self) if not h.lower().startswith('server:')]
    _wsgi.Response.default_headers = _without_server_header
else:
    print('gunicorn.conf.py: Response.default_headers not found, Server header stays')
