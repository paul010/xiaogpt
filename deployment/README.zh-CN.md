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

## 动态模型选择与每月费用

在两份私有配置加入以下内容，可开启本分支的路由；必须使用OpenRouter HTTPS地址。关闭`enabled`即可回到上游固定模型模式。

```yaml
routing:
  enabled: true
  monthly_budget_usd: 3
```

普通问题使用`qwen/qwen3-235b-a22b-2507`；包含分析、对比、排查等意图时选择`openai/gpt-6.1-sol`。说“请用GPT重新回答”可携带当前音箱上一个问题重答；“请用千问……”强制省钱。音乐、闹钟、音量和直接开关灯等原生命令不转给模型，也不静音小爱。此为可核验的本地规则，不是通用语义分类器。

两实例共用同一SQLite账本，按Asia/Shanghai自然月统计，默认路径`~/.local/state/xiaogpt/budget.sqlite3`。请求发送前在事务中预留保守最大费用，结束后按流式尾部usage.cost结算；连接中断、缺失费用和进程崩溃的预留不自动释放，并跨月保留，避免重启绕过额度。数据库故障时停止调用。达到预算或余额不足时停止付费AI，小爱原生功能继续。累计达到80%后仅使用Qwen。Provider价格上限与输出预算同时限制单次调用；账本不包含原始问题或回答。

Qwen上限512输出tokens；Sol低推理档、1536总输出tokens（包括内部推理）。上下文保留最近三轮，按UTF-8字节和消息框架开销保守限制输入。Sol首个可见token前超时或服务错误，可回退Qwen一次；有回答文字交给播报管线后不自动重答。Sol返回服务商条款403会在共享账本中标记不可用，后续不再重复试探，也不绕过服务商限制。提示音使用“AI”，不会把Qwen冒充成GPT。

建议同时把音箱专用OpenRouter Key设为$3 Monthly，不与其他程序共用。这是服务器的额外拦截层；OpenRouter自然月按UTC重置，与本地北京时间差8小时，重叠处可能更早暂停。费用指API credits，不包括充值手续费/税费。

首次切换前停止两实例，在私有终端读取此Key已经发生的当月usage_monthly并只初始化一次：

```bash
.venv/bin/python -m xiaogpt.routing seed --amount 已发生的美元费用
.venv/bin/python -m xiaogpt.routing status
```

不要在已有请求后反复用累计费用重新seed，否则会重复计算。不能把账本删除当作“费用已清零”。恢复服务前备份账本；如果某次费用未知，须用对应generation记录核查后再由维护人员处理，不自动退款。

若已实际验证Sol返回403，先执行以下命令将其停用，然后上线Qwen与预算控制。权限恢复并单次测试成功后，才解除标记；不能把代码有Sol路径说成账号已能使用Sol。

```bash
.venv/bin/python -m xiaogpt.routing block --model openai/gpt-6.1-sol
# 仅在账号权限恢复并测试成功后：
.venv/bin/python -m xiaogpt.routing unblock --model openai/gpt-6.1-sol
```

离线验证：`.venv/bin/python -m unittest discover -s tests -v`。测试覆盖跨进程争用、月界、缺失usage、重启、403停用、空/部分回答、上下文与原生命令，使用占位凭据，不产生模型费用。

## 配置和回滚

所有账号、密码、DID、cookie、token、API密钥和家庭运行日志都放在仓库外。仓库中只有占位示例；提交前检查 `git diff --cached`。不要 `git add -f` 添加被忽略的运行配置。

升级前备份服务模板、配置和小米 token 到权限为700的本地备份目录。新版本先在独立环境验证小米登录、模型流式输出和两台播报，再切换模板并重启服务；失败则恢复之前模板，执行 `systemctl --user daemon-reload` 后重启实例。保留旧源码和虚拟环境便于回滚。
