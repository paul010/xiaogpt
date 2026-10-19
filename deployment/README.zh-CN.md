# 家庭双音箱分支

基于 [yihong0618/xiaogpt](https://github.com/yihong0618/xiaogpt)，为卧室、客厅分别运行一个实例。保留原作者的核心逻辑、许可证和上游来源。两台音箱分别保存对话状态，使用各自 DID，共用一个 OpenRouter 密钥。

## 安装和启动

使用 Python 3.12–3.14 的独立虚拟环境安装本仓库，不覆盖已有 Anaconda 环境。确认 `miservice` 和 `miservice-fork` 不混装；以当前 pyproject.toml 为准。

1. 将 `speaker.yaml.example` 复制到 `~/.config/xiaogpt/bedroom.yaml` 与 `livingroom.yaml`，填写各自 DID 和原有小米登录资料。
2. 将 `common.env.example` 复制到同目录的 `common.env`，在本地填写 OpenRouter 密钥。配置目录权限设为 700，三个文件为 600。
3. 首次小米登录如需验证码，在交互终端按原项目说明完成；之后复用 `~/.mi.token`。不要把 token 上传到 GitHub。
4. 在仓库目录执行：

```bash
.venv/bin/python deployment/install_user_services.py --enable
loginctl enable-linger "$USER"
```

`--enable` 会开启开机启动并重启两个实例。省略该选项只生成并验证服务文件。安装器将先前的服务模板保存在 `xiaogpt@.service.previous`。每个实例异常退出后等待15秒重启，5分钟内最多启动5次，避免凭据失效时持续重试登录。

首次安装会从现有 `~/.mi.token` 初始化两份权限600的设备实例 token，已有实例 token 不覆盖。两台服务分别使用 `bedroom.mi.token` 和 `livingroom.mi.token`，避免同时续期写同一个文件。若两台属于不同小米账号，应分别在交互终端登录生成各自 token，不能共用原始 token。

```bash
systemctl --user status xiaogpt@bedroom xiaogpt@livingroom
journalctl --user -u xiaogpt@bedroom -u xiaogpt@livingroom --since '10 minutes ago'
systemctl --user stop xiaogpt@bedroom xiaogpt@livingroom
```

日志可能包含家庭提问内容，只在本地查看。开机启动还取决于虚拟机和宿主机启动，不能将服务已启用当作已通过整机重启测试。

## OpenRouter 和语音验证

OpenRouter 使用 `api_base: https://openrouter.ai/api/v1`，模型用完整 `provider/model` 标识。示例为 Qwen，实际可用性以账号测试为准；这不代表使用了 ChatGPT 模型。若某模型返回403，保留错误类型并核对服务商权限；不要把有效密钥或服务进程存活当作聊天成功，也不要循环创建密钥或修改出口来规避限制。

分别向两台音箱说“小爱同学，请告诉我一加一等于几”。验收要同时确认获取到了该设备的新提问、OpenRouter返回有效文本、音箱实际播报。小米播报接口返回 code=0 只表示接受了请求，仍需实际听到。两台设备音量不同可能造成听感差异，不自动大幅改变音量。

## 配置和回滚

所有账号、密码、DID、cookie、token、API密钥和家庭运行日志都放在仓库外。仓库中只有占位示例；提交前检查 `git diff --cached`。不要 `git add -f` 添加被忽略的运行配置。

升级前备份服务模板、配置和小米 token 到权限为700的本地备份目录。新版本先在独立环境验证小米登录、模型流式输出和两台播报，再切换模板并重启服务；失败则恢复之前模板，执行 `systemctl --user daemon-reload` 后重启实例。保留旧源码和虚拟环境便于回滚。
