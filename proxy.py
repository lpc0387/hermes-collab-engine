#!/usr/bin/env python3
from __future__ import annotations
import json, os, sys, urllib.request, urllib.error, signal
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

TARGET = os.environ.get('PROXY_TARGET', 'https://opencode.ai/zen/go/v1')
API_KEY = os.environ.get('PROXY_API_KEY', '')
CONFIG_PATH = Path(os.environ.get('PROXY_CONFIG',
                   str(Path(__file__).resolve().parent / '.runtime-config.json')))

MODEL_MAP = {
    'claude-sonnet-4-20250514':   'deepseek-v4-flash',
    'claude-sonnet-4':            'deepseek-v4-flash',
    'claude-opus-4-20250514':     'deepseek-v4-pro',
    'claude-opus-4':              'deepseek-v4-pro',
    'gpt-4o':                     'deepseek-v4-flash',
    'gpt-4o-mini':                'deepseek-v4-flash',
    'claude-3-haiku-20240307':    'deepseek-v4-flash',
    'claude-3-sonnet-20240229':   'deepseek-v4-flash',
    'claude-3-opus-20240229':     'deepseek-v4-pro',
}


def _load_api_key() -> str:
    if API_KEY:
        return API_KEY
    try:
        cfg = json.loads(CONFIG_PATH.read_text())
        return cfg.get('leader', {}).get('api_key', '') or cfg.get('api_key', '')
    except Exception:
        return ''


class ProxyHandler(BaseHTTPRequestHandler):
    api_key = ''

    def _model_rewrite(self, body: dict) -> dict:
        model = body.get('model', '')
        target = MODEL_MAP.get(model)
        if target:
            body['model'] = target
        return body

    def _forward(self, target_path: str) -> None:
        length = int(self.headers.get('Content-Length', 0))
        raw = self.rfile.read(length) if length else b''
        try:
            body = json.loads(raw) if raw else {}
            body = self._model_rewrite(body)
            raw = json.dumps(body).encode()
        except json.JSONDecodeError:
            pass
        target_base = TARGET.rstrip('/')
        if target_path.startswith('/v1') and target_base.endswith('/v1'):
            target_path = target_path[3:]
        url = f'{target_base}{target_path}'
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {self.api_key}',
            'x-api-key': self.api_key,
            'anthropic-version': '2023-06-01',
            'User-Agent': 'Proxy/1.0',
        }
        req = urllib.request.Request(url, data=raw, headers=headers, method=self.command)
        try:
            resp = urllib.request.urlopen(req, timeout=120)
            self.send_response(resp.status)
            for k, v in resp.headers.items():
                if k.lower() not in ('transfer-encoding', 'content-encoding', 'content-length'):
                    self.send_header(k, v)
            self.send_header('Content-Length', resp.length or 0)
            self.end_headers()
            self.wfile.write(resp.read())
        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(e.read())

    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({
            'proxy': 'opencode-go adapter',
            'target': TARGET,
            'model_map': MODEL_MAP,
        }).encode())

    def do_POST(self):
        if self.path in ('/v1/chat/completions', '/chat/completions'):
            self._forward('/v1/chat/completions')
        elif self.path in ('/v1/messages', '/messages'):
            self._forward('/v1/messages')
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):
        sys.stderr.write(f'[proxy] {args[0]} {args[1]} {args[2]}\n')


def main():
    port = int(os.environ.get('PROXY_PORT', '9876'))
    ProxyHandler.api_key = _load_api_key()
    server = HTTPServer(('127.0.0.1', port), ProxyHandler)
    print(f'[proxy] :{port} → {TARGET}  (key={ProxyHandler.api_key[:8]}...)',
          file=sys.stderr)
    print(f'[proxy] model map: {json.dumps(MODEL_MAP, indent=2)}', file=sys.stderr)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n[proxy] stopped', file=sys.stderr)
        sys.exit(0)


if __name__ == '__main__':
    main()
