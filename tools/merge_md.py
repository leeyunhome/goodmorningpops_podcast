"""추출된 개별 마크다운(.md) 파일들을 하나의 큰 마크다운 파일로 병합합니다.

사용법:
  python tools/merge_md.py [입력_폴더] [출력_파일]

예시:
  # audio/corners 폴더의 모든 md 파일을 하나로 합쳐서 all_corners.md 로 저장
  python tools/merge_md.py audio/corners all_corners.md

  # 특정 연도만 필터링하고 싶을 때 (옵션)
  python tools/merge_md.py audio/corners all_2020.md --filter "2020-"
"""

import argparse
import sys
from pathlib import Path

# Windows cp949 콘솔에서도 한글/특수문자 출력이 깨지지 않도록 강제 UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass

def main():
    parser = argparse.ArgumentParser(description="여러 마크다운 파일을 하나로 병합합니다.")
    parser.add_argument("input_dir", help="입력 마크다운 파일들이 있는 폴더 (예: audio/corners)")
    parser.add_argument("output_file", help="저장할 병합 마크다운 파일 경로 (예: merged_corners.md)")
    parser.add_argument("--filter", default="", help="파일명에 이 문자열이 포함된 파일만 병합 (예: '2020-06')")
    
    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    output_path = Path(args.output_file)

    if not input_dir.is_dir():
        print(f"오류: '{input_dir}' 폴더를 찾을 수 없습니다.", file=sys.stderr)
        sys.exit(1)

    # .md 파일들을 찾아서 이름순(날짜순)으로 정렬
    md_files = sorted(
        p for p in input_dir.glob("*.md")
        if args.filter in p.name
    )

    if not md_files:
        print(f"병합할 .md 파일이 '{input_dir}'에 없습니다. (필터: '{args.filter}')", file=sys.stderr)
        sys.exit(0)

    print(f"총 {len(md_files)}개의 파일을 병합합니다...")

    # 부모 폴더가 없으면 생성
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as outfile:
        outfile.write(f"# 굿모닝 팝스 추출 대본 모음\n\n")
        outfile.write(f"- 병합된 파일 수: {len(md_files)}개\n")
        outfile.write(f"- 생성일: {Path(__file__).parent.parent.name}\n\n")
        outfile.write("---\n\n")

        for i, md_file in enumerate(md_files, 1):
            try:
                content = md_file.read_text(encoding="utf-8")
                
                # 원본 파일이 이미 # 제목을 가지고 있다면 그대로 쓰고, 
                # 구분을 위해 파일명 정보를 주석이나 헤더로 추가할 수도 있음.
                outfile.write(f"<!-- Source: {md_file.name} -->\n")
                outfile.write(content.strip() + "\n\n")
                
                # 에피소드 간 확실한 구분선 추가
                outfile.write("---\n\n")
                
                print(f"  [{i}/{len(md_files)}] 병합 완료: {md_file.name}")
            except Exception as e:
                print(f"  [오류] {md_file.name} 읽기 실패: {e}", file=sys.stderr)

    print(f"\n완료! 병합된 파일이 저장되었습니다: {output_path.absolute()}")

if __name__ == "__main__":
    main()
