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


TRANSLATE_MODEL = os.environ.get("TRANSLATE_MODEL", "openai/gpt-4o-mini")


def get_translate_model() -> str:
    return TRANSLATE_MODEL or "openai/gpt-4o-mini"


# 강화 검색 규칙 프롬프트 (검색+답변 생성 시 system 프롬프트로 사용)
SEARCH_SYSTEM_PROMPT = """Rules:
1. Cite sources - Every answer must include a URL as evidence/proof.
2. Verify via web search - Don't rely solely on training data; actively search the internet to confirm accuracy.
3. Multilingual search - When a topic is country-specific (e.g., Korea), search in both the local language (e.g., Korean) and English to get the most accurate and comprehensive results."""


async def complete(messages: list, model: str) -> str:
    """비스트리밍 단일 완성 호출. content 문자열을 반환."""
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
    }
    headers = {
        "Authorization": f"Bearer {get_api_key()}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=15.0)) as client:
            resp = await client.post(OPENROUTER_URL, json=payload, headers=headers)
            if resp.status_code != 200:
                body = resp.text
                try:
                    detail = json.loads(body).get("error", {}).get("message", body)
                except json.JSONDecodeError:
                    detail = body
                if resp.status_code == 401:
                    detail = "API 키가 올바르지 않습니다. .env를 확인하세요."
                elif resp.status_code == 402:
                    detail = "OpenRouter 크레딧이 부족합니다."
                raise OpenRouterError(f"OpenRouter 오류 ({resp.status_code}): {detail}")
            data = resp.json()
    except httpx.HTTPError as e:
        raise OpenRouterError(f"네트워크 오류: {e}") from e
    msg = (data.get("choices") or [{}])[0].get("message") or {}
    return (msg.get("content") or "").strip()


def _extract_json(text: str) -> str:
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start:end + 1]
    return text


async def detect_and_translate_to_english(text: str) -> tuple[str, str]:
    """입력 텍스트의 언어를 감지하고 영어로 번역. (source_lang, english) 반환.

    이미 영어이면 english는 원문 그대로, source_lang은 'en'.
    """
    if not text.strip():
        return "unknown", ""
    content = await complete(
        [
            {
                "role": "system",
                "content": (
                    "You are a language detection and translation helper. "
                    'Respond with STRICT JSON only, no other text: '
                    '{"source_lang": "<ISO 639-1 code or unknown>", "english": "<English translation>"}. '
                    'If the input is already English, set source_lang to "en" and '
                    'english to the exact original input text.'
                ),
            },
            {"role": "user", "content": text},
        ],
        get_translate_model(),
    )
    try:
        obj = json.loads(_extract_json(content))
        source = str(obj.get("source_lang") or "unknown").lower()
        english = str(obj.get("english") or "").strip() or text
    except (json.JSONDecodeError, ValueError):
        source, english = "unknown", text
    return source, english


async def translate_back(text: str, source_lang: str) -> str:
    """영어 답변을 원문 언어로 역번역. URL/출처 링크는 그대로 유지.

    영어이거나 언어를 알 수 없으면 원문 그대로 반환.
    """
    if source_lang in ("en", "english", "unknown"):
        return text
    if not text.strip():
        return text
    return await complete(
        [
            {
                "role": "system",
                "content": (
                    f"You are a translation assistant. Translate the following English text "
                    f"into {source_lang} for the user. Keep all source URLs, links, and citation "
                    "references unchanged and intact. Output only the translation."
                ),
            },
            {"role": "user", "content": text},
        ],
        get_translate_model(),
    )


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
                    # 사용량/비용: 보통 마지막 또는 그 직전 청크에 한 번 옴
                    usage = chunk.get("usage")
                    if usage and usage.get("cost") is not None:
                        yield ("usage", {"cost": usage["cost"]})
        yield ("done", None)
    except httpx.HTTPError as e:
        yield ("error", f"네트워크 오류: {e}")
    except OpenRouterError as e:
        yield ("error", str(e))
