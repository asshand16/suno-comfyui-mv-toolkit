#!/usr/bin/env python3
"""
產生「對嘴」ComfyUI 工作流程（MiniMax H3 ref2va，全部內建節點）。

人物參考圖固定長相，人聲軌錨定在第 0 格，讓嘴型跟著歌聲動。
輸出存成 output/<輸出資料夾>/shot_XX_000NN_.mp4，和一般片段同一個資料夾，
assemble_mv.py 會自動用編號最新的版本，所以對嘴版會直接取代原本那段。

用法：
  python build_lipsync_workflow.py <專案> --shots 9,10,15 --face 臉部特寫.png [--body 全身.png]
                                   [--singer "the woman"] [--vocals 人聲.wav] [--song 原曲.mp3]

  --shots   要對嘴的段落編號（逗號分隔）
  --face    人物臉部參考圖（正面特寫最好），必填
  --body    第二張參考圖（服裝、全身），選填
  --singer  提示詞裡唱歌的角色名稱，要和 shots.json 的 Characters 一樣（預設 the woman）
  --vocals  已經分離好的人聲檔；沒給就找 <專案>/stems/vocals.wav，再沒有就用 Demucs 從原曲分離

產出：
  <專案>/<歌名>_對嘴_MiniMaxH3_本機.json     拖進 ComfyUI 用
  <專案>/lipsync_api.json                     API 格式（給程式直接送出）
  ComfyUI/input/<輸出資料夾>_vocals.wav、_lip_face.png、_lip_body.png
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


def lipsync_prompt(shot, singer, desc, refs, all_prompts):
    style, chars, overview, neg = parse_prompt(shot.get("prompt", ""))
    desc = desc or find_char(chars, singer, all_prompts)
    pics = "<Picture 1> and <Picture 2> show" if refs == 2 else "<Picture 1> shows"
    who = f"{singer} ({desc})" if desc else singer
    lyr = " / ".join(shot.get("lyrics", [])) or "(humming)"
    return "\n\n".join([
        style,
        f"{pics} {who}. Keep the face, hair and outfit exactly as in the reference{'s' if refs == 2 else ''}.",
        f"Scene overview: {overview} {singer[0].upper() + singer[1:]} faces the camera and sings the lyrics of the "
        f"soundtrack with deep emotion; the lips, jaw and breathing are precisely synchronized to the sung vocal "
        f"in the audio: the mouth opens on held notes, shapes each syllable, and closes during pauses.",
        f"Lyrics sung in this shot (Mandarin): {lyr}",
        "Storyboard (1 continuous shot):\n[0.0s-10.0s] Shot 1: slow push-in from a medium close-up to a close-up "
        "of the singer's face, the face clearly visible and well lit the whole time.",
        "Camera: slow, smooth push-in, no cuts, no shaky handheld.",
        "Audio: keep the given vocal exactly as it is; no added music, no other voices.",
        neg,
    ])


# ---------------------------------------------------------------- 素材
def comfy_input_dir():
    return os.environ.get("COMFYUI_INPUT", os.path.expanduser(os.path.join("~", "Documents", "ComfyUI", "input")))


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
    for cand in (os.path.join(project, "stems", "vocals.wav"),):
        if os.path.exists(cand):
            return cand
    found = [os.path.join(r, f) for r, _, fs in os.walk(os.path.join(project, "stems")) for f in fs if f == "vocals.wav"] \
        if os.path.isdir(os.path.join(project, "stems")) else []
    if found:
        return found[0]
    song = find_song(project, a.song)
    if not song:
        sys.exit("找不到原曲，請用 --song 指定，或用 --vocals 給分離好的人聲")
    print(f"用 Demucs 分離人聲：{song}")
    out = os.path.join(project, "stems")
    r = subprocess.run([sys.executable, "-m", "demucs", "--two-stems", "vocals", "-n", "htdemucs", "-o", out, song])
    if r.returncode:
        sys.exit("Demucs 失敗；請先 pip install demucs，或用 --vocals 指定人聲檔")
    found = [os.path.join(rr, f) for rr, _, fs in os.walk(out) for f in fs if f == "vocals.wav"]
    return found[0]


# ---------------------------------------------------------------- 主程式
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("project")
    ap.add_argument("--shots", required=True)
    ap.add_argument("--face", required=True)
    ap.add_argument("--body", default=None)
    ap.add_argument("--singer", default="the woman")
    ap.add_argument("--desc", default="", help="角色外型（英文）；沒給就從提示詞的 Characters 找")
    ap.add_argument("--vocals", default=None)
    ap.add_argument("--song", default=None)
    ap.add_argument("--megapixels", type=float, default=0.3)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    P = os.path.abspath(a.project)
    plan = json.load(open(os.path.join(P, "shots.json"), encoding="utf-8"))
    byid = {s["id"]: s for s in plan["shots"]}
    ids = [int(x) for x in re.split(r"[,\s]+", a.shots.strip()) if x]
    bad = [i for i in ids if i not in byid]
    if bad:
        sys.exit(f"沒有這些段：{bad}")
    folder = plan.get("output_folder") or "mv"
    title = plan.get("song", "MV")
    k = len(ids)

    # 素材放進 ComfyUI/input
    inp = comfy_input_dir()
    os.makedirs(inp, exist_ok=True)
    voc_name = f"{folder}_vocals.wav"
    vocals = ensure_vocals(P, a)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", vocals, "-ar", "48000", "-ac", "2",
                    os.path.join(inp, voc_name)], check=True)
    face_name = f"{folder}_lip_face{os.path.splitext(a.face)[1]}"
    shutil.copyfile(a.face, os.path.join(inp, face_name))
    body_name = None
    if a.body:
        body_name = f"{folder}_lip_body{os.path.splitext(a.body)[1]}"
        shutil.copyfile(a.body, os.path.join(inp, body_name))
    refs = 2 if body_name else 1

    all_prompts = [s.get("prompt", "") for s in plan["shots"]]
    cfg_path = os.path.join(P, "分鏡設定.json")
    cfg = json.load(open(cfg_path, encoding="utf-8")) if os.path.exists(cfg_path) else {}
    prompts = []
    for i in ids:
        override = cfg.get("shots", {}).get(str(i), {}).get("lipsync_prompt")
        prompts.append(override or lipsync_prompt(byid[i], a.singer, a.desc, refs, all_prompts))

    def piecewise(vals, fmt):
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
    g.group("② 選段（每次 Run 會自動 +1）", X1, Y1, 820, 1180)
    g.add("lip_no", "PrimitiveInt", [X1 + 30, Y1 + 80], [360, 82], "lip_no（第幾個對嘴段）", value=1, _control="increment")
    g.add("reroll", "PrimitiveInt", [X1 + 30, Y1 + 200], [360, 82], "reroll（重抽時 +1）", value=0)
    g.add("secs", "PrimitiveFloat", [X1 + 30, Y1 + 320], [360, 58], "每段秒數", value=10.0)
    g.add("idx", "ComfyMathExpression", [X1 + 430, Y1 + 80], [360, 110], "清單索引", expression=f"max(0, min({k - 1}, a - 1))")
    g.add("n", "ComfyMathExpression", [X1 + 430, Y1 + 220], [360, 110], "對嘴段（1～%d）" % k, expression=f"max(1, min({k}, a))")
    g.add("shot", "ComfyMathExpression", [X1 + 430, Y1 + 360], [360, 150], "段落編號",
          expression=piecewise(ids, str))
    g.add("start", "ComfyMathExpression", [X1 + 430, Y1 + 540], [360, 150], "人聲起點（秒）",
          expression=piecewise([byid[i]["start"] for i in ids], lambda v: f"{v:.3f}"))
    g.add("seed", "ComfyMathExpression", [X1 + 430, Y1 + 720], [360, 110], "種子 seed",
          expression=f"{SEED_BASE} + a * 1000 + 500 + b")
    g.add("fname", "StringFormat", [X1 + 430, Y1 + 860], [360, 90], "檔名", f_string=f"{folder}/shot_{{a:02d}}")
    g.add("frames", "ComfyMathExpression", [X1 + 30, Y1 + 420], [360, 130], "影格數（17k+5）",
          expression="max(5, round(a * 24)) + (5 - (max(5, round(a * 24)) % 17)) % 17")
    g.add("adur", "ComfyMathExpression", [X1 + 30, Y1 + 580], [360, 110], "人聲長度（秒）", expression="a / 24")
    g.add("res", "ResolutionSelector", [X1 + 30, Y1 + 720], [360, 130], aspect_ratio="16:9 (Widescreen)",
          megapixels=a.megapixels, multiple=32)
    g.link("lip_no", 0, "idx", "values.a"); g.link("lip_no", 0, "n", "values.a")
    g.link("n", 1, "shot", "values.a"); g.link("n", 1, "start", "values.a")
    g.link("shot", 1, "seed", "values.a"); g.link("reroll", 0, "seed", "values.b")
    g.link("shot", 1, "fname", "values.a")
    g.link("secs", 0, "frames", "values.a"); g.link("frames", 1, "adur", "values.a")

    # ③ 參考素材
    X2, Y2 = -1790, 1120
    g.group("③ 參考素材（人物圖、人聲）", X2, Y2, 1010, 1180, "#8A8")
    g.add("face", "LoadImage", [X2 + 30, Y2 + 80], [460, 420], "<Picture 1> 臉部特寫", image=face_name)
    if body_name:
        g.add("body", "LoadImage", [X2 + 520, Y2 + 80], [460, 420], "<Picture 2> 服裝／全身", image=body_name)
    g.add("vocals", "LoadAudio", [X2 + 30, Y2 + 540], [460, 140], "整首人聲（Demucs 分離）", audio=voc_name)
    g.add("trim", "TrimAudioDuration", [X2 + 520, Y2 + 540], [360, 106], "切出這段的人聲")
    g.link("vocals", 0, "trim", "audio"); g.link("start", 0, "trim", "start_index"); g.link("adur", 0, "trim", "duration")

    # ④ 提示詞
    X3, Y3 = -1790, 2340
    rows = (k + 1) // 2
    g.group(f"④ {k} 段對嘴提示詞（可直接改文字）", X3, Y3, 1590, 120 + rows * 420, "#A88")
    pkeys = []
    for j, (i, p) in enumerate(zip(ids, prompts)):
        r, c = divmod(j, 2)
        s = byid[i]
        t = f"對嘴 {j + 1}｜第 {i:02d} 段｜{mmss(s['start'])}–{mmss(s['end'])}｜{' / '.join(s.get('lyrics', []))}"
        pkeys.append(g.add(f"p{j}", "PrimitiveStringMultiline", [X3 + 30 + c * 780, Y3 + 80 + r * 420], [740, 380],
                           t[:80], value=p))
    chunks = [pkeys[x:x + 9] for x in range(0, k, 9)]
    lists = []
    for ci, ch in enumerate(chunks):
        lk = g.add(f"list{ci}", "CreateList", [X3 + 1620, Y3 + 80 + ci * 290], [300, 260], f"Create List（{ci * 9 + 1}～{ci * 9 + len(ch)}）")
        for j, pk in enumerate(ch):
            g.link(pk, 0, lk, f"inputs.input{j}")
        lists.append(lk)
    if len(lists) > 1:
        top = g.add("listall", "CreateList", [X3 + 1620, Y3 + 80 + len(lists) * 290], [300, 160], "Create List（合併）")
        for j, lk in enumerate(lists):
            g.link(lk, 0, top, f"inputs.input{j}")
    else:
        top = lists[0]
    g.add("pick", "GetItemFromList", [X3 + 1620, Y3 + 80 + (len(lists) + 1) * 290], [300, 80], "取出這段提示詞", index=0)
    g.link(top, 0, "pick", "list"); g.link("idx", 1, "pick", "index")

    # ⑤ 生成
    X4, Y4 = 400, 1120
    g.group("⑤ 生成與輸出", X4, Y4, 1210, 1300, "#88A")
    g.add("r2v", "MiniMaxH3ReferenceToVideo", [X4 + 30, Y4 + 80], [420, 260], prompt="", width=736, height=416,
          length=243, ref_image_size="match")
    g.add("guide", "MiniMaxH3AddGuide", [X4 + 30, Y4 + 380], [420, 170], "人聲錨定在第 0 格", frame_idx=0)
    g.add("noise", "RandomNoise", [X4 + 480, Y4 + 80], [320, 82], noise_seed=SEED_BASE)
    g.add("ksel", "KSamplerSelect", [X4 + 480, Y4 + 200], [320, 58], sampler_name="res_multistep")
    g.add("sched", "BasicScheduler", [X4 + 480, Y4 + 300], [320, 106], scheduler="simple", steps=4, denoise=1.0)
    g.add("guider", "BasicGuider", [X4 + 480, Y4 + 440], [320, 46])
    g.add("sampler", "SamplerCustomAdvanced", [X4 + 830, Y4 + 80], [340, 106])
    g.add("dec", "VAEDecode", [X4 + 830, Y4 + 240], [240, 46])
    g.add("adec", "VAEDecodeAudio", [X4 + 830, Y4 + 320], [240, 46])
    g.add("cv", "CreateVideo", [X4 + 830, Y4 + 400], [300, 150], fps=FPS)
    g.add("save", "SaveVideo", [X4 + 30, Y4 + 600], [1140, 640], filename_prefix=f"{folder}/shot")
    for src, slot, dst, nm in [
        ("clip", 0, "r2v", "clip"), ("vae", 0, "r2v", "vae"), ("avae", 0, "r2v", "audio_vae"),
        ("face", 0, "r2v", "ref_images.ref_image_0"), ("pick", 0, "r2v", "prompt"),
        ("res", 0, "r2v", "width"), ("res", 1, "r2v", "height"), ("frames", 1, "r2v", "length"),
        ("r2v", 0, "guide", "positive"), ("r2v", 1, "guide", "latent"), ("avae", 0, "guide", "audio_vae"),
        ("trim", 0, "guide", "audio"),
        ("seed", 1, "noise", "noise_seed"), ("sw_model", 0, "sched", "model"), ("sw_steps", 0, "sched", "steps"),
        ("sw_model", 0, "guider", "model"), ("guide", 0, "guider", "conditioning"),
        ("noise", 0, "sampler", "noise"), ("guider", 0, "sampler", "guider"), ("ksel", 0, "sampler", "sampler"),
        ("sched", 0, "sampler", "sigmas"), ("r2v", 1, "sampler", "latent_image"),
        ("sampler", 0, "dec", "samples"), ("vae", 0, "dec", "vae"),
        ("sampler", 0, "adec", "samples"), ("avae", 0, "adec", "vae"),
        ("dec", 0, "cv", "images"), ("adec", 0, "cv", "audio"),
        ("cv", 0, "save", "video"), ("fname", 0, "save", "filename_prefix"),
    ]:
        g.link(src, slot, dst, nm)
    if body_name:
        g.link("body", 0, "r2v", "ref_images.ref_image_1")

    lines = "\n".join(f"| {j + 1} | {i:02d} | {mmss(byid[i]['start'])} | {' / '.join(byid[i].get('lyrics', [])) or '（無）'} |"
                      for j, i in enumerate(ids))
    g.add("note", "MarkdownNote", [-2610, -40], [760, 1080], "使用說明（先看這裡）", text=(
        f"## 《{title}》對嘴版｜MiniMax H3 ref2va（{k} 段）\n\n"
        "人物參考圖固定長相，該段的人聲錨定在第 0 格，嘴型會跟著歌聲動。全部內建節點，不用點數。\n\n"
        "| 對嘴 | 段落 | 起點 | 歌詞 |\n|---|---|---|---|\n" + lines + "\n\n"
        "### 跑法\n"
        f"1. `lip_no` 設 **1**、Run 旁的批次數量設 **1**，先測第一個\n"
        f"2. 一次跑完：`lip_no` 設 1、批次數量設 **{k}**\n"
        "3. 重抽：`lip_no` 設那個編號、`reroll` +1、批次數量 1\n\n"
        "### 輸出\n"
        f"存到 `output/{folder}/shot_XX_…mp4`，和一般片段同一個資料夾。剪輯腳本會自動用編號最新的版本，"
        "所以對嘴版會直接取代原本那段；不滿意就刪掉對嘴版的檔案。\n\n"
        "### 小提醒\n"
        "- 人物要正面、臉夠大，嘴型才看得清楚；解析度預設 0.3，對嘴段可改 0.6 看細節\n"
        "- Turbo 預設開（4 步）；關掉 = 20 步，畫質較好、約慢 4～5 倍\n"
        "- 換人物：換 ③ 的參考圖；換歌聲：換 ③ 的人聲檔"))

    outp = a.out or os.path.join(P, f"{title}_對嘴_MiniMaxH3_本機.json")
    json.dump(g.ui(), open(outp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump(g.api(), open(os.path.join(P, "lipsync_api.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"已產生 {outp}")
    print(f"  對嘴 {k} 段：{', '.join(f'{i:02d}' for i in ids)}；輸出到 output/{folder}/")
    print(f"  素材已放進 {inp}：{voc_name}、{face_name}" + (f"、{body_name}" if body_name else ""))


if __name__ == "__main__":
    main()
