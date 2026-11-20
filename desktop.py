"""Local desktop entry. Opens an application window, not a LAN service."""
import os
import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

if getattr(sys,'frozen',False):
    os.environ.setdefault('SUIJIAN_DATA',str(Path(sys.executable).parent/'user-data'))
    Path(os.environ['SUIJIAN_DATA']).mkdir(exist_ok=True)
    sys.stdout=open(Path(os.environ['SUIJIAN_DATA'])/'desktop.log','a',encoding='utf8',buffering=1)
    sys.stderr=sys.stdout
os.environ['SUIJIAN_CODEX_DEFAULT']='1'
from server.app import app,SESSION
import uvicorn

@app.get('/desktop-entry')
def desktop_entry():
    from fastapi.responses import RedirectResponse
    response=RedirectResponse('/')
    response.set_cookie('suijian_session',SESSION,httponly=True,samesite='strict',max_age=86400*30)
    return response

# StaticFiles is a catch-all mount, so the desktop entry must precede it.
app.router.routes.insert(0, app.router.routes.pop())

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, new_url):
        return None


def is_suijian_running(url):
    """Identify a local studio without following redirects or using a proxy."""
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),_NoRedirect())
    request=urllib.request.Request(url+'/healthz',headers={'Accept':'application/json'})
    try:
        with opener.open(request,timeout=1) as response:
            raw=response.read(4097)
            if response.status!=200 or len(raw)>4096:
                return False
            payload=json.loads(raw)
        return isinstance(payload,dict) and payload.get('app')=='suijian-studio'
    except urllib.error.HTTPError as exc:
        exc.close()
        return False
    except (OSError,ValueError):
        return False


def port_in_use(port):
    try:
        with socket.create_connection(('127.0.0.1',port),timeout=.4):
            return True
    except OSError:
        return False


def open_window(url):
    import subprocess
    candidates=[Path('C:/Program Files/Google/Chrome/Application/chrome.exe'),
                Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'),
                Path('C:/Program Files/Microsoft/Edge/Application/msedge.exe')]
    browser=next((p for p in candidates if p.exists()),None)
    if browser:
        subprocess.Popen([str(browser),'--app='+url+'/desktop-entry','--window-size=1480,980'])
    else:
        webbrowser.open(url+'/desktop-entry')


def open_when_ready(url):
    # Wait for the real endpoint instead of showing an empty browser on slow machines.
    deadline=time.monotonic()+20
    while time.monotonic()<deadline:
        if is_suijian_running(url):
            open_window(url)
            return
        time.sleep(.15)
    print('随剪启动等待超时。请查看服务窗口或 desktop.log；服务尚未准备就绪。',file=sys.stderr)


def main():
    try:
        port=int(os.environ.get('SUIJIAN_PORT','8876'))
        if not 1<=port<=65535:
            raise ValueError
    except ValueError as exc:
        raise RuntimeError('SUIJIAN_PORT 必须是 1—65535 之间的整数，例如 8876。') from exc
    url=f'http://127.0.0.1:{port}'
    if port_in_use(port):
        if is_suijian_running(url):
            open_window(url)
            return
        raise RuntimeError(f'端口 {port} 已被其它程序占用，未打开该页面。请关闭占用程序，或设置 SUIJIAN_PORT 为其它空闲端口后重试。')
    threading.Thread(target=open_when_ready,args=(url,),daemon=True).start()
    uvicorn.run(app,host='127.0.0.1',port=port,access_log=False,log_config=None)

if __name__=='__main__':
    try:
        main()
    except RuntimeError as exc:
        print(str(exc),file=sys.stderr)
        if os.name=='nt' and getattr(sys,'frozen',False):
            import ctypes
            ctypes.windll.user32.MessageBoxW(None,str(exc),'随剪 Studio · 无法启动',0x10)
        raise SystemExit(1)
