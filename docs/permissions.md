# 控制台准备与最小权限

本文说明阿里云资源准备、RAM 用户创建、运行时授权及 Telegram 配置。每个阿里云账户分别执行对应步骤。

## 1. 操作身份

| 身份 | 用途 | 授权范围 |
| --- | --- | --- |
| 账户管理员 | 创建资源、开通 CDT、创建 RAM 用户和策略 | 使用已有管理身份完成一次性配置。 |
| 控制器 RAM 用户 | 在 VPS 上调用 ECS/CDT 和可选账单接口 | 使用下文列出的动作及资源范围，仅启用程序访问。 |
| OOS 执行角色（可选） | 在云端执行定时启动 | 使用独立角色，见 [OOS 配置](oos-setup.md)。 |
| Linux 服务用户 | 运行本地 Python 服务和访问状态文件 | 本地文件权限，与 RAM 用户无关。 |

控制器 RAM 用户不需要控制台登录、RAM 管理、资源购买或 OOS 管理权限。本文给出的策略面向程序调用，不用于提供完整的 ECS 控制台浏览能力。

## 2. ECS 与 CDT 资源准备

### ECS

在 ECS 控制台选择实例所在地域，记录以下信息：

| 控制台信息 | 配置字段或用途 |
| --- | --- |
| 地域 ID | `accounts.<key>.region`，例如 `cn-hongkong`。 |
| 实例 ID | `accounts.<key>.instance_id`。 |
| 已绑定 EIP 或稳定公网 IP | `accounts.<key>.eip`。 |
| 业务的公网 TCP 端口 | `accounts.<key>.service_port`。 |
| 账户 ID | RAM 策略 ARN 中的 `ACCOUNT_ID`，使用资源所属阿里云账户 ID。 |

实例应支持 `StopInstance` 的 `StoppedMode=StopCharging`。在维护窗口通过 ECS 控制台验证节省停机和重新启动，并确认业务服务可自动恢复。节省停机会释放计算资源，再次启动受库存影响；系统盘、数据盘和 EIP 等可能继续计费。适用条件和资源计费见 [StopInstance 文档](https://www.alibabacloud.com/help/en/ecs/developer-reference/api-ecs-2014-05-26-stopinstance)。

在安全组及实例防火墙中，允许控制机访问配置的 TCP 探测端口。业务客户端所需的其他访问规则由业务部署决定。程序通过 ECS 返回的信息核对公网地址，不调用 EIP 分配、绑定或修改接口。

程序只管理配置中的现有实例。若实例已释放，需由管理员准备替代实例并更新实例 ID、授权 ARN 和地址配置。

### CDT

在每个账户的 CDT 控制台开通服务，并确认使用的按流量计费公网资源已纳入 CDT。资源支持范围和升级方式见 [CDT 支持的产品](https://www.alibabacloud.com/help/en/cdt/product-overview/supported-services-and-billing-methods)。

核对该账户实际享有的非中国内地额度及当月累计用量，再设置 `traffic_threshold_gb`。默认 `188` 是程序的流量保护阈值，不是对账户免费额度的确认。其他同账户资源产生的非内地流量也可能计入查询结果。

## 3. 创建控制器 RAM 用户

使用账户管理员进入 RAM 控制台：

1. 进入 **身份管理 / Identities → 用户 / Users → 创建用户 / Create User**。
2. 设置用户名称，例如 `cdt-switcher`。
3. 选择程序访问方式（控制台可能显示为 **Permanent AccessKey**），不启用控制台登录。
4. 创建并安全保存 AccessKey ID、AccessKey Secret。密钥用于本账户的 `accounts` 配置。

若用户创建流程未生成 AccessKey，可在该用户详情页创建。AccessKey Secret 仅在创建时展示。参考[创建 RAM 用户](https://www.alibabacloud.com/help/en/ram/user-guide/create-a-ram-user)及[创建 AccessKey](https://www.alibabacloud.com/help/en/ram/user-guide/create-an-accesskey-pair)。

## 4. 控制器基础权限

当前控制器调用以下接口：

| Action | 用途 | Resource |
| --- | --- | --- |
| `ecs:DescribeInstances` | 查询实例状态、公网地址、回收标志及标签；请求指定实例 ID。 | 受管实例 ARN。 |
| `ecs:StartInstance` | 启动目标实例。 | 受管实例 ARN。 |
| `ecs:StopInstance` | 提交节省停机请求。 | 受管实例 ARN。 |
| `ecs:TagResources` | 为当班实例设置 `duty=on`。 | 受管实例 ARN。 |
| `ecs:UntagResources` | 删除非当班实例的 `duty` 标签。 | 受管实例 ARN。 |
| `cdt:ListCdtInternetTraffic` | 查询账户当月公网流量。 | `*`，账户级查询。 |

ECS 操作的实例授权范围见 [DescribeInstances](https://www.alibabacloud.com/help/en/ecs/developer-reference/api-ecs-2014-05-26-describeinstances)、[ECS Action 与资源类型](https://www.alibabacloud.com/help/en/ram/api-elastic-compute-service)、[TagResources](https://www.alibabacloud.com/help/en/ecs/developer-reference/api-ecs-2014-05-26-tagresources) 和 [UntagResources](https://www.alibabacloud.com/help/en/ecs/developer-reference/api-ecs-2014-05-26-untagresources)。CDT 自定义策略按本程序调用收窄为一个 List 动作，资源范围沿用[官方 CDT 只读策略](https://www.alibabacloud.com/help/en/ram/developer-reference/aliyuncdtreadonlyaccess)的 `*`。

在 RAM 控制台进入 **权限管理 / Permissions → 权限策略 / Policies → 创建权限策略 / Create Policy**，选择脚本编辑或 JSON 编辑模式，创建名为 `CDTSwitcherRuntime` 的自定义策略：

```json
{
  "Version": "1",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "ecs:DescribeInstances",
        "ecs:StartInstance",
        "ecs:StopInstance",
        "ecs:TagResources",
        "ecs:UntagResources"
      ],
      "Resource": "acs:ecs:REGION_ID:ACCOUNT_ID:instance/INSTANCE_ID"
    },
    {
      "Effect": "Allow",
      "Action": ["cdt:ListCdtInternetTraffic"],
      "Resource": "*"
    }
  ]
}
```

替换 `REGION_ID`、`ACCOUNT_ID`、`INSTANCE_ID`；`ACCOUNT_ID` 是资源所属账户 ID，不是 RAM 用户 ID 或 AccessKey ID。每个账户使用其自身的资源 ARN。同一策略确需覆盖多个受管实例时，将 `Resource` 改为这些实例 ARN 的数组。

回到用户列表，选择控制器用户并执行 **添加权限 / Attach Policy**，在账户范围内附加该自定义策略。账户范围授权仍受策略中列出的实例 ARN 限制。控制台策略创建与绑定步骤见[自定义策略](https://www.alibabacloud.com/help/en/ram/create-a-custom-policy)和[用户授权](https://www.alibabacloud.com/help/en/ram/user-guide/grant-permissions-to-the-ram-user)。

这套权限覆盖基础控制流程，包括未启用 OOS 时的标签维护。无需附加 `AliyunECSFullAccess`、`AliyunCDTFullAccess` 或 `AdministratorAccess`。如果用户同时附加了其他策略，应一并核对其权限；新增窄范围策略不会抵消已有的宽范围授权。

标签动作限制在指定实例上，未进一步限定标签键。程序仅操作 `duty`，但该 RAM 用户在这些实例上的标签权限范围大于单个键；这一点应纳入密钥权限评估。

## 5. 可选账单权限

仅在启用 `billing.enabled` 或需要执行独立账单工具时，额外创建并附加 `CDTSwitcherBillingRead` 策略：

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

`QueryBillOverview` 对应的 RAM Action 为 `bss:DescribeBillList`，授权范围是整个账户的账单，不能限制为某台 ECS 的费用。依据见[账单接口授权表](https://www.alibabacloud.com/help/en/user-center/developer-reference/api-bssopenapi-2017-12-14-querybilloverview)。具体开关、查询方式和验证步骤见[账单配置指南](billing.md)。

## 6. Telegram Bot 与聊天授权

1. 在 Telegram 打开官方 [@BotFather](https://t.me/BotFather)，执行 `/newbot` 创建机器人并保存 Token。参见[官方 Bot 创建教程](https://core.telegram.org/bots/tutorial)。
2. 打开机器人私聊，发送 `/start`。私聊适合单人管理；若使用群组，将机器人加入目标群组并发送 `/start@机器人用户名`。
3. 在控制器启动前获取聊天 ID。安装依赖后，可在 VPS 上使用下列只读脚本；Token 通过隐藏输入读取，不写入 shell 历史。

```bash
cd /opt/cdt-switcher
runuser -u cdt-switcher -- .venv/bin/python - <<'PY'
import getpass
import requests

token = getpass.getpass('Telegram Bot Token: ')
try:
    response = requests.get(
        f'https://api.telegram.org/bot{token}/getUpdates',
        params={'timeout': 0}, timeout=15,
    )
    if response.status_code != 200:
        raise RuntimeError('请求失败，请检查 Token、网络及是否存在其他轮询或 Webhook')
    payload = response.json()
    if payload.get('ok') is not True:
        raise RuntimeError('Telegram 未返回成功结果')
    chats = {}
    for update in payload.get('result', []):
        chat = (update.get('message') or {}).get('chat') or {}
        if 'id' in chat:
            chats[chat['id']] = chat.get('type')
    print(chats or '未取得消息，请向机器人发送 /start 后重试')
except Exception:
    print('未取得聊天 ID；请检查配置及 Telegram 连通性。')
PY
```

`getUpdates` 与 Webhook 不能同时使用，同一 Bot 也应由单个控制器轮询。接口行为见 [Telegram Bot API](https://core.telegram.org/bots/api#getupdates)。

将 Token 写入 `telegram.bot_token`，将目标聊天的数字 ID 写入 `telegram.chat_ids`；群组 ID 可能为负数。`chat_ids` 是聊天级授权，程序不逐个检查群成员身份。授权聊天中可发送命令的成员能够触发切换或保护停机；所有授权聊天都会收到报告和通知。

完成后继续[控制机部署](deploy.md)。
