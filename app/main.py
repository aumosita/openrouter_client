"""FastAPI 앱 — 라우트 및 SSE 채팅 엔드포인트."""

import json
import os
import time
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from . import credits, images, openrouter, storage

load_dotenv()

DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", "openai/gpt-4o-mini")
BASE_SYSTEM_PROMPT = os.environ.get("BASE_SYSTEM_PROMPT", "")
STATIC_DIR = Path(__file__).resolve().parent / "static"
DEFAULT_PORT = int(os.environ.get("PORT", "8004"))

app = FastAPI(title="OpenRouter 로컬 챗")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=DEFAULT_PORT, reload=True)


# ---------- 요청 모델 ----------

class ConversationCreate(BaseModel):
    preset_id: str | None = None
    model: str | None = None


class ConversationUpdate(BaseModel):
    title: str | None = None
    preset_id: str | None = None
    model: str | None = None
    pinned: bool | None = None


class BulkDeleteRequest(BaseModel):
    keep_pinned: bool = True


class ChatRequest(BaseModel):
    conversation_id: str
    message: str = ""
    model: str | None = None
    web_search: bool = False
    regenerate_last: bool = False
    modalities: list[str] | None = None


class VariantSelect(BaseModel):
    active: int


class UploadRequest(BaseModel):
    data_uri: str  # data:image/png;base64,... 형식
class PresetCreate(BaseModel):
    name: str
    description: str = ""
    system_prompt: str
    model: str | None = None


class PresetUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    system_prompt: str | None = None
    model: str | None = None


class ModelAdd(BaseModel):
    model_id: str


# ---------- 페이지 ----------

@app.get("/")
def index():
    return FileResponse(
        STATIC_DIR / "index.html",
        headers={"Cache-Control": "no-store"},
    )


# ---------- 업로드 / 정적 서빙 ----------

@app.post("/api/uploads", status_code=201)
def upload_image(body: UploadRequest):
    url = images.save_data_uri(body.data_uri)
    if not url:
        raise HTTPException(400, "이미지 저장 실패 (형식 또는 4MB 크기 제한)")
    return {"url": url}


@app.get("/uploads/{filename}")
def serve_upload(filename: str):
    path = images.path_for(filename)
    if not path:
        raise HTTPException(404, "파일을 찾을 수 없습니다.")
    return FileResponse(path)


@app.get("/api/config")
def config():
    return {"default_model": DEFAULT_MODEL}


@app.get("/api/credits")
async def get_credits():
    """OpenRouter 계정의 크레딧 잔액 / 사용량. 캐시 TTL=60초."""
    try:
        info = await credits.fetch_key_info(force=False)
    except openrouter.OpenRouterError as e:
        raise HTTPException(502, str(e))
    return {
        "label": info.get("label"),
        "limit": info.get("limit"),
        "limit_remaining": info.get("limit_remaining"),
        "usage": info.get("usage"),
        "usage_daily": info.get("usage_daily"),
        "usage_weekly": info.get("usage_weekly"),
        "usage_monthly": info.get("usage_monthly"),
        "is_free_tier": info.get("is_free_tier"),
    }


@app.post("/api/credits/refresh")
async def refresh_credits():
    """크레딧 캐시를 무효화하고 즉시 재조회."""
    credits.invalidate_cache()
    return await get_credits()


# ---------- 대화 ----------

@app.get("/api/conversations")
def list_conversations():
    return storage.list_conversations()


@app.post("/api/conversations", status_code=201)
def create_conversation(body: ConversationCreate):
    return storage.create_conversation(preset_id=body.preset_id, model=body.model)


@app.patch("/api/conversations/{conv_id}")
def update_conversation(conv_id: str, body: ConversationUpdate):
    conv = storage.update_conversation(
        conv_id, **body.model_dump(exclude_unset=True)
    )
    if not conv:
        raise HTTPException(404, "대화를 찾을 수 없습니다.")
    return conv


@app.delete("/api/conversations/{conv_id}")
def delete_conversation(conv_id: str):
    if not storage.delete_conversation(conv_id):
        raise HTTPException(404, "대화를 찾을 수 없습니다.")
    return {"ok": True}


@app.post("/api/conversations/bulk-delete")
def bulk_delete_conversations(body: BulkDeleteRequest):
    stats = storage.count_conversations()
    deleted = storage.delete_all_conversations(keep_pinned=body.keep_pinned)
    return {
        "ok": True,
        "deleted": deleted,
        "kept": max(stats["total"] - deleted, 0),
    }


# ---------- 프리셋 ----------

@app.get("/api/presets")
def list_presets():
    return storage.list_presets()


@app.post("/api/presets", status_code=201)
def create_preset(body: PresetCreate):
    return storage.create_preset(
        body.name, body.description, body.system_prompt, body.model
    )


@app.put("/api/presets/{preset_id}")
def update_preset(preset_id: str, body: PresetUpdate):
    preset = storage.update_preset(preset_id, **body.model_dump(exclude_unset=True))
    if not preset:
        raise HTTPException(404, "프리셋을 찾을 수 없습니다.")
    return preset


@app.delete("/api/presets/{preset_id}")
def delete_preset(preset_id: str):
    if not storage.delete_preset(preset_id):
        raise HTTPException(404, "프리셋을 찾을 수 없습니다.")
    return {"ok": True}


# ---------- 모델 ----------

_models_cache: dict = {"data": None, "fetched_at": 0.0}
MODELS_CACHE_TTL = 1800.0  # 30분


@app.get("/api/openrouter/models")
async def search_openrouter_models(query: str | None = None):
    now = time.monotonic()
    if (
        _models_cache["data"] is None
        or now - _models_cache["fetched_at"] > MODELS_CACHE_TTL
    ):
        try:
            _models_cache["data"] = await openrouter.list_models()
            _models_cache["fetched_at"] = now
        except openrouter.OpenRouterError:
            if _models_cache["data"] is None:
                raise HTTPException(502, "모델 목록을 가져올 수 없습니다.")
            # 실패 시 캐시된 (stale) 데이터 사용
    models = _models_cache["data"]
    if query:
        q = query.lower()
        models = [
            m for m in models
            if q in m["id"].lower() or q in m["name"].lower()
        ]
        return models[:50]
    return {"count": len(models)}


@app.get("/api/models")
def list_favorite_models():
    return storage.list_models()


@app.get("/api/models/capabilities")
async def model_capabilities():
    """모델 ID별 이미지 입출력 지원 여부 맵."""
    try:
        now = time.monotonic()
        if (
            _models_cache["data"] is None
            or now - _models_cache["fetched_at"] > MODELS_CACHE_TTL
        ):
            _models_cache["data"] = await openrouter.list_models()
            _models_cache["fetched_at"] = now
        return {
            m["id"]: {
                "input_image": "image" in (m.get("input_modalities") or []),
                "output_image": "image" in (m.get("output_modalities") or []),
            }
            for m in _models_cache["data"]
        }
    except openrouter.OpenRouterError:
        return {}  # 목록 조회 실패 시 전원 미지원 처리


@app.post("/api/models", status_code=201)
def add_favorite_model(body: ModelAdd):
    added = storage.add_model(body.model_id)
    if not added:
        raise HTTPException(400, "유효하지 않거나 이미 등록된 모델입니다.")
    return {"ok": True, "model_id": added}


@app.delete("/api/models/{model_id:path}")
def delete_favorite_model(model_id: str):
    if not storage.delete_model(model_id):
        raise HTTPException(404, "모델을 찾을 수 없습니다.")
    return {"ok": True}


@app.patch("/api/messages/{seq}")
def select_message_variant(seq: int, body: VariantSelect):
    msg = storage.set_active_variant(seq, body.active)
    if not msg:
        raise HTTPException(404, "메시지를 찾을 수 없습니다.")
    return msg


# ---------- 채팅 (SSE 스트리밍) ----------

def _to_api_content(content):
    """저장 형식의 content(문자열 또는 배열)를 OpenRouter API 형식으로 변환."""
    if isinstance(content, str):
        return content
    parts = []
    for p in content:
        if not isinstance(p, dict):
            continue
        if p.get("type") == "text":
            parts.append({"type": "text", "text": p.get("text", "")})
        elif p.get("type") == "image" and p.get("url"):
            # 로컬 업로드 URL을 절대 경로로 읽어 base64 data URI로 변환
            path = images.path_for(p["url"].rsplit("/", 1)[-1])
            if path:
                import base64
                b64 = base64.b64encode(path.read_bytes()).decode()
                mime = f"image/{path.suffix.lstrip('.')}"
                if mime == "image/jpg":
                    mime = "image/jpeg"
                parts.append({"type": "image_url",
                              "image_url": {"url": f"data:{mime};base64,{b64}"}})
        else:
            parts.append(p)
    return parts

@app.post("/api/chat")
async def chat(body: ChatRequest):
    conv = storage.get_conversation(body.conversation_id)
    if not conv:
        raise HTTPException(404, "대화를 찾을 수 없습니다.")
    if not body.regenerate_last and not body.message.strip():
        raise HTTPException(400, "메시지가 필요합니다.")

    model = body.model or conv.get("model") or DEFAULT_MODEL

    # 메시지 구성: 기본 프롬프트 + 프리셋 시스템 프롬프트 + 히스토리 (+ 새 메시지)
    history = list(conv["messages"])
    if body.regenerate_last:
        # 마지막 assistant 답변을 제외한 히스토리로 재생성
        if not history or history[-1]["role"] != "assistant":
            raise HTTPException(400, "재생성할 답변이 없습니다.")
        history = history[:-1]

    messages = []
    if BASE_SYSTEM_PROMPT:
        messages.append({"role": "system", "content": BASE_SYSTEM_PROMPT})
    if conv.get("preset_id"):
        preset = storage.get_preset(conv["preset_id"])
        if preset:
            messages.append({"role": "system", "content": preset["system_prompt"]})
    for m in history:
        messages.append({"role": m["role"], "content": _to_api_content(m["content"])})
    if not body.regenerate_last:
        messages.append({"role": "user", "content": body.message})

    async def event_stream():
        full_text = ""
        annotations = None
        image_urls: list[str] = []
        full_reasoning = ""
        last_usage: dict | None = None

        def sse(event: str, data) -> str:
            return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

        async for kind, data in openrouter.stream_chat(
            messages, model, body.web_search, body.modalities
        ):
            if kind == "token":
                full_text += data
                yield sse("token", data)
            elif kind == "reasoning":
                full_reasoning += data
                yield sse("reasoning", data)
            elif kind == "image_data":
                url = images.save_data_uri(data)  # base64 → 파일 저장, URL만 전달
                if url:
                    image_urls.append(url)
                    yield sse("image", url)
            elif kind == "annotations":
                annotations = data
                yield sse("annotations", data)
            elif kind == "usage":
                last_usage = data
                yield sse("usage", data)
            elif kind == "error":
                yield sse("error", data)
                return

        # 스트림 완료 후 대화에 저장 (이미지가 있으면 content를 배열로 구성)
        if image_urls:
            assistant_content: str | list = [{"type": "text", "text": full_text}]
            assistant_content += [{"type": "image", "url": u} for u in image_urls]
        else:
            assistant_content = full_text

        if body.regenerate_last:
            storage.replace_last_assistant_message(
                body.conversation_id, assistant_content, annotations, model,
                reasoning=full_reasoning or None,
                usage=last_usage,
            )
        else:
            assistant_msg = {"role": "assistant", "content": assistant_content, "model": model}
            if annotations:
                assistant_msg["annotations"] = annotations
            if full_reasoning:
                assistant_msg["reasoning"] = full_reasoning
            if last_usage:
                assistant_msg["usage"] = last_usage
            storage.append_messages(
                body.conversation_id,
                [
                    {"role": "user", "content": body.message},
                    assistant_msg,
                ],
            )
        yield sse("done", {"title": storage.get_conversation(body.conversation_id)["title"]})

    return StreamingResponse(event_stream(), media_type="text/event-stream")
