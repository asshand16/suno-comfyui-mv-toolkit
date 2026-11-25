"""每段抽 5 格畫面拼成縮圖表，附上每段歌詞，方便挑對嘴段落或檢查整支片。

用法：
  python contact_sheet.py <專案資料夾>                 # 一般段落
  python contact_sheet.py <專案資料夾> --lipsync       # 對嘴版（_lipsync 資料夾）
輸出：<專案>/縮圖表_1.jpg、縮圖表_2.jpg…（每張 10 段）和 縮圖表.md（段落、歌詞、建議）
"""
import argparse, json, os, re, subprocess, tempfile

FONT = "C\\:/Windows/Fonts/arial.ttf" if os.name == "nt" else "DejaVuSans"


def newest(clips):
    best = {}
    for f in os.listdir(clips) if os.path.isdir(clips) else []:
        m = re.match(r"shot_(\d{2})_(\d+)_?\.(mp4|webm|mov)$", f)
        if m:
            k, n = int(m.group(1)), int(m.group(2))
            if k not in best or n > best[k][0]:
                best[k] = (n, f)
    return {k: os.path.join(clips, v[1]) for k, v in best.items()}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("project")
    ap.add_argument("--comfy-output", default=os.environ.get("COMFYUI_OUTPUT", os.path.expanduser("~/Documents/ComfyUI/output")))
    ap.add_argument("--clips", default=None)
    ap.add_argument("--lipsync", action="store_true")
    ap.add_argument("--per-sheet", type=int, default=10)
    ap.add_argument("--width", type=int, default=240)
    a = ap.parse_args()

    data = json.load(open(os.path.join(a.project, "shots.json"), encoding="utf-8"))
    shots = data["shots"] if isinstance(data, dict) else data
    folder = data.get("output_folder", "") if isinstance(data, dict) else ""
    clips = a.clips or os.path.join(a.comfy_output, folder + ("_lipsync" if a.lipsync else ""))
    files = newest(clips)
    tmp = tempfile.mkdtemp()
    tag = "對嘴" if a.lipsync else ""
    rows, md = [], [f"# 縮圖表{tag}", "", f"片段資料夾：`{clips}`", "",
                     "| # | 長度 | 段落 | 歌詞 | 檔案 |", "|---|---|---|---|---|"]
    for s in shots:
        i = s["id"]
        f = files.get(i)
        lyr = " / ".join(l for l in s.get("lyrics", []) if l.strip())
        md.append(f"| {i:02d} | {s['duration']:.1f}s | {s.get('section', '')} | {lyr or '（無歌詞）'} | {os.path.basename(f) if f else '未生成'} |")
        if not f:
            continue
        imgs = []
        for j, k in enumerate((0.06, 0.3, 0.54, 0.78, 0.96)):
            out = os.path.join(tmp, f"s{i:02d}_{j}.jpg")
            vf = (f"scale={a.width}:-2,drawtext=fontfile='{FONT}':text='{i:02d}':x=4:y=4:"
                  "fontsize=22:fontcolor=yellow:box=1:boxcolor=black")
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{s['duration'] * k:.2f}", "-i", f,
                            "-frames:v", "1", "-vf", vf, out], check=True)
            imgs.append(out)
        rows.append(imgs)

    sheets = []
    for g in range(0, len(rows), a.per_sheet):
        chunk = rows[g:g + a.per_sheet]
        inp = [x for r in chunk for p in r for x in ("-i", p)]
        n = len(chunk) * 5
        out = os.path.join(a.project, f"縮圖表{tag}_{g // a.per_sheet + 1}.jpg")
        subprocess.run(["ffmpeg", "-v", "error", "-y", *inp, "-filter_complex",
                        "".join(f"[{k}]" for k in range(n)) + f"xstack=inputs={n}:grid=5x{len(chunk)}", out], check=True)
        sheets.append(out)

    md += ["", "挑對嘴段落：找**有歌詞、臉清楚（特寫或正面半身）、正在唱的人物**的段落；背影、遠景、只有手或物件的段落不適合。", ""]
    md += [f"![縮圖表 {k + 1}]({os.path.basename(p)})" for k, p in enumerate(sheets)]
    open(os.path.join(a.project, f"縮圖表{tag}.md"), "w", encoding="utf-8").write("\n".join(md) + "\n")
    print(f"{len(rows)} 段 → " + "、".join(sheets))


if __name__ == "__main__":
    main()
