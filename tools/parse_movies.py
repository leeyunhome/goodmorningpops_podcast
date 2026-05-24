import json
from pathlib import Path
import re

def parse_movies():
    table_path = Path("references/GMP_Screen_English_Movies_Table.md")
    if not table_path.exists():
        print("Movies table not found.")
        return

    mapping = {}
    lines = table_path.read_text(encoding="utf-8").strip().split("\n")
    for line in lines:
        if not line.startswith("|") or "연도" in line or "---" in line:
            continue
        
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 6:
            continue
            
        year_str = parts[2]
        month_str = parts[3]
        kor_title = parts[4].replace("(","").replace(")","").strip()
        eng_title = parts[5].replace("—", "").strip()

        if not year_str.isdigit():
            continue
        
        # Handle "1~9월호" or "10월호"
        months = []
        m_match = re.findall(r"\d+", month_str)
        if m_match:
            if len(m_match) == 1:
                months.append(int(m_match[0]))
            elif len(m_match) == 2:
                months.extend(range(int(m_match[0]), int(m_match[1])+1))
                
        for m in months:
            ym = f"{year_str}-{m:02d}"
            title = kor_title
            if eng_title:
                title = f"{kor_title} ({eng_title})"
            if title and title != "—" and "본문 미등록" not in title:
                mapping[ym] = title

    out_path = Path("movie_mapping.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(mapping, f, ensure_ascii=False, indent=2)
    print(f"Generated movie_mapping.json with {len(mapping)} entries.")

if __name__ == "__main__":
    parse_movies()
