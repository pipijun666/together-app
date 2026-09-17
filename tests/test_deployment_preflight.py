import sys
import unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'operations'))
from deployment_preflight import validate


TOKEN='x'*48
PROXY={'PROXY_URL':'http://127.0.0.1:8118'}
PRIMARY={
    'WORKER_TOKEN':TOKEN,'NODE_ROLE':'primary','COOKIE_SECURE':'1',
    'MAX_MEDIA_BYTES':'8589934592','USER_ONE_PASSWORD':'one-password-ok',
    'USER_TWO_PASSWORD':'two-password-ok',
}


class DeploymentPreflightTests(unittest.TestCase):
    def test_worker_accepts_https(self):
        self.assertEqual(validate('worker',{'SERVER_URL':'https://app.example.com','WORKER_TOKEN':TOKEN,**PROXY}),[])

    def test_worker_rejects_retired_wireguard_http(self):
        values={'SERVER_URL':'http://10.77.0.1:18765','ALLOW_INSECURE_HTTP':'1','WORKER_TOKEN':TOKEN}
        self.assertTrue(validate('worker',values))

    def test_worker_accepts_loopback_http_without_flag(self):
        # DB-001：单机 importer 走本机回环，无需 ALLOW_INSECURE_HTTP
        values={'SERVER_URL':'http://127.0.0.1:8765','WORKER_TOKEN':TOKEN,**PROXY}
        self.assertEqual(validate('worker',values),[])

    def test_worker_rejects_public_http(self):
        values={'SERVER_URL':'http://203.0.113.199:8765','ALLOW_INSECURE_HTTP':'1','WORKER_TOKEN':TOKEN}
        self.assertTrue(validate('worker',values))

    def test_primary_accepts_single_machine_template(self):
        # DB-001：单机拓扑不再需要 WG_BIND_IP
        self.assertEqual(validate('primary',PRIMARY),[])
        self.assertTrue(validate('primary',{**PRIMARY,'WORKER_TOKEN':'replace-with-at-least-32-random-characters'}))

    def test_primary_requires_secure_cookie_and_real_passwords(self):
        self.assertTrue(validate('primary',{**PRIMARY,'COOKIE_SECURE':'0'}))
        self.assertTrue(validate('primary',{**PRIMARY,'USER_ONE_PASSWORD':'replace-me'}))

    def test_worker_rejects_global_proxy_and_non_loopback_proxy(self):
        base={'SERVER_URL':'http://127.0.0.1:8765','WORKER_TOKEN':TOKEN}
        self.assertTrue(validate('worker',{**base,'HTTP_PROXY':'http://127.0.0.1:8118',**PROXY}))
        self.assertTrue(validate('worker',{**base,'PROXY_URL':'http://proxy.example:8118'}))

    def test_standby_cannot_start_primary_by_template(self):
        self.assertTrue(validate('standby',{'NODE_ROLE':'primary','WORKER_TOKEN':TOKEN}))
        self.assertEqual(validate('standby',{'NODE_ROLE':'standby','WORKER_TOKEN':TOKEN}),[])

    def test_placeholder_token_is_rejected(self):
        self.assertTrue(validate('worker',{
            'SERVER_URL':'https://app.example.com',
            **PROXY,
            'WORKER_TOKEN':'replace-with-at-least-32-random-characters'}))


if __name__=='__main__':
    unittest.main()
