# OpenRouter 로컬 챗

OpenRouter API를 사용하는 로컬 대화형 웹 서비스입니다. 웹 검색 토글과 사전작성 프롬프트 프리셋(Gemini의 Gem 유사) 기능을 제공합니다.

## 기능

- OpenRouter의 다양한 모델과 대화 (스트리밍 응답)
- 대화 저장/관리 (서버 재시작 후에도 유지, `data/conversations.json`)
- 웹 검색 토글 — OpenRouter 웹 검색 플러그인 사용, 답변에 출처 링크 표시
- 강화 검색 — 새 대화 버튼 아래 토글. 입력을 영어로 번역해 웹 검색하고, 결과를 원문 언어로 다시 번역해 표시 (출처 필수·다국어 병행 검색 규칙 적용)
- 프리셋 — 이름 + 시스템 프롬프트 + 기본 모델을 저장해 두고 대화에 적용 (CRUD 지원)

## 사전 요구사항

- Python 3.10 이상
- OpenRouter API 키 — https://openrouter.ai/keys 에서 발급

## 실행

프로젝트 폴터에서 아래 명령을 순서대로 실행하세요. 명령 프롬프트(cmd) 기준, 최초 1회는 1~3번, 이후에는 3번만 실행하면 됩니다.

### Windows (cmd)

```
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8004
```

### macOS / Linux

```
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8004
```

실행 전에 `.env.example` 파일을 복사해 `.env`를 만들고 API 키를 입력하세요:

```
OPENROUTER_API_KEY=sk-or-...
```

이후 브라우저에서 http://localhost:8004 접속.

## 사용법

- **새 대화**: 사이드바의 프리셋 드롭다운에서 프리셋을 선택한 후 "새 대화" 클릭 (선택 안 하면 일반 대화)
- **모델 변경**: 상단 바의 드롭다운 (대화별로 기억됨, `.env`의 `DEFAULT_MODEL`로 기본값 변경 가능)
- **웹 검색**: 상단 바의 토글을 켜면 `plugins: [{"id": "web", "max_results": 5}]`가 요청에 추가됩니다. 검색 결과 출처가 답변 아래에 표시됩니다.
- **강화 검색**: 사이드바 "새 대화" 아래 녹색 토글(기본 ON, 설정은 브라우저에 기억됨). 켜면 각 질문을 ① 영어로 번역(원문이 영어면 생략) → ② 3가지 규칙(출처 필수·웹 검색 검증·국가별 다국어 병행 검색)을 적용해 웹 검색+답변 생성 → ③ 결과를 원문 언어로 역번역해 표시합니다. 번역엔 `.env`의 `TRANSLATE_MODEL`, 답변 생성엔 선택한 대화 모델을 사용합니다. 이미지 첨부 시엔 자동으로 일반(비강화) 방식으로 동작합니다.
- **프리셋 관리**: 사이드바 하단 "프리셋 관리"에서 생성/편집/삭제. 프리셋에 기본 모델을 지정하면 해당 프리셋으로 시작한 대화에 자동 적용됩니다.
- **기본 프롬프트**: `.env`의 `BASE_SYSTEM_PROMPT`에 지정하면 모든 대화(프리셋 포함)에 공통으로 적용되는 system 메시지가 프리셋 프롬프트 앞에 추가됩니다. 여러 줄은 큰따옴표로 감싸서 작성할 수 있습니다.
- **대화 제목 변경**: 사이드바에서 제목 더블클릭

## 주의사항

- OpenRouter 웹 검색 플러그인은 검색당 소액의 크레딧이 차감됩니다. 요금은 OpenRouter 대시보드에서 확인하세요.
- 강화 검색은 질문 한 건당 감지/번역(1) + 검색 답변(웹 크레딧 차감 포함) + 역번역(1)으로 일반 질문보다 LLM 호출과 크레딧을 더 사용합니다.
- 로컬 단일 사용자용입니다. 인증 기능이 없으니 외부에 노출하지 마세요 (기본적으로 127.0.0.1에만 바인딩됩니다).
- 데이터는 `data/` 디렉터리의 JSON 파일에 저장됩니다.

## 프로젝트 구조

```
app/
├── main.py            # FastAPI 라우트, SSE 채팅 엔드포인트
├── openrouter.py      # OpenRouter 스트리밍 호출
├── storage.py         # JSON 파일 저장소 (대화/프리셋)
└── static/index.html  # 채팅 UI (단일 파일)
data/                  # 런타임 생성 — conversations.json, presets.json
```
