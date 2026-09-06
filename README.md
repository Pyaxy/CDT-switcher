# CDT-switcher

运行在独立控制机上的阿里云国际站多账号 Spot ECS 轮换守护程序。程序根据累计运行时间和 CDT 非内地流量选择节点，先确认目标实例及服务端口可用，再将旧实例置于节省停机状态。Telegram 提供运行状态查询、调度通知和控制命令，并可选启用账户月账单查询及每日费用汇总。

## 主要能力

- 支持任意数量的账号和实例，不依赖固定的 A/B 命名。
- 状态机游标持久化，进程重启后继续未完成的切换。
- 严格区分未知状态、实例不存在、普通停机和节省停机。
- 流量或运行时长超限时自动调度；无可用节点时进入全局保护停机。
- 目标服务连续 TCP 探测成功后才停止原节点。
- Telegram 通知及 `/check`、`/traffic`、`/switch`、`/breaker`、`/resume`、`/last` 命令。
- 可选账单功能：`/bill` 查询各账户本月费用，指定账号键可按产品展开；每日运行报告附加账户费用。
- 账单查询独立于状态机执行，支持请求合并和当月内存缓存；账单查询失败不影响实例调度。
- 通知优先显示配置的 `instance_name`，并保留命令使用的稳定账号键。
- 兼容 OOS `duty=on` 标签看门狗，包含本机进程互斥和 SQLite 历史清理。

## 快速开始

要求 Python 3.10 或更高版本。不要使用主账号 AccessKey，也不要把生产配置提交到 Git。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp config.example.yaml config.yaml
chmod 600 config.yaml
```

填写 `config.yaml` 后先运行离线测试：

```bash
.venv/bin/python tests/mock_e2e.py
.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
```

确认控制机能够访问阿里云 API、Telegram API 和各实例的服务端口，再以前台方式首次启动：

```bash
.venv/bin/python cdt_switcher.py config.yaml
```

## 账户账单查询（可选）

账单功能默认关闭。为各账户使用的 RAM 用户增加只读权限 `bss:DescribeBillList` 后，在 `config.yaml` 顶层配置：

```yaml
billing:
  enabled: true
```

重启主程序后启用以下入口：

| 入口 | 功能 |
| --- | --- |
| `/bill` | 显示全部配置账户本月的税前金额小计，按币种和账单类型分别列示。 |
| `/bill A` | 显示账号键 `A` 对应账户的产品费用明细。 |
| 每日运行报告 | 每日 23:58（UTC+8）刷新并附加各账户本月费用。 |

手动查询在五分钟内复用同一账户的最近结果；缓存仅保留当月汇总，不写入数据库。刷新失败时当前金额标记为未知，同月历史结果附带旧数据标记和查询时间。账单统计范围为凭据所属账户，费用不参与流量保护、选机或停机决策。

配置字段、RAM 策略、终端验证和故障排查见[账单查询配置指南](docs/billing.md)。

## 文档

完整说明：

- [部署教程](docs/deploy.md)
- [状态机设计](docs/state-machine.md)
- [OOS 看门狗配置](docs/oos-setup.md)
- [账单查询配置指南](docs/billing.md)

## 安全提示

- `config.yaml`、`.env`、`state.json`、`rotator.db*` 和运行锁均已加入 `.gitignore`。
- 本机文件锁不能阻止两台不同主机同时控制同一批 ECS；迁移前必须停止旧控制器。
- TCP 探测只能确认端口可连接，不能验证代理密码或完整业务可用性。
- EventBridge webhook 是实验功能，默认关闭；启用前应增加来源认证和网络边界保护。
- 启动守护程序后，它可能调用 ECS 启停和标签 API。请先完成只读检查并在低流量窗口演练。
