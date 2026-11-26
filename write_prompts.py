#!/usr/bin/env python3
"""
把「分鏡設定.json」組成每段的英文提示詞，寫進 shots.json（格式和《汪洋》一樣）。

用法：
  python write_prompts.py projects/某首歌                 # 原本的格式
  python write_prompts.py projects/某首歌 --format h3     # MiniMax H3 官方格式（三段式、[Shot 2] At 00:03.500 切點）
  python write_prompts.py projects/某首歌 --format h3 --shots-out 另一個.json   # 寫到別的檔案（比較測試用）

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
import argparse, json, os, sys

GEN = 10.0

def ts(sec):
    return f"{int(sec // 60):02d}:{sec % 60:06.3f}"

def classic_prompt(cfg, c, subs, edges):
    n = len(subs)
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
    return "\n\n".join(parts)

def h3_prompt(cfg, c, subs, edges):
    """MiniMax H3 官方 T2VA 格式：integrated_multimodal_description / overall_soundscape / non_diegetic_music"""
    body = [f"[Shot 1] {cfg['style']} {c['overview']}"]
    if c.get("chars"):
        body.append("On screen: " + "; ".join(f"{k}, {cfg['characters'][k]}" for k in c["chars"]) + ".")
    body.append(subs[0])
    for k in range(1, len(subs)):
        body.append(f"[Shot {k + 1}] At {ts(edges[k])}, the camera cuts to the next shot. {subs[k]}")
    body.append("Camera throughout: " + c.get("camera", cfg["camera"]))
    body.append(cfg.get("h3_silence", "Nobody in the video speaks or sings; every visible face keeps its lips closed."))
    body.append(cfg["negative"])
    sound = cfg.get("soundscape") or cfg["audio"]
    sound = sound[:1].upper() + sound[1:]
    return ("integrated_multimodal_description: " + " ".join(b.strip() for b in body) +
            "\n\noverall_soundscape: " + sound +
            "\n\nnon_diegetic_music: " + cfg.get("non_diegetic_music", "N/A"))

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("project")
    ap.add_argument("--format", choices=["classic", "h3"], default="classic")
    ap.add_argument("--shots-out", default=None, help="提示詞寫到這個檔案（預設覆寫專案的 shots.json）")
    a = ap.parse_args()
    P = a.project
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
        s["prompt"] = (h3_prompt if a.format == "h3" else classic_prompt)(cfg, c, subs, edges)
        s["summary_zh"] = c["zh"]
    plan["prompt_format"] = a.format
    json.dump(plan, open(a.shots_out or os.path.join(P, "shots.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    if a.shots_out:
        print(f"已寫入 {a.shots_out}（{a.format} 格式）"); return

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
