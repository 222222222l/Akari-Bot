# 开发与测试

## 回归测试

在仓库根目录：

```bash
python3 -m unittest discover -s tests -v
node tests/test_context.mjs
```

默认 Python 测试包括指令授权、任务/会话隔离、并发队列、取消/超时、发件箱、分片上传、视频分段、音乐管线、控制台登录/CSRF/配置校验/脱敏和队列重启保护。

测试不会发送 QQ 消息或调用真实音乐模型。Pi 集成测试使用本机临时 HTTP provider，需要已安装 Pi；未安装时相应测试跳过。媒体测试需要 FFmpeg/ffprobe；HTTP 集成需要允许本机端口监听。

Node 测试验证长上下文预算、图片数量、工具调用与返回配对、可恢复的归档及不可缩减输入的错误行为。

## 控制台浏览器测试

```bash
npm ci
npx playwright-core install chromium
python3 tests/admin_browser_fixture.py
```

在另一终端执行：

```bash
npm run test:browser
```

临时服务使用测试数据，地址记录于 `/tmp/akari-admin-browser.json`，不会控制真实 Bot。执行结束后用 Ctrl+C 关闭临时服务。

可以用 `CHROMIUM_PATH` 指定已有浏览器，或 `PLAYWRIGHT_MODULE` 指向独立安装的 `playwright-core/index.mjs`。截图写到 `/tmp/akari-admin-*.png`，不提交真实用户页面截图。

`tests/admin_live_smoke.mjs` 是对本机已部署 `18763` 控制台的**显式**只读验收，需本地访问口令和已安装服务，会调用端口/GPU 诊断。不要在普通 CI 中运行。

## 真实模型验证

`python3 tests/smoke_local.py` 显式调用当前配置的 Pi/模型，在临时目录完成一项文件生成，不运行 QQ 发送器。音乐真实推理见 [唱歌文档](singing.md)。这两类测试会实际使用模型资源，默认回归不执行。

## 开发边界

- 网关线程只接收/入队，避免阻塞会话控制。
- 任务指令与人设聊天分开，完整结果由任务持久化与交付模块负责。
- 新控制台字段在 `akari_admin_schema.py` 中定义，不向浏览器暴露原始配置。
- 管理 API 写操作保留 CSRF、Host/Origin 校验和配置 revision 检查。
- 外部操作不能因“返回失败”就自动重放，应先确认是否已产生影响。
- 新生成管线应通过任务执行器和 `artifacts/` 交付，保留诊断日志，避免把中间文件当作完成结果。

## 发布内容

纳入版本控制：源码、静态 UI、人工编写的人设、脱敏示例、测试、部署脚本、文档和测试数据截图。

不纳入：真实配置、密钥、任务数据库、聊天/错误运行日志、参考声音、歌曲、生成文件、模型、第三方 vendor、虚拟环境、下载缓存和本机完整依赖快照。发布前同时检查 `git diff --cached` 和实际待提交文件列表；忽略规则不能清除已经提交的敏感文件。

## 本次发布验证

2026-09-15，在未复制生产配置的独立检出中完成：103 项 Python 测试、6 项 Node 测试和 11 个 Chromium 交互流程通过；生成的 systemd 单元通过 `systemd-analyze verify`。这不包含 QQ 实际发送与新的 GPU 模型推理验证。

角色真实模型验证（显式联网到所配置模型，不发送 QQ 消息）：`python3 tests/role_local.py`。使用隔离任务库验证闲聊/执行意图、上下文指代，并只执行一个生成 `42` 文本文件的任务，检查角色结果回复。

白名单浏览器验收：启动 `tests/admin_browser_fixture.py` 后运行 `node tests/admin_accounts.mjs`；使用隔离账号验证添加、移除、手填去重、保存重载及移动布局。
