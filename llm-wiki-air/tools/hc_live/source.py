"""Write data/mount-hc/hc-test.md for a test round: python source.py <round>."""
import copy, os, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from make_doc import FACTS, build

def facts(round_: int) -> dict:
    f = copy.deepcopy(FACTS)
    if round_ >= 1:
        f["B1"][1] = "戻り温度が 28 °C を超えた場合は負荷を確認する。"   # disjoint from the human edit (case 3)
        f["B2"][0] = "高温警報のしきい値は 55 °C である。"            # human wrote 65 (case 4)
        f["C1"][0] = "定期点検は 2 か月ごとに実施する。"               # human made the same change (case 2)
    return f

NEW_FILLER = [
    "作業前に安全帯と保護手袋を着用し、ロックアウト札を掛ける。",
    "測定値は計測端末から保守システムへ直接登録する。",
    "異常値を見つけた場合は、その場で写真を撮影して報告書に添付する。",
    "作業終了後は試運転を 15 分間行い、警報が出ないことを確認する。",
]

def filler(round_: int) -> dict:
    return {"C1": NEW_FILLER, "C3": NEW_FILLER} if round_ >= 2 else {}

round_ = int(sys.argv[1])
sections = None
order = ("A", "B", "C")
text = build(facts(round_), order=order, sections=sections, filler_lines=20, filler=filler(round_))
open(ROOT / "data" / "mount-hc" / os.environ.get("HC_DOC", "hc-test2.md"), "w", encoding="utf-8").write(text)
print("round", round_, "written", len(text.splitlines()), "lines")
