# 独立只读账单查询

`billing_query.py` 是手动执行一次即退出的终端入口，目前尚未接入主程序、Telegram 或每日汇总。它仅调用 `QueryBillOverview`，查看凭据所属账户的月账单，并非只查看配置中那台 ECS 的费用。

## 隔离边界

- 不导入或启动 `cdt_switcher.py`，不启动状态机、调度器、Webhook 或 TG 轮询。
- 不调用 ECS/CDT 接口，不启停实例，不改标签。
- 不读写 `state.json`、SQLite 数据库或运行锁，不保存账单缓存。
- 只读取指定 YAML 的 `accounts` 配置；已有主程序配置可以直接复用。
- 可以与 VPS 上的主程序同时运行，无需停止 systemd 服务。不要为了测试账单再启动一份主程序。

首次上线前仍需人工验证 API 的账户适用性和金额口径；提供此入口不代表已完成真实账户验证。

## RAM 权限

为每个需要查询的 RAM 用户新增以下自定义只读策略，不要替换原有 ECS/CDT 权限，也不要使用主账户 AccessKey：

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

这里的 RAM Action 与接口名不同。该查询使用账户级授权，不能用 ECS 实例 ARN 代替 `*`。尚不需要实例明细权限 `bssapi:DescribeInstanceBill`，也不需要支付或购买权限。权限依据：[QueryBillOverview 官方文档](https://www.alibabacloud.com/help/en/user-center/developer-reference/api-bssopenapi-2017-12-14-querybilloverview)。

## 终端使用

在仓库根目录执行，使用已安装 `requirements.txt` 依赖的虚拟环境。现有 uv 创建的 `.venv` 可以直接复用，无需新增依赖或激活环境。

```bash
# 所有配置账户，当前月份（UTC+8）
.venv/bin/python billing_query.py --config config.yaml

# 先只验证一个账户：A 替换为 accounts 中的实际账号键
.venv/bin/python billing_query.py --config config.yaml --account A

# 查询指定月份；替换为要核对的账期
.venv/bin/python billing_query.py --config config.yaml --account A --month 2026-08

# 配置保存在其他目录时，可显式指定路径，无需复制生产状态文件
.venv/bin/python billing_query.py --config /path/to/config.yaml --account A
```

未设置账单权限时，请先完成授权再执行。只想查看参数可运行 `--help`，不会发起请求。不要把凭据写到命令行，也不要提交真实配置。此入口仅使用 YAML 内的 AccessKey，不额外读取 `.env` 或覆盖凭据。

请求按账号顺序执行，每个账号一次；连接超时 5 秒、读取超时 10 秒，不启用自动重试。这些是网络阶段超时，不是整个命令的严格总时限。单个账号失败会继续查询后续账号；Ctrl+C 可取消。

退出码：`0` 表示全部查询成功（包括明确返回空列表），`1` 表示至少一个账号查询失败，`2` 表示参数或配置错误，`130` 表示手动取消。权限、网络或响应解析失败不会显示成 0 元；为避免泄露凭据，不打印 SDK 原始异常或响应。

## 如何核对结果

1. 选择同一账户、同一月份，在费用控制台核对产品分类和税前金额。
2. 输出取 `PretaxAmount`，按币种、账单类型和产品汇总；不是现金实付、余额或税后应付金额。
3. 预付费、按量付费、退款、调账分别显示。金额保留接口原有符号和小数，不自行对退款取负、不跨类型推算净应付。
4. 不跨账户计算总额。如果多个配置键实际属于同一阿里云账户，会重复显示该账户账单，不代表多笔消费。
5. 空列表显示“暂无账单条目”，不表示没有产生费用。查询时间也不是账单数据截止时间。

当月费用不是最终账单。官方实例账单文档说明存在约 24 小时数据延迟、未结算按量费用可能未包含，并在次月 3 日 12:00 后确认最终月账单；不可把本工具作为实时费用保护或停机依据。参见[账单数据时效说明](https://www.alibabacloud.com/help/en/user-center/developer-reference/api-bssopenapi-2017-12-14-describeinstancebill)。

接口固定使用国际站账单接入点 `business.ap-southeast-1.aliyuncs.com`，不跟随 ECS 所在地域拼接地址，不能据此查询中国站账户。参考[服务接入点](https://www.alibabacloud.com/help/en/user-center/developer-reference/api-bssopenapi-2017-12-14-endpoint)。

## 后续集成

先人工核对真实账单与控制台。确认无误后再接入独立后台任务、有限缓存和 TG `/bill` 命令；账单请求或权限故障不得影响状态机。当前版本没有对正在运行的主程序做任何功能改动。
