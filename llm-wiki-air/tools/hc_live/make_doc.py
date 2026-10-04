"""Build the human-change test document (Japanese technical guide, 3 chapters x 3 sections)."""
import sys

FACTS = {
    "A1": ["HX-200 は研究棟B3の液冷サーバー列を冷却する冷却ユニットである。",
           "設計寿命は運転時間 60,000 時間であり、年次レビューで延長可否を判断する。"],
    "A2": ["本体は冷凍機モジュール RM-4、循環ポンプ CP-12、制御盤 CB-7 で構成される。",
           "制御盤 CB-7 のファームウェア版数は FW 3.2.1 である。"],
    "A3": ["設置場所の周囲温度は 5〜35 °C、相対湿度は 20〜80 % とする。",
           "床荷重は 1 平方メートルあたり 800 kg 以上が必要である。"],
    "B1": ["冷却水の供給温度は通常 18 °C に設定する。",
           "戻り温度が 26 °C を超えた場合は負荷を確認する。"],
    "B2": ["高温警報のしきい値は 60 °C である。",
           "低圧警報のしきい値は 0.15 MPa である。"],
    "B3": ["電源は三相 200 V、定格電流 32 A である。",
           "非常用発電機への切替時間は最大 10 秒である。"],
    "C1": ["定期点検は 3 か月ごとに実施する。",
           "点検では冷媒圧力、ポンプ振動、フィルタ差圧を記録する。"],
    "C2": ["フィルタ FL-9 は 6 か月ごとに交換する。",
           "交換後は差圧が 20 kPa 以下であることを確認する。"],
    "C3": ["エラーコード E-41 は冷媒漏れを示し、直ちに運転を停止する。",
           "エラーコード E-17 はポンプ過負荷を示し、再起動前に軸受を点検する。"],
}
TITLES = {
    "A": "第1章 概要", "B": "第2章 運転条件", "C": "第3章 保守",
    "A1": "目的", "A2": "構成", "A3": "設置環境",
    "B1": "温度条件", "B2": "警報しきい値", "B3": "電源",
    "C1": "定期点検", "C2": "部品交換", "C3": "障害対応",
}
FILLER = [
    "作業は二名体制で行い、作業記録を保守台帳に残す。",
    "設定値を変更した場合は、変更理由と承認者を記録する。",
    "不明点は設備管理グループに問い合わせる。",
]


def build(facts=FACTS, order=("A", "B", "C"), sections=None, filler_lines=13, filler=None):
    sections = sections or {chapter: [f"{chapter}{n}" for n in (1, 2, 3)] for chapter in order}
    out = ["# HX-200 冷却ユニット 運用ガイド", "", "研究棟B3に設置された HX-200 冷却ユニットの運用ガイドである。", ""]
    for chapter in order:
        out += [f"## {TITLES[chapter]}", ""]
        for key in sections[chapter]:
            out += [f"### {TITLES[key]}", ""]
            for fact in facts[key]:
                out += [fact, ""]
            texts = (filler or {}).get(key, FILLER)
            for n in range(filler_lines):
                out += [f"- {TITLES[key]}の手順{n + 1}: {texts[n % len(texts)]}"]
            out += [""]
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    sys.stdout.write(build())
