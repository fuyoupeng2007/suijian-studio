"""Launch a local development backend. No public hosting or third-party calls by default."""
import socket
import uvicorn
from .app import PAIR


def main():
    print('\n随剪 Suijian · 手机与电脑连接同一 Wi-Fi\n')
    try:
        addresses = sorted({x[4][0] for x in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)})
        for address in addresses:
            print(f'手机服务地址：http://{address}:8765')
    except OSError:
        pass
    print('电脑预览：http://127.0.0.1:8765')
    print('配对码：' + PAIR + '\n')
    uvicorn.run('server.app:app', host='0.0.0.0', port=8765, access_log=False)


if __name__ == '__main__':
    main()
