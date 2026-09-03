"""이미지 파일 저장/조회 유틸. 업로드 이미지는 data/uploads/에 파일로 보관."""

import base64
import binascii
import re
import uuid
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
UPLOADS_DIR = DATA_DIR / "uploads"

MAX_BYTES = 4 * 1024 * 1024  # 4MB
EXT_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

_SAFE_NAME = re.compile(r"^[0-9a-f]{12}\.(png|jpg|webp|gif)$")


def save_bytes(raw: bytes, mime: str):
    """이미지 바이트를 파일로 저장하고 URL 반환. 실패 시 None."""
    ext = EXT_BY_MIME.get(mime)
    if not ext or not raw or len(raw) > MAX_BYTES:
        return None
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    name = uuid.uuid4().hex[:12] + ext
    (UPLOADS_DIR / name).write_bytes(raw)
    return f"/uploads/{name}"


_DATA_URI_RE = re.compile(r"^data:(image/(?:png|jpeg|webp|gif));base64,(.+)$", re.S)


def save_data_uri(uri: str):
    """data URI를 디코딩해 파일로 저장하고 URL 반환. 실패 시 None."""
    m = _DATA_URI_RE.match((uri or "").strip())
    if not m:
        return None
    try:
        raw = base64.b64decode(m.group(2))
    except (binascii.Error, ValueError):
        return None
    return save_bytes(raw, m.group(1))


def path_for(filename: str) -> Path | None:
    """요청된 파일명의 실제 경로 반환(경로 탐색 방지). 없으면 None."""
    if not _SAFE_NAME.match(filename):
        return None
    p = UPLOADS_DIR / filename
    return p if p.is_file() else None


def filenames_in_content(content) -> list:
    """content(문자열 또는 배열)에서 참조 중인 업로드 파일명 추출."""
    names = []
    parts = []
    if isinstance(content, list):
        parts = [str(p.get("url", "")) for p in content if isinstance(p, dict)]
    elif isinstance(content, str):
        parts = [content]
    for part in parts:
        for m in re.finditer(r"/uploads/([0-9a-f]{12}\.(?:png|jpg|webp|gif))", part):
            names.append(m.group(1))
    return names


def delete_files(names: list) -> None:
    for name in names:
        if _SAFE_NAME.match(name):
            p = UPLOADS_DIR / name
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass