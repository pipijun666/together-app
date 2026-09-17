#!/usr/bin/env python3
import hashlib
import json
import os
import subprocess
import unittest
import urllib.request
import urllib.parse
from xml.etree import ElementTree

from tests.ui_system_columns.mock_server import start_mock_server

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
CLIENT_DIR = os.path.join(ROOT_DIR, 'client')

class TestClientAssets(unittest.TestCase):
    def test_avatar_system_svg(self):
        svg_path = os.path.join(CLIENT_DIR, 'avatar-system.svg')
        self.assertTrue(os.path.exists(svg_path), 'avatar-system.svg must exist')
        with open(svg_path, 'rb') as f:
            content = f.read()
        sha256 = hashlib.sha256(content).hexdigest()
        self.assertEqual(sha256, '673ae6fbfdfd7ce6cdbd87b35f62092470687d7701f43df690b396e8320cccb1')
        tree = ElementTree.fromstring(content)
        self.assertEqual(tree.attrib.get('viewBox'), '0 0 512 512')

    def test_style_css_rules(self):
        css_path = os.path.join(CLIENT_DIR, 'style.css')
        with open(css_path, 'r', encoding='utf-8') as f:
            content = f.read()
        self.assertIn('.column-tabs', content)
        self.assertIn('.column-tab', content)
        self.assertIn('.hotspot-banner', content)
        self.assertIn('.badge.system-badge', content)
        self.assertIn('.author-tag', content)

class TestAppContractSyntax(unittest.TestCase):
    def test_js_syntax_check(self):
        app_js = os.path.join(CLIENT_DIR, 'app.js')
        result = subprocess.run(['node', '--check', app_js], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, f'app.js syntax error: {result.stderr}')

    def test_app_js_system_columns_contract(self):
        app_js = os.path.join(CLIENT_DIR, 'app.js')
        with open(app_js, 'r', encoding='utf-8') as f:
            code = f.read()
        self.assertIn('source_kind=', code)
        self.assertIn('avatar-system.svg', code)
        self.assertIn('isSystemShare', code)
        self.assertIn('系统推送', code)
        self.assertIn('从热点中移除', code)
        self.assertIn('spark', code)

class TestMockServerAndAPIContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = 28825
        cls.server = start_mock_server(port=cls.port)
        cls.base_url = f'http://127.0.0.1:{cls.port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_manual_column_filtering(self):
        url = f'{self.base_url}/api/shares?source_kind=manual'
        with urllib.request.urlopen(url) as res:
            self.assertEqual(res.status, 200)
            data = json.loads(res.read().decode('utf-8'))
            shares = data.get('shares', [])
            self.assertTrue(len(shares) > 0)
            for s in shares:
                self.assertNotEqual(s.get('source_kind'), 'system')
                self.assertNotEqual(s.get('author'), 'system')

    def test_system_column_filtering(self):
        url = f'{self.base_url}/api/shares?source_kind=system'
        with urllib.request.urlopen(url) as res:
            self.assertEqual(res.status, 200)
            data = json.loads(res.read().decode('utf-8'))
            shares = data.get('shares', [])
            self.assertTrue(len(shares) >= 3)
            for s in shares:
                self.assertEqual(s.get('source_kind'), 'system')
                self.assertEqual(s.get('author'), 'system')
                self.assertEqual(s.get('author_name'), '系统管理员')
                self.assertIn('metadata', s)
                self.assertTrue('platform' in s['metadata'])

    def test_backward_compatible_unfiltered(self):
        url = f'{self.base_url}/api/shares'
        with urllib.request.urlopen(url) as res:
            self.assertEqual(res.status, 200)
            data = json.loads(res.read().decode('utf-8'))
            shares = data.get('shares', [])
            has_manual = any(s.get('source_kind') == 'manual' for s in shares)
            has_system = any(s.get('source_kind') == 'system' for s in shares)
            self.assertTrue(has_manual)
            self.assertTrue(has_system)

    def test_static_asset_serving(self):
        for path, mime in [
            ('/avatar-system.svg', 'image/svg+xml'),
            ('/app.js', 'javascript'),
            ('/style.css', 'text/css'),
        ]:
            req = urllib.request.Request(f'{self.base_url}{path}')
            with urllib.request.urlopen(req) as res:
                self.assertEqual(res.status, 200)
                self.assertIn(mime, res.headers.get('Content-Type', ''))

    def test_reaction_and_comment_on_system_share(self):
        req = urllib.request.Request(
            f'{self.base_url}/api/shares/sys-hotspot-001/reaction',
            data=json.dumps({'emoji': '❤️'}).encode('utf-8'),
            headers={'Content-Type': 'application/json'},
            method='POST'
        )
        with urllib.request.urlopen(req) as res:
            self.assertEqual(res.status, 200)
            data = json.loads(res.read().decode('utf-8'))
            self.assertTrue(data.get('ok'))

        req = urllib.request.Request(
            f'{self.base_url}/api/shares/sys-hotspot-001/comment',
            data=json.dumps({'body': '测试系统热点评论'}).encode('utf-8'),
            headers={'Content-Type': 'application/json'},
            method='POST'
        )
        with urllib.request.urlopen(req) as res:
            self.assertEqual(res.status, 200)
            data = json.loads(res.read().decode('utf-8'))
            self.assertTrue(data.get('ok'))

    def test_delete_system_share(self):
        req = urllib.request.Request(
            f'{self.base_url}/api/shares/sys-hotspot-003',
            method='DELETE'
        )
        with urllib.request.urlopen(req) as res:
            self.assertEqual(res.status, 200)
            data = json.loads(res.read().decode('utf-8'))
            self.assertTrue(data.get('ok'))

if __name__ == '__main__':
    unittest.main()