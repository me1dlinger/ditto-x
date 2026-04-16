import sys
import os
import socket
import ctypes
import threading
import time
import webbrowser
import urllib.request

from pystray import Icon, Menu, MenuItem
from PIL import Image, ImageDraw

from server import app, get_resource_path


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
        icon_path = get_resource_path('static/icon/ditto-x.ico')
        if not os.path.exists(icon_path):
             icon_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'static/icon', 'ditto-x.ico'))

        if os.path.exists(icon_path):
            img = Image.open(icon_path)
        else:
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
        
        if ready:
            icon.notify("DittoX 已启动", "服务已运行在 http://127.0.0.1:53980/")
            
        icon.run()
    except Exception:
        while True:
            time.sleep(60)
