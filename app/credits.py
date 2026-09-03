"""OpenRouter 계정 크레딧/잔액 조회. 간단한 메모리 캐시 포함."""

import time

import httpx

from . import openrouter

OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"
_TTL_SECONDS = 60.0

_cache: dict = {"data": None, "ts": 0.0}
_lock = __import__("threading").RLock()


async def fetch_key_info(force: bool = False) -> dict:
    """OpenRouter API 키 정보를 조회. 캐시는 TTL 동안 유효.

    Raises:
        openrouter.OpenRouterError: API 키 미설정 또는 네트워크 오류
    """
    now = time.time()
    with _lock:
        if not force and _cache["data"] is not None and now - _cache["ts"] < _TTL_SECONDS:
            return _cache["data"]

    key = openrouter.get_api_key()

    headers = {"Authorization": f"Bearer {key}"}
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0)) as client:
        resp = await client.get(OPENROUTER_KEY_URL, headers=headers)
        if resp.status_code != 200:
            raise openrouter.OpenRouterError(
                f"OpenRouter 키 정보 조회 실패 ({resp.status_code})"
            )
        payload = resp.json().get("data") or {}

    with _lock:
        _cache["data"] = payload
        _cache["ts"] = now
    return payload


def invalidate_cache() -> None:
    with _lock:
        _cache["data"] = None
        _cache["ts"] = 0.0
