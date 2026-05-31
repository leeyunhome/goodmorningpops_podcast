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

## 15. 정규식(Regex)의 한계와 N-gram 기반 문맥 감지

### 증상
"오늘 준비된 장면 듣고 오겠습니다"와 같은 시작 마커나 코너 종료 마커가 "오늘 장면 듣고 올게요", "듣고 오겠습니다" 등 미묘하게 변형되거나, Whisper 전사 과정에서 오류가 발생하면 정규식 패턴에 매칭되지 않음. 그 결과 약 70%의 에피소드에서 표준 마커 검출에 실패하고, 사연에 "스크린 잉글리시" 단어만 등장해도 엉뚱한 곳을 시작점으로 오인(6월 8일자 오추출)하는 문제 발생.

### 처방
**Character N-gram + Word N-gram 유사도 측정 기법 도입**
- 정답 레이블(예: `"장면 듣고 오겠습니다"`)의 N-gram 집합과 실제 오디오 세그먼트의 N-gram 집합 간 **Jaccard 유사도**를 계산.
- 이진 판별(매칭/불매칭) 대신 **연속적인 유사도 점수(0~1)**를 통해 가장 매칭될 확률이 높은 세그먼트를 경계로 선정.

### 장점
1. **변이에 대한 내성 (Fuzzy Matching):** 조사나 어미가 조금 달라도 높은 유사도 점수를 얻으므로 정규식을 매번 수정할 필요가 없음.
2. **문맥 파악에 의한 오감지 방어:** "스크린 잉글리시"라는 단어 하나만 등장하는 사연 맥락과, "스크린 잉글리시 마무리할게요"라는 코너 종료 맥락은 N-gram 프로파일이 완전히 다름.
3. **자가 학습 구조 (Zero-Code Maintenance):** 정답 레이블용 JSON 파일(`ngram_labels.json`)에 특이 케이스 문장을 한 줄 추가하기만 하면 파이썬 코드 수정 없이 감지 능력이 자동으로 올라감.

적용 결과 237개 에피소드 기준 시작 감지율 84%, 종료 감지율 92%로 커버리지가 대폭 상승함.

---

## 16. 정리 — 새 환경에 옮길 때 0일차 체크리스트

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

---

## 17. 대규모 배치 실행 시 C드라이브 용량 고갈 및 MP3 누적 대책 (OSError: [Errno 28])

### 증상
4시간 이상 장기간 수백 개의 에피소드를 몰아서 처리하는 과정에서, 편당 30~50MB 크기의 대용량 원본 방송 `.mp3` 파일들이 `audio/` 폴더에 지속적으로 다운로드되어 누적됨. 이로 인해 수십 기가바이트의 디스크 공간을 소모하여 결국 C드라이브가 0바이트로 꽉 차 파이프라인이 중단되는 현상 발생.

### 처방
각 에피소드의 전사(`.srt`, `.txt`, `.md`), 코너 음성 추출(`.mp3`), 비트레이트 최적화, Supabase 업로드 및 배포 빌드가 완료되면 **원본 대용량 방송 MP3 파일은 로컬에 남겨둘 필요가 없음.**
* 주기적으로 루트 `audio/` 폴더의 원본 파일만 안전하게 제거하여 수십 GB의 공간을 즉시 확보 가능.
* 안전 삭제 명령어 (하위 `corners/` 폴더는 지우지 않는 규격):
  ```powershell
  Remove-Item -Path "audio\*.mp3" -Force
  ```

---

## 18. 디스크 풀(Full) 중단 후 재개 시 손상된 파일 스킵 함정 (av.error.InvalidDataError)

### 증상
디스크 용량이 고갈되는 순간 다운로드 중이던 파일(예: 73KB 크기)이 완전히 다운로드되지 못한 채 **손상된 상태**로 남음. 이후 디스크 공간을 확보하고 파이프라인을 재개할 때, 스크립트가 파일이 이미 존재한다고 판단하여 다운로드를 건너뛰고 바로 Whisper 전사 단계를 실행함. 이로 인해 PyAV 디코더가 깨진 오디오 데이터를 파싱하지 못해 `av.error.InvalidDataError` 에러와 함께 크래시 발생.

### 처방
디스크 용량 초과 등으로 비정상 종료된 후 파이프라인을 다시 돌릴 때에는, **이전 단계에서 깨진 임시 파일들이 남아있을 확률이 높으므로** 반드시 `audio/` 루트의 MP3 파일들을 완전히 비우고(`Remove-Item -Path "audio\*.mp3" -Force`) 시작해야 함.

---

## 19. 복수 코너(다형성) 파싱 시 정규식 컴파일 일관성 및 KeyError 방지

### 증상
`extract_corner.py` 내부의 다형성 설정 구조(`CORNER_DEFS`)에서 신규 코너(`review_time_screen`)를 추가할 때, 패턴을 정규식 컴파일 객체(`re.compile(...)`) 대신 일반 raw string(`r"..."`)으로 등록하면 런타임 에러(`AttributeError: 'str' object has no attribute 'search'`) 발생. 또한 `fallback` 설정 키가 코너 규격마다 다를 경우(예: `fallback_start` vs `intro_skip_seconds`) 정형화된 공통 메서드 호출 과정에서 `KeyError` 발생.

### 처방
1. `CORNER_DEFS` 내의 모든 `start_patterns` 및 `end_patterns`는 **반드시 일관되게 `re.compile()`을 씌워서 등록**해야 함.
2. 모든 코너에 적용되는 시작/종료 탐색 제한 상한 및 폴백 파라미터 키 구조(`start_max_minutes`, `start_window_segments`, `max_end_minutes`, `end_window_segments`, `intro_skip_seconds`, `max_duration_minutes`)를 엄격하게 상호 일치시켜야 런타임에 다형성 분기가 안전하게 작동함.

---

## 20. Edge TTS — 음높이(Pitch) 규격 제한 및 기호 단독 청크 예외 처리

### 증상 1 (피치 단위 규격 에러)
`tts.py`에서 청아하고 우아한 하이톤(예: 엘사 모드)을 표현하기 위해 `pitch="+10%"`로 매개변수를 넘겼을 때 다음과 같은 에러 발생:
```text
ValueError: Invalid pitch '+10%'.
validate_string_param("pitch", self.pitch, r"^[+-]\d+Hz$")
```

### 원인 1
`edge-tts` 라이브러리의 `Communicate` 생성자 내부에서 피치(`pitch`) 매개변수를 검증할 때, 퍼센트(`%`) 단위는 허용하지 않고 정규식 `^[+-]\d+Hz$` 패턴만 통과시킴. 즉, **오직 Hz 단위(예: `+15Hz`, `-10Hz`)로만 피치를 설정할 수 있음.**

### 처방 1
피치를 Hz 단위인 `+15Hz`로 변경하여 `edge-tts` 내부 정규식 검증을 통과시킴. 청아하고 깨끗한 톤(엘사 모드)은 `+15Hz` 정도로 충분히 훌륭하게 연출 가능.

---

### 증상 2 (기호 단독 청크 합성 에러)
피치 규격을 고친 뒤에도 텍스트 맨 뒤의 `>`와 같은 단독 특수문자나 발음 불가능한 한 글자 짜리 청크(`[2/3] [SunHi] 1자`)가 합성 단계로 넘어가면 다음과 같은 에러와 함께 전체 변환 작업이 크래시(Crash)를 일으키며 중단됨:
```text
edge_tts.exceptions.NoAudioReceived: No audio was received. Please verify that your parameters are correct.
```

### 원인 2
한영 감지 텍스트 분할 알고리즘(`split_by_language`)이 문장 종결 기호 등을 쪼개는 과정에서 알파벳이나 숫자가 전혀 포함되어 있지 않은 단독 특수 기호 청크를 생성할 수 있음. TTS 엔진에 이러한 텍스트를 전송하면, 소리를 전혀 추출할 수 없어 `NoAudioReceived` 예외가 발생함.

### 처방 2
`tts.py`의 `synthesize` 함수 루프에 `try-except` 구문을 씌워 예외 처리를 보강함. 발음 불가능한 기호로 인해 `edge-tts` 엔진이 음성 수신 실패 예외를 던지더라도, 스크립트 전체가 죽지 않고 단순 경고 로그(`[경고] 합성 건너뜀...`)만 출력한 뒤 다음 청크의 합성을 안전하게 이어가도록 개선함.


