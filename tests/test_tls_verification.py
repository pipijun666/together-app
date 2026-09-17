import unittest
import socket
import ssl
import threading
import tempfile
import os
import datetime
from pathlib import Path

from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization

class TLSTestServer:
    """Ephemeral TLS server running on 127.0.0.1 for negative and positive TLS verification."""
    def __init__(self, cert_path, key_path):
        self.cert_path = cert_path
        self.key_path = key_path
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(('127.0.0.1', 0))
        self.port = self.sock.getsockname()[1]
        self.sock.listen(5)
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.load_cert_chain(certfile=self.cert_path, keyfile=self.key_path)
        while self.running:
            try:
                raw_conn, _ = self.sock.accept()
            except OSError:
                break
            try:
                with context.wrap_socket(raw_conn, server_side=True) as tls_conn:
                    data = tls_conn.recv(1024)
                    if data:
                        tls_conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK")
            except Exception:
                pass
            finally:
                try:
                    raw_conn.close()
                except Exception:
                    pass

    def stop(self):
        self.running = False
        try:
            self.sock.close()
        except Exception:
            pass
        self.thread.join(timeout=1.0)


def generate_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)

def generate_ca(key, common_name="Test Local CA"):
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "CN"),
        x509.NameAttribute(NameOID.COMMON_NAME, common_name),
    ])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return cert

def generate_cert(key, issuer_cert, issuer_key, san_hosts, expired=False, self_signed=False):
    subject = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "CN"),
        x509.NameAttribute(NameOID.COMMON_NAME, san_hosts[0] if san_hosts else "localhost"),
    ])
    signing_key = key if self_signed else issuer_key
    issuer_name = subject if self_signed else issuer_cert.subject
    now = datetime.datetime.now(datetime.timezone.utc)
    
    if expired:
        not_before = now - datetime.timedelta(days=30)
        not_after = now - datetime.timedelta(days=1)
    else:
        not_before = now - datetime.timedelta(days=1)
        not_after = now + datetime.timedelta(days=30)

    san_list = []
    for h in san_hosts:
        try:
            ip = socket.inet_aton(h)
            import ipaddress
            san_list.append(x509.IPAddress(ipaddress.ip_address(h)))
        except Exception:
            san_list.append(x509.DNSName(h))

    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.SubjectAlternativeName(san_list), critical=False)
    )
    return builder.sign(signing_key, hashes.SHA256())


class TestTLSVerification(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        td = Path(cls.temp_dir.name)

        # 1. Generate Root CA
        cls.ca_key = generate_key()
        cls.ca_cert = generate_ca(cls.ca_key, "Together Test Root CA")
        cls.ca_cert_path = str(td / "ca.crt")
        with open(cls.ca_cert_path, "wb") as f:
            f.write(cls.ca_cert.public_bytes(serialization.Encoding.PEM))

        # 2. Self-signed certificate (CN = together.example.com, SAN = 127.0.0.1, together.example.com)
        cls.self_signed_key = generate_key()
        cls.self_signed_cert = generate_cert(
            cls.self_signed_key, None, None,
            san_hosts=["127.0.0.1", "together.example.com"],
            self_signed=True
        )
        cls.self_signed_cert_path = str(td / "self_signed.crt")
        cls.self_signed_key_path = str(td / "self_signed.key")
        with open(cls.self_signed_cert_path, "wb") as f:
            f.write(cls.self_signed_cert.public_bytes(serialization.Encoding.PEM))
        with open(cls.self_signed_key_path, "wb") as f:
            f.write(cls.self_signed_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption()
            ))

        # 3. Expired certificate (signed by trusted CA)
        cls.expired_key = generate_key()
        cls.expired_cert = generate_cert(
            cls.expired_key, cls.ca_cert, cls.ca_key,
            san_hosts=["127.0.0.1", "together.example.com"],
            expired=True
        )
        cls.expired_cert_path = str(td / "expired.crt")
        cls.expired_key_path = str(td / "expired.key")
        with open(cls.expired_cert_path, "wb") as f:
            f.write(cls.expired_cert.public_bytes(serialization.Encoding.PEM))
        with open(cls.expired_key_path, "wb") as f:
            f.write(cls.expired_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption()
            ))

        # 4. Hostname mismatch certificate (signed by trusted CA, but SAN is wrong-domain.com)
        cls.mismatch_key = generate_key()
        cls.mismatch_cert = generate_cert(
            cls.mismatch_key, cls.ca_cert, cls.ca_key,
            san_hosts=["wrong-domain.com", "attacker.local"],
            expired=False
        )
        cls.mismatch_cert_path = str(td / "mismatch.crt")
        cls.mismatch_key_path = str(td / "mismatch.key")
        with open(cls.mismatch_cert_path, "wb") as f:
            f.write(cls.mismatch_cert.public_bytes(serialization.Encoding.PEM))
        with open(cls.mismatch_key_path, "wb") as f:
            f.write(cls.mismatch_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption()
            ))

        # 5. Valid certificate (signed by trusted CA, valid SAN for 127.0.0.1 and together.example.com)
        cls.valid_key = generate_key()
        cls.valid_cert = generate_cert(
            cls.valid_key, cls.ca_cert, cls.ca_key,
            san_hosts=["127.0.0.1", "together.example.com"],
            expired=False
        )
        cls.valid_cert_path = str(td / "valid.crt")
        cls.valid_key_path = str(td / "valid.key")
        with open(cls.valid_cert_path, "wb") as f:
            f.write(cls.valid_cert.public_bytes(serialization.Encoding.PEM))
        with open(cls.valid_key_path, "wb") as f:
            f.write(cls.valid_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption()
            ))

    @classmethod
    def tearDownClass(cls):
        cls.temp_dir.cleanup()

    def test_negative_self_signed_cert_rejected(self):
        """Self-signed certificate must fail verification (Fail-Closed)."""
        server = TLSTestServer(self.self_signed_cert_path, self.self_signed_key_path)
        try:
            client_ctx = ssl.create_default_context()
            client_ctx.load_verify_locations(cafile=self.ca_cert_path)
            with socket.create_connection(('127.0.0.1', server.port), timeout=2.0) as sock:
                with self.assertRaises((ssl.SSLCertVerificationError, ssl.CertificateError)):
                    client_ctx.wrap_socket(sock, server_hostname="together.example.com")
        finally:
            server.stop()

    def test_negative_expired_cert_rejected(self):
        """Expired certificate must fail verification even if CA is trusted (Fail-Closed)."""
        server = TLSTestServer(self.expired_cert_path, self.expired_key_path)
        try:
            client_ctx = ssl.create_default_context()
            client_ctx.load_verify_locations(cafile=self.ca_cert_path)
            with socket.create_connection(('127.0.0.1', server.port), timeout=2.0) as sock:
                with self.assertRaises((ssl.SSLCertVerificationError, ssl.CertificateError)):
                    client_ctx.wrap_socket(sock, server_hostname="together.example.com")
        finally:
            server.stop()

    def test_negative_hostname_mismatch_rejected(self):
        """Certificate with mismatched SAN must fail verification even if signed by trusted CA (Fail-Closed)."""
        server = TLSTestServer(self.mismatch_cert_path, self.mismatch_key_path)
        try:
            client_ctx = ssl.create_default_context()
            client_ctx.load_verify_locations(cafile=self.ca_cert_path)
            with socket.create_connection(('127.0.0.1', server.port), timeout=2.0) as sock:
                with self.assertRaises((ssl.SSLCertVerificationError, ssl.CertificateError)) as cm:
                    client_ctx.wrap_socket(sock, server_hostname="together.example.com")
                err_msg = str(cm.exception).lower()
                self.assertTrue("match" in err_msg or "hostname" in err_msg or "certificate verify failed" in err_msg)
        finally:
            server.stop()

    def test_positive_trusted_cert_matching_san_succeeds(self):
        """Trusted CA certificate with matching hostname/SAN must succeed without bypass."""
        server = TLSTestServer(self.valid_cert_path, self.valid_key_path)
        try:
            client_ctx = ssl.create_default_context()
            client_ctx.load_verify_locations(cafile=self.ca_cert_path)
            with socket.create_connection(('127.0.0.1', server.port), timeout=2.0) as sock:
                with client_ctx.wrap_socket(sock, server_hostname="together.example.com") as tls_sock:
                    tls_sock.sendall(b"GET / HTTP/1.1\r\nHost: together.example.com\r\n\r\n")
                    response = tls_sock.recv(1024)
                    self.assertIn(b"200 OK", response)
        finally:
            server.stop()

    def test_android_mainactivity_fail_closed_contract(self):
        """MainActivity.java must strictly fail-closed on SSL errors with handler.cancel() and NO proceed bypass."""
        main_activity_path = Path(__file__).parent.parent / "android" / "src" / "app" / "together" / "reader" / "MainActivity.java"
        content = main_activity_path.read_text(encoding="utf-8")
        
        # 1. FIXED_ORIGIN must reference BuildConfig.FIXED_ORIGIN
        self.assertIn('private static final String FIXED_ORIGIN = BuildConfig.FIXED_ORIGIN;', content)
        
        # 2. Must not contain the vulnerable bypass pattern
        self.assertNotIn('handler.proceed();', content)
        self.assertNotIn('cn.endsWith("payload.dpdns.org")', content)
        
        # 3. Must contain fail-closed call
        self.assertIn('handler.cancel();', content)

    def test_android_manifest_cleartext_disabled(self):
        """AndroidManifest.xml must set usesCleartextTraffic to false."""
        manifest_path = Path(__file__).parent.parent / "android" / "AndroidManifest.xml"
        content = manifest_path.read_text(encoding="utf-8")
        self.assertIn('android:usesCleartextTraffic="false"', content)

    def test_network_security_config_cleartext_disabled(self):
        """network_security_config.xml must disallow cleartextTrafficPermitted in base-config."""
        config_path = Path(__file__).parent.parent / "android" / "res" / "xml" / "network_security_config.xml"
        content = config_path.read_text(encoding="utf-8")
        self.assertIn('base-config cleartextTrafficPermitted="false"', content)


if __name__ == '__main__':
    unittest.main()
