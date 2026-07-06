# 내부규정 RAG Console 프로토타입

지방공기업 내부망 규정 검색 아이디어를 빠르게 검증하기 위한 로컬 웹앱입니다.

## 보안 원칙

이 저장소에는 내부 규정 원문과 로컬 색인 파일을 커밋하지 않습니다.

- `*.hwp`, `*.hwpx`, `*.pdf`, `*.zip`은 `.gitignore`로 제외합니다.
- `data/index.json`은 로컬 실행 중 생성되는 전문 색인이라 커밋하지 않습니다.
- GitHub에는 앱 코드, UI, 실행 설정, smoke test만 올립니다.

## 실행

```bash
python3 -m pip install -r requirements.txt
python3 server.py
```

브라우저에서 `http://127.0.0.1:8765`를 엽니다.

## 로컬 규정 색인

서버 실행 후 화면의 로컬 색인 기능이나 API를 사용하면 현재 워크스페이스의 규정 파일을 색인합니다.

```bash
curl -X POST http://127.0.0.1:8765/api/ingest-local
```

HWP 본문 파싱은 `hwp5txt`를 사용합니다. 자동 탐색이 안 되면 환경변수로 경로를 지정합니다.

```bash
HWP5TXT_BIN=/opt/anaconda3/bin/hwp5txt python3 server.py
```

PDF 요약 다운로드에는 `reportlab`을 사용합니다. 기본 Python에 `reportlab`이 없고 별도 Python을 쓰려면 다음처럼 지정할 수 있습니다.

```bash
REG_RAG_PDF_PYTHON=/path/to/python3 python3 server.py
```

## Docker 실행

```bash
docker build -t internal-reg-rag .
docker run --rm -p 8765:8765 internal-reg-rag
```

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
- 검색 결과 PDF/HWP 요약 문서 다운로드
- 샘플 규정 데이터
- 현재 폴더의 PDF/HWPX/HWP/ZIP 색인

## 제한

- LLM 생성 답변은 아직 연결하지 않았고, 1차 버전은 검색 근거 기반의 결정적 답변을 생성합니다.
- HWP 파싱 품질은 `hwp5txt`가 추출하는 텍스트 품질에 의존합니다.
- 실제 AD/ERP 권한 연동 대신 역할 선택으로 권한 필터링을 시뮬레이션합니다.
- GitHub Actions smoke test는 샘플 색인 기준으로만 실행합니다. 실제 내부 규정 파일은 로컬에서 별도로 색인합니다.
