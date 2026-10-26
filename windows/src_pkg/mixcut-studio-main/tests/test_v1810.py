"""Local launcher transport never initializes an HTTPS context or consults proxies."""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import unittest
from unittest.mock import patch
import maclaunch

class LauncherTransportTests(unittest.TestCase):
    def setUp(self):
        self.requests=[];owner=self
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                owner.requests.append(('GET',self.path))
                self.send_response(200);self.end_headers()
                self.wfile.write(json.dumps({'version':'1.8.16','api_protocol':17,'ffmpeg_available':True,'batches':[]}).encode())
            def do_POST(self):
                owner.requests.append(('POST',self.path,self.rfile.read(int(self.headers['Content-Length']))))
                self.send_response(200);self.end_headers();self.wfile.write(b'{"ok":true}')
            def log_message(self,*args):pass
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);self.port=self.server.server_port
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)

    def test_readiness_with_unavailable_ca_bundle_and_dead_system_proxies(self):
        with patch('ssl.create_default_context',side_effect=FileNotFoundError('missing CA bundle')),patch('urllib.request.getproxies',side_effect=AssertionError('must not inspect proxies')):
            data=maclaunch._readiness(self.port)
        self.assertEqual('1.8.16',data['version']);self.assertEqual([('GET','/api/bootstrap')],self.requests)

    def test_shutdown_post_uses_direct_transport(self):
        with patch('ssl.create_default_context',side_effect=FileNotFoundError('missing CA bundle')):
            self.assertEqual({'ok':True},maclaunch._local_json('/api/shutdown',port=self.port,data=b'{}'))
        self.assertEqual([('POST','/api/shutdown',b'{}')],self.requests)

    def test_failed_readiness_does_not_raise(self):
        for error in [OSError('refused'),http.client.RemoteDisconnected('closed'),ValueError('invalid json')]:
            with patch.object(maclaunch,'_local_json',side_effect=error):self.assertIsNone(maclaunch._readiness(self.port))

    def test_transport_always_closes_connection(self):
        with patch('maclaunch.http.client.HTTPConnection') as constructor:
            connection=constructor.return_value;connection.request.side_effect=OSError('refused')
            with self.assertRaises(OSError):maclaunch._local_json('/api/bootstrap',port=self.port)
            connection.close.assert_called_once()
