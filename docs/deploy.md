# 部署指南

本文介绍在独立 Linux 控制机上部署 CDT-switcher，使用 Git 获取源码、uv 管理 Python 环境、systemd 托管服务。示例适用于 Debian/Ubuntu + systemd；其他发行版需调整包管理和服务管理命令。

除特别说明外，命令在服务器的 **root shell** 中逐段执行。具有 sudo 权限的管理员可先执行 `sudo -i`。任一步失败时，应解决错误后再继续。

首次部署流程：下载项目 → 创建运行用户 → 准备 Python 环境 → 配置账号 → 验证 → 启动服务。已有部署的更新见第 10 节，跨控制机迁移见第 12 节。

部署前准备：

- 一台独立、持续在线的 Linux 控制机，不属于脚本管理的 ECS 实例。
- 每个账号的 RAM AccessKey、地域、ECS 实例 ID 和预期公网 IP。需具备实例查询、启停、标签管理及 CDT 流量查询权限。
- 支持节省停机的实例，以及随实例启动自动运行的业务服务。
- Telegram Bot Token 和允许发送控制命令的 chat ID。
- 控制机到阿里云 API、Telegram API 和实例业务端口的网络连接。

项目使用 Python 3.10 及以上版本；下文由 uv 准备 Python 3.12。运行配置、凭据和状态文件不包含在 Git 仓库中。

## 1. 部署约定

本指南使用不能交互登录的 `cdt-switcher` 服务用户，与仓库提供的 systemd unit 保持一致。管理员负责安装和配置，服务进程以普通用户身份运行。

uv 隔离 Python 和依赖环境，服务用户限制系统文件访问权限。专用用户并非程序的硬性要求；复用现有普通用户时，需同步调整目录属主、runuser 命令和 unit 中的 User/Group。

| 用途 | 路径 |
|---|---|
| 项目、配置、运行数据 | /opt/cdt-switcher |
| 虚拟环境 | /opt/cdt-switcher/.venv |
| uv 程序 | /usr/local/bin/uv |
| uv 下载的 Python | /var/lib/cdt-switcher/python |
| 日志 | systemd journal |

只允许一个生产控制器。迁移前停止本地或旧服务器上的脚本；控制机不能是被管理的 ECS 自己。

## 2. 下载项目

先检查系统：

~~~bash
cat /etc/os-release
ps -p 1 -o comm=
free -h
df -h /
timedatectl status
~~~

若不是 Debian/Ubuntu + systemd，服务管理步骤需另行适配。确认有磁盘和内存余量、时间同步正常。

安装工具并克隆：

~~~bash
apt-get update
apt-get install -y git curl ca-certificates tzdata nano
git clone https://github.com/Pyaxy/CDT-switcher.git /opt/cdt-switcher
cd /opt/cdt-switcher
~~~

上面是首次安装。如果目录已存在，先检查内容；已有安装按第 10 节更新，不要删除目录重新 clone。

## 3. 创建运行用户

~~~bash
id cdt-switcher >/dev/null 2>&1 || useradd --system --user-group \
  --home-dir /opt/cdt-switcher --shell /usr/sbin/nologin cdt-switcher

chown -R cdt-switcher:cdt-switcher /opt/cdt-switcher
install -d -o cdt-switcher -g cdt-switcher -m 750 /var/lib/cdt-switcher
~~~

命令说明：

- useradd：创建不能交互登录的服务用户，已经存在就跳过。
- chown：让该用户拥有项目目录，能写入数据库、状态和锁文件。
- install -d：创建 uv 托管 Python 的父目录并设置属主。

后续 `runuser -u cdt-switcher -- 命令` 表示以服务用户身份执行指定命令。

### 可选：用别名缩短命令

在当前交互式 Bash 终端中，可以定义：

~~~bash
alias cdt='runuser -u cdt-switcher --'
~~~

随后将命令开头的 `runuser -u cdt-switcher --` 替换为 `cdt`，例如：

~~~bash
cd /opt/cdt-switcher
cdt .venv/bin/python --version
cdt /usr/local/bin/uv pip check --python .venv/bin/python
~~~

别名仅简化输入，不改变运行身份或当前目录；仍需在 root shell 中使用。默认只在当前终端有效。若需在后续交互式 Bash 会话中使用，可将 alias 定义加入管理员自己的 ~/.bashrc，再执行 `source ~/.bashrc`。取消别名使用 `unalias cdt`。

后续示例保留完整命令，便于独立复制。非交互式 Bash 脚本默认不展开别名，systemd 的 ExecStart 也不读取 shell 别名；这些场景继续使用完整命令。Shell 重定向（例如 `> 文件`）仍由当前 shell 执行，不能据此认为文件会以服务用户身份创建。


## 4. 用 uv 创建环境，填写配置

### 4.1 确保服务用户也能执行 uv

检查：

~~~bash
command -v uv
runuser -u cdt-switcher -- /usr/local/bin/uv --version
~~~

如果服务用户能够通过 /usr/local/bin/uv 输出版本，可跳过安装。仅安装在管理员私有目录（例如 /root/.local/bin）中的 uv 可能无法被服务用户访问；本指南统一使用公共路径 /usr/local/bin/uv：

~~~bash
curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
runuser -u cdt-switcher -- /usr/local/bin/uv --version
~~~

该安装方式不替换系统 Python。安装方式见 [uv 官方文档](https://docs.astral.sh/uv/getting-started/installation/)。

### 4.2 创建项目环境并安装依赖

~~~bash
cd /opt/cdt-switcher

runuser -u cdt-switcher -- env UV_PYTHON_INSTALL_DIR=/var/lib/cdt-switcher/python \
  /usr/local/bin/uv venv --managed-python --python 3.12 .venv

runuser -u cdt-switcher -- /usr/local/bin/uv pip install \
  --python .venv/bin/python --no-cache -r requirements.txt

runuser -u cdt-switcher -- /usr/local/bin/uv pip check --python .venv/bin/python
runuser -u cdt-switcher -- .venv/bin/python --version
~~~

第一条环境命令由 uv 下载/选择托管的 Python 3.12 并创建 .venv；后两条安装、检查依赖。Python 存放在服务用户可访问的路径，避免虚拟环境指向 /root 里的解释器而导致 systemd 启动失败。不要删除 /var/lib/cdt-switcher/python，.venv 仍依赖它。[uv 环境说明](https://docs.astral.sh/uv/pip/environments/)、[Python 存放路径设置](https://docs.astral.sh/uv/reference/environment/#uv_python_install_dir)

已有正常的 .venv 时可跳过重建，仅安装和检查依赖。项目目前使用 requirements.txt，因此用 `uv pip install`；不要求引入 pyproject.toml 或改用 uv sync。

uv 创建的环境不一定自带 pip；后续继续用 uv 管理依赖。**运行程序直接使用 .venv/bin/python**，不用 activate，systemd 也不用在每次启动时运行 uv 或下载依赖。

### 4.3 配置文件

仅在还没有 config.yaml 时复制模板：

~~~bash
cd /opt/cdt-switcher
runuser -u cdt-switcher -- cp -n config.example.yaml config.yaml
chmod 600 config.yaml
nano config.yaml
~~~

按照 config.example.yaml 填写配置。YOUR_* 和文档示例 IP 均为占位内容，必须替换。

| 配置字段 | 含义 |
|---|---|
| accounts 下的账号键 | 稳定的内部标识，供 /switch 使用；开始运行后不宜随意改名 |
| region / instance_id | 实际地域和 ECS 资源 ID |
| access_key_id / access_key_secret | 对应 RAM 用户的访问凭据 |
| instance_name | 可选显示名；省略时从云端查询 |
| eip | 预期绑定的公网 IP |
| service_port | 控制机实际探测的公网 TCP 端口 |
| telegram.bot_token / chat_ids | Bot 凭据与允许访问的会话 ID |
| thresholds | 流量、运行时长、探测和保留策略；完整说明见配置模板 |

建议使用 Bot 私聊配置控制权限。当前 chat_ids 授权的是整个会话，不是群内的特定用户。

标准安装路径对应的 thresholds 设置如下，应合并到已有映射中：

~~~yaml
thresholds:
  db_path: /opt/cdt-switcher/rotator.db
  state_path: /opt/cdt-switcher/state.json
  enable_eventbridge_webhook: false
~~~

service_port 应对应实际业务的公网监听端口；存在反向代理或转发层时，填写控制机可访问的入口端口。流量和运行时长阈值应根据账号额度及业务需求设置。默认关闭实验性 webhook，Telegram 长轮询不需要额外开放入站端口。

## 5. 启动前：离线测试与真实只读查询

**VPS：**

~~~bash
cd /opt/cdt-switcher
runuser -u cdt-switcher -- .venv/bin/python tests/mock_e2e.py
runuser -u cdt-switcher -- .venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
runuser -u cdt-switcher -- .venv/bin/python -c \
  'import cdt_switcher as m; m.load_config("config.yaml"); print("配置校验通过")'
~~~

前两项不连接真实云服务。故障注入测试可能输出预期的 WARNING；以最终场景全部通过、单元测试 OK 和退出码 0 为准。

下面真实查询阿里云、测试 TCP，**不启动、不停止实例，不修改标签**：

~~~bash
runuser -u cdt-switcher -- .venv/bin/python - <<'PY'
import socket
import cdt_switcher as m

cfg = m.load_config('config.yaml')
for name, account in cfg.accounts.items():
    client = m.AliyunAccountClient(account)
    obs = client.get_instance_obs()
    print(name, obs.status, obs.stopped_mode,
          'EIP绑定=', obs.eip_bound, 'duty=', obs.duty_on)
    print(name, '本月非内地流量=', client.get_traffic_gb(), 'GB')
    if obs.obs == m.OBS_RUNNING:
        with socket.create_connection((account.eip, account.service_port), timeout=5):
            print(name, '服务端口可达')
PY
curl -I --connect-timeout 5 --max-time 15 https://api.telegram.org
~~~

权限错误、TLS 超时、Running 节点端口不可达时，先解决再启动。Telegram 返回 HTTP 响应只说明 HTTPS 链路有响应，不证明 Token/chat ID 正确；最终用 /check 验证。

TCP 成功仅表明端口可连接，业务认证与端到端可用性仍需使用实际客户端验证。

## 6. 前台试跑：从这里开始会控制真实 ECS

确认没有其他控制器在管理同一批实例。常规首次验收建议一台实例 Running、其余节省停机，并保证账号有额度余量。

**VPS：**

~~~bash
cd /opt/cdt-switcher
runuser -u cdt-switcher -- .venv/bin/python cdt_switcher.py config.yaml
~~~

预期日志有“启动时刷新 CDT 流量”“CDT 流量刷新完成”“首轮 reconcile 完成”。稳态不会每几秒刷日志；**安静不代表退出，也不保证启动立即发送 TG 通知**。在 TG 私聊发 /check、/traffic 确认响应。

重启会查询当前云端状态：无未完成切换且未熔断时，唯一 Running 节点成为当班；有流水线则续跑；已熔断则保留保护、纠正意外启动。不能通过重启清除保护意图。

确认正常后 Ctrl+C，等“已优雅退出”。另开 VPS 终端检查：

~~~bash
pgrep -af '[p]ython.*cdt_switcher.py'
~~~

没有 Python 控制进程后，再交给 systemd。

## 7. systemd 托管

**VPS：**直接安装仓库现成 unit，路径与本文一致。

~~~bash
install -m 644 /opt/cdt-switcher/cdt-switcher.service \
  /etc/systemd/system/cdt-switcher.service
install -d -m 755 /etc/systemd/system/cdt-switcher.service.d
nano /etc/systemd/system/cdt-switcher.service.d/override.conf
~~~

首次创建此 drop-in 时填入；已有内容则合并，不盲目覆盖：

~~~ini
[Unit]
StartLimitIntervalSec=300
StartLimitBurst=10

[Service]
RestartSec=15
TimeoutStopSec=120
~~~

仓库 unit 已包含非 root 用户、UMask=0077、独立环境路径、北京时间、自动重启和日志速率限制。这里把停机等待从 20 秒延长到 120 秒，给正在执行的请求和调度任务留出退出时间；反复启动失败则限速，修好后手动复位。systemd 的自动重启仍受启动限速约束。[systemd 服务文档](https://github.com/systemd/systemd/blob/main/man/systemd.service.xml)

~~~bash
systemd-analyze verify /etc/systemd/system/cdt-switcher.service
systemctl daemon-reload
systemctl enable --now cdt-switcher
systemctl status cdt-switcher --no-pager
journalctl -u cdt-switcher -n 80 --no-pager
~~~

verify 若报错或提示未知指令，先检查，不要直接忽略。状态应为 active (running)，日志是正常初始化而不是不断重启。

~~~bash
systemctl show cdt-switcher -p User -p MainPID -p NRestarts -p MemoryCurrent
journalctl -u cdt-switcher -f
~~~

Ctrl+C 只退出日志查看，不停止服务。现在可以断开 SSH，不需要另加后台命令。

本机进程锁会拒绝共享配置、数据库或状态路径的第二个进程；.lock 文件保留是正常的，退出/崩溃由内核释放锁。**运行中不要删除或替换锁文件**。不同主机、使用完全独立文件路径的副本不在该锁保护范围内。

## 8. 日志与数据保留

数据库默认保留流量/事件 180 天、运行时长 24 个月，每日北京时间 00:30 清理。这不是固定字节上限；SQLite 删除记录后文件也不一定立即缩小。仍需观察实际占用，不能定期删除数据库或 state.json 来省空间。

### 8.1 journal 保留限制（可选，影响全机）

**以下策略作用于整台 VPS 的 journal，不只本脚本，旧日志会被淘汰。** 如果已有策略，保留原策略即可；其他服务需要更长记录时，先调整数值。

**VPS：**

~~~bash
journalctl --disk-usage
install -d -m 755 /etc/systemd/journald.conf.d
nano /etc/systemd/journald.conf.d/60-retention.conf
~~~

填入：

~~~ini
[Journal]
Storage=persistent
SystemMaxUse=200M
SystemKeepFree=1G
RuntimeMaxUse=32M
MaxRetentionSec=14day
~~~

~~~bash
systemctl restart systemd-journald
journalctl --flush
journalctl --disk-usage
~~~

这是日志轮转/保留策略，不是全盘配额，不保证立即缩到精确 200MB；其他文件和 /var/log/syslog 不受它限制。不要额外把脚本输出无限重定向到 run.log。[journald 文档](https://github.com/systemd/systemd/blob/main/man/journald.conf.xml)

### 8.2 日常观察

~~~bash
systemctl show cdt-switcher -p MemoryCurrent -p NRestarts
du -sh /opt/cdt-switcher
du -h /opt/cdt-switcher/rotator.db /opt/cdt-switcher/rotator.db-wal 2>/dev/null
journalctl --disk-usage
df -h /
~~~

不要未经实测设置很低的 MemoryMax，避免杀掉正常请求。部署后观察一天的内存、重启次数、磁盘增长，再决定是否增加资源限制。

## 9. 上线与重启验收

按顺序确认：

1. 服务持续 active (running)，NRestarts 不持续增长。
2. /check 有报告，/traffic 能刷新真实用量，没有长期未知或异常零值。
3. 云端一台 Running，其余节省停机；当班 duty=on，其余无 duty=on。代码关闭标签采用删除，因此标签缺失不等于故障。
4. 使用实际客户端验证在线节点的业务功能，不只检查 TCP 连接。
5. 如做真机切换演练，在低流量时发 /switch B（替换为真实备用账号键），确认目标启动、服务探测、旧机节省停机和 TG 完成通知。不要调低阈值硬触发。
6. 每日北京时间 23:58 应有汇总；没收到先查服务与 TG 链路，不单凭缺失通知判断程序死亡。

**重启验收会中断控制机上的其他服务。** 选择维护窗口、确认有办法重新 SSH 后才执行：

~~~bash
systemctl is-enabled cdt-switcher
reboot
~~~

重新连接 VPS 后：

~~~bash
systemctl is-active cdt-switcher
systemctl status cdt-switcher --no-pager
journalctl -u cdt-switcher -b -n 80 --no-pager
~~~

再发 /check，核对当班节点。enabled 仅表示注册了自启；**重启后的进程、日志、TG、云端状态都正常，才算自启验收通过**。

默认 webhook 关闭，无需检查入站监听。已有 OOS 可以保留，见 [OOS 教程](oos-setup.md)。它按 duty=on 定时拉起 ECS，不能修复控制机或控制器本身；标签更新失败时也可能干扰手动停机测试，需结合日志判断。

## 10. 日常操作、更新、备份

| 目的 | VPS 命令 |
|---|---|
| 查看最近日志 | journalctl -u cdt-switcher -n 100 --no-pager |
| 停控制器，不停 ECS | systemctl stop cdt-switcher |
| 改配置/代码后重启 | systemctl restart cdt-switcher |
| 改 unit/drop-in | systemctl daemon-reload 后 systemctl restart cdt-switcher |
| 修复连续启动失败后恢复 | systemctl reset-failed cdt-switcher 后 systemctl start cdt-switcher |
| 查看生效服务配置 | systemctl cat cdt-switcher |

### 更新代码：git pull + uv

等当前切换完成，停止控制器并先按下一小节备份。然后执行：

~~~bash
systemctl stop cdt-switcher
cd /opt/cdt-switcher
runuser -u cdt-switcher -- git status --short
runuser -u cdt-switcher -- git pull --ff-only
runuser -u cdt-switcher -- /usr/local/bin/uv pip install \
  --python .venv/bin/python --no-cache -r requirements.txt
runuser -u cdt-switcher -- /usr/local/bin/uv pip check --python .venv/bin/python
runuser -u cdt-switcher -- .venv/bin/python tests/mock_e2e.py
runuser -u cdt-switcher -- .venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
~~~

逐条执行，任一步失败就停止后续步骤。若 git status 显示源码有本地修改，先检查差异，不强制覆盖；git pull 失败时也不要继续安装。

确认通过后：

~~~bash
systemctl start cdt-switcher
systemctl status cdt-switcher --no-pager
journalctl -u cdt-switcher -n 80 --no-pager
~~~

git pull 不会携带旧本地配置来覆盖生产状态。若本次更新了 service 文件，需按第 7 节重新安装 unit 并 daemon-reload，保留自己的 override.conf。

### 启用账户账单查询

账单功能默认关闭，已有部署更新代码后仍保持原有运行行为。启用前，需为各账户配置的 RAM 用户增加 `bss:DescribeBillList` 只读权限，并在 `config.yaml` 顶层设置 `billing.enabled: true`。配置变更通过重启 `cdt-switcher` 生效；仅修改应用配置不需要 `systemctl daemon-reload`。

启用后可通过 `/bill` 查询全部账户本月费用，或通过 `/bill 账号键` 展开单账户产品费用。每日 23:58（UTC+8）的运行报告会附加本月费用。完整配置、独立终端验证及故障排查见[账单查询配置指南](billing.md)。

### 停机备份

只在 systemctl stop 已完成、没有其他控制进程时执行。备份包含密钥，必须私有保存：

~~~bash
install -d -m 700 /root/cdt-backups
CDT_BACKUP="/root/cdt-backups/cdt-$(date +%Y%m%d-%H%M%S).tar.gz"
cd /opt/cdt-switcher
(
  umask 077
  tar --exclude='./.venv' --exclude='./.git' --exclude='./*.lock' --exclude='./__pycache__' \
    -czf "$CDT_BACKUP" .
)
ls -lh "$CDT_BACKUP"
~~~

备份不含 venv，恢复环境须按保存的 requirements 重建。备份不会自动过期，建议只保留最近 2～3 份，把长期副本安全转移到本地，避免备份本身占满小盘。备份含生产密钥，不要放进公开仓库。

恢复时先停服务，在**新暂存目录**解压并检查，再明确选择恢复文件、设置属主和权限。不要往运行中的目录直接解压；恢复旧数据库会回退时长记录，先核对备份时间点及当前云端状态。

## 11. 常见异常

| 表现 | 先检查 |
|---|---|
| uv 无法执行或 Python 无权限 | 检查 /usr/local/bin/uv 与 /var/lib/cdt-switcher/python；不要让 .venv 指向 /root 下的解释器 |
| 203/EXEC、217/USER | venv 解释器、服务用户、目录权限 |
| Permission denied | 配置及数据库/state/lock 目录的属主、读写权限 |
| “已有 CDT-switcher 使用此配置或状态文件” | 前台进程或重复服务；不要删锁文件 |
| TG 409、轮询冲突 | 其他控制器是否仍在使用同一 bot 长轮询 |
| TG TLS 超时、发送重试 | VPS 到 Telegram 的出站网络；不要关闭 TLS 验证 |
| 状态未知、暂停启停 | 阿里云 API 权限、网络、响应；不是实例已被删除 |
| T3 超时但客户端能用 | 控制机到公网服务端口的网络、安全组限源、外层代理端口 |
| 重启后停掉手动启动的机器 | 是否已熔断；满足余量后用 /resume，不要删状态 |
| 系统 OOM 杀进程 | journalctl -k -b、free -h，检查同机其他服务 |

## 12. 迁移已有部署（可选）

首次部署可跳过本节。git clone 只下载公开代码，不包含运行配置、数据库和状态。

迁移已有部署时：先等待切换完成并停止旧脚本，再通过 SFTP/scp 等方式，将实际使用的 config.yaml、state.json、rotator.db 以及仍存在的 rotator.db-wal/rotator.db-shm 复制到新项目目录。不要上传到 GitHub，也不要覆盖正在运行的数据库。然后在 VPS 执行：

~~~bash
chown -R cdt-switcher:cdt-switcher /opt/cdt-switcher
chmod 600 /opt/cdt-switcher/config.yaml
~~~

迁移后核对 YAML 中的数据路径及账号键。本月时长来自 SQLite，切换游标和熔断意图来自 state.json；不迁移就无法保留这些本地记录。旧 shell 环境变量不会随文件迁移，应把需要的配置写入 YAML。

停止控制器不会自动停止 ECS；/breaker 是云实例保护停机命令，不能代替退出脚本。同一批实例只能由一个生产控制器管理。
