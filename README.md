# 내부규정 RAG Console 프로토타입

지방공기업 내부망 규정 검색 아이디어를 빠르게 검증하기 위한 로컬 웹앱입니다.

## 폴더 구조

```
dev/                      # 개발 폴더 (모든 파이썬 개발 코드)
  server.py               # 검색 서버 본체 (API + 화면 제공)
  auto_ingest.py          # 규정 폴더 자동 스캔 스케줄러
  regulation_registry.py  # 규정 버전 레지스트리 (감지→검토 대기→승인)
  tests/                  # 단위·통합·E2E 테스트
static/                   # 웹 화면 (HTML/CSS/JS, GitHub Pages 배포 대상)
data/                     # 로컬 색인·레지스트리 JSON (커밋하지 않음)
docs/                     # 설계 문서와 구현 계획
```

## 보안 원칙

이 저장소에는 내부 규정 원문과 로컬 색인 파일을 커밋하지 않습니다.

- `*.hwp`, `*.hwpx`, `*.pdf`, `*.zip`은 `.gitignore`로 제외합니다.
- `data/index.json`은 로컬 실행 중 생성되는 전문 색인이라 커밋하지 않습니다.
- GitHub에는 앱 코드, UI, 실행 설정, smoke test만 올립니다.

## 개발용 로컬 실행

```bash
python3 -m pip install -r requirements.txt
python3 dev/server.py
```

브라우저에서 `http://127.0.0.1:8765`를 엽니다.

승인, 반려, 업로드, 재색인, 초기화 API는 기본적으로 비활성화됩니다. 화면 시연 중 해당 변경 동작까지 사용할 때만 다음처럼 명시적으로 활성화합니다. 이 플래그는 실제 사용자 인증을 대신하지 않습니다.

```bash
REG_RAG_ENABLE_DEMO_MUTATIONS=1 python3 dev/server.py
```

전체 단위 테스트와 브라우저 E2E는 다음처럼 실행합니다. E2E에는 Python Playwright와 Chromium이 설치되어 있어야 합니다.

```bash
python3 -m unittest discover -s dev/tests -v
python3 -m py_compile dev/auto_ingest.py dev/regulation_registry.py dev/server.py
node --check static/app.js
python3 dev/tests/e2e_smoke.py
```

E2E는 임시 앱 복사본과 합성 HWPX 규정을 만들고, 사용 가능한 로컬 포트에서 서버를 직접 시작한 뒤 종료합니다. 기존 서버를 점검할 때만 `REG_RAG_BASE_URL=http://127.0.0.1:8765`를 지정합니다.

## 폐쇄망 내부 서버 배포

실제 운영판은 내부망 서버에서만 실행합니다. 내부 규정 원문은 GitHub, GitHub Pages, 공개 컨테이너 이미지에 포함하지 않고, 내부 서버의 읽기 전용 볼륨으로만 연결합니다. 운영 환경에서는 공개 API, 외부 CDN, 인터넷 호출 없이 로컬 정적 파일과 로컬 API만 사용합니다.

승인된 폐쇄망 설치 명령은 다음과 같습니다.

```bash
python3 -m pip install --no-index --find-links ./wheelhouse -r requirements.txt
REG_RAG_SOURCE_DIRS="/srv/cheonan/regulations" \
REG_RAG_AUTO_INGEST=1 \
REG_RAG_AUTO_INGEST_INTERVAL_SECONDS=60 \
python3 dev/server.py --host 0.0.0.0 --port 8765
```

권장 볼륨 구성은 다음과 같습니다.

- `/srv/cheonan/regulations`: 내부 규정 HWP/HWPX/PDF 원문을 읽기 전용으로 마운트합니다.
- `REG_RAG_SOURCE_DIRS`: 자동 색인 대상 폴더를 지정합니다. 여러 경로는 macOS/Linux 기준 `:`로 구분합니다.
- `REG_RAG_AUTO_INGEST=1`: 서버 시작 시 최초 스캔을 실행하고 서버 운영 중에도 주기적으로 규정 폴더를 다시 검사합니다.
- `REG_RAG_AUTO_INGEST_INTERVAL_SECONDS=60`: 자동 스캔 주기입니다. 운영 환경에서는 10초 이상의 정수를 사용합니다.
- 새 파일과 변경 파일은 자동으로 색인되지만 `검토 대기`로 등록되며, 감사팀장 승인 전에는 현재 검색·다운로드 결과를 변경하지 않습니다.
- `data/index.json`: 서버 로컬에서 생성되는 색인 파일이며 저장소에 커밋하지 않습니다.
- `data/index.json`과 `data/regulation_registry.json`은 하나의 서버 프로세스만 기록해야 합니다. 같은 데이터 폴더에 서버(컨테이너)를 두 개 이상 띄우지 않습니다.

Docker로 내부 서버를 실행할 때도 원문 폴더는 읽기 전용 볼륨으로 붙입니다.

```bash
docker build -t internal-reg-rag .
docker run --rm -p 8765:8765 \
  -e REG_RAG_SOURCE_DIRS=/sources \
  -e REG_RAG_AUTO_INGEST=1 \
  -e REG_RAG_AUTO_INGEST_INTERVAL_SECONDS=60 \
  -v "/srv/cheonan/regulations":/sources:ro \
  internal-reg-rag
```

HWP 본문 파싱은 `hwp5txt`를 사용합니다. 자동 탐색이 안 되면 환경변수로 경로를 지정합니다.

```bash
HWP5TXT_BIN=/opt/anaconda3/bin/hwp5txt python3 dev/server.py
```

원본 PDF 다운로드에는 `reportlab`을 사용합니다. 기본 Python에 `reportlab`이 없고 별도 Python을 쓰려면 다음처럼 지정할 수 있습니다.

```bash
REG_RAG_PDF_PYTHON=/path/to/python3 python3 dev/server.py
```

## 로컬 규정 색인

서버 실행 후 화면의 로컬 색인 기능이나 API를 사용하면 현재 워크스페이스의 규정 파일을 색인합니다. API로 실행하려면 서버를 시연 변경 플래그와 함께 시작한 뒤 요청합니다.

```bash
REG_RAG_ENABLE_DEMO_MUTATIONS=1 python3 dev/server.py
```

```bash
curl -X POST http://127.0.0.1:8765/api/ingest-local
```

서버 시작과 동시에 색인하려면 다음처럼 실행합니다.

```bash
python3 dev/server.py --ingest-local
```

규정 폴더가 앱 폴더 밖에 있으면 `REG_RAG_SOURCE_DIRS`에 지정합니다.

```bash
REG_RAG_SOURCE_DIRS="/path/to/regulations" \
REG_RAG_AUTO_INGEST=1 \
python3 dev/server.py --host 0.0.0.0 --port 8765
```

## GitHub Pages 시연판

GitHub Pages는 코드와 UI 동작을 보여주는 시연판입니다. 내부 원문, 로컬 색인, 실제 운영 데이터는 제공하지 않습니다. 실제 HWP 규정 검색과 원본 다운로드는 내부 서버 또는 로컬 서버 API가 실행 중일 때만 동작합니다.

정적 프론트엔드 URL:

```bash
https://lsb-afk.github.io/Search-for-AI-based-internal-regulations/
```

이 URL은 기본적으로 현재 PC의 로컬 검색 API(`http://127.0.0.1:8765`)에 연결합니다. 실제 검색을 하려면 로컬에서 서버가 실행 중이어야 합니다. Chrome에서 로컬 네트워크 접근 권한을 묻는 경우 허용해야 검색됩니다.

GitHub Pages 시연판에서 로컬 API 호출을 허용하려면 서버 실행 시 Pages 출처를 명시합니다. 승인·반려까지 시연할 때만 두 번째 플래그를 함께 사용합니다.

```bash
REG_RAG_ALLOWED_ORIGINS="https://lsb-afk.github.io" \
REG_RAG_ENABLE_DEMO_MUTATIONS=1 \
python3 dev/server.py
```

별도 HTTPS 백엔드 API를 연결할 때는 다음처럼 엽니다.

```bash
https://lsb-afk.github.io/Search-for-AI-based-internal-regulations/?api=https://YOUR_API_HOST
```

`main` 브랜치에 push하면 GitHub Actions가 세 가지를 수행합니다.

- `smoke`: 서버 문법 검사와 기본 API smoke test
- `docker-publish`: GitHub Container Registry에 서버 이미지 배포
- `pages`: GitHub Pages 정적 프론트엔드 배포

이미지 주소:

```bash
ghcr.io/lsb-afk/search-for-ai-based-internal-regulations:latest
```

공개 이미지에는 내부 문서를 넣지 않습니다. 실제 운영 서버에서는 폐쇄망 내부 서버 배포 절차처럼 규정 폴더를 별도 읽기 전용 볼륨으로 붙여야 합니다.

## 업데이트 워크플로

규정 갱신은 폐쇄망 원본 폴더 기준으로 다음 상태 전이를 따릅니다.

1. `scan`: `REG_RAG_SOURCE_DIRS` 폴더를 스캔하고 신규 또는 변경 파일을 감지합니다.
2. `pending`: 감지된 버전을 검토 대기 상태로 등록합니다.
3. `audit lead approval/rejection`: 감사팀장 역할의 검토 화면에서 승인 또는 반려를 시뮬레이션합니다.
4. `scheduled/current/superseded`: 승인된 시행일 기준으로 시행 예정, 현재본, 이전 버전을 구분합니다.

화면의 역할 선택과 승인·반려 동작은 제품 흐름 검증용 시뮬레이션입니다. 실제 인증, AD 그룹, ERP 권한, 전자결재 승인 강제는 이 프로토타입에 포함되어 있지 않습니다.

## 역할 권한 범위

역할 권한은 검색 결과 필터링과 화면 표시를 검증하기 위한 시각적·시뮬레이션 기능입니다. 실제 사용자 인증, 세션 관리, AD/LDAP 권한 검증, 감사팀장 실명 승인, 원문 파일 ACL enforcement를 대체하지 않습니다. 운영 적용 시에는 내부 인증 프록시나 사내 SSO/AD 권한 검증 계층을 서버 앞단에 별도로 연결해야 합니다.

## 현재 구현 범위

- PDF 텍스트 추출 및 페이지 단위 청킹
- HWPX XML 텍스트 추출
- HWP 바이너리 파일 본문 추출(`hwp5txt`)
- 자연어 질의 검색
- 사용자 권한별 검색 전 필터링
- 시행 기준일 기반 검색 전 필터링
- 근거 조항, 권한 등급, 시행 기간, 페이지 표시
- 검색 결과 요약 표시
- 원본 파일 다운로드
- 원본 PDF 다운로드
- 샘플 규정 데이터
- 현재 폴더의 PDF/HWPX/HWP/ZIP 색인
- 폐쇄망 상태, 업데이트 센터, 개정 이력, 운영 현황 화면

## 제한

- LLM 생성 답변은 아직 연결하지 않았고, 1차 버전은 검색 근거 기반의 결정적 답변을 생성합니다.
- HWP 파싱 품질은 `hwp5txt`가 추출하는 텍스트 품질에 의존합니다.
- 실제 AD/ERP 권한 연동 대신 역할 선택으로 권한 필터링을 시뮬레이션합니다. 이는 실제 인증이나 접근통제가 아닙니다.
- GitHub Actions smoke test는 샘플 색인 기준으로만 실행합니다. 실제 내부 규정 파일은 로컬에서 별도로 색인합니다.
