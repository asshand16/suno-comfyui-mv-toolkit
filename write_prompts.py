#!/usr/bin/env python3
"""
把「分鏡設定.json」組成每段的英文提示詞，寫進 shots.json（格式和《汪洋》一樣）。

用法：
  python write_prompts.py projects/某首歌

分鏡設定.json 的格式：
{
  "style": "共用風格（英文）",
  "characters": {"the man": "外型描述", "the woman": "外型描述"},
  "camera": "預設運鏡",
  "audio": "預設音效描述",
  "negative": "最後一行（不要字幕、浮水印…）",
  "shots": {
    "1": {"zh": "中文畫面說明", "chars": ["the man"], "overview": "英文場景概述",
          "shots": ["Wide shot: …", "Close-up: …"], "camera": "（可省略，覆寫預設運鏡）"}
  }
}
每段裡的鏡頭時間會依這段實際長度、對齊拍子平均分配，最後一個鏡頭延伸到 10 秒（生成長度）。
"""
import json, os, sys

GEN = 10.0


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__); sys.exit(0 if len(sys.argv) >= 2 else 1)
    P = sys.argv[1]
    plan = json.load(open(os.path.join(P, "shots.json"), encoding="utf-8"))
    cfg = json.load(open(os.path.join(P, "分鏡設定.json"), encoding="utf-8"))
    spb = 60.0 / plan["bpm"] if plan.get("bpm") else 0.5
    missing = []
    for s in plan["shots"]:
        c = cfg["shots"].get(str(s["id"]))
        if not c:
            missing.append(s["id"]); continue
        subs = c["shots"]
        n = len(subs)
        beats = max(n, round(s["duration"] / spb))
        edges = [round(beats * k / n) * spb for k in range(n)] + [GEN]
        if n > 1 and edges[-2] > s["duration"] - 1.0:  # 最後一個鏡頭至少留 1 秒在剪輯範圍內
            edges = [s["duration"] * k / n for k in range(n)] + [GEN]
        board = "\n".join(f"[{edges[k]:.1f}s-{edges[k + 1]:.1f}s] Shot {k + 1}: {t}" for k, t in enumerate(subs))
        parts = [cfg["style"]]
        if c.get("chars"):
            parts.append("Characters: " + "; ".join(f"{k} ({cfg['characters'][k]})" for k in c["chars"]) + ".")
        parts.append("Scene overview: " + c["overview"])
        parts.append(f"Storyboard ({n} shot{'s' if n > 1 else ''}"
                     f"{', hard cuts landing on the beat' if n > 1 else ''}):\n" + board)
        parts.append("Camera: " + c.get("camera", cfg["camera"]))
        parts.append("Audio: " + cfg["audio"])
        parts.append(cfg["negative"])
        s["prompt"] = "\n\n".join(parts)
        s["summary_zh"] = c["zh"]
    json.dump(plan, open(os.path.join(P, "shots.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    mmss = lambda x: f"{int(x // 60)}:{int(x % 60):02d}"
    md = [f"# 《{plan['song']}》分鏡表", "",
          f"- 長度 {mmss(plan['duration'])}｜{plan.get('bpm')} BPM｜{plan.get('key')}｜{len(plan['shots'])} 段",
          f"- ComfyUI 輸出資料夾：`output/{plan.get('output_folder')}/`", "",
          "## 共用設定", "", f"**風格**：{cfg['style']}", ""]
    md += [f"- **{k}**：{v}" for k, v in cfg["characters"].items()]
    md += ["", "## 分鏡", "", "| # | 時間 | 長度 | 段落 | 歌詞 | 畫面 |", "|---|---|---|---|---|---|"]
    for s in plan["shots"]:
        md.append(f"| {s['id']:02d} | {mmss(s['start'])}–{mmss(s['end'])} | {s['duration']:.1f}s | {s['section']} | "
                  f"{' / '.join(s['lyrics']) or '（無）'} | {s['summary_zh']} |")
    open(os.path.join(P, "分鏡表.md"), "w", encoding="utf-8").write("\n".join(md) + "\n")
    if missing:
        print("還沒寫的段：" + ", ".join(f"{i:02d}" for i in missing))
    print(f"已寫入 {len(plan['shots']) - len(missing)} 段提示詞 → shots.json、分鏡表.md")


if __name__ == "__main__":
    main()
