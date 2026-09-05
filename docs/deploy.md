# CDT-switcher：部署到常驻 Linux 控制机

目标：把本地已验证的项目迁到常驻 VPS，以 **独立 Python 环境 + 专用用户 + systemd** 长期运行。无需 Docker，不额外开放公网端口。

离线测试通过不代表已经在目标服务器上验证；实际网络、内存和重启恢复仍须按本文确认。

本文假设控制机使用 **Debian/Ubuntu，且 PID 1 是 systemd**。如果是 Alpine/OpenRC，不要套用 systemd 步骤，应改用对应的服务管理方式。已有 AK、TG Token 和实例配置可以继续使用，不需要重建云资源。

## 1. 统一目录与执行位置

| 项目 | 本教程使用 |
|---|---|
| 本地项目 | /path/to/CDT-switcher |
| VPS 上传暂存目录 | /root/cdt-upload（仅 root 可访问） |
| VPS 正式目录 | /opt/cdt-switcher |
| 服务用户 | cdt-switcher（不能登录，不以 root 跑脚本） |
| 独立 Python 环境 | /opt/cdt-switcher/.venv |
| 生产文件 | 正式目录下 config.yaml、state.json、rotator.db |
| 日志 | systemd journal，不另写无限增长的日志文件 |

标注“VPS”的命令在服务器 **root shell** 执行；普通用户先运行 sudo -i。上传示例假设可 SSH 登录 root。如果禁用了 root SSH，就经普通用户上传，再用 sudo 安装到正式目录，不需要为部署开启 root SSH。

**只运行一个生产控制器。** 本机文件锁不能阻止 Mac 和 VPS 同时控制同一批 ECS。不要另用 cron、nohup、screen 再启动一份，也不能部署在被管理的 ECS 自己身上。

## 2. 检查控制机，准备环境

**VPS：**

~~~bash
cat /etc/os-release
ps -p 1 -o comm=
uname -m
free -h
df -h /
timedatectl status
~~~

确认 Debian/Ubuntu、PID 1 为 systemd、磁盘有余量、时间同步正常。若已有服务占满内存，先处理余量问题；本教程不会擅自创建 swap、重装系统或改动其他服务。

~~~bash
apt-get update
apt-get install -y python3 python3-venv ca-certificates tzdata rsync curl nano
python3 --version
~~~

当前 requirements 中的 requests 要求 **Python ≥ 3.10**；本地验证版本是 **3.12**。若只有 3.9 或更老，先停在这里，另行准备受支持的解释器。不要替换系统 /usr/bin/python3，也不要擅自降低固定依赖版本。

~~~bash
id cdt-switcher >/dev/null 2>&1 || useradd --system --user-group \
  --home-dir /opt/cdt-switcher --shell /usr/sbin/nologin cdt-switcher
install -d -m 700 /root/cdt-upload
install -d -o cdt-switcher -g cdt-switcher -m 750 /opt/cdt-switcher
~~~

如果正式目录已有生产安装，不要执行首次安装覆盖流程，转到第 10 节更新步骤。

## 3. 停止 Mac 控制器，再上传

先等当前切换结束，确认一台 Running、其余节省停机。如果原来已熔断，也能迁移，但迁移后仍保持保护状态。

**Mac：**在原来运行脚本的终端按 Ctrl+C，等待退出，再检查：

~~~bash
pgrep -fl cdt_switcher.py
~~~

没有相关进程才继续；若有后台托管，要同时停掉自动重启来源。**不要用 /breaker 代替停止脚本**：它会停止云实例。控制器正常退出不会自动停止当前 ECS。

### 3.1 上传代码：明确列出文件

**Mac：**把地址换成实际 IP，或以 root 登录的已有 SSH 别名。下面命令在同一个终端执行：

~~~bash
DEPLOY_HOST='root@server.example.com'
cd /path/to/CDT-switcher
rsync -av --exclude '__pycache__/' --exclude '*.pyc' \
  cdt_switcher.py requirements.txt config.example.yaml cdt-switcher.service \
  tests docs "$DEPLOY_HOST:/root/cdt-upload/"
~~~

不上传 .venv、.git、本地工具元数据和锁文件。虚拟环境不能从 macOS 搬到 Linux，应在目标目录重新创建；运行时直接指定解释器即可，无需 activate。[Python venv 文档](https://docs.python.org/3/library/venv.html)

### 3.2 上传配置与运行状态：只做首次迁移

确认源脚本已退出，在上面的 Mac 终端执行：

~~~bash
rsync -av \
  --include='/config.yaml' --include='/state.json' \
  --include='/rotator.db' --include='/rotator.db-wal' --include='/rotator.db-shm' \
  --exclude='*' ./ "$DEPLOY_HOST:/root/cdt-upload/"
~~~

注意：

- 保留账号键，例如 A/B；可以修改 instance_name，别随意更改账号键。
- 数据库存有本月累计运行时长。不要丢弃；脚本停止期间的时长无法靠云端状态查询补回。
- state.json 存切换游标和熔断意图。不要为了“干净启动”删它。
- SQLite 使用 WAL：不要在源程序运行时直接复制。正常退出后若仍有 -wal/-shm，一并迁移。
- 如果配置使用了自定义数据库/状态路径，上面白名单不会替你寻找文件。找到实际文件、停止源进程后一起迁移，并在下一节修改 VPS 路径。
- 本教程不迁移 .env 或 shell 环境变量。Mac 若曾通过环境变量覆盖阈值，应把期望值写入 VPS 的 YAML；否则会采用 YAML/默认值。
- 后续更新代码时**不再执行本节**，防止旧 Mac 数据覆盖 VPS 的新状态。

## 4. 安装文件与独立环境

**VPS，首次安装：**

~~~bash
install -o cdt-switcher -g cdt-switcher -m 640 \
  /root/cdt-upload/cdt_switcher.py /root/cdt-upload/requirements.txt \
  /root/cdt-upload/config.example.yaml /root/cdt-upload/cdt-switcher.service \
  /opt/cdt-switcher/
rsync -a --chown=cdt-switcher:cdt-switcher \
  /root/cdt-upload/tests /root/cdt-upload/docs /opt/cdt-switcher/
for cdt_file in config.yaml state.json rotator.db rotator.db-wal rotator.db-shm; do
  if [ -f "/root/cdt-upload/$cdt_file" ]; then
    install -o cdt-switcher -g cdt-switcher -m 600 \
      "/root/cdt-upload/$cdt_file" "/opt/cdt-switcher/$cdt_file"
  fi
done
cd /opt/cdt-switcher
runuser -u cdt-switcher -- python3 -m venv /opt/cdt-switcher/.venv
runuser -u cdt-switcher -- .venv/bin/python -m pip install --no-cache-dir -r requirements.txt
runuser -u cdt-switcher -- .venv/bin/python -m pip check
~~~

失败时先停在这里。不要往系统 Python 安装依赖，不要为了“更新”执行所有包的无差别升级。

只有**没迁移配置、首次从零配置**时才执行下面的复制；已有 config.yaml 就跳过：

~~~bash
install -o cdt-switcher -g cdt-switcher -m 600 \
  /opt/cdt-switcher/config.example.yaml /opt/cdt-switcher/config.yaml
~~~

编辑生产配置：

~~~bash
nano /opt/cdt-switcher/config.yaml
~~~

核对以下字段。它们属于不同层级，**不要把片段整段覆盖完整配置**：

~~~yaml
# 每个 accounts 条目内：
service_port: 31227

# 已有 thresholds 映射内：
db_path: /opt/cdt-switcher/rotator.db
state_path: /opt/cdt-switcher/state.json
enable_eventbridge_webhook: false
~~~

- service_port 填从 VPS 能访问的**公网服务监听端口**。如果 ShadowTLS 外层端口不同于 Snell 内层端口，应填外层端口；不是固定探测 443。
- 使用已验证的 AK、实例 ID、EIP、TG Token 和个人私聊 chat ID；首次部署不要用多人共享控制群。
- 保持正常流量阈值（默认 188GB），不要为测试改到当前用量以下。
- 默认启动等待 600 秒；进入服务探测后另计 240 秒。先保留默认值、观察真实耗时。
- 关闭 webhook 后无需放行 8787。TG 使用出站长轮询，不需要额外入站端口。
- ECS 安全组允许控制机访问实际服务端口；控制机需要出站访问 ECS/CDT API 和 Telegram。

密钥不要贴进聊天、截图或公开日志。

## 5. 启动前：离线测试与真实只读查询

**VPS：**

~~~bash
cd /opt/cdt-switcher
runuser -u cdt-switcher -- .venv/bin/python tests/mock_e2e.py
runuser -u cdt-switcher -- .venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
runuser -u cdt-switcher -- .venv/bin/python -c \
  'import cdt_switcher as m; m.load_config("config.yaml"); print("配置校验通过")'
~~~

前两项不连接真实云服务。故障注入中的 WARNING 是预期；最终应看到“全部 30 个场景通过”、Ran 21 tests 和 OK，不能有 FAILED。

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

TCP 成功只代表端口可连接，不证明 Snell PSK、ShadowTLS password 正确，实际代理使用还要由 Surge 验证。

## 6. 前台试跑：从这里开始会控制真实 ECS

确认 Mac 已停，VPS 无其他控制器。正常首次验收用一台 Running、其余节省停机，各账号有额度余量。

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

## 8. 小磁盘的日志与数据库

数据库默认保留流量/事件 180 天、运行时长 24 个月，每日北京时间 00:30 清理。这不是固定字节上限；SQLite 删除记录后文件也不一定立即缩小。仍需观察实际占用，不能定期删除数据库或 state.json 来省空间。

### 8.1 journal 保留限制（可选，影响全机）

**以下策略作用于整台 VPS 的 journal，不只本脚本，旧日志会被淘汰。** 如果已有策略，保留原策略即可；其他服务需要更长记录时，先调整数值。

**VPS：**

~~~bash
journalctl --disk-usage
install -d -m 755 /etc/systemd/journald.conf.d
nano /etc/systemd/journald.conf.d/60-small-vps.conf
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
4. Surge 通过在线节点实际访问成功，不只验证 TCP。
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

### 更新顺序

1. 等切换结束，VPS 停止服务并确认进程退出。
2. 做下述停机备份。
3. Mac 只重复 **3.1 代码上传**，不再上传旧 Mac 状态/配置，不用整目录 rsync --delete。
4. VPS 仅重新安装代码、requirements、tests、docs；**不要重跑第 4 节迁移生产状态的 for 循环**。保留 VPS 的配置、数据库、状态、锁文件和现有 venv。
5. 依赖变化时在同一 venv 执行 pip install --no-cache-dir -r requirements.txt 和 pip check；重新做离线测试。更新 unit 时也要重新安装 unit 并 daemon-reload，保留自己的 drop-in。
6. 启动服务，检查日志和 TG。更新失败先保持停止、查原因，不要顺手重新启动 Mac 控制器。

### 停机备份

只在 systemctl stop 已完成、没有其他控制进程时执行。备份包含密钥，必须私有保存：

~~~bash
install -d -m 700 /root/cdt-backups
CDT_BACKUP="/root/cdt-backups/cdt-$(date +%Y%m%d-%H%M%S).tar.gz"
cd /opt/cdt-switcher
(
  umask 077
  tar --exclude='./.venv' --exclude='./*.lock' --exclude='./__pycache__' \
    -czf "$CDT_BACKUP" .
)
ls -lh "$CDT_BACKUP"
~~~

备份不含 venv，恢复环境须按保存的 requirements 重建。备份不会自动过期，建议只保留最近 2～3 份，把长期副本安全转移到本地，避免备份本身占满小盘。上传暂存目录也含初次迁移的密钥和旧状态，不要公开或当作下一次更新的生产数据来源。

恢复时先停服务，在**新暂存目录**解压并检查，再明确选择恢复文件、设置属主和权限。不要往运行中的目录直接解压；恢复旧数据库会回退时长记录，先核对备份时间点及当前云端状态。

## 11. 常见异常

| 表现 | 先检查 |
|---|---|
| 安装依赖要求更高 Python | python3 --version；别修改系统解释器链接 |
| 203/EXEC、217/USER | venv 解释器、服务用户、目录权限 |
| Permission denied | 配置及数据库/state/lock 目录的属主、读写权限 |
| “已有 CDT-switcher 使用此配置或状态文件” | 前台进程或重复服务；不要删锁文件 |
| TG 409、轮询冲突 | Mac 或其他设备是否仍在使用同一 bot 长轮询 |
| TG TLS 超时、发送重试 | VPS 到 Telegram 的出站网络；不要关闭 TLS 验证 |
| 状态未知、暂停启停 | 阿里云 API 权限、网络、响应；不是实例已被删除 |
| T3 超时但客户端能用 | 控制机到公网服务端口的网络、安全组限源、外层代理端口 |
| 重启后停掉手动启动的机器 | 是否已熔断；满足余量后用 /resume，不要删状态 |
| 系统 OOM 杀进程 | journalctl -k -b、free -h，检查同机其他服务 |

稳定部署的标准：**只有一个控制器、真实只读查询正常、日志和磁盘可控、切换验收通过、重启后自动恢复。**
