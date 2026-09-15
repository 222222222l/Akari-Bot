# systemd 部署

在仓库根目录执行 `python3 scripts/render_services.py`，根据当前用户、Python 和仓库路径生成 `.local/systemd/akari-bot.service` 与 `akari-admin.service`。

使用 `--port` 可指定控制台端口，使用 `--python` 可选择安装了图像处理依赖的 Python。生成后按 [部署文档](../docs/deployment.md) 检查并安装。

不直接提交本机服务文件，避免把维护者的用户、绝对路径和运行环境作为所有部署的默认值。
