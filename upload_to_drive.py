"""로컬 텍스트/전사 파일을 본인 Google Drive 폴더로 업로드한다.

- private 업로드 전용. 공개 공유 링크는 만들지 않는다.
- 같은 이름 파일이 이미 있으면 기본은 스킵 (--overwrite로 강제 갱신).
- 인증 토큰(token.json)은 첫 실행 시 OAuth로 생성되어 캐시된다.

설정 (최초 1회):
  1. https://console.cloud.google.com 에서 새 프로젝트 생성
  2. "API 및 서비스" → "라이브러리"에서 Google Drive API 활성화
  3. "OAuth 동의 화면" 구성 (External, 본인 이메일을 테스트 사용자로 추가)
  4. "사용자 인증 정보" → "OAuth 2.0 클라이언트 ID 만들기"
       - 애플리케이션 유형: "데스크톱 앱"
  5. 생성된 클라이언트의 JSON을 다운로드해서 이 폴더에 credentials.json 으로 저장
  6. pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib

사용 예:
  python upload_to_drive.py audio/corners
  python upload_to_drive.py audio/corners --ext txt,md --folder-name "GMP 전사"
  python upload_to_drive.py audio --ext txt --dry-run
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCOPES = [
    # drive.file = 이 앱이 만들거나 명시적으로 연 파일에만 접근.
    # 전체 Drive 권한이 아니라 더 안전.
    "https://www.googleapis.com/auth/drive.file",
]


def _import_google():
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload

        return Request, Credentials, InstalledAppFlow, build, MediaFileUpload
    except ImportError as e:
        raise SystemExit(
            "Google API 패키지가 필요합니다:\n"
            "  pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib"
        ) from e


def get_service(credentials_path: Path, token_path: Path):
    Request, Credentials, InstalledAppFlow, build, _ = _import_google()

    creds = None
    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        except Exception:
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not credentials_path.exists():
                raise SystemExit(
                    f"인증 파일이 없습니다: {credentials_path}\n"
                    "Google Cloud Console에서 OAuth 클라이언트 JSON을 받아 "
                    "이 폴더에 credentials.json 으로 저장해주세요. (스크립트 docstring 참조)"
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(credentials_path), SCOPES
            )
            # 브라우저가 열리며 본인 Google 계정 인증 후 권한 동의
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json(), encoding="utf-8")

    return build("drive", "v3", credentials=creds)


def _q_escape(s: str) -> str:
    """Drive 쿼리 문자열 안에서 작은따옴표를 이스케이프."""
    return s.replace("\\", "\\\\").replace("'", "\\'")


def get_or_create_folder(service, name: str, parent_id: str | None = None) -> str:
    name_q = _q_escape(name)
    query = (
        f"name='{name_q}' "
        f"and mimeType='application/vnd.google-apps.folder' "
        f"and trashed=false"
    )
    if parent_id:
        query += f" and '{parent_id}' in parents"
    results = (
        service.files()
        .list(q=query, fields="files(id, name)", pageSize=10)
        .execute()
    )
    items = results.get("files", [])
    if items:
        return items[0]["id"]

    metadata: dict = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
    }
    if parent_id:
        metadata["parents"] = [parent_id]
    folder = service.files().create(body=metadata, fields="id").execute()
    return folder["id"]


def list_existing(service, folder_id: str) -> dict[str, str]:
    """폴더 안 파일의 name → fileId 매핑."""
    out: dict[str, str] = {}
    page_token = None
    while True:
        params = {
            "q": f"'{folder_id}' in parents and trashed=false",
            "fields": "nextPageToken, files(id, name)",
            "pageSize": 1000,
        }
        if page_token:
            params["pageToken"] = page_token
        result = service.files().list(**params).execute()
        for f in result.get("files", []):
            out[f["name"]] = f["id"]
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return out


def mimetype_for(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".txt":
        return "text/plain"
    if ext == ".md":
        return "text/markdown"
    if ext == ".srt":
        return "application/x-subrip"
    if ext == ".json":
        return "application/json"
    return "application/octet-stream"


def upload_one(
    service,
    media_cls,
    file_path: Path,
    folder_id: str,
    existing: dict[str, str],
    overwrite: bool,
) -> str:
    name = file_path.name
    media = media_cls(str(file_path), mimetype=mimetype_for(file_path), resumable=False)
    if name in existing:
        if overwrite:
            service.files().update(
                fileId=existing[name],
                media_body=media,
            ).execute()
            return "updated"
        return "skipped"
    service.files().create(
        body={"name": name, "parents": [folder_id]},
        media_body=media,
        fields="id, name",
    ).execute()
    return "uploaded"


def main() -> int:
    p = argparse.ArgumentParser(
        description="텍스트 파일을 본인 Google Drive로 private 업로드"
    )
    p.add_argument("input", help="업로드할 폴더 (예: audio/corners)")
    p.add_argument(
        "--folder-id",
        default=None,
        help=(
            "기존 Google Drive 폴더 ID. URL의 /folders/뒤 부분. "
            "지정 시 --folder-name 무시. 권장."
        ),
    )
    p.add_argument(
        "--folder-name",
        default="GoodMorningPops Transcripts",
        help=(
            "Google Drive 내 대상 폴더 이름 (--folder-id 없을 때만 사용). "
            "같은 이름이 있으면 재사용, 없으면 생성."
        ),
    )
    p.add_argument(
        "--ext",
        default="txt",
        help="업로드할 확장자 콤마 구분 (기본: txt). 예: txt,md,srt",
    )
    p.add_argument(
        "--recursive",
        action="store_true",
        help="하위 폴더까지 재귀적으로 탐색",
    )
    p.add_argument(
        "--credentials",
        default="credentials.json",
        help="OAuth 클라이언트 JSON 경로 (기본: credentials.json)",
    )
    p.add_argument(
        "--token",
        default="token.json",
        help="캐시된 토큰 경로 (기본: token.json)",
    )
    p.add_argument(
        "--overwrite",
        action="store_true",
        help="이미 같은 이름 파일이 있으면 덮어쓰기 (기본: 스킵)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="대상 파일만 출력하고 실제 업로드는 안 함",
    )
    args = p.parse_args()

    input_dir = Path(args.input)
    if not input_dir.is_dir():
        raise SystemExit(f"폴더가 아닙니다: {input_dir}")

    extensions = {
        f".{e.strip().lstrip('.').lower()}" for e in args.ext.split(",") if e.strip()
    }
    if not extensions:
        raise SystemExit("--ext 가 비어 있습니다.")

    if args.recursive:
        candidates = [p for p in input_dir.rglob("*") if p.is_file()]
    else:
        candidates = [p for p in input_dir.iterdir() if p.is_file()]
    files = sorted(p for p in candidates if p.suffix.lower() in extensions)

    if not files:
        print(f"업로드할 파일 없음 (확장자 필터: {','.join(sorted(extensions))})")
        return 1

    target_label = (
        f"folder-id={args.folder_id}" if args.folder_id else f"'{args.folder_name}'"
    )
    print(f"대상 폴더: {target_label}  |  확장자: {','.join(sorted(extensions))}")
    print(f"파일 {len(files)}개:")
    for f in files[:10]:
        print(f"  {f.relative_to(input_dir)}")
    if len(files) > 10:
        print(f"  ... 외 {len(files) - 10}개")

    if args.dry_run:
        print("\n--dry-run: 인증 및 업로드 생략")
        return 0

    print()
    _, _, _, _, MediaFileUpload = _import_google()
    service = get_service(Path(args.credentials), Path(args.token))

    if args.folder_id:
        folder_id = args.folder_id
        print(f"기존 폴더 사용 (folder-id={folder_id})")
    else:
        print(f"대상 폴더 확인/생성: {args.folder_name}")
        folder_id = get_or_create_folder(service, args.folder_name)

    print("기존 파일 목록 조회 중...")
    existing = list_existing(service, folder_id)
    print(f"  기존 파일 {len(existing)}개")

    print(f"\n업로드 시작 ({len(files)}개)")
    counts = {"uploaded": 0, "updated": 0, "skipped": 0, "error": 0}
    for i, f in enumerate(files, 1):
        try:
            action = upload_one(
                service, MediaFileUpload, f, folder_id, existing, args.overwrite
            )
            counts[action] += 1
            mark = {"uploaded": "+", "updated": "~", "skipped": "."}[action]
            print(f"  [{i}/{len(files)}] {mark} {f.name}")
        except Exception as e:
            counts["error"] += 1
            print(f"  [{i}/{len(files)}] ! {f.name}: {e}")

    print(
        f"\n완료: 신규 {counts['uploaded']}  /  갱신 {counts['updated']}  "
        f"/  스킵 {counts['skipped']}  /  에러 {counts['error']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
