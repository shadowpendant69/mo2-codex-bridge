"""Qt-native HTTP listener. It stays responsive in MO2's installer event loops."""
import hmac
import json

from PyQt6.QtCore import QTimer
from PyQt6.QtNetwork import QHostAddress, QTcpServer


class LocalTransport:
    def __init__(self, owner, parent):
        self.owner = owner
        self.server = QTcpServer(parent)
        self.connections = {}
        self.server.newConnection.connect(self.accept)
        if not self.server.listen(QHostAddress('127.0.0.1'), 0):
            raise RuntimeError(self.server.errorString())
        self.server_port = self.server.serverPort()

    def accept(self):
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            self.connections[socket] = bytearray()
            socket.readyRead.connect(lambda s=socket: self.receive(s))
            socket.disconnected.connect(lambda s=socket: self.close_socket(s))
            QTimer.singleShot(5000, lambda s=socket: self.expire(s))

    def expire(self, socket):
        if socket in self.connections:
            socket.abort()

    def close_socket(self, socket):
        self.connections.pop(socket, None)
        socket.deleteLater()

    def respond(self, socket, code, value):
        body = json.dumps(value, ensure_ascii=False).encode('utf-8')
        title = {200: 'OK', 202: 'Accepted', 400: 'Bad Request', 403: 'Forbidden', 404: 'Not Found'}.get(code, 'Error')
        header = (f'HTTP/1.1 {code} {title}\r\nContent-Type: application/json; charset=utf-8\r\n'
                  f'Content-Length: {len(body)}\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n').encode('ascii')
        socket.write(header + body)
        socket.disconnectFromHost()

    def receive(self, socket):
        buffer = self.connections.get(socket)
        if buffer is None:
            return
        buffer.extend(bytes(socket.readAll()))
        if len(buffer) > 2 * 1024 * 1024 + 16384:
            return self.respond(socket, 400, {'error': 'Request too large'})
        end = buffer.find(b'\r\n\r\n')
        if end == -1:
            if len(buffer) > 16384:
                return self.respond(socket, 400, {'error': 'Headers too large'})
            return
        try:
            rows = bytes(buffer[:end]).decode('iso-8859-1').split('\r\n')
            method, route, protocol = rows[0].split(' ')
            headers = {}
            for row in rows[1:]:
                key, value = row.split(':', 1)
                key = key.strip().lower()
                if key in headers:
                    raise ValueError('Duplicate headers are not accepted')
                headers[key] = value.strip()
            if (headers.get('host') != '127.0.0.1:' + str(self.server_port)
                    or 'origin' in headers
                    or not hmac.compare_digest(headers.get('authorization', ''), 'Bearer ' + self.owner.token)):
                return self.respond(socket, 403, {'error': 'Unauthorized local request'})
            if 'transfer-encoding' in headers:
                raise ValueError('Chunked requests are not accepted')
            size = int(headers.get('content-length', '0'))
            if not 0 <= size <= 2 * 1024 * 1024:
                raise ValueError('Request too large')
            if len(buffer) < end + 4 + size:
                return
            if method == 'GET' and route.startswith('/jobs/'):
                result = self.owner.get_job(route[len('/jobs/'):])
                return self.respond(socket, 200 if result else 404, result or {'error': 'Unknown job ID'})
            if method != 'POST' or route != '/rpc' or headers.get('content-type', '').split(';')[0] != 'application/json':
                raise ValueError('Use the JSON RPC endpoint')
            request = json.loads(bytes(buffer[end + 4:end + 4 + size]))
            result = self.owner.enqueue(request)
            return self.respond(socket, 202, result)
        except Exception as error:
            self.respond(socket, 400, {'error': str(error)})

    def close(self):
        self.server.close()
        for socket in list(self.connections):
            socket.abort()
