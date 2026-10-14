"""Synthetic ELF fixtures exercise parsing without shipping Amazon code."""

from copy import deepcopy
import hashlib
import http.client
import importlib.util
import io
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from dillflix_prime_player.native_adapter import (
    build_native_profile, ContractError, descriptor_identity, fetch_resource,
    native_identity, NativeAdapterError, RESOURCE_NAME, _decode, _NoRedirect,
)


def elf_image(seed=b"fixture"):
    data = bytearray(4096)
    ident = b"\x7fELF\x01\x01\x01" + b"\0" * 9
    struct.pack_into("<16sHHIIIIIHHHHHH", data, 0, ident, 3, 40, 1, 0, 52, 0,
                     0x05000200, 52, 32, 3, 0, 0, 0)
    # The executable virtual address intentionally differs from the file offset.
    for i, segment in enumerate(((1, 0, 0, 0, 512, 512, 4, 4096),
                                  (1, 512, 4096, 4096, 3584, 3584, 5, 4096),
                                  (4, 256, 256, 256, 36, 36, 4, 4))):
        struct.pack_into("<8I", data, 52 + i * 32, *segment)
    build = hashlib.sha1(seed).digest()
    struct.pack_into("<III", data, 256, 4, 20, 3)
    data[268:272], data[272:292] = b"GNU\0", build
    for offset in range(512, len(data), 32):
        data[offset:offset + 32] = hashlib.sha256(seed + str(offset).encode()).digest()
    return bytes(data)


def base_profile():
    return {"engine": {"architecture": "ARM32 Thumb", "buildId": "old-engine", "functions": {"JS_Eval": 123}},
            "adapter": {"contractVersion": 1}, "testRuntime": {"old": True}}


def descriptor(data):
    digest = hashlib.sha256(data).hexdigest()
    return {"name": RESOURCE_NAME,
            "url": "https://ab8mt4dd97et.api.amazonvideo.com/living-room-app-vending/getAppResource?filename=" + digest + "_" + RESOURCE_NAME + "&token=private-test-token",
            "metadata": {"dataIntegrity": {"sha256": digest}, "compression": "zstd",
                         "binaryMetadata": {"target_triple": "armv7thumb", "engine_version": "6.0.0"},
                         "featureFlagSelectors": {"bootstrapRustClientTestUtils": True}}}


class NativeAdapterTests(unittest.TestCase):
    def test_identity_uses_virtual_offsets_and_arbitrary_new_build(self):
        image = elf_image()
        parsed = native_identity(image)
        self.assertEqual(parsed["buildId"], hashlib.sha1(b"fixture").hexdigest())
        self.assertEqual(parsed["checks"][0]["offset"], 256)
        for anchor in parsed["executableAnchors"]:
            offset = anchor["offset"] - 4096 + 512
            self.assertEqual(bytes.fromhex(anchor["hex"]), image[offset:offset + 64])
        self.assertNotEqual(parsed["buildId"], native_identity(elf_image(b"new-build"))["buildId"])

    def test_rejects_unknown_abi_and_malformed_mapping(self):
        mutations = [(18, "<H", 183, "abi"), (28, "<I", 4090, "headers"),
                     (52 + 32 + 8, "<I", 100, "mapping"),
                     (52 + 32 + 16, "<I", 5000, "segment"),
                     (256 + 4, "<I", 0xffffffff, "notes")]
        for offset, fmt, value, reason in mutations:
            with self.subTest(reason=reason):
                data = bytearray(elf_image())
                struct.pack_into(fmt, data, offset, value)
                with self.assertRaisesRegex(ContractError, reason):
                    native_identity(bytes(data))

    def test_rejects_text_relocations_and_ambiguous_executable_bytes(self):
        data = bytearray(elf_image())
        struct.pack_into("<H", data, 44, 4)
        struct.pack_into("<8I", data, 52 + 3 * 32, 2, 320, 320, 320, 16, 16, 4, 4)
        struct.pack_into("<4I", data, 320, 22, 0, 0, 0)
        with self.assertRaisesRegex(ContractError, "text_relocations"):
            native_identity(bytes(data))
        data = bytearray(elf_image())
        data[512:] = bytes(3584)
        with self.assertRaisesRegex(ContractError, "unique_anchors"):
            native_identity(bytes(data))

    def test_descriptor_is_sanitized_and_rejects_new_family(self):
        resource = descriptor(b"transfer")
        identity = descriptor_identity(resource)
        self.assertNotIn("url", identity)
        self.assertNotIn("private-test-token", str(identity))
        for field, value in (("compression", "br"), ("binaryMetadata", {"target_triple": "aarch64", "engine_version": "6.0.0"}), ("featureFlagSelectors", {"bootstrapRustClientTestUtils": False})):
            candidate = deepcopy(resource)
            candidate["metadata"][field] = value
            with self.assertRaises(ContractError):
                descriptor_identity(candidate)

    def test_candidate_changes_no_engine_or_javascript_contract(self):
        image = elf_image()
        resource, base = descriptor(image), base_profile()
        with patch("dillflix_prime_player.native_adapter._decode", return_value=image):
            profile = build_native_profile(base, resource, fetch=lambda url: image)
        self.assertEqual(profile["engine"], base["engine"])
        self.assertEqual(profile["adapter"], base["adapter"])
        self.assertEqual(base["testRuntime"], {"old": True})
        self.assertIn("live_method_contracts_required", profile["automaticNative"]["qualification"])
        self.assertNotIn("private-test-token", str(profile))

    def test_hash_mismatch_is_not_a_contract_change_and_never_decodes(self):
        with patch("dillflix_prime_player.native_adapter._decode") as decode:
            with self.assertRaises(NativeAdapterError) as error:
                build_native_profile(base_profile(), descriptor(b"expected"), fetch=lambda url: b"corrupt")
            self.assertNotIsInstance(error.exception, ContractError)
            decode.assert_not_called()

    def test_untrusted_url_never_fetches(self):
        resource = descriptor(b"x")
        for url in (resource["url"].replace("https:", "http:"), resource["url"].replace("api.amazonvideo.com", "api.amazonvideo.com.evil.example"), resource["url"].replace("filename=", "filename=changed"), resource["url"] + "#fragment"):
            with self.subTest(url=url), patch("dillflix_prime_player.native_adapter.fetch_resource") as fetch:
                with self.assertRaises(ContractError):
                    build_native_profile(base_profile(), dict(resource, url=url))
                fetch.assert_not_called()

    def test_redirect_and_fetch_exception_do_not_expose_url(self):
        with self.assertRaisesRegex(NativeAdapterError, "redirect_rejected"):
            _NoRedirect().redirect_request(None)
        with patch("urllib.request.build_opener") as opener:
            opener.return_value.open.side_effect = OSError("https://secret.example/token")
            with self.assertRaisesRegex(NativeAdapterError, "^native_resource_fetch_failed$"):
                fetch_resource("https://example.invalid/")

    def test_transfer_is_bounded_and_encoding_is_not_implicitly_changed(self):
        class Response(io.BytesIO):
            status = 200
            headers = {}
        with patch("urllib.request.build_opener") as opener, patch("dillflix_prime_player.native_adapter.MAX_TRANSFER_BYTES", 4):
            opener.return_value.open.return_value = Response(b"12345")
            with self.assertRaisesRegex(NativeAdapterError, "transfer_size"):
                fetch_resource("https://example.invalid/")
        with patch("urllib.request.build_opener") as opener:
            response = Response(b"x")
            response.headers = {"Content-Encoding": "gzip"}
            opener.return_value.open.return_value = response
            with self.assertRaisesRegex(NativeAdapterError, "response_invalid"):
                fetch_resource("https://example.invalid/")

    def test_completed_http_response_does_not_reset_closed_socket_timeout(self):
        # Exercise HTTPResponse.read1 itself: it closes fp immediately when the
        # declared Content-Length is consumed, not on the next read.
        payload = b"x" * 90000
        stream = io.BytesIO(b"HTTP/1.1 200 OK\r\nContent-Length: 90000\r\n\r\n" + payload)
        def set_timeout(_):
            if stream.closed:
                raise OSError("Bad file descriptor")
        socket = SimpleNamespace(makefile=lambda *args: stream, settimeout=set_timeout)
        stream.raw = SimpleNamespace(_sock=socket)
        response = http.client.HTTPResponse(socket)
        response.begin()
        with patch("urllib.request.build_opener") as opener:
            opener.return_value.open.return_value = response
            self.assertEqual(fetch_resource("https://example.invalid/"), payload)
        self.assertTrue(stream.closed)

    def test_slow_trickle_has_a_total_deadline_and_shrinking_read_timeout(self):
        now, timeouts = [0], []
        class Response(io.BytesIO):
            status = 200
            headers = {}
            fp = SimpleNamespace(raw=SimpleNamespace(_sock=SimpleNamespace(settimeout=timeouts.append)))
            def read1(self, size):
                now[0] += 9
                return b"x"  # Socket inactivity timeout alone would never fire.
        with patch("urllib.request.build_opener") as opener, patch("dillflix_prime_player.native_adapter.time.monotonic", side_effect=lambda: now[0]):
            opener.return_value.open.return_value = Response()
            with self.assertRaisesRegex(NativeAdapterError, "^native_resource_fetch_timeout$"):
                fetch_resource("https://example.invalid/")
        self.assertEqual(timeouts[:2], [10, 10])
        self.assertEqual(timeouts[-1], 6)
        self.assertLessEqual(len(timeouts), 7)

    @unittest.skipUnless(importlib.util.find_spec("zstandard"), "device decoder dependency unavailable")
    def test_unknown_content_size_allows_bounded_large_window(self):
        import zstandard
        # Zstd frames may omit the decoded size; that is a packaging choice,
        # not a native contract change. Exercise the real streaming fallback.
        image = elf_image() + b"padding" * 300000
        packed = zstandard.ZstdCompressor(write_content_size=False).compress(image)
        params = zstandard.get_frame_parameters(packed)
        self.assertEqual(params.content_size, zstandard.CONTENTSIZE_UNKNOWN)
        self.assertGreater(params.window_size, 128 * 1024)
        profile = build_native_profile(base_profile(), descriptor(packed), fetch=lambda url: packed)
        self.assertEqual(profile["testRuntime"]["decodedSha256"], hashlib.sha256(image).hexdigest())
        with patch("dillflix_prime_player.native_adapter.MAX_IMAGE_BYTES", 1024 * 1024):
            with self.assertRaisesRegex(ContractError, "window_exceeded"):
                _decode(packed)

    @unittest.skipUnless(importlib.util.find_spec("zstandard"), "device decoder dependency unavailable")
    def test_resource_address_accepts_decoded_hash_and_verifies_it(self):
        import zstandard
        image = elf_image()
        packed = zstandard.ZstdCompressor().compress(image)
        resource = descriptor(packed)
        transfer_hash = hashlib.sha256(packed).hexdigest()
        decoded_hash = hashlib.sha256(image).hexdigest()
        resource["url"] = resource["url"].replace(transfer_hash, decoded_hash)
        profile = build_native_profile(base_profile(), resource, fetch=lambda url: packed)
        self.assertEqual(profile["testRuntime"]["compressedSha256"], transfer_hash)
        self.assertEqual(profile["testRuntime"]["decodedSha256"], decoded_hash)
        resource["url"] = resource["url"].replace(decoded_hash, "0" * 64)
        with self.assertRaisesRegex(ContractError, "address_hash_mismatch"):
            build_native_profile(base_profile(), resource, fetch=lambda url: packed)

    @unittest.skipUnless(importlib.util.find_spec("zstandard"), "device decoder dependency unavailable")
    def test_real_zstd_roundtrip_rejects_extra_frame_and_decoded_size_limit(self):
        import zstandard
        image = elf_image()
        packed = zstandard.ZstdCompressor().compress(image)
        profile = build_native_profile(base_profile(), descriptor(packed), fetch=lambda url: packed)
        self.assertEqual(profile["testRuntime"]["decodedSha256"], hashlib.sha256(image).hexdigest())
        with self.assertRaisesRegex(ContractError, "decompression_failed"):
            _decode(packed + packed)
        with patch("dillflix_prime_player.native_adapter.MAX_IMAGE_BYTES", 1024):
            with self.assertRaises(ContractError):
                _decode(packed)


if __name__ == "__main__":
    unittest.main()
