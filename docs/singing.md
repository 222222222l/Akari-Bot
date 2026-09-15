# 阿卡林唱歌：调研与接入

调研日期：2026-09-15。目标：使用当前 bot 的阿卡林参考声音，对动漫歌曲生成可播放的翻唱；通过现有后台任务提供进度、取消、结果文件和补发。

## 方案选择

| 实现 | 已核实能力 | 本项目用途 |
| --- | --- | --- |
| [官方 YuE2](https://github.com/multimodal-art-projection/YuE) | 目前 main 为 YuE2，旧版保留在 YuE-v1。歌词、风格及 ABC 旋律谱驱动完整歌曲生成。官方起点为 Python 3.12、支持 BF16 的 NVIDIA GPU、24 GB 显存 | `/重编曲` 后端，保留谱面和模型生成记录 |
| [SheetSage2](https://huggingface.co/m-a-p/SheetSage2) | 音频转旋律谱；自动载入 MERT2；官方要求独立 Python 3.10/3.11、Torch 2.8、FFmpeg 6.1 环境 | 无外部 .abc 时自动提取旋律 |
| [Seed-VC](https://github.com/Plachtaa/seed-vc) | v1 有 44.1 kHz、200M、F0 条件歌声转换模型，支持短参考音频和无需训练的音色迁移 | 阿卡林音色转换，40 步，保留原音高、速度，不自动移调 |
| [RVC](https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI) | 面向特定声音训练、音高提取及检索式音色转换 | 后续有足够阿卡林干净语料时可比较；当前没有角色专用权重，先采用 Seed-VC |
| [Demucs](https://github.com/facebookresearch/demucs) | htdemucs 及 two-stems 人声/伴奏输出；官方仓库已归档 | 本地离线分离，固定 4.0.1；后续可针对残留原唱比较更新的分离器 |
| [ComfyUI-FL-YuE2](https://github.com/filliptm/ComfyUI-FL-YuE2) | 谱面钢琴卷帘、生成、AR LoRA 训练入口；音频转谱需另装，输出仍为混合音轨 | 适合人工编辑与训练探索；QQ 无须引入 ComfyUI 运行时 |
| [YuE2_WebUI](https://github.com/Ladypoly/YuE2_WebUI) | 提供图形界面组织生成/谱面翻唱流程 | 作为工作流参考，bot 直接接 Python API |
| [YuE-exllamav2](https://github.com/sgsdxzy/YuE-exllamav2)、[audio.cpp dev](https://github.com/0xShug0/audio.cpp/tree/dev) | 分别是旧 YuE 的推理优化分支，以及持续变化的 C++ 音频推理开发分支 | 未作为当前已验证后端；不可把旧 YuE 的参数照搬到 YuE2 |

官方 YuE2 的“cover”是**原音频 → 旋律谱 → 重新演绎**；当前官方生成 API 没有接收任意角色参考音频的声音克隆参数。因此要得到阿卡林声音，在歌曲生成之后继续进行人声分离、Seed-VC 歌声转换及混音。仅在风格提示中填写“阿卡林”不能保证声音身份。

本项目的默认翻唱直接转换原歌曲的人声，以保留动漫歌曲的原旋律、日语发音节奏和伴奏；指定 `/重编曲` 才走 YuE2。自动转谱和生成可能改变细节，人工检查旋律与歌词对齐仍有价值。参见[官方翻唱指南](https://github.com/multimodal-art-projection/YuE/blob/main/docs/covers.md)。

YuE2 当前代码为 Apache-2.0，权重单独采用 CC BY-NC 4.0；旧 YuE 的许可不能替代新版模型许可。各依赖沿用自身许可，详见[官方模型许可](https://github.com/multimodal-art-projection/YuE/blob/main/MODEL_LICENSE)。

## 使用

授权用户可直接描述需求，由原生 Pi 工具 `akari_music` 搜索、填写参数并执行：

```text
/音乐 写一首关于雨后放学的日语动漫歌，轻快温暖
/作曲 一首可爱的原创歌曲
/唱歌 帮我翻唱《残酷天使的行动纲领》
/翻唱 残酷天使的行动纲领 /片段 30
/任务 用阿卡林的声音生成一首原创动漫歌，并整理歌词
```

开启 `music_auto_route` 后，“阿卡林帮我翻唱……”等明确音乐请求也可自动进入音乐任务。普通讨论不主动启动生成；`/聊` 可明确走聊天。原创无需提供歌曲或歌词，Pi 自动写歌词、风格和种子；也支持精确输入：

```text
/唱歌 /原创 /风格 "Japanese, anime pop, warm female vocal"
[Verse]
这里填写原创歌词
[Chorus]
这里填写副歌歌词
```

`/翻唱` 可附带音频、填写本地曲库文件名或公开歌曲链接。曲库优先匹配 `music/songs/` 直接子文件；缺少时联网搜索下载。音乐 Agent 会先查看搜索结果标题与发布者，选择合适音源；直接 `/翻唱 歌名` 在候选失败时自动换源，YouTube 不可用时尝试 B 站，每平台最多 3 个候选，总换源时间预算约 5 分钟；来源与失败历史保存在 `source-selection.json`。明确指定的 URL 不会被静默替换。找错版本时可提供明确链接。原生工具 `search` 支持 `platform=youtube` 或 `platform=bilibili`，下载遇到验证时 Pi 可切换平台；下载支持配置内的 YouTube、Bilibili、SoundCloud、Internet Archive、网易云公开页面。站点可用性取决于网络和上游，不使用账号 cookies，不绕过登录、付费或 DRM。任务保存来源、标题、发布者及下载日志。

`/唱歌 /重编曲 /风格 "Japanese, anime opening"` 加歌曲与歌词走旋律转谱路线，可附带 `.abc` 跳过 SheetSage2。生成后仍转换成默认参考音色。原创不经过转谱；时长需求属于模型风格提示，并非精确时长保证。`/片段 5..120` 只对原伴奏翻唱进行明确裁剪。

`/唱歌 状态` 查看安装及默认参考；`/进度`、`/取消`、`/文件`、`/补发` 沿用任务机制。音乐 Agent 可通过 `/继续` 调整结果；结构化直达管线需重新提交命令。产物包括 MP3、WAV，原创同时保存歌词和模型谱面。

### 原生 Pi 接入

`akari_agent.py` 自动加载 `akari_music_tools.mjs`，注册 `akari_music`：`status`、`search`、`generate`、`cover`、`rearrange`。工具调用 Python 后端，报告阶段进度并登记经过验证的产物；参数和调用收据保存在任务目录。成功调用可按同一调用 ID 取回结果，失败不自动重放。

音乐专用入口仅开放音乐工具和 `read`，避免模型为下载失败修改系统配置；普通 `/任务` 保留原有 Pi 工具并额外获得音乐能力。二者均受现有用户授权、任务队列、取消和超时约束。生成过程不通过普通聊天模型假装完成。

## 安装与资源

```bash
bash music/setup.sh all
```

可分别安装 `svc`、`yue`、`sheetsage`、`download`。下载器使用独立 `.venv-download`，并需要 Node.js 执行站点脚本。脚本固定两个源码 commit 和 SheetSage2 版本，拒绝覆盖不同版本或修改过的源码。SVC 使用精简推理依赖，避开 upstream requirements 中同时存在 nightly 与 Torch 2.4 的冲突。YuE2 使用官方包依赖（本次为 yue2-infer 0.1.6 / Torch 2.10）。三个环境都不修改已有 TTS 环境。

`music/config.json` 配置参考音频、解释器、模型路径和 GPU。当前配置选择宿主 GPU 7；部署到不同机器时应按实际资源修改 `cuda_visible_devices`。所有音乐任务共享文件锁，模型阶段顺序执行，各阶段子进程退出后释放显存。此锁只约束音乐任务，不调度宿主机其他 GPU 服务。

默认参考为 `music/songs/三上枝織 - ハイっ!アッカリーン.mp3`（需自行提供，不随源码发布）。开启 `reference_extract_vocals` 后自动分离前 180 秒人声，选择有效演唱的 `reference_seconds` 秒并按源文件内容缓存到 `music/reference-cache/`。本机选中第 58–76 秒，共 18 秒；原文件不变。已有 3–25 秒干净单人参考可关闭自动提取直接使用。实际音色相似度仍需试听。模型首次运行下载权重；音乐缓存位于项目内，Seed-VC 自己还使用 vendor/seed-vc/checkpoints。

顶层 bot 配置：`music_enabled`、`music_auto_route`、`music_timeout`（默认一小时）、`music_config`。仍需 `agent_enabled=true` 且用户在工作指令授权名单内；`dry_run=true` 不创建可执行任务。初始最大歌曲时长 600 秒；`/片段 5..120` 是显式试听裁剪，只用于原伴奏路线。

## 行为与验收

- 音乐命令在普通聊天前识别，结构化参数直达管线，自然语言需求使用原生 Pi 音乐工具。
- 附件下载错误、缺少歌词、多个音频、无效路径、缺少环境明确失败，不静默选择文件或退回聊天执行器。
- 先验证音频与参考声音，音乐推理串行执行；任务取消/超时可终止整个进程组。
- 原伴奏模式保留 F0、长度和伴奏；混音前检查转换人声与伴奏时差不超过 250 ms，超出则失败。
- 最终 MP3/WAV 通过 ffprobe 后才登记交付。模型失败日志和生成参数保存在 `music-work/`，不作为最终音乐附件自动发送。
- YuE2 截断标记为失败，保留其原生 artifacts；不会把碰到 token 上限的半首歌标成完整完成。
- 音乐专用 Agent 不具备 shell/写文件工具；普通工作任务沿用已授权的完整执行能力。

验证命令：

```bash
python3 -m unittest discover -s tests -v
node tests/test_context.mjs
```

`tests/test_music.py` 中模型阶段替身只验证流程，真实 FFmpeg 验证混音/编码；不能证明阿卡林音色相似度。真实推理记录另见 `music/validation/`。测试不会主动向 QQ 发消息。

## 本机实测（2026-09-15）

- 82 项 Python 回归、6 项 Node 上下文测试通过。Python 测试需要允许绑定本机回环地址。
- 三个独立环境分别通过 `uv pip check`，准确依赖清单保存在 `music/installed-*.txt`。本机 SheetSage2 使用 PyPI 的 Torch 2.8 / CUDA 12.8 wheel；官方安装示例使用 CUDA 12.6 wheel。本机转谱已经实际成功。
- 已从官方 MV 下载约 243.888 秒源文件。使用 GPU 7 / H100 生成 30 秒样本及整曲，均经过实际 Demucs + Seed-VC + FFmpeg 流程；测试不经过 QQ 发送器。
- 30 秒样本首次运行：分离 31.46 秒，转换 31.82 秒，包含首次加载/下载开销。整曲后续运行：分离 9.88 秒，转换 29.57 秒。这是两种长度、不同缓存状态的单次记录，不作为严格的速度对比。
- 整曲最终 WAV：44.1 kHz 双声道，243.844 秒，无 NaN/Inf，无削波；转换人声与原伴奏时长差在允许范围内。MP3 容器有少量编码填充，因此显示时长略有差别。
- 整曲共发声帧的 RMVPE 音高绝对偏差中位数 0.093 半音，90 分位 0.371 半音。CAMPPlus 参考音色余弦相似度原唱 0.312、转换后 0.687。CAMPPlus 本身参与转换模型，以上仅为诊断代理指标，不能替代独立听感和声音身份评估。
- 官方 Seed-VC 加载旧权重时会提示忽略多余键。按其实际去掉 `module.` 前缀的规则核对后，243 个 CFM、24 个长度调整模块的可训练参数均匹配；记录在 `music/seed-checkpoint-check.log`。
- 真实自动转谱成功，无返回警告，见 `music/validation/score-preview/`。30 秒样本/整曲任务记录分别位于 `music/validation/preview/`、`music/validation/full/`；`summary.json` 给出实际文件路径，任务目录包含 `quality.json`、阶段日志及参数 manifest。
- YuE2 已使用测试曲30秒旋律谱和 Whisper 转写歌词完成真实重编曲，再通过相同 Seed-VC 流程转换为阿卡林声音。最终音频约38.1秒，`abc`、`semantic` 截断标志均为 false，记录在 `music/validation/yue-preview/`。重编曲允许改变速度、编配与时长，不承诺逐采样对齐原曲。
- 易于取用的整曲、30秒试听及 YuE2 重编曲版 MP3/WAV 已整理到 `music/outputs/`。当前 bot 已在任务队列为空时重启加载新命令；本次验证没有进行 QQ 实际发消息测试。

本地复现（不发送 QQ）：

```bash
python3 music/local_test.py --output music/validation/new-preview
python3 music/local_test.py --request '残酷天使的行动纲领' --output music/validation/new-full
python3 music/local_test.py --request '残酷天使的行动纲领 /重编曲' \
  --lyrics-file music/validation/preview-lyrics.txt \
  --score music/validation/score-preview/score.abc \
  --output music/validation/new-yue
```

首次部署遇到整包下载反复重试，改用公开 HTTP Range 分块下载后核对 PyPI/模型库 SHA256。
已验证的 wheel 保存在 `music/wheels/`，安装脚本优先复用；诊断下载脚本保存在 `music/validation/`，
它们是本次环境修复记录，不是 QQ 唱歌运行时依赖。

## 原生 Agent 新增实测（2026-09-15）

自然语言原创任务实际调用 YuE2 `cot=full`，无需源音频，随后完成分离、默认参考音色转换和混音。最终《虹の放課後》约 40.9 秒。自动翻唱任务由 Pi 搜索并选择 KING RECORDS 官方 MV，真实下载后完成 30 秒翻唱。两者通过 ffprobe 和非静音检查；验证不启动 QQ 发送器，声音相似度需试听判断。

```bash
python3 music/prepare_reference.py
python3 music/agent_test.py --request '写一首关于雨后放学的日语动漫歌' --output music/validation/my-original
python3 music/agent_test.py --request '搜索官方音源，翻唱残酷天使的行动纲领，试听30秒' --output music/validation/my-cover
```

配置 `auto_download=false` 可关闭工具联网找歌；`download_timeout`、`download_max_seconds`、`download_max_bytes` 限制下载耗时、源时长和文件大小。`reference`、人声提取、参考时长、自动下载和自然语言路由均可在控制台调整。错误保留在任务日志中，不会自动降级成朗读。

### 下载失败恢复

`/翻唱 为我翻唱花泽香菜的恋爱循环` 等自然语言请求由 Pi 理解，不再把整句当歌名。YouTube 要求登录验证时显示简短中文原因，原始信息保留在候选目录 `metadata.log` / `download.log`。B 站候选元数据独立补全，单个失败不丢弃其他搜索结果。平台仍可能限流或要求验证，所有来源都失败时会明确告知，不保证任意歌曲始终可下载。现场版与录音室版请在需求中明确区分。
