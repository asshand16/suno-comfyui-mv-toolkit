#!/usr/bin/env python3
"""
新歌準備腳本：把 Suno 的三份資料整理成「切點 + 歌詞字幕 + 分鏡草稿」。

專案資料夾裡需要（由 Claude 透過 Suno 連接器存進來）：
  suno_retrieve.json    歌曲資訊（歌名、風格、帶段落標籤的歌詞）
  suno_analysis.json    節拍、BPM、調性、段落
  suno_alignment.json   每個字的演唱時間

用法：
  python prepare_song.py projects/某首歌
  python prepare_song.py projects/某首歌 --max 9.9 --target 8.8 --folder mysong_mv

輸出到同一個資料夾：
  song_info.json   歌曲摘要（BPM、調性、段落時間、每句歌詞時間）
  lyrics.srt       對好時間的歌詞字幕（句子之間不重疊）
  shots.json       每段的切點、段落、歌詞；prompt 先留空，由 Claude 填寫
  分鏡草稿.md       人看的段落表
"""
import argparse, json, math, os, re, sys

FPS = 24
TAG_ZH = [  # (關鍵字, 中文)，依序比對
    ("final chorus", "最終副歌"), ("pre-chorus", "導歌"), ("prechorus", "導歌"), ("pre chorus", "導歌"),
    ("post-chorus", "副歌後段"), ("chorus", "副歌"), ("hook", "副歌"), ("refrain", "副歌"),
    ("verse", "主歌"), ("bridge", "橋段"), ("build", "鋪陳"), ("rise", "鋪陳"), ("breakdown", "間奏"), ("intro", "前奏"), ("outro", "尾奏"), ("end", "結尾"),
    ("instrumental", "器樂段"), ("interlude", "間奏"), ("break", "間奏"), ("solo", "器樂段"), ("drop", "Drop"),
]
ZH_NUM = "零一二三四五六七八九十"


def tag_kind(tag):
    """只看標籤 ' - ' 前面的名稱，避免編曲說明裡的字被誤判。"""
    head = tag.split(" - ")[0].lower()
    for k, zh in TAG_ZH:
        if k in head:
            return zh, head
    return None, head


def tag_to_zh(tag):
    zh, head = tag_kind(tag)
    if zh is None:
        return tag.split(" - ")[0].strip()
    m = re.search(r"\b(\d+)\b", head)
    if m and zh in ("主歌", "副歌", "導歌", "橋段") and int(m.group(1)) < 11:
        zh += ZH_NUM[int(m.group(1))]
    return zh


def norm(s):
    s = re.sub(r"\([^)]*\)", "", s)
    return re.sub(r"[\s\W_]+", "", s)


def clean_line(s):
    s = re.sub(r"\([^)]*\)", "", s)
    return re.sub(r"\s+", " ", s).strip()


def parse_tagged_lyrics(text):
    """回傳 [(段落標籤, [歌詞行...]), ...]，略過 [Sound Effect] 之類的說明和純和聲行。"""
    secs = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = re.fullmatch(r"\[(.+)\]", line)
        if m:
            tag = m.group(1)
            if re.match(r"\s*(sound effect|sfx|end\s*$)", tag, re.I):
                continue
            # 不是段落名稱的方括號（例如 [ambient synth pads]）只是編曲說明，不算新段落
            if secs and tag_kind(tag)[0] is None:
                continue
            secs.append([tag, []])
            continue
        if not norm(line):
            continue
        if not secs:
            secs.append(["Intro", []])
        secs[-1][1].append(clean_line(line))
    return secs


def aligned_lines(words):
    """把逐字時間拆成一句一句：[(文字, 開始, 結束)]。"""
    chars = []
    for w in words:
        for ch in w["word"]:
            chars.append((ch, w["start_s"], w["end_s"]))
    lines, cur, depth = [], [], 0
    for ch, s, e in chars + [("\n", None, None)]:
        if ch == "\n":
            txt = clean_line("".join(c for c, _, _ in cur))
            timed = [(s2, e2) for c, s2, e2 in cur if c.strip() and c not in "()" and not c.isspace()]
            if norm(txt) and timed:
                # 只用括號外的字算時間
                d, ts = 0, []
                for c, s2, e2 in cur:
                    if c == "(":
                        d += 1
                    elif c == ")":
                        d = max(0, d - 1)
                    elif d == 0 and c.strip():
                        ts.append((s2, e2))
                ts = ts or timed
                lines.append([txt, min(t[0] for t in ts), max(t[1] for t in ts)])
            cur = []
        else:
            cur.append((ch, s, e))
    return lines


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("project")
    ap.add_argument("--max", type=float, default=10.0, help="每段最長秒數（生成的片段是 243 影格 ≈ 10.1 秒）")
    ap.add_argument("--min", type=float, default=4.0, help="每段最短秒數")
    ap.add_argument("--target", type=float, default=8.8, help="理想段長")
    ap.add_argument("--folder", default=None, help="ComfyUI 輸出子資料夾名稱（預設用專案資料夾名稱）")
    a = ap.parse_args()
    P = a.project
    rd = lambda f: json.load(open(os.path.join(P, f), encoding="utf-8"))
    info, ana, ali = rd("suno_retrieve.json"), rd("suno_analysis.json"), rd("suno_alignment.json")
    meta = info.get("metadata", {})
    dur = float(ana.get("duration_s") or meta.get("duration"))
    spb = 60.0 / ana["bpm"]
    if "beats" in ana:  # Suno 原始格式
        beats = [b["time_s"] for b in ana["beats"]]
        downs = [b["time_s"] for b in ana["beats"] if b.get("bar_position") == 1]
    else:  # 精簡格式：只存強拍，其餘拍子依每小節拍數平均補上
        downs = sorted(ana["downbeats"])
        beats = []
        t = downs[0] - spb
        while t > -0.05:
            beats.insert(0, round(max(t, 0.0), 3)); t -= spb
        for d0, d1 in zip(downs, downs[1:]):
            n = max(1, round((d1 - d0) / spb))
            beats += [round(d0 + (d1 - d0) * k / n, 3) for k in range(n)]
        t = downs[-1]
        while t < dur - 0.05:
            beats.append(round(t, 3)); t += spb
        beats = sorted(b for b in set(beats) if b >= 0)
    ali["words"] = [w if isinstance(w, dict) else {"word": w[0], "start_s": w[1], "end_s": w[2]}
                    for w in ali.get("words", [])]

    # ---- 歌詞：逐句時間 + 段落 ----
    lines = aligned_lines(ali.get("words", []))
    tagged = parse_tagged_lyrics(meta.get("lyrics", ""))
    flat = [(si, l) for si, (_, ls) in enumerate(tagged) for l in ls]
    sec_of_line = []
    j = 0
    for txt, s, e in lines:
        k = j
        while k < len(flat) and norm(flat[k][1]) != norm(txt):
            k += 1
        if k < len(flat):
            sec_of_line.append(flat[k][0]); j = k + 1
        else:
            sec_of_line.append(sec_of_line[-1] if sec_of_line else 0)
            print(f"  注意：對不到段落的歌詞行「{txt}」，先歸到前一段")
    sections = []  # {name, start, end, lines}
    for si, (tag, ls) in enumerate(tagged):
        idx = [i for i, x in enumerate(sec_of_line) if x == si]
        sections.append({"tag": tag, "name": tag_to_zh(tag),
                         "first": lines[idx[0]][1] if idx else None,
                         "last": lines[idx[-1]][2] if idx else None, "lines": idx})

    def beat_at_or_before(t):
        c = [b for b in beats if b <= t + 1e-6]
        return c[-1] if c else 0.0

    def downbeat_after(t, within=2.6):
        c = [b for b in downs if t - 0.05 <= b <= t + within]
        if c:
            return c[0]
        c = [b for b in beats if b >= t - 0.05]
        return c[0] if c else t

    # 段落開始：有歌詞的段 = 第一句前的那一拍；沒歌詞的段（器樂、間奏）= 前一段最後一句之後的第一個強拍
    starts = []
    for i, s in enumerate(sections):
        if s["first"] is not None:
            near = [d for d in downs if s["first"] - 2.05 * spb <= d <= s["first"] + 0.05]
            t = near[-1] if near else beat_at_or_before(s["first"] - 0.02)  # 優先切在人聲前的強拍
        elif i == 0:
            t = 0.0
        else:
            prev_last = next((x["last"] for x in reversed(sections[:i]) if x["last"] is not None), None)
            t = downbeat_after(prev_last) if prev_last is not None else 0.0
        starts.append(t)
    # 第一段固定從 0 開始；最後一句結束後若還有一段，視為尾奏
    starts[0] = 0.0
    for i in range(1, len(starts)):
        starts[i] = max(starts[i], starts[i - 1])
    last_sung = max(l[2] for l in lines) if lines else 0
    if sections and sections[-1]["first"] is not None and dur - last_sung > a.min:
        # 最後一段有歌詞時，歌詞唱完到結束的器樂尾巴另成一段
        sections.append({"tag": "Outro (instrumental)", "name": "尾奏", "first": None, "last": None, "lines": []})
        starts.append(downbeat_after(last_sung))
    for i, s in enumerate(sections):
        s["start"] = starts[i]
        s["end"] = starts[i + 1] if i + 1 < len(sections) else dur
    sections = [s for s in sections if s["end"] - s["start"] > 0.3]

    # 太短的段落併到後一段（或最後一段併到前一段）
    merged = []
    for s in sections:
        if merged and (s["end"] - s["start"] < a.min or merged[-1]["end"] - merged[-1]["start"] < a.min):
            m = merged[-1]
            m["end"] = s["end"]; m["lines"] += s["lines"]
            if s["name"] not in m["name"].split("／"):
                m["name"] += "／" + s["name"]
        else:
            merged.append(dict(s))
    sections = merged

    # ---- 每個段落內用動態規劃選切點：段長 ≤ max、≥ min、接近 target，切在強拍，不切在字中間 ----
    word_spans = [(w["start_s"], w["end_s"]) for w in ali.get("words", [])]
    line_starts = [l[1] for l in lines]

    def cut_cost(t):
        c = 0.0
        if any(s + 0.05 < t < e - 0.05 for s, e in word_spans):
            c += 4.0  # 切在唱一個字的中間
        if any(0 <= ls - t <= 1.2 for ls in line_starts):
            c -= 0.8  # 剛好在一句開頭前
        return c

    # 每一拍在小節裡的位置（0 = 強拍）
    pos = {}
    for b in beats:
        prev_d = [d for d in downs if d <= b + 1e-3]
        pos[b] = round((b - prev_d[-1]) / spb) if prev_d else 1

    def beat_pen(b):
        p = pos.get(b, 1)
        return 0.0 if p == 0 else (0.35 if p % 2 == 0 else 0.9)  # 強拍 < 半小節 < 其他拍

    def split(A, B):
        L = B - A
        if L <= a.max:
            return [A, B]
        cand = [A] + [b for b in beats if A + a.min - 1e-6 <= b <= B - a.min + 1e-6] + [B]
        n = len(cand)
        best = [math.inf] * n; prev = [-1] * n; best[0] = 0
        for j in range(1, n):
            for i in range(j):
                seg = cand[j] - cand[i]
                if seg > a.max + 1e-6 or seg < a.min - 1e-6:
                    continue
                c = best[i] + (seg - a.target) ** 2 + (0 if j == n - 1 else cut_cost(cand[j]) + beat_pen(cand[j]))
                if c < best[j]:
                    best[j], prev[j] = c, i
        if best[-1] < math.inf:
            out, j = [], n - 1
            while j >= 0:
                out.append(cand[j]); j = prev[j]
            return out[::-1]
        k = math.ceil(L / a.max)  # 找不到合適的拍子：平均切
        return [A + L * i / k for i in range(k + 1)]

    cuts = [0.0]
    shot_sec = []
    for si, s in enumerate(sections):
        pts = split(s["start"], s["end"])
        for p in pts[1:]:
            cuts.append(round(p, 3)); shot_sec.append(si)
    cuts[-1] = round(dur, 3)

    # ---- 字幕 ----
    srt = []
    for i, (txt, s, e) in enumerate(lines):
        nxt = lines[i + 1][1] if i + 1 < len(lines) else dur
        st = max(0.0, s - 0.1)
        en = min(max(e + 1.2, st + 1.0), nxt - 0.14, dur)
        srt.append((st, max(en, st + 0.3), txt))

    def ts(x):
        ms = round(x * 1000)
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"
    with open(os.path.join(P, "lyrics.srt"), "w", encoding="utf-8-sig", newline="\r\n") as f:
        f.write("\n\n".join(f"{i+1}\n{ts(s)} --> {ts(e)}\n{t}" for i, (s, e, t) in enumerate(srt)) + "\n")

    # ---- shots.json ----
    title = info.get("title") or os.path.basename(os.path.normpath(P))
    folder = a.folder or re.sub(r"[^\w-]+", "_", os.path.basename(os.path.normpath(P))) + "_mv"
    old = {}
    sp = os.path.join(P, "shots.json")
    if os.path.exists(sp):  # 重跑時保留已經寫好的提示詞（依編號）
        try:
            old = {s["id"]: s for s in json.load(open(sp, encoding="utf-8"))["shots"]}
        except Exception:
            old = {}
    shots = []
    for i in range(len(cuts) - 1):
        s0, s1 = cuts[i], cuts[i + 1]
        ly = [t for (t, ls, le) in lines if s0 <= (ls + le) / 2 < s1]  # 一句歌詞歸到它中點所在的段
        o = old.get(i + 1, {})
        shots.append({"id": i + 1, "start": s0, "end": s1, "duration": round(s1 - s0, 3),
                      "section": sections[shot_sec[i]]["name"], "lyrics": ly,
                      "summary_zh": o.get("summary_zh", ""), "prompt": o.get("prompt", "")})
    plan = {"song": title, "fps": FPS, "duration": round(dur, 3), "output_folder": folder,
            "bpm": ana.get("bpm"), "key": ana.get("key"), "style": meta.get("style", ""), "shots": shots}
    json.dump(plan, open(sp, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    song_info = {"title": title, "suno_id": info.get("id"), "duration": dur, "bpm": ana.get("bpm"),
                 "key": ana.get("key"), "time_signature": ana.get("time_signature"),
                 "style": meta.get("style", ""), "terms": ana.get("terms", []),
                 "sections": [{"name": s["name"], "tag": s["tag"], "start": round(s["start"], 3),
                               "end": round(s["end"], 3)} for s in sections],
                 "lines": [{"text": t, "start": round(s, 3), "end": round(e, 3)} for t, s, e in lines],
                 "lyrics_tagged": meta.get("lyrics", "")}
    json.dump(song_info, open(os.path.join(P, "song_info.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    mmss = lambda x: f"{int(x // 60)}:{int(x % 60):02d}"
    md = [f"# 《{title}》分鏡草稿", "",
          f"- 長度 {mmss(dur)}（{dur:.1f} 秒）｜{ana.get('bpm')} BPM｜{ana.get('key')}｜{len(shots)} 段",
          f"- 風格：{meta.get('style', '')}", f"- ComfyUI 輸出資料夾：`output/{folder}/`", "",
          "| # | 時間 | 長度 | 段落 | 歌詞 | 畫面 |", "|---|---|---|---|---|---|"]
    for s in shots:
        md.append(f"| {s['id']:02d} | {mmss(s['start'])}–{mmss(s['end'])} | {s['duration']:.1f}s | {s['section']} | "
                  f"{' / '.join(s['lyrics']) or '（無）'} | {s['summary_zh']} |")
    open(os.path.join(P, "分鏡草稿.md"), "w", encoding="utf-8").write("\n".join(md) + "\n")

    lens = [s["duration"] for s in shots]
    print(f"《{title}》{dur:.1f} 秒，{ana.get('bpm')} BPM，{ana.get('key')}")
    print(f"段落 {len(sections)} 個 → 切成 {len(shots)} 段，最短 {min(lens):.1f}s、最長 {max(lens):.1f}s")
    print(f"歌詞 {len(lines)} 句 → lyrics.srt；切點 → shots.json；表格 → 分鏡草稿.md")


if __name__ == "__main__":
    main()
