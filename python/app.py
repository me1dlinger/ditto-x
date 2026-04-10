import sqlite3
import os
import re
import struct
import json
from datetime import datetime, timezone
from flask import Flask, jsonify, request, render_template, Response
import threading, time, webbrowser, urllib.request, socket, ctypes

import sys

# ─── Flask Setup ─────────────────────────────────────────────────────────────

def get_resource_path(relative_path):
    """ Get absolute path to resource, works for dev and for PyInstaller """
    try:
        # PyInstaller creates a temp folder and stores path in _MEIPASS
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)

# Handle template folder for PyInstaller
template_dir = get_resource_path('python/templates')
if not os.path.exists(template_dir):
    template_dir = get_resource_path('templates')

app = Flask(__name__, template_folder=template_dir)
app.config['JSON_SORT_KEYS'] = False

# ─── Configuration ───────────────────────────────────────────────────────────

def get_settings_path():
    """Find settings.json beside the executable or script."""
    if getattr(sys, 'frozen', False):
        # We are running in a bundle
        base_dir = os.path.dirname(sys.executable)
    else:
        # We are running in a normal Python environment
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

# ─── DB helpers ──────────────────────────────────────────────────────────────

def get_db():
    """Open ditto.db read-only using URI mode."""
    db_path = config.db_path
    if not os.path.exists(db_path):
        # Fallback to check if it's a relative path
        alt_path = os.path.join(os.path.dirname(__file__), db_path)
        if os.path.exists(alt_path):
            db_path = alt_path
            
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA cache_size = -8000")   # 8 MB page cache
    conn.execute("PRAGMA temp_store = MEMORY")
    return conn


def row_to_dict(row):
    return dict(row)


# ─── Clip-type detection ──────────────────────────────────────────────────────

FORMAT_PRIORITY = [
    'CF_UNICODETEXT', 'CF_TEXT', 'HTML Format',
    'Rich Text Format', 'CF_HDROP', 'PNG', 'CF_DIB',
]

def detect_clip_type(mtext: str, formats: list[str]) -> str:
    """Return a simple type tag."""
    if (mtext == 'CF_DIB'  and 'CF_TEXT' not in formats) or 'PNG' in formats or 'CF_DIB' in formats:
        if 'PNG' in formats:
            return 'image'
        return 'image'
    if 'CF_HDROP' in formats:
        return 'file'
    if 'HTML Format' in formats:
        return 'html'
    if 'Rich Text Format' in formats:
        return 'rtf'
    return 'text'


def decode_cf_text(blob: bytes) -> str:
    """Try to decode CF_TEXT blob (cp1252 / gbk fallback)."""
    if not blob:
        return ''
    # Remove null terminator
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
        # DROPFILES struct: size(4) pt(8) fNC(4) fWide(4) then filenames
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
    Reads Data rows ordered by format priority.
    """
    rows = conn.execute(
        "SELECT strClipBoardFormat, ooData FROM Data WHERE lParentID=? ORDER BY lID",
        (lID,)
    ).fetchall()

    formats = [r['strClipBoardFormat'] for r in rows]
    clip_type = detect_clip_type(mtext, formats)

    if clip_type == 'image':
        return '[图片]', 'image'

    if clip_type == 'file':
        for r in rows:
            if r['strClipBoardFormat'] == 'CF_HDROP' and r['ooData']:
                paths = parse_hdrop(bytes(r['ooData']))
                return '\n'.join(paths) if paths else '[文件]', 'file'
        return '[文件]', 'file'

    # For text types, prefer UNICODETEXT > mText > CF_TEXT
    for fmt in ('CF_UNICODETEXT', 'HTML Format', 'Rich Text Format', 'CF_TEXT'):
        for r in rows:
            if r['strClipBoardFormat'] == fmt and r['ooData']:
                blob = bytes(r['ooData'])
                if fmt == 'CF_UNICODETEXT':
                    try:
                        # Strip null-word terminator (2 bytes) without corrupting UTF-16 pairs
                        b = blob
                        while b.endswith(b'\x00\x00'):
                            b = b[:-2]
                        text = b.decode('utf-16-le', errors='replace')
                        return text, clip_type
                    except Exception:
                        pass
                elif fmt == 'HTML Format':
                    try:
                        raw = blob.decode('utf-8', errors='replace')
                        # strip HTML markup for preview
                        clean = re.sub(r'<[^>]+>', '', raw)
                        clean = re.sub(r'\s+', ' ', clean).strip()
                        return clean, 'html'
                    except Exception:
                        pass
                elif fmt == 'Rich Text Format':
                    try:
                        raw = blob.decode('ascii', errors='replace')
                        # Strip RTF tags
                        clean = re.sub(r'\\[a-z]+\d*\s?', '', raw)
                        clean = re.sub(r'[{}]', '', clean).strip()
                        return clean[:500], 'rtf'
                    except Exception:
                        pass
                elif fmt == 'CF_TEXT':
                    return decode_cf_text(blob), clip_type

    # Fallback to mtext
    if mtext and mtext != 'CF_DIB':
        return mtext, clip_type
    return '[无文本]', clip_type


def get_image_data(lID: int, conn) -> tuple[bytes | None, str]:
    """Return image bytes and mimetype."""
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
        # Prepend BMP file header
        try:
            # BITMAPINFOHEADER size (4 bytes)
            header_size = struct.unpack('<I', dib_data[:4])[0]
            # BitCount at offset 14 (2 bytes)
            bit_count = struct.unpack('<H', dib_data[14:16])[0]
            # Compression at offset 16 (4 bytes)
            compression = struct.unpack('<I', dib_data[16:20])[0]
            # ClrUsed at offset 32 (4 bytes)
            clr_used = struct.unpack('<I', dib_data[32:36])[0]
            
            if clr_used == 0 and bit_count <= 8:
                clr_used = 1 << bit_count
                
            pixel_offset = header_size + (clr_used * 4)
            # If BI_BITFIELDS (3) and BITMAPINFOHEADER (40), masks follow header (12 bytes)
            # For V4(108) and V5(124), masks are part of the header itself.
            if compression == 3 and header_size == 40:
                pixel_offset += 12
                
            # If 32-bit, force alpha to 255 to avoid transparency issues (black images)
            if bit_count == 32:
                dib_mut = bytearray(dib_data)
                # Ensure we don't go out of bounds if data is truncated
                for i in range(pixel_offset + 3, len(dib_mut), 4):
                    dib_mut[i] = 255
                dib_data = bytes(dib_mut)

            file_size = 14 + len(dib_data)
            offset = 14 + pixel_offset
            
            # BITMAPFILEHEADER (14 bytes): 'BM', size, 0, 0, offset
            bmp_header = struct.pack('<2sIHHI', b'BM', file_size, 0, 0, offset)
            return bmp_header + dib_data, 'image/bmp'
        except Exception:
            return dib_data, 'image/bmp'
            
    return None, 'image/png'


def get_html_data(lID: int, conn) -> str | None:
    """Return raw HTML string for an html clip."""
    row = conn.execute(
        "SELECT ooData FROM Data WHERE lParentID=? AND strClipBoardFormat='HTML Format'",
        (lID,)
    ).fetchone()
    if row and row['ooData']:
        return bytes(row['ooData']).decode('utf-8', errors='replace')
    return None


# ─── Routes ──────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/clips')
def api_clips():
    """
    Query params:
      page, page_size
      q          – full-text search on mText
      type       – text|image|file|html|rtf
      date_from  – unix timestamp
      date_to    – unix timestamp
      sort       – date_desc (default) | date_asc | alpha
      pinned     – 1 = only lDontAutoDelete=1
    """
    page       = max(1, int(request.args.get('page', 1)))
    page_size  = min(200, max(10, int(request.args.get('page_size', 50))))
    q          = request.args.get('q', '').strip()
    ftype      = request.args.get('type', '')
    date_from  = request.args.get('date_from', '')
    date_to    = request.args.get('date_to', '')
    sort       = request.args.get('sort', 'date_desc')
    pinned     = request.args.get('pinned', '')

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

    # type filter uses subquery on Data formats
    type_where = ''
    if ftype == 'image':
        type_where = "AND ((mText='CF_DIB' and lID IN (SELECT lParentID FROM Data WHERE strClipBoardFormat IN ('PNG','CF_DIB'))))"
    elif ftype == 'file':
        type_where = "AND lID IN (SELECT lParentID FROM Data WHERE strClipBoardFormat='CF_HDROP' or strClipBoardFormat='CF_DIB')"
    elif ftype == 'html':
        type_where = "AND lID IN (SELECT lParentID FROM Data WHERE strClipBoardFormat='HTML Format')"
    elif ftype == 'rtf':
        type_where = "AND lID IN (SELECT lParentID FROM Data WHERE strClipBoardFormat='Rich Text Format')"
    elif ftype == 'text':
        type_where = ("AND mText != 'CF_DIB' "
                      "AND lID NOT IN (SELECT lParentID FROM Data WHERE strClipBoardFormat IN ('CF_HDROP','PNG'))")

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
                'id':      r['lID'],
                'date':    r['lDate'],
                'date_iso': dt,
                'text':    text[:300],   # truncate for list view
                'type':    ctype,
                'pinned':  bool(r['lDontAutoDelete']),
                'shortcut': r['lShortCut'] or r['globalShortCut'],
                'crc':     r['CRC'],
            })

        return jsonify({
            'total': total,
            'page':  page,
            'page_size': page_size,
            'clips': clips,
        })
    finally:
        conn.close()


@app.route('/api/clip/<int:clip_id>')
def api_clip_detail(clip_id):
    """Full detail for a single clip."""
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
        if ctype == 'html':
            html_raw = get_html_data(clip_id, conn)

        return jsonify({
            'id':        clip_id,
            'date':      row['lDate'],
            'date_iso':  datetime.fromtimestamp(row['lDate'], tz=timezone.utc).isoformat(),
            'text':      text,
            'type':      ctype,
            'pinned':    bool(row['lDontAutoDelete']),
            'shortcut':  row['lShortCut'],
            'formats':   [{'id': f['lID'], 'format': f['strClipBoardFormat']} for f in formats],
            'html_raw':  html_raw,
            'mText':     row['mText'],
        })
    finally:
        conn.close()


@app.route('/api/clip/<int:clip_id>/image')
def api_clip_image(clip_id):
    """Stream image data for image clips."""
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
    """Dashboard statistics."""
    conn = get_db()
    try:
        total = conn.execute("SELECT COUNT(*) FROM Main WHERE bIsGroup=0").fetchone()[0]
        pinned = conn.execute("SELECT COUNT(*) FROM Main WHERE bIsGroup=0 AND lDontAutoDelete=1").fetchone()[0]
        images = conn.execute(
            "SELECT COUNT(DISTINCT lParentID) FROM Data WHERE strClipBoardFormat IN ('PNG','CF_DIB')"
        ).fetchone()[0]
        files = conn.execute(
            "SELECT COUNT(DISTINCT lParentID) FROM Data WHERE strClipBoardFormat='CF_HDROP'"
        ).fetchone()[0]
        html_c = conn.execute(
            "SELECT COUNT(DISTINCT lParentID) FROM Data WHERE strClipBoardFormat='HTML Format'"
        ).fetchone()[0]

        # Timeline: clips per day (last 30 days, using localtime)
        timeline = conn.execute("""
            SELECT date(lDate,'unixepoch','localtime') as day, COUNT(*) as cnt
            FROM Main WHERE bIsGroup=0
              AND lDate >= strftime('%s','now','-30 days')
            GROUP BY day ORDER BY day
        """).fetchall()

        oldest = conn.execute("SELECT MIN(lDate) FROM Main WHERE bIsGroup=0").fetchone()[0]
        newest = conn.execute("SELECT MAX(lDate) FROM Main WHERE bIsGroup=0").fetchone()[0]

        # Top hours (all time, using localtime)
        hours = conn.execute("""
            SELECT strftime('%H', lDate, 'unixepoch', 'localtime') as hr, COUNT(*) as cnt
            FROM Main WHERE bIsGroup=0
            GROUP BY hr ORDER BY hr
        """).fetchall()

        # Last 24 hours distribution (rolling 24h window, using localtime)
        last_24h = conn.execute("""
            SELECT strftime('%Y-%m-%d %H', lDate, 'unixepoch', 'localtime') as hr_key, COUNT(*) as cnt
            FROM Main WHERE bIsGroup = 0 AND lDate >= strftime('%s','now','-24 hours')
            GROUP BY hr_key ORDER BY hr_key
        """).fetchall()

        # Last 7 days distribution (using localtime)
        last_7d = conn.execute("""
            SELECT date(lDate,'unixepoch','localtime') as day, COUNT(*) as cnt
            FROM Main WHERE bIsGroup=0 AND lDate >= strftime('%s','now','-7 days')
            GROUP BY day ORDER BY day
        """).fetchall()

        # Some extra stats
        active_days_row = conn.execute("""
            SELECT COUNT(DISTINCT date(lDate, 'unixepoch', 'localtime')) FROM Main WHERE bIsGroup=0
        """).fetchone()
        active_days = active_days_row[0] if active_days_row else 1
        
        days_diff = max(1, (newest - oldest) / 86400) if newest and oldest else 1
        avg_per_day = round(total / days_diff, 1)

        return jsonify({
            'total': total,
            'pinned': pinned,
            'images': images,
            'files': files,
            'html': html_c,
            'text': total - images - files - html_c,
            'oldest': oldest,
            'newest': newest,
            'timeline': [{'day': r['day'], 'cnt': r['cnt']} for r in timeline],
            'hours': [{'hr': r['hr'], 'cnt': r['cnt']} for r in hours],
            'last_24h': [{'hr_key': r['hr_key'], 'cnt': r['cnt']} for r in last_24h],
            'last_7d': [{'day': r['day'], 'cnt': r['cnt']} for r in last_7d],
            'active_days': active_days,
            'avg_per_day': avg_per_day,
        })
    finally:
        conn.close()


@app.route('/api/duplicates')
def api_duplicates():
    """Find duplicate clips by CRC."""
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
                'crc':   r['CRC'],
                'count': r['cnt'],
                'type':  ctype,
                'preview': text[:150],
                'ids':   [i['lID'] for i in ids],
                'dates': [i['lDate'] for i in ids],
            })

        return jsonify({'groups': groups, 'total_duplicate_groups': len(groups)})
    finally:
        conn.close()


@app.route('/api/timeline')
def api_timeline():
    """Group clips by date for calendar view."""
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
    """Quick suggestions for search autocomplete."""
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
    """Basic DB file info."""
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
    """Get current configuration."""
    return jsonify(config.settings)


@app.route('/api/config/path', methods=['POST'])
def add_db_path():
    """Add a new DB path to settings."""
    data = request.json
    path = data.get('path', '').strip()
    if not path:
        return jsonify({'error': 'Path is required'}), 400
    
    # Normalize path
    path = os.path.normpath(path).replace('\\', '/')
    
    if path not in config.settings['db_paths']:
        config.settings['db_paths'].append(path)
    
    config.settings['current_path'] = path
    config.save()
    return jsonify({'success': True, 'settings': config.settings})


@app.route('/api/config/switch', methods=['POST'])
def switch_db_path():
    """Switch current active DB path."""
    data = request.json
    path = data.get('path', '').strip()
    if path not in config.settings['db_paths']:
        return jsonify({'error': 'Path not found in list'}), 404
    
    config.settings['current_path'] = path
    config.save()
    return jsonify({'success': True, 'settings': config.settings})


@app.route('/api/config/path', methods=['DELETE'])
def remove_db_path():
    """Remove a DB path from list."""
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


if __name__ == '__main__':
    lock_port = 53981
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(('127.0.0.1', lock_port))
        s.listen(1)
    except OSError:
        try:
            ctypes.windll.user32.MessageBoxW(0, "DittoX 已在运行，点击确定打开页面。", "DittoX", 0x00000040 | 0x00001000)
        except Exception:
            pass
        try:
            webbrowser.open('http://127.0.0.1:53980/')
        finally:
            sys.exit(0)

    def run_server():
        app.run(debug=False, port=53980, use_reloader=False)

    t = threading.Thread(target=run_server, daemon=True)
    t.start()

    start = time.time()
    ready = False
    while time.time() - start < 10:
        try:
            urllib.request.urlopen('http://127.0.0.1:53980/api/db/info', timeout=1)
            ready = True
            break
        except Exception:
            time.sleep(0.3)
    try:
        if ready:
            webbrowser.open('http://127.0.0.1:53980/')
    except Exception:
        pass

    try:
        from pystray import Icon, Menu, MenuItem
        from PIL import Image
        
        icon_path = get_resource_path('static/icon/ditto-x.ico')
        if not os.path.exists(icon_path):
             # Fallback for development if assets is in parent dir
             icon_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'static/icon', 'ditto-x.ico'))

        if os.path.exists(icon_path):
            img = Image.open(icon_path)
        else:
            # Fallback to generated image if icon missing
            from PIL import ImageDraw
            img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.rectangle([6, 6, 30, 30], fill=(79, 142, 247, 230))
            d.rectangle([34, 6, 58, 30], fill=(79, 142, 247, 150))
            d.rectangle([6, 34, 30, 58], fill=(79, 142, 247, 150))
            d.rectangle([34, 34, 58, 58], fill=(79, 142, 247, 90))

        def open_browser(icon, item):
            webbrowser.open('http://127.0.0.1:53980/')

        def quit_app(icon, item):
            icon.stop()
            os._exit(0)

        menu = Menu(MenuItem('打开浏览器', open_browser), MenuItem('退出', quit_app))
        icon = Icon('DittoReader', img, 'DittoX', menu)
        
        # Show toast notification
        if ready:
            icon.notify("DittoX 已启动", "服务已运行在 http://127.0.0.1:53980/")
            
        icon.run()
    except Exception:
        while True:
            time.sleep(60)
