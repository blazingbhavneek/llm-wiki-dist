"""Synthetic HX-300 test document (invented facts only), in four versions.

  .venv/bin/python tools/hc_live/canary.py <version 0-3> <mount>/hc/canary.md

Version 0 is the first publish (about 220 lines: 3 chapters of 3-4 sections, a table,
inline code, a link and a fenced block; the repeated line "運転モードは AUTO とする。"
appears in two sections). Versions 1-3 are the source changes of handoff-conflicts-tests.md
section 6, each commented with the case it produces. Set structure_target_lines = 80 in
the test INI so the document becomes several pages.
"""
import sys

FACTS = {
    "A1": ["HX-300 は研究棟C2の液冷サーバー列を冷却する冷却ユニットである。", "設計寿命は運転時間 70,000 時間である。"],
    "A2": ["本体は冷凍機 RM-7、循環ポンプ CP-21、制御盤 CB-9 で構成される。", "制御盤 CB-9 のファームウェア版数は FW 4.1.3 である。"],
    "B1": ["冷却水の供給温度は通常 17 °C に設定する。", "戻り温度が 25 °C を超えた場合は負荷を確認する。", "運転モードは AUTO とする。"],
    "B2": ["高温警報のしきい値は 62 °C である。", "低圧警報のしきい値は 0.18 MPa である。", "運転モードは AUTO とする。"],
    "A3": ["設置場所の周囲温度は 5〜32 °C、相対湿度は 25〜75 % とする。", "床荷重は 1 平方メートルあたり 900 kg 以上が必要である。"],
    "B3": ["電源は三相 200 V、定格電流 40 A である。", "非常用発電機への切替時間は最大 12 秒である。"],
    "C4": ["分解点検は 3 年ごとにメーカーが実施する。", "分解点検の前に冷媒を回収装置 RC-5 で回収する。"],
    "C1": ["定期点検は 4 か月ごとに実施する。", "点検では冷媒圧力、ポンプ振動、フィルタ差圧を記録する。"],
    "C2": ["フィルタ FL-12 は 8 か月ごとに交換する。", "交換後は差圧が 22 kPa 以下であることを確認する。"],
    "C3": ["エラーコード E-52 は冷媒漏れを示し、直ちに運転を停止する。", "エラーコード E-23 はポンプ過負荷を示し、再起動前に軸受を点検する。"],
}
TITLES = {"A": "第1章 概要", "B": "第2章 運転条件", "C": "第3章 保守", "D": "第4章 参考データ",
          "A1": "目的", "A2": "構成", "B1": "温度条件", "B2": "警報しきい値",
          "C1": "定期点検", "C2": "部品交換", "C3": "障害対応", "A3": "設置環境", "B3": "電源", "C4": "分解点検"}
STEPS = ["作業前に制御盤の表示を確認する。", "結果は保守台帳に記録する。", "異常があれば設備管理グループに連絡する。",
         "使用した工具を点検後に所定の棚へ戻す。", "作業区域に立入禁止の表示を掲げる。", "作業者は保護手袋と保護眼鏡を着用する。"]


def build(facts, order=("A", "B", "C"), extra=None):
    out = ["# HX-300 冷却ユニット 運用ガイド", "", "研究棟C2に設置された HX-300 冷却ユニットの運用ガイドである。", ""]
    for chapter in order:
        out += [f"## {TITLES[chapter]}", ""]
        for key in sorted(k for k in facts if k[0] == chapter):
            out += [f"### {TITLES[key]}", ""] + [x for fact in facts[key] for x in (fact, "")]
            out += [f"- {TITLES[key]}の手順{n + 1}: {STEPS[n % len(STEPS)]}" for n in range(12)] + [""]
    out += ["## 第4章 参考データ", "", "| 項目 | 値 | 単位 |", "|---|---|---|", "| 定格冷却能力 | 120 | kW |",
            "| 冷媒充填量 | 38 | kg |", "| 騒音値 | 61 | dB |", "",
            "設定は `hxctl set mode=auto` で変更する。詳細は [HX-300 技術資料](https://example.test/hx300) を参照する。", "",
            "```text", "hxctl status --unit HX-300", "```", ""] + (extra or [])
    return "\n".join(out) + "\n"


def version(round_):
    import copy
    f = copy.deepcopy(FACTS)
    if round_ >= 1:  # v1: page 002
        f["B3"][0] = "電源は三相 200 V、定格電流 45 A である。"           # case 2: untouched value
        f["B2"][0] = "高温警報のしきい値は 63 °C である。"              # case 10: human wrote 65
        f["B1"][0] = "冷却水の供給温度は通常 18 °C に設定する。"         # case 9: same section as HCT-05
        f["B2"] = f["B2"][:2]                                          # repeated mode line removed (HCT-W3)
    if round_ >= 2:  # v2: pages 001 and 003
        f["A3"][1] = "床荷重は 1 平方メートルあたり 950 kg 以上が必要である。"  # case 43: human deleted it
        f["C1"][0] = "定期点検は 6 か月ごとに実施する。"                # case 8: human made the same change
        del f["C4"]                                                     # case 44: section with HCT-44 deleted
    if round_ >= 3:  # v3
        f["C4"] = copy.deepcopy(FACTS["C4"])                            # case 59: the deleted section is restored
        f["C1"][0] = "定期点検は 5 か月ごとに実施する。"                # case 58: source changes again after catching up
        f["B1"][1] = "戻り温度が 26 °C を超えた場合は負荷を確認する。"   # case 57: next to the reverted HCT-05 line
    return f


E_CHAPTER = ["## 第5章 運用記録", ""] + [x for key, title, facts in (
    ("E1", "日次記録", ["運転日誌は毎日 9 時に記入する。", "記入者は当番の運転員とする。"]),
    ("E2", "月次報告", ["月次報告は翌月 5 日までに提出する。", "報告には稼働率と警報件数を含める。"]),
    ("E3", "記録の保存", ["運転日誌は 7 年間保存する。", "電子記録はバックアップを週 1 回取得する。"]),
) for x in [f"### {title}", ""] + [y for fact in facts for y in (fact, "")]
  + [f"- {title}の手順{n + 1}: {STEPS[n % len(STEPS)]}" for n in range(12)] + [""]]


def render(round_):
    text = build(version(round_))
    if round_ >= 3:
        text = text.replace("| 騒音値 | 61 | dB |\n", "")                # case 45: human deleted the same row
        text += "\n".join(E_CHAPTER) + "\n"                             # case 44: new page, Appendix renamed
    return text


if __name__ == "__main__":
    open(sys.argv[2], "w", encoding="utf-8").write(render(int(sys.argv[1])))
