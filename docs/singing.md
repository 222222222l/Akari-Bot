# 阿卡林唱歌与翻唱

## 两条管线

- **原曲翻唱（默认）**：FFmpeg 标准化 → Demucs 人声/伴奏分离 → Seed-VC F0 音色转换 → 对齐检查 → 与原伴奏混音 → MP3/WAV。
- **YuE2 重编曲**：用户歌词与 ABC 谱（缺谱时使用 SheetSage2）→ YuE2 生成 → 分离与 Seed-VC 音色转换 → MP3/WAV。

YuE2 负责音乐生成/重编曲。仅换成参考音色并保留原曲伴奏时，使用 Seed-VC 管线；这不是通过 YuE2 一步完成的严格换声。

## 安装

准备 Linux、可用 NVIDIA GPU、FFmpeg/ffprobe、Git 和 uv。不同模型依赖分开安装，不与 Bot/TTS 环境混装。

```bash
python3 scripts/init_config.py
bash music/setup.sh svc        # 默认翻唱：Seed-VC + Demucs
bash music/setup.sh yue        # 可选：YuE2
bash music/setup.sh sheetsage  # 可选：自动转谱
# 或 bash music/setup.sh all
```

安装脚本会下载第三方代码与模型，可能产生较大的网络与磁盘开销。`music/vendor/`、`models/`、`hf-cache/` 和隔离环境均不纳入 Git。

当前锁定版本来自本项目已验证环境：

| 组件 | 版本 |
| --- | --- |
| YuE 源码 | `0edaf2f4053ef4731334b8329834b107977f9637` |
| Seed-VC 源码 | `51383efd921027683c89e5348211d93ff12ac2a8` |
| SheetSage2 模型 | `cd2f39b3b807b81f6e32bbb8ea6011da40f77649` |

YuE2 主模型/VAE 的仓库与 revision 在 `music/config.example.json`。原曲转换 checkpoint 在安装脚本中固定版本。安装后记录 `music/installed-*.txt` 供本机排查，不提交机器完整环境快照。

## 准备配置与声音

把自行准备、具有使用权限的参考声音放到 `voice/ref/akari_ref.wav`，或修改 `music/config.json` 的 `reference`。所有音乐内部相对路径都相对于该配置文件目录。

主要参数：

| 参数 | 含义 |
| --- | --- |
| `cuda_visible_devices` | 使用的物理 GPU 编号，示例为 `0`，部署时选择空闲设备 |
| `reference` | 参考声音文件路径 |
| `max_seconds` | 最大整曲时长，默认 600 秒 |
| `diffusion_steps` | Seed-VC 步数，默认 40 |
| `memory_budget_gib` | YuE2 推理显存预算，默认 24，不是最低显存保证 |
| `style` | YuE2 重编曲默认风格描述 |
| `svc_python` / `yue_python` / `sheetsage_python` | 三个独立 Python 环境 |

顶层 `config.json` 需启用 `agent_enabled` 和 `music_enabled`，配置 `music_config` 与 `agent_allowed_openids`；`music_timeout` 默认 3600 秒。歌曲推理通过任务队列执行，不依赖 Pi 来拼接模型命令。

## QQ 使用

```text
/唱歌 帮助
/唱歌 状态
/唱歌 /片段 30
/翻唱
/唱歌 /重编曲
第一行歌词
第二行歌词
```

- 在同一条 QQ 消息附歌曲文件。歌词可从命令下一行提供，或附 UTF-8 `.txt`。
- 重编曲可以附 `.abc` 旋律谱；不附则需要安装自动转谱环境。
- `/片段 5..120` 只用于原曲翻唱试听。
- `/风格 "Japanese anime opening, J-pop"` 指定重编曲风格。
- 本地曲库目录为 `music/songs/`，可按文件名/不带扩展名的歌名点歌；需要唯一匹配。
- 同时给歌名和音频附件、多个音频、路径无效、缺歌词等情况会明确失败。
- 不内置在线歌曲搜索或下载；仓库不包含测试歌曲与生成音频。

用 `/进度`、`/取消`、`/结果`、`/文件`、`/补发` 追踪或取回产物。音乐任务不能通过 `/继续` 变为通用 Pi 任务，重新生成请重新发送唱歌指令。

## 本地验证（不发送 QQ）

将自己的 `测试歌曲.mp3` 放到 `music/songs/`：

```bash
python3 music/local_test.py --request '测试歌曲 /片段 30' --output music/validation/preview
python3 music/local_test.py --request '测试歌曲' --output music/validation/full
python3 music/local_test.py --request '测试歌曲 /重编曲' \
  --lyrics-file /path/to/lyrics.txt --score /path/to/melody.abc \
  --output music/validation/yue
```

模型阶段参数、耗时和错误日志在任务目录的 `music-work/`，最终结果在 `artifacts/`。推理使用文件锁串行，取消与超时会终止进程组。混音前验证人声/伴奏时长差不超过 250 ms，最终媒体经 ffprobe 验证后登记交付。YuE2 的截断输出保留作诊断，不当作完整歌曲发送。

默认回归测试使用模型替身验证流程，真实 FFmpeg 验证混音，**不能证明特定硬件上的推理速度或音色相似度**。核心适配曾在 H100 环境验证过原曲和 YuE2 管线；相应歌曲、运行日志及含用户数据的记录不随源码分发。

## 上游

- [YuE / YuE2](https://github.com/multimodal-art-projection/YuE)
- [Seed-VC](https://github.com/Plachtaa/seed-vc)
- [SheetSage2](https://huggingface.co/m-a-p/SheetSage2)

请分别遵守上游代码、权重、歌曲与参考声音的许可条件；替换依赖或权重版本后重新验证。
