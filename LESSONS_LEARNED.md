# LESSONS LEARNED

이 프로젝트를 만들면서 실제로 밟은 지뢰들. 비슷한 ML/오디오 파이프라인을 만들거나
이 프로젝트를 새 환경에 옮길 때 같은 함정을 다시 밟지 않기 위한 기록.

---

## 1. Whisper / ctranslate2 / CUDA Windows 환경 (가장 시간 많이 씀)

### 증상별 처방

| 증상 | 원인 | 처방 |
|------|------|------|
| `WinError 193` (torch DLL 로딩) | PyTorch 휠 손상 / 64-bit 불일치 | CPU-only 휠로 재설치 후 GPU 휠로 다시 |
| `WinError 1114` (c10.dll init 실패) | VC++ 런타임 누락 또는 conda PATH 충돌 | VC++ 2015-2022 x64 재배포 패키지 + conda 비활성화 |
| ctranslate2 4.7.2 traceback 없이 종료 | 4.7.x Windows 휠 native crash | `ctranslate2==4.5.0` 핀 |
| ctranslate2 4.4.0의 `cudnn_ops_infer64_8.dll` 누락 | 4.4.0은 cuDNN **8** 요구, PyTorch 2.x는 cuDNN **9** 번들 | 4.5.0+로 (cuDNN 9 사용) |
| `ModuleNotFoundError: pkg_resources` | setuptools 81+ 가 제거 | `setuptools<81` 핀 |
| onnxruntime DLL init 실패 | 최신 1.26.0 휠 깨짐 | `onnxruntime==1.19.2` 핀 |
| GPU 추론 시 cuDNN 못 찾음 (조용한 exit) | ctranslate2의 C++ LoadLibrary가 `os.add_dll_directory()` 무시 | `import torch` **먼저** + `os.environ["PATH"]` 에 torch lib 추가 |
| `(.venv) (base)` 가 둘 다 떠 있음 | conda 자동 활성화 + venv 활성화 | `conda config --set auto_activate_base false` |

### 작동하는 환경 조합 (Python 3.11)

```
ctranslate2==4.5.0
setuptools<81
onnxruntime==1.19.2
torch (cu121, PyTorch 공식 인덱스)
faster-whisper>=1.0.3
```

### 모든 스크립트 상단에 박혀 있는 패턴

```python
if sys.platform == "win32":
    try:
        import torch  # noqa: F401   ← faster_whisper 보다 먼저!
        _torch_lib = os.path.join(os.path.dirname(torch.__file__), "lib")
        if os.path.isdir(_torch_lib):
            os.add_dll_directory(_torch_lib)
            os.environ["PATH"] = _torch_lib + os.pathsep + os.environ.get("PATH", "")
    except ImportError:
        pass

from faster_whisper import WhisperModel
```

순서가 중요. torch가 자신의 lib를 DLL 검색 경로에 등록한 뒤에 faster_whisper 로딩.

---

## 2. PyAV / libavformat의 유니코드 경로 문제

### 증상
한글이 포함된 파일 경로를 `model.transcribe(str(path))` 에 넘기면:
```
av.error.ArgumentError: Invalid argument: 'audio\\2020-05-20__... 수_ Screen English ...mp3' returned 22
```

### 원인
libavformat이 Windows에서 비-ASCII 경로(한글, 일부 특수문자)를 fopen하지 못함.

### 처방
파일을 Python으로 열어 **파일 핸들**로 전달:
```python
with open(audio_path, "rb") as audio_fp:
    segments, info = model.transcribe(audio_fp, ...)
```
PyAV가 파일 객체를 받으면 Python I/O를 통해 읽으므로 경로 인코딩 우회.

---

## 3. RSS pubDate vs 방송일

### 증상
`--date 2020-05-20` 로 받았는데 5월 21일자 방송 mp3가 같이 받아짐.

### 원인
feedparser의 `entry.published_parsed` 는 **UTC로 정규화**됨. KBS는 KST(+09:00) 새벽에 업로드하는 회차가 종종 있어, 그게 UTC로는 전날이 되어 날짜 필터가 잘못 매칭.

### 처방
1. `entry.published` 원본 문자열을 `email.utils.parsedate_to_datetime()` 으로 파싱 → publisher의 원래 타임존 보존
2. 더 정확한 건 **제목 안 방송일 표기** 기반:
   - `5월 20일`, `2020-05-20`, `5/20`, `20200520` 패턴 자동 추출
   - `--by title` (기본) / `--by pubdate` (필요 시)

---

## 4. 코너 추출 — 단순 정규식 vs 슬라이딩 윈도우

### 증상
"오늘 스크린 잉글리시는 여기까지" 가 두 SRT segment에 걸쳐 끊겨 있으면 정규식 매칭 실패.

### 처방
**슬라이딩 윈도우** — 인접 4개 segment의 텍스트를 합쳐서 패턴 매칭. 매칭되면 윈도우 안에서
"여기까지|마무리" 가 실제로 들어있는 마지막 segment를 코너 종료점으로 잡음.

### 추가 보조 마커
일부 회차는 "Screen English" 단어 없이 진행자-게스트(Chris) 작별 인사로 끝남:
- "다음 주 월요일에 만날게요, Chris. I'll see you Monday. Bye"

이 패턴을 보조 정규식으로 추가하면 검출률 4/9 → 9/9 (6월 데이터 기준).

### START 마커 (추가 예정)
사용자가 알려준 정형구: "오늘 준비된 장면 듣고 오겠습니다" 가 진짜 코너 시작점.
현재 heuristic (인트로 60초 스킵)은 약 6분의 사연/곡 소개까지 포함하므로,
START 마커 기반으로 바꾸면 평균 17~19분 → 10~13분으로 코너 정확도 상승.

---

## 5. TTS — 한국어/영어 혼용 자연스럽게

### 증상
`ko-KR-SunHiNeural` 한 음성으로 영어를 읽으면 한국식 발음.

### 처방
**문장 단위 언어 감지 후 음성 전환**:
1. 한글 자모 개수 vs ASCII 라틴 문자 개수 비교 → 'ko' / 'en' 판별
2. 한국어 문장 → ko 음성 / 영어 문장 → en 음성
3. 청크별 합성 후 MP3 바이트 스트림 이어붙이기

### 액센트 옵션
Edge TTS는 영어 9개 액센트 + 일본어 음성에 영어 먹이기로 일본식 발음 시뮬레이션도 가능.

### 단일 음성으로 가고 싶을 때
`ko-KR-HyunsuMultilingualNeural` — 한 음성이 한·영 둘 다 원어민급. 청크 경계 호흡이 끊기지 않아 가장 매끄러움. 단, 남성 한 종류뿐.

---

## 6. PowerShell 인코딩 (cp949) 함정

### 증상 1 — 스크립트 출력 한글이 깨짐
`UnicodeEncodeError: 'cp949' codec can't encode character '—' in position 14`

→ em-dash(—) 같은 문자를 PowerShell이 못 출력. 모든 스크립트 상단:
```python
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass
```

### 증상 2 — pip install 실패
`UnicodeDecodeError: 'cp949' codec can't decode byte 0xec`

→ requirements.txt 에 한국어 주석이 있으면 pip이 cp949로 읽으려다 실패.
**requirements.txt는 ASCII 주석만**.

---

## 7. ffmpeg PATH 불안정

### 증상
`FileNotFoundError: [WinError 2]` — subprocess가 ffmpeg을 못 찾음.

### 원인
- PowerShell 세션마다 PATH 다름
- 사용자가 conda 환경 활성화 등으로 PATH 변동
- venv 활성화로 시스템 PATH 일부 가려짐

### 처방
`shutil.which("ffmpeg")` 으로 PATH 검색 → 실패 시 다음 후보 직접 탐색:
- `C:\ProgramData\chocolatey\bin\ffmpeg.exe`
- `C:\Program Files\ffmpeg\bin\ffmpeg.exe`
- `%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg*\**\bin\ffmpeg.exe`

한 번 찾으면 모듈 전역 변수에 캐시.

---

## 8. PyTorch + ctranslate2의 cuDNN 버전 매칭

### 핵심 사실
- PyTorch 2.x (cu121) 는 cuDNN 9 를 자기 `lib/` 에 번들로 가져옴
- ctranslate2 4.4.0 은 cuDNN 8 요구 → 충돌 (PyTorch lib에 없음)
- ctranslate2 4.5.0+ 는 cuDNN 9 요구 → PyTorch 번들과 매칭 OK

따라서 GPU 추론을 하려면:
- ctranslate2 4.5.0 이상
- PyTorch CUDA 12.x 휠 (자동으로 cuDNN 9 포함)
- 두 라이브러리 모두 같은 cuDNN 9 DLL 을 사용 (PyTorch가 lib/cudnn_ops64_9.dll 등을 가짐)

PyTorch가 없으면 ctranslate2는 cuDNN을 찾지 못함. **GPU 사용 시 torch는 사실상 필수**.

---

## 9. 파일 보호 — `.gitignore` 우선

### 위험 요소
- `audio/`: 저작권 + 수십 GB
- `.env`: SUPABASE_KEY, 다른 시크릿
- `credentials.json`, `token.json`: Google OAuth 시크릿
- `supabase_urls.json`: 공개 URL이지만 재생성 가능, commit 부담

### 안전 패턴
`git init` 직전에 `.gitignore` 부터 작성. 그 다음 `git add .` 해도 안전.

```gitignore
.env
audio/
references/
*.mp3
credentials.json
token.json
supabase_urls.json
temp_repo_podcast/
.venv/
__pycache__/
```

---

## 10. 코너 추출 시 본 방송 mp3가 같이 업로드될 위험

### 시나리오
실수로 `python upload_supabase.py audio` 라고 입력하면 본 방송 30~40분 mp3가
공개 버킷에 올라감 → 저작권 + 용량 폭증.

### 처방
upload_supabase.py에 **파일명 패턴 화이트리스트**:
```python
CORNER_FILENAME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}_(?:screen_english|review_time|...)\.mp3$"
)
```

코너 추출본만 통과. 본 방송은 자동 거부. 의도적 강행은 `--allow-full` 옵션.

---

## 11. 단계 사이는 파일 시스템으로 디커플링

각 단계가 서로의 함수를 import하지 않고, **파일을 매개**로 다음 단계에 전달:

```
fetch_episode.py    → audio/*.mp3
transcribe.py       → audio/*.{txt,srt,md}
extract_corner.py   → audio/corners/*.{mp3,txt,srt,md}
optimize_mp3.py     → audio/corners/optimized/*.mp3
upload_supabase.py  → supabase_urls.json + Supabase Storage
build_player.py     → temp_repo_podcast/data/*.json + html 복사
```

각 스크립트 단독 실행 가능. 한 단계 실패 시 그 지점부터 재시도. 디버깅에 결정적.

`run_pipeline.py` 오케스트레이터는 위 단계들을 회차별로 순서대로 호출하지만,
**각 단계는 여전히 디스크에 흔적을 남김**. 중간에 끊겨도 다음 실행 시 이어짐.

---

## 12. 진단 출력은 "다음에 무엇을 해야 하는지" 까지

좋은 예 — extract_corner.py:
```
스킵 (.srt 없음, 전사 진행 중일 수 있음): 2020-06-08_Screen English.mp3
스킵 (끝 마커 못 찾음): 2020-06-08_Screen English.mp3
```

사용자가 보고 즉시 "아 전사가 안 됐구나" 또는 "이 회차는 마커 패턴이 다르구나"
판단 가능. 그냥 "실패" 만 외치면 디버깅에 사람 시간이 들어감.

`fail-loudly with next-action hints` 라는 패턴.

---

## 13. 환경 재현을 위한 핀 전략

`requirements.txt`에 **모든 ML 의존성은 `==`로 정확 핀**:

```
ctranslate2==4.5.0       # 4.7.x silent exit, 4.4.x cuDNN 8 mismatch
onnxruntime==1.19.2      # 1.26.0 Windows wheel broken
setuptools<81            # ctranslate2가 pkg_resources 사용
```

`>=`는 미래의 자기를 한밤중에 깨우는 버튼. 일반 라이브러리는 `>=` OK,
ML 핵심 의존성은 `==`.

---

## 14. Pages 호스팅 = 두 저장소 분리

- **코드 repo** (이 폴더): 코드, web/ 템플릿, README, requirements.txt
- **배포 repo** (`temp_repo_podcast/` 또는 별도 디렉토리): `data/*.json`, `play.html`, `index.html`, `style.css`, `app.js`

`build_player.py`가 코드 repo 안에서 빌드 산출물을 배포 repo 디렉토리에 복사 후,
배포 repo만 git push.

`temp_repo_podcast/`는 코드 repo의 `.gitignore`에 들어 있어야 함. 안 그러면
코드 repo가 데이터까지 들고 다니게 됨.

---

## 정리 — 새 환경에 옮길 때 0일차 체크리스트

1. Python 3.11 64-bit (3.13/3.14는 torch 휠이 아직 부실)
2. `conda config --set auto_activate_base false` (PATH 오염 방지)
3. VC++ 2015-2022 x64 재배포 패키지 설치
4. ffmpeg 설치 + PATH (`winget install Gyan.FFmpeg`)
5. NVIDIA 드라이버 + CUDA 12.x 호환 (`nvidia-smi` 확인)
6. `pip install -r requirements.txt` (위 핀들 포함)
7. PyTorch CUDA 휠 별도: `pip install torch --index-url https://download.pytorch.org/whl/cu121`
8. `python -c "import torch; from faster_whisper import WhisperModel; m = WhisperModel('tiny', device='cuda', compute_type='float16'); list(m.transcribe('test.mp3'))[0]; print('OK')"` 으로 검증
9. `.env` 생성 (SUPABASE_URL, SUPABASE_KEY, SUPABASE_BUCKET)
10. `credentials.json` (Google Drive 쓸 거면) 배치
