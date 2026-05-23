# Podcast 전사 도구

faster-whisper + large-v3-turbo 조합으로 팟캐스트 오디오를 한국어 스크립트로 변환한다.

## 1. 설치

가상환경 권장:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

GPU(NVIDIA CUDA)가 있다면 `torch`도 같이 설치하면 4~8배 빨라진다:

```powershell
pip install torch --index-url https://download.pytorch.org/whl/cu121
```

GPU가 없으면 자동으로 CPU(int8)로 동작한다.

## 2. 에피소드 다운로드

Apple Podcasts URL의 `id=` 뒤 숫자를 이용한다. 예) `id1494088127`.

```powershell
# 최근 에피소드 목록 보기 (날짜는 제목에서 추출된 방송일)
python fetch_episode.py 1494088127

# 가장 최근(0번) 에피소드 다운로드
python fetch_episode.py 1494088127 --index 0

# 특정 방송일 다운로드
python fetch_episode.py 1494088127 --date 2026-05-18

# 날짜 범위 다운로드 (양 끝 포함)
python fetch_episode.py 1494088127 --from 2026-05-01 --to 2026-05-15

# 다운로드하면서 전사까지 자동 수행
python fetch_episode.py 1494088127 --date 2026-05-18 --transcribe

# 매칭만 확인하고 실제 다운로드는 하지 않음
python fetch_episode.py 1494088127 --from 2026-05-01 --to 2026-05-15 --dry-run
```

### 날짜 판단 기준 (`--by`)

KBS 같은 방송 팟캐스트는 RSS pubDate가 실제 방송일과 다를 수 있다(업로드 시각 기준).
이를 보정하기 위해 기본적으로 **제목에서 방송일 표기를 추출**해 날짜로 사용한다.

- `--by title` (기본) — 제목의 `5월 18일`, `2026-05-18`, `5/18`, `20260518` 등 우선 인식,
  실패 시 pubDate fallback
- `--by pubdate` — 강제로 RSS pubDate 사용

저장 파일명은 `YYYY-MM-DD_제목.mp3` 형식이며, 이미 같은 파일이 있으면 건너뛴다.
기본 저장 위치는 `audio/` 폴더.

### 다운로드 + 전사 한 번에

`--transcribe` 플래그를 주면 다운로드 직후 같은 프로세스에서 faster-whisper로 전사한다.
모델 로딩이 한 번만 일어나므로 범위 다운로드와 잘 어울린다.

```powershell
# 5월 한달치 한꺼번에 받고 전사까지
python fetch_episode.py 1494088127 --from 2026-05-01 --to 2026-05-31 --transcribe `
    --prompt "조정현, 굿모닝 팝스, KBS"
```

전사 옵션은 `--model`, `--language`, `--prompt`, `--formats`, `--overwrite` 가 그대로 전달된다.

## 3. 전사

단일 파일 또는 폴더를 입력으로 받는다. 폴더면 안의 오디오 파일을 일괄 처리하며
모델을 한 번만 로딩하므로 효율적이다.

```powershell
# 단일 파일
python transcribe.py audio/2026-05-20_에피소드제목.mp3

# 폴더 전체 일괄 전사
python transcribe.py audio/

# 폴더에서 특정 날짜만
python transcribe.py audio/ --date 2026-05-20

# 폴더에서 날짜 범위만
python transcribe.py audio/ --from 2026-05-01 --to 2026-05-15

# 어떤 파일이 처리될지만 확인
python transcribe.py audio/ --from 2026-05-01 --to 2026-05-15 --dry-run
```

옵션:

- `--model large-v3` : 정확도를 더 높이고 싶을 때 (속도는 느려짐)
- `--model medium`   : 사양이 낮은 환경
- `--prompt "조정현, 굿모닝 팝스, KBS"` : 고유명사를 정확히 표기하도록 유도
- `--no-vad` : 무음 자동 제거 비활성화
- `--overwrite` : 이미 결과 파일이 있어도 다시 전사 (기본은 건너뜀)
- `--formats txt,srt,md,json` : 생성할 출력 포맷 (기본: `txt,srt,md`)

### 출력 포맷

같은 이름으로 다음 파일이 생성된다:

- `.txt` — segment 단위 평문 (한 줄 = 한 segment)
- `.srt` — 표준 자막 포맷 (영상 플레이어용)
- `.md`  — **읽기용 스크립트**. 짧은 segment를 자연스러운 단락으로 묶고
  60초마다 `[HH:MM:SS]` 타임스탬프 마커를 표시. 가장 읽기 편한 형태.
- `.json` — 메타데이터 + 모든 segment 정보 (프로그래밍 후처리용, 기본 비활성화)

예) `.md`만 빠르게 보고 싶다면:

```powershell
python transcribe.py audio/ --formats md
```

날짜 필터는 파일명 앞에 붙은 `YYYY-MM-DD` 접두사(fetch_episode.py가 생성하는 형식)를 기준으로 한다.

## 4. 참고

- 첫 실행 시 모델 파일(약 1.5GB)이 자동 다운로드된다.
- CPU 전사는 실제 오디오 길이의 0.5~2배 정도 시간이 걸린다.
- 저작권: 개인 학습/연구 목적으로만 사용하고, 전사 결과를 공개 배포하지 말 것.

## 5. 트러블슈팅

### `WinError 193 ... shm.dll` / torch 로딩 실패

증상: 전사 시작 시 `OSError: [WinError 193] %1은(는) 올바른 Win32 응용 프로그램이 아닙니다.
Error loading "...torch/lib/shm.dll"` 발생.

원인: Python 3.13/3.14 환경에 PyTorch Windows 휠이 아직 완전히 호환되지 않거나,
ctranslate2 최신 버전이 torch를 강제 의존성으로 끌고 들어와 잘못된 빌드가 설치됨.

**해결 1 (권장): Python 3.11 또는 3.12로 venv 재생성**

```powershell
deactivate
Remove-Item -Recurse -Force .venv
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

`py -0` 으로 설치된 Python 버전 목록을 확인할 수 있다. 3.11/3.12가 없으면
python.org에서 설치한다.

**해결 2: ctranslate2 다운그레이드 (Python 버전 유지하고 싶을 때)**

```powershell
pip install "ctranslate2<4.5.0" --force-reinstall
pip uninstall torch -y
```

구버전 ctranslate2는 torch 없이 CPU 추론이 가능하다.

### 첫 실행이 너무 느림

모델(약 1.5GB)을 처음 받아오는 중이다. 한 번 받은 뒤에는 `~\.cache\huggingface\hub\`
에 캐시되어 빨라진다.

### 전사가 부정확함

- `--prompt` 에 고유명사를 더 자세히 적는다.
- `--model large-v3` 로 변경 (속도는 느려지지만 정확도 향상).
- `--no-vad` 로 VAD를 끄면 짧은 발화도 빠지지 않지만 노이즈도 같이 들어감.

## 6. 정적 사이트 배포 (Supabase + GitHub Pages)

추출된 Screen English 코너를 웹 플레이어로 만들어 모바일에서도 듣고 자막 보고 싶을 때.

### 6.0 사전 안내 (저작권)

KBS 방송 콘텐츠를 공개 호스팅하는 건 저작권 침해 소지가 있다. 개인 학습 목적이라도
public 배포 전에 본인이 위험을 인지하고 진행할 것.

### 6.1 전체 파이프라인

```
audio/corners/*.mp3
    │
    │  optimize_mp3.py                        # 모노+64kbps 재인코딩 (용량 70~80% 절감)
    ▼
audio/corners/optimized/*.mp3
    │
    │  upload_supabase.py                     # Supabase Storage 업로드
    ▼
supabase_urls.json                            # 파일명 -> 공개 URL 매핑
    │
    │  build_player.py                        # SRT + URL + 영화매핑 → JSON 생성
    ▼
temp_repo_podcast/
    ├── play.html, index.html, app.js, style.css  (web/ 에서 복사)
    └── data/
        ├── index.json                        # 회차 목록
        └── <id>.json                         # 회차별 player 데이터
    │
    │  git add/commit/push (별도 repo)
    ▼
https://<user>.github.io/<repo>/              # 라이브 사이트
```

### 6.2 사전 준비

**A. Supabase 계정 + 프로젝트**

1. https://supabase.com 가입 (무료 플랜)
2. New project 생성 (region: Northeast Asia (Seoul) 권장)
3. 좌측 메뉴 → **Storage → New bucket** → 이름 `gmp-audio`, **Public** 체크
4. 좌측 메뉴 → **Project Settings → API**
   - Project URL 복사
   - anon public key 복사 (service_role 아님, 절대 commit 금지)

**B. `.env` 파일 (이 폴더에)**

```env
SUPABASE_URL=https://xxxxxxxx.supabase.co
SUPABASE_KEY=eyJhbGciOi...
SUPABASE_BUCKET=gmp-audio
```

`.env`는 `.gitignore`에 추가해서 절대 push되지 않도록.

**C. 패키지 설치**

```powershell
pip install -r requirements.txt
```

**D. GitHub repo (배포용)**

1. github.com에서 새 repo 생성 (예: `goodmorningpops-player`, public)
2. 로컬에 클론한 위치를 기억 (예: `C:\coding\github-repository\goodmorningpops-player`)
3. 빈 상태로 두면 됨

### 6.3 빌드 + 배포

```powershell
# 1. mp3 용량 절감 (36MB → ~9MB)
python optimize_mp3.py audio/corners

# 2. Supabase Storage로 업로드
python upload_supabase.py audio/corners/optimized

# 3. 정적 자산 + JSON 생성
python build_player.py --target ../goodmorningpops-player

# 4. Pages repo 에서 commit + push
cd ../goodmorningpops-player
git add .
git commit -m "deploy: 2020-06 Screen English"
git push origin main
```

### 6.4 Pages 활성화 (1회)

GitHub repo → Settings → Pages → Source: `Deploy from a branch`, Branch: `main` / `/ (root)` 선택.
1~2분 후 `https://<user>.github.io/<repo>/` 에서 접속 가능.

### 6.5 영화별 매핑 (선택)

`movie_mapping.json` 을 만들어두면 카드에 영화명이 표시됨:

```json
{
  "2020-06-22": "The Current War",
  "2020-06-23": "The Current War",
  "2020-06-29": "Marie Curie"
}
```

비어 있어도 빌드는 정상 진행됨 (영화 표시만 생략).

### 6.6 신규 회차 추가 시

새 달 다운로드/전사/코너 추출 후 같은 4단계 반복:

```powershell
python optimize_mp3.py audio/corners
python upload_supabase.py audio/corners/optimized   # 기존 파일은 자동 스킵
python build_player.py --target ../goodmorningpops-player
cd ../goodmorningpops-player && git add . && git commit -m "deploy: 2020-07" && git push
```

### 6.7 비트레이트 더 줄이고 싶을 때

```powershell
# 48k (음성 약간 가벼움, 8MB 정도)
python optimize_mp3.py audio/corners --bitrate 48k --overwrite

# 32k (Supabase 무료 1GB 안에 더 많은 회차)
python optimize_mp3.py audio/corners --bitrate 32k --overwrite
```

Supabase 무료 티어는 Storage 1GB, 월 트래픽 5GB. 64k 모노 평균 8~9MB/편이면 약 110편까지.
초과 시 유료(약 $25/월부터)로 전환 가능.
