#!/usr/bin/env python3
"""
產生「對嘴」ComfyUI 工作流程（MiniMax H3 ref2va，全部內建節點）。

這是「第二階段」：一般段落先跑完、剪好，再挑想對嘴的段落跑這個。

做法（從原本的片段重新生成）：
  - 找出那段原本生成好的片段，自動偵測裡面的硬切點
  - 第 0 格和每個切點後的第一格當錨點（MiniMaxH3AddGuide），場景、人物、構圖、剪接點都跟原本一樣
  - 那段的人聲（Demucs 分離後切出 10 秒）錨定在第 0 格，動作和嘴型重新生成、跟著歌聲
  - 提示詞沿用原本那段：分鏡時間改成偵測到的切點，加上歌詞和對嘴說明，「不要唱歌」改成「保留給的人聲」

輸出存到另一個資料夾 output/<輸出資料夾>_lipsync/shot_XX_000NN_.mp4，不會蓋掉原本的段落；
剪輯時用 assemble_mv.py --lipsync 5,9 指定哪幾段換成對嘴版。

用法：
  python build_lipsync_workflow.py <專案> --shots 5,9 [--face 臉部特寫.png] [--prompts 自訂.json]

  --shots    要對嘴的段落編號（逗號分隔，一次最多 10 段）
  --face     選用：再加一張臉部參考圖，讓長相更穩定
  --prompts  選用：自訂對嘴提示詞 JSON {"5": "…"}；沒寫到的段落自動轉換
  --vocals   已經分離好的人聲檔；沒給就找 <專案>/stems/**/vocals.wav，再沒有就用 Demucs 從原曲分離
  --clips    原本片段的資料夾（預設 ComfyUI/output/<輸出資料夾>）

產出：
  <專案>/<歌名>_對嘴_MiniMaxH3_本機.json     拖進 ComfyUI 用
  <專案>/lipsync_api.json                     API 格式（給程式直接送出）
  ComfyUI/input/<輸出資料夾>_vocals.wav、<輸出資料夾>_lip_sXX_fNNN.png（錨點畫面）
"""
import argparse, json, os, re, shutil, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
FPS = 24
SEED_BASE = 20261125
MODELS = {
    "unet": "minimax_h3_ref2va_pruned_int8_convrot.safetensors",
    "lora": "minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors",
    "clip": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
    "vae": "minimax_h3_video_vae_int8_convrot.safetensors",
    "avae": "minimax_h3_audio_vae_fp32.safetensors",
}
WIDGET_TYPES = {"INT", "FLOAT", "STRING", "BOOLEAN", "COMBO", "COMFY_DYNAMICCOMBO_V3"}


# ---------------------------------------------------------------- 節點定義
def load_object_info():
    url = os.environ.get("COMFYUI_URL", "http://127.0.0.1:8000")
    try:
        import urllib.request
        return json.load(urllib.request.urlopen(url + "/object_info", timeout=30))
    except Exception:
        snap = os.path.join(HERE, "templates", "object_info_lipsync.json")
        print(f"（連不到 ComfyUI，改用節點定義快照 {snap}）")
        return json.load(open(snap, encoding="utf-8"))


def is_widget(spec):
    t = spec[0]
    return isinstance(t, list) or t in WIDGET_TYPES


class Graph:
    """用一份描述同時產生 UI 工作流程和 API prompt。"""

    def __init__(self, info):
        self.info = info
        self.nodes = {}   # key -> dict(type, widgets, title, pos, size)
        self.links = []   # (src, slot, dst, input_name)
        self.groups = []

    def add(self, key, typ, pos, size=None, title=None, **widgets):
        self.nodes[key] = dict(type=typ, widgets=widgets, title=title, pos=pos, size=size)
        return key

    def link(self, src, slot, dst, name):
        self.links.append((src, slot, dst, name))

    def group(self, title, x, y, w, h, color="#3f789e"):
        self.groups.append({"title": title, "bounding": [x, y, w, h], "color": color, "font_size": 24, "flags": {}})

    # ---- API
    def api(self):
        out = {}
        ids = {k: str(i + 1) for i, k in enumerate(self.nodes)}
        for k, n in self.nodes.items():
            if n["type"] == "MarkdownNote":
                continue
            inputs = {name: v for name, v in n["widgets"].items() if not name.startswith("_")}
            out[ids[k]] = {"class_type": n["type"], "inputs": inputs, "_meta": {"title": n["title"] or n["type"]}}
        for s, slot, d, name in self.links:
            out[ids[d]]["inputs"][name] = [ids[s], slot]
        return out

    # ---- UI
    def _input_type(self, typ, name):
        spec = self._spec(typ, name)
        if spec is None:
            return "*"
        t = spec[0]
        return "COMBO" if isinstance(t, list) else t

    def _spec(self, typ, name):
        d = self.info[typ]["input"]
        for sec in ("required", "optional"):
            for nm, spec in d.get(sec, {}).items():
                if nm == name:
                    return spec
                if spec[0] == "COMFY_AUTOGROW_V3" and name.startswith(nm + "."):
                    tpl = spec[1]["template"]["input"]
                    inner = next(iter({**tpl.get("required", {}), **tpl.get("optional", {})}.values()))
                    return inner
        return None

    def _widget_values(self, typ, w):
        if typ == "MarkdownNote":
            return [w.get("text", "")]
        if typ == "SaveVideo":
            return [w["filename_prefix"], "auto", "auto", "auto"]
        if typ == "CreateVideo":
            return [w.get("fps", 24), "auto", "sRGB", "auto", "none"]
        vals = []
        d = self.info[typ]["input"]
        for sec in ("required", "optional"):
            for nm, spec in d.get(sec, {}).items():
                if not is_widget(spec) or spec[0] == "COMFY_DYNAMICCOMBO_V3":
                    continue
                opt = spec[1] if len(spec) > 1 else {}
                vals.append(w.get(nm, opt.get("default")))
                if spec[0] == "INT" and opt.get("control_after_generate"):
                    vals.append(w.get("_control", "fixed"))
                if opt.get("image_upload"):
                    vals.append("image")
                if opt.get("audio_upload"):
                    vals += [None, ""]
        return vals

    def ui(self, extra_note_nodes=()):
        ids = {k: i + 1 for i, k in enumerate(self.nodes)}
        ui_nodes, ui_links = {}, []
        for k, n in self.nodes.items():
            typ = n["type"]
            node = {"id": ids[k], "type": typ, "pos": n["pos"], "size": n["size"] or [320, 120], "flags": {},
                    "order": ids[k], "mode": 0, "inputs": [], "outputs": [],
                    "properties": {"Node name for S&R": typ, "cnr_id": "comfy-core"},
                    "widgets_values": self._widget_values(typ, n["widgets"])}
            if n["title"]:
                node["title"] = n["title"]
            if typ != "MarkdownNote":
                info = self.info[typ]
                for i, t in enumerate(info["output"]):
                    nm = info.get("output_name", info["output"])[i]
                    o = {"name": nm, "type": t, "links": []}
                    if info.get("output_is_list") and info["output_is_list"][i]:
                        o["shape"] = 6
                    node["outputs"].append(o)
                # 連線輸入：依定義順序列出所有非 widget 輸入（autogrow 只列有接的）
                linked = [(s, slot, name) for s, slot, d, name in self.links if d == k]
                names = [name for _, _, name in linked]
                d = info["input"]
                for sec in ("required", "optional"):
                    for nm, spec in d.get(sec, {}).items():
                        if spec[0] == "COMFY_AUTOGROW_V3":
                            for name in [x for x in names if x.startswith(nm + ".")]:
                                node["inputs"].append({"name": name, "type": self._input_type(typ, name),
                                                       "label": name.split(".", 1)[1], "link": None})
                        elif not is_widget(spec):
                            node["inputs"].append({"name": nm, "type": self._input_type(typ, nm), "link": None,
                                                   **({"shape": 7} if sec == "optional" else {})})
                for name in names:  # 接了線的 widget 輸入
                    spec = self._spec(typ, name)
                    if spec is not None and is_widget(spec) and not name.count("."):
                        node["inputs"].append({"name": name, "type": self._input_type(typ, name),
                                               "widget": {"name": name}, "link": None})
            ui_nodes[k] = node
        lid = 0
        for s, slot, d, name in self.links:
            lid += 1
            dst = ui_nodes[d]
            idx = next(i for i, x in enumerate(dst["inputs"]) if x["name"] == name)
            dst["inputs"][idx]["link"] = lid
            typ = ui_nodes[s]["outputs"][slot]["type"]
            ui_nodes[s]["outputs"][slot]["links"].append(lid)
            ui_links.append([lid, ui_nodes[s]["id"], slot, dst["id"], idx, typ])
        import uuid
        return {"id": str(uuid.uuid4()),
                "revision": 0, "last_node_id": len(ui_nodes), "last_link_id": lid,
                "nodes": list(ui_nodes.values()), "links": ui_links, "groups": self.groups,
                "config": {}, "extra": {"ds": {"scale": 0.45, "offset": [2700, 200]}}, "version": 0.4}


# ---------------------------------------------------------------- 提示詞
def parse_prompt(p):
    paras = [x.strip() for x in p.split("\n\n") if x.strip()]
    style = paras[0] if paras else ""
    chars = next((x[len("Characters:"):].strip() for x in paras if x.startswith("Characters:")), "")
    overview = next((x[len("Scene overview:"):].strip() for x in paras if x.startswith("Scene overview:")), "")
    neg = next((x for x in paras if x.startswith("No text")), "No text, subtitles, logos or watermarks of any kind; "
               "no cartoon or anime rendering; keep a real live-action texture.")
    return style, chars, overview, neg


def find_char(chars_line, singer, all_prompts):
    for line in [chars_line] + all_prompts:
        m = re.search(re.escape(singer) + r" \(", line)
        if m:
            i, depth = m.end() - 1, 0
            for j in range(i, len(line)):
                depth += {"(": 1, ")": -1}.get(line[j], 0)
                if depth == 0:
                    return line[i + 1:j]
    return ""


def lipsync_prompt(shot, cuts_sec, extra_ref=False):
    """把原本那段的提示詞改成對嘴版：分鏡時間改成偵測到的切點、加上歌詞和對嘴說明。"""
    paras = [x.strip() for x in shot.get("prompt", "").split("\n\n") if x.strip()]
    lyr = " / ".join(shot.get("lyrics", [])) or "(humming)"
    sing = (f"The singer sings the Mandarin lyrics of the soundtrack ({lyr}); her lips, jaw and breathing are "
            "precisely synchronized to the sung vocal in the audio: the mouth shapes each syllable, opens on held "
            "notes, and closes during pauses. Keep every person, place and object exactly as in the anchored frames.")
    if extra_ref:
        sing += " <Picture 1> shows the singer's face; use it only to keep her face identical."
    out, added = [], False
    for p in paras:
        if p.startswith("Storyboard"):
            lines = p.split("\n")
            # 原本的分鏡文字不一定和實際生成的畫面一致，所以改成「延續錨定畫面」，避免和錨點打架
            edges = [0.0] + list(cuts_sec) + [10.0]
            n_sh = len(edges) - 1
            body = "\n".join(
                f"[{edges[n]:.1f}s-{edges[n + 1]:.1f}s] Shot {n + 1}: continue exactly from the anchored frame at "
                f"{edges[n]:.1f}s (same framing, place, lighting and people) with gentle natural motion; whenever the "
                f"singer's face is visible, her lips move in sync with the vocal." for n in range(n_sh))
            out.append(f"Storyboard ({n_sh} shot{'s' if n_sh > 1 else ''}"
                       + (", hard cuts landing on the beat" if n_sh > 1 else ", no cuts") + "):\n" + body)
        elif p.startswith("Audio:"):
            out.append("Audio: keep the given vocal exactly as it is; no added music, no other voices.")
        else:
            out.append(p)
        if p.startswith("Scene overview") and not added:
            out.append(sing); added = True
    if not added:
        out.insert(min(2, len(out)), sing)
    return "\n\n".join(out)


# ---------------------------------------------------------------- 素材
def comfy_input_dir():
    return os.environ.get("COMFYUI_INPUT", os.path.expanduser(os.path.join("~", "Documents", "ComfyUI", "input")))


def comfy_output_dir():
    return os.environ.get("COMFYUI_OUTPUT", os.path.expanduser(os.path.join("~", "Documents", "ComfyUI", "output")))


def find_song(project, song_arg):
    if song_arg:
        return song_arg
    for f in sorted(os.listdir(project)):
        if f.lower().endswith((".mp3", ".wav", ".flac", ".m4a")) and "vocals" not in f.lower():
            return os.path.join(project, f)
    return None


def ensure_vocals(project, a):
    if a.vocals:
        return a.vocals
    stems = os.path.join(project, "stems")
    found = [os.path.join(r, f) for r, _, fs in os.walk(stems) for f in fs if f == "vocals.wav"] \
        if os.path.isdir(stems) else []
    if found:
        return found[0]
    song = find_song(project, a.song)
    if not song:
        sys.exit("找不到原曲，請用 --song 指定，或用 --vocals 給分離好的人聲")
    print(f"用 Demucs 分離人聲：{song}")
    r = subprocess.run([sys.executable, "-m", "demucs", "--two-stems", "vocals", "-n", "htdemucs", "-o", stems, song])
    if r.returncode:
        sys.exit("Demucs 失敗；請先 pip install demucs，或用 --vocals 指定人聲檔")
    return [os.path.join(rr, f) for rr, _, fs in os.walk(stems) for f in fs if f == "vocals.wav"][0]


CLIP_PAT = re.compile(r"shot_(\d{2})_(\d+)_?\.(mp4|mov|mkv|webm)$", re.I)


def newest_clips(folder):
    best = {}
    for f in os.listdir(folder) if os.path.isdir(folder) else []:
        m = CLIP_PAT.search(f)
        if m and (int(m.group(1)) not in best or int(m.group(2)) > best[int(m.group(1))][0]):
            best[int(m.group(1))] = (int(m.group(2)), os.path.join(folder, f))
    return {k: v[1] for k, v in best.items()}


def detect_cuts(path, used_sec, threshold=0.15, max_cuts=2):
    """回傳剪輯會用到的範圍內，硬切點後第一格的影格編號。"""
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-vf", f"select='gt(scene,{threshold})',metadata=print:file=-",
                        "-an", "-f", "null", "-"], capture_output=True, text=True)
    times = [float(x) for x in re.findall(r"pts_time:([0-9.]+)", r.stdout)]
    frames = [round(t * FPS) for t in times if 0.5 < t < used_sec - 0.5]
    return frames[:max_cuts]


def probe_size(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries", "stream=width,height",
                        "-of", "csv=p=0", path], capture_output=True, text=True)
    w, h = r.stdout.strip().split(",")[:2]
    return int(w), int(h)


def grab_frame(path, n, dst):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", path, "-vf", f"select=eq(n\\,{n})", "-frames:v", "1", dst],
                   check=True)


# ---------------------------------------------------------------- 主程式
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("project")
    ap.add_argument("--shots", required=True)
    ap.add_argument("--face", default=None)
    ap.add_argument("--vocals", default=None)
    ap.add_argument("--song", default=None)
    ap.add_argument("--clips", default=None)
    ap.add_argument("--prompts", default=None)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    P = os.path.abspath(a.project)
    plan = json.load(open(os.path.join(P, "shots.json"), encoding="utf-8"))
    byid = {s["id"]: s for s in plan["shots"]}
    ids = [int(x) for x in re.split(r"[,\s]+", a.shots.strip()) if x]
    bad = [i for i in ids if i not in byid]
    if bad:
        sys.exit(f"沒有這些段：{bad}")
    if len(ids) > 10:
        sys.exit("一次最多 10 段，請分批產生")
    folder = plan.get("output_folder") or "mv"
    title = plan.get("song", "MV")
    k = len(ids)

    clips_dir = a.clips or os.path.join(comfy_output_dir(), folder)
    clips = newest_clips(clips_dir)
    missing = [i for i in ids if i not in clips]
    if missing:
        sys.exit(f"這些段還沒有原本的片段（{clips_dir}）：{', '.join(f'{i:02d}' for i in missing)}\n"
                 "對嘴是第二階段，請先用一般工作流程把這幾段跑出來")

    inp = comfy_input_dir()
    os.makedirs(inp, exist_ok=True)
    voc_name = f"{folder}_vocals.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", ensure_vocals(P, a), "-ar", "48000", "-ac", "2",
                    os.path.join(inp, voc_name)], check=True)
    face_name = None
    if a.face:
        face_name = f"{folder}_lip_face{os.path.splitext(a.face)[1]}"
        shutil.copyfile(a.face, os.path.join(inp, face_name))

    # 每段的錨點：第 0 格 + 最多兩個切點；沒有切點時重複上一個錨點（同一格、同一張圖）
    W, H = probe_size(clips[ids[0]])
    anchors = []   # [(f0_name, (cutA_frame, cutA_name), (cutB_frame, cutB_name))]
    for i in ids:
        used = byid[i]["duration"]
        cuts = detect_cuts(clips[i], used)
        names = []
        for n in [0] + cuts:
            nm = f"{folder}_lip_s{i:02d}_f{n:03d}.png"
            grab_frame(clips[i], n, os.path.join(inp, nm))
            names.append((n, nm))
        while len(names) < 3:
            names.append(names[-1])
        anchors.append(names)
        print(f"  第 {i:02d} 段：{os.path.basename(clips[i])}，切點 {', '.join(f'{c / FPS:.2f}s' for c in cuts) or '無'}")

    custom = json.load(open(a.prompts, encoding="utf-8")) if a.prompts else {}
    cfg_path = os.path.join(P, "分鏡設定.json")
    cfg = json.load(open(cfg_path, encoding="utf-8")) if os.path.exists(cfg_path) else {}
    prompts = []
    for i, an in zip(ids, anchors):
        cuts_sec = sorted({n / FPS for n, _ in an[1:] if n > 0})
        prompts.append(custom.get(str(i)) or cfg.get("shots", {}).get(str(i), {}).get("lipsync_prompt")
                       or lipsync_prompt(byid[i], cuts_sec, bool(face_name)))

    def piecewise(vals, fmt=str):
        return " + ".join(f"(a == {n}) * {fmt(v)}" for n, v in enumerate(vals, 1))

    mmss = lambda x: f"{int(x // 60)}:{int(x % 60):02d}"
    g = Graph(load_object_info())

    # ① 模型
    X0, Y0 = -1790, 400
    g.group("① 模型（ref2va）", X0, Y0, 1010, 640)
    g.add("unet", "UNETLoader", [X0 + 30, Y0 + 60], [420, 82], unet_name=MODELS["unet"], weight_dtype="default")
    g.add("lora", "LoraLoaderModelOnly", [X0 + 30, Y0 + 180], [420, 82], lora_name=MODELS["lora"], strength_model=1.0)
    g.add("turbo", "PrimitiveBoolean", [X0 + 480, Y0 + 60], [300, 58], "Turbo（4 步）", value=True)
    g.add("steps_hi", "PrimitiveInt", [X0 + 480, Y0 + 160], [300, 82], "步數（Turbo 關）", value=20)
    g.add("steps_lo", "PrimitiveInt", [X0 + 480, Y0 + 280], [300, 82], "步數（Turbo 開）", value=4)
    g.add("sw_model", "ComfySwitchNode", [X0 + 30, Y0 + 300], [300, 80], "模型切換", switch=True)
    g.add("sw_steps", "ComfySwitchNode", [X0 + 480, Y0 + 400], [300, 80], "步數切換", switch=True)
    g.add("clip", "CLIPLoader", [X0 + 30, Y0 + 420], [420, 106], clip_name=MODELS["clip"], type="minimax", device="default")
    g.add("vae", "VAELoader", [X0 + 30, Y0 + 560], [420, 58], "影片 VAE", vae_name=MODELS["vae"])
    g.add("avae", "VAELoader", [X0 + 480, Y0 + 520], [420, 58], "音訊 VAE", vae_name=MODELS["avae"])
    g.link("unet", 0, "lora", "model")
    g.link("unet", 0, "sw_model", "on_false"); g.link("lora", 0, "sw_model", "on_true"); g.link("turbo", 0, "sw_model", "switch")
    g.link("steps_hi", 0, "sw_steps", "on_false"); g.link("steps_lo", 0, "sw_steps", "on_true"); g.link("turbo", 0, "sw_steps", "switch")

    # ② 選段
    X1, Y1 = -2610, 1120
    g.group("② 選段（每次 Run 會自動 +1）", X1, Y1, 820, 1360)
    g.add("lip_no", "PrimitiveInt", [X1 + 30, Y1 + 80], [360, 82], "lip_no（第幾個對嘴段）", value=1, _control="increment")
    g.add("reroll", "PrimitiveInt", [X1 + 30, Y1 + 200], [360, 82], "reroll（重抽時 +1）", value=0)
    g.add("secs", "PrimitiveFloat", [X1 + 30, Y1 + 320], [360, 58], "每段秒數", value=10.0)
    g.add("frames", "ComfyMathExpression", [X1 + 30, Y1 + 420], [360, 130], "影格數（17k+5）",
          expression="max(5, round(a * 24)) + (5 - (max(5, round(a * 24)) % 17)) % 17")
    g.add("adur", "ComfyMathExpression", [X1 + 30, Y1 + 580], [360, 110], "人聲長度（秒）", expression="a / 24")
    g.add("idx", "ComfyMathExpression", [X1 + 430, Y1 + 80], [360, 110], "清單索引", expression=f"max(0, min({k - 1}, a - 1))")
    g.add("n", "ComfyMathExpression", [X1 + 430, Y1 + 220], [360, 110], f"對嘴段（1～{k}）", expression=f"max(1, min({k}, a))")
    g.add("shot", "ComfyMathExpression", [X1 + 430, Y1 + 360], [360, 130], "段落編號", expression=piecewise(ids))
    g.add("start", "ComfyMathExpression", [X1 + 430, Y1 + 520], [360, 130], "人聲起點（秒）",
          expression=piecewise([byid[i]["start"] for i in ids], lambda v: f"{v:.3f}"))
    g.add("cutA", "ComfyMathExpression", [X1 + 430, Y1 + 680], [360, 130], "錨點 2 的影格",
          expression=piecewise([an[1][0] for an in anchors]))
    g.add("cutB", "ComfyMathExpression", [X1 + 430, Y1 + 840], [360, 130], "錨點 3 的影格",
          expression=piecewise([an[2][0] for an in anchors]))
    g.add("seed", "ComfyMathExpression", [X1 + 430, Y1 + 1000], [360, 110], "種子 seed",
          expression=f"{SEED_BASE} + a * 1000 + 700 + b")
    g.add("fname", "StringFormat", [X1 + 430, Y1 + 1140], [360, 90], "檔名", f_string=f"{folder}_lipsync/shot_{{a:02d}}")
    g.add("w", "PrimitiveInt", [X1 + 30, Y1 + 720], [360, 82], "寬（和原片段一樣）", value=W)
    g.add("h", "PrimitiveInt", [X1 + 30, Y1 + 840], [360, 82], "高（和原片段一樣）", value=H)
    g.link("lip_no", 0, "idx", "values.a"); g.link("lip_no", 0, "n", "values.a")
    for key in ("shot", "start", "cutA", "cutB"):
        g.link("n", 1, key, "values.a")
    g.link("shot", 1, "seed", "values.a"); g.link("reroll", 0, "seed", "values.b")
    g.link("shot", 1, "fname", "values.a")
    g.link("secs", 0, "frames", "values.a"); g.link("frames", 1, "adur", "values.a")

    # ③ 原片段的錨點畫面＋人聲
    X2, Y2 = -1790, 1120
    g.group("③ 原片段的錨點畫面（第 0 格、切點後第一格）＋人聲", X2, Y2, 1590, 200 + k * 300, "#8A8")
    lists = {}
    for col, (key, lab) in enumerate([("f0", "錨點 1：第 0 格"), ("fa", "錨點 2：切點後"), ("fb", "錨點 3：切點後")]):
        lk = g.add(f"list_{key}", "CreateList", [X2 + 30 + col * 380 + 1140 - 1140, Y2 + 80 + k * 300], [300, 200],
                   f"Create List（{lab}）")
        lists[key] = lk
    for j, (i, an) in enumerate(zip(ids, anchors)):
        for col, key in enumerate(("f0", "fa", "fb")):
            n, nm = an[col]
            node = g.add(f"img_{j}_{key}", "LoadImage", [X2 + 30 + col * 380, Y2 + 60 + j * 300], [360, 280],
                         f"第 {i:02d} 段・第 {n} 格", image=nm)
            g.link(node, 0, lists[key], f"inputs.input{j}")
    picks = {}
    for col, key in enumerate(("f0", "fa", "fb")):
        picks[key] = g.add(f"pick_{key}", "GetItemFromList", [X2 + 30 + col * 380, Y2 + 300 + k * 300], [300, 80],
                           "取出這段的錨點", index=0)
        g.link(lists[key], 0, picks[key], "list"); g.link("idx", 1, picks[key], "index")
    g.add("vocals", "LoadAudio", [X2 + 1180, Y2 + 60], [380, 140], "整首人聲（Demucs 分離）", audio=voc_name)
    g.add("trim", "TrimAudioDuration", [X2 + 1180, Y2 + 240], [380, 106], "切出這段的人聲")
    g.link("vocals", 0, "trim", "audio"); g.link("start", 0, "trim", "start_index"); g.link("adur", 0, "trim", "duration")
    if face_name:
        g.add("face", "LoadImage", [X2 + 1180, Y2 + 400], [380, 300], "<Picture 1> 臉部參考（選用）", image=face_name)

    # ④ 提示詞
    X3, Y3 = -1790, Y2 + 260 + k * 300 + 160
    rows = (k + 1) // 2
    g.group(f"④ {k} 段對嘴提示詞（可直接改文字）", X3, Y3, 1960, 120 + rows * 420, "#A88")
    pkeys = []
    for j, (i, p) in enumerate(zip(ids, prompts)):
        r, c = divmod(j, 2)
        s = byid[i]
        t = f"對嘴 {j + 1}｜第 {i:02d} 段｜{mmss(s['start'])}–{mmss(s['end'])}｜{' / '.join(s.get('lyrics', []))}"
        pkeys.append(g.add(f"p{j}", "PrimitiveStringMultiline", [X3 + 30 + c * 780, Y3 + 80 + r * 420], [740, 380],
                           t[:80], value=p))
    plist = g.add("plist", "CreateList", [X3 + 1620, Y3 + 80], [300, 260], "Create List（提示詞）")
    for j, pk in enumerate(pkeys):
        g.link(pk, 0, plist, f"inputs.input{j}")
    g.add("pick", "GetItemFromList", [X3 + 1620, Y3 + 380], [300, 80], "取出這段提示詞", index=0)
    g.link(plist, 0, "pick", "list"); g.link("idx", 1, "pick", "index")

    # ⑤ 生成
    X4, Y4 = 400, 1120
    g.group("⑤ 生成與輸出", X4, Y4, 1210, 1500, "#88A")
    g.add("r2v", "MiniMaxH3ReferenceToVideo", [X4 + 30, Y4 + 80], [420, 260], prompt="", width=W, height=H,
          length=243, ref_image_size="match")
    g.add("g0", "MiniMaxH3AddGuide", [X4 + 30, Y4 + 380], [420, 170], "錨點 1＋人聲（第 0 格）", frame_idx=0)
    g.add("ga", "MiniMaxH3AddGuide", [X4 + 30, Y4 + 580], [420, 170], "錨點 2（切點後第一格）", frame_idx=0)
    g.add("gb", "MiniMaxH3AddGuide", [X4 + 30, Y4 + 780], [420, 170], "錨點 3（切點後第一格）", frame_idx=0)
    g.add("noise", "RandomNoise", [X4 + 480, Y4 + 80], [320, 82], noise_seed=SEED_BASE)
    g.add("ksel", "KSamplerSelect", [X4 + 480, Y4 + 200], [320, 58], sampler_name="res_multistep")
    g.add("sched", "BasicScheduler", [X4 + 480, Y4 + 300], [320, 106], scheduler="simple", steps=4, denoise=1.0)
    g.add("guider", "BasicGuider", [X4 + 480, Y4 + 440], [320, 46])
    g.add("sampler", "SamplerCustomAdvanced", [X4 + 830, Y4 + 80], [340, 106])
    g.add("dec", "VAEDecode", [X4 + 830, Y4 + 240], [240, 46])
    g.add("adec", "VAEDecodeAudio", [X4 + 830, Y4 + 320], [240, 46])
    g.add("cv", "CreateVideo", [X4 + 830, Y4 + 400], [300, 150], fps=FPS)
    g.add("save", "SaveVideo", [X4 + 30, Y4 + 1000], [1140, 460], filename_prefix=f"{folder}_lipsync/shot",
          format="auto", codec="auto")
    for src, slot, dst, nm in [
        ("clip", 0, "r2v", "clip"), ("vae", 0, "r2v", "vae"), ("avae", 0, "r2v", "audio_vae"),
        ("pick", 0, "r2v", "prompt"), ("w", 0, "r2v", "width"), ("h", 0, "r2v", "height"), ("frames", 1, "r2v", "length"),
        ("r2v", 0, "g0", "positive"), ("r2v", 1, "g0", "latent"), ("vae", 0, "g0", "vae"), ("avae", 0, "g0", "audio_vae"),
        ("pick_f0", 0, "g0", "image"), ("trim", 0, "g0", "audio"),
        ("g0", 0, "ga", "positive"), ("r2v", 1, "ga", "latent"), ("vae", 0, "ga", "vae"), ("avae", 0, "ga", "audio_vae"),
        ("pick_fa", 0, "ga", "image"), ("cutA", 1, "ga", "frame_idx"),
        ("ga", 0, "gb", "positive"), ("r2v", 1, "gb", "latent"), ("vae", 0, "gb", "vae"), ("avae", 0, "gb", "audio_vae"),
        ("pick_fb", 0, "gb", "image"), ("cutB", 1, "gb", "frame_idx"),
        ("seed", 1, "noise", "noise_seed"), ("sw_model", 0, "sched", "model"), ("sw_steps", 0, "sched", "steps"),
        ("sw_model", 0, "guider", "model"), ("gb", 0, "guider", "conditioning"),
        ("noise", 0, "sampler", "noise"), ("guider", 0, "sampler", "guider"), ("ksel", 0, "sampler", "sampler"),
        ("sched", 0, "sampler", "sigmas"), ("r2v", 1, "sampler", "latent_image"),
        ("sampler", 0, "dec", "samples"), ("vae", 0, "dec", "vae"),
        ("sampler", 0, "adec", "samples"), ("avae", 0, "adec", "vae"),
        ("dec", 0, "cv", "images"), ("adec", 0, "cv", "audio"),
        ("cv", 0, "save", "video"), ("fname", 0, "save", "filename_prefix"),
    ]:
        g.link(src, slot, dst, nm)
    if face_name:
        g.link("face", 0, "r2v", "ref_images.ref_image_0")

    table = "\n".join(
        f"| {j + 1} | {i:02d} | {mmss(byid[i]['start'])} | {', '.join(f'{n / FPS:.2f}s' for n, _ in an[1:] if n) or '無'} | "
        f"{' / '.join(byid[i].get('lyrics', [])) or '（無）'} |" for j, (i, an) in enumerate(zip(ids, anchors)))
    g.add("note", "MarkdownNote", [-2610, -40], [760, 1080], "使用說明（先看這裡）", text=(
        f"## 《{title}》對嘴版｜第二階段（{k} 段）\n\n"
        "從**原本生成好的片段**重新生成：第 0 格和每個切點後的第一格當錨點，場景、人物、剪接點跟原本一樣；"
        "那段的人聲錨定在第 0 格，動作和嘴型重新生成、跟著歌聲。全部內建節點，不用點數。\n\n"
        "| 對嘴 | 段落 | 起點 | 切點 | 歌詞 |\n|---|---|---|---|---|\n" + table + "\n\n"
        "### 跑法\n"
        "1. `lip_no` 設 **1**、Run 旁的批次數量設 **1**，先測第一個\n"
        f"2. 一次跑完：`lip_no` 設 1、批次數量設 **{k}**\n"
        "3. 重抽：`lip_no` 設那個編號、`reroll` +1、批次數量 1\n\n"
        "### 輸出\n"
        f"存到 `output/{folder}_lipsync/shot_XX_…mp4`，不會蓋掉原本那段。剪輯時指定要換上的段落：\n"
        f"`python assemble_mv.py --project … --subs --lipsync {','.join(str(i) for i in ids)}`\n\n"
        "### 小提醒\n"
        "- 原片段裡臉要清楚；遠景、背影的段落對嘴效果有限\n"
        "- 原片段重抽過的話，重新執行 build_lipsync_workflow.py，錨點會換成最新版本的畫面\n"
        "- Turbo 預設開（4 步）；關掉 = 20 步，畫質較好、約慢 4～5 倍"))

    outp = a.out or os.path.join(P, f"{title}_對嘴_MiniMaxH3_本機.json")
    json.dump(g.ui(), open(outp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump(g.api(), open(os.path.join(P, "lipsync_api.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"已產生 {outp}")
    print(f"  對嘴 {k} 段：{', '.join(f'{i:02d}' for i in ids)}；{W}×{H}；輸出到 output/{folder}_lipsync/")


if __name__ == "__main__":
    main()
