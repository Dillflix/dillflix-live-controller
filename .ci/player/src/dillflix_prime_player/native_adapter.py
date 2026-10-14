"""Qualify the identity of an app-vended inspection image, never its engine ABI.

The inspection library is accessed through named Prime plugin commands. These
helpers derive *identity* checks for that library; they neither change engine
offsets nor establish that a plugin method still has the expected semantics.
"""

from copy import deepcopy
import hashlib
import re
import struct
import time
import urllib.error
import urllib.parse
import urllib.request

from .errors import CompatibilityError

VALIDATOR_VERSION = 2
MAX_TRANSFER_BYTES = 64 * 1024 * 1024
MAX_IMAGE_BYTES = 128 * 1024 * 1024
MAX_FETCH_SECONDS = 60
RESOURCE_NAME = (
    "libamzn_av_living_room_rust_client_test_utils-armv7thumb-ignite-6.0.0.so.zst"
)
_HASH = re.compile(r"[a-f0-9]{64}\Z")


class NativeAdapterError(CompatibilityError):
    """A bounded, non-secret reason suitable for persistent diagnostics."""


class ContractError(NativeAdapterError):
    """Resource validation failed; failure_category determines recovery policy."""


def failure_category(reason):
    # Failure to acquire/parse an image does not establish an application API
    # change. Only explicit architecture/ABI evidence belongs to this category.
    if reason in {"native_architecture_unsupported", "native_engine_family_unsupported",
                  "native_base_engine_unsupported", "native_elf_abi_unsupported"}:
        return "engine_contract"
    return "packaging"


def descriptor_identity(descriptor):
    """Validate an observed resource descriptor and return only public identity."""
    if not isinstance(descriptor, dict):
        raise ContractError("native_descriptor_invalid")
    metadata = descriptor.get("metadata", descriptor)
    if not isinstance(metadata, dict):
        raise ContractError("native_metadata_invalid")
    integrity = metadata.get("dataIntegrity", {})
    digest = descriptor.get("sha256") or (
        integrity.get("sha256") if isinstance(integrity, dict) else None
    )
    binary = metadata.get("binaryMetadata", {})
    selectors = metadata.get("featureFlagSelectors", {})
    if descriptor.get("name") != RESOURCE_NAME:
        raise ContractError("native_resource_family_unsupported")
    if not isinstance(digest, str) or not _HASH.fullmatch(digest):
        raise ContractError("native_transfer_hash_invalid")
    if metadata.get("compression") != "zstd":
        raise ContractError("native_encoding_unsupported")
    if not isinstance(binary, dict) or binary.get("target_triple") != "armv7thumb":
        raise ContractError("native_architecture_unsupported")
    if binary.get("engine_version") != "6.0.0":
        raise ContractError("native_engine_family_unsupported")
    if not isinstance(selectors, dict) or selectors.get("bootstrapRustClientTestUtils") is not True:
        raise ContractError("native_inspection_selector_missing")
    hint = metadata.get("decompressedSizeHintBytes")
    if hint is not None and (type(hint) is not int or not 52 <= hint <= MAX_IMAGE_BYTES):
        raise ContractError("native_image_size_invalid")
    return {
        "name": RESOURCE_NAME,
        "sha256": digest,
        "compression": "zstd",
        "target": "armv7thumb",
        "engine": "6.0.0",
    }


def _resource_url(descriptor, identity):
    url = descriptor.get("url")
    if not isinstance(url, str) or len(url) > 16384 or any(ord(c) <= 32 for c in url):
        raise ContractError("native_resource_url_invalid")
    try:
        parsed = urllib.parse.urlsplit(url)
        valid = (
            parsed.scheme == "https"
            and re.fullmatch(r"[a-z0-9-]+\.api\.amazonvideo\.com", parsed.hostname or "")
            and parsed.port in (None, 443)
            and parsed.username is None
            and parsed.password is None
            and not parsed.fragment
            and parsed.path == "/living-room-app-vending/getAppResource"
        )
        query = urllib.parse.parse_qs(parsed.query, max_num_fields=32)
    except ValueError:
        raise ContractError("native_resource_url_invalid") from None
    filenames = query.get("filename", [])
    # Prime can address the resource by either compressed or decoded digest.
    # Verify the address against those bytes after decoding, rather than
    # assuming its digest must equal metadata.dataIntegrity (the transfer hash).
    if (not valid or len(filenames) != 1
            or not re.fullmatch(r"[a-f0-9]{64}_" + re.escape(identity["name"]), filenames[0])):
        raise ContractError("native_resource_url_untrusted")
    return url


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        raise NativeAdapterError("native_resource_redirect_rejected")


def fetch_resource(url):
    """Fetch the exact manifest URL with no redirects or transfer decompression."""
    request = urllib.request.Request(url, headers={"Accept-Encoding": "identity"})
    deadline = time.monotonic() + MAX_FETCH_SECONDS
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=10) as response:
            if response.status != 200 or response.headers.get("Content-Encoding", "identity") != "identity":
                raise NativeAdapterError("native_resource_response_invalid")
            length = response.headers.get("Content-Length")
            if length is not None and (not length.isdigit() or int(length) > MAX_TRANSFER_BYTES):
                raise NativeAdapterError("native_transfer_size_invalid")
            chunks, total = [], 0
            read = getattr(response, "read1", response.read)
            # CPython's HTTPSResponse exposes the TLS socket through SocketIO.
            # read1 performs one bounded receive, unlike read(n), which can keep
            # accumulating an indefinitely slow trickle of bytes.
            socket = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
            set_timeout = getattr(socket, "settimeout", None)
            while True:
                # HTTPResponse closes its stream when read1 consumes the last
                # Content-Length byte, before a subsequent read returns EOF.
                # Its saved socket may already be closed at that point.
                if getattr(response, "isclosed", lambda: False)():
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise NativeAdapterError("native_resource_fetch_timeout")
                if set_timeout:
                    set_timeout(min(10, remaining))
                chunk = read(min(64 * 1024, MAX_TRANSFER_BYTES + 1 - total))
                if time.monotonic() >= deadline:
                    raise NativeAdapterError("native_resource_fetch_timeout")
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_TRANSFER_BYTES:
                    raise NativeAdapterError("native_transfer_size_invalid")
            data = b"".join(chunks)
    except NativeAdapterError:
        raise
    except (OSError, ValueError, urllib.error.URLError):
        # urllib's exception text may contain the resource URL and its tokens.
        raise NativeAdapterError("native_resource_fetch_failed") from None
    if len(data) > MAX_TRANSFER_BYTES:
        raise NativeAdapterError("native_transfer_size_invalid")
    return data


def _decode(data):
    try:
        import zstandard
    except ImportError:
        raise NativeAdapterError("native_zstandard_dependency_missing") from None
    try:
        params = zstandard.get_frame_parameters(data)
        if params.window_size > MAX_IMAGE_BYTES:
            raise ContractError("native_decompression_window_exceeded")
        if params.content_size not in (zstandard.CONTENTSIZE_UNKNOWN, zstandard.CONTENTSIZE_ERROR) and params.content_size > MAX_IMAGE_BYTES:
            raise ContractError("native_image_size_invalid")
        # The decoder applies this value in bytes. Frames without a declared
        # content size take a different decoding path and enforce the limit;
        # converting it to KiB incorrectly rejects ordinary multi-MiB windows.
        # Keep the explicit frame-window and decoded-output bounds as well.
        decoded = zstandard.ZstdDecompressor(max_window_size=MAX_IMAGE_BYTES).decompress(
            data, max_output_size=MAX_IMAGE_BYTES, allow_extra_data=False
        )
    except NativeAdapterError:
        raise
    except zstandard.ZstdError:
        raise ContractError("native_decompression_failed") from None
    if not 52 <= len(decoded) <= MAX_IMAGE_BYTES:
        raise ContractError("native_image_size_invalid")
    return decoded


def native_identity(data):
    """Parse bounded ELF32 ARM headers and derive mapped, unique read-only anchors."""
    if not isinstance(data, bytes) or not 52 <= len(data) <= MAX_IMAGE_BYTES:
        raise ContractError("native_image_size_invalid")
    if data[:7] != b"\x7fELF\x01\x01\x01":
        raise ContractError("native_elf_format_unsupported")
    header = struct.unpack_from("<16sHHIIIIIHHHHHH", data)
    _, kind, machine, version, _, phoff, _, flags, ehsize, phsize, phnum, _, _, _ = header
    if kind != 3 or machine != 40 or version != 1 or ehsize != 52 or flags >> 24 != 5:
        raise ContractError("native_elf_abi_unsupported")
    if phsize != 32 or not 1 <= phnum <= 128 or phoff < 52 or phoff + phnum * phsize > len(data):
        raise ContractError("native_elf_headers_invalid")
    segments = [struct.unpack_from("<8I", data, phoff + i * phsize) for i in range(phnum)]
    for p in segments:
        if p[1] + p[4] > len(data) or p[2] + p[5] > 2**32 or p[4] > p[5]:
            raise ContractError("native_elf_segment_invalid")
    loads = sorted((p for p in segments if p[0] == 1), key=lambda p: p[2])
    if not loads or any(a[2] + a[5] > b[2] for a, b in zip(loads, loads[1:])):
        raise ContractError("native_elf_mapping_ambiguous")
    executable = [p for p in loads if p[6] & 1 and not p[6] & 2 and p[4] >= 512]
    if not executable:
        raise ContractError("native_executable_segment_missing")
    # Text relocations could change the selected bytes when the loader maps them.
    for p in segments:
        if p[0] == 2:
            if p[4] % 8:
                raise ContractError("native_dynamic_table_invalid")
            for offset in range(p[1], p[1] + p[4], 8):
                tag, value = struct.unpack_from("<II", data, offset)
                if tag == 22 or (tag == 30 and value & 4):
                    raise ContractError("native_text_relocations_unsupported")
                if tag == 0:
                    break
    notes = []
    for segment in segments:
        if segment[0] != 4:
            continue
        pos, end, count = segment[1], segment[1] + segment[4], 0
        while pos < end:
            count += 1
            if count > 256 or pos + 12 > end:
                raise ContractError("native_elf_notes_invalid")
            namesz, descsz, note_kind = struct.unpack_from("<III", data, pos)
            start = pos + 12 + ((namesz + 3) & ~3)
            next_pos = start + ((descsz + 3) & ~3)
            if next_pos > end:
                raise ContractError("native_elf_notes_invalid")
            if note_kind == 3 and namesz == 4 and data[pos + 12:pos + 16] == b"GNU\0":
                if not 4 <= descsz <= 64:
                    raise ContractError("native_build_id_invalid")
                mapped = [p for p in loads if p[1] <= pos and start + descsz <= p[1] + p[4]]
                if len(mapped) != 1:
                    raise ContractError("native_build_note_unmapped")
                p = mapped[0]
                notes.append((p[2] + pos - p[1], data[pos:start + descsz].hex(), data[start:start + descsz].hex()))
            pos = next_pos
    if len(notes) != 1:
        raise ContractError("native_build_id_ambiguous")
    segment = max(executable, key=lambda p: p[4])
    anchors = []
    for fraction in (0.35, 0.5, 0.65):
        candidate = segment[1] + (int(segment[4] * fraction) & ~15)
        # At most 32 uniqueness scans per region, including pathological images.
        for offset in range(candidate, min(candidate + 8192, segment[1] + segment[4] - 63), 256):
            value = data[offset:offset + 64]
            virtual = segment[2] + offset - segment[1]
            if len(value) == 64 and len(set(value)) >= 8 and data.count(value) == 1 and all(abs(virtual - a["offset"]) >= 64 for a in anchors):
                anchors.append({"offset": virtual, "hex": value.hex()})
                break
        else:
            raise ContractError("native_unique_anchors_unavailable")
    note_offset, note_hex, build_id = notes[0]
    return {
        "buildId": build_id,
        "decodedSha256": hashlib.sha256(data).hexdigest(),
        "checks": [{"name": "GNU build-id note", "offset": note_offset, "hex": note_hex}],
        "executableAnchors": anchors,
    }


def build_native_profile(base_profile, descriptor, *, fetch=None):
    """Return a candidate profile; native method contracts still need live checks."""
    identity = descriptor_identity(descriptor)
    if base_profile.get("engine", {}).get("architecture") != "ARM32 Thumb":
        raise ContractError("native_base_engine_unsupported")
    url = _resource_url(descriptor, identity)
    data = (fetch or fetch_resource)(url)
    if not isinstance(data, bytes) or len(data) > MAX_TRANSFER_BYTES:
        raise NativeAdapterError("native_transfer_size_invalid")
    if hashlib.sha256(data).hexdigest() != identity["sha256"]:
        raise NativeAdapterError("native_transfer_hash_mismatch")
    native = native_identity(_decode(data))
    filename_hash = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["filename"][0][:64]
    if filename_hash not in (identity["sha256"], native["decodedSha256"]):
        raise ContractError("native_resource_address_hash_mismatch")
    native.update(resourceName=identity["name"], compressedSha256=identity["sha256"])
    profile = deepcopy(base_profile)
    profile["testRuntime"] = native
    profile["automaticNative"] = {
        "validatorVersion": VALIDATOR_VERSION,
        "identity": identity,
        "qualification": "image_identity_only; live_method_contracts_required",
    }
    return profile
