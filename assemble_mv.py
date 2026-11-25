#!/usr/bin/env python3
"""
《汪洋》MV 合成腳本
把 ComfyUI 產出的 27 段 shot_XX_*.mp4 依分鏡表剪到對齊節拍的長度、接起來、換上原曲。

用法（需要 Python 3.8+ 和 ffmpeg）：
  python assemble_mv.py --clips "C:/ComfyUI/output/wangyang_mv" --song "汪洋.mp3"
  python assemble_mv.py --clips ... --song ... --subs        # 燒上歌詞字幕
選項：
  --out 檔名            輸出檔（預設 汪洋_MV.mp4）
  --size 1920x1080      輸出解析度（預設 1920x1080；填 native 保留原始大小）
  --font "字型名稱"      字幕字型（預設 Windows: Microsoft JhengHei／macOS: PingFang TC）
  --allow-missing       缺的段落用黑畫面先頂著，方便先看粗剪
同一段有多個檔案（重抽過）時，自動用編號最大的那一個。
"""
import argparse, json, os, platform, re, shutil, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
PAT = re.compile(r"shot_(\d{2})_(\d+)_?\.(mp4|mov|mkv|webm)$", re.I)


def find_clips(folder):
    best = {}
    for root, _, files in os.walk(folder):
        for f in files:
            m = PAT.search(f)
            if not m:
                continue
            sid, cnt = int(m.group(1)), int(m.group(2))
            if sid not in best or cnt > best[sid][0]:
                best[sid] = (cnt, os.path.abspath(os.path.join(root, f)))
    return {k: v[1] for k, v in best.items()}


def main():
    ap = argparse.ArgumentParser(description="Assemble the 汪洋 MV from ComfyUI clips")
    ap.add_argument("--project", default=None,
                    help="新歌的專案資料夾（projects/某首歌）：自動使用裡面的 shots.json、lyrics.srt、音樂檔")
    ap.add_argument("--comfy-output",
                    default=os.environ.get("COMFYUI_OUTPUT", os.path.expanduser(os.path.join("~", "Documents", "ComfyUI", "output"))),
                    help="ComfyUI 的 output 資料夾（預設：環境變數 COMFYUI_OUTPUT，或 ~/Documents/ComfyUI/output）")
    ap.add_argument("--clips", default=None, help="放 shot_XX 影片的資料夾")
    ap.add_argument("--song", default=None, help="原曲 mp3")
    ap.add_argument("--out", default=None)
    ap.add_argument("--shots", default=None)
    ap.add_argument("--srt", default=None)
    ap.add_argument("--size", default="1920x1080")
    ap.add_argument("--subs", action="store_true", help="燒上歌詞字幕")
    ap.add_argument("--font", default=None)
    ap.add_argument("--fontsize", type=int, default=30, help="字幕大小（以 288 高的畫面為基準）")
    ap.add_argument("--marginv", type=int, default=16, help="字幕離畫面底部的距離，越小越低")
    ap.add_argument("--allow-missing", action="store_true")
    ap.add_argument("--labels", action="store_true", help="左上角標出段落編號和檔案版本（方便回 ComfyUI 修改）")
    ap.add_argument("--labelfont", default=None, help="標籤字型檔（預設 Windows 微軟正黑體 msjh.ttc）")
    ap.add_argument("--crf", type=int, default=18)
    a = ap.parse_args()

    base = os.path.abspath(a.project) if a.project else HERE
    a.shots = a.shots or os.path.join(base, "shots.json")
    a.srt = a.srt or os.path.join(base, "lyrics.srt")
    plan = json.load(open(a.shots, encoding="utf-8"))
    if not a.clips:
        if not plan.get("output_folder"):
            sys.exit("請用 --clips 指定放 shot_XX 影片的資料夾")
        a.clips = os.path.join(a.comfy_output, plan["output_folder"])
    if not a.song:
        found = sorted(f for f in os.listdir(base) if f.lower().endswith((".mp3", ".wav", ".m4a", ".flac")))
        if not found and os.path.isdir(a.clips):
            base_c = a.clips
            found = [os.path.join(base_c, f) for f in sorted(os.listdir(base_c)) if f.lower().endswith((".mp3", ".wav"))]
        if not found:
            sys.exit("找不到音樂檔：請把 mp3 放進專案資料夾，或用 --song 指定")
        a.song = os.path.join(base, found[0])
    if not a.out:
        a.out = os.path.join(base if a.project else os.getcwd(), f"{plan.get('song', 'MV')}_MV.mp4")

    ff = shutil.which("ffmpeg")
    if not ff:
        sys.exit("找不到 ffmpeg。請先安裝 ffmpeg 並加入 PATH（Windows 可用 winget install ffmpeg）。")
    fps = plan["fps"]
    shots = plan["shots"]
    clips = find_clips(a.clips)
    missing = [s["id"] for s in shots if s["id"] not in clips]
    if missing and not a.allow_missing:
        sys.exit("還缺這些段落：" + ", ".join(f"{m:02d}" for m in missing) + "\n（想先看粗剪可加 --allow-missing）")

    if a.size == "native":
        W, H = 1344, 768
    else:
        W, H = map(int, a.size.lower().split("x"))

    def label(sid):
        if not a.labels:
            return ""
        if sid in clips:
            ver = PAT.search(os.path.basename(clips[sid])).group(2)
            txt = f"第 {sid:02d} 段  ·  {ver}"
        else:
            txt = f"第 {sid:02d} 段  ·  未生成"
        fontfile = (a.labelfont or r"C:\Windows\Fonts\msjh.ttc").replace("\\", "/").replace(":", "\\:")
        fs = round(H * 0.04)
        return (f",drawtext=fontfile='{fontfile}':text='{txt}':x={round(H*0.03)}:y={round(H*0.03)}:"
                f"fontsize={fs}:fontcolor=white:box=1:boxcolor=black@0.55:boxborderw={round(fs*0.35)}")

    args = [ff, "-hide_banner", "-y"]
    filt = []
    idx = 0
    for s in shots:
        n = round(s["end"] * fps) - round(s["start"] * fps)
        if s["id"] in clips:
            args += ["-i", clips[s["id"]]]
            filt.append(f"[{idx}:v]fps={fps},scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,"
                        f"crop={W}:{H},setsar=1,tpad=stop_mode=clone:stop_duration=3,"
                        f"trim=end_frame={n},setpts=PTS-STARTPTS{label(s['id'])},format=yuv420p[v{s['id']}]")
        else:
            args += ["-f", "lavfi", "-i", f"color=c=black:s={W}x{H}:r={fps}"]
            filt.append(f"[{idx}:v]trim=end_frame={n},setpts=PTS-STARTPTS{label(s['id'])},format=yuv420p[v{s['id']}]")
        idx += 1
    args += ["-i", os.path.abspath(a.song)]
    song_idx = idx
    total = plan["duration"]
    chain = "".join(f"[v{s['id']}]" for s in shots) + f"concat=n={len(shots)}:v=1:a=0,"
    chain += f"fade=t=in:st=0:d=1.2,fade=t=out:st={total - 3:.2f}:d=3"
    cwd = None
    if a.subs:
        font = a.font or ("PingFang TC" if platform.system() == "Darwin" else "Microsoft JhengHei")
        cwd = os.path.dirname(os.path.abspath(a.srt))
        style = (f"FontName={font},FontSize={a.fontsize},PrimaryColour=&H00F0F0F0,OutlineColour=&H80000000,"
                 f"BorderStyle=1,Outline=1.2,Shadow=0,Alignment=2,WrapStyle=2,MarginV={a.marginv}")
        chain += f",subtitles={os.path.basename(a.srt)}:charenc=UTF-8:force_style='{style}'"
    chain += "[vout]"
    filt.append(chain)
    out = os.path.abspath(a.out)
    args += ["-filter_complex", ";".join(filt), "-map", "[vout]", "-map", f"{song_idx}:a",
             "-c:v", "libx264", "-preset", "medium", "-crf", str(a.crf), "-r", str(fps),
             "-c:a", "aac", "-b:a", "320k", "-t", f"{total:.3f}", "-movflags", "+faststart", out]
    print(f"合成 {len(shots)} 段（{len(clips)} 段有檔案）→ {out}")
    r = subprocess.run(args, cwd=cwd)
    if r.returncode != 0:
        sys.exit("ffmpeg 失敗，請看上面的錯誤訊息。")
    print("完成：", out)


if __name__ == "__main__":
    main()
