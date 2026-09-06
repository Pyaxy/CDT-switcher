# 账单查询配置指南

账单功能用于查询阿里云国际站账户的月度税前费用。主程序通过 Telegram 提供本月账单查询，并在每日运行报告中附加各账户费用；`billing_query.py` 提供独立终端入口，用于授权验证、金额核对和历史月份查询。

查询接口为 `QueryBillOverview`，统计范围为 AccessKey 所属账户，而非 `accounts` 中配置的单台 ECS。账单金额和查询结果不参与流量阈值判断、目标节点选择、实例启停或全局保护停机。

## 1. 配置字段

在 `config.yaml` 顶层添加 `billing`，与 `accounts`、`telegram`、`thresholds` 同级：

```yaml
billing:
  enabled: true
```

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `billing.enabled` | YAML 布尔值 | `false` | 启用 Telegram 账单命令和每日费用汇总；修改后需重启主程序。 |
| `accounts.<账号键>.instance_name` | 字符串，可选 | 空 | 账户在通知和账单中的友好名称，例如 `香港节点 [A]`。 |

省略 `billing`、省略 `enabled` 或设置 `enabled: false` 时，主程序不发起账单请求，机器人菜单和帮助不显示 `/bill`，每日运行报告保留原有内容。字符串 `"true"`、`"false"` 及数字不属于该开关接受的类型。

当前 `billing` 仅支持 `enabled` 字段。缓存复用窗口固定为 300 秒，每日报告时间固定为 23:58（UTC+8）；不支持通过该配置节调整时间、账单接入点或历史月份保留数量。

账单功能复用 `accounts` 中已有的 `access_key_id`、`access_key_secret`，无需单独配置凭据或安装额外依赖。所有已配置账户均纳入 `/bill` 和每日费用汇总，不提供逐账户启用开关。

`instance_name` 不改变账号键。命令参数使用 `accounts` 下的键，例如 `/bill A`；未配置友好名称时，主程序采用已观测到的 ECS `InstanceName`，尚未取得名称时回退到账号键。

## 2. RAM 授权

为每个需要查询的 RAM 用户增加以下自定义只读策略，保留原有 ECS/CDT 权限：

```json
{
  "Version": "1",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["bss:DescribeBillList"],
      "Resource": "*"
    }
  ]
}
```

该接口对应的 RAM Action 为 `bss:DescribeBillList`，与接口名 `QueryBillOverview` 不同。查询采用账户级授权，策略中的 `Resource` 使用 `*`，不能替换为 ECS 实例 ARN。此功能不要求支付、购买或实例账单明细权限。参考 [QueryBillOverview 官方文档](https://www.alibabacloud.com/help/en/user-center/developer-reference/api-bssopenapi-2017-12-14-querybilloverview)。

使用 RAM 用户凭据，不使用主账户 AccessKey。真实凭据仅保存在受保护的配置文件中，不写入命令参数或版本库。

## 3. 启用与验证

### 3.1 验证单账户查询

完成 RAM 授权后，在项目目录使用已有虚拟环境执行：

```bash
.venv/bin/python billing_query.py --config config.yaml --account A
```

将 `A` 替换为实际账号键，在费用控制台选择相同账户和月份，核对产品分类及税前金额。

独立终端入口不受 `billing.enabled` 控制，可在主程序账单功能关闭时执行。它不启动状态机、Telegram 轮询或调度器，不操作 ECS/CDT，也不读写运行状态、数据库、运行锁或账单缓存，可与已有控制器并行运行。

### 3.2 应用配置

设置 `billing.enabled: true` 后重启主程序。按默认部署方式安装的 systemd 服务，可由 root 执行：

```bash
systemctl restart cdt-switcher
systemctl status cdt-switcher --no-pager
journalctl -u cdt-switcher -n 50 --no-pager
```

普通登录用户需为上述命令添加 `sudo`。仅修改 `config.yaml` 不需要执行 `systemctl daemon-reload`。启动时会根据开关注册命令菜单；账单查询在收到命令或执行每日任务时触发。

### 3.3 验证 Telegram 入口

在 `telegram.chat_ids` 配置的授权聊天中执行：

| 入口 | 查询范围 | 输出 |
| --- | --- | --- |
| `/bill` | 全部配置账户，UTC+8 当前月份 | 按账户、币种和账单类型分别列示税前金额小计。 |
| `/bill A` | 账号键 `A` 对应账户，UTC+8 当前月份 | 在各小计下按产品展开费用。 |
| 每日运行报告 | 全部配置账户，UTC+8 当前月份 | 每日 23:58 刷新并附加账户费用，保留状态、流量、时长和额度提醒。 |

Telegram 入口仅支持本月查询，不接受月份或强制刷新参数。历史月份查询使用独立终端入口。

命令立即返回查询提示，结果沿用现有通知机制，发送至全部已配置的授权聊天，而非仅回复命令发起者。账户显示为友好名称及账号键；长报告自动分段发送。

## 4. 查询、缓存与失败语义

### 4.1 后台执行与请求合并

手动查询由独立后台线程执行，不占用 Telegram 轮询线程或状态机控制锁。最多运行一个手动查询工作线程；相同请求合并，不同账户及产品明细请求串行完成，待处理查询类型最多为配置账户数加一。

每日查询在调度器线程执行，与手动查询串行访问账单接口。单账户请求失败不会中断其他账户查询，账单异常不会取消每日运行状态报告。

### 4.2 缓存生命周期

| 场景 | 行为 |
| --- | --- |
| 手动查询距该账户上次查询完成不足 300 秒 | 复用最近结果，包括成功结果或失败状态。 |
| 手动查询超过复用窗口 | 重新请求该账户账单。 |
| 每日运行报告 | 主动刷新，不受手动查询的 300 秒窗口限制。 |
| 每日查询等待已执行中的查询完成 | 复用等待期间完成的账户结果，避免重复请求。 |
| 跨月后的下一次查询 | 清除上月缓存，按新月份查询。 |
| 进程重启 | 内存缓存清空，下一次查询重新请求接口。 |

`/bill` 与 `/bill 账号键` 共用账户缓存。缓存仅包含当月最近一次格式化产品汇总、小计、成功查询时间和最近失败状态；不保留原始响应，不写入 SQLite 或其他持久化文件。

### 4.3 结果状态

| 状态 | 显示方式 |
| --- | --- |
| 查询成功 | 显示汇总金额及查询时间。 |
| 复用成功缓存 | 标注“缓存数据”并显示原查询时间。 |
| 查询失败，存在同月成功记录 | 标注当前金额未知；保留上次结果，并显示“旧数据”和上次成功查询时间。 |
| 查询失败，无同月成功记录 | 仅显示失败原因和最近尝试时间，不生成金额。 |
| 接口成功返回空列表 | 显示“暂无账单条目”，不解释为未产生费用。 |

失败不计为 0 元，旧数据不作为本次刷新成功的结果。权限不足、网络超时和响应解析错误均不触发停机、切换或熔断。原始 SDK 异常及响应不直接输出，避免泄露请求中的敏感信息。

## 5. 独立终端入口

在项目目录执行，使用已安装 `requirements.txt` 依赖的虚拟环境：

```bash
# 全部配置账户，UTC+8 当前月份
.venv/bin/python billing_query.py --config config.yaml

# 指定账户，当前月份
.venv/bin/python billing_query.py --config config.yaml --account A

# 指定账户及历史月份
.venv/bin/python billing_query.py --config config.yaml --account A --month 2026-08

# 指定配置文件的绝对路径
.venv/bin/python billing_query.py --config /path/to/config.yaml --account A

# 仅显示参数，不发起请求
.venv/bin/python billing_query.py --help
```

终端入口仅从 YAML 读取账户配置，不读取 `.env` 或额外覆盖凭据。每次执行均重新查询，不复用主程序缓存。

请求按账号顺序执行，每个账号一次；连接超时为 5 秒，读取超时为 10 秒，不启用自动重试。上述超时作用于网络阶段，不构成整个命令的严格总时限。单账户失败后继续处理后续账户，`Ctrl+C` 可取消执行。

| 退出码 | 含义 |
| --- | --- |
| `0` | 全部账户查询成功，包括接口明确返回空列表。 |
| `1` | 至少一个账户查询失败。 |
| `2` | 参数或配置错误。 |
| `130` | 用户取消执行。 |

## 6. 金额口径与适用范围

- 金额取自 `PretaxAmount`，按币种、账单类型和产品汇总，不等于现金实付、账户余额或税后应付金额。
- 预付费、按量付费、退款和调账分别列示；保留接口金额的符号和小数，不对退款自行取负，不跨类型推算净应付。
- 不跨账户计算总额。多个配置键使用同一阿里云账户凭据时，会重复显示同一账户账单，不代表产生了多笔费用。
- 当月账单存在数据延迟，金额尚未最终确认；查询时间不代表计费数据截止时间。具体时效以费用控制台和[官方账单时效说明](https://www.alibabacloud.com/help/en/user-center/developer-reference/api-bssopenapi-2017-12-14-describeinstancebill)为准。
- 接口固定使用国际站接入点 `business.ap-southeast-1.aliyuncs.com`，不随 ECS 地域变化，不用于中国站账户。参考[服务接入点文档](https://www.alibabacloud.com/help/en/user-center/developer-reference/api-bssopenapi-2017-12-14-endpoint)。

## 7. 常见问题

| 现象 | 检查项或处理方式 |
| --- | --- |
| `/bill` 未出现在菜单，或被识别为未知命令 | 检查服务实际加载的配置文件，确认顶层 `billing.enabled` 为布尔值 `true`，并已重启服务；检查命令菜单注册日志。 |
| 配置加载提示 `billing.enabled` 类型错误 | 使用不带引号的 `true` 或 `false`，不使用字符串、数字或空值。 |
| 提示账号键无效 | 使用 `accounts` 下的实际键，不使用 `instance_name` 或 ECS 实例 ID；可通过 `/check` 核对节点名称。 |
| 返回权限错误 | 检查对应 AccessKey 所属 RAM 用户是否具有 `bss:DescribeBillList` 的账户级只读权限，并用终端入口单独验证。 |
| 短时间修改权限后仍显示之前的失败 | 手动查询可能复用五分钟内的失败结果；等待复用窗口结束，或使用独立终端入口立即验证。 |
| 报告显示旧数据 | 本轮刷新失败；按上次成功查询时间判断数据时效，排查失败原因后重新查询。 |
| 金额与单台 ECS 的估算费用不一致 | 本功能查询账户级账单，应按相同账户、月份、币种和账单类型核对费用控制台。 |

更新代码时需同时保留 `cdt_switcher.py` 和 `billing_query.py`。通用更新与服务管理步骤见[部署教程](deploy.md)。
