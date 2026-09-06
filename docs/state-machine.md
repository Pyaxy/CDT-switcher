# CDT-switcher 状态机设计（v2）

> Aliyun 多账号 CDT 抢占式 ECS 轮换保活 · 核心设计文档
>
> **目标**：N 个阿里云国际站账号，每账号一台抢占式（Spot）ECS + EIP，永远恰好一台实例在线服务；通过轮换叠加各账号的 CDT 免费流量额度（非内地 200GB/月/账号），并让各账号的月运行时长自然均衡。
>
> **原则**：云侧真实状态是唯一事实来源；一切动作幂等、异步核实；单写者单飞；**节省停机未确认绝不放过**。

---

## 1. 设计原则

1. **Level-triggered（电平触发）**：每 60s reconcile 一次，读云侧真实状态 → 推导期望状态 → 有偏差就纠偏。脚本崩溃、重启、漏事件都不会脑裂——状态不依赖"上次发生了什么"，只依赖"现在是什么"。
2. **一切动作幂等 + 异步核实**：任何写操作（启停实例、打标签）发出后必须回读确认生效才进入下一步；重复执行同一步无副作用，并按实例分别退避，避免持续失败时打爆 API。
3. **单写者单飞**：全局同一时刻最多一个 TRANSITION 流水线在跑。TG 命令、定时事件、巡检纠偏全部先转成"意图"入队，由状态机串行消费。
4. **节省停机确认是硬门槛**：`Stopped + StopCharging` 回读确认之前，流程不结束、通知不发出、状态不推进；确认失败进入升级流程（持续重试 + 周期告警），绝不静默放过。

---

## 2. 实例观测状态（每台实例，`DescribeInstances` 回读）

| 观测态 | 判定条件 | 含义 | 处理 |
|---|---|---|---|
| `RUNNING` | Status=Running | 运行中 | 正常；每 tick 为其账号累积运行时长 |
| `STARTING` | Status=Starting | 启动过渡中 | 等待轮询 |
| `STOPPING` | Status=Stopping | 停机过渡中 | 等待轮询 |
| `STOPPED_SC` | Status=Stopped **且** StoppedMode=StopCharging | **节省停机（已确认）** ✅ | 唯一合法的停机终态 |
| `STOPPED_KM` | Status=Stopped 且 StoppedMode=KeepCharging | **普通停机——节省停机未生效，计算费照收** ⚠️ | 重新下发 StopCharging 停机 + 告警 |
| `NOT_FOUND` | DescribeInstances 成功返回且明确查无实例 | 被释放 | 按实例丢失处理，告警 |
| `UNKNOWN` | 云 API、网络、权限、解析异常，或未识别的新状态 | 暂时无法确认 | 保留最后有效状态，冻结新的自动启停并告警；绝不当作 NOT_FOUND |

**正交标志**：`LOCK_RECYCLING`（LockReason=Recycling，仅 RUNNING 时有意义）= 抢占回收 5 分钟预警，立即触发重调度流水线。

> 为什么必须回读 StoppedMode：节省停机有前置条件（VPC、无本地盘等），条件不满足时 API **不报错、静默退化为普通停机**，只看 StopInstance 返回成功会被骗。

---

## 3. 调度规则（核心 · 无轮班）

**没有班次概念**。系统目标只有一条：永远恰好一台实例在线。

### 3.1 触发条件（三者走同一条重调度流水线）

| 触发 | 来源 |
|---|---|
| 当班实例被抢占回收（LOCK_RECYCLING 预警或 NOT_FOUND） | 60s 巡检 / EventBridge webhook（可选加速） |
| 当班账号 runtime ≥ Tmax（默认 360h，环境变量） | reconcile 中发现 |
| 当班账号 CDT 流量 ≥ 阈值（默认 188GB） | 15-30min 周期查询 |

### 3.2 目标选择

```
eligible = runtime < Tmax  AND  CDT流量 < 阈值（最近一次快照）  AND  账号健康（非欠费/非长期 NoStock）
target   = argmin runtime_seconds over eligible
eligible 为空 → 全局熔断流水线（全线节省停机）
```

- **优先级：熔断 > 流量阈值 / Tmax（硬门）> 均衡**。硬门撞线即移出 eligible。
- **均衡是自然结果**：每次选时间最短者，各账号 runtime 自然收敛，无需 deficit 公式、无需排班。
- **被回收的账号可以是自己的目标**：若它 runtime 最短且有余量，argmin 结果就是它自己 → 直接拉起，无需特判。
- **新账号中途加入**：runtime 计数器初始化为现有账号均值（虚拟起点），防止独吞。
- **账号不可用期**（欠费/NoStock）不累积 runtime，恢复后自然优先被选中，属预期补偿。
- Tmax 是均衡手段；流量阈值（188GB / 200GB 额度）才是真实资源约束——现实中通常流量先撞线。

### 3.3 流量守卫（双层）

1. **拉起前预检**：进程启动时先刷新全部账号 CDT；T1 选目标时，候选账号必须有可用快照且低于阈值。查询失败或尚无快照时视为“未知”，不得按 0GB 放行。
2. **运行中周期守卫**：每 15-30min 刷新全部账号；当班账号撞阈值后触发重调度。

流量查询走 **CDT OpenAPI `ListCdtInternetTraffic`**（账号维度公网累积用量，非 ECS API；RAM 需授权 `cdt:ListCdtInternetTraffic`）。数据为小时级延迟的累积值，188GB 阈值中的 12GB 缓冲即为延迟买保险。

---

## 4. 系统全局状态

| 全局态 | 定义（判定式） | 说明 |
|---|---|---|
| `STEADY(X)` | 账号 X 的实例 RUNNING 且服务探测健康；**其余所有实例均为 STOPPED_SC** | 唯一稳态。全网恰好一台在线 |
| `TRANSITION(from→to, step)` | 流水线执行中，step ∈ T1..T5 | 单飞；允许短暂双在线（先启后停），但必须收敛 |
| `BREAKER` | 全局熔断：**所有实例 STOPPED_SC 已确认** | 零在线是合法态；等月初复位或手动 /resume |
| `DEGRADED_MULTI` | 非 TRANSITION 期间检测到 >1 台 RUNNING | 不变量 I1 被破坏，自动纠偏 |
| `DEGRADED_ZERO` | 非 BREAKER 期间检测到 0 台 RUNNING | 在线实例丢失且拉起失败，自动纠偏 |
| `UNKNOWN` | 进程启动后首轮 reconcile 完成前 | 只读不动作 |

### state.json 结构（崩溃恢复用，一切可重建）

```json
{
  "global_state": "STEADY",
  "duty_account": "A",
  "transition": null,
  "accounts": {
    "A": {"traffic_gb": 102.4, "quota_exceeded": false, "last_observed": "RUNNING"},
    "B": {"traffic_gb": 188.6, "quota_exceeded": true,  "last_observed": "STOPPED_SC"}
  },
  "updated_at": "..."
}
```

`transition` 非空时形如 `{"from":"A","to":"B","step":"T3","started_at":"...","attempts":{...}}` —— **step 游标持久化**，崩溃重启后从该步幂等续跑。

---

## 5. 不变量（每次 reconcile 校验）

- **I1 单机在线**：非 TRANSITION、非 BREAKER 期间，全网恰好 1 台 RUNNING。违反 → DEGRADED_MULTI / DEGRADED_ZERO。
- **I2 停机必须是节省停机**：任何非在线实例必须是 `STOPPED_SC`；出现 `STOPPED_KM` → 立即重发 StopCharging 停机 + 告警（计费和流量双重泄漏点）。
- **I3 标签契约**：在线实例 `duty=on`，其余 `duty=off`（OOS 看门狗的选择器），漂移即修正。
- **I4 单飞**：state.json 存在未完成 transition 时，普通新事件只更新"意图"。手动熔断和切换目标超限可在同一个控制锁内替换当前流水线，不并发执行；标签重试不能阻塞保护动作。
- **I4a 不确定即冻结**：任一实例观测为 `UNKNOWN` 时，常规巡检和快速控制循环均暂停云侧写入，包括已有流水线；恢复观测后继续。手动熔断请求仍先持久化，不丢失保护意图。
- **I4b 进程互斥**：初始化数据库和云客户端之前，对配置、数据库、状态文件的规范路径分别获取本机排他文件锁。任一冲突直接退出；锁覆盖整个运行周期，正常退出或崩溃均由内核释放。不是跨主机分布式锁。

API 解析采用严格结构：ECS 只有明确的空 `Instances.Instance` 列表才表示实例不存在；异常结构视为查询失败。CDT 只接受顶层或 `Data` 内的 `TrafficDetails` 列表，每项必须有地域和有限非负流量（数字字符串也支持）；明确空列表可计 0，缺字段或格式异常不能覆盖最后有效快照。

---

## 6. TRANSITION 流水线（T1→T5，每步幂等、有核实门槛）

| 步 | 动作 | 核实门槛（过了才进下一步） | 超时/失败处理 |
|---|---|---|---|
| **T1 选定目标** | 按 §3.2：eligible 中 argmin runtime；**拉起前用最近 CDT 快照做流量预检** | 目标账号两项硬门均有余量 | eligible 为空 → 转**熔断流水线** |
| **T2 拉起目标** | 快速控制循环调用 StartInstance | 回读 Status=Running | NoStock → 退避 1/2/4/8min（封顶）重试；总超时 10min → 告警，转 DEGRADED_ZERO 纠偏 |
| **T3 服务探测** | TCP 连接目标 EIP:服务端口，默认每 5s 一次 | **连续 3 次成功** | 从进入 T3 起计时；超时 4min → 回滚：停掉新实例（回 STOPPED_SC），留在旧机，告警 |
| **T4 旧机节省停机** ★ | 每次停机请求前重新查询目标 Running/EIP 并探测服务端口，再提交 StopCharging | 旧机实时回读 **Status=Stopped 且 StoppedMode=StopCharging**，不信任旧 stop_done | 目标不可用则保留原节点并退回 T3；未知态冻结。旧机未确认停机则按实例节流重试并告警；明确 NOT_FOUND 视为满足 |
| **T5 收尾** | 复核停机状态 → 逐台写 duty 标签 → 全量回读 → 落盘 → 发通知 | 目标 Running/EIP/端口可用且标签 on，其余节省停机且标签 off；不信任旧 tag_done | 标签失败退避重试；停机漂移优先退回 T4，即使标签仍失败。最终状态和标签再次漂移则不宣布完成 |

**熔断流水线**（eligible 为空 / /breaker 触发）：对全部实例执行 T4 同款"节省停机 + 确认"→ **全部确认 STOPPED_SC 后**才发全局保护报告（TG 使用中文状态描述，不展示内部状态码）→ 置 BREAKER。任何一台确认不了 → 该台进入持续重试升级，报告延迟到确认后发出。

**回滚语义**：T2/T3 失败可安全回滚（新机停掉、旧机保持在线）；只有 T3 连续探测成功才会进入 T4 停旧机。

**保护优先级**：`/breaker confirm` 使用独立紧急通道，普通意图队列满或 T5 重试均不会阻塞接收。T1～T5 期间继续检查目标的已确认流量和累计时长；目标超限则安全转移到有余量节点，无可用承接者则保护停机。多机在线且所有运行节点已确认超限时同样处理，不再反复选择超限胜者；用量未知不等于已超限。

以上检查缩小了观测与操作之间的窗口，但云端查询、TCP 探测、停机请求并非原子事务，无法保证目标在检查通过后绝不被回收。

---

## 7. 事件表

| 事件 | 来源 | 载荷 |
|---|---|---|
| EV_TICK | 每 60s reconcile | — |
| EV_TMAX | reconcile 中发现当班 runtime ≥ Tmax | 账号 |
| EV_TRAFFIC | 每 15-30min CDT 查询，发现当班超阈值 | 各账号用量 |
| EV_SPOT_WARN | 巡检发现 LockReason=Recycling / webhook 回调 | 账号 |
| EV_SERVICE_DOWN | 在线服务端口连续 3 次探测失败（跨 3 个 tick） | 账号 |
| EV_TG_CMD | TG 命令入队 | switch / breaker / resume / check 等 |
| EV_MONTH_RESET | cron 每月 1 号 00:05（清零 runtime 计数器 + 熔断复位） | — |

> 三个调度触发（EV_TMAX / EV_TRAFFIC 超限 / EV_SPOT_WARN）收敛为同一条重调度流水线，选目标逻辑统一为 §3.2。

---

## 8. 状态 × 事件 转换表

| 当前态 | 事件 | 动作 | 下一态 |
|---|---|---|---|
| STEADY(X) | EV_TMAX（X 达上限） | 开流水线重调度（§3.2 选目标）；eligible 空 → 熔断流水线 | TRANSITION(X→Y) / TRANSITION(→BREAKER) |
| STEADY(X) | EV_TRAFFIC：X 超限 | 同上 | TRANSITION |
| STEADY(X) | EV_SPOT_WARN / 实例 NOT_FOUND | 立即开 TRANSITION（先启后停，不等旧机死）；目标可以是 X 自己 | TRANSITION(X→Y) |
| STEADY(X) | EV_SERVICE_DOWN | **只告警不自动切换**（VM/EIP 健康，属服务层问题）；预留 `AUTO_SWITCH_ON_SERVICE_DOWN=false` | STEADY(X) |
| TRANSITION | 任意新事件 | 记为意图，当前流水线跑完后由 reconcile 重新推导 | TRANSITION |
| TRANSITION | step 全部通过 | T5 落盘 + 通知 | STEADY(Y) |
| TRANSITION | T2 超时 | 告警，旧机仍是入口 | DEGRADED_ZERO 纠偏 / STEADY(X) |
| TRANSITION | T4 确认失败 | 持续重发停机 + 告警升级 | DEGRADED_MULTI |
| BREAKER | EV_MONTH_RESET | 额度与计数器重置，按 §3.2 选目标自动拉起 | TRANSITION(→target) |
| BREAKER | /resume | 校验余量后拉起；无余量拒绝并回复 | TRANSITION / BREAKER |
| DEGRADED_MULTI | EV_TICK | 优先选有余量在线胜者；所有在线节点确认超限则转可用备用节点或熔断；用量未知则告警等待 | STEADY / TRANSITION(→BREAKER) / 保持等待 |
| DEGRADED_ZERO | EV_TICK | 按 §3.2 选目标退避拉起（NoStock 退避），周期告警 | 收敛后 TRANSITION/STEADY |
| 任意 | /breaker | 熔断流水线 | TRANSITION(→BREAKER) |

---

## 9. 探测体系（三层）

| 层 | 内容 | 频率 | 用途 |
|---|---|---|---|
| 云状态探测 | DescribeInstances：Status / StoppedMode / LockReason；EIP 绑定状态；账号欠费异常识别 | 60s | reconcile 输入；I1/I2 校验；抢占 5min 预警 |
| 服务健康探测 | TCP 连接在线实例 EIP:服务端口 | TRANSITION 默认 5s；STEADY 60s | T3 连续 3 次成功门槛；STEADY 连续 3 败告警 |
| 流量探测 | CDT OpenAPI `ListCdtInternetTraffic`（账号维度） | 15-30min（数据本身小时级延迟） | T1 预检 + 运行中阈值守卫 + 每日汇总 |

---

## 10. 持久化（SQLite）

单文件 `rotator.db`（WAL 模式），三张表：

| 表 | 内容 | 增长量 |
|---|---|---|
| `runtime_counters` | 每账号每月一行：`month`, `runtime_seconds` | 默认保留 24 个月 |
| `events` | 调度/保护/抢占/恢复/告警流水（`/last` 数据源） | 默认保留 180 天 |
| `traffic_snapshots` | CDT 用量快照（趋势、预警） | 默认保留 180 天 |

默认两账号、每 20 分钟采样约 5.3 万条/年，容量远小于 30GB；每日维护任务删除超过保留期的数据，SQLite 复用空闲页。最新流量按每账号最大自增 id 聚合，并有 `(account, id)` 索引，避免历史增长导致查询平方退化。

**时长记账用 tick 累计，不做事件配对**：每 60s reconcile 时，凡回读确认 `Status=Running` 的实例，其账号 `runtime_seconds += 60`。崩溃最多丢一个 tick；无配对逻辑，永不错账。计数器每月 1 号清零（与 CDT 额度重置周期对齐）。

---

## 11. 超时与重试参数（全部环境变量可调）

| 参数 | 默认 | 说明 |
|---|---|---|
| PATROL_INTERVAL | 60s | reconcile 周期 |
| TRAFFIC_INTERVAL | 20min | CDT 查询周期 |
| TRAFFIC_THRESHOLD_GB | 188 | 流量保护阈值（对 200GB 非内地额度） |
| TMAX_HOURS | 360 | 每账号每月运行时长上限（≈ 24h×30/N 参考值） |
| START_TIMEOUT | 10min | T2 拉起总超时 |
| NOSTOCK_BACKOFF | 1/2/4/8min 封顶 | NoStock 退避 |
| probe_interval_sec | 5s | T3 两次 TCP 探测之间的最小间隔 |
| probe_required_success | 3 | T3 连续成功门槛，未满足前不进入停机步骤 |
| PROBE_TIMEOUT | 4min | T3 服务探测总超时，超时则回滚 |
| STOP_TIMEOUT | 10min | T4 停机确认超时 |
| STOP_RETRY_INTERVAL | 5min | 确认失败后重发停机周期 |
| STOP_ALARM_INTERVAL | 30min | 告警升级重复周期 |
| ALERT_REPEAT_INTERVAL | 30min | 同节点同类普通告警最短重复间隔 |
| TRAFFIC_STALE | 1h | 流量快照超过此时间或跨月后不得参与选机 |
| TRAFFIC_RETENTION_DAYS | 180d | 流量快照保留期 |
| EVENT_RETENTION_DAYS | 180d | 事件保留期 |
| RUNTIME_RETENTION_MONTHS | 24 | 月运行时长保留期 |
| REPORT_CRON | 58 23 * * * | 每日汇总（兼心跳） |
| RESET_CRON | 5 0 1 * * | 月初清零计数器 + 熔断复位 |

---

## 12. 与 OOS 看门狗的标签契约

- T5 写入：在线实例 `duty=on`，其余 `duty=off`；每次 reconcile 校验 I3。
- OOS 每小时拉起 `duty=on` 的实例（官方模板 `ACS-ECS-ScheduleToStartInstances`，每账号一套，免费）。
- 脚本死亡时：OOS 保住在线实例的保活底线（最坏 1 小时拉回）；脚本恢复后首轮 reconcile 重新对齐，幂等无冲突。
- 标签默认 `duty=off`，只有明确在线才置 `on`——防止脚本死在窗口期时 OOS 复活本该停机的实例。

---

## 13. 崩溃恢复语义

进程任意时刻被杀 → systemd 拉起 → 首轮 reconcile：

1. 读 state.json；若 `transition` 非空 → 按 step 游标幂等续跑（重复执行已完成步骤无副作用）；
2. 若运行中 state.json 损坏/暂时不可读 → 使用内存中的最后有效状态；冷启动时确实缺失才从云侧状态 + SQLite 计数器 + CDT 用量重建；
3. UNKNOWN 期间两个控制循环均冻结云侧写入；已有流水线保留，恢复观测后继续幂等推进。
4. 已保存的 BREAKER 或 `breaker_latched` 优先于当前在线数量；OOS/人工拉起不会解除保护，而会触发重新停机。只有有效 `/resume` 或月初恢复流程可解除保护意图。
5. `stop_done`、`tag_done` 仅记录进度，不是永久证明；恢复后按新观测重算，T5 完成前再次全量核验。

---

## 14. 通知与 TG 交互

- **六类通知**：抢占恢复 / 超限停机 / 全局保护 / 切换异常 / 临时调度 / 调度完成。开始通知在请求进入状态机后发出；完成通知只在目标服务和旧机节省停机状态**回读确认后**发出。
- **展示约束**：本地日志保留状态码、步骤号和字段名用于诊断；TG 只显示中文状态、实例友好名、触发原因和下一步动作，并用 emoji、分隔线和分段提高可读性。`/last` 会对旧数据库事件做兼容转换，不再展示 `obs/from/to/reason`、`T1~T5` 等内部表达。
- **每日汇总**：23:58 推送系统状态、当前节点、切换进度、各节点状态、流量、运行时长与额度预警。仅在 `billing.enabled: true` 时刷新并附加各账户本月税前费用。账单请求与控制锁独立；失败显示当前金额未知，同月旧结果明确标注旧数据和查询时间。
- **长期运行保护**：流量快照跨月或超过 `traffic_stale_sec` 即不再参与选机；流量/事件/运行时长历史每日 00:30 按配置保留期清理；TG 待发队列和控制意图队列均有硬上限，同类异常默认 30 分钟只通知一次。
- **TG 双向**：setMyCommands 注册菜单 + getUpdates 长轮询（无需公网入口）+ chat_id 白名单 + 命令入队（TG 线程绝不直接调云 API）。
- **名称语义**：`accounts` 下的键（如 `A`）是状态机、`/switch` 和 `/bill` 使用的稳定账号键；`instance_id` 是 ECS 资源 ID；TG 优先显示可选的 `instance_name`，未配置时自动采用 DescribeInstances 返回的 ECS InstanceName，并附 `[账号键]` 防止重名。
- **命令**：`/check`（使用最近快照生成运行状态汇总，与每日任务共用状态正文，不查询账单）`/traffic`（入队后在下一次巡检实时调用 CDT API）`/bill`（独立后台查询各账户本月费用，指定账号键按产品展开）`/switch`（手动调度，走完整流水线）`/breaker`（二次确认）`/resume` `/last`（最近 10 条事件）。
- **内联按钮**：当前不实现。已有命令菜单足够完成全部操作，也更便于保留明确的二次确认；若以后增加，先只为“状态 / 流量 / 最近事件”等只读功能提供按钮，切换和全局保护仍需二次确认。
