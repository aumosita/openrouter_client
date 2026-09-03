"""SQLite 기반 저장소. 로컬 단일 사용자 가정.

공개 함수는 기존 JSON 버전과 동일한 시그니처/반환 형식을 유지하며,
기존 JSON 파일이 있으면 시작 시 1회 자동 마이그레이션한다.
"""

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DB_FILE = DATA_DIR / "app.db"
CONVERSATIONS_FILE = DATA_DIR / "conversations.json"
PRESETS_FILE = DATA_DIR / "presets.json"
MODELS_FILE = DATA_DIR / "models.json"

_lock = threading.RLock()

DEFAULT_MODELS = [
    "stealth/ox-alpha",
    # 텍스트 전용 모델
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "tencent/hy3",
    "~deepseek/deepseek-v4-flash-latest",
    "mistralai/mistral-nemo",
    "~anthropic/claude-sonnet-latest",
    "anthropic/claude-fable-5",
]

DEFAULT_PRESETS = [
    {
        "id": "default-translator",
        "name": "한국어 번역가",
        "description": "자연스러운 한국어 번역",
        "system_prompt": "당신은 전문 번역가입니다. 사용자가 보내는 텍스트를 자연스러운 한국어로 번역하세요. 이미 한국어인 경우 영어로 번역하세요. 번역 결과만 출력하고 부가 설명은 하지 마세요.",
        "model": None,
    },
    {
        "id": "default-code-reviewer",
        "name": "코드 리뷰어",
        "description": "코드 리뷰 및 개선 제안",
        "system_prompt": "당신은 시니어 소프트웨어 엔지니어입니다. 사용자가 보내는 코드를 리뷰하고 버그, 성능, 가독성 관점의 개선점을 한국어로 간결하게 제안하세요.",
        "model": None,
    },
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def _save(path: Path, data) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


# ---------- SQLite 부트스트랩 ----------

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS conversations (
          id TEXT PRIMARY KEY,
          title TEXT NOT NULL DEFAULT '새 대화',
          preset_id TEXT,
          model TEXT,
          pinned INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS messages (
          seq INTEGER PRIMARY KEY AUTOINCREMENT,
          conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
          role TEXT NOT NULL,
          content TEXT NOT NULL,
          annotations TEXT
        );
        CREATE TABLE IF NOT EXISTS presets (
          id TEXT PRIMARY KEY,
          name TEXT NOT NULL,
          description TEXT DEFAULT '',
          system_prompt TEXT NOT NULL,
          model TEXT
        );
        """
    )
    # 기존 DB 마이그레이션: 답변 변형(variants) 지원 컬럼 추가
    msg_cols = {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}
    if "variants" not in msg_cols:
        conn.execute("ALTER TABLE messages ADD COLUMN variants TEXT")
    if "active" not in msg_cols:
        conn.execute("ALTER TABLE messages ADD COLUMN active INTEGER NOT NULL DEFAULT 0")
    if "variant_models" not in msg_cols:
        conn.execute("ALTER TABLE messages ADD COLUMN variant_models TEXT")
    if "reasoning" not in msg_cols:
        conn.execute("ALTER TABLE messages ADD COLUMN reasoning TEXT")
    if "usage" not in msg_cols:
        conn.execute("ALTER TABLE messages ADD COLUMN usage TEXT")


def _migrate_json_if_needed(conn: sqlite3.Connection) -> None:
    """기존 JSON 파일을 SQLite로 1회 이관하고 원본은 .bak으로 백업."""
    has_convs = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
    has_presets = conn.execute("SELECT COUNT(*) FROM presets").fetchone()[0]

    if PRESETS_FILE.exists() and not has_presets:
        try:
            presets = json.loads(PRESETS_FILE.read_text(encoding="utf-8"))
            for p in presets:
                conn.execute(
                    "INSERT OR IGNORE INTO presets VALUES (?, ?, ?, ?, ?)",
                    (p["id"], p["name"], p.get("description", ""),
                     p["system_prompt"], p.get("model")),
                )
            PRESETS_FILE.rename(PRESETS_FILE.with_suffix(".json.migrated.bak"))
        except (json.JSONDecodeError, KeyError, OSError):
            pass  # 손상된 파일은 무시

    if CONVERSATIONS_FILE.exists() and not has_convs:
        try:
            convs = json.loads(CONVERSATIONS_FILE.read_text(encoding="utf-8"))
            for c in convs:
                conn.execute(
                    "INSERT INTO conversations VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (c["id"], c["title"], c.get("preset_id"), c.get("model"),
                     1 if c.get("pinned") else 0, c["created_at"], c["updated_at"]),
                )
                for m in c.get("messages", []):
                    conn.execute(
                        "INSERT INTO messages (conversation_id, role, content, annotations) "
                        "VALUES (?, ?, ?, ?)",
                        (c["id"], m["role"], m["content"],
                         json.dumps(m["annotations"], ensure_ascii=False)
                         if m.get("annotations") is not None else None),
                    )
            CONVERSATIONS_FILE.rename(CONVERSATIONS_FILE.with_suffix(".json.migrated.bak"))
        except (json.JSONDecodeError, KeyError, OSError):
            pass


def _bootstrap() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    fresh = not DB_FILE.exists()
    conn = _connect()
    try:
        _init_db(conn)
        if fresh:
            conn.execute("PRAGMA journal_mode = WAL")
        with _lock:
            _migrate_json_if_needed(conn)
            # 프리셋이 비어있으면 기본값 시딩
            if not conn.execute("SELECT COUNT(*) FROM presets").fetchone()[0]:
                for p in DEFAULT_PRESETS:
                    conn.execute(
                        "INSERT OR IGNORE INTO presets VALUES (?, ?, ?, ?, ?)",
                        (p["id"], p["name"], p["description"],
                         p["system_prompt"], p["model"]),
                    )
        conn.commit()
    finally:
        conn.close()


_bootstrap()


def _row_to_conversation(row: sqlite3.Row, messages: list) -> dict:
    conv = {
        "id": row["id"],
        "title": row["title"],
        "preset_id": row["preset_id"],
        "model": row["model"],
        "pinned": bool(row["pinned"]),
        "messages": messages,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    # 회당 usage 합산 (사이드바 비용 칩 / 메시지 액션 줄 표시용)
    cost = 0.0
    has_usage = False
    for m in messages:
        u = m.get("usage")
        if not u:
            continue
        has_usage = True
        if isinstance(u.get("cost"), (int, float)):
            cost += float(u["cost"])
    if has_usage:
        conv["usage_total"] = {"cost": cost}
    return conv


def _usage_aggregate_sql(conv_ids: list[str]) -> tuple[str, list]:
    """대화 ID 목록에 대해 회당 usage 합계 쿼리. conv_ids 비어있으면 전체."""
    if conv_ids:
        placeholders = ",".join("?" for _ in conv_ids)
        return (
            "SELECT conversation_id, usage FROM messages "
            f"WHERE role='assistant' AND usage IS NOT NULL AND conversation_id IN ({placeholders})",
            conv_ids,
        )
    return (
        "SELECT conversation_id, usage FROM messages "
        "WHERE role='assistant' AND usage IS NOT NULL",
        [],
    )


def _get_messages(conn: sqlite3.Connection, conv_id: str) -> list:
    msgs = []
    for r in conn.execute(
        "SELECT seq, role, content, annotations, variants, active, variant_models, reasoning, usage "
        "FROM messages WHERE conversation_id = ? ORDER BY seq",
        (conv_id,),
    ):
        msg = {"seq": r["seq"], "role": r["role"], "content": r["content"]}
        # 이미지 등 구조화된 content는 JSON 배열로 저장됨
        if isinstance(msg["content"], str) and msg["content"].startswith("["):
            try:
                parsed = json.loads(msg["content"])
                if isinstance(parsed, list):
                    msg["content"] = parsed
            except json.JSONDecodeError:
                pass
        if r["variants"]:
            try:
                variants = json.loads(r["variants"])
                models = []
                if r["variant_models"]:
                    try:
                        models = json.loads(r["variant_models"])
                    except json.JSONDecodeError:
                        models = []
                active = r["active"] if 0 <= r["active"] < len(variants) else len(variants) - 1
                msg["variants"] = variants
                msg["active"] = active
                if 0 <= active < len(models) and models[active]:
                    msg["model"] = models[active]
            except json.JSONDecodeError:
                pass
        if r["annotations"] is not None:
            try:
                msg["annotations"] = json.loads(r["annotations"])
            except json.JSONDecodeError:
                pass
        if r["reasoning"]:
            msg["reasoning"] = r["reasoning"]
        if r["usage"]:
            try:
                usage = json.loads(r["usage"])
                if isinstance(usage, dict) and usage.get("cost") is not None:
                    msg["usage"] = {"cost": usage["cost"]}
            except json.JSONDecodeError:
                pass
        msgs.append(msg)
    return msgs


# ---------- 프리셋 ----------

def list_presets() -> list:
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT id, name, description, system_prompt, model FROM presets"
            ).fetchall()
            return [
                {"id": r["id"], "name": r["name"], "description": r["description"],
                 "system_prompt": r["system_prompt"], "model": r["model"]}
                for r in rows
            ]
        finally:
            conn.close()


def get_preset(preset_id: str):
    return next((p for p in list_presets() if p["id"] == preset_id), None)


def create_preset(name: str, description: str, system_prompt: str, model=None) -> dict:
    with _lock:
        preset = {
            "id": uuid.uuid4().hex[:12],
            "name": name,
            "description": description,
            "system_prompt": system_prompt,
            "model": model,
        }
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO presets VALUES (?, ?, ?, ?, ?)",
                (preset["id"], name, description, system_prompt, model),
            )
            conn.commit()
            return preset
        finally:
            conn.close()


def update_preset(preset_id: str, **fields):
    with _lock:
        keys = [k for k in ("name", "description", "system_prompt", "model")
                if k in fields]
        conn = _connect()
        try:
            if not keys:
                row = conn.execute(
                    "SELECT id, name, description, system_prompt, model FROM presets "
                    "WHERE id = ?", (preset_id,),
                ).fetchone()
                return dict(row) if row else None
            sets = ", ".join(f"{k} = ?" for k in keys)
            cur = conn.execute(
                f"UPDATE presets SET {sets} WHERE id = ?",
                [fields[k] for k in keys] + [preset_id],
            )
            conn.commit()
            if cur.rowcount == 0:
                return None
            row = conn.execute(
                "SELECT id, name, description, system_prompt, model FROM presets WHERE id = ?",
                (preset_id,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


def delete_preset(preset_id: str) -> bool:
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute("DELETE FROM presets WHERE id = ?", (preset_id,))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


# ---------- 모델 즐겨찾기 ----------

def list_models() -> list:
    with _lock:
        models = _load(MODELS_FILE, None)
        if models is None:
            models = DEFAULT_MODELS
            _save(MODELS_FILE, models)
        return models


def add_model(model_id: str):
    """모델 ID를 즐겨찾기에 추가. 이미 있으면 None 반환."""
    model_id = model_id.strip()
    if not model_id:
        return None
    with _lock:
        models = list_models()
        if model_id in models:
            return None
        models.append(model_id)
        _save(MODELS_FILE, models)
        return model_id


def delete_model(model_id: str) -> bool:
    with _lock:
        models = list_models()
        new = [m for m in models if m != model_id]
        if len(new) == len(models):
            return False
        _save(MODELS_FILE, new)
        return True


# ---------- 대화 ----------

def list_conversations() -> list:
    """고정 대화를 상단에, 최근 수정순으로 반환."""
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT * FROM conversations ORDER BY pinned DESC, updated_at DESC"
            ).fetchall()
            return [_row_to_conversation(row, _get_messages(conn, row["id"]))
                    for row in rows]
        finally:
            conn.close()


def get_conversation(conv_id: str):
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT * FROM conversations WHERE id = ?", (conv_id,)
            ).fetchone()
            if not row:
                return None
            return _row_to_conversation(row, _get_messages(conn, conv_id))
        finally:
            conn.close()


def create_conversation(title: str = "새 대화", preset_id=None, model=None) -> dict:
    with _lock:
        conv = {
            "id": uuid.uuid4().hex[:12],
            "title": title,
            "preset_id": preset_id,
            "model": model,
            "pinned": False,
            "messages": [],
            "created_at": _now(),
            "updated_at": _now(),
        }
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO conversations VALUES (?, ?, ?, ?, 0, ?, ?)",
                (conv["id"], title, preset_id, model,
                 conv["created_at"], conv["updated_at"]),
            )
            conn.commit()
            return conv
        finally:
            conn.close()


def update_conversation(conv_id: str, **fields):
    with _lock:
        keys = [k for k in ("title", "preset_id", "model", "pinned") if k in fields]
        if not keys:
            return get_conversation(conv_id)
        if "pinned" in fields:
            fields["pinned"] = 1 if fields["pinned"] else 0
        sets = ", ".join(f"{k} = ?" for k in keys)
        params = [fields[k] for k in keys] + [_now(), conv_id]
        conn = _connect()
        try:
            cur = conn.execute(
                f"UPDATE conversations SET {sets}, updated_at = ? WHERE id = ?", params
            )
            conn.commit()
            if cur.rowcount == 0:
                return None
            row = conn.execute(
                "SELECT * FROM conversations WHERE id = ?", (conv_id,)
            ).fetchone()
            return _row_to_conversation(row, _get_messages(conn, conv_id))
        finally:
            conn.close()


def delete_conversation(conv_id: str) -> bool:
    with _lock:
        from . import images
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT content, variants FROM messages WHERE conversation_id = ?",
                (conv_id,),
            ).fetchall()
            cur = conn.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
            conn.commit()
            if cur.rowcount == 0:
                return False
        finally:
            conn.close()
        names = []
        for r in rows:
            names += images.filenames_in_content(r["content"])
            if r["variants"]:
                try:
                    for v in json.loads(r["variants"]):
                        names += images.filenames_in_content(v)
                except json.JSONDecodeError:
                    pass
        images.delete_files(names)
        return True


def delete_all_conversations(keep_pinned: bool = True) -> int:
    """대화를 일괄 삭제. keep_pinned이면 고정된 대화는 유지. 삭제 개수 반환."""
    with _lock:
        from . import images
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT content, variants FROM messages"
                + ("" if keep_pinned else " WHERE conversation_id IN "
                   "(SELECT id FROM conversations WHERE pinned = 0)")
            ).fetchall()
            where = "WHERE pinned = 0" if keep_pinned else ""
            cur = conn.execute(f"DELETE FROM conversations {where}")
            conn.commit()
        finally:
            conn.close()
        names = []
        for r in rows:
            names += images.filenames_in_content(r["content"])
            if r["variants"]:
                try:
                    for v in json.loads(r["variants"]):
                        names += images.filenames_in_content(v)
                except json.JSONDecodeError:
                    pass
        images.delete_files(names)
        return cur.rowcount


def count_conversations() -> dict:
    """모달 표시용 통계: 전체 및 고정(보존) 대화 수."""
    with _lock:
        conn = _connect()
        try:
            total = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
            pinned = conn.execute(
                "SELECT COUNT(*) FROM conversations WHERE pinned = 1"
            ).fetchone()[0]
            return {"total": total, "pinned": pinned}
        finally:
            conn.close()


def replace_last_assistant_message(conv_id: str, new_content: str, annotations=None,
                                   model: str | None = None, reasoning: str | None = None,
                                   usage: dict | None = None):
    """마지막 assistant 메시지를 재생성 결과로 교체하고 기존 답변을 보관.

    불변식: variants는 모든 답변(현재 답변 포함)의 배열이고,
    content는 항상 variants[active]와 동일하다. variant_models는 각 답변의 생성 모델.
    반환: {"seq": ..., "variant_count": ...} 또는 None
    """
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT seq, content, variants, variant_models FROM messages "
                "WHERE conversation_id = ? AND role = 'assistant' "
                "ORDER BY seq DESC LIMIT 1",
                (conv_id,),
            ).fetchone()
            if not row:
                return None
            variants = json.loads(row["variants"]) if row["variants"] else [row["content"]]
            models = json.loads(row["variant_models"]) if row["variant_models"] else []
            # 길이 정합성 보정 (구버전 데이터)
            while len(models) < len(variants):
                models.insert(0, None)
            variants.append(new_content)
            models.append(model)
            conn.execute(
                "UPDATE messages SET content = ?, variants = ?, active = ?, "
                "annotations = ?, variant_models = ?, reasoning = ?, usage = ? WHERE seq = ?",
                (
                    new_content,
                    json.dumps(variants, ensure_ascii=False),
                    len(variants) - 1,
                    json.dumps(annotations, ensure_ascii=False)
                    if annotations is not None else None,
                    json.dumps(models, ensure_ascii=False),
                    reasoning,
                    json.dumps(usage, ensure_ascii=False) if usage is not None else None,
                    row["seq"],
                ),
            )
            conn.commit()
            return {"seq": row["seq"], "variant_count": len(variants)}
        finally:
            conn.close()


def set_active_variant(seq: int, index: int):
    """답변 변형 선택(index). 선택된 답변을 content로 승격해 반환."""
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT seq, role, content, annotations, variants, variant_models "
                "FROM messages WHERE seq = ?", (seq,),
            ).fetchone()
            if not row or not row["variants"]:
                return None
            try:
                variants = json.loads(row["variants"])
                models = json.loads(row["variant_models"]) if row["variant_models"] else []
            except json.JSONDecodeError:
                return None
            if index < 0 or index >= len(variants):
                return None
            conn.execute(
                "UPDATE messages SET content = ?, active = ? WHERE seq = ?",
                (variants[index], index, seq),
            )
            conn.commit()
            msg = {"role": row["role"], "content": variants[index],
                   "variants": variants, "active": index}
            if 0 <= index < len(models) and models[index]:
                msg["model"] = models[index]
            if row["annotations"]:
                try:
                    msg["annotations"] = json.loads(row["annotations"])
                except json.JSONDecodeError:
                    pass
            return msg
        finally:
            conn.close()


def append_messages(conv_id: str, messages: list) -> None:
    """메시지 목록을 대화에 추가하고 저장."""
    with _lock:
        now = _now()
        conn = _connect()
        try:
            for m in messages:
                is_assistant = m["role"] == "assistant"
                content = m["content"]
                # 이미지 등 구조화 content(배열)는 JSON 문자열로 저장
                stored_content = (
                    json.dumps(content, ensure_ascii=False)
                    if not isinstance(content, str) else content
                )
                conn.execute(
                    "INSERT INTO messages (conversation_id, role, content, annotations, "
                    "variants, active, variant_models, reasoning, usage) "
                    "VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)",
                    (conv_id, m["role"], stored_content,
                     json.dumps(m.get("annotations"), ensure_ascii=False)
                     if m.get("annotations") is not None else None,
                     # assistant 답변은 variants에도 저장 (재생성 이력 관리의 기준점)
                     json.dumps([stored_content], ensure_ascii=False)
                     if is_assistant else None,
                     # 답변을 생성한 모델 기록 (변형별 모델 추적)
                     json.dumps([m.get("model")], ensure_ascii=False)
                     if is_assistant and m.get("model") else None,
                     m.get("reasoning") if is_assistant else None,
                     json.dumps(m.get("usage"), ensure_ascii=False)
                     if is_assistant and m.get("usage") else None),
                )
            # 첫 사용자 메시지로 제목 자동 설정
            title_row = conn.execute(
                "SELECT title FROM conversations WHERE id = ?", (conv_id,)
            ).fetchone()
            if title_row and title_row["title"] == "새 대화":
                first_user = next((m for m in messages if m["role"] == "user"), None)
                if first_user:
                    c = first_user["content"]
                    # 배열 content면 텍스트 파트만 이어서 제목으로 사용
                    title_text = "".join(
                        p.get("text", "") for p in c
                        if isinstance(p, dict) and p.get("type") == "text"
                    ) if isinstance(c, list) else (c or "")
                    conn.execute(
                        "UPDATE conversations SET title = ? WHERE id = ?",
                        (title_text[:40], conv_id),
                    )
            conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conv_id)
            )
            conn.commit()
        finally:
            conn.close()
