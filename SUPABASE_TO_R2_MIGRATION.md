# Supabase Storage → Cloudflare R2 마이그레이션 가이드

## 배경

Supabase 무료 티어 프로젝트가 일시 정지(`paused`)되어 오디오 재생이 불가능해짐.
조직 차원의 서비스 제한으로 인해 재개 불가.

## 목표

| 항목 | 기존 | 변경 후 |
|------|------|---------|
| 스토리지 | Supabase Storage (`gmp-audio` 버킷) | Cloudflare R2 |
| MP3 URL | `https://rtyzdyyfahddqshgyjvn.supabase.co/storage/v1/object/public/gmp-audio/...` | `https://pub-XXXX.r2.dev/...` |
| 이미지 URL | `https://rtyzdyyfahddqshgyjvn.supabase.co/storage/v1/object/public/gmp-audio/artwork/...` | `https://pub-XXXX.r2.dev/artwork/...` |
| 코드 | `upload_supabase.py`, `generate_artwork.py` | 동일 파일 수정 (boto3 사용) |

---

## STEP 1: Cloudflare R2 버킷 생성

1. [Cloudflare 대시보드](https://dash.cloudflare.com) 로그인
2. 좌측 메뉴 → **R2 Object Storage** → **Overview**
3. **Create bucket** 클릭
4. 버킷 이름: `gmp-audio` (기존과 동일하게 맞추는 것 권장)
5. Region: Automatic

---

## STEP 2: Public Development URL 활성화

1. 생성된 버킷 클릭 → **Settings** 탭
2. **Public Development URL** → **Enable** 클릭
3. 확인창에 `allow` 입력 후 **Allow** 클릭
4. 표시되는 URL 메모: `https://pub-XXXX.r2.dev`

---

## STEP 3: CORS 정책 설정

1. Settings → **CORS Policy** → 편집 아이콘
2. 기존 내용 전체 삭제 후 아래 붙여넣기:

```json
[
  {
    "AllowedOrigins": ["*"],
    "AllowedMethods": ["GET"],
    "AllowedHeaders": ["*"]
  }
]
```

3. **Save** 클릭

---

## STEP 4: R2 API 토큰 생성

1. R2 Overview 페이지 우측 → **Manage R2 API tokens**
2. **Create API token** 클릭
3. 설정:
   - Token name: `gmp-audio-upload`
   - Permissions: **Object Read & Write**
   - Specify bucket: `gmp-audio`
4. **Create API Token** 클릭
5. 아래 두 값 복사 (창 닫으면 재확인 불가):
   - `Access Key ID`
   - `Secret Access Key`

---

## STEP 5: .env 수정

`.env` 파일에 R2 설정 추가:

```env
# 기존 Supabase 설정 (주석 처리)
# SUPABASE_URL=https://rtyzdyyfahddqshgyjvn.supabase.co
# SUPABASE_KEY=eyJhbGci...
# SUPABASE_SERVICE_KEY=eyJhbGci...
# SUPABASE_BUCKET=gmp-audio

# Cloudflare R2
R2_ACCOUNT_ID=f5280adda1535bcb64e9ae4f9d49f8fb   # Cloudflare 대시보드 URL에서 확인
R2_ACCESS_KEY_ID=<위에서 복사한 Access Key ID>
R2_SECRET_ACCESS_KEY=<위에서 복사한 Secret Access Key>
R2_BUCKET_NAME=gmp-audio
R2_PUBLIC_URL=https://pub-XXXX.r2.dev            # STEP 2에서 메모한 URL
```

> `R2_ACCOUNT_ID`: Cloudflare 대시보드 URL `dash.cloudflare.com/<ACCOUNT_ID>/r2/...` 에서 확인

---

## STEP 6: upload_supabase.py 수정

`upload_supabase.py` 전체를 boto3 기반으로 교체:

```python
"""
Cloudflare R2에 MP3 업로드 (boto3 S3-compatible API 사용).
supabase_urls.json 에 {filename: public_url} 형태로 누적 저장.
"""
import json, os, re, sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

ACCOUNT_ID      = os.getenv("R2_ACCOUNT_ID")
ACCESS_KEY_ID   = os.getenv("R2_ACCESS_KEY_ID")
SECRET_KEY      = os.getenv("R2_SECRET_ACCESS_KEY")
BUCKET_NAME     = os.getenv("R2_BUCKET_NAME", "gmp-audio")
PUBLIC_URL_BASE = os.getenv("R2_PUBLIC_URL", "").rstrip("/")

CORNER_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}_"
    r"(screen_english|review_time|review_time_screen"
    r"|friday_news_pick|laura_scrapbook|pop_song)\.mp3$"
)

URLS_FILE = Path(__file__).parent / "supabase_urls.json"


def get_client():
    import boto3
    return boto3.client(
        "s3",
        endpoint_url=f"https://{ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=ACCESS_KEY_ID,
        aws_secret_access_key=SECRET_KEY,
        region_name="auto",
    )


def load_urls() -> dict:
    if URLS_FILE.exists():
        return json.loads(URLS_FILE.read_text(encoding="utf-8"))
    return {}


def save_urls(mapping: dict):
    URLS_FILE.write_text(
        json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def upload_one(mp3_path: Path, allow_full: bool = False) -> str | None:
    """MP3 한 개를 R2에 업로드하고 public URL을 반환."""
    name = mp3_path.name
    if not allow_full and not CORNER_RE.match(name):
        print(f"  [SKIP] 안전 필터: {name}")
        return None

    client = get_client()
    r2_key = name

    # 이미 존재하면 스킵
    try:
        client.head_object(Bucket=BUCKET_NAME, Key=r2_key)
        url = f"{PUBLIC_URL_BASE}/{r2_key}"
        print(f"  [EXISTS] {name}")
        return url
    except Exception:
        pass

    print(f"  [UPLOAD] {name} ...", end=" ", flush=True)
    client.upload_file(
        str(mp3_path), BUCKET_NAME, r2_key,
        ExtraArgs={"ContentType": "audio/mpeg"},
    )
    url = f"{PUBLIC_URL_BASE}/{r2_key}"
    print(f"OK → {url}")

    mapping = load_urls()
    mapping[name] = url
    save_urls(mapping)
    return url


def get_public_url(filename: str) -> str | None:
    return load_urls().get(filename)


def load_env():
    """run_pipeline.py 호환용 no-op."""
    pass
```

---

## STEP 7: generate_artwork.py 수정

`upload_to_supabase()` 함수 내부를 R2로 교체:

```python
def upload_to_supabase(image_bytes: bytes, ep_id: str) -> str:
    """앨범아트 JPEG를 R2에 업로드하고 public URL 반환."""
    import boto3, os
    from dotenv import load_dotenv
    load_dotenv()

    account_id   = os.getenv("R2_ACCOUNT_ID")
    access_key   = os.getenv("R2_ACCESS_KEY_ID")
    secret_key   = os.getenv("R2_SECRET_ACCESS_KEY")
    bucket       = os.getenv("R2_BUCKET_NAME", "gmp-audio")
    public_base  = os.getenv("R2_PUBLIC_URL", "").rstrip("/")

    client = boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="auto",
    )

    r2_key = f"artwork/{ep_id}.jpg"
    import io
    client.upload_fileobj(
        io.BytesIO(image_bytes), bucket, r2_key,
        ExtraArgs={"ContentType": "image/jpeg"},
    )
    url = f"{public_base}/{r2_key}"
    print(f"  [ARTWORK] R2 업로드 완료: {url}")
    return url
```

---

## STEP 8: boto3 설치

```bash
pip install boto3
```

또는 `requirements.txt`에 추가:

```
boto3>=1.34.0
```

---

## STEP 9: 기존 파일 R2로 이전 (일괄 업로드)

기존에 Supabase에 올려둔 파일들을 R2로 복사하는 스크립트:

```bash
python tools/migrate_to_r2.py
```

`tools/migrate_to_r2.py` 내용 (새로 생성):

```python
"""
audio/corners/optimized/ 의 MP3 + artwork/ 의 JPEG를 R2로 일괄 업로드.
supabase_urls.json, artwork_urls.json 을 R2 URL로 업데이트.
"""
import json, os, sys
from pathlib import Path
from dotenv import load_dotenv

PROJECT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT))
load_dotenv(PROJECT / ".env")

import boto3

ACCOUNT_ID   = os.getenv("R2_ACCOUNT_ID")
ACCESS_KEY   = os.getenv("R2_ACCESS_KEY_ID")
SECRET_KEY   = os.getenv("R2_SECRET_ACCESS_KEY")
BUCKET       = os.getenv("R2_BUCKET_NAME", "gmp-audio")
PUBLIC_BASE  = os.getenv("R2_PUBLIC_URL", "").rstrip("/")

client = boto3.client(
    "s3",
    endpoint_url=f"https://{ACCOUNT_ID}.r2.cloudflarestorage.com",
    aws_access_key_id=ACCESS_KEY,
    aws_secret_access_key=SECRET_KEY,
    region_name="auto",
)


def upload(local_path: Path, r2_key: str, content_type: str):
    try:
        client.head_object(Bucket=BUCKET, Key=r2_key)
        print(f"  [SKIP] {r2_key}")
        return f"{PUBLIC_BASE}/{r2_key}"
    except Exception:
        pass
    print(f"  [UP] {r2_key} ...", end=" ", flush=True)
    client.upload_file(str(local_path), BUCKET, r2_key,
                       ExtraArgs={"ContentType": content_type})
    print("OK")
    return f"{PUBLIC_BASE}/{r2_key}"


# 1. MP3 업로드
optimized_dir = PROJECT / "audio" / "corners" / "optimized"
urls = {}
if optimized_dir.exists():
    mp3s = sorted(optimized_dir.glob("*.mp3"))
    print(f"\n[MP3] {len(mp3s)}개 업로드")
    for mp3 in mp3s:
        url = upload(mp3, mp3.name, "audio/mpeg")
        urls[mp3.name] = url
    (PROJECT / "supabase_urls.json").write_text(
        json.dumps(urls, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"supabase_urls.json 업데이트 완료 ({len(urls)}개)")

# 2. 이미지 업로드
artwork_dir = PROJECT / "artwork"
art_urls = {}
if artwork_dir.exists():
    jpegs = sorted(artwork_dir.glob("*.jpg"))
    print(f"\n[ARTWORK] {len(jpegs)}개 업로드")
    for jpg in jpegs:
        ep_id = jpg.stem
        r2_key = f"artwork/{jpg.name}"
        url = upload(jpg, r2_key, "image/jpeg")
        art_urls[ep_id] = url
    (PROJECT / "artwork_urls.json").write_text(
        json.dumps(art_urls, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"artwork_urls.json 업데이트 완료 ({len(art_urls)}개)")

print("\n완료!")
```

---

## STEP 10: GitHub Pages 재배포

`supabase_urls.json`과 `artwork_urls.json`이 R2 URL로 업데이트된 후:

```bash
python build_player.py          # data/*.json 재빌드 (R2 URL 반영)
python run_pipeline.py --push-only   # 배포 repo push (있는 경우)
```

또는 수동으로 배포 repo에서:

```bash
cd <배포 repo 경로>
git add .
git commit -m "Migrate audio URLs to Cloudflare R2"
git push origin main
```

---

## 체크리스트

- [ ] R2 버킷 생성 (`gmp-audio`)
- [ ] Public Development URL 활성화 → URL 메모
- [ ] CORS 정책 설정
- [ ] R2 API 토큰 생성 (Access Key ID + Secret Access Key)
- [ ] `.env`에 R2 환경변수 추가
- [ ] `upload_supabase.py` 교체
- [ ] `generate_artwork.py`의 `upload_to_supabase()` 교체
- [ ] `pip install boto3`
- [ ] `python tools/migrate_to_r2.py` 실행 (기존 파일 이전)
- [ ] `python build_player.py` 실행 (JSON 재빌드)
- [ ] 배포 repo push
- [ ] 플레이어 재생 확인

---

## 참고: 이 프로젝트(whisper-segment-extractor)에서 겪은 이슈

1. **R2 Public Development URL은 rate-limited** — 프로덕션 트래픽이 많으면 Custom Domain 설정 권장
2. **한글 파일명 URL** — 브라우저가 자동 인코딩하므로 실사용에는 문제없음
3. **GitHub Actions deploy 실패** — "Deployment failed, try again later"는 GitHub 인프라 일시 오류. Re-run 또는 빈 커밋으로 재트리거
4. **boto3 설치 환경** — `pip install boto3`가 안 될 경우 올바른 Python 경로 확인 (`C:\Users\<user>\miniconda3\python.exe -m pip install boto3`)
