# Suno → ComfyUI MV 工具包（MiniMax H3 本機生成）

把一首 Suno 生成的歌，做成**切點對齊節拍、附歌詞字幕**的完整 MV。
影片在自己電腦的 ComfyUI 上用 **MiniMax H3** 生成，全部是內建節點，不用點數。

流程：

```
Suno 歌曲資料（歌詞、節拍、逐字時間）
  → prepare_song.py   依節拍切成每段約 9 秒，產生字幕
  → 分鏡設定.json      每段的畫面（人或 AI 寫）
  → write_prompts.py  組成英文提示詞
  → build_workflow.py 產生 ComfyUI 工作流程（段數自動）
  → ComfyUI 逐段生成 10 秒影片
  → assemble_mv.py    剪到節拍長度、換上原曲、燒字幕
```

範例：[`examples/汪洋/`](examples/汪洋/)：一首 3:58 的歌，27 段的分鏡、提示詞、字幕和工作流程。

---

## 需要的東西

- **ComfyUI**（新版，要有內建的 MiniMax H3 節點）
- **MiniMax H3 模型**（約 39 GB），放進 `ComfyUI/models/`：

  | 資料夾 | 檔名 | 大小 |
  |---|---|---|
  | `diffusion_models/` | `minimax_h3_fl2va_pruned_int8_convrot.safetensors` | 19.5 GB |
  | `text_encoders/` | `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors` | 14.6 GB |
  | `vae/` | `minimax_h3_video_vae_int8_convrot.safetensors` | 2.6 GB |
  | `vae/` | `minimax_h3_audio_vae_fp32.safetensors` | 0.6 GB |
  | `loras/` | `minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors` | 1.8 GB |

  下載：<https://huggingface.co/Comfy-Org/MiniMax-H3>（Turbo LoRA：<https://huggingface.co/lightx2v/Minimax-h3-Turbo>）
- **Python 3.8+**
- **ffmpeg**（剪輯用；Windows：`winget install ffmpeg`）

在 RTX 3090 24GB 上，預設 736×416（megapixels 0.3）、Turbo 8 步，每段約 7～8 分鐘。

---

## 做一首新歌

### 1. 準備專案資料夾

開一個資料夾放這首歌，例如 `projects/我的歌/`，放進：

| 檔案 | 內容 |
|---|---|
| `歌名.mp3` | 原曲 |
| `suno_retrieve.json` | 歌名、風格、**帶段落標籤的歌詞**（`[Verse 1]`、`[Chorus]`…） |
| `suno_analysis.json` | BPM、調性、拍子 |
| `suno_alignment.json` | 每個字的演唱時間 |

這三份 JSON 是 Suno API 的回傳內容（`/audio/{id}`、`/audio/{id}/analysis`、`/audio/{id}/lyrics-alignment`）。
用 Claude 搭配 Suno 連接器時，直接請它把資料存進來即可。也接受精簡格式：

```jsonc
// suno_analysis.json：只存強拍，其餘拍子會自動補上
{"bpm": 108.2, "duration_s": 238.4, "key": "F minor", "time_signature": 4,
 "downbeats": [2.364, 6.779, 11.197, ...]}

// suno_alignment.json：每個字 [文字, 開始秒, 結束秒]，"\n" 代表換句
{"words": [["你", 18.254, 18.569], ["又", 18.569, 18.649], ["\n潮", 22.632, 22.712], ...]}
```

### 2. 切段和字幕

```
python prepare_song.py projects/我的歌 --folder my_song_mv
```

- 每段 4～10 秒（理想 8.8 秒），切點都在拍子上，優先強拍
- 每個段落（主歌、副歌…）從人聲進來前的強拍開始
- 不切在唱一個字的中間；歌詞唱完後的器樂尾奏另成一段
- 字幕前後句不重疊（字幕高度不會跳）
- 想少幾段：`--target 9.5`；多幾段：`--target 7.5`

產生 `shots.json`（切點）、`lyrics.srt`、`分鏡草稿.md`、`song_info.json`。

### 3. 寫分鏡

在專案資料夾建立 `分鏡設定.json`（格式見 `write_prompts.py` 開頭說明）：共用風格、角色外型、每段 1～3 個鏡頭。然後：

```
python write_prompts.py projects/我的歌
```

每段的鏡頭時間會依那段實際長度、對齊拍子分配。產生的提示詞結構：

```
[共用風格]
Characters: [這段出場的角色和外型]
Scene overview: …
Storyboard (2 shots, hard cuts landing on the beat):
[0.0s-3.4s] Shot 1: …
[3.4s-10.0s] Shot 2: …
Camera: …
Audio: …
No text, subtitles, logos or watermarks …
```

### 4. 產生 ComfyUI 工作流程

```
python build_workflow.py projects/我的歌
```

以 `templates/MiniMaxH3_本機_範本.json` 為範本，依段數重建提示詞節點。
CreateList 一個最多 10 個輸入，所以每 9 段一組再合併（最多 90 段）。改範本（解析度、步數…）之後產生的都會跟著改。

### 5. 在 ComfyUI 生成

1. 把 `歌名MV_MiniMaxH3_本機.json` 拖進 ComfyUI
2. `clip_no` 設 1、Run 旁的批次數量設 1，先測第 1 段
3. 滿意後 `clip_no` 設 2、批次數量設剩下的段數，按一次 Run
4. 輸出到 `ComfyUI/output/<folder>/shot_01_00001_.mp4` …

- **重抽某段**：`clip_no` 設那段、`reroll` +1、批次數量 1
- **解析度**：Resolution Selector 預設 megapixels 0.3（736×416）；要更清楚改 0.6（1056×608）或 0.98（1344×768），會慢很多
- **Turbo**：預設 8 步；關掉 = 20 步，畫質好一些，約慢 2.5 倍

### 6. 剪成 MV

```
python assemble_mv.py --project projects/我的歌 --allow-missing --subs --labels   # 修改用：左上角標段號和版本
python assemble_mv.py --project projects/我的歌 --subs                            # 成品
```

- 依 `shots.json` 把每段剪到精確影格數、接起來、換上原曲
- 同一段重抽過，自動用編號最大（最新）的版本
- 1920×1080、24fps、H.264 + AAC 320k，開頭淡入、結尾淡出
- ComfyUI output 位置預設 `~/Documents/ComfyUI/output`，可用環境變數 `COMFYUI_OUTPUT` 或 `--comfy-output` 指定
- 其他選項：`--fontsize 34`、`--marginv 10`（字幕大小和高度）、`--size native`

---

## 檔案

| 檔案 | 用途 |
|---|---|
| `prepare_song.py` | Suno 資料 → 切點、字幕、分鏡草稿 |
| `write_prompts.py` | 分鏡設定 → 英文提示詞 |
| `build_workflow.py` | shots.json → ComfyUI 工作流程 |
| `assemble_mv.py` | 生成的片段 + 原曲 → 完整 MV |
| `templates/MiniMaxH3_本機_範本.json` | ComfyUI 工作流程範本（全部內建節點） |
| `examples/汪洋/` | 完整範例（不含音樂和影片） |

## 授權提醒

MiniMax H3 使用 MiniMax H3 Community License，美國、歐盟、英國、韓國不在授權範圍內；
商用前請先看 <https://huggingface.co/MiniMaxAI/MiniMax-H3> 的條款。
