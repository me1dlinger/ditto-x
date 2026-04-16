import sqlite3
import os
import re
import struct
import json
from datetime import datetime, timezone
from flask import Flask, jsonify, request, render_template, Response


def get_resource_path(relative_path):
    """Get absolute path to resource, works for dev and for PyInstaller"""
    import sys
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)


def get_settings_path():
    """Find settings.json beside the executable or script."""
    import sys
    if getattr(sys, 'frozen', False):
        base_dir = os.path.dirname(sys.executable)
    else:
        base_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_dir, 'settings.json')


SETTINGS_FILE = get_settings_path()


class ConfigManager:
    def __init__(self):
        self.default_db = os.environ.get('DITTO_DB', 'E:/DittoFile/Ditto.db')
        self.settings = self.load()

    def load(self):
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                pass
        return {"db_paths": [self.default_db], "current_path": self.default_db}

    def save(self):
        try:
            with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.settings, f, indent=4, ensure_ascii=False)
        except Exception as e:
            print(f"Error saving settings: {e}")

    @property
    def db_path(self):
        return self.settings.get("current_path", self.default_db)


config = ConfigManager()


def get_db():
    """Open ditto.db read-only using URI mode."""
    db_path = config.db_path
    if not os.path.exists(db_path):
        alt_path = os.path.join(os.path.dirname(__file__), db_path)
        if os.path.exists(alt_path):
            db_path = alt_path

    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA cache_size = -8000")
    conn.execute("PRAGMA temp_store = MEMORY")
    return conn


def row_to_dict(row):
    return dict(row)


def get_db_rw():
    """Open ditto.db in read-write mode for cleanup operations."""
    db_path = config.db_path
    if not os.path.exists(db_path):
        alt_path = os.path.join(os.path.dirname(__file__), db_path)
        if os.path.exists(alt_path):
            db_path = alt_path
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA cache_size = -8000")
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


# ── Fine-grained Type Detection ──────────────────────────────────────────────────────────────
#
# Type System:
#   text          - Plain text (CF_TEXT / CF_UNICODETEXT, no HTML Format)
#   richtext      - Rich text (HTML Format with background/font styles, or Rich Text Format)
#                   Covers the original html / rtf types
#   screenshot    - Screenshot (mText='CF_DIB', has CF_DIB but no PNG, no CF_HDROP, no HTML Format)
#   copied_image  - Image copied from local software (mText starts with 'Copied File' and has CF_DIB)
#   web_image     - Image copied from web (has PNG format)
#   file          - File path (has CF_HDROP, no CF_DIB/PNG; mText contains 'Copied File' and no CF_DIB)
#
# Note: both copied_image and file may have mText = 'Copied File ...'
#   Distinction: copied_image has CF_DIB; file has no CF_DIB but only CF_HDROP

def detect_clip_type(mtext: str, formats: list[str]) -> str:
    """
    Returns the fine-grained type string.
    """
    has_png = 'PNG' in formats
    has_dib = 'CF_DIB' in formats
    has_hdrop = 'CF_HDROP' in formats
    has_html = 'HTML Format' in formats
    has_rtf = 'Rich Text Format' in formats
    has_text = 'CF_UNICODETEXT' in formats and 'CF_TEXT' in formats

    # ── Image Types ──
    # Image copied from web: has PNG
    if has_png:
        return 'web_image'

    # Image copied from local software: mText='Copied File...' and has CF_DIB
    if  has_dib and has_hdrop:
        return 'copied_image'

    # Screenshot: mText='CF_DIB', has CF_DIB, no PNG, no CF_HDROP, no HTML Format
    if mtext == 'CF_DIB' and has_dib:
        return 'screenshot'

    # ── File Path Types ──
    # Has CF_HDROP and no image data (CF_DIB/PNG)
    if has_hdrop and not has_dib and not has_png:
        return 'file'

    # ── Rich Text Types (merged html + rtf) ──
    # Has HTML Format (web copied text, rich text code, styled text)
    if has_text and has_html:
        return 'richtext'


    # ── Plain Text ──
    if has_text and not has_html and not has_rtf:
        return 'text'

    # Fallback
    if mtext and mtext not in ('CF_DIB',):
        return 'text'
    return 'text'


# Used for API type parameter filtering → SQL conditions
# Frontend can pass: text, richtext, image, file
#   image  → Includes screenshot / copied_image / web_image
#   file   → Only file (excluding images)
def _type_filter_sql(ftype: str) -> str:
    if ftype == 'image':
        # Image copied from web: has PNG
        # Screenshot: mText='CF_DIB', has CF_DIB, no PNG
        # Image copied locally: has CF_DIB, has CF_HDROP
        return """AND (
            (
                 (lID IN (SELECT lParentID FROM Data WHERE strClipBoardFormat='CF_DIB')
                AND lID IN (SELECT lParentID FROM Data WHERE strClipBoardFormat IN ('CF_HDROP','PNG')))
                OR (mText = 'CF_DIB' AND (lID IN (SELECT lParentID FROM Data WHERE strClipBoardFormat='CF_DIB')))
            )
        )"""

    elif ftype == 'file':
        # Has CF_HDROP but no CF_DIB/PNG
        return """AND (
            (
                lID IN (SELECT lParentID FROM Data WHERE strClipBoardFormat='CF_HDROP')
                AND lID NOT IN (SELECT lParentID FROM Data WHERE strClipBoardFormat IN ('CF_DIB','PNG'))
            )
        )"""

    elif ftype == 'richtext':
        # Has CF_UNICODETEXT, CF_TEXT, HTML Format
        return """AND (
             (mText = 'HTML Format' AND lID IN (SELECT lParentID FROM Data WHERE strClipBoardFormat = 'HTML Format'))
                OR (lID IN (SELECT lParentID FROM Data WHERE strClipBoardFormat = 'HTML Format')
                AND (lID IN (SELECT lParentID FROM Data WHERE strClipBoardFormat = 'CF_TEXT')
                OR lID IN (SELECT lParentID FROM Data WHERE strClipBoardFormat = 'CF_UNICODETEXT'))
                AND lID NOT IN (SELECT lParentID FROM Data WHERE strClipBoardFormat IN ('PNG')))
        )"""

    elif ftype == 'text':
        # Has text format, no images, no files, no HTML/RTF Format
        return """AND (
            (lID IN (SELECT lParentID FROM Data WHERE strClipBoardFormat = 'CF_UNICODETEXT')
            OR lID IN (SELECT lParentID FROM Data WHERE strClipBoardFormat = 'CF_TEXT'))
            AND lID NOT IN (SELECT lParentID FROM Data WHERE strClipBoardFormat IN ('HTML Format','CF_HDROP'))
        )"""

    return ''


def decode_cf_text(blob: bytes) -> str:
    """Try to decode CF_TEXT blob (cp1252 / gbk fallback)."""
    if not blob:
        return ''
    b = blob.rstrip(b'\x00')
    for enc in ('utf-8', 'gbk', 'gb18030', 'cp1252', 'latin-1'):
        try:
            return b.decode(enc)
        except Exception:
            continue
    return b.decode('latin-1', errors='replace')


def parse_hdrop(blob: bytes) -> list[str]:
    """Parse CF_HDROP blob to list of file paths."""
    if not blob or len(blob) < 20:
        return []
    try:
        offset = struct.unpack_from('<I', blob, 0)[0]
        is_wide = struct.unpack_from('<I', blob, 16)[0]
        data = blob[offset:]
        paths = []
        if is_wide:
            text = data.decode('utf-16-le', errors='replace')
            paths = [p for p in text.split('\x00') if p]
        else:
            text = data.decode('mbcs', errors='replace')
            paths = [p for p in text.split('\x00') if p]
        return paths
    except Exception:
        return []


def get_best_text(lID: int, mtext: str, conn) -> tuple[str, str]:
    """
    Return (display_text, clip_type) for a Main record.
    """
    rows = conn.execute(
        "SELECT strClipBoardFormat, ooData FROM Data WHERE lParentID=? ORDER BY lID",
        (lID,)
    ).fetchall()

    formats = [r['strClipBoardFormat'] for r in rows]
    clip_type = detect_clip_type(mtext, formats)

    # Image types
    if clip_type in ('screenshot', 'copied_image', 'web_image'):
        # For copied_image, extract filename from mText for display
        if clip_type == 'copied_image' and mtext and mtext.startswith('Copied File'):
            parts = mtext.strip().split(' - ')
            if len(parts) >= 2:
                return f'[Image] {parts[1]}', clip_type
        return '[Image]', clip_type

    # File paths
    if clip_type == 'file':
        # Prioritize using CF_HDROP to parse path
        for r in rows:
            if r['strClipBoardFormat'] == 'CF_HDROP' and r['ooData']:
                paths = parse_hdrop(bytes(r['ooData']))
                if paths:
                    return '\n'.join(paths), 'file'
        # Extract path from mText
        if mtext and mtext.startswith('Copied File'):
            parts = mtext.strip().split(' - ')
            if len(parts) >= 3:
                return parts[2].strip(), 'file'
        return '[File]', 'file'

    # Rich text: prioritize returning plain text for list display
    if clip_type == 'richtext':
        for fmt in ('CF_UNICODETEXT', 'CF_TEXT'):
            for r in rows:
                if r['strClipBoardFormat'] == fmt and r['ooData']:
                    blob = bytes(r['ooData'])
                    if fmt == 'CF_UNICODETEXT':
                        try:
                            b = blob
                            while b.endswith(b'\x00\x00'):
                                b = b[:-2]
                            return b.decode('utf-16-le', errors='replace'), clip_type
                        except Exception:
                            pass
                    else:
                        return decode_cf_text(blob), clip_type

        for r in rows:
            if r['strClipBoardFormat'] == 'HTML Format' and r['ooData']:
                raw = bytes(r['ooData']).decode('utf-8', errors='replace')
                clean = re.sub(r'<[^>]+>', '', raw)
                clean = re.sub(r'\s+', ' ', clean).strip()
                return clean, clip_type
        return mtext or '[Rich Text]', clip_type

    # Plain text
    for fmt in ('CF_UNICODETEXT', 'CF_TEXT'):
        for r in rows:
            if r['strClipBoardFormat'] == fmt and r['ooData']:
                blob = bytes(r['ooData'])
                if fmt == 'CF_UNICODETEXT':
                    try:
                        b = blob
                        while b.endswith(b'\x00\x00'):
                            b = b[:-2]
                        return b.decode('utf-16-le', errors='replace'), clip_type
                    except Exception:
                        pass
                else:
                    return decode_cf_text(blob), clip_type

    if mtext and mtext != 'CF_DIB':
        return mtext, clip_type
    return '[No Text]', clip_type


def get_image_data(lID: int, conn) -> tuple[bytes | None, str]:
    """Return image bytes and mimetype. PNG preferred."""
    row = conn.execute(
        "SELECT ooData FROM Data WHERE lParentID=? AND strClipBoardFormat='PNG'",
        (lID,)
    ).fetchone()
    if row and row['ooData']:
        return bytes(row['ooData']), 'image/png'

    row = conn.execute(
        "SELECT ooData FROM Data WHERE lParentID=? AND strClipBoardFormat='CF_DIB'",
        (lID,)
    ).fetchone()
    if row and row['ooData']:
        dib_data = bytes(row['ooData'])
        if dib_data.startswith(b'BM'):
            return dib_data, 'image/bmp'
        try:
            header_size = struct.unpack('<I', dib_data[:4])[0]
            bit_count = struct.unpack('<H', dib_data[14:16])[0]
            compression = struct.unpack('<I', dib_data[16:20])[0]
            clr_used = struct.unpack('<I', dib_data[32:36])[0]

            if clr_used == 0 and bit_count <= 8:
                clr_used = 1 << bit_count

            pixel_offset = header_size + (clr_used * 4)

            if compression == 3 and header_size == 40:
                pixel_offset += 12

            if bit_count == 32:
                dib_mut = bytearray(dib_data)
                for i in range(pixel_offset + 3, len(dib_mut), 4):
                    dib_mut[i] = 255
                dib_data = bytes(dib_mut)

            file_size = 14 + len(dib_data)
            offset = 14 + pixel_offset

            bmp_header = struct.pack('<2sIHHI', b'BM', file_size, 0, 0, offset)
            return bmp_header + dib_data, 'image/bmp'
        except Exception:
            return dib_data, 'image/bmp'

    return None, 'image/png'


def get_html_data(lID: int, conn) -> str | None:
    """Return raw HTML string."""
    row = conn.execute(
        "SELECT ooData FROM Data WHERE lParentID=? AND strClipBoardFormat='HTML Format'",
        (lID,)
    ).fetchone()
    if row and row['ooData']:
        return bytes(row['ooData']).decode('utf-8', errors='replace')
    return None


template_dir = get_resource_path('python/templates')
if not os.path.exists(template_dir):
    template_dir = get_resource_path('templates')

app = Flask(__name__, template_folder=template_dir)
app.config['JSON_SORT_KEYS'] = False


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/clips')
def api_clips():
    page = max(1, int(request.args.get('page', 1)))
    page_size = min(200, max(10, int(request.args.get('page_size', 50))))
    q = request.args.get('q', '').strip()
    ftype = request.args.get('type', '')
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')
    sort = request.args.get('sort', 'date_desc')
    pinned = request.args.get('pinned', '')

    where = ["bIsGroup = 0"]
    params: list = []

    if q:
        where.append("mText LIKE ?")
        params.append(f'%{q}%')
    if date_from:
        where.append("lDate >= ?")
        params.append(int(date_from))
    if date_to:
        where.append("lDate <= ?")
        params.append(int(date_to))
    if pinned == '1':
        where.append("lDontAutoDelete = 1")

    type_where = _type_filter_sql(ftype)

    order = {
        'date_desc': 'lDate DESC',
        'date_asc':  'lDate ASC',
        'alpha':     'mText COLLATE NOCASE ASC',
    }.get(sort, 'lDate DESC')

    where_sql = ' AND '.join(where)
    base_sql = f"FROM Main WHERE {where_sql} {type_where}"

    conn = get_db()
    try:
        total = conn.execute(f"SELECT COUNT(*) {base_sql}", params).fetchone()[0]
        offset = (page - 1) * page_size
        rows = conn.execute(
            f"SELECT lID, lDate, mText, lDontAutoDelete, CRC, lShortCut, globalShortCut {base_sql} "
            f"ORDER BY {order} LIMIT ? OFFSET ?",
            params + [page_size, offset]
        ).fetchall()

        clips = []
        for r in rows:
            text, ctype = get_best_text(r['lID'], r['mText'], conn)
            dt = datetime.fromtimestamp(r['lDate'], tz=timezone.utc).isoformat()
            clips.append({
                'id': r['lID'],
                'date': r['lDate'],
                'date_iso': dt,
                'text': text[:300],
                'type': ctype,
                'pinned': bool(r['lDontAutoDelete']),
                'shortcut': r['lShortCut'] or r['globalShortCut'],
                'crc': r['CRC'],
            })

        return jsonify({
            'total': total,
            'page': page,
            'page_size': page_size,
            'clips': clips,
        })
    finally:
        conn.close()


@app.route('/api/clip/<int:clip_id>')
def api_clip_detail(clip_id):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM Main WHERE lID=?", (clip_id,)
        ).fetchone()
        if not row:
            return jsonify({'error': 'not found'}), 404

        row = row_to_dict(row)
        text, ctype = get_best_text(clip_id, row['mText'], conn)

        formats = conn.execute(
            "SELECT lID, strClipBoardFormat FROM Data WHERE lParentID=? ORDER BY lID",
            (clip_id,)
        ).fetchall()

        html_raw = None
        if ctype == 'richtext':
            html_raw = get_html_data(clip_id, conn)

        return jsonify({
            'id': clip_id,
            'date': row['lDate'],
            'date_iso': datetime.fromtimestamp(row['lDate'], tz=timezone.utc).isoformat(),
            'text': text,
            'type': ctype,
            'pinned': bool(row['lDontAutoDelete']),
            'shortcut': row['lShortCut'],
            'formats': [{'id': f['lID'], 'format': f['strClipBoardFormat']} for f in formats],
            'html_raw': html_raw,
            'mText': row['mText'],
        })
    finally:
        conn.close()


@app.route('/api/clip/<int:clip_id>/image')
def api_clip_image(clip_id):
    conn = get_db()
    try:
        data, mimetype = get_image_data(clip_id, conn)
        if not data:
            return Response(status=404)
        return Response(data, mimetype=mimetype)
    finally:
        conn.close()


@app.route('/api/stats')
def api_stats():
    conn = get_db()
    try:
        total = conn.execute("SELECT COUNT(*) FROM Main WHERE bIsGroup=0").fetchone()[0]
        total_images = conn.execute("""
            SELECT COUNT(*) FROM Main
            WHERE bIsGroup=0
                AND (lID IN (SELECT lParentID FROM Data WHERE strClipBoardFormat='CF_DIB')
                AND lID IN (SELECT lParentID FROM Data WHERE strClipBoardFormat IN ('CF_HDROP','PNG')))
                OR (mText = 'CF_DIB' AND (lID IN (SELECT lParentID FROM Data WHERE strClipBoardFormat='CF_DIB')))
        """).fetchone()[0]
        # File paths
        files = conn.execute("""
            SELECT COUNT(*) FROM Main
            WHERE bIsGroup=0 
                AND lID IN (SELECT lParentID FROM Data WHERE strClipBoardFormat='CF_HDROP')
                AND lID NOT IN (SELECT lParentID FROM Data WHERE strClipBoardFormat IN ('CF_DIB','PNG'))
            
        """).fetchone()[0]

        # Rich text
        richtext = conn.execute("""
            SELECT COUNT(*) FROM Main
            WHERE bIsGroup=0
                AND (mText = 'HTML Format' AND lID IN (SELECT lParentID FROM Data WHERE strClipBoardFormat = 'HTML Format'))
                OR (lID IN (SELECT lParentID FROM Data WHERE strClipBoardFormat = 'HTML Format')
                AND (lID IN (SELECT lParentID FROM Data WHERE strClipBoardFormat = 'CF_TEXT')
                OR lID IN (SELECT lParentID FROM Data WHERE strClipBoardFormat = 'CF_UNICODETEXT'))
                AND lID NOT IN (SELECT lParentID FROM Data WHERE strClipBoardFormat IN ('PNG')))
        """).fetchone()[0]

        # Plain text
        text_count = conn.execute("""
            SELECT COUNT(*) FROM Main
            WHERE bIsGroup=0
                AND (lID IN (SELECT lParentID FROM Data WHERE strClipBoardFormat = 'CF_UNICODETEXT')
                OR lID IN (SELECT lParentID FROM Data WHERE strClipBoardFormat = 'CF_TEXT'))
                AND lID NOT IN (SELECT lParentID FROM Data WHERE strClipBoardFormat IN ('HTML Format','CF_HDROP'))
        """).fetchone()[0]

        timeline = conn.execute("""
            SELECT date(lDate,'unixepoch','localtime') as day, COUNT(*) as cnt
            FROM Main WHERE bIsGroup=0
              AND lDate >= strftime('%s','now','-30 days')
            GROUP BY day ORDER BY day
        """).fetchall()

        oldest = conn.execute("SELECT MIN(lDate) FROM Main WHERE bIsGroup=0").fetchone()[0]
        newest = conn.execute("SELECT MAX(lDate) FROM Main WHERE bIsGroup=0").fetchone()[0]

        hours = conn.execute("""
            SELECT strftime('%H', lDate, 'unixepoch', 'localtime') as hr, COUNT(*) as cnt
            FROM Main WHERE bIsGroup=0
            GROUP BY hr ORDER BY hr
        """).fetchall()

        last_24h = conn.execute("""
            SELECT strftime('%Y-%m-%d %H', lDate, 'unixepoch', 'localtime') as hr_key, COUNT(*) as cnt
            FROM Main WHERE bIsGroup=0 AND lDate >= strftime('%s','now','-24 hours')
            GROUP BY hr_key ORDER BY hr_key
        """).fetchall()

        last_7d = conn.execute("""
            SELECT date(lDate,'unixepoch','localtime') as day, COUNT(*) as cnt
            FROM Main WHERE bIsGroup=0 AND lDate >= strftime('%s','now','-7 days')
            GROUP BY day ORDER BY day
        """).fetchall()

        active_days_row = conn.execute("""
            SELECT COUNT(DISTINCT date(lDate, 'unixepoch', 'localtime')) FROM Main WHERE bIsGroup=0
        """).fetchone()
        active_days = active_days_row[0] if active_days_row else 1

        days_diff = max(1, (newest - oldest) / 86400) if newest and oldest else 1
        avg_per_day = round(total / days_diff, 1)
        # 1. Plain text size
        text_bytes = conn.execute("""
            SELECT COALESCE(SUM(LENGTH(ooData)), 0)
            FROM Data
            WHERE lParentID IN (
                SELECT lID FROM Main
                WHERE bIsGroup=0
                AND (lID IN (SELECT lParentID FROM Data WHERE strClipBoardFormat='CF_UNICODETEXT' OR strClipBoardFormat='CF_TEXT'))
                AND lID NOT IN (SELECT lParentID FROM Data WHERE strClipBoardFormat IN ('HTML Format','CF_HDROP'))
            )
        """).fetchone()[0]

        # 2. Image (web image) size
        total_image_bytes = conn.execute("""
            SELECT COALESCE(SUM(LENGTH(ooData)), 0)
            FROM Data
            WHERE lParentID IN (
                SELECT lID FROM Main
                WHERE bIsGroup=0
                AND (lID IN (SELECT lParentID FROM Data WHERE strClipBoardFormat='CF_DIB')
                AND lID IN (SELECT lParentID FROM Data WHERE strClipBoardFormat IN ('CF_HDROP','PNG')))
                OR (mText='CF_DIB' AND lID IN (SELECT lParentID FROM Data WHERE strClipBoardFormat='CF_DIB'))
            )
        """).fetchone()[0]

        # 3. File path size
        file_bytes = conn.execute("""
            SELECT COALESCE(SUM(LENGTH(ooData)), 0)
            FROM Data
            WHERE lParentID IN (
                SELECT lID FROM Main
                WHERE bIsGroup=0
                AND lID IN (SELECT lParentID FROM Data WHERE strClipBoardFormat='CF_HDROP')
                AND lID NOT IN (SELECT lParentID FROM Data WHERE strClipBoardFormat IN ('CF_DIB','PNG'))
            )
        """).fetchone()[0]

        # 4. Rich text size
        richtext_bytes = conn.execute("""
            SELECT COALESCE(SUM(LENGTH(ooData)), 0)
            FROM Data
            WHERE lParentID IN (
                SELECT lID FROM Main
                WHERE bIsGroup=0
                AND (mText='HTML Format' AND lID IN (SELECT lParentID FROM Data WHERE strClipBoardFormat='HTML Format'))
                OR (lID IN (SELECT lParentID FROM Data WHERE strClipBoardFormat='HTML Format')
                AND (lID IN (SELECT lParentID FROM Data WHERE strClipBoardFormat='CF_TEXT' OR strClipBoardFormat='CF_UNICODETEXT'))
                AND lID NOT IN (SELECT lParentID FROM Data WHERE strClipBoardFormat IN ('PNG')))
            )
        """).fetchone()[0]

        breakdown = [
            {'type': 'text',         'count': text_count,    'bytes': text_bytes},
            {'type': 'richtext',     'count': richtext,      'bytes': richtext_bytes},
            {'type': 'image',   'count': total_images,   'bytes': total_image_bytes},
            {'type': 'file',         'count': files,         'bytes': file_bytes},
        ]
        breakdown = [b for b in breakdown if b['bytes'] > 0 or b['count'] > 0]
        breakdown.sort(key=lambda x: x['bytes'], reverse=True)
        total_bytes = sum(b['bytes'] for b in breakdown)

        for item in breakdown:
            item['percentage'] = round((item['bytes'] / total_bytes) * 100, 1) if total_bytes > 0 else 0

        return jsonify({
            'total': total,
            'images': total_images,
            'files': files,
            'richtext': richtext,
            'text': text_count,
            'oldest': oldest,
            'newest': newest,
            'timeline': [{'day': r['day'], 'cnt': r['cnt']} for r in timeline],
            'hours': [{'hr': r['hr'], 'cnt': r['cnt']} for r in hours],
            'last_24h': [{'hr_key': r['hr_key'], 'cnt': r['cnt']} for r in last_24h],
            'last_7d': [{'day': r['day'], 'cnt': r['cnt']} for r in last_7d],
            'active_days': active_days,
            'avg_per_day': avg_per_day,
            'space': {
                'total_bytes': total_bytes,
                'breakdown': breakdown
            }
        })
    finally:
        conn.close()

@app.route('/api/duplicates')
def api_duplicates():
    conn = get_db()
    try:
        rows = conn.execute("""
            SELECT CRC, COUNT(*) as cnt, MIN(lID) as first_id, MAX(lDate) as last_date
            FROM Main WHERE bIsGroup=0 AND CRC != 0
            GROUP BY CRC HAVING cnt > 1
            ORDER BY cnt DESC, last_date DESC
            LIMIT 100
        """).fetchall()

        groups = []
        for r in rows:
            ids = conn.execute(
                "SELECT lID, lDate, mText FROM Main WHERE CRC=? AND bIsGroup=0 ORDER BY lDate DESC",
                (r['CRC'],)
            ).fetchall()
            text, ctype = get_best_text(ids[0]['lID'], ids[0]['mText'], conn)
            groups.append({
                'crc': r['CRC'],
                'count': r['cnt'],
                'type': ctype,
                'preview': text[:150],
                'ids': [i['lID'] for i in ids],
                'dates': [i['lDate'] for i in ids],
            })

        return jsonify({'groups': groups, 'total_duplicate_groups': len(groups)})
    finally:
        conn.close()


@app.route('/api/cleanup/preview', methods=['POST'])
def api_cleanup_preview():
    data = request.json
    rules = data.get('rules', [])
    if not rules:
        return jsonify({'error': 'No rules provided'}), 400

    import time
    now_ts = int(time.time())

    # cleanup still uses broad format mapping (text/image/file), does not affect display logic
    TYPE_FORMAT_MAP = {
        'text':  ('CF_UNICODETEXT', 'CF_TEXT'),
        'image': ('PNG', 'CF_DIB'),
        'file':  ('CF_HDROP',),
    }

    conn = get_db()
    try:
        total_count = 0
        total_bytes = 0
        by_type = []

        for rule in rules:
            rtype = rule.get('type')
            days = int(rule.get('days', 100))
            size_kb = float(rule.get('size_kb', 0))
            size_bytes = int(size_kb * 1024)
            cutoff_ts = now_ts - days * 86400
            fmts = TYPE_FORMAT_MAP.get(rtype)
            if not fmts:
                continue

            fmt_placeholders = ','.join('?' * len(fmts))

            candidate_rows = conn.execute(f"""
                SELECT DISTINCT m.lID
                FROM Main m
                WHERE m.bIsGroup = 0
                  AND m.lDontAutoDelete = 0
                  AND m.lDate < ?
                  AND m.lID IN (
                      SELECT lParentID FROM Data
                      WHERE strClipBoardFormat IN ({fmt_placeholders})
                  )
            """, [cutoff_ts] + list(fmts)).fetchall()

            count = 0
            bytes_sum = 0
            for row in candidate_rows:
                lid = row[0]
                b = conn.execute("""
                    SELECT COALESCE(SUM(LENGTH(ooData)), 0)
                    FROM Data WHERE lParentID = ?
                """, (lid,)).fetchone()[0]
                if b >= size_bytes:
                    count += 1
                    bytes_sum += b

            total_count += count
            total_bytes += bytes_sum
            by_type.append({'type': rtype, 'count': count, 'bytes': bytes_sum})

        return jsonify({
            'total_count': total_count,
            'total_bytes': total_bytes,
            'by_type': by_type,
        })
    finally:
        conn.close()


@app.route('/api/cleanup/run', methods=['POST'])
def api_cleanup_run():
    data = request.json
    rules = data.get('rules', [])
    if not rules:
        return jsonify({'error': 'No rules provided'}), 400

    import time
    now_ts = int(time.time())

    TYPE_FORMAT_MAP = {
        'text':  ('CF_UNICODETEXT', 'CF_TEXT'),
        'image': ('PNG', 'CF_DIB'),
        'file':  ('CF_HDROP',),
    }

    conn = get_db_rw()
    try:
        conn.execute("BEGIN")

        deleted_count = 0
        freed_bytes = 0
        by_type = []
        ids_to_delete = []

        for rule in rules:
            rtype = rule.get('type')
            days = int(rule.get('days', 100))
            size_kb = float(rule.get('size_kb', 0))
            size_bytes = int(size_kb * 1024)
            cutoff_ts = now_ts - days * 86400
            fmts = TYPE_FORMAT_MAP.get(rtype)
            if not fmts:
                continue

            fmt_placeholders = ','.join('?' * len(fmts))

            candidate_rows = conn.execute(f"""
                SELECT DISTINCT m.lID
                FROM Main m
                WHERE m.bIsGroup = 0
                  AND m.lDontAutoDelete = 0
                  AND m.lDate < ?
                  AND m.lID IN (
                      SELECT lParentID FROM Data
                      WHERE strClipBoardFormat IN ({fmt_placeholders})
                  )
            """, [cutoff_ts] + list(fmts)).fetchall()

            count = 0
            bytes_sum = 0
            type_ids = []
            for row in candidate_rows:
                lid = row[0]
                b = conn.execute("""
                    SELECT COALESCE(SUM(LENGTH(ooData)), 0)
                    FROM Data WHERE lParentID = ?
                """, (lid,)).fetchone()[0]
                if b >= size_bytes:
                    type_ids.append(lid)
                    count += 1
                    bytes_sum += b

            ids_to_delete.extend(type_ids)
            deleted_count += count
            freed_bytes += bytes_sum
            by_type.append({'type': rtype, 'count': count, 'bytes': bytes_sum})

        ids_to_delete = list(set(ids_to_delete))

        batch = 200
        for i in range(0, len(ids_to_delete), batch):
            chunk = ids_to_delete[i:i+batch]
            placeholders = ','.join('?' * len(chunk))
            conn.execute(f"DELETE FROM Data WHERE lParentID IN ({placeholders})", chunk)
            conn.execute(f"DELETE FROM Main WHERE lID IN ({placeholders})", chunk)

        conn.execute("COMMIT")

        remaining = conn.execute("SELECT COUNT(*) FROM Main WHERE bIsGroup=0").fetchone()[0]

        return jsonify({
            'deleted_count': deleted_count,
            'freed_bytes': freed_bytes,
            'remaining_count': remaining,
            'by_type': by_type,
        })
    except Exception as e:
        conn.execute("ROLLBACK")
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/api/timeline')
def api_timeline():
    conn = get_db()
    try:
        rows = conn.execute("""
            SELECT date(lDate,'unixepoch','localtime') as day, COUNT(*) as cnt
            FROM Main WHERE bIsGroup=0
            GROUP BY day ORDER BY day DESC
            LIMIT 365
        """).fetchall()
        return jsonify({'days': [{'day': r['day'], 'cnt': r['cnt']} for r in rows]})
    finally:
        conn.close()


@app.route('/api/search/suggest')
def api_suggest():
    q = request.args.get('q', '').strip()
    if len(q) < 2:
        return jsonify({'suggestions': []})
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT mText FROM Main WHERE bIsGroup=0 AND mText LIKE ? AND mText != 'CF_DIB' "
            "ORDER BY lDate DESC LIMIT 8",
            (f'%{q}%',)
        ).fetchall()
        return jsonify({'suggestions': [r['mText'][:80] for r in rows]})
    finally:
        conn.close()


@app.route('/api/db/info')
def api_db_info():
    db_path = config.db_path
    try:
        stat = os.stat(db_path)
        return jsonify({
            'path': db_path,
            'size_bytes': stat.st_size,
            'size_mb': round(stat.st_size / 1024 / 1024, 2),
            'modified': datetime.fromtimestamp(stat.st_mtime).isoformat(),
        })
    except Exception as e:
        return jsonify({'error': str(e), 'path': db_path}), 500


@app.route('/api/config', methods=['GET'])
def get_config():
    return jsonify(config.settings)


@app.route('/api/config/path', methods=['POST'])
def add_db_path():
    data = request.json
    path = data.get('path', '').strip()
    if not path:
        return jsonify({'error': 'Path is required'}), 400

    path = os.path.normpath(path).replace('\\', '/')

    if path not in config.settings['db_paths']:
        config.settings['db_paths'].append(path)

    config.settings['current_path'] = path
    config.save()
    return jsonify({'success': True, 'settings': config.settings})


@app.route('/api/config/switch', methods=['POST'])
def switch_db_path():
    data = request.json
    path = data.get('path', '').strip()
    if path not in config.settings['db_paths']:
        return jsonify({'error': 'Path not found in list'}), 404

    config.settings['current_path'] = path
    config.save()
    return jsonify({'success': True, 'settings': config.settings})


@app.route('/api/config/path', methods=['DELETE'])
def remove_db_path():
    path = request.args.get('path', '').strip()
    if path in config.settings['db_paths']:
        if len(config.settings['db_paths']) <= 1:
            return jsonify({'error': 'Cannot remove the last path'}), 400

        config.settings['db_paths'].remove(path)
        if config.settings['current_path'] == path:
            config.settings['current_path'] = config.settings['db_paths'][0]
        config.save()
        return jsonify({'success': True, 'settings': config.settings})
    return jsonify({'error': 'Path not found'}), 404


def create_app():
    return app


if __name__ == '__main__':
    app.run(debug=True, port=53980)