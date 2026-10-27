"""Screenshot bytes: type sniffing from magic bytes, size flags and the PwnDoc data URL (no lxml).

The type is read from the bytes only; the part name, the extension and ``[Content_Types].xml``
are never trusted. Images are not decoded or re-encoded (Pillow is not a dependency).
"""

from __future__ import annotations

import base64
import hashlib
import struct
from dataclasses import dataclass

from . import limits
from .errors import ImportInputError

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
GIF_MAGICS = (b"GIF87a", b"GIF89a")
JPEG_MAGIC = b"\xff\xd8\xff"
JPEG_SOF = frozenset({0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF})
JPEG_STANDALONE = frozenset({0x01, *range(0xD0, 0xD8)})
MAX_JPEG_SEGMENTS = 4096
EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif"}


def _png(data: bytes) -> tuple[str, int, int]:
    if len(data) < 24 or data[12:16] != b"IHDR":
        return "", 0, 0
    width, height = struct.unpack(">II", data[16:24])
    return "image/png", width, height


def _gif(data: bytes) -> tuple[str, int, int]:
    if len(data) < 10:
        return "", 0, 0
    width, height = struct.unpack("<HH", data[6:10])
    return "image/gif", width, height


def _jpeg(data: bytes) -> tuple[str, int, int]:
    index, seen = 2, 0
    while index + 4 <= len(data) and seen < MAX_JPEG_SEGMENTS:
        seen += 1
        if data[index] != 0xFF:
            return "", 0, 0
        marker = data[index + 1]
        if marker == 0xFF:
            index += 1
            continue
        if marker in JPEG_STANDALONE:
            index += 2
            continue
        (length,) = struct.unpack(">H", data[index + 2 : index + 4])
        if length < 2:
            return "", 0, 0
        if marker in JPEG_SOF:
            if index + 9 > len(data):
                return "", 0, 0
            height, width = struct.unpack(">HH", data[index + 5 : index + 9])
            return "image/jpeg", width, height
        index += 2 + length
    return "", 0, 0


def sniff(data: bytes) -> tuple[str, int, int]:
    """``(media_type or "", width, height)`` from the bytes: PNG IHDR, JPEG SOFn, GIF header."""
    if not isinstance(data, bytes | bytearray):
        return "", 0, 0
    head = bytes(data[:16])
    if head.startswith(PNG_MAGIC):
        return _png(bytes(data[:24]))
    if head[:6] in GIF_MAGICS:
        return _gif(bytes(data[:10]))
    if head.startswith(JPEG_MAGIC):
        return _jpeg(bytes(data))
    return "", 0, 0


def data_url(data: bytes) -> str:
    """``data:<type>;base64,<b64>`` for PwnDoc's image upload. Raises ``image_not_usable`` when
    the type is not PNG, JPEG or GIF, or the image is over ``MAX_IMAGE_BYTES``."""
    media_type, _, _ = sniff(data)
    if not media_type or len(data) > limits.MAX_IMAGE_BYTES:
        raise ImportInputError("image_not_usable")
    return f"data:{media_type};base64,{base64.b64encode(bytes(data)).decode('ascii')}"


@dataclass(frozen=True)
class ImageFacts:
    """What the reader knows about one picture's bytes."""

    sha256: str
    media_type: str
    size: int
    width: int
    height: int
    flags: tuple[str, ...]

    @property
    def usable(self) -> bool:
        return bool(self.media_type) and not limits.UNUSABLE_IMAGE_FLAGS & set(self.flags)


def facts_for(data: bytes, *, size: int, digest: str = "") -> ImageFacts:
    """Facts for an image of ``size`` bytes. When ``size`` is over the cap ``data`` holds only
    the first bytes (enough to sniff the type) and ``digest`` is the streamed sha256."""
    media_type, width, height = sniff(data)
    if size > limits.MAX_IMAGE_BYTES:
        return ImageFacts(digest or _sha(data), media_type, size, 0, 0, ("too_large",))
    flags: list[str] = []
    if not media_type:
        flags.append("unsupported")
        width = height = 0
    elif max(width, height) > limits.MAX_IMAGE_PIXELS:
        flags.append("too_large")
        width = height = 0
    if size < limits.PLACEHOLDER_BYTES or (width, height) == (1, 1):
        flags.append("placeholder")
    return ImageFacts(digest or _sha(data), media_type, size, width, height, tuple(flags))


def _sha(data: bytes) -> str:
    return hashlib.sha256(bytes(data)).hexdigest()


def extension(media_type: str) -> str:
    return EXTENSIONS.get(media_type, "bin")
