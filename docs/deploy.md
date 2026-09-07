# 部署指南

本文使用 Debian/Ubuntu、Git、uv 和 systemd，在独立 Linux 控制机上部署 CDT-switcher。其他发行版需调整软件包及服务管理命令。

## 1. 部署准备

先完成[控制台准备与最小权限](permissions.md)中的 ECS/CDT 配置和 RAM 授权。准备各账户的地域、实例 ID、RAM AccessKey、公网地址，以及 Telegram Bot Token。

控制机需要访问阿里云 API、Telegram API 和各实例的业务 TCP 端口。业务服务应随受管实例启动；客户端自行负责选择可用节点。一个受管实例组使用一个控制器。

以下安装命令在控制机的 root shell 中执行；使用 sudo 的管理员可先运行 `sudo -i`。`runuser -u cdt-switcher --` 表示以本地服务用户执行后续命令。

| 用途 | 默认值 |
| --- | --- |
| 服务用户、服务组 | `cdt-switcher` |
| 项目、配置和状态目录 | `/opt/cdt-switcher` |
| Python 虚拟环境 | `/opt/cdt-switcher/.venv` |
| uv | `/usr/local/bin/uv` |
| uv 托管的 Python | `/var/lib/cdt-switcher/python` |
| systemd 服务 | `cdt-switcher.service` |
| 日志 | systemd journal |

使用其他目录或服务用户时，同步修改 unit 的 `User`、`Group`、`WorkingDirectory`、`ExecStart` 及配置中的状态路径。

## 2. 安装代码与运行环境

### 2.1 安装系统工具并下载项目

```bash
apt-get update
apt-get install -y git curl ca-certificates tzdata nano

git clone https://github.com/Pyaxy/CDT-switcher.git /opt/cdt-switcher
cd /opt/cdt-switcher
```

已有安装使用本文的更新流程。首次安装前确认系统时间同步正常，Python 和 HTTPS 请求依赖正确的系统时间及证书。

### 2.2 创建服务用户

```bash
id cdt-switcher >/dev/null 2>&1 || useradd --system --user-group \
  --home-dir /opt/cdt-switcher --shell /usr/sbin/nologin cdt-switcher

chown -R cdt-switcher:cdt-switcher /opt/cdt-switcher
chmod 750 /opt/cdt-switcher
install -d -o cdt-switcher -g cdt-switcher -m 750 /var/lib/cdt-switcher
```

服务用户负责读取配置和写入数据库、状态及运行锁。RAM 用户负责阿里云 API 授权，两者独立。

### 2.3 安装 uv 与 Python 依赖

若 `/usr/local/bin/uv` 尚未安装，执行：

```bash
curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
```

在项目目录创建 Python 3.12 环境：

```bash
cd /opt/cdt-switcher
runuser -u cdt-switcher -- /usr/local/bin/uv --version

runuser -u cdt-switcher -- env UV_PYTHON_INSTALL_DIR=/var/lib/cdt-switcher/python \
  /usr/local/bin/uv venv --managed-python --python 3.12 .venv

runuser -u cdt-switcher -- /usr/local/bin/uv pip install \
  --python .venv/bin/python --no-cache -r requirements.txt

runuser -u cdt-switcher -- /usr/local/bin/uv pip check --python .venv/bin/python
runuser -u cdt-switcher -- .venv/bin/python --version
```

虚拟环境依赖 `/var/lib/cdt-switcher/python` 下的解释器，该目录需保持服务用户可访问。程序通过 `.venv/bin/python` 运行；uv 用于安装阶段的环境和依赖管理。安装参数见 [uv 官方文档](https://docs.astral.sh/uv/reference/environment/#uv_install_dir)。

## 3. 填写配置

首次创建配置文件：

```bash
cd /opt/cdt-switcher
runuser -u cdt-switcher -- cp -n config.example.yaml config.yaml
chmod 600 config.yaml
nano config.yaml
```

替换模板中的 `YOUR_*` 占位符和文档示例地址。所有配置节位于 YAML 顶层：

| 配置 | 必填 | 说明 |
| --- | --- | --- |
| `accounts` | 是 | 每个键对应一组账户凭据及一台受管 ECS。 |
| `accounts.<key>.region` | 是 | 实例所在地域 ID。 |
| `accounts.<key>.access_key_id` / `access_key_secret` | 是 | 该账户的控制器 RAM 用户凭据。 |
| `accounts.<key>.instance_id` | 是 | 已授权的 ECS 实例 ID。 |
| `accounts.<key>.eip` | 是 | 预期在实例信息中出现的公网地址。 |
| `accounts.<key>.service_port` | 是 | 控制机探测的公网 TCP 端口。 |
| `accounts.<key>.instance_name` | 否 | 通知显示名；未配置时使用观测到的 ECS 名称。 |
| `telegram.bot_token` | 使用 Telegram 时 | Bot Token。 |
| `telegram.chat_ids` | 使用 Telegram 时 | 授权聊天的数字 ID 列表。 |
| `billing.enabled` | 否 | 布尔值，默认 `false`；开启账单命令和每日费用汇总。 |
| `thresholds` | 否 | 调度、探测、历史保留及状态路径参数。 |

账号键用于命令参数和本地历史记录，例如 `/switch A`、`/bill A`；运行期间应保持稳定。`chat_ids` 授权整个聊天，群组内的命令权限边界见 [Telegram 配置](permissions.md#6-telegram-bot-与聊天授权)。

安装环境准备完成后，可按该页面取得聊天 ID，再填写 `telegram.chat_ids`。

### 3.1 流量与运行时长

按账户实际非中国内地额度和业务需求设置：

```yaml
thresholds:
  traffic_threshold_gb: 188
  tmax_hours: 360
  db_path: /opt/cdt-switcher/rotator.db
  state_path: /opt/cdt-switcher/state.json
```

将字段合并到现有 `thresholds`，避免重复定义同名顶层键。程序以 `1024 ** 3` 换算流量，界面字段标记为 GB；核对控制台时需注意单位口径。流量累计范围、快照有效期和时长算法见[运行机制](state-machine.md)。

支持环境变量覆盖的字段在[配置示例](../config.example.yaml)中注明。systemd 服务不会继承交互式终端临时设置的环境变量；长期配置优先写入 YAML，或在 unit 的 `[Service]` 中使用 `Environment=`。

### 3.2 可选功能

- **账单查询**：按[账单指南](billing.md)增加只读权限并设置 `billing.enabled: true`。
- **OOS 恢复**：按 [OOS 指南](oos-setup.md)在云端独立配置，不需要在控制器中启用开关。
- **EventBridge Webhook**：实验性事件入口，默认关闭。启用时由部署者提供来源认证和受限的网络入口；Telegram 长轮询不需要此入口。

## 4. 启动前验证

### 4.1 本地配置与离线测试

```bash
cd /opt/cdt-switcher
runuser -u cdt-switcher -- .venv/bin/python -c \
  'import cdt_switcher as m; m.load_config("config.yaml"); print("配置校验通过")'

runuser -u cdt-switcher -- .venv/bin/python tests/mock_e2e.py
runuser -u cdt-switcher -- .venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
```

离线测试使用模拟云服务，验证程序流程；配置校验检查结构和字段，不验证云端授权。故障测试中的告警日志可能是预期输出，以测试最终结果和退出码为准。

### 4.2 云端只读验证

以下命令仅查询实例、CDT 用量，并检查运行中实例的 TCP 端口：

```bash
runuser -u cdt-switcher -- .venv/bin/python - <<'PY'
import socket
import cdt_switcher as m

cfg = m.load_config('config.yaml')
for name, account in cfg.accounts.items():
    client = m.AliyunAccountClient(account)
    obs = client.get_instance_obs()
    print(name, '状态=', obs.obs, '停机模式=', obs.stopped_mode,
          '公网地址匹配=', obs.eip_bound, '当班标签=', obs.duty_on)
    print(name, '本月非内地流量=', client.get_traffic_gb(), 'GB')
    if obs.obs == m.OBS_RUNNING:
        with socket.create_connection((account.eip, account.service_port), timeout=5):
            print(name, 'TCP 端口可达')
PY
```

预期获得有效实例状态和 CDT 数据；运行节点的公网地址应匹配，端口应可达。账户权限、网络或实例配置不匹配时，修正对应配置后再启动。

启用账单功能时，另执行：

```bash
runuser -u cdt-switcher -- .venv/bin/python billing_query.py --config config.yaml
```

这些只读检查不验证启停和标签写权限。写权限及完整切换流程需在服务启动后的维护窗口验证。

## 5. 安装并启动 systemd 服务

从此步骤开始，程序会根据当前状态调用真实实例的启停和标签接口。建议首次启动时保留一台业务正常的运行实例，其余实例处于节省停机状态。

安装仓库提供的 unit：

```bash
install -m 644 /opt/cdt-switcher/cdt-switcher.service \
  /etc/systemd/system/cdt-switcher.service
```

可通过 `systemctl edit cdt-switcher` 设置服务退出等待和启动限速：

```ini
[Unit]
StartLimitIntervalSec=300
StartLimitBurst=10

[Service]
RestartSec=15
TimeoutStopSec=120
```

`TimeoutStopSec` 给进行中的请求及任务留出退出时间；服务连续启动失败时，systemd 按上述规则限制重启频率。沿用已有服务管理策略时，可按实际要求调整这些值。

```bash
systemd-analyze verify /etc/systemd/system/cdt-switcher.service
systemctl daemon-reload
systemctl enable --now cdt-switcher
systemctl status cdt-switcher --no-pager
journalctl -u cdt-switcher -n 80 --no-pager
```

服务应显示 `active (running)`。在 Telegram 执行 `/check`、`/traffic`，核对当班节点、实例状态及流量。使用业务客户端确认认证和实际访问正常。

需要验证切换时，在维护窗口执行 `/switch 账号键`，观察启动、探测、停机和收尾结果，再到 ECS 控制台确认原节点已进入节省停机。启用账单时执行 `/bill` 和 `/bill 账号键`。

## 6. 运行管理

| 操作 | 命令 |
| --- | --- |
| 查看服务状态 | `systemctl status cdt-switcher --no-pager` |
| 查看最近日志 | `journalctl -u cdt-switcher -n 100 --no-pager` |
| 持续查看日志 | `journalctl -u cdt-switcher -f` |
| 查看实际服务配置 | `systemctl cat cdt-switcher` |
| 查看进程和重启次数 | `systemctl show cdt-switcher -p MainPID -p NRestarts` |
| 停止控制器 | `systemctl stop cdt-switcher` |
| 修改应用配置后重启 | `systemctl restart cdt-switcher` |
| 修改 unit 后加载 | `systemctl daemon-reload`，然后重启服务。 |
| 启动限速解除 | 排除启动错误后执行 `systemctl reset-failed cdt-switcher`，再启动服务。 |

停止控制器会结束管理进程，实例保持其云端状态。`/breaker confirm` 则请求停止受管实例，控制器继续运行。

日志由 systemd journal 管理。数据库中的流量和事件默认保留 180 天，运行时长按月份清理；账单仅缓存于内存。保留参数见配置示例。磁盘占用可使用 `journalctl --disk-usage` 和 `du -sh /opt/cdt-switcher` 检查；全机日志保留策略由系统管理员统一设置。

### 主机重启验证

检查 `systemctl is-enabled cdt-switcher` 返回 `enabled`。在允许重启控制机的维护窗口重启，重新登录后确认：

```bash
systemctl status cdt-switcher --no-pager
journalctl -u cdt-switcher -b -n 80 --no-pager
```

再次使用 `/check` 和业务客户端验证。开机自启注册、进程运行及业务可用性是三个独立检查项。

## 7. 更新代码与配置

等待正在执行的切换完成，再更新控制器。以下命令逐条执行，失败时停止后续步骤：

```bash
systemctl stop cdt-switcher
cd /opt/cdt-switcher
runuser -u cdt-switcher -- git status --short
runuser -u cdt-switcher -- git pull --ff-only

runuser -u cdt-switcher -- /usr/local/bin/uv pip install \
  --python .venv/bin/python --no-cache -r requirements.txt
runuser -u cdt-switcher -- /usr/local/bin/uv pip check --python .venv/bin/python

nano config.yaml
runuser -u cdt-switcher -- .venv/bin/python -c \
  'import cdt_switcher as m; m.load_config("config.yaml"); print("配置校验通过")'

systemctl start cdt-switcher
systemctl status cdt-switcher --no-pager
journalctl -u cdt-switcher -n 50 --no-pager
```

更新前可按下一节备份。若源码存在本地修改，先检查差异；`--ff-only` 不会强行覆盖本地分支。依赖文件未变化时可跳过依赖安装。`config.yaml` 和运行状态不由 Git 跟踪，需按更新说明手动合并新增配置字段。

若更新了 service 文件且需采用其变更，重新安装 unit 并执行 `daemon-reload`，保留已有 drop-in。仅修改 `config.yaml` 时，编辑后直接重启服务即可。

## 8. 备份与迁移

本地数据包括 `config.yaml`、`state.json`、`rotator.db` 及可能存在的 SQLite WAL/SHM 文件。配置包含密钥，备份应存放在仅管理员可读的位置。

等待切换完成并停止控制器后，执行：

```bash
systemctl stop cdt-switcher
install -d -m 700 /root/cdt-backups
CDT_BACKUP="/root/cdt-backups/cdt-$(date +%Y%m%d-%H%M%S).tar.gz"
cd /opt/cdt-switcher
(
  umask 077
  tar --exclude='./.venv' --exclude='./.git' --exclude='./*.lock' --exclude='./__pycache__' \
    -czf "$CDT_BACKUP" .
)
ls -lh "$CDT_BACKUP"
```

普通备份完成后重新启动服务；迁移时保持旧控制器停止。新控制机先安装代码和环境，再将备份解压到暂存目录，核对并复制配置及状态文件。虚拟环境在目标机重建。

恢复文件后设置属主和权限：

```bash
chown -R cdt-switcher:cdt-switcher /opt/cdt-switcher
chmod 600 /opt/cdt-switcher/config.yaml
```

核对账号键、状态路径及服务配置，再启动新控制器。SQLite 保存本地时长历史，`state.json` 保存切换进度和保护意图；使用更早的备份会回退这些本地记录。运行锁由本机内核管理，迁移不复制锁文件；运行期间保留锁文件路径不变。

## 9. 故障排查

| 现象 | 检查方向 |
| --- | --- |
| ECS/CDT 权限错误 | RAM 用户、动作名称、实例 ARN 和账户范围，见[权限配置](permissions.md)。 |
| 服务启动失败 | `journalctl` 中的配置错误、Python 路径、服务用户及文件权限。 |
| 本地运行锁冲突 | 是否存在另一前台进程或 systemd 服务使用相同配置和状态路径。 |
| Telegram 轮询冲突 | 是否有其他进程使用同一 Bot，或仍配置了 Telegram Webhook。 |
| 实例状态未知 | 阿里云 API 连通性、权限和返回数据。 |
| TCP 探测失败 | 公网地址、安全组、实例防火墙、业务监听及控制机到入口的连接。 |
| 控制器反复停止人工启动的实例 | 检查是否处于全局保护状态；恢复服务使用 `/resume`。 |
| 账单失败或旧数据 | 参见[账单故障排查](billing.md#7-常见问题)。 |

在提交日志用于排查前，移除密钥、Bot Token 和不需公开的资源标识。
