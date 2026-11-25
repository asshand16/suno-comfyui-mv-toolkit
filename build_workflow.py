#!/usr/bin/env python3
"""
依專案的 shots.json 產生 ComfyUI 工作流程（MiniMax H3、全部內建節點）。

用法：
  python build_workflow.py projects/某首歌
  python build_workflow.py projects/某首歌 --template 汪洋MV_MiniMaxH3_本機.json

以《汪洋》的工作流程當範本：模型、生成、存檔那幾組節點原封不動，
只重建「分鏡提示詞」那一區（段數 = shots.json 的段數），並更新：
  - 選段的上限（clip_no 超過段數會停在最後一段）
  - 輸出資料夾 output/<output_folder>/shot_XX
  - 左上角使用說明
CreateList 一個最多接 10 個輸入，所以每 9 段接一個，再合併成一個清單（最多 90 段）。
"""
import argparse, copy, json, math, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
COLS = [-1760, -1170, -580]
ROW0, ROWH = 1200, 380
CHUNK = 9


def mmss(x):
    return f"{int(x // 60)}:{int(x % 60):02d}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("project")
    default_tpl = next((p for p in (os.path.join(HERE, "templates", "MiniMaxH3_本機_範本.json"),
                                    os.path.join(HERE, "汪洋MV_MiniMaxH3_本機.json")) if os.path.exists(p)), None)
    ap.add_argument("--template", default=default_tpl, help="範本工作流程（預設 templates/MiniMaxH3_本機_範本.json）")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    plan = json.load(open(os.path.join(a.project, "shots.json"), encoding="utf-8"))
    shots = plan["shots"]
    N = len(shots)
    if N > CHUNK * 10:
        sys.exit(f"段數 {N} 超過 {CHUNK * 10}，請把 prepare_song.py 的 --target 調長一點")
    empty = [s["id"] for s in shots if not s.get("prompt", "").strip()]
    if empty:
        sys.exit("這些段還沒有提示詞：" + ", ".join(f"{i:02d}" for i in empty))
    folder = plan.get("output_folder") or "mv"
    title = plan.get("song", "MV")

    w = json.load(open(a.template, encoding="utf-8"))
    nodes = {n["id"]: n for n in w["nodes"]}
    links = {l[0]: l for l in w["links"]}

    get_item = next(n for n in w["nodes"] if n["type"] == "GetItemFromList")
    merge = nodes[links[get_item["inputs"][0]["link"]][1]]
    chunk_ids = [links[i["link"]][1] for i in merge["inputs"] if i.get("link")]
    prompt_ids = [links[i["link"]][1] for c in chunk_ids for i in nodes[c]["inputs"] if i.get("link")]
    proto_prompt = copy.deepcopy(nodes[prompt_ids[0]])
    proto_chunk = copy.deepcopy(nodes[chunk_ids[0]])

    # 拿掉舊的提示詞和分組清單，以及連到它們的線
    drop = set(chunk_ids) | set(prompt_ids)
    w["nodes"] = [n for n in w["nodes"] if n["id"] not in drop]
    for lid in [l[0] for l in w["links"] if l[1] in drop or l[3] in drop]:
        links.pop(lid)
    for i in merge["inputs"]:
        i["link"] = None
    w["links"] = list(links.values())

    nid = [w["last_node_id"]]
    lid = [w["last_link_id"]]

    def new_node(proto, **kw):
        nid[0] += 1
        n = copy.deepcopy(proto); n["id"] = nid[0]; n.update(kw)
        w["nodes"].append(n)
        return n

    def connect(src, src_slot, dst, dst_slot, typ):
        lid[0] += 1
        w["links"].append([lid[0], src["id"], src_slot, dst["id"], dst_slot, typ])
        src["outputs"][src_slot].setdefault("links", [])
        src["outputs"][src_slot]["links"] = (src["outputs"][src_slot]["links"] or []) + [lid[0]]
        dst["inputs"][dst_slot]["link"] = lid[0]

    # 提示詞節點：一列 3 個
    pnodes = []
    for k, s in enumerate(shots):
        r, c = divmod(k, 3)
        t = f"{s['id']:02d}｜{mmss(s['start'])}–{mmss(s['end'])}｜{s.get('section', '')}｜{s.get('summary_zh', '')}"
        n = new_node(proto_prompt, pos=[COLS[c], ROW0 + r * ROWH], title=t[:80], widgets_values=[s["prompt"]])
        n["outputs"][0]["links"] = []
        pnodes.append(n)

    # 每 9 段一個 CreateList
    nchunks = math.ceil(N / CHUNK)
    cnodes = []
    for k in range(nchunks):
        lo, hi = k * CHUNK + 1, min(N, (k + 1) * CHUNK)
        n = new_node(proto_chunk, pos=[50, ROW0 + k * 290], title=f"Create List（第 {lo}～{hi} 段）")
        for i in n["inputs"]:
            i["link"] = None
        n["outputs"][0]["links"] = []
        for j, p in enumerate(pnodes[lo - 1:hi]):
            connect(p, 0, n, j, "STRING")
        cnodes.append(n)

    # 合併清單：輸入槽數 = 分組數 + 1（ComfyUI 的動態輸入慣例）
    slot = copy.deepcopy(merge["inputs"][1])
    merge["inputs"] = merge["inputs"][:1] + [
        dict(copy.deepcopy(slot), name=f"inputs.input{i}", localized_name=f"input{i}", label=f"input{i}")
        for i in range(1, nchunks + 1)]
    merge["inputs"][0]["link"] = None
    for k, c in enumerate(cnodes):
        connect(c, 0, merge, k, "*")
    merge["title"] = f"Create List（合併成 {N} 段）"
    ybot = ROW0 + nchunks * 290
    merge["pos"] = [50, ybot + 20]
    get_item["pos"] = [50, ybot + 180]

    # 其他設定
    for n in w["nodes"]:
        if n["type"] == "ComfyMathExpression" and "min(" in str(n.get("widgets_values", [""])[0]):
            n["widgets_values"][0] = f"max(0, min({N - 1}, a - 1))"
        if n["type"] == "StringFormat":
            n["widgets_values"][0] = f"{folder}/shot_{{a:02d}}"
        if n["type"] == "PrimitiveInt" and n.get("title") == "clip_no":
            n["widgets_values"][0] = 1
        if n["type"] == "PrimitiveInt" and str(n.get("title", "")).startswith("reroll"):
            n["widgets_values"][0] = 0
    rows = math.ceil(N / 3)
    for g in w["groups"]:
        if "③" in g["title"] or "分鏡" in g["title"]:
            g["title"] = f"③ {N} 段分鏡提示詞（可直接改文字）"
            g["bounding"][3] = max(rows * ROWH + 120, ybot + 320 - ROW0)
    note = next((n for n in w["nodes"] if n["type"] == "MarkdownNote" and n["id"] == 1), None)
    if note:
        note["widgets_values"][0] = (
            f"## 《{title}》MV｜MiniMax H3 本機分段生成（{N} 段）\n\n"
            f"**整首 {mmss(plan['duration'])} 分成 {N} 段，每段生成 10 秒，剪輯時修到對齊節拍的長度。"
            f"全部是 ComfyUI 內建節點，不用點數。**\n\n"
            "### 測第 1 段\n1. `clip_no` 設 **1**，Run 旁的批次數量設 **1**，按 Run\n"
            "2. 看花多久、畫面對不對\n\n"
            f"### 一次跑完\n3. `clip_no` 設 **1**，批次數量設 **{N}**，按一次 Run，會依序做第 1～{N} 段"
            f"（排完後 `clip_no` 顯示 {N + 1} 是正常的）\n"
            f"4. 檔案存在 `ComfyUI/output/{folder}/shot_01_00001_.mp4` … `shot_{N:02d}_…mp4`\n\n"
            "### 重抽某一段\n- `clip_no` 設那段的編號、`reroll` +1、批次數量 1、Run\n"
            "- 改畫面：直接改「③」裡那一段的文字\n\n"
            "### 中斷後接續\n- `clip_no` 設下一個還沒做的段，批次數量設剩下的段數\n\n"
            "### 速度／顯存\n- Turbo（8 步）預設開；關掉 = 20 步，畫質較好，約慢 2.5 倍\n"
            "- 解析度：Resolution Selector 預設 megapixels **0.3**（736×416）；要更清楚可改 0.6（1056×608）或 0.98（1344×768），會慢很多\n\n"
            f"### 剪成整首\n- `python assemble_mv.py --project \"{os.path.abspath(a.project)}\" --subs`")
    w["last_node_id"], w["last_link_id"] = nid[0], lid[0]

    # 自我檢查：每條線兩端都對得上
    byid = {n["id"]: n for n in w["nodes"]}
    for l in w["links"]:
        assert l[1] in byid and l[3] in byid, l
        assert byid[l[3]]["inputs"][l[4]]["link"] == l[0], l
        assert l[0] in (byid[l[1]]["outputs"][l[2]]["links"] or []), l

    out = a.out or os.path.join(a.project, f"{title}MV_MiniMaxH3_本機.json")
    json.dump(w, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"已產生 {out}（{N} 段提示詞、{nchunks} 個分組清單、輸出到 output/{folder}/）")


if __name__ == "__main__":
    main()
