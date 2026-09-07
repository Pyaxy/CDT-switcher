# OOS 定时恢复（可选）

OOS 可在控制器之外定时启动带有 `duty=on` 标签的实例，用于恢复已停止的当班节点。它不检测控制器是否故障，也不检查 CDT 额度、账单或业务端口。

本指南使用阿里云公用模板 `ACS-ECS-ScheduleToStartInstances`，每个账户、每个受管地域分别配置。模板参数和权限以[官方模板文档](https://www.alibabacloud.com/help/en/oos/user-guide/acs-ecs-scheduletostartinstances)为准。

## 1. 当班标签

控制器为当班实例设置 `duty=on`，并删除其他实例的 `duty` 标签。OOS 按 `duty=on` 筛选目标。

配置定时任务前，先让控制器完成一次正常运行检查，在 ECS 控制台确认只有预期当班实例带有 `duty=on`。该标签应专用于本实例组。

## 2. 创建执行角色

使用已有 RAM/OOS 管理员在 RAM 控制台进入 **身份管理 → 角色 → 创建角色**，选择阿里云服务作为可信实体，服务选择 OOS，角色名称例如 `cdt-switcher-oos`。

角色的信任策略为：

```json
{
  "Version": "1",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "sts:AssumeRole",
      "Principal": {
        "Service": ["oos.aliyuncs.com"]
      }
    }
  ]
}
```

该策略允许 OOS 扮演角色，资源操作权限需另行附加。信任关系说明见 [OOS 角色授权说明](https://www.alibabacloud.com/help/en/oos/support/faq)。

### 执行角色权限

公用模板声明 `ecs:DescribeInstances`、`ecs:StartInstance` 和 `oos:GetApplicationGroup`。以下策略按动作列出权限，并将实例启动限制为受管实例：

```json
{
  "Version": "1",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["ecs:DescribeInstances"],
      "Resource": "acs:ecs:REGION_ID:ACCOUNT_ID:instance/*"
    },
    {
      "Effect": "Allow",
      "Action": ["ecs:StartInstance"],
      "Resource": "acs:ecs:REGION_ID:ACCOUNT_ID:instance/INSTANCE_ID"
    },
    {
      "Effect": "Allow",
      "Action": ["oos:GetApplicationGroup"],
      "Resource": "acs:oos:*:ACCOUNT_ID:application/*/applicationgroup/*"
    }
  ]
}
```

替换账户、地域和实例占位符，创建自定义权限策略并附加至执行角色。ECS 查询用于按标签发现目标，因此读取范围是该账户的受管地域；启动只允许指定实例。`oos:GetApplicationGroup` 是公用模板声明的目标选择相关只读权限，使用其支持的账户内应用分组 ARN 范围。资源类型见 [ECS 授权表](https://www.alibabacloud.com/help/en/ram/api-elastic-compute-service)及 [GetApplicationGroup 授权表](https://www.alibabacloud.com/help/en/oos/developer-reference/api-oos-2019-06-01-getapplicationgroup)。

角色不需要 ECS 全量管理、停止实例、修改标签、CDT 或账单权限。OOS 执行角色与控制器 RAM 用户分别授权。

### 控制台操作身份与 PassRole

创建和管理定时执行使用管理员的 OOS 管理身份。向 OOS 传入执行角色时，操作身份还需能够传递该角色；可将以下策略附加给实际操作的 RAM 用户：

```json
{
  "Version": "1",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "ram:PassRole",
      "Resource": "acs:ram::ACCOUNT_ID:role/cdt-switcher-oos",
      "Condition": {
        "StringEquals": {
          "acs:Service": "oos.aliyuncs.com"
        }
      }
    }
  ]
}
```

该策略仅允许向 OOS 传递指定角色，不提供创建或管理 OOS 执行的权限。使用组织中已有的 OOS 管理身份完成配置；这些控制台管理权限不附加到控制器 RAM 用户或执行角色。说明见 [OOS 访问控制](https://www.alibabacloud.com/help/doc-detail/122529.html)。

## 3. 创建定时执行

在 OOS 控制台选择受管实例所在地域：

1. 进入 **自动化任务 → 公共模板**，查找 `ACS-ECS-ScheduleToStartInstances` 并创建执行。
2. 配置下表参数，确认目标选择使用标签条件。
3. 检查执行角色和目标范围，提交定时执行。

| 参数 | 设置 |
| --- | --- |
| `regionId` | 受管实例所在地域。 |
| `targets` | 按实例标签选择，标签键 `duty`、值 `on`。 |
| `cron` | 使用控制台向导设置每小时执行。 |
| `timeZone` | `Asia/Shanghai`。 |
| `endDate` | 选择适合运维周期的结束时间，到期前续建或更新任务。 |
| `rateControl` | 并发数 `1`，最大错误数 `0`，便于逐项观察执行失败。 |
| `OOSAssumeRole` | `cdt-switcher-oos`。 |

目标使用运行时的标签筛选，不把首次选中的实例列表保存为固定目标。任务启用后，通过执行详情检查下一次触发时间、目标实例和启动结果。菜单名称可能因控制台语言变化，以模板名和参数名定位。

## 4. 验证与维护

先检查一次定时执行的目标范围，确认没有选中备用节点或实例组之外的资源。

如需验证实际启动，在维护窗口暂停控制器，确认当班标签后将该实例停止，再观察下一次定时触发及启动结果。该操作会中断业务，验证完成后恢复控制器并检查 `/check`。首次验证也应检查角色权限错误、库存不足及任务结束时间。

停用 OOS 时，在控制台取消或终止对应的定时执行，并确认没有等待触发的任务。仅停止 VPS 上的控制器不会停止 OOS 定时任务。

## 5. 运行边界

- OOS 与控制器独立运行，看到 `duty=on` 时即可尝试启动，不受本地 `state.json` 或账单状态约束。
- 切换和全局保护过程中，实例启停与标签更新并非原子操作。OOS 可能在标签更新前选中原节点；控制器恢复观测后会按当前意图处理。
- 全局保护完成并移除当班标签后，不应再有符合条件的 OOS 目标。标签写入失败时需处理告警，并检查 OOS 是否仍能选中实例。
- 实例已释放、库存不足、账户欠费或 API 故障时，启动可能失败；定时频率不代表恢复时间保证。
- OOS 不验证业务健康，不重建已释放实例。模板执行及被启动资源的费用以对应产品的控制台和计费规则为准。
