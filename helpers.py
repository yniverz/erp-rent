import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from models import db, Item, Quote, QuoteItem, PackageComponent, ThrottleEvent
from sqlalchemy import and_, or_
from datetime import datetime, timedelta

# Longest throttle window in use; older ThrottleEvent rows are deleted
THROTTLE_RETENTION = timedelta(hours=1)


def replacement_value_basis(tax_mode):
    """Replacement values are net when regularly taxed (input VAT is recoverable), otherwise gross."""
    return 'netto' if tax_mode == 'regular' else 'brutto'


def get_upload_path():
    """Get the path for uploaded files"""
    base = os.path.join(os.path.dirname(__file__), 'instance', 'uploads')
    os.makedirs(base, exist_ok=True)
    return base


def throttle_count(kind, window, ip=None, subject=None):
    """Number of `kind` events within `window`, optionally filtered by ip and/or subject."""
    q = ThrottleEvent.query.filter(ThrottleEvent.kind == kind,
                                   ThrottleEvent.created_at >= datetime.utcnow() - window)
    if ip is not None:
        q = q.filter(ThrottleEvent.ip == ip)
    if subject is not None:
        q = q.filter(ThrottleEvent.subject == subject[:80])
    return q.count()


def throttle_record(kind, ip, subject=''):
    """Record one event and prune rows past the retention window."""
    ThrottleEvent.query.filter(ThrottleEvent.created_at < datetime.utcnow() - THROTTLE_RETENTION).delete()
    db.session.add(ThrottleEvent(kind=kind, ip=ip, subject=(subject or '')[:80]))
    db.session.commit()


TOTP_PERIOD = 30
TOTP_DIGITS = 6


def totp_new_secret():
    """Random 160-bit base32 secret (what authenticator apps expect)."""
    import base64
    import secrets
    return base64.b32encode(secrets.token_bytes(20)).decode('ascii').rstrip('=')


def _totp_at(secret, step):
    import base64
    import hashlib
    import hmac
    import struct
    key = base64.b32decode(secret + '=' * (-len(secret) % 8))
    digest = hmac.new(key, struct.pack('>Q', step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = (struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** TOTP_DIGITS)
    return f'{value:0{TOTP_DIGITS}d}'


def totp_verify(secret, code, last_step=None):
    """RFC 6238 check allowing ±1 step of clock drift.
    Returns the matched time step (store it to block replays) or None."""
    import hmac
    import time
    code = ''.join(ch for ch in str(code or '') if ch.isdigit())
    if not secret or len(code) != TOTP_DIGITS:
        return None
    now = int(time.time()) // TOTP_PERIOD
    for step in (now - 1, now, now + 1):
        if last_step is not None and step <= last_step:
            continue
        if hmac.compare_digest(_totp_at(secret, step), code):
            return step
    return None


def totp_uri(secret, account, issuer):
    from urllib.parse import quote
    label = quote(f'{issuer}:{account}')
    return f'otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}&period={TOTP_PERIOD}&digits={TOTP_DIGITS}'


def header_safe(value):
    """Collapse CR/LF and other whitespace so user input can't inject e-mail headers."""
    return ' '.join(str(value or '').split())


def _booked_quantity(item_id, start_date, end_date, exclude_quote_id=None):
    """Total quantity of an item booked in overlapping quotes."""
    overlapping_quotes = Quote.query.filter(
        Quote.status == 'draft',
        Quote.start_date.isnot(None),
        Quote.end_date.isnot(None),
        or_(
            and_(Quote.start_date <= end_date, Quote.start_date >= start_date),
            and_(Quote.end_date <= end_date, Quote.end_date >= start_date),
            and_(Quote.start_date <= start_date, Quote.end_date >= end_date)
        )
    )
    if exclude_quote_id:
        overlapping_quotes = overlapping_quotes.filter(Quote.id != exclude_quote_id)

    booked = 0
    for quote in overlapping_quotes.all():
        for quote_item in quote.quote_items:
            if quote_item.is_custom or quote_item.is_heading or quote_item.is_optional:
                continue
            if quote_item.item_id == item_id:
                booked += quote_item.quantity
    return booked


def get_available_quantity(item_id, start_date, end_date, exclude_quote_id=None):
    """
    Calculate available quantity for an item during a specific date range.
    Considers overlapping quotes, subtracts out-of-service units, and includes
    supplier quantities on top of own stock.
    Returns -1 for unlimited items.
    """
    item = Item.query.get(item_id)
    if not item:
        return 0

    total = item.operational_quantity
    if total == -1:
        return -1

    booked = _booked_quantity(item_id, start_date, end_date, exclude_quote_id)
    return max(0, total - booked)


def get_own_stock_available(item_id, start_date, end_date, exclude_quote_id=None):
    """
    Available quantity from OWN stock only (no suppliers) during a date range.
    Bookings are counted against own stock first. Returns -1 for unlimited.
    """
    item = Item.query.get(item_id)
    if not item:
        return 0

    own = item.operational_stock
    if own == -1:
        return -1

    booked = _booked_quantity(item_id, start_date, end_date, exclude_quote_id)
    return max(0, own - booked)


def get_package_available_quantity(package_id, start_date, end_date, exclude_quote_id=None):
    """
    Calculate how many units of a package can be rented based on component availability.
    Returns min(available_component / component_qty) across all components.
    Returns -1 if all components are unlimited.
    """
    package = Item.query.get(package_id)
    if not package or not package.is_package or not package.package_components:
        return 0

    min_available = None
    for pc in package.package_components:
        comp_available = get_available_quantity(
            pc.component_item_id, start_date, end_date, exclude_quote_id
        )
        if comp_available == -1:
            continue  # unlimited component doesn't constrain
        packages_from_this = comp_available // pc.quantity
        if min_available is None or packages_from_this < min_available:
            min_available = packages_from_this

    return min_available if min_available is not None else -1


def send_inquiry_notification(inquiry, settings):
    """Send email notification about a new inquiry"""
    smtp_server = os.getenv('SMTP_SERVER')
    smtp_port = int(os.getenv('SMTP_PORT', '587'))
    smtp_user = os.getenv('SMTP_USER')
    smtp_password = os.getenv('SMTP_PASSWORD')
    smtp_from = os.getenv('SMTP_FROM', smtp_user)

    if not all([smtp_server, smtp_user, smtp_password]):
        print("SMTP not configured, skipping email notification")
        return False

    recipient = settings.notification_email if settings and settings.notification_email else smtp_user
    if not recipient:
        print("No notification email configured")
        return False

    business_name = settings.display_name or settings.business_name if settings and (settings.display_name or settings.business_name) else 'ERP Rent'

    # Build item list
    item_lines = []
    for inq_item in inquiry.items:
        if inq_item.price_snapshot is not None:
            item_lines.append(f"  - {inq_item.quantity}x {inq_item.item_name_snapshot} @ €{inq_item.price_snapshot:.2f}/Tag")
        else:
            item_lines.append(f"  - {inq_item.quantity}x {inq_item.item_name_snapshot} (Preis auf Anfrage)")
    items_text = '\n'.join(item_lines) if item_lines else '  (keine Artikel)'

    dates_text = ''
    if inquiry.desired_start_date and inquiry.desired_end_date:
        dates_text = f"{inquiry.desired_start_date.strftime('%d.%m.%Y')} - {inquiry.desired_end_date.strftime('%d.%m.%Y')}"
    else:
        dates_text = 'Nicht angegeben'

    body = f"""## Mietanfrage ##

Kunde: {inquiry.customer_name}
E-Mail: {inquiry.customer_email}
Telefon: {inquiry.customer_phone or 'Nicht angegeben'}

Gewünschter Zeitraum: {dates_text}

Artikel:
{items_text}

Nachricht:
{inquiry.message or '(keine Nachricht)'}

---

"""

    msg = MIMEMultipart()
    msg['From'] = smtp_from
    msg['To'] = recipient
    msg['Subject'] = header_safe(f'[{business_name}] Mietanfrage von {inquiry.customer_name}')
    # customer_email is validated on submit (no whitespace); header_safe is belt and braces
    msg['Reply-To'] = header_safe(inquiry.customer_email)
    msg.attach(MIMEText(body, 'plain'))

    try:
        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_password)
            server.send_message(msg)
        print(f"Inquiry notification sent to {recipient}")
        return True
    except Exception as e:
        print(f"Failed to send inquiry notification: {e}")
        return False


IMAGE_FORMATS = {'JPEG': 'jpg', 'PNG': 'png', 'GIF': 'gif', 'WEBP': 'webp'}


class InvalidImage(ValueError):
    pass


# ── Image metadata ────────────────────────────────────────────────────
# Uploads lose all metadata (GPS, camera/lens, serial numbers, timestamps, editor
# data, comments) except rights-management information: author, copyright notice,
# licence/usage terms, credit and source. Removing those can be unlawful
# (§ 95c UrhG / Art. 7 InfoSoc directive), so they are carried over.

_NS = {
    'x': 'adobe:ns:meta/',
    'rdf': 'http://www.w3.org/1999/02/22-rdf-syntax-ns#',
    'dc': 'http://purl.org/dc/elements/1.1/',
    'xmpRights': 'http://ns.adobe.com/xap/1.0/rights/',
    'photoshop': 'http://ns.adobe.com/photoshop/1.0/',
    'cc': 'http://creativecommons.org/ns#',
}
# rights key -> XMP property (namespace prefix, name)
_XMP_RIGHTS = {
    'creator': ('dc', 'creator'),
    'rights': ('dc', 'rights'),
    'usage_terms': ('xmpRights', 'UsageTerms'),
    'web_statement': ('xmpRights', 'WebStatement'),
    'credit': ('photoshop', 'Credit'),
    'source': ('photoshop', 'Source'),
    'license': ('cc', 'license'),
}
# IPTC IIM (record 2) datasets
_IPTC_RIGHTS = {(2, 80): 'creator', (2, 116): 'rights', (2, 110): 'credit', (2, 115): 'source'}
_EXIF_ARTIST, _EXIF_COPYRIGHT = 0x013B, 0x8298


def _decode_text(value):
    if isinstance(value, bytes):
        try:
            value = value.decode('utf-8')
        except UnicodeDecodeError:
            value = value.decode('latin-1')
    return value.replace('\x00', '').strip()


def _xmp_values(root, prefix, name):
    """Values of an XMP property: attribute form, simple element or rdf:Seq/Bag/Alt items."""
    tag = f'{{{_NS[prefix]}}}{name}'
    values = []
    for desc in root.iter(f'{{{_NS["rdf"]}}}Description'):
        if tag in desc.attrib:
            values.append(desc.attrib[tag])
    for el in root.iter(tag):
        items = list(el.iter(f'{{{_NS["rdf"]}}}li'))
        if items:
            values.extend(li.text or '' for li in items)
        elif el.get(f'{{{_NS["rdf"]}}}resource'):
            values.append(el.get(f'{{{_NS["rdf"]}}}resource'))
        else:
            values.append(el.text or '')
    return [v.strip() for v in values if v and v.strip()]


def _extract_rights(img):
    """Collect rights-management info from EXIF, XMP and IPTC.
    Returns {'creator': [..], 'rights': str, ...} (only present keys)."""
    import xml.etree.ElementTree as ET
    found = {}

    def add(key, value):
        value = _decode_text(value) if value else ''
        if not value:
            return
        if key == 'creator':
            names = found.setdefault('creator', [])
            for n in (n.strip() for n in value.split(';')):
                # EXIF holds an ASCII copy of the XMP name ("Jorg" for "Jörg"): same person
                if n and _exif_ascii(n).lower() not in {_exif_ascii(x).lower() for x in names}:
                    names.append(n)
        else:
            found.setdefault(key, value)

    xmp = img.info.get('xmp') or img.info.get('XML:com.adobe.xmp')
    if xmp:
        try:
            root = ET.fromstring(xmp.encode('utf-8') if isinstance(xmp, str) else xmp.strip(b'\x00 \n'))
            for key, (prefix, name) in _XMP_RIGHTS.items():
                for v in _xmp_values(root, prefix, name):
                    add(key, v)
        except ET.ParseError:
            pass
    try:
        from PIL import IptcImagePlugin
        iptc = IptcImagePlugin.getiptcinfo(img) or {}
    except Exception:
        iptc = {}
    for dataset, key in _IPTC_RIGHTS.items():
        values = iptc.get(dataset)
        for v in values if isinstance(values, list) else [values]:
            if v:
                add(key, v)
    try:
        exif = img.getexif()
    except Exception:
        exif = {}
    add('creator', exif.get(_EXIF_ARTIST))
    # EXIF Copyright may hold "photographer\0editor" copyrights
    copyright_ = _decode_text(exif.get(_EXIF_COPYRIGHT) or '')
    add('rights', ' / '.join(p for p in copyright_.split('\x00') if p.strip()) if copyright_ else '')
    return found


def _exif_ascii(text):
    import unicodedata
    text = text.replace('©', '(c)').replace('®', '(R)').replace('ß', 'ss')
    return unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode('ascii')


def _rights_exif(rights):
    from PIL import Image
    exif = Image.Exif()
    if rights.get('creator'):
        exif[_EXIF_ARTIST] = _exif_ascii('; '.join(rights['creator']))
    if rights.get('rights'):
        exif[_EXIF_COPYRIGHT] = _exif_ascii(rights['rights'])
    return exif


def _rights_xmp(rights):
    """Minimal XMP packet holding only the rights fields (UTF-8 bytes)."""
    from xml.sax.saxutils import escape, quoteattr
    if not rights:
        return b''
    props = []
    if rights.get('creator'):
        items = ''.join(f'<rdf:li>{escape(n)}</rdf:li>' for n in rights['creator'])
        props.append(f'<dc:creator><rdf:Seq>{items}</rdf:Seq></dc:creator>')
    for key in ('rights', 'usage_terms'):
        if rights.get(key):
            prefix, name = _XMP_RIGHTS[key]
            props.append(f'<{prefix}:{name}><rdf:Alt><rdf:li xml:lang="x-default">{escape(rights[key])}'
                         f'</rdf:li></rdf:Alt></{prefix}:{name}>')
    for key in ('credit', 'source'):
        if rights.get(key):
            prefix, name = _XMP_RIGHTS[key]
            props.append(f'<{prefix}:{name}>{escape(rights[key])}</{prefix}:{name}>')
    for key in ('web_statement', 'license'):
        if rights.get(key):
            prefix, name = _XMP_RIGHTS[key]
            props.append(f'<{prefix}:{name} rdf:resource={quoteattr(rights[key])}/>')
    if rights.get('rights'):
        props.append('<xmpRights:Marked>True</xmpRights:Marked>')
    ns = ' '.join(f'xmlns:{k}="{v}"' for k, v in _NS.items() if k != 'x')
    return (f'<x:xmpmeta xmlns:x="{_NS["x"]}"><rdf:RDF {ns}><rdf:Description rdf:about="">'
            + ''.join(props) + '</rdf:Description></rdf:RDF></x:xmpmeta>').encode('utf-8')


def _clean_image_bytes(data):
    """Validate raster image bytes and re-encode them without metadata, keeping only
    the colour profile and rights info (author, copyright, licence, credit, source).
    Returns (bytes, extension)."""
    import io
    from PIL import Image, ImageOps, PngImagePlugin
    try:
        probe = Image.open(io.BytesIO(data))
        probe.verify()
        img = Image.open(io.BytesIO(data))
        fmt = img.format
    except Exception:
        raise InvalidImage('Die Datei ist kein gültiges Bild.')
    ext = IMAGE_FORMATS.get(fmt)
    if not ext:
        raise InvalidImage(f'Bildformat {fmt} wird nicht unterstützt (JPEG, PNG, GIF, WebP).')

    rights = _extract_rights(img)
    save_kwargs = {}
    icc = img.info.get('icc_profile')
    if icc:
        save_kwargs['icc_profile'] = icc  # colour profile only, no location data
    if rights:
        if fmt in ('JPEG', 'WEBP', 'PNG'):
            save_kwargs['exif'] = _rights_exif(rights)
        if fmt in ('JPEG', 'WEBP'):
            save_kwargs['xmp'] = _rights_xmp(rights)
        elif fmt == 'PNG':
            info = PngImagePlugin.PngInfo()
            info.add_itxt('XML:com.adobe.xmp', _rights_xmp(rights).decode('utf-8'))
            save_kwargs['pnginfo'] = info
    if fmt in ('JPEG', 'GIF'):
        # Explicit comment: Pillow would otherwise copy the original comment over.
        # GIF has no rights fields, so the copyright notice goes into its comment.
        notice = (rights.get('rights') or '; '.join(rights.get('creator', []))) if fmt == 'GIF' else ''
        save_kwargs['comment'] = notice.encode('utf-8')
    if fmt == 'GIF' and getattr(img, 'n_frames', 1) > 1:
        from PIL import ImageSequence
        durations = [frame.info.get('duration', 100) for frame in ImageSequence.Iterator(img)]
        img.seek(0)
        frames_out = img  # keep animation; GIF carries no EXIF
        save_kwargs.update(save_all=True, loop=img.info.get('loop', 0), duration=durations)
    else:
        # Orientation from EXIF is applied to the pixels first so photos stay upright
        frames_out = ImageOps.exif_transpose(img)
        frames_out.info = {k: v for k, v in frames_out.info.items()
                           if k in ('transparency', 'background')}
        if fmt == 'JPEG':
            if frames_out.mode not in ('RGB', 'L', 'CMYK'):
                frames_out = frames_out.convert('RGB')
            save_kwargs.update(quality=90, optimize=True)
        elif fmt == 'WEBP':
            save_kwargs.update(quality=90)

    out = io.BytesIO()
    frames_out.save(out, format=fmt, **save_kwargs)
    return out.getvalue(), ext


def save_clean_image(data, filename_stem=None):
    """Validate raster image bytes, strip metadata (rights info is kept) and write
    them to uploads/. Returns the stored filename (<uuid or stem>.<ext>)."""
    import uuid
    clean, ext = _clean_image_bytes(data)
    filename = f'{filename_stem or uuid.uuid4().hex}.{ext}'
    with open(os.path.join(get_upload_path(), filename), 'wb') as f:
        f.write(clean)
    return filename


_SVG_NS = 'http://www.w3.org/2000/svg'
# Editor namespaces that carry document names, export paths and editor state
_SVG_EDITOR_NS = ('http://sodipodi.sourceforge.net/', 'http://www.inkscape.org/',
                  'http://ns.adobe.com/', 'http://www.bohemiancoding.com/sketch/',
                  'http://ns.serif.com/')


def _svg_rights(metadata):
    """Rights info from an SVG <metadata> block (RDF / Dublin Core / Creative Commons)."""
    found = {}
    if metadata is None:
        return found
    for key, prop in (('creator', 'creator'), ('rights', 'rights'), ('publisher', 'publisher')):
        for el in metadata.iter(f'{{{_NS["dc"]}}}{prop}'):
            # Inkscape: <dc:creator><cc:Agent><dc:title>Name</dc:title></cc:Agent></dc:creator>
            texts = [t.strip() for t in el.itertext() if t.strip()]
            if texts:
                found.setdefault(key, ' '.join(texts))
    for el in metadata.iter(f'{{{_NS["cc"]}}}license'):
        url = el.get(f'{{{_NS["rdf"]}}}resource') or (el.text or '').strip()
        if url:
            found.setdefault('license', url)
    return found


def clean_svg(data):
    """Strip metadata from an SVG: comments, editor namespaces (Inkscape/Sodipodi/
    Illustrator/Sketch) and their attributes, and everything in <metadata> except
    rights info (creator, rights, publisher, licence), which is rewritten minimally."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(data)  # comments and processing instructions are dropped
    except ET.ParseError:
        raise InvalidImage('Die SVG-Datei ist ungültig.')
    if root.tag != f'{{{_SVG_NS}}}svg':
        raise InvalidImage('Die Datei ist keine SVG-Grafik.')

    def is_editor(name):
        return name.startswith('{') and name[1:].startswith(_SVG_EDITOR_NS)

    rights = {}
    for parent in root.iter():
        for child in list(parent):
            if child.tag == f'{{{_SVG_NS}}}metadata':
                rights.update({k: v for k, v in _svg_rights(child).items() if k not in rights})
                parent.remove(child)
            elif is_editor(child.tag):
                parent.remove(child)
    for el in root.iter():
        for attr in [a for a in el.attrib if is_editor(a)]:
            del el.attrib[attr]

    if rights:
        rdf, dc, cc = _NS['rdf'], _NS['dc'], _NS['cc']
        meta = ET.Element(f'{{{_SVG_NS}}}metadata')
        work = ET.SubElement(ET.SubElement(meta, f'{{{rdf}}}RDF'), f'{{{cc}}}Work', {f'{{{rdf}}}about': ''})
        for key in ('creator', 'rights', 'publisher'):
            if rights.get(key):
                agent = ET.SubElement(ET.SubElement(work, f'{{{dc}}}{key}'), f'{{{cc}}}Agent')
                ET.SubElement(agent, f'{{{dc}}}title').text = rights[key]
        if rights.get('license'):
            ET.SubElement(work, f'{{{cc}}}license', {f'{{{rdf}}}resource': rights['license']})
        root.insert(0, meta)

    ET.register_namespace('', _SVG_NS)
    for prefix in ('rdf', 'dc', 'cc'):
        ET.register_namespace(prefix, _NS[prefix])
    ET.register_namespace('xlink', 'http://www.w3.org/1999/xlink')
    return ET.tostring(root, encoding='utf-8', xml_declaration=True)


def _image_has_metadata(path):
    """True if a stored raster image carries anything beyond what _clean_image_bytes keeps."""
    from PIL import Image
    with Image.open(path) as img:
        rights = _extract_rights(img)
        exif = img.getexif()
        if set(exif) - {_EXIF_ARTIST, _EXIF_COPYRIGHT} or exif.get_ifd(0x8769) or exif.get_ifd(0x8825):
            return True
        if any(k in img.info for k in ('comment', 'photoshop')) and img.format != 'GIF':
            return True
        xmp = img.info.get('xmp') or img.info.get('XML:com.adobe.xmp')
        if xmp:
            xmp = xmp.encode('utf-8') if isinstance(xmp, str) else xmp
            if xmp.strip(b'\x00 \n') != _rights_xmp(rights):
                return True
        if set(getattr(img, 'text', None) or {}) - {'XML:com.adobe.xmp'}:  # other PNG text chunks
            return True
        if img.format == 'GIF':
            notice = rights.get('rights') or '; '.join(rights.get('creator', []))
            return bool(img.info.get('comment')) and _decode_text(img.info['comment']) != notice
        # JPEG APP segments other than JFIF (APP0), EXIF/XMP (APP1), ICC (APP2), Adobe colour (APP14)
        return any(marker not in ('APP0', 'APP1', 'APP2', 'APP14') for marker, _ in getattr(img, 'applist', []))


# One-time cleanup of images uploaded before metadata stripping existed.
# TODO: remove (with its call in app.py) two releases after 2026-10.
METADATA_CLEANUP_MARKER = '.metadata-cleaned-v1'


def clean_existing_uploads():
    """Strip metadata from all images already in uploads/ (runs once, guarded by
    a marker file and a lock so several gunicorn workers don't race)."""
    import fcntl
    folder = get_upload_path()
    marker = os.path.join(folder, METADATA_CLEANUP_MARKER)
    if os.path.exists(marker):
        return
    with open(os.path.join(folder, '.metadata-cleanup.lock'), 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if os.path.exists(marker):
            return
        cleaned = failed = 0
        for name in sorted(os.listdir(folder)):
            path = os.path.join(folder, name)
            ext = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
            if name.startswith('.') or not os.path.isfile(path):
                continue
            try:
                if ext == 'svg':
                    with open(path, 'rb') as f:
                        original = f.read()
                    clean = clean_svg(original)
                    if clean == original:
                        continue
                elif ext in ('jpg', 'jpeg', 'png', 'gif', 'webp'):
                    if not _image_has_metadata(path):
                        continue
                    with open(path, 'rb') as f:
                        clean, _ = _clean_image_bytes(f.read())
                else:
                    continue
                tmp = path + '.tmp'
                with open(tmp, 'wb') as f:
                    f.write(clean)
                os.replace(tmp, path)  # same filename, so DB references stay valid
                cleaned += 1
            except Exception as e:
                failed += 1
                print(f"  metadata cleanup: skipped {name}: {e}")
        with open(marker, 'w') as f:
            f.write(datetime.utcnow().isoformat())
        print(f"  metadata cleanup: {cleaned} image(s) cleaned, {failed} skipped")


def allowed_image_file(filename):
    """Check if a filename has an allowed image extension"""
    allowed = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in allowed


def allowed_document_file(filename):
    """Check if a filename has an allowed document/image extension"""
    allowed = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'pdf', 'doc', 'docx', 'xls', 'xlsx', 'txt', 'csv'}
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in allowed


def public_base_url():
    """External base URL of this app without trailing slash.
    PUBLIC_URL (e.g. https://verleih.example.com) wins; needed behind a
    reverse proxy so OAuth/MCP metadata advertises the https URL."""
    from flask import request
    configured = (os.getenv('PUBLIC_URL') or '').strip().rstrip('/')
    return configured or request.url_root.rstrip('/')
