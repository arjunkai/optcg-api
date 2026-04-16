"""
Extract Don card images from the official PDF.

Usage:
  python scripts/extract-don-cards.py <pdf_path>

Extracts images to data/don_images/ and writes data/don_image_map.json.
"""

import fitz
import json
import sys
from pathlib import Path

OUT_DIR = Path("data/don_images")
MAP_FILE = Path("data/don_image_map.json")

SET_MAP = {
    "通常デザイン": {"set": "DON-STANDARD", "name": "Standard Don"},
    "イベント配布": {"set": "DON-PROMO", "name": "Event Promo"},
    "スタンダードバトルパックVol.1": {"set": "DON-SBP-01", "name": "Standard Battle Pack Vol. 1"},
    "ストレージボックス×ドン!!カードセット": {"set": "DON-SB", "name": "Storage Box Don Set"},
    "アルティメットデッキ": {"set": None, "name": "Ultra Deck"},
    "ブースターパック ROMANCE DAWN【OP-01】": {"set": "OP-01", "name": "Romance Dawn"},
    "ブースターパック 頂上決戦【OP-02】": {"set": "OP-02", "name": "Paramount War"},
    "ブースターパック 強大な敵【OP-03】": {"set": "OP-03", "name": "Pillars of Strength"},
    "ブースターパック 謀略の王国【OP-04】": {"set": "OP-04", "name": "Kingdoms of Intrigue"},
    "ブースターパック 新時代の主役【OP-05】": {"set": "OP-05", "name": "Awakening of the New Era"},
    "ブースターパック 双璧の覇者【OP-06】": {"set": "OP-06", "name": "Wings of the Captain"},
    "ブースターパック 500年後の未来【OP-07】": {"set": "OP-07", "name": "500 Years in the Future"},
    "ブースターパック 二つの伝説【OP-08】": {"set": "OP-08", "name": "Two Legends"},
    "ブースターパック 王族の血統【OP-10】": {"set": "OP-10", "name": "Royal Blood"},
    "ブースターパック 神速の拳【OP-11】": {"set": "OP-11", "name": "A Fist of Divine Speed"},
    "ブースターパック 蒼海の七傑【OP14】": {"set": "OP-14", "name": "The Azure Sea's Seven"},
    "ONE PIECE CARD THE BEST【PRB-01】": {"set": "PRB-01", "name": "The Best"},
    "ONE PIECE CARD THE BEST Vol.2【PRB-02】": {"set": "PRB-02", "name": "The Best Vol. 2"},
    "Anime25th collection【EB-02】": {"set": "EB-02", "name": "Anime 25th Collection"},
    "ONE PIECE Heroines Edition【EB-03】": {"set": "EB-03", "name": "Heroines Edition"},
    "プロモーションドン!!カードパック vol.1": {"set": "DON-PACK-01", "name": "Promo Don Pack Vol. 1"},
    "Grand Asia Open": {"set": "DON-EVENT", "name": "Grand Asia Open"},
    "雑誌付録": {"set": "DON-MAG", "name": "Magazine Promo"},
    "オフィシャルカードケース": {"set": "DON-CASE", "name": "Official Card Case"},
    "リミテッドエディション付属": {"set": "DON-LTD", "name": "Limited Edition"},
    "チャンピオンシップ2023": {"set": "DON-CHAMP", "name": "Championship 2023"},
    "ワールドファイナル参加記念品": {"set": "DON-WF", "name": "World Finals"},
    "ONE PIECE DAY'24": {"set": "DON-OPD24", "name": "One Piece Day 2024"},
    "ONE PIECE DAY'25": {"set": "DON-OPD25", "name": "One Piece Day 2025"},
    "English 2nd ANNIVERSARY SET": {"set": "DON-ANNIV-EN2", "name": "English 2nd Anniversary"},
    "China 2nd ANNIVERSARY SET": {"set": "DON-ANNIV-CN2", "name": "China 2nd Anniversary"},
    "ONE PIECE Heroines Special Set": {"set": "DON-HEROINES", "name": "Heroines Special Set"},
}


def find_set_for_label(label):
    for key, val in SET_MAP.items():
        if key in label:
            return val
    return None


def extract(pdf_path):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(pdf_path)
    entries = []
    img_index = 0

    for page_num in range(len(doc)):
        page = doc[page_num]
        text = page.get_text()
        lines = [l.strip() for l in text.split("\n")
                 if l.strip() and l.strip() != "ドン!!カードリスト"]

        images = page.get_images(full=True)
        for img_pos, img_info in enumerate(images):
            xref = img_info[0]
            pix = fitz.Pixmap(doc, xref)

            # Convert CMYK or other non-RGB colorspaces to RGB for PNG export
            if pix.alpha:
                pix = fitz.Pixmap(fitz.csRGB, pix)
            elif pix.n != 3:
                pix_no_alpha = fitz.Pixmap(fitz.csRGB, pix)
                pix = pix_no_alpha

            filename = f"don_{img_index:03d}.png"
            filepath = OUT_DIR / filename
            pix.save(str(filepath))

            set_info = None
            for line in lines:
                match = find_set_for_label(line)
                if match:
                    set_info = match
                    break

            entries.append({
                "index": img_index,
                "page": page_num + 1,
                "position": img_pos,
                "filename": filename,
                "set_label": set_info["name"] if set_info else "Unknown",
                "set_id": set_info["set"] if set_info else None,
                "text_lines": lines,
                "width": pix.width,
                "height": pix.height,
            })

            img_index += 1
            pix = None

    with MAP_FILE.open("w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)

    print(f"Extracted {img_index} images to {OUT_DIR}/")
    print(f"Map written to {MAP_FILE}")
    doc.close()


if __name__ == "__main__":
    pdf_path = sys.argv[1] if len(sys.argv) > 1 else "C:/tmp/don-cardlist.pdf"
    extract(pdf_path)
