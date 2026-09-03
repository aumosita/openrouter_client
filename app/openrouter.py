"""OpenRouter API 스트리밍 호출."""

import json
import os

import httpx

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"


class OpenRouterError(Exception):
    pass


def get_api_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key or key == "sk-or-...":
        raise OpenRouterError(
            "OPENROUTER_API_KEY가 설정되지 않았습니다. .env 파일에 키를 입력하세요."
        )
    return key


async def list_models(query: str | None = None) -> list[dict]:
    """OpenRouter 공개 모델 목록을 조회. query가 있으면 id/name에서 부분 일치 필터링."""
    headers = {"Content-Type": "application/json"}
    try:
        key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        if key and key != "sk-or-...":
            headers["Authorization"] = f"Bearer {key}"
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
            resp = await client.get(OPENROUTER_MODELS_URL, headers=headers)
            if resp.status_code != 200:
                raise OpenRouterError(f"모델 목록 조회 실패 ({resp.status_code})")
            data = resp.json().get("data", [])
    except httpx.HTTPError as e:
        raise OpenRouterError(f"네트워크 오류: {e}") from e

    models = [
        {
            "id": m.get("id", ""),
            "name": m.get("name", ""),
            "context_length": m.get("context_length"),
            "prompt_price": (m.get("pricing") or {}).get("prompt"),
            "completion_price": (m.get("pricing") or {}).get("completion"),
            "input_modalities": (m.get("architecture") or {}).get("input_modalities") or ["text"],
            "output_modalities": (m.get("architecture") or {}).get("output_modalities") or ["text"],
        }
        for m in data
        if m.get("id")
    ]
    if query:
        q = query.lower()
        models = [m for m in models if q in m["id"].lower() or q in m["name"].lower()]
    return models


async def stream_chat(messages: list, model: str, web_search: bool = False,
                      modalities: list | None = None):
    """OpenRouter chat completions을 스트리밍으로 호출.

    (이벤트 종류, 데이터) 튜플을 yield하는 async generator.
    이벤트 종류: "token"(텍스트 청크), "image"(이미지 URL), "annotations"(출처 목록), "error", "done"
    """
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
    }
    if modalities:
        payload["modalities"] = modalities
    plugins = []
    if web_search:
        plugins.append({"id": "web", "max_results": 5})
    if plugins:
        payload["plugins"] = plugins

    try:
        headers = {
            "Authorization": f"Bearer {get_api_key()}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=15.0)) as client:
            async with client.stream(
                "POST", OPENROUTER_URL, json=payload, headers=headers
            ) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread()).decode("utf-8", errors="replace")
                    try:
                        detail = json.loads(body).get("error", {}).get("message", body)
                    except json.JSONDecodeError:
                        detail = body
                    if resp.status_code == 401:
                        detail = "API 키가 올바르지 않습니다. .env를 확인하세요."
                    elif resp.status_code == 402:
                        detail = "OpenRouter 크레딧이 부족합니다."
                    yield ("error", f"OpenRouter 오류 ({resp.status_code}): {detail}")
                    return

                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("error"):
                        yield ("error", str(chunk["error"].get("message", chunk["error"])))
                        return
                    for choice in chunk.get("choices", []):
                        delta = choice.get("delta") or {}
                        reasoning = delta.get("reasoning")
                        if reasoning:
                            yield ("reasoning", reasoning)
                        content = delta.get("content")
                        if content:
                            yield ("token", content)
                        # 이미지 출력 모델: delta.images에 base64 data URI로 반환됨
                        for img in delta.get("images") or []:
                            url = img.get("image_url", {}).get("url") if isinstance(img, dict) else None
                            if url:
                                yield ("image_data", url)
                        annotations = delta.get("annotations") or (
                            choice.get("message") or {}
                        ).get("annotations")
                        if annotations:
                            yield ("annotations", annotations)
        yield ("done", None)
    except httpx.HTTPError as e:
        yield ("error", f"네트워크 오류: {e}")
    except OpenRouterError as e:
        yield ("error", str(e))
