# KBS 굿모닝 팝스 — 팟캐스트 파이프라인

faster-whisper + Supabase + GitHub Pages + Google Imagen 4 조합으로 KBS 굿모닝 팝스
팟캐스트를 자동 처리하는 엔드투엔드 파이프라인.

**라이브 사이트**: https://leeyunhome.github.io/goodmorningpops_podcast/

```
다운로드 → Whisper 전사 → 코너 추출 → MP3 최적화
    → Supabase 업로드 → AI 아트워크 생성 → 빌드 → GitHub Pages 배포
```

> **저작권 주의**: KBS 방송 콘텐츠의 공개 호스팅은 저작권 침해 소지가 있습니다.
> 개인 학습 목적으로만 사용하고 본인이 위험을 인지한 상태에서 진행하세요.

---

## 1. 설치

### Python 3.11 권장 (3.13/3.14는 torch 휠 미지원)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

GPU 사용 시 torch 추가 설치 (4~8배 빠른 전사):

```powershell
pip install torch --index-url https://download.pytorch.org/whl/cu121
```

GPU 없으면 자동으로 CPU(int8)로 동작.

### .env 파일 설정

```env
# Cloudflare R2 (오디오 + 아트워크 스토리지)
R2_ACCOUNT_ID=f5280adda1535bcb64e9ae4f9d49f8fb
R2_ACCESS_KEY_ID=<Access Key ID>
R2_SECRET_ACCESS_KEY=<Secret Access Key>
R2_BUCKET_NAME=gmp-audio
R2_PUBLIC_URL=https://pub-XXXX.r2.dev

# Google AI Studio API 키 (아트워크 생성용, 없으면 스킵)
gemini_api_key=AIzaSy...
```

자세한 R2 설정 방법은 [SUPABASE_TO_R2_MIGRATION.md](SUPABASE_TO_R2_MIGRATION.md) 참조.

`.env`는 `.gitignore`에 포함되어 있어 절대 commit되지 않음.

---

## 2. 한 줄 실행 (권장)

`run_pipeline.py` 하나로 다운로드부터 배포까지 자동 처리.

```powershell
# 특정 날짜 하루
python run_pipeline.py 1494088127 --date 2026-06-20 --repo . --prompt "조정현, 굿모닝 팝스, KBS"

# 날짜 범위
python run_pipeline.py 1494088127 --from 2026-06-19 --to 2026-06-23 --repo . --prompt "조정현, 굿모닝 팝스, KBS"

# Screen English 시절 (2020~2024)
python run_pipeline.py 1494088127 --from 2020-06-01 --to 2020-06-30 `
    --repo ../goodmorningpops_podcast --prompt "조정현, 굿모닝 팝스, KBS"
```

`--repo .` 는 이 repo 자체가 GitHub Pages 배포 repo인 경우.

### 8단계 파이프라인 (회차별 순서)

```
[1/8] download           mp3가 없으면 RSS에서 받음
[2/8] transcribe         GPU로 Whisper 전사 (.srt .txt .md)
[3/8] extract/copy       Screen English·Review Time → 코너 추출
                         팝송 코너 → 원본 전체 복사
[4/8] optimize           64kbps 모노 재인코딩 (용량 70~80% 절감)
[5/8] upload supabase    public bucket 업로드 + URL 기록
[6/8] generate artwork   Google Imagen 4로 AI 이미지 생성 → Supabase 업로드
[7/8] build player data  회차 JSON + index.json 갱신 + 정적 자산 복사
[8/8] git push           배포 repo commit & push → Pages 라이브
```

회차 루프 완료 후 **누락 아트워크 자동 보충** (`sync_missing_artwork`):
이전 실행에서 API 오류로 실패한 날짜의 아트워크를 자동으로 채운 뒤 최종 재빌드.

### 옵션

| 옵션 | 의미 |
|------|------|
| `--date YYYY-MM-DD` | 하루만 처리 |
| `--from / --to` | 날짜 범위 |
| `--repo <path>` | 배포 repo 로컬 경로 (필수) |
| `--prompt "..."` | 전사 초기 프롬프트 (고유명사 유도) |
| `--model large-v3` | 정확도 우선 (기본: large-v3-turbo) |
| `--device cuda/cpu/auto` | 디바이스 (기본: auto) |
| `--bitrate 48k` | MP3 비트레이트 (기본: 64k) |
| `--overwrite` | 모든 단계 강제 재실행 |
| `--backup-corners` | 시작 전 corners/ 백업 |
| `--no-push` | git push 생략 (로컬 테스트용) |
| `--stop-on-error` | 첫 에러에서 중단 (기본: 다음 회차로) |
| `--limit N` | 앞에서 N개만 처리 |

---

## 3. 단계별 개별 실행

각 스크립트는 독립적으로 실행 가능. 중간 단계만 다시 돌릴 때 사용.

### 다운로드

```powershell
python fetch_episode.py 1494088127 --date 2026-06-20
python fetch_episode.py 1494088127 --from 2026-06-01 --to 2026-06-30
python fetch_episode.py 1494088127 --date 2026-06-20 --transcribe  # 전사까지
```

### 전사

```powershell
python transcribe.py audio/2026-06-20_파일명.mp3
python transcribe.py audio/ --from 2026-06-01 --to 2026-06-30
```

출력 포맷: `.txt` (평문) / `.srt` (자막) / `.md` (읽기용 스크립트, 타임스탬프 포함)

### 코너 추출

```powershell
python extract_corner.py audio --dry-run   # 미리 보기
python extract_corner.py audio             # 실제 추출
```

Screen English·Review Time Screen 코너를 START/END 마커로 잘라냄.
팝송 코너(2024.06 이후)는 `run_pipeline.py`가 자동으로 전체 방송 복사.

### MP3 최적화

```powershell
python optimize_mp3.py audio/corners           # 기본 64k 모노
python optimize_mp3.py audio/corners --bitrate 48k
```

### Supabase 업로드

```powershell
python upload_supabase.py audio/corners/optimized
```

### AI 아트워크 생성

```powershell
python generate_artwork.py audio/corners --date 2026-06-20
python generate_artwork.py audio/corners --from 2026-06-01 --to 2026-06-30
python generate_artwork.py audio/corners --overwrite   # 전체 재생성
python generate_artwork.py audio/corners --dry-run     # 프롬프트만 확인
```

- 팝송 파일명에서 곡명·아티스트 자동 추출 (예: `2026-06-20_6_20_토_ Song Title - Artist.mp3`)
- Vintage 70s-80s concert poster 스타일 이미지 생성
- PNG 1~2MB → JPEG 30~50KB 압축 후 Supabase Storage에 업로드
- 결과는 `artwork_urls.json`에 저장

API 폴백 순서: `imagen-4.0-generate-001` → `imagen-4.0-fast-generate-001` → `gemini-2.0-flash`

### 플레이어 빌드

```powershell
python build_player.py --target .          # --repo . 인 경우
python build_player.py --target ../goodmorningpops_podcast
```

---

## 4. 아트워크만 따로 보충

`run_pipeline.py` 실행 중 API 오류(503·429)로 아트워크가 누락된 경우:

```powershell
# 누락 날짜만 생성
python generate_artwork.py audio/corners --from 2026-06-11 --to 2026-06-18

# 플레이어 재빌드 + 푸시
python build_player.py --target .
git add data/ play.html index.html app.js style.css
git commit -m "deploy: artwork sync"
git push
```

또는 `run_pipeline.py`를 아무 날짜로 재실행하면 루프 후 자동 보충됨.

---

## 5. 사전 준비 (처음 설정)

### Cloudflare R2

1. https://dash.cloudflare.com → R2 Object Storage → **Create bucket** (`gmp-audio`)
2. 버킷 Settings → **Public Development URL** → Enable
3. Settings → **CORS Policy** → `[{"AllowedOrigins":["*"],"AllowedMethods":["GET"],"AllowedHeaders":["*"]}]`
4. R2 Overview → **Manage R2 API tokens** → Create (Object Read & Write) → Access Key ID + Secret 복사
5. `.env`에 저장 (`.env.example` 참고)

자세한 절차: [SUPABASE_TO_R2_MIGRATION.md](SUPABASE_TO_R2_MIGRATION.md)

### GitHub Pages (배포 repo)

1. GitHub에서 새 repo 생성 (public)
2. 로컬에 `git clone`
3. Settings → Pages → Source: `main` / `/ (root)` 선택
4. `--repo <clone된_경로>` 로 지정

이 repo 자체를 Pages repo로 쓰는 경우 `--repo .`.

### Google AI Studio (아트워크 선택)

1. https://aistudio.google.com/apikey 에서 API 키 발급
2. `.env`에 `gemini_api_key=AIzaSy...` 추가
3. 없으면 아트워크 단계 자동 스킵 (나머지 파이프라인은 정상 동작)

---

## 6. 데이터 파일

| 파일 | 내용 | git 포함 |
|------|------|---------|
| `supabase_urls.json` | 파일명 → Supabase 공개 URL | ✗ |
| `artwork_urls.json` | 에피소드 ID → 아트워크 URL | ✗ |
| `movie_mapping.json` | 날짜 → 영화 제목 | ✗ |
| `references/ngram_labels.json` | 코너 경계 감지용 레이블 | ✓ |
| `LESSONS_LEARNED.md` | 23개 트러블슈팅 기록 | ✓ |

---

## 7. 트러블슈팅

자세한 내용은 [LESSONS_LEARNED.md](LESSONS_LEARNED.md) 참조.

| 증상 | 처방 |
|------|------|
| `WinError 193` torch DLL | Python 3.11로 venv 재생성 |
| ctranslate2 traceback 없이 종료 | `ctranslate2==4.5.0` 핀 |
| cuDNN not found | `import torch` → PATH 등록 후 `from faster_whisper import` |
| 한글 경로 PyAV 에러 | 파일 핸들로 전달 (`open(path, "rb")`) |
| `pkg_resources` 없음 | `setuptools<81` |
| Imagen 503 / 429 에러 | 자동으로 다음 모델 폴백 (fast → Gemini Flash) |
| 아트워크 생성됐는데 사이트 미반영 | `build_player.py --target .` 후 `git push` |
| pip install cp949 에러 | requirements.txt에 한글 주석 금지 |
