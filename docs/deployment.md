# 配置与部署

## 最小运行环境

核心与控制台使用 Python 标准库，建议 Linux + Python 3.11 及以上。先运行 `python3 scripts/init_config.py`，再按 README 填入凭据和模型地址。

所有相对文件路径由初始化脚本转为当前仓库绝对路径；移动仓库后，请更新配置中的路径。手动启动 Bot 要设置 `AKARI_BASE`，否则入口默认使用 `~/.akari`。不要让新部署误读旧机器目录。

对于图像/视频预处理，推荐单独环境：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-optional.txt
```

FFmpeg/ffprobe 用于媒体编码和视频分段；请通过操作系统包管理器安装。音乐、ComfyUI 和 TTS 使用各自的独立环境。

## QQ 接入

在 QQ 机器人应用后台获取 App ID 和 App Secret，设置运行场景和事件订阅权限，将其填入 `config.json`。示例 `intents` 沿用项目接入配置，不保证适用于每个账号与场景。

默认 `group_reply_mode=at_only`，群聊需 @ Bot 后回复。可选 `all`（参与所有群消息）或 `at_or_keyword`（@ 或匹配 `keyword_regex`）。只有实际 QQ 网关 READY 才算已连上 QQ；控制台能分别显示进程与网关状态。

## 模型与 Pi

聊天配置：`llm_base` 为根地址，不带 `/v1`；Bot 追加 `/v1/chat/completions`。模型名必须与服务加载的模型匹配。`llm_key_file` 指向一个私有文件，无鉴权的本机服务也需要该文件存在，内容可为空。

任务执行依赖 Pi CLI。本项目验证版本为 `@earendil-works/pi-coding-agent@0.85.1`、Node 22：

```bash
npm install -g @earendil-works/pi-coding-agent@0.85.1
pi --version
pi --help
```

使用 Pi 自身的 provider / model 配置并先独立验证能执行工具调用。Bot 的 `agent_provider` 和 `agent_model` 应与该配置匹配。Pi 配置目录可由 `PI_CODING_AGENT_DIR` 指定；连接地址可在 Bot 的 `agent_base` 中覆盖，该地址一般带 `/v1`，与聊天的 `llm_base` 不同。

填写 `agent_allowed_openids` 后才能使用工作和唱歌指令。群聊与私聊实际用户标识可能不同，需要配置所用场景中的授权标识。空列表不会开放任意执行。授权用户可以通过 Pi 使用运行账户的工具与文件权限，请只加入可信的身份。

桥接器支持 `AKARI_PI_BIN`、`AKARI_AGENT_PROVIDER`、`AKARI_AGENT_MODEL`、`AKARI_AGENT_BASE`、`AKARI_AGENT_KEY_FILE`，以及标准的 `SSL_CERT_FILE` / `NODE_EXTRA_CA_CERTS`。原有代码保留了本机 CA 路径兼容回退；路径不存在时不使用该回退，新部署请显式配置自己的 CA。

现有聊天 HTTP helper 对模型请求使用 `verify=False`。这是当前实现的兼容行为，不能把它当作已经验证远程服务器证书；优先对接受信任的本机模型服务。

## ComfyUI

需另行部署 ComfyUI，并准备与对应适配器匹配的 API 工作流 JSON：

- `comfy_url`：实际服务地址；示例为 `http://127.0.0.1:8188`。
- `comfy_workflow` / `comfy_workflow_plain`：图片工作流。
- `video_workflow`：图片到视频工作流。
- `comfy_input_dir` / `comfy_output_dir`：与 ComfyUI 所在机器共享的输入/输出目录。

适配器按既有节点类型与标题填写提示词、尺寸、种子等字段。不能保证任意工作流 JSON 直接兼容；接入自定义节点时，检查 `akari_image.py`、`akari_video.py` 的节点映射。仓库不包含原部署的商业节点、模型或私有工作流，初始化脚本不会创建可直接推理的工作流。

完成单独推理验证后，再开启图片或视频功能。生成分辨率、时长、超时与冷却可在控制台调整。

## TTS

Bot 客户端调用 `POST <voice_url>/tts`，请求带 `text`，需要返回可用音频字节。`voice/indextts_server.py` 是当前 IndexTTS 适配源码，使用 `indextts.infer_v2_5.IndexTTS2`；需另行安装与此接口匹配的 IndexTTS 环境、权重和依赖。

默认目录：`voice/index-tts/`、`voice/models/IndexTTS-2.5/`、`voice/ref/akari_ref.wav`。在该独立环境运行适配器，使用 `CUDA_VISIBLE_DEVICES` 选择 GPU、`VOICE_PORT` 选择端口（默认 8903）。配置 `AKARI_BASE` 可指定 Bot 配置根目录。

`GET /health` 是模型完成加载后提供的端点；启动会加载模型。控制台的诊断只检查 TCP 端口连通性，不会调用 TTS 推理。开启 `voice_enabled` 不会自动启动语音服务。

## systemd

先完成初始化和配置，再在仓库根目录生成当前路径对应的服务文件：

```bash
python3 scripts/render_services.py
# 可选：--python "$PWD/.venv/bin/python" --port 18763
cat .local/systemd/akari-bot.service
cat .local/systemd/akari-admin.service
systemd-analyze verify .local/systemd/akari-bot.service .local/systemd/akari-admin.service
```

服务以执行生成脚本的用户运行。若用户名不同，使用 `--user` 指定，并确保该账户有读取源码、写入配置和状态目录的权限。控制台不需要 root 身份运行。

确认文件后安装：

```bash
sudo install -m 0644 .local/systemd/akari-bot.service /etc/systemd/system/akari-bot.service
sudo install -m 0644 .local/systemd/akari-admin.service /etc/systemd/system/akari-admin.service
sudo systemctl daemon-reload
sudo systemctl enable --now akari-bot.service akari-admin.service
sudo systemctl status akari-bot.service akari-admin.service
```

### 控制台服务操作权限

查看/保存配置不依赖 sudo。控制台启动、重启和停止 Bot 则固定调用 `sudo -n systemctl`，需要管理员为实际运行账户配置这三个命令的权限，或沿用已存在的授权。

可通过 `sudo visudo -f /etc/sudoers.d/akari-bot-control` 为自己的运行用户配置限定规则（下面的 `YOUR_USER` 和 `systemctl` 绝对路径必须替换）：

```sudoers
YOUR_USER ALL=(root) NOPASSWD: /usr/bin/systemctl start akari-bot.service, /usr/bin/systemctl restart akari-bot.service, /usr/bin/systemctl stop akari-bot.service
```

此步骤由管理员执行，配置/服务生成脚本不会修改 sudoers。未授权时，页面会明确显示服务操作失败。

## 日常维护

```bash
journalctl -u akari-bot.service -n 100 --no-pager
journalctl -u akari-admin.service -n 100 --no-pager
sudo systemctl restart akari-bot.service
```

生成的服务把 Bot 输出写入 `state/console.log`，管理服务输出写入 `state/admin-service.log`；journal 主要用于查看服务启动/退出。Bot 的业务日志在 `state/bot.log`，可在控制台查看。

更新前先等长任务完成，备份配置和 `state/`，再更新源码、运行测试并重启。配置示例不会自动迁移或覆盖已有配置。不要用 `git clean -fdx` 清理部署目录：被忽略的配置、状态和模型仍是实际运行数据。
