# CDT-switcher —— 单文件组装件（开头部分）
# 单文件守护进程，按功能区块组织。
# 全部 import 集中在本文件（Section 0），后续 Section 不得再 import。

# ===== Section 0: 常量 / 异常 / 日志 =====
from __future__ import annotations

import copy, fcntl, json, logging, math, os, queue, re, signal, socket, sqlite3, sys, threading, time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests, yaml
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from alibabacloud_tea_openapi.client import Client as OpenApiClient
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_tea_util import models as util_models
try:
    from Tea.exceptions import TeaException
except Exception:  # Tea 为传递依赖，缺失时退化为通用异常，保证可导入
    TeaException = Exception

LOGGER = logging.getLogger("cdt_switcher")
try:
    APP_TZ = ZoneInfo("Asia/Shanghai")
except ZoneInfoNotFoundError:  # 极简系统未安装 tzdata 时仍按中国标准时间运行
    APP_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")

# —— 实例观测态（由 Status + StoppedMode 派生）——
OBS_RUNNING = "RUNNING"
OBS_STARTING = "STARTING"
OBS_STOPPING = "STOPPING"
OBS_STOPPED_SC = "STOPPED_SC"      # 节省停机（已确认，合法终态）
OBS_STOPPED_KM = "STOPPED_KM"      # 普通停机（未生效节省停机，计费泄漏）
OBS_NOT_FOUND = "NOT_FOUND"
OBS_UNKNOWN = "UNKNOWN"            # API/网络/权限/新状态导致无法确认，绝不能等同 NOT_FOUND

# —— 全局态 ——
GS_STEADY = "STEADY"
GS_TRANSITION = "TRANSITION"
GS_BREAKER = "BREAKER"
GS_MULTI = "DEGRADED_MULTI"
GS_ZERO = "DEGRADED_ZERO"
GS_UNKNOWN = "UNKNOWN"

# —— 流水步骤游标 ——
STEP_T1 = "T1"
STEP_T2 = "T2"
STEP_T3 = "T3"
STEP_T4 = "T4"  # 停旧机并确认节省停机
STEP_T5 = "T5"  # 写标签并收尾
TRANSITION_VERSION = 2  # v1 含 DNS T4；v2 已移除 DNS，并将后续步骤前移

# OOS 看门狗标签键
DUTY_TAG_KEY = "duty"

_STATE_UI = {
    GS_STEADY: ("🟢", "运行稳定"),
    GS_TRANSITION: ("🔄", "正在切换节点"),
    GS_BREAKER: ("🛑", "全局保护停机"),
    GS_MULTI: ("🟠", "检测到多台在线，正在收敛"),
    GS_ZERO: ("🔴", "当前没有在线节点"),
    GS_UNKNOWN: ("⚪️", "状态确认中"),
}

_OBS_UI = {
    OBS_RUNNING: ("🟢", "运行中"),
    OBS_STARTING: ("🟡", "启动中"),
    OBS_STOPPING: ("🟡", "停止中"),
    OBS_STOPPED_SC: ("⚪️", "节省停机"),
    OBS_STOPPED_KM: ("🟠", "普通停机，可能仍在计费"),
    OBS_NOT_FOUND: ("🔴", "实例不存在或已被回收"),
    OBS_UNKNOWN: ("⚪️", "暂时无法确认"),
}

_STEP_UI = {
    STEP_T1: "检查目标节点资格",
    STEP_T2: "启动目标节点",
    STEP_T3: "确认目标服务可用",
    STEP_T4: "停止原节点并确认",
    STEP_T5: "更新当班标记并收尾",
}

_REASON_UI = {
    "manual": "手动切换",
    "traffic": "本月流量达到保护阈值",
    "tmax": "本月运行时长达到上限",
    "spot_warn": "收到抢占或回收预警",
    "service_down": "服务连续探测失败",
    "month_reset": "月度额度重置后恢复",
    "resume": "手动恢复服务",
    "no_eligible": "没有符合条件的可用节点",
    "cleanup_multi": "检测到多台实例同时在线",
    "degraded_zero": "当前没有在线节点",
    "rollback": "切换失败后恢复原节点",
    "manual_breaker": "手动启动全局保护停机",
}

_NOTIFY_UI = {
    "抢占恢复": ("♻️", "抢占恢复"),
    "超限停机": ("📊", "额度保护切换"),
    "全局熔断": ("🛑", "全局保护"),
    "切换异常": ("⚠️", "需要注意"),
    "临时调度": ("🔄", "节点调度"),
    "调度完成": ("✅", "切换完成"),
}


class SwitcherError(Exception):
    """所有可预期业务异常的基类。"""


class NoStockError(SwitcherError):
    """拉起实例时库存不足（可退避重试）。"""


class ArrearsError(SwitcherError):
    """账号欠费（账号移出 eligible）。"""


class CloudAPIError(SwitcherError):
    """其它云 API / 外部服务错误。"""


def now_iso() -> str:
    """上海时区 ISO 字符串（精确到秒，保持无时区后缀以兼容旧数据库）。"""
    return datetime.now(APP_TZ).replace(tzinfo=None).isoformat(timespec="seconds")


def setup_logging(level: str = "INFO") -> None:
    """配置全局 LOGGER。"""
    logging.basicConfig(
        level=getattr(logging, str(level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # 5 秒快速控制任务会让 APScheduler 在 INFO 下每轮打印两行，淹没 T1~T5 业务日志。
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


# ===== Section 1: 配置 =====

@dataclass
class AccountCfg:
    name: str                                      # 状态机内部账号键（如 A/B），需保持稳定
    region: str
    access_key_id: str
    access_key_secret: str
    instance_id: str
    eip: str
    service_port: int
    instance_name: str = ""                       # TG 友好显示名；空则采用 ECS InstanceName
    ecs_endpoint: str = ""                          # 空则默认 f"ecs.{region}.aliyuncs.com"
    cdt_endpoint: str = "cdt.aliyuncs.com"


@dataclass
class TGCfg:
    bot_token: str
    chat_ids: list[int] = field(default_factory=list)


@dataclass
class Thresholds:
    patrol_interval_sec: int = 60
    traffic_interval_min: int = 20
    traffic_threshold_gb: float = 188.0
    tmax_hours: float = 360.0
    start_timeout_sec: int = 600
    nostock_backoff_sec: list[int] = field(default_factory=lambda: [60, 120, 240, 480])  # 封顶循环
    probe_interval_sec: int = 5
    probe_required_success: int = 3
    probe_timeout_sec: int = 240
    stop_timeout_sec: int = 600
    stop_retry_interval_sec: int = 300
    stop_alarm_interval_sec: int = 1800
    alert_repeat_interval_sec: int = 1800
    traffic_stale_sec: int = 3600
    traffic_retention_days: int = 180
    event_retention_days: int = 180
    runtime_retention_months: int = 24
    auto_switch_on_service_down: bool = False
    enable_eventbridge_webhook: bool = False
    webhook_listen: str = "127.0.0.1:8787"
    db_path: str = "rotator.db"
    state_path: str = "state.json"


@dataclass
class Config:
    accounts: dict[str, AccountCfg]
    tg: TGCfg
    th: Thresholds


# 环境变量 → Thresholds 字段（存在即覆盖，类型转换失败则告警忽略）
_ENV_TYPE_MAP = {
    "PATROL_INTERVAL": ("patrol_interval_sec", int),
    "TRAFFIC_INTERVAL": ("traffic_interval_min", int),
    "TRAFFIC_THRESHOLD_GB": ("traffic_threshold_gb", float),
    "TMAX_HOURS": ("tmax_hours", float),
    "START_TIMEOUT": ("start_timeout_sec", int),
    "PROBE_INTERVAL": ("probe_interval_sec", int),
    "PROBE_REQUIRED_SUCCESS": ("probe_required_success", int),
    "PROBE_TIMEOUT": ("probe_timeout_sec", int),
    "STOP_TIMEOUT": ("stop_timeout_sec", int),
    "STOP_RETRY_INTERVAL": ("stop_retry_interval_sec", int),
    "STOP_ALARM_INTERVAL": ("stop_alarm_interval_sec", int),
    "ALERT_REPEAT_INTERVAL": ("alert_repeat_interval_sec", int),
    "TRAFFIC_STALE": ("traffic_stale_sec", int),
    "TRAFFIC_RETENTION_DAYS": ("traffic_retention_days", int),
    "EVENT_RETENTION_DAYS": ("event_retention_days", int),
    "RUNTIME_RETENTION_MONTHS": ("runtime_retention_months", int),
    "AUTO_SWITCH_ON_SERVICE_DOWN": ("auto_switch_on_service_down", bool),
    "ENABLE_EVENTBRIDGE_WEBHOOK": ("enable_eventbridge_webhook", bool),
    "DB_PATH": ("db_path", str),
    "STATE_PATH": ("state_path", str),
}

_BOOL_TRUE = ("1", "true", "yes", "on")
_BOOL_FALSE = ("0", "false", "no", "off")
_REMOVED_THRESHOLD_KEYS = {"dns_retry", "dns_retry_interval_sec"}


def _apply_env_overrides(th: Thresholds) -> None:
    """用环境变量覆盖 thresholds 中对应字段。"""
    for env_key, (attr, typ) in _ENV_TYPE_MAP.items():
        raw = os.environ.get(env_key)
        if not raw:
            continue
        try:
            if typ is bool:
                low = raw.strip().lower()
                if low in _BOOL_TRUE:
                    val = True
                elif low in _BOOL_FALSE:
                    val = False
                else:
                    LOGGER.warning("环境变量 %s 布尔值无法识别: %r，已忽略", env_key, raw)
                    continue
            else:
                val = typ(raw)
        except (ValueError, TypeError):
            LOGGER.warning("环境变量 %s 类型转换失败: %r，已忽略", env_key, raw)
            continue
        setattr(th, attr, val)


def _validate_config(accounts: dict[str, AccountCfg], th: Thresholds) -> None:
    """启动前拒绝会让调度器失效或导致错误控制目标的配置。"""
    if not accounts:
        raise ValueError("配置中未定义任何账号（accounts 为空）")
    instance_ids: set[str] = set()
    for name, ac in accounts.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("accounts 的账号键必须是非空字符串")
        if name != name.strip():
            raise ValueError(f"账号键 {name!r} 不能包含首尾空格")
        if not 1 <= ac.service_port <= 65535:
            raise ValueError(f"账号 {name} 的 service_port 必须在 1..65535")
        for field_name in ("region", "access_key_id", "access_key_secret", "instance_id", "eip"):
            if not str(getattr(ac, field_name, "") or "").strip():
                raise ValueError(f"账号 {name} 的 {field_name} 不能为空")
        if ac.instance_id in instance_ids:
            raise ValueError(f"instance_id 重复: {ac.instance_id}")
        instance_ids.add(ac.instance_id)

    positive_ints = (
        "patrol_interval_sec", "traffic_interval_min", "start_timeout_sec",
        "probe_interval_sec", "probe_required_success", "probe_timeout_sec",
        "stop_timeout_sec", "stop_retry_interval_sec", "stop_alarm_interval_sec",
        "alert_repeat_interval_sec", "traffic_stale_sec", "traffic_retention_days",
        "event_retention_days", "runtime_retention_months",
    )
    for field_name in positive_ints:
        if getattr(th, field_name) <= 0:
            raise ValueError(f"thresholds.{field_name} 必须大于 0")
    if (not math.isfinite(th.traffic_threshold_gb)
            or not math.isfinite(th.tmax_hours)
            or th.traffic_threshold_gb <= 0 or th.tmax_hours <= 0):
        raise ValueError("流量阈值和运行时长上限必须大于 0")
    if not isinstance(th.nostock_backoff_sec, list) or not th.nostock_backoff_sec:
        raise ValueError("thresholds.nostock_backoff_sec 必须是非空列表")
    try:
        th.nostock_backoff_sec = [int(v) for v in th.nostock_backoff_sec]
    except (TypeError, ValueError) as exc:
        raise ValueError("thresholds.nostock_backoff_sec 只能包含整数") from exc
    if any(v < 0 for v in th.nostock_backoff_sec):
        raise ValueError("thresholds.nostock_backoff_sec 不能包含负数")
    if os.path.abspath(str(th.db_path)) == os.path.abspath(str(th.state_path)):
        raise ValueError("thresholds.db_path 与 state_path 不能指向同一文件")


def load_config(path: str = "config.yaml") -> Config:
    """从 YAML 加载配置；thresholds 缺省用默认，环境变量二次覆盖。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError as e:
        raise ValueError(f"配置文件不存在: {path}") from e

    # 账号映射
    if not isinstance(data, dict):
        raise ValueError("配置文件根节点必须是对象")
    accounts_raw = data.get("accounts") or {}
    if not isinstance(accounts_raw, dict):
        raise ValueError("accounts 必须是对象映射")
    accounts: dict[str, AccountCfg] = {}
    for name, ac in accounts_raw.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("accounts 的账号键必须是非空字符串")
        ac = ac or {}
        if not isinstance(ac, dict):
            raise ValueError(f"账号 {name} 的配置必须是对象")
        try:
            accounts[name] = AccountCfg(
                name=name,
                region=ac["region"],
                access_key_id=ac["access_key_id"],
                access_key_secret=ac["access_key_secret"],
                instance_id=ac["instance_id"],
                eip=ac["eip"],
                service_port=int(ac["service_port"]),
                instance_name=str(ac.get("instance_name", "") or "").strip(),
                ecs_endpoint=ac.get("ecs_endpoint", ""),
                cdt_endpoint=ac.get("cdt_endpoint", "cdt.aliyuncs.com"),
            )
        except KeyError as e:
            raise ValueError(f"账号 {name} 缺少必填字段: {e}") from e
    if not accounts:
        raise ValueError("配置中未定义任何账号（accounts 为空）")

    # Telegram
    tg_raw = data.get("telegram") or {}
    if not isinstance(tg_raw, dict):
        raise ValueError("telegram 必须是对象")
    try:
        chat_ids = [int(x) for x in tg_raw.get("chat_ids", [])]
    except (ValueError, TypeError) as e:
        raise ValueError(f"telegram.chat_ids 含非整数: {e}") from e
    tg = TGCfg(bot_token=tg_raw.get("bot_token", ""), chat_ids=chat_ids)

    # Thresholds（yaml 覆盖默认，再叠加环境变量）
    th_raw = data.get("thresholds") or {}
    if not isinstance(th_raw, dict):
        raise ValueError("thresholds 必须是对象")
    th = Thresholds()
    for k, v in th_raw.items():
        if k in _REMOVED_THRESHOLD_KEYS:
            continue  # 兼容旧 config.yaml；DNS 模块已移除
        if not hasattr(th, k):
            LOGGER.warning("未知 thresholds 字段: %s，已忽略", k)
            continue
        cur = getattr(th, k)
        try:
            if isinstance(cur, bool):
                low = str(v).strip().lower()
                if low in _BOOL_TRUE:
                    setattr(th, k, True)
                elif low in _BOOL_FALSE:
                    setattr(th, k, False)
                else:
                    LOGGER.warning("thresholds.%s 布尔值无法识别: %r，已忽略", k, v)
            elif isinstance(cur, float):
                setattr(th, k, float(v))
            elif isinstance(cur, int):
                setattr(th, k, int(v))
            else:
                setattr(th, k, v)
        except (ValueError, TypeError):
            LOGGER.warning("thresholds.%s 类型转换失败: %r，已忽略", k, v)
    _apply_env_overrides(th)

    _validate_config(accounts, th)

    return Config(accounts=accounts, tg=tg, th=th)


# ===== Section 2: 阿里云账号客户端 =====

# 欠费相关错误码：精确匹配 + 前缀匹配
_ARREARS_EXACT = {"InvalidAccountStatus.NotEnoughBalance"}
_ARREARS_PREFIX = ("Arrearage", "OverduePayment")

def _tea_code(e: BaseException) -> str:
    return str(getattr(e, "code", "") or "")


def _tea_msg(e: BaseException) -> str:
    return str(getattr(e, "message", "") or "")


def _resp_summary(resp: object) -> object:
    if isinstance(resp, dict):
        return {k: type(v).__name__ for k, v in resp.items()}
    return type(resp).__name__


def _is_mainland_region(region: str) -> bool:
    """CDT 内地流量池判定：cn-* 地域且非香港（与已投产项目 CDT-Monitor 的分类一致）。
    内地池（20GB 免费额度）与非内地池（200GB）分开计量，香港属非内地池。"""
    return region.startswith("cn-") and region != "cn-hongkong"


def _sum_non_mainland(details: list) -> float:
    """对 TrafficDetails 按 BusinessRegionId 分类，只累加非内地池流量（字节）。"""
    total = 0.0
    for d in details:
        if not isinstance(d, dict) or not isinstance(d.get("BusinessRegionId"), str) or not d["BusinessRegionId"].strip():
            raise ValueError("CDT 明细缺少有效地域")
        try:
            if isinstance(d.get("Traffic"), bool):
                raise ValueError("布尔值不是流量")
            value = float(d["Traffic"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("CDT 明细缺少有效 Traffic 数值") from exc
        if not math.isfinite(value) or value < 0:
            raise ValueError("CDT 流量必须是有限非负数")
        if not _is_mainland_region(d["BusinessRegionId"]):
            total += value
    return total


def _parse_traffic_gb(resp: object) -> float:
    """从 ListCdtInternetTraffic 响应解析当月非内地池累积流量（字节→GB）。

    响应结构经已投产项目实战确认：TrafficDetails[].{BusinessRegionId, Traffic}，
    可能嵌在 Data 下；按 BusinessRegionId 分地域。阈值判断针对 200GB 非内地池，
    因此必须分类过滤——内地流量（若有）不得计入，否则虚增消耗、提前误触发换班。
    只接受明确可分类的 TrafficDetails；结构漂移必须报错，不猜测数值或流量池。
    """
    if not isinstance(resp, dict) or not resp:
        raise ValueError("CDT 响应为空或不是对象")
    # 权威路径：TrafficDetails 分类聚合（顶层优先，Data 嵌套兜底）
    for container in (resp, resp.get("Data")):
        if not isinstance(container, dict):
            continue
        td = container.get("TrafficDetails")
        if isinstance(td, list):
            return _sum_non_mainland(td) / (1024 ** 3)
        if "TrafficDetails" in container:
            raise ValueError("CDT TrafficDetails 不是列表")
    raise ValueError("CDT 响应缺少 TrafficDetails 列表，拒绝推测流量")


@dataclass
class InstanceObs:
    exists: Optional[bool] = None
    obs: str = OBS_UNKNOWN                         # OBS_* 之一（由 Status+StoppedMode 派生）
    status: str = ""                               # 原始 Status
    stopped_mode: str = ""                         # 原始 StoppedMode
    lock_recycling: bool = False                   # OperationLocks 含 LockReason=Recycling
    eip_bound: bool = False                        # 期望 EIP 出现在实例上
    duty_on: bool = False                          # 标签 duty=on
    instance_name: str = ""                       # ECS InstanceName（TG 显示名兜底来源）


class AliyunAccountClient:
    """封装单账号的 ECS + CDT 泛化 OpenAPI 调用（不依赖产品 SDK 模型类）。"""

    def __init__(self, cfg: AccountCfg) -> None:
        self.cfg = cfg
        ecs_ep = cfg.ecs_endpoint or f"ecs.{cfg.region}.aliyuncs.com"
        cdt_ep = cfg.cdt_endpoint or "cdt.aliyuncs.com"
        self.ecs_client = self._build(ecs_ep)
        self.cdt_client = self._build(cdt_ep)

    def _build(self, endpoint: str) -> OpenApiClient:
        c = open_api_models.Config()
        c.access_key_id = self.cfg.access_key_id
        c.access_key_secret = self.cfg.access_key_secret
        c.endpoint = endpoint
        return OpenApiClient(c)

    # —— 泛化 OpenAPI 调用（契约第 0 节）——
    def _call(self, client: OpenApiClient, action: str, version: str, query: dict) -> dict:
        params = open_api_models.Params(
            action=action,
            version=version,
            protocol="HTTPS",
            pathname="/",
            method="POST",
            auth_type="AK",
            style="RPC",
            req_body_type="formData",
            body_type="json",
        )
        # RPC 查询参数在 Tea OpenAPI 的签名链路中必须是字符串；整数等值会被
        # urllib.parse.quote 当作 bytes 分支处理，并触发 encoding TypeError。
        rpc_query = {
            str(k): (v if isinstance(v, str) else str(v))
            for k, v in query.items()
            if v is not None
        }
        request = open_api_models.OpenApiRequest(query=rpc_query)
        # Tea RuntimeOptions 的超时单位是毫秒，而不是秒。
        runtime = util_models.RuntimeOptions(read_timeout=10_000, connect_timeout=5_000)
        try:
            resp = client.call_api(params, request, runtime)
        except TeaException as e:
            self._raise_for_code(_tea_code(e), _tea_msg(e))
        except Exception as e:                      # 网络/序列化等未知错误
            raise CloudAPIError(f"{action} 调用异常: {e}") from e
        if not isinstance(resp, dict):
            raise CloudAPIError(f"{action} 返回非对象响应")
        # 通用 OpenAPI 客户端返回 {body, headers, statusCode} 包装；业务字段
        # （Instances、TrafficDetails 等）位于 body 中。
        body = resp.get("body")
        return body if isinstance(body, dict) else resp

    @staticmethod
    def _raise_for_code(code: str, message: str) -> None:
        c = code or ""
        if "NoStock" in c:
            raise NoStockError(f"{c}: {message}")
        if c in _ARREARS_EXACT or c.startswith(_ARREARS_PREFIX):
            raise ArrearsError(f"{c}: {message}")
        raise CloudAPIError(f"{c}: {message}")

    # —— 观测 ——
    def get_instance_obs(self) -> InstanceObs:
        query = {
            "RegionId": self.cfg.region,
            "InstanceIds": json.dumps([self.cfg.instance_id]),
            "PageSize": 10,
        }
        resp = self._call(self.ecs_client, "DescribeInstances", "2014-05-26", query)
        container = resp.get("Instances")
        if not isinstance(container, dict) or not isinstance(container.get("Instance"), list):
            raise CloudAPIError("DescribeInstances 响应缺少有效 Instances.Instance 列表")
        insts = container["Instance"]
        if not insts:
            return InstanceObs(exists=False, obs=OBS_NOT_FOUND)
        if len(insts) != 1 or not isinstance(insts[0], dict) or insts[0].get("InstanceId") != self.cfg.instance_id:
            raise CloudAPIError("DescribeInstances 返回的实例与请求目标不一致")
        inst = insts[0]
        status = str(inst.get("Status", "") or "")
        stopped_mode = str(inst.get("StoppedMode", "") or "")

        if status == "Running":
            obs = OBS_RUNNING
        elif status == "Starting":
            obs = OBS_STARTING
        elif status == "Stopping":
            obs = OBS_STOPPING
        elif status == "Stopped":
            obs = OBS_STOPPED_SC if stopped_mode == "StopCharging" else OBS_STOPPED_KM
        else:
            # 新增/短暂云侧状态不能冒充“实例不存在”；未知态只等待和告警。
            obs = OBS_UNKNOWN

        locks = (inst.get("OperationLocks") or {}).get("OperationLock") or []
        lock_recycling = any(str(lk.get("LockReason", "")) == "Recycling" for lk in locks)
        eip_bound = self._is_eip_bound(inst)
        duty_on = self._duty_tag_on(inst)
        return InstanceObs(
            exists=True,
            obs=obs,
            status=status,
            stopped_mode=stopped_mode,
            lock_recycling=lock_recycling,
            eip_bound=eip_bound,
            duty_on=duty_on,
            instance_name=str(inst.get("InstanceName", "") or "").strip(),
        )

    def _is_eip_bound(self, inst: dict) -> bool:
        eip = self.cfg.eip
        ea = inst.get("EipAddress")
        if isinstance(ea, dict):
            v = ea.get("IpAddress")
            if isinstance(v, str) and v == eip:
                return True
            if isinstance(v, list) and eip in v:
                return True
        elif isinstance(ea, str) and ea == eip:
            return True
        pa = inst.get("PublicIpAddress")
        if isinstance(pa, dict):
            pa = pa.get("IpAddress")
        if isinstance(pa, list) and eip in pa:
            return True
        if isinstance(pa, str) and pa == eip:
            return True
        return False

    @staticmethod
    def _duty_tag_on(inst: dict) -> bool:
        tags = (inst.get("Tags") or {}).get("Tag") or []
        for t in tags:
            key = t.get("TagKey") or t.get("Key")
            val = t.get("TagValue") or t.get("Value")
            if key == DUTY_TAG_KEY and str(val).lower() == "on":
                return True
        return False

    # —— 写操作（全部幂等）——
    def start_instance(self) -> None:
        obs = self.get_instance_obs()
        if obs.obs == OBS_UNKNOWN:
            raise CloudAPIError("实例状态未知，暂不启动")
        if not obs.exists:
            raise CloudAPIError(f"实例 {self.cfg.instance_id} 不存在，无法拉起")
        if obs.obs in (OBS_RUNNING, OBS_STARTING):
            return  # 幂等：已运行/启动中直接返回
        self._call(
            self.ecs_client, "StartInstance", "2014-05-26",
            {"RegionId": self.cfg.region, "InstanceId": self.cfg.instance_id},
        )

    def stop_instance_stopcharging(self) -> None:
        obs = self.get_instance_obs()
        if obs.obs == OBS_UNKNOWN:
            raise CloudAPIError("实例状态未知，暂不停机")
        if obs.obs == OBS_NOT_FOUND:
            return
        if obs.exists and obs.obs == OBS_STOPPED_SC:
            return  # 幂等：已是节省停机
        self._call(
            self.ecs_client, "StopInstance", "2014-05-26",
            {
                "RegionId": self.cfg.region,
                "InstanceId": self.cfg.instance_id,
                "StoppedMode": "StopCharging",
            },
        )

    def set_duty(self, on: bool) -> None:
        if on:
            query = {
                "RegionId": self.cfg.region,
                "ResourceId.1": self.cfg.instance_id,
                "ResourceType": "instance",
                "Tag.1.Key": DUTY_TAG_KEY,
                "Tag.1.Value": "on",
            }
            self._call(self.ecs_client, "TagResources", "2014-05-26", query)
        else:
            query = {
                "RegionId": self.cfg.region,
                "ResourceId.1": self.cfg.instance_id,
                "ResourceType": "instance",
                "TagKey.1": DUTY_TAG_KEY,
            }
            self._call(self.ecs_client, "UntagResources", "2014-05-26", query)

    def get_traffic_gb(self) -> float:
        # CDT ListCdtInternetTraffic（version 2021-08-13，与已投产项目实战用法对齐）：
        # 账号维度当月公网累积用量（字节），按 BusinessRegionId 分地域明细返回。
        # _parse_traffic_gb 只统计非内地池（阈值针对 200GB 非内地免费额度）；
        # 解析失败记日志并抛 CloudAPIError。
        resp = self._call(self.cdt_client, "ListCdtInternetTraffic", "2021-08-13", {})
        try:
            return _parse_traffic_gb(resp)
        except Exception as e:
            LOGGER.error("流量解析失败，响应摘要: %r", _resp_summary(resp))
            raise CloudAPIError(f"ListCdtInternetTraffic 流量解析失败: {e}") from e


# ===== Section 3: 持久化（current_month / StateStore / StateJson） =====
#
# 记账语义（依据 state-machine.md §10 / §13）：
#   - 时长用 tick 累计：每 60s reconcile 时，凡回读 Running 的实例，其账号 runtime_seconds += patrol_interval。
#   - 不做事件配对：不依赖“启动/停止”事件，纯按观测态累加，永不错账。
#   - 崩溃最多丢一个 tick：SQLite 立即落盘，进程被杀只可能漏掉当次未提交的一拍。
#   - 每月 1 号清零：与 CDT 额度重置对齐，reset_month 把当月行归零。

def current_month() -> str:
    """返回当前月份 "YYYY-MM"，用作 runtime_counters 的分区键。"""
    return datetime.now(APP_TZ).strftime("%Y-%m")


class StateStore:
    """SQLite 持久化层：时长计数 / 事件流水 / 流量快照。线程安全。"""

    def __init__(self, db_path: str) -> None:
        # check_same_thread=False：TG 轮询线程与 scheduler 线程并发访问同一连接。
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        # WAL：读写不互斥，崩溃恢复更安全。
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA wal_autocheckpoint=1000")
        self._conn.execute("PRAGMA journal_size_limit=16777216")
        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS runtime_counters ("
                "account TEXT NOT NULL, month TEXT NOT NULL, "
                "runtime_seconds INTEGER NOT NULL DEFAULT 0, "
                "PRIMARY KEY (account, month))"
            )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS events ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "ts TEXT NOT NULL, kind TEXT NOT NULL, message TEXT)"
            )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS traffic_snapshots ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "ts TEXT NOT NULL, account TEXT NOT NULL, gb REAL NOT NULL)"
            )
            # 旧数据库原地迁移；按账号取最大 id，避免历史增长后相关子查询平方退化。
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_traffic_account_id "
                "ON traffic_snapshots(account, id DESC)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts)"
            )
            self._conn.commit()

    def add_runtime(self, account: str, seconds: int) -> None:
        """当月行 UPSERT 累加。tick 累计核心入口，缺行则插入初值。"""
        month = current_month()
        with self._lock:
            self._conn.execute(
                "INSERT INTO runtime_counters (account, month, runtime_seconds) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT (account, month) DO UPDATE SET "
                "runtime_seconds = runtime_counters.runtime_seconds + ?",
                (account, month, seconds, seconds),
            )
            self._conn.commit()

    def get_runtimes(self) -> dict[str, int]:
        """返回当月全部账号的累计时长；调用方对缺失账号按 0 处理。"""
        month = current_month()
        with self._lock:
            rows = self._conn.execute(
                "SELECT account, runtime_seconds FROM runtime_counters WHERE month = ?",
                (month,),
            ).fetchall()
        return {acc: sec for acc, sec in rows}

    def set_runtime(self, account: str, seconds: int) -> None:
        """新账号虚拟起点：用现有均值初始化，防止独吞（state-machine.md §3.2）。"""
        month = current_month()
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO runtime_counters "
                "(account, month, runtime_seconds) VALUES (?, ?, ?)",
                (account, month, seconds),
            )
            self._conn.commit()

    def reset_month(self, accounts: list[str]) -> None:
        """每月 1 号清零：把给定账号的当月行 runtime_seconds 置 0。"""
        month = current_month()
        with self._lock:
            for acc in accounts:
                self._conn.execute(
                    "INSERT OR REPLACE INTO runtime_counters "
                    "(account, month, runtime_seconds) VALUES (?, ?, 0)",
                    (acc, month),
                )
            self._conn.commit()

    def add_traffic(self, account: str, gb: float) -> None:
        """写入一条 CDT 用量快照（趋势 / 阈值守卫 / 每日汇总数据源）。"""
        with self._lock:
            self._conn.execute(
                "INSERT INTO traffic_snapshots (ts, account, gb) VALUES (?, ?, ?)",
                (now_iso(), account, gb),
            )
            self._conn.commit()

    def latest_traffic_details(self) -> dict[str, tuple[float, str]]:
        """每账号按自增 id 取唯一最新快照，返回 gb 与采集时间。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT t.account, t.gb, t.ts FROM traffic_snapshots t "
                "JOIN (SELECT account, MAX(id) AS id FROM traffic_snapshots "
                "GROUP BY account) latest ON latest.id = t.id"
            ).fetchall()
        return {acc: (gb, ts) for acc, gb, ts in rows}

    def latest_traffic(self, max_age_sec: Optional[int] = None) -> dict[str, float]:
        """返回最新流量；指定 max_age_sec 时自动排除过期快照。"""
        details = self.latest_traffic_details()
        if max_age_sec is None:
            return {acc: gb for acc, (gb, _ts) in details.items()}
        now = datetime.now(APP_TZ).replace(tzinfo=None)
        fresh: dict[str, float] = {}
        for acc, (gb, ts) in details.items():
            try:
                age = (now - datetime.fromisoformat(ts).replace(tzinfo=None)).total_seconds()
            except (TypeError, ValueError):
                continue
            # 少量 NTP 漂移可容忍；明显来自未来的数据不得绕过新鲜度守卫。
            if ts[:7] == current_month() and -300 <= age <= max_age_sec:
                fresh[acc] = gb
        return fresh

    def log_event(self, kind: str, message: str) -> None:
        """记录调度 / 熔断 / 抢占 / 恢复 / 告警流水，供 /last 与每日汇总。"""
        with self._lock:
            self._conn.execute(
                "INSERT INTO events (ts, kind, message) VALUES (?, ?, ?)",
                (now_iso(), kind, message),
            )
            self._conn.commit()

    def recent_events(self, n: int = 10) -> list[tuple[str, str, str]]:
        """返回最近 n 条事件 (ts, kind, message)，新→旧。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, kind, message FROM events ORDER BY id DESC LIMIT ?",
                (n,),
            ).fetchall()
        return [(ts, kind, msg) for ts, kind, msg in rows]

    def cleanup_history(self, traffic_days: int, event_days: int,
                        runtime_months: int) -> dict[str, int]:
        """删除过期历史；SQLite 会复用空闲页，避免数据库无限增长。"""
        now = datetime.now(APP_TZ).replace(tzinfo=None)
        traffic_cutoff = (now - timedelta(days=traffic_days)).isoformat(timespec="seconds")
        event_cutoff = (now - timedelta(days=event_days)).isoformat(timespec="seconds")
        month_index = now.year * 12 + now.month - 1 - runtime_months
        runtime_cutoff = f"{month_index // 12:04d}-{month_index % 12 + 1:02d}"
        with self._lock:
            t_cur = self._conn.execute(
                "DELETE FROM traffic_snapshots WHERE ts < ?", (traffic_cutoff,)
            )
            e_cur = self._conn.execute(
                "DELETE FROM events WHERE ts < ?", (event_cutoff,)
            )
            r_cur = self._conn.execute(
                "DELETE FROM runtime_counters WHERE month < ?", (runtime_cutoff,)
            )
            self._conn.commit()
            self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        return {
            "traffic": max(0, t_cur.rowcount),
            "events": max(0, e_cur.rowcount),
            "runtime": max(0, r_cur.rowcount),
        }

    def close(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._conn.close()


class StateJson:
    """流水线游标 + 全局态落盘（崩溃恢复依据，state-machine.md §4 / §13）。"""

    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._last_good: Optional[dict] = None
        self._last_read_warning_at = 0.0

    def load(self) -> dict:
        """读取有效字典；运行中损坏时返回最后一次有效状态，避免误判为空。"""
        with self._lock:
            try:
                with open(self._path, "r", encoding="utf-8") as fh:
                    payload = json.load(fh)
                if not isinstance(payload, dict):
                    raise ValueError("根节点不是对象")
                self._last_good = copy.deepcopy(payload)
                return payload
            except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError) as exc:
                now = time.time()
                if now - self._last_read_warning_at >= 60:
                    fallback = "最后有效状态" if self._last_good is not None else "空状态"
                    LOGGER.warning(
                        "state.json 读取失败（%s），使用%s: %s",
                        type(exc).__name__, fallback, exc,
                    )
                    self._last_read_warning_at = now
                return copy.deepcopy(self._last_good) if self._last_good is not None else {}

    def save(self, data: dict) -> None:
        """原子写：先写临时文件再 os.replace 改名，避免半截文件；自动补 updated_at。"""
        if not isinstance(data, dict):
            raise ValueError("state.json 只能保存字典状态")
        with self._lock:
            payload = copy.deepcopy(data)
            payload.pop("dns", None)  # v1 遗留字段；v2 不再维护 DNS
            payload["updated_at"] = now_iso()
            tmp = f"{self._path}.tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, ensure_ascii=False, indent=2)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self._path)
                parent = str(Path(self._path).resolve().parent)
                dir_fd = os.open(parent, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
                self._last_good = copy.deepcopy(payload)
            except (OSError, TypeError, ValueError) as exc:
                LOGGER.error("state.json 写入失败，已停止本轮状态推进: %s", exc)
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise
# ===== Section 4: Telegram 客户端 TGClient =====

class TGClient:
    # Telegram Bot API 基址（无公网入口也能收命令，靠 getUpdates 长轮询）
    BASE = "https://api.telegram.org"
    RETRY_BASE_SEC = 5.0
    RETRY_MAX_SEC = 300.0
    WARN_INTERVAL_SEC = 60.0
    MAX_QUEUE_SIZE = 1000

    def __init__(self, cfg: TGCfg) -> None:
        self._cfg = cfg
        self._token = cfg.bot_token
        # 白名单 chat_id 集合，非白名单一律忽略
        self._chat_ids = set(cfg.chat_ids)
        # 内存维护的 update offset，避免重复收取同一条消息
        self._offset = 0
        # 退出事件：stop() 置位即让轮询线程退出
        self._stop_event = threading.Event()
        self._poll_thread: Optional[threading.Thread] = None
        # 发送失败不丢消息：按 next_attempt_at 排序的内存重试队列。
        self._send_queue: "queue.PriorityQueue" = queue.PriorityQueue(
            maxsize=self.MAX_QUEUE_SIZE
        )
        self._send_thread: Optional[threading.Thread] = None
        self._send_seq = 0
        self._send_lock = threading.Lock()
        # 同类网络告警做节流，并在下次输出时报告被抑制条数。
        self._warn_lock = threading.Lock()
        self._warn_state: dict[str, tuple[float, int]] = {}

    def _api_url(self, method: str) -> str:
        # 形如 https://api.telegram.org/bot<token>/<method>
        return f"{self.BASE}/bot{self._token}/{method}"

    def _safe_error(self, exc: object) -> str:
        """异常文本可能包含请求 URL；无条件抹掉 bot token。"""
        text = f"{type(exc).__name__}: {exc}"
        return text.replace(self._token, "<redacted>") if self._token else text

    def _warn_throttled(self, key: str, message: str, *args) -> None:
        now = time.time()
        with self._warn_lock:
            last, suppressed = self._warn_state.get(key, (0.0, 0))
            if now - last < self.WARN_INTERVAL_SEC:
                self._warn_state[key] = (last, suppressed + 1)
                return
            self._warn_state[key] = (now, 0)
        rendered = message % args if args else message
        if suppressed:
            rendered += f"（期间抑制 {suppressed} 条同类告警）"
        LOGGER.warning("%s", rendered)

    @staticmethod
    def _retry_after(resp: requests.Response) -> Optional[float]:
        if resp.status_code != 429:
            return None
        try:
            value = (resp.json().get("parameters") or {}).get("retry_after")
            return max(0.0, float(value)) if value is not None else None
        except (TypeError, ValueError, AttributeError):
            return None

    def _retry_delay(self, attempt: int, retry_after: Optional[float] = None) -> float:
        if retry_after is not None:
            return max(retry_after, 1.0)
        return min(self.RETRY_BASE_SEC * (2 ** min(attempt, 6)), self.RETRY_MAX_SEC)

    def _ensure_send_worker(self) -> None:
        with self._send_lock:
            if self._send_thread is not None and self._send_thread.is_alive():
                return
            if self._stop_event.is_set():
                return
            self._send_thread = threading.Thread(target=self._send_loop, daemon=True)
            self._send_thread.start()

    def _enqueue_send(self, cid: int, text: str, attempt: int = 0,
                      delay: float = 0.0) -> bool:
        self._ensure_send_worker()
        with self._send_lock:
            self._send_seq += 1
            seq = self._send_seq
        try:
            self._send_queue.put_nowait((time.time() + delay, seq, cid, text, attempt))
            return True
        except queue.Full:
            self._warn_throttled(
                "send-queue-full",
                "TG 待发队列已达上限 %s，丢弃一条消息以保护进程内存",
                self.MAX_QUEUE_SIZE,
            )
            return False

    def _send_once(self, cid: int, text: str) -> tuple[bool, bool, Optional[float], str]:
        """返回 success, retryable, retry_after, safe_reason。"""
        try:
            resp = requests.post(
                self._api_url("sendMessage"),
                json={"text": text, "parse_mode": "HTML", "chat_id": cid},
                timeout=10,
            )
        except Exception as exc:
            return False, True, None, self._safe_error(exc)
        if resp.ok:
            return True, False, None, ""
        status = int(resp.status_code)
        return False, status == 429 or status >= 500, self._retry_after(resp), f"HTTP {status}"

    def send(self, text: str) -> None:
        # 发送线程与状态机解耦；网络失败进入重试队列，绝不拖死或打断切换。
        if not self._token or not self._chat_ids:
            return
        # 全部通知均为纯文本：统一转义 & < >，防止内容含特殊字符导致 TG 400 静默丢失
        safe = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        for cid in self._chat_ids:
            self._enqueue_send(cid, safe)

    def _send_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                item = self._send_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            due_at, seq, cid, text, attempt = item
            delay = due_at - time.time()
            if delay > 0:
                try:
                    self._send_queue.put_nowait(item)
                except queue.Full:
                    self._warn_throttled(
                        "send-queue-full",
                        "TG 待发队列已达上限 %s，丢弃一条重试消息以保护进程内存",
                        self.MAX_QUEUE_SIZE,
                    )
                self._stop_event.wait(min(delay, 0.5))
                continue
            success, retryable, retry_after, reason = self._send_once(cid, text)
            if success:
                if attempt:
                    LOGGER.info("TG 消息补发成功 chat=%s attempts=%s", cid, attempt + 1)
                continue
            if not retryable:
                self._warn_throttled(
                    f"send-permanent:{cid}",
                    "TG 发送失败且不重试 chat=%s reason=%s", cid, reason,
                )
                continue
            next_attempt = attempt + 1
            wait = self._retry_delay(attempt, retry_after)
            self._warn_throttled(
                f"send-retry:{cid}",
                "TG 发送失败，已排队重试 chat=%s reason=%s retry_in=%.1fs",
                cid, reason, wait,
            )
            self._enqueue_send(cid, text, attempt=next_attempt, delay=wait)

    def register_commands(self) -> None:
        # 注册机器人命令菜单（六条，description 中文）
        if not self._token:
            return
        commands = [
            {"command": "check", "description": "查看系统状态与全部节点详情"},
            {"command": "traffic", "description": "刷新并查看本月流量"},
            {"command": "switch", "description": "安全切换节点，可填写节点键"},
            {"command": "breaker", "description": "让全部节点进入节省停机"},
            {"command": "resume", "description": "从保护停机中恢复服务"},
            {"command": "last", "description": "查看最近 10 条系统事件"},
        ]
        try:
            resp = requests.post(
                self._api_url("setMyCommands"),
                json={"commands": commands},
                timeout=10,
            )
            if resp.ok:
                LOGGER.info("TG 命令菜单注册成功")
            else:
                LOGGER.warning("TG 命令菜单注册失败 status=%s", resp.status_code)
        except Exception as exc:
            LOGGER.warning("TG 命令菜单注册异常: %s", self._safe_error(exc))

    def start_polling(self, on_command: Callable[[str, int], None]) -> None:
        # 启动守护线程做 getUpdates 长轮询；白名单过滤；把 "/cmd args"
        # 归一为 "cmd args" 后回调 on_command。线程内绝不调云 API。
        if not self._token:
            LOGGER.warning("TG bot_token 为空，跳过轮询")
            return
        self._poll_thread = threading.Thread(
            target=self._poll_loop, args=(on_command,), daemon=True
        )
        self._poll_thread.start()

    def _poll_loop(self, on_command: Callable[[str, int], None]) -> None:
        # 长轮询主循环：失败指数退避；429 优先服从 retry_after；同类日志节流。
        failures = 0
        while not self._stop_event.is_set():
            try:
                resp = requests.get(
                    self._api_url("getUpdates"),
                    params={"timeout": 25, "offset": self._offset},
                    timeout=30,
                )
                if not resp.ok:
                    retry_after = self._retry_after(resp)
                    wait = self._retry_delay(failures, retry_after)
                    failures += 1
                    self._warn_throttled(
                        "poll",
                        "TG getUpdates 失败 status=%s retry_in=%.1fs",
                        resp.status_code, wait,
                    )
                    self._stop_event.wait(wait)
                    continue
                if failures:
                    LOGGER.info("TG 轮询连接已恢复")
                failures = 0
                data = resp.json()
                for upd in data.get("result", []):
                    # 推进 offset，确保已处理的 update 不再回放
                    self._offset = upd.get("update_id", self._offset) + 1
                    self._handle_update(upd, on_command)
            except Exception as exc:  # 网络抖动等，退避后重试
                wait = self._retry_delay(failures)
                failures += 1
                self._warn_throttled(
                    "poll", "TG 轮询异常: %s retry_in=%.1fs",
                    self._safe_error(exc), wait,
                )
                self._stop_event.wait(wait)

    def _handle_update(self, upd: dict, on_command: Callable[[str, int], None]) -> None:
        # 只处理白名单 chat 的文本消息，非命令消息忽略。
        msg = upd.get("message")
        if not msg:
            return
        chat = msg.get("chat") or {}
        cid = chat.get("id")
        if cid not in self._chat_ids:
            return  # 非白名单直接丢弃
        text = msg.get("text")
        if not text or not text.startswith("/"):
            return  # 非命令文本忽略
        cmd = self._normalize_cmd(text)
        if cmd:
            # 回调交由状态机层入队，线程内不碰云 API
            on_command(cmd, cid)

    @staticmethod
    def _normalize_cmd(text: str) -> str:
        # "/switch B" -> "switch B"；去斜杠、去 @botname、压空白。
        t = text.strip()
        if not t.startswith("/"):
            return ""
        t = t[1:]  # 去掉前导斜杠
        parts = t.split()
        if not parts:
            return ""
        # 首 token 可能带 @botname（如 switch@mybot）
        cmd_token = parts[0].split("@", 1)[0]
        rest = parts[1:]
        return " ".join([cmd_token] + rest)

    def stop(self) -> None:
        # 置退出事件，轮询与发送线程在当前请求结束后自然退出。
        self._stop_event.set()
# ===== Section 5: Rotator 状态机核心 =====
# 单写者单飞：tick 非阻塞获取锁，拿不到直接返回。
# 一切跨 tick 等待都落在 transition 游标的时间戳上（started_at / deadline /
# next_retry_at / t3_last），tick 内绝不长睡。每推进一扇门先 save state.json 再
# 继续（崩溃可幂等续跑）。云侧真实状态是唯一事实来源。

class Rotator:
    MAX_INTENT_QUEUE_SIZE = 100

    def __init__(self, cfg: Config, store: StateStore, sj: StateJson, tg: TGClient) -> None:
        self.cfg = cfg
        self.store = store
        self.sj = sj
        self.tg = tg
        # 每账号一个阿里云客户端
        self.clients = {name: AliyunAccountClient(ac) for name, ac in cfg.accounts.items()}
        # 友好名优先读取配置；未配置时由首次 DescribeInstances 的 InstanceName 补齐。
        self._instance_names = {
            name: ac.instance_name for name, ac in cfg.accounts.items() if ac.instance_name
        }
        # 意图队列（TG 线程 / webhook / traffic_job 入队，tick 串行消费）
        self.intents: "queue.Queue" = queue.Queue(maxsize=self.MAX_INTENT_QUEUE_SIZE)
        self._pending: list = []          # 已出队的待处理意图
        self._probe_fails: dict = {}      # 账号 -> 连续服务探测失败次数
        self._arrears: set = set()        # 欠费账号（内存态，月重置清空）
        self._cooldown: dict = {}         # 账号/特殊键 -> 退避截止 epoch
        self._notification_last: dict[str, float] = {}  # 重复告警节流
        self._tick_lock = threading.Lock()
        self._traffic_lock = threading.Lock()
        self._breaker_requested = threading.Event()
        self._first_done = False          # 首轮 reconcile 是否完成

    # —— 调度入口（APScheduler 注册）——

    def tick(self) -> None:
        # 单飞：拿到锁才干活，否则直接返回
        if not self._tick_lock.acquire(blocking=False):
            return
        try:
            self._drain_intents()         # 意图入 _pending
            self._consume_traffic_refresh_requests()
            obs = self._observe_all()
            self._account(obs)
            if self._control_safety(obs):
                return
            state = self.sj.load()
            if state.get("transition"):
                # 有流水线游标：推进一扇门并落盘（要求 1）
                self._first_done = True
                ns = self._advance_transition(state["transition"], obs)
                if ns is not None:
                    self.sj.save(ns)
            else:
                # 无流水线：派生态处理（I1-I3 纠偏 / 守卫 / 触发）
                self._derive_and_act(obs)
        except Exception:
            LOGGER.exception("tick 异常")  # 单账号/单 tick 异常不致命
        finally:
            self._tick_lock.release()

    def transition_tick(self) -> None:
        """快速消费控制意图并推进流水线；不做运行时记账或稳态巡检。"""
        state = self.sj.load()
        if not state.get("transition") and self.intents.empty() and not self._pending and not self._breaker_requested.is_set():
            return
        if not self._tick_lock.acquire(blocking=False):
            return
        try:
            self._drain_intents()
            self._consume_traffic_refresh_requests()
            obs = self._observe_all()
            if self._control_safety(obs):
                return
            state = self.sj.load()
            if not state.get("transition"):
                if not self._first_done or not self._pending:
                    return
                self._consume_intents(obs)
                state = self.sj.load()
                if not state.get("transition"):
                    return
            tr = state["transition"]
            LOGGER.info(
                "快速推进流水线 step=%s from=%s to=%s",
                tr.get("step"), tr.get("from"), tr.get("to"),
            )
            ns = self._advance_transition(tr, obs)
            if ns is not None:
                self.sj.save(ns)
        except Exception:
            LOGGER.exception("transition_tick 异常")
        finally:
            self._tick_lock.release()

    def _refresh_traffic(self) -> tuple[dict[str, float], dict[str, str]]:
        """串行刷新全部账号 CDT 快照，返回本轮成功值与错误。"""
        values: dict[str, float] = {}
        errors: dict[str, str] = {}
        with self._traffic_lock:
            for name, cli in self.clients.items():
                try:
                    gb = cli.get_traffic_gb()
                    if not math.isfinite(gb) or gb < 0:
                        raise ValueError(f"CDT 返回非法流量值: {gb!r}")
                    self.store.add_traffic(name, gb)
                    values[name] = gb
                except Exception as e:
                    errors[name] = str(e)
                    LOGGER.warning("流量查询 %s 失败: %s", name, e)
            st = self.sj.load()
            duty = st.get("duty_account")
            traffic = self.store.latest_traffic(self.cfg.th.traffic_stale_sec)
            if duty in traffic and traffic[duty] >= self.cfg.th.traffic_threshold_gb:
                self.submit_intent("TRAFFIC", account=duty)
        if values:
            summary = ", ".join(f"{name}={gb:.1f}GB" for name, gb in values.items())
            LOGGER.info("CDT 流量刷新完成: %s", summary)
        return values, errors

    def traffic_job(self) -> None:
        # 周期 CDT 查询 -> 快照；当班超阈值 -> 入队重调度意图（要求 1/5）
        try:
            self._refresh_traffic()
        except Exception:
            LOGGER.exception("traffic_job 异常")

    def _consume_traffic_refresh_requests(self) -> None:
        """在 tick 单写者上下文处理 TG 实时查询，避免 TG 轮询线程直接调用云 API。"""
        requested = any(it.get("type") == "TRAFFIC_REFRESH" for it in self._pending)
        if not requested:
            return
        self._pending = [it for it in self._pending if it.get("type") != "TRAFFIC_REFRESH"]
        values, errors = self._refresh_traffic()
        self._tg_traffic(values, errors, title="📡 实时流量报告")

    def daily_report(self) -> None:
        # 23:58 汇总（流量 + 时长 + 额度预警），兼心跳（要求：六类通知外每日汇总）
        try:
            self.tg.send(self._summary_text("🕘 每日运行报告"))
            self.store.log_event("daily", "每日汇总已进入发送队列")
        except Exception:
            LOGGER.exception("daily_report 异常")

    def maintenance_job(self) -> None:
        """每日清理历史记录，限制长期数据库增长。"""
        try:
            deleted = self.store.cleanup_history(
                self.cfg.th.traffic_retention_days,
                self.cfg.th.event_retention_days,
                self.cfg.th.runtime_retention_months,
            )
            if any(deleted.values()):
                LOGGER.info("历史数据清理完成: %s", deleted)
        except Exception:
            LOGGER.exception("maintenance_job 异常")

    def month_reset(self) -> None:
        """调度器入口只投递意图，实际重置由状态机单写者执行。"""
        self.submit_intent("MONTH_RESET")

    def _perform_month_reset(self) -> None:
        # 清零计数器；BREAKER 且有 eligible -> 自动拉起（设计 §8 / 契约）
        try:
            st = self.sj.load()
            self.store.reset_month(list(self.cfg.accounts.keys()))
            self._arrears.clear()
            if st.get("global_state") == GS_BREAKER:
                # 上月最后一条累积流量不能用于新月份；先刷新，失败则安全地不拉起。
                self._refresh_traffic()
                to = self._select_target(self.store.get_runtimes())
                if to is not None:
                    self._start_transition(to=to, reason="month_reset")
                    self._notify(
                        "临时调度",
                        "本月额度已经重置。\n"
                        f"准备恢复节点：{self._account_label(to)}",
                    )
                else:
                    self._notify(
                        "切换异常",
                        "本月额度已经重置，但目前没有符合流量、运行时长和账号状态要求的节点可恢复。",
                    )
        except Exception:
            LOGGER.exception("month_reset 异常")

    def handle_tg_command(self, cmd: str, chat_id: int) -> None:
        # 只入队 / 只读查询，绝不直接调云写 API（要求 8/9，TG 线程安全）
        parts = cmd.strip().split()
        c = parts[0].lstrip("/") if parts else ""
        arg = parts[1] if len(parts) > 1 else ""
        if c == "check":
            self._tg_check()
        elif c == "traffic":
            if not self.submit_intent("TRAFFIC_REFRESH"):
                self.tg.send("⚠️ 系统请求较多，请稍后重试。")
                return
            self.tg.send(
                "📡 已收到流量查询请求\n\n"
                "正在从阿里云更新各节点的本月流量，完成后会发送详细报告。"
            )
        elif c == "last":
            self._tg_last()
        elif c == "switch":
            if not self.submit_intent("SWITCH", account=arg or None):
                self.tg.send("⚠️ 系统请求较多，本次切换请求未进入队列，请稍后重试。")
                return
            target = self._account_label(arg) if arg else "由系统自动选择"
            self.tg.send(
                "🔄 已收到切换请求\n\n"
                f"目标节点：{target}\n"
                "系统会先启动并确认新节点可用，再安全停止原节点。"
            )
        elif c == "breaker":
            if arg == "confirm":
                if not self.submit_intent("BREAKER"):
                    self.tg.send("⚠️ 系统请求较多，本次保护停机请求未进入队列，请稍后重试。")
                    return
                self.tg.send(
                    "🛑 已确认全局保护停机\n\n"
                    "系统将依次停止所有实例，并确认它们进入节省停机状态。"
                )
            else:
                self.tg.send(
                    "⚠️ 请确认高风险操作\n\n"
                    "全局保护停机会停止所有实例，服务将暂时不可用。\n"
                    "确认执行请发送：/breaker confirm"
                )
        elif c == "resume":
            if not self.submit_intent("RESUME"):
                self.tg.send("⚠️ 系统请求较多，本次恢复请求未进入队列，请稍后重试。")
                return
            self.tg.send(
                "▶️ 已收到恢复请求\n\n"
                "系统将检查流量和运行额度，并选择符合条件的节点恢复服务。"
            )
        else:
            self.tg.send(
                "❓ 无法识别这条命令\n\n"
                "可用命令：\n"
                "• /check — 查看完整运行报告\n"
                "• /traffic — 刷新本月流量\n"
                "• /switch [账号键] — 切换节点\n"
                "• /breaker confirm — 全部保护停机\n"
                "• /resume — 恢复服务\n"
                "• /last — 查看最近事件"
            )

    # —— 内部：观测与记账 ——

    def _observe_all(self) -> dict:
        # 单账号观测失败只记日志，该账号 obs 视为未知不崩溃（要求 1）
        out = {}
        for name, cli in self.clients.items():
            try:
                observed = cli.get_instance_obs()
                out[name] = observed
                if observed.instance_name and not self.cfg.accounts[name].instance_name:
                    self._instance_names[name] = observed.instance_name
            except Exception as e:
                LOGGER.warning("观测 %s 失败: %s", name, e)
                out[name] = InstanceObs(exists=None, obs=OBS_UNKNOWN)
        return out

    def _account(self, obs: dict) -> None:
        # 记账：每 tick 对 RUNNING 账号累加 patrol_interval_sec（要求 2）
        runtimes = self.store.get_runtimes()
        existing = [a for a in self.cfg.accounts if runtimes.get(a, 0) > 0]
        mean = int(sum(runtimes.values()) / len(runtimes)) if runtimes else 0
        for name in self.cfg.accounts:
            # 新账号（计数器与 state.json 均无）-> 虚拟起点（要求 10）
            if name not in runtimes:
                self.store.set_runtime(name, mean)
        for name, o in obs.items():
            if o.obs == OBS_RUNNING:
                self.store.add_runtime(name, self.cfg.th.patrol_interval_sec)

    def _drain_intents(self) -> None:
        while True:
            try:
                it = self.intents.get_nowait()
            except queue.Empty:
                break
            if it not in self._pending:      # 同参意图去重，防止长流水线期间堆积
                self._pending.append(it)
        if len(self._pending) > 50:          # 积压上限：只保留最新 50 条
            dropped = len(self._pending) - 50
            self._pending = self._pending[-50:]
            LOGGER.warning("意图积压超限，丢弃最旧 %d 条", dropped)

    def submit_intent(self, itype: str, **kwargs) -> bool:
        # 统一意图入口（TG 命令 / webhook / traffic_job 共用）
        if itype == "BREAKER":
            # 独立紧急通道：满队列、标签重试都不能饿死人工保护请求。
            self._breaker_requested.set()
            return True
        try:
            self.intents.put_nowait({"type": itype, **kwargs})
            return True
        except queue.Full:
            LOGGER.warning(
                "意图队列已达上限 %s，拒绝新意图 type=%s",
                self.MAX_INTENT_QUEUE_SIZE, itype,
            )
            return False

    # —— 内部：派生态处理（无 transition 时） ——

    def _freeze_unknown(self, obs: dict) -> bool:
        unknown = [a for a in self.cfg.accounts
                   if a not in obs or obs[a].obs == OBS_UNKNOWN]
        if not unknown:
            return False
        state = self.sj.load()
        state.setdefault("global_state", GS_UNKNOWN)
        self._sync_accounts_obs(state, obs)
        self.sj.save(state)
        self._notify_throttled(
            "observation-unknown", self.cfg.th.alert_repeat_interval_sec, "切换异常",
            "暂时无法确认以下节点状态：\n"
            + "\n".join(f"• {self._account_label(a)}" for a in unknown)
            + "\n系统已暂停自动启停，恢复观测后会继续运行。",
        )
        return True

    def _control_safety(self, obs: dict) -> bool:
        """两个调度入口共用；先持久化紧急意图，未知态冻结，切换中仍检查额度。"""
        if self._breaker_requested.is_set():
            self._start_transition(breaker=True, reason="manual_breaker", replace=True)
            self._breaker_requested.clear()  # 落盘成功后才确认消费
            self._pending = [it for it in self._pending if it.get("type") == "TRAFFIC_REFRESH"]
        if self._freeze_unknown(obs):
            return True
        state = self.sj.load()
        tr = state.get("transition")
        if not tr or tr.get("breaker") or tr.get("step") == STEP_T1:
            # T1 尚未执行云侧动作，由预检直接拒绝不合格目标；不能悄悄改写手动选择。
            return False
        traffic = self.store.latest_traffic(self.cfg.th.traffic_stale_sec)
        runtimes = self.store.get_runtimes()
        target = tr.get("to")
        # 原节点超限但已在安全切换中时保持原流程；目标超限则重新选目标。
        if target and self._quota_exceeded(target, runtimes, traffic):
            candidates = [a for a in self.cfg.accounts
                          if obs[a].obs != OBS_NOT_FOUND and self._eligible(a, runtimes, traffic)]
            if candidates:
                to = min(candidates, key=lambda a: runtimes.get(a, 0))
                self._start_transition(
                    from_=target, to=to, reason="traffic", replace=True,
                    stop_list=[a for a in self.cfg.accounts if a != to],
                )
            else:
                # 已确认目标超限且没有可安全承接的节点，启动保护停机。
                self._start_transition(breaker=True, reason="no_eligible", replace=True)
        return False

    def _quota_exceeded(self, account: str, runtimes: dict, traffic: dict) -> bool:
        return (runtimes.get(account, 0) >= self.cfg.th.tmax_hours * 3600
                or (account in traffic and traffic[account] >= self.cfg.th.traffic_threshold_gb))

    def _derive_and_act(self, obs: dict) -> None:
        if self._freeze_unknown(obs):
            return
        state = self.sj.load()
        # 首轮 reconcile：只读不写云侧，仅本地落盘全局态（要求 7，设计 §13.3）
        if not self._first_done or state.get("global_state") == GS_UNKNOWN:
            ns = self._classify(obs, state)
            self.sj.save(ns)
            self._first_done = True
            LOGGER.info("首轮 reconcile 完成，全局态=%s", ns.get("global_state"))
            return
        # 先消费意图（可能启动流水线）；若已启动则本 tick 收手（I5 单飞）
        self._consume_intents(obs)
        st = self.sj.load()
        if st.get("transition"):
            return
        gs = st.get("global_state", GS_UNKNOWN)
        if gs == GS_BREAKER:
            # BREAKER 唯一合法形态是全员 STOPPED_SC：任何偏差都是计费泄漏。
            # 普通停机（KM）立即重发纠偏 + 告警；仍在运行（如熔断流水线崩溃恢复后遗留）
            # 同样 level-triggered 重发，按 stop_retry_interval 节流、按 stop_alarm_interval 升级告警
            now = time.time()
            retry_at = st.get("breaker_stop_retry_at")
            if not isinstance(retry_at, dict):
                retry_at = {}  # 兼容旧版本保存的全局时间戳
                st["breaker_stop_retry_at"] = retry_at
            for a, ob in obs.items():
                if ob.obs in (OBS_STOPPED_SC, OBS_NOT_FOUND):
                    continue
                if now < retry_at.get(a, 0):
                    continue
                try:
                    self.clients[a].stop_instance_stopcharging()
                except CloudAPIError as e:
                    LOGGER.warning("BREAKER 纠偏停机 %s: %s", a, e)
                retry_at[a] = now + self.cfg.th.stop_retry_interval_sec
                if ob.obs == OBS_STOPPED_KM:
                    self._notify_throttled(
                        f"breaker-stop-mode:{a}",
                        self.cfg.th.alert_repeat_interval_sec,
                        "切换异常",
                        f"发现节点 {self._account_label(a)} 没有进入节省停机。\n"
                        "系统已重新提交节省停机请求。",
                    )
                else:
                    self._notify_throttled(
                        f"breaker-running:{a}",
                        self.cfg.th.stop_alarm_interval_sec,
                        "全局熔断",
                        f"全局保护期间发现节点 {self._account_label(a)} 仍在运行。\n"
                        "系统会继续尝试停止，请留意可能产生的费用。",
                    )
            self._sync_accounts_obs(st, obs)
            self.sj.save(st)
            return
        if gs == GS_MULTI:
            self._handle_degraded_multi(obs, st)
            return
        if gs == GS_ZERO:
            self._handle_degraded_zero(obs, st)
            return
        # STEADY / UNKNOWN 落入：默认按稳态处理
        self._handle_steady(obs, st)

    def _classify(self, obs: dict, state: dict) -> dict:
        # 由云侧真实观测推导全局态（崩溃恢复 / 首轮）
        ns = dict(state)
        duty = state.get("duty_account")
        running = [a for a, o in obs.items() if o.obs == OBS_RUNNING]
        if state.get("transition"):
            ns["global_state"] = GS_TRANSITION
        elif state.get("global_state") == GS_BREAKER or state.get("breaker_latched"):
            ns["global_state"] = GS_BREAKER
            ns["breaker_latched"] = True
            ns["duty_account"] = None
        elif len(running) == 0:
            ns["global_state"] = GS_BREAKER if state.get("global_state") == GS_BREAKER else GS_ZERO
        elif len(running) > 1:
            ns["global_state"] = GS_MULTI
        else:
            ns["global_state"] = GS_STEADY
            if duty not in running:
                ns["duty_account"] = running[0]   # 以观测为准
        self._sync_accounts_obs(ns, obs)
        return ns

    def _consume_intents(self, obs: dict) -> None:
        if self._freeze_unknown(obs):
            return
        if not self._pending:
            return
        state = self.sj.load()
        it = self._pending.pop(0)
        t = it.get("type")
        if t == "BREAKER":
            self._start_transition(breaker=True, reason="manual_breaker")
        elif t == "MONTH_RESET":
            self._perform_month_reset()
        elif t == "SWITCH":
            if state.get("global_state") == GS_BREAKER or state.get("breaker_latched"):
                self._notify("切换异常", "当前处于全局保护停机，请先发送 /resume 恢复服务。")
            else:
                self._start_transition(to=it.get("account"), reason="manual")
        elif t == "RESUME":
            if state.get("global_state") == GS_BREAKER:
                to = self._select_target(self.store.get_runtimes())
                if to is None:
                    self._notify(
                        "切换异常",
                        "无法恢复服务：当前没有同时满足流量、运行时长和账号状态要求的节点。",
                    )
                else:
                    self._start_transition(to=to, reason="resume")
            else:
                self._notify(
                    "切换异常",
                    "当前系统并未处于全局保护停机状态，无需执行恢复。",
                )
        elif t == "TRAFFIC":
            account = it.get("account")
            traffic = self.store.latest_traffic(self.cfg.th.traffic_stale_sec)
            if (state.get("global_state") != GS_BREAKER
                    and account == state.get("duty_account")
                    and account in traffic and traffic[account] >= self.cfg.th.traffic_threshold_gb):
                self._start_transition(reason="traffic")
        elif t == "SPOT_WARN":
            if state.get("global_state") != GS_BREAKER:
                self._start_transition(from_=it.get("account"), reason="spot_warn")

    def _handle_steady(self, obs: dict, state: dict) -> None:
        # 稳态维护 + 漂移纠偏(I2/I3) + STEADY 守卫触发（要求 3/5/6）
        duty = state.get("duty_account")
        # 重新分类，防御性处理 I1 被破坏
        running = [a for a, o in obs.items() if o.obs == OBS_RUNNING]
        if len(running) > 1:
            self._handle_degraded_multi(obs, state)
            return
        if len(running) == 0:
            self._handle_degraded_zero(obs, state)
            return
        if duty is None or duty not in obs:
            self._handle_degraded_zero(obs, state)
            return
        o = obs[duty]
        if o.obs == OBS_UNKNOWN:
            return
        if o.obs in (OBS_STARTING, OBS_STOPPING):
            self._sync_accounts_obs(state, obs)
            self.sj.save(state)
            return
        if o.obs != OBS_RUNNING:
            # 当班机丢失（NOT_FOUND / STOPPED_KM / STOPPED_SC）-> 重调度
            self._start_transition(from_=duty, reason="spot_warn")
            return

        # 非当班账号纠偏：I2 普通停机重发 / I3 标签不符
        for a, ob in obs.items():
            if a == duty:
                continue
            if ob.obs == OBS_STOPPED_KM:
                retry_key = f"standby-stop:{a}"
                if time.time() >= self._cooldown.get(retry_key, 0):
                    try:
                        self.clients[a].stop_instance_stopcharging()
                    except CloudAPIError as e:
                        LOGGER.warning("纠偏停机 %s: %s", a, e)
                    self._cooldown[retry_key] = (
                        time.time() + self.cfg.th.stop_retry_interval_sec
                    )
                self._notify_throttled(
                    f"standby-stop-mode:{a}",
                    self.cfg.th.alert_repeat_interval_sec,
                    "切换异常",
                    f"发现备用节点 {self._account_label(a)} 没有进入节省停机。\n"
                    "系统已重新提交节省停机请求。",
                )
            if ob.duty_on:
                try:
                    self.clients[a].set_duty(False)
                except CloudAPIError as e:
                    LOGGER.warning("纠偏标签 %s: %s", a, e)

        # I3 当班 duty 标签
        if not o.duty_on:
            try:
                self.clients[duty].set_duty(True)
            except CloudAPIError as e:
                LOGGER.warning("写入 duty 标签 %s: %s", duty, e)

        # EIP 未绑定 -> 告警（要求 6）
        if not o.eip_bound:
            self._notify_throttled(
                f"eip-unbound:{duty}",
                self.cfg.th.alert_repeat_interval_sec,
                "切换异常",
                f"节点 {self._account_label(duty)} 未检测到预期的公网 IP 绑定，请检查阿里云控制台。",
            )

        # STEADY 守卫：服务探测（要求 5）
        now = time.time()
        if self._probe_service(duty):
            self._probe_fails[duty] = 0
        else:
            self._probe_fails[duty] = self._probe_fails.get(duty, 0) + 1
            if self._probe_fails[duty] >= 3:
                self._notify_throttled(
                    f"service-down:{duty}",
                    self.cfg.th.alert_repeat_interval_sec,
                    "切换异常",
                    f"节点 {self._account_label(duty)} 连续 3 次无法建立服务连接。\n"
                    + (
                        "系统将尝试切换到其他可用节点。"
                        if self.cfg.th.auto_switch_on_service_down
                        else "自动故障切换当前未启用，请手动检查服务。"
                    ),
                )
                if self.cfg.th.auto_switch_on_service_down:
                    self._start_transition(from_=duty, reason="service_down")
                    return

        # 退避：避免触发后立刻重入（与 _abort 协同）
        cool = self._cooldown.get(duty, 0)
        if now < cool:
            self._sync_accounts_obs(state, obs)
            self.sj.save(state)
            return

        # 触发：Tmax（要求 5）
        runtimes = self.store.get_runtimes()
        if runtimes.get(duty, 0) >= self.cfg.th.tmax_hours * 3600:
            self._start_transition(from_=duty, reason="tmax")
            return
        # 触发：流量快照超阈值（要求 5，双层守卫之二）
        traffic = self.store.latest_traffic(self.cfg.th.traffic_stale_sec)
        if duty not in traffic:
            self._notify_throttled(
                f"traffic-stale:{duty}",
                self.cfg.th.alert_repeat_interval_sec,
                "切换异常",
                f"节点 {self._account_label(duty)} 的流量数据已超过 "
                f"{self._duration_label(self.cfg.th.traffic_stale_sec)} 没有更新。\n"
                "系统不会把过期数据用于选择新节点，请检查 CDT 查询和本地日志。",
            )
        elif traffic[duty] >= self.cfg.th.traffic_threshold_gb:
            self._start_transition(from_=duty, reason="traffic")
            return
        # 触发：抢占回收预警 / 实例丢失（要求 5）
        if o.lock_recycling:
            self._start_transition(from_=duty, reason="spot_warn")
            return

        self._sync_accounts_obs(state, obs)
        self.sj.save(state)

    def _handle_degraded_multi(self, obs: dict, state: dict) -> None:
        # DEGRADED_MULTI：胜者=Running 中 argmin runtime 且有余量者，其余 T4 停机（要求 6，设计 §8）
        running = [a for a, o in obs.items() if o.obs == OBS_RUNNING]
        runtimes = self.store.get_runtimes()
        traffic = self.store.latest_traffic(self.cfg.th.traffic_stale_sec)
        cands = [a for a in running if self._eligible(a, runtimes, traffic)]
        if not cands:
            if running and all(self._quota_exceeded(a, runtimes, traffic) for a in running):
                to = self._select_target(runtimes)
                if to is None:
                    self._start_transition(breaker=True, reason="no_eligible")
                else:
                    self._start_transition(to=to, reason="traffic", stop_list=running)
            else:
                self._notify_throttled(
                    "multi-quota-unknown", self.cfg.th.alert_repeat_interval_sec, "切换异常",
                    "检测到多台在线，但没有足够的新鲜用量数据安全选机，等待流量查询恢复。",
                )
            return
        winner = min(cands, key=lambda a: runtimes.get(a, 0))
        losers = [a for a in running if a != winner]
        if losers:
            self._start_transition(from_=winner, to=winner, reason="cleanup_multi", stop_list=losers)
        else:
            ns = dict(state)
            ns["global_state"] = GS_STEADY
            ns["duty_account"] = winner
            self._sync_accounts_obs(ns, obs)
            self.sj.save(ns)

    def _handle_degraded_zero(self, obs: dict, state: dict) -> None:
        # DEGRADED_ZERO：按 §3.2 选目标退避拉起（要求 6，设计 §8）
        now = time.time()
        if now < self._cooldown.get("_zero", 0):
            return
        to = self._select_target(self.store.get_runtimes())
        if to is None:
            self._cooldown["_zero"] = now + self.cfg.th.stop_retry_interval_sec
            self._notify_throttled(
                "no-eligible-zero",
                self.cfg.th.alert_repeat_interval_sec,
                "切换异常",
                "当前没有在线节点，也没有符合流量、运行时长和账号状态要求的备用节点。\n"
                "系统将在稍后继续检查。",
            )
            return
        self._start_transition(from_=None, to=to, reason="degraded_zero")

    # —— 内部：TRANSITION 流水线推进 ——

    def _advance_transition(self, tr: dict, obs: dict):
        if self._freeze_unknown(obs):
            return None
        # 推进一扇门；返回更新后的 state（含 transition），tick 负责落盘
        # v1 的 T4/T5/T6 分别是 DNS/停机/收尾。升级时安全迁移：旧 T4 已通过
        # 服务探测，可直接进入停机；旧 T5 仍须继续确认停机，绝不能误当成收尾。
        if tr.get("version", 1) < TRANSITION_VERSION:
            old_step = tr.get("step")
            tr["step"] = {"T4": STEP_T4, "T5": STEP_T4, "T6": STEP_T5}.get(old_step, old_step)
            tr["version"] = TRANSITION_VERSION
            meta = tr.setdefault("meta", {})
            for key in ("t4_retry", "dns_record_id", "target_eip"):
                meta.pop(key, None)
            LOGGER.info("迁移 v1 流水线游标: %s -> %s", old_step, tr.get("step"))
        state = self.sj.load()
        step = tr.get("step")
        if step == STEP_T1:
            return self._gate_t1(tr, obs, state)
        if step == STEP_T2:
            return self._gate_t2(tr, obs, state)
        if step == STEP_T3:
            return self._gate_t3(tr, obs, state)
        if step == STEP_T4:
            return self._gate_t4(tr, obs, state)
        if step == STEP_T5:
            return self._gate_t5(tr, obs, state)
        return self._abort_transition(tr, state, "遇到无法识别的切换步骤")

    def _gate_t1(self, tr, obs, state):
        # T1 双门：运行时不超 Tmax 且最近 CDT 快照不超阈值且账号健康（要求 3）
        to = tr.get("to")
        if to is not None:
            runtimes = self.store.get_runtimes()
            traffic = self.store.latest_traffic(self.cfg.th.traffic_stale_sec)
            if not self._eligible(to, runtimes, traffic):
                LOGGER.warning(
                    "T1 目标校验失败 account=%s runtime=%ss traffic=%sGB",
                    to, runtimes.get(to, 0), traffic.get(to, "未知"),
                )
                return self._abort_transition(
                    tr,
                    state,
                    f"节点 {self._account_label(to)} 不满足流量、运行时长或账号状态要求",
                )
            LOGGER.info(
                "T1 目标校验通过 account=%s runtime=%ss/%ss traffic=%.1fGB/%.1fGB",
                to, runtimes.get(to, 0), self.cfg.th.tmax_hours * 3600,
                traffic[to], self.cfg.th.traffic_threshold_gb,
            )
        if tr.get("stop_list") is None:
            # 目标机永不在停机列表（from==to 自救拉起场景）
            tr["stop_list"] = ([tr["from"]]
                               if (tr.get("from") and tr.get("from") != tr.get("to")) else [])
        tr["step"] = STEP_T2          # 进入 T2，超时由 started_at 推算
        return self._with_tr(state, tr)

    def _gate_t2(self, tr, obs, state):
        # T2 拉起目标：回读 Running；NoStock 退避，总超 start_timeout -> 告警转 STEADY(旧)/ZERO（要求 3）
        to = tr.get("to")
        if to is None:
            tr["step"] = STEP_T3
            return self._with_tr(state, tr)
        o = obs.get(to)
        if o is not None and o.obs == OBS_RUNNING:
            meta = tr.setdefault("meta", {})
            meta.setdefault("t3_started_at", time.time())
            LOGGER.info("T2 目标实例已 Running account=%s，进入 T3 服务探测", to)
            tr["step"] = STEP_T3
            return self._with_tr(state, tr)
        meta = tr.setdefault("meta", {})
        now = time.time()
        elapsed = now - tr["started_at"]
        if elapsed > self.cfg.th.start_timeout_sec:
            timeout = self._duration_label(self.cfg.th.start_timeout_sec)
            return self._abort_transition(
                tr,
                state,
                f"节点 {self._account_label(to)} 在 {timeout} 内未进入运行状态",
            )
        if o is not None and o.obs == OBS_STARTING:
            LOGGER.info(
                "T2 等待目标实例 Running account=%s status=%s elapsed=%.1fs/%.1fs",
                to, o.obs, elapsed, self.cfg.th.start_timeout_sec,
            )
            return self._with_tr(state, tr)   # 等待轮询
        if o is None or o.obs == OBS_UNKNOWN:
            LOGGER.info(
                "T2 暂时无法确认目标实例状态 account=%s elapsed=%.1fs/%.1fs",
                to, elapsed, self.cfg.th.start_timeout_sec,
            )
            return self._with_tr(state, tr)
        # NoStock 退避未到期不重发（否则退避序列形同虚设，每 tick 重复告警）
        if now < meta.get("next_retry_at", 0):
            LOGGER.info(
                "T2 等待重试 account=%s remaining=%.1fs",
                to, meta["next_retry_at"] - now,
            )
            return self._with_tr(state, tr)
        # 发出 StartInstance（幂等：Running/Starting 直接返回）
        try:
            LOGGER.info(
                "T2 调用 StartInstance account=%s observed=%s elapsed=%.1fs",
                to, o.obs if o is not None else "NO_OBSERVATION", elapsed,
            )
            self.clients[to].start_instance()
            meta["t2_started"] = True
            LOGGER.info("T2 StartInstance 请求已提交 account=%s", to)
        except NoStockError:
            idx = meta.get("t2_backoff_idx", 0)
            backs = self.cfg.th.nostock_backoff_sec
            wait = backs[min(idx, len(backs) - 1)]
            meta["t2_backoff_idx"] = idx + 1
            meta["next_retry_at"] = time.time() + wait
            self._notify(
                "切换异常",
                f"节点 {self._account_label(to)} 暂时没有可用库存。\n"
                f"系统将在 {self._duration_label(wait)} 后重试。",
            )
        except ArrearsError:
            self._arrears.add(to)
            return self._abort_transition(
                tr, state, f"节点 {self._account_label(to)} 所属账号可能欠费"
            )
        except CloudAPIError as e:
            LOGGER.warning("T2 start %s 云API错误: %s", to, e)
        return self._with_tr(state, tr)

    def _gate_t3(self, tr, obs, state):
        # T3 服务探测：连续 probe_required_success 次成功；超时回滚（要求 3）
        to = tr.get("to")
        if to is None:
            tr["step"] = STEP_T4
            return self._with_tr(state, tr)
        meta = tr.setdefault("meta", {})
        now = time.time()
        t3_started_at = meta.setdefault("t3_started_at", now)
        elapsed = now - t3_started_at
        if elapsed > self.cfg.th.probe_timeout_sec:
            cfg = self.cfg.accounts[to]
            LOGGER.warning(
                "T3 服务探测超时 account=%s endpoint=%s:%s elapsed=%.1fs/%.1fs",
                to, cfg.eip, cfg.service_port, elapsed, self.cfg.th.probe_timeout_sec,
            )
            return self._rollback(
                tr,
                state,
                f"节点 {self._account_label(to)} 已启动，但服务端口在 "
                f"{self._duration_label(self.cfg.th.probe_timeout_sec)} 内始终未通过连续可用性检查",
            )
        if now - meta.get("t3_last", 0) < self.cfg.th.probe_interval_sec:
            return self._with_tr(state, tr)   # 控制探测频率，跨 tick 等待
        meta["t3_last"] = now
        target_obs = obs.get(to)
        ok = (target_obs is not None and target_obs.obs == OBS_RUNNING
              and target_obs.eip_bound and self._probe_service(to))
        if ok:
            meta["t3_success"] = meta.get("t3_success", 0) + 1
            if meta["t3_success"] >= self.cfg.th.probe_required_success:
                tr["step"] = STEP_T4
        else:
            meta["t3_success"] = 0
        cfg = self.cfg.accounts[to]
        LOGGER.info(
            "T3 TCP 探测 account=%s endpoint=%s:%s result=%s consecutive=%s/%s "
            "elapsed=%.1fs/%.1fs",
            to, cfg.eip, cfg.service_port, "成功" if ok else "失败",
            meta["t3_success"], self.cfg.th.probe_required_success,
            elapsed, self.cfg.th.probe_timeout_sec,
        )
        return self._with_tr(state, tr)

    def _gate_t4(self, tr, obs, state):
        # T4 旧机节省停机。只有 T3 连续探测目标服务成功后才会到达这里。
        # 确认失败持续重发 + 升级告警，NOT_FOUND 视为满足。
        meta = tr.setdefault("meta", {})
        if self._freeze_unknown(obs):
            return self._with_tr(state, tr)
        stop_list = tr.get("stop_list") or []
        stop_done = meta.setdefault("stop_done", {})
        stop_retry_at = meta.setdefault("stop_retry_at", {})
        if not isinstance(stop_retry_at, dict):
            stop_retry_at = {}  # 防御旧/损坏游标
            meta["stop_retry_at"] = stop_retry_at
        now = time.time()
        all_done = True
        for acc in stop_list:
            o = obs.get(acc)
            # 每轮重新推导，持久化标记只能记录进度，不能替代当前云侧状态。
            stop_done[acc] = False
            if o is not None and o.obs == OBS_STOPPED_SC:
                stop_done[acc] = True
                LOGGER.info("T4 停机确认完成 account=%s status=%s", acc, o.obs)
                continue
            if o is not None and o.obs == OBS_NOT_FOUND:
                stop_done[acc] = True
                if acc not in meta.setdefault("not_found", []):
                    meta["not_found"].append(acc)
                LOGGER.info("T4 目标已不存在，视为停机完成 account=%s", acc)
                continue
            # 每次实际发送停机前复核目标；熔断没有目标，允许停止所有已确认实例。
            if now >= stop_retry_at.get(acc, 0) and tr.get("to") is not None:
                to = tr["to"]
                if acc == to:
                    raise ValueError("停机列表包含目标实例，拒绝执行")
                try:
                    target_obs = self.clients[to].get_instance_obs()
                except Exception:
                    target_obs = InstanceObs(obs=OBS_UNKNOWN)
                current = dict(obs)
                current[to] = target_obs
                if self._freeze_unknown(current):
                    return self._with_tr(state, tr)
                if not (target_obs.obs == OBS_RUNNING and target_obs.eip_bound
                        and self._probe_service(to)):
                    # 保留旧机，重新从连续服务检查开始，超时后走已有回滚路径。
                    tr["step"] = STEP_T3
                    meta["t3_started_at"] = now
                    meta["t3_success"] = 0
                    meta["t3_last"] = 0
                    self._notify_throttled(
                        f"pre-stop-unavailable:{to}", self.cfg.th.alert_repeat_interval_sec,
                        "切换异常", "停机前复核发现目标节点不可用，已暂停停止原节点，重新检查服务。",
                    )
                    return self._with_tr(state, tr)
            if o is not None and o.obs == OBS_STOPPED_KM:
                if now >= stop_retry_at.get(acc, 0):
                    try:
                        LOGGER.info("T4 检测到普通停机，重发 StopCharging account=%s", acc)
                        self.clients[acc].stop_instance_stopcharging()   # 重发 StopCharging
                    except CloudAPIError as e:
                        LOGGER.warning("T4 重发停机 %s: %s", acc, e)
                    stop_retry_at[acc] = now + self.cfg.th.stop_retry_interval_sec
                all_done = False
                continue
            # RUNNING/STARTING/STOPPING -> 未确认，节流重发
            if now >= stop_retry_at.get(acc, 0):
                try:
                    LOGGER.info(
                        "T4 调用 StopInstance(StopCharging) account=%s observed=%s",
                        acc, o.obs if o is not None else "NO_OBSERVATION",
                    )
                    self.clients[acc].stop_instance_stopcharging()
                except CloudAPIError as e:
                    LOGGER.warning("T4 停机 %s: %s", acc, e)
                stop_retry_at[acc] = now + self.cfg.th.stop_retry_interval_sec
            all_done = False
        # 升级告警（持续重试，绝不静默放过，设计原则 4）
        # 过渡期旧机未停属正常：仅确实仍有 RUNNING 未停机时才标 MULTI；告警按周期节流
        if not all_done:
            pending = [a for a in stop_list if not stop_done.get(a)]
            ns = self._with_tr(state, tr)
            still_running = [a for a in pending
                             if obs.get(a) is not None and obs[a].obs == OBS_RUNNING]
            if still_running:
                ns["global_state"] = GS_MULTI   # 双在线，标记 MULTI 直到确认
            la = meta.get("last_alarm", tr["started_at"])
            if now - la >= self.cfg.th.stop_alarm_interval_sec:
                meta["last_alarm"] = now
                self._notify(
                    "切换异常",
                    "以下原节点尚未确认进入节省停机：\n"
                    + "\n".join(f"• {self._account_label(a)}" for a in pending)
                    + "\n系统会继续重试，请留意可能产生的费用。",
                )
            return ns
        tr["step"] = STEP_T5
        LOGGER.info("T4 全部旧实例已确认节省停机，进入 T5 stop_list=%s", stop_list)
        return self._with_tr(state, tr)

    def _gate_t5(self, tr, obs, state):
        # T5 收尾：写 duty 标签 -> 落盘 -> 发通知
        if self._freeze_unknown(obs):
            return self._with_tr(state, tr)
        to = tr.get("to")
        meta = tr.setdefault("meta", {})
        tag_done = meta.setdefault("tag_done", {})
        tag_retry_at = meta.setdefault("tag_retry_at", {})
        if not isinstance(tag_done, dict):
            tag_done = {}
            meta["tag_done"] = tag_done
        if not isinstance(tag_retry_at, dict):
            tag_retry_at = {}
            meta["tag_retry_at"] = tag_retry_at
        now = time.time()
        retry_interval = max(10, self.cfg.th.probe_interval_sec)

        # T3 与 T5 之间目标仍可能被回收；旧机此时已经确认停机，不能伪装成 STEADY。
        if to is not None:
            target_obs = obs.get(to)
            if target_obs is not None and target_obs.obs not in (
                    OBS_RUNNING, OBS_STARTING, OBS_UNKNOWN):
                ns = dict(state)
                ns["global_state"] = GS_ZERO
                ns["duty_account"] = None
                ns["transition"] = None
                self._sync_accounts_obs(ns, obs)
                self.sj.save(ns)
                self._notify(
                    "切换异常",
                    f"节点 {self._account_label(to)} 在收尾前已不再运行。\n"
                    "系统不会宣布切换成功，下一轮将重新选择可用节点。",
                )
                return ns

        # 停机纠偏不能排在标签成功之后；尤其熔断中标签失败时，OOS 仍可能拉起旧机。
        if self._return_to_stop_confirmation(tr, obs):
            return self._with_tr(state, tr)

        # duty 是 OOS 看门狗的控制接口，必须逐台回读一致后才能宣布完成。
        for account in self.cfg.accounts:
            tag_done[account] = False
            desired = to is not None and account == to
            observed = obs.get(account)
            if observed is not None and observed.obs == OBS_NOT_FOUND:
                # 非目标实例已回收，等同于不再携带有效 duty 标签。
                tag_done[account] = not desired
                continue
            if observed is None or observed.obs == OBS_UNKNOWN or (
                    desired and observed.obs == OBS_STARTING):
                self._notify_throttled(
                    f"t5-observation-unknown:{account}",
                    self.cfg.th.alert_repeat_interval_sec,
                    "切换异常",
                    f"暂时无法确认节点 {self._account_label(account)} 的标签状态。\n"
                    "系统会等待观测恢复，确认前不会宣布切换完成。",
                )
                continue
            if (observed is not None and observed.obs != OBS_UNKNOWN
                    and observed.duty_on == desired):
                tag_done[account] = True
                continue
            if now < tag_retry_at.get(account, 0):
                continue
            try:
                LOGGER.info("T5 写入 duty 标签 account=%s desired=%s", account, desired)
                self.clients[account].set_duty(desired)
                confirmed = self.clients[account].get_instance_obs()
                if confirmed.obs in (OBS_UNKNOWN, OBS_STARTING):
                    raise CloudAPIError("标签回读时实例状态暂不可确认")
                if desired and confirmed.obs != OBS_RUNNING:
                    ns = dict(state)
                    ns["global_state"] = GS_ZERO
                    ns["duty_account"] = None
                    ns["transition"] = None
                    updated_obs = dict(obs)
                    updated_obs[account] = confirmed
                    self._sync_accounts_obs(ns, updated_obs)
                    self.sj.save(ns)
                    self._notify(
                        "切换异常",
                        f"节点 {self._account_label(account)} 在标签确认时已不再运行。\n"
                        "系统不会宣布切换成功，下一轮将重新选择可用节点。",
                    )
                    return ns
                if (confirmed.obs == OBS_NOT_FOUND and not desired) or confirmed.duty_on == desired:
                    tag_done[account] = True
                    LOGGER.info("T5 duty 标签确认完成 account=%s desired=%s", account, desired)
                    continue
                LOGGER.warning("T5 %s duty 标签回读不一致 desired=%s", account, desired)
            except CloudAPIError as e:
                LOGGER.warning("T5 标签写入或回读失败 %s: %s", account, e)
            tag_retry_at[account] = now + retry_interval
            self._notify_throttled(
                f"duty-tag-mismatch:{account}",
                self.cfg.th.alert_repeat_interval_sec,
                "切换异常",
                f"节点 {self._account_label(account)} 的当班标签尚未确认更新。\n"
                f"系统会在 {self._duration_label(retry_interval)} 后重试，"
                "确认前不会宣布切换完成。",
            )

        if not all(tag_done.get(a) for a in self.cfg.accounts):
            return self._with_tr(state, tr)
        # 写标签后再全量读取：OOS/人工操作可能让旧机重新在线或让标签再次漂移。
        final_obs = self._observe_all()
        if self._freeze_unknown(final_obs):
            return self._with_tr(state, tr)
        if self._return_to_stop_confirmation(tr, final_obs):
            return self._with_tr(state, tr)
        if to is not None:
            if final_obs[to].obs in (OBS_STOPPED_SC, OBS_STOPPED_KM, OBS_STOPPING, OBS_NOT_FOUND):
                ns = dict(state)
                ns.update(global_state=GS_ZERO, duty_account=None, transition=None)
                self._sync_accounts_obs(ns, final_obs)
                self.sj.save(ns)
                self._notify("切换异常", "目标节点在最终核验时已停止，系统将重新选择可用节点。")
                return ns
            if not (final_obs[to].obs == OBS_RUNNING and final_obs[to].eip_bound
                    and self._probe_service(to)):
                return self._with_tr(state, tr)
        for a, ob in final_obs.items():
            tag_done[a] = (ob.obs == OBS_NOT_FOUND and a != to) or ob.duty_on == (a == to)
        if not all(tag_done.values()):
            return self._with_tr(state, tr)
        obs = final_obs
        if tr.get("breaker"):
            ns = dict(state)
            ns["global_state"] = GS_BREAKER
            ns["duty_account"] = None
            ns["transition"] = None
            self._sync_accounts_obs(ns, obs)
            self.sj.save(ns)
            self._notify(
                "全局熔断",
                "所有实例均已确认进入节省停机状态。\n"
                "当前不会自动恢复服务，发送 /resume 可重新启动。",
            )
            return ns
        ns = dict(state)
        ns["global_state"] = GS_STEADY
        ns["duty_account"] = to
        ns["transition"] = None
        self._sync_accounts_obs(ns, obs)
        self.sj.save(ns)
        LOGGER.info(
            "T5 流水线完成 from=%s to=%s reason=%s",
            tr.get("from"), to, meta.get("reason"),
        )
        self._notify(
            "调度完成",
            f"当前节点：{self._account_label(to)}\n"
            f"原节点：{self._account_label(tr.get('from'))}\n"
            f"切换原因：{self._reason_label(meta.get('reason'))}\n\n"
            "目标服务已确认可用，原节点已确认节省停机。",
        )
        return ns

    def _return_to_stop_confirmation(self, tr: dict, obs: dict) -> bool:
        to = tr.get("to")
        if all(ob.obs in (OBS_STOPPED_SC, OBS_NOT_FOUND)
               for a, ob in obs.items() if a != to):
            return False
        tr["step"] = STEP_T4
        tr["stop_list"] = [a for a in self.cfg.accounts if a != to]
        meta = tr.setdefault("meta", {})
        meta.update(stop_done={}, stop_retry_at={}, tag_done={})
        LOGGER.warning("T5 发现非目标实例未保持节省停机，返回 T4 重新确认")
        return True

    def _abort_transition(self, tr, state, reason):
        # 流水线中止：有旧机则留在旧机（STEADY），无旧机则 DEGRADED_ZERO（要求 3/7）
        frm = tr.get("from")
        ns = dict(state)
        if frm is not None:
            ns["global_state"] = GS_STEADY
            ns["duty_account"] = frm
            ns["transition"] = None
            self._cooldown[frm] = time.time() + self.cfg.th.stop_retry_interval_sec
            self.sj.save(ns)
            self._notify(
                "切换异常",
                "本次切换未完成。\n"
                f"继续使用：{self._account_label(frm)}\n"
                f"原因：{reason}",
            )
        else:
            ns["global_state"] = GS_ZERO
            ns["duty_account"] = None
            ns["transition"] = None
            self.sj.save(ns)
            self._notify(
                "切换异常",
                "本次切换未完成，当前没有在线节点。\n"
                f"原因：{reason}",
            )
        return ns

    def _rollback(self, tr, state, reason):
        # T2/T3 失败可安全回滚：停新机、留旧机。转 cleanup 流水线确认新机已停。
        to = tr.get("to")
        frm = tr.get("from")
        if to is not None and to != frm:
            try:
                self.clients[to].stop_instance_stopcharging()
            except CloudAPIError as e:
                LOGGER.warning("回滚停新机 %s: %s", to, e)
            new_tr = {
                "version": TRANSITION_VERSION,
                "from": frm, "to": frm, "step": STEP_T4,
                "started_at": time.time(),
                "attempts": {}, "breaker": False,
                "deadline": time.time() + self.cfg.th.stop_timeout_sec,
                "stop_list": [to],          # 顶层单一事实源（T4 直读；rollback 流水线不过 T1）
                "meta": {"reason": "rollback", "stop_done": {}},
            }
            ns = self._with_tr(state, new_tr)
            self.sj.save(ns)
            self._notify(
                "切换异常",
                "目标节点未通过可用性检查，已启动安全回滚。\n"
                f"停止目标：{self._account_label(to)}\n"
                f"继续使用：{self._account_label(frm)}\n"
                f"原因：{reason}",
            )
            return ns
        return self._abort_transition(tr, state, reason)   # 目标即旧机或无旧机

    # —— 内部：目标选择 / 启动 / 探测 / 通知 ——

    def _select_target(self, runtimes: dict) -> Optional[str]:
        # eligible 中 argmin runtime；空 -> None（要求：设计 §3.2）
        traffic = self.store.latest_traffic(self.cfg.th.traffic_stale_sec)
        tmax = self.cfg.th.tmax_hours * 3600
        thresh = self.cfg.th.traffic_threshold_gb
        eligible = [a for a in self.cfg.accounts
                    if runtimes.get(a, 0) < tmax
                    and a in traffic
                    and traffic[a] < thresh
                    and self._healthy(a)]
        if not eligible:
            return None
        return min(eligible, key=lambda a: runtimes.get(a, 0))

    def _eligible(self, a: str, runtimes: dict, traffic: dict) -> bool:
        tmax = self.cfg.th.tmax_hours * 3600
        return (runtimes.get(a, 0) < tmax
                and a in traffic
                and traffic[a] < self.cfg.th.traffic_threshold_gb
                and self._healthy(a))

    def _healthy(self, a: str) -> bool:
        # 欠费账号移出 eligible（ArrearsError 标记，月重置清空）
        return a not in self._arrears

    def _current_duty(self) -> Optional[str]:
        return self.sj.load().get("duty_account")

    def _account_label(self, account: Optional[str]) -> str:
        """TG 使用 ECS 友好名，方括号保留稳定账号键供 /switch 使用。"""
        if account is None:
            return "无"
        friendly = self._instance_names.get(account) or account
        return f"{friendly} [{account}]" if friendly != account else account

    def _account_labels(self, accounts) -> str:
        return ", ".join(self._account_label(a) for a in accounts)

    @staticmethod
    def _reason_label(reason: Optional[str]) -> str:
        return _REASON_UI.get(reason or "", "系统自动调度")

    @staticmethod
    def _state_label(state: Optional[str]) -> str:
        emoji, text = _STATE_UI.get(state or "", ("⚪️", "状态未知"))
        return f"{emoji} {text}"

    @staticmethod
    def _obs_label(obs: Optional[str]) -> tuple[str, str]:
        return _OBS_UI.get(obs or "", ("⚪️", "尚未取得状态"))

    @staticmethod
    def _step_label(step: Optional[str]) -> str:
        return _STEP_UI.get(step or "", "正在处理中")

    @staticmethod
    def _duration_label(seconds: float) -> str:
        seconds = max(0, int(seconds))
        if seconds >= 3600 and seconds % 3600 == 0:
            return f"{seconds // 3600} 小时"
        if seconds >= 60 and seconds % 60 == 0:
            return f"{seconds // 60} 分钟"
        return f"{seconds} 秒"

    def _start_transition(self, to=None, reason="manual", from_=None,
                          stop_list=None, breaker=False, replace=False) -> None:
        # 启动一条流水线（单飞守卫 I5）。breaker=True 走熔断流水线（要求 4）
        if self.sj.load().get("transition") and not replace:
            LOGGER.warning("已有流水线在跑，忽略新请求(reason=%s)", reason)
            return
        if breaker:
            stop_list = list(self.cfg.accounts.keys())
            to = None
            from_ = None
        else:
            if from_ is None:
                from_ = self._current_duty()
            if to is None:
                runtimes = self.store.get_runtimes()
                to = self._select_target(runtimes)
                if to is None:
                    self._start_transition(breaker=True, reason="no_eligible")  # 无 eligible -> 熔断
                    return
            if stop_list is None:
                # 目标机永不在停机列表：from==to（被回收账号自救拉起 / /switch 当前账号）
                # 时 stop_list 必须为空，否则 T4 会把刚拉起的当班机停掉，服务中断+启停震荡
                stop_list = [from_] if (from_ and from_ != to) else []
        # 账号名校验：非法账号名不得进入游标（否则 T2 每 tick KeyError 卡死流水线）
        for acc in (from_, to):
            if acc is not None and acc not in self.cfg.accounts:
                self._notify(
                    "切换异常",
                    f"找不到节点键“{acc}”，本次{self._reason_label(reason)}请求已忽略。\n"
                    "请发送 /check 查看可用节点及其方括号内的节点键。",
                )
                return
        now = time.time()
        tr = {
            "version": TRANSITION_VERSION,
            "from": from_, "to": to,
            "step": (STEP_T4 if breaker else STEP_T1),
            "started_at": now, "attempts": {}, "breaker": breaker,
            "deadline": now + (self.cfg.th.stop_timeout_sec if breaker else self.cfg.th.start_timeout_sec),
            "stop_list": stop_list or [],   # 单一事实源在顶层；T4 直读（熔断流水线不过 T1，不可依赖 T1 补丁）
            "meta": {"reason": reason,
                     "stop_done": {}, "t3_success": 0, "t2_backoff_idx": 0,
                     "last_alarm": now, "next_retry_at": 0, "t3_last": 0},
        }
        state = self.sj.load()
        ns = self._with_tr(state, tr)
        if breaker:
            ns["breaker_latched"] = True
        elif reason in ("resume", "month_reset"):
            ns["breaker_latched"] = False
        self.sj.save(ns)
        LOGGER.info("启动流水线 reason=%s from=%s to=%s breaker=%s", reason, from_, to, breaker)
        kind = {"spot_warn": "抢占恢复", "tmax": "超限停机", "traffic": "超限停机",
                "manual": "临时调度", "month_reset": "临时调度", "resume": "临时调度",
                "no_eligible": "全局熔断", "cleanup_multi": "切换异常"}.get(reason, "临时调度")
        if breaker:
            kind = "全局熔断"
        if breaker:
            message = (
                f"触发原因：{self._reason_label(reason)}\n\n"
                "系统将停止所有实例，并逐一确认它们进入节省停机状态。"
            )
        else:
            message = (
                f"原节点：{self._account_label(from_)}\n"
                f"目标节点：{self._account_label(to)}\n"
                f"触发原因：{self._reason_label(reason)}\n\n"
                "安全顺序：启动目标 → 确认可用 → 停止原节点"
            )
        self._notify(kind, message)

    def _probe_service(self, account: str) -> bool:
        # 服务健康探测：TCP connect eip:service_port（3s 超时），连通即视为存活
        cfg = self.cfg.accounts[account]
        try:
            with socket.create_connection((cfg.eip, cfg.service_port), timeout=3):
                return True
        except OSError as exc:
            LOGGER.info(
                "TCP 连接失败 account=%s endpoint=%s:%s error=%s: %s",
                account, cfg.eip, cfg.service_port, type(exc).__name__, exc,
            )
            return False

    def _notify(self, kind: str, text: str) -> None:
        # 六类通知：tg.send + store.log_event（要求：通知六类）
        emoji, title = _NOTIFY_UI.get(kind, ("ℹ️", "系统通知"))
        public_text = f"{emoji} {title}\n━━━━━━━━━━━━\n{text}"
        try:
            self.tg.send(public_text)
        except Exception as e:
            LOGGER.warning("TG 发送失败: %s", e)
        try:
            self.store.log_event(kind, text)
        except Exception as e:
            LOGGER.warning("事件记录失败: %s", e)

    def _notify_throttled(self, key: str, interval_sec: int,
                          kind: str, text: str) -> bool:
        """同类异常在窗口内只记录和发送一次，避免断网时队列与数据库膨胀。"""
        now = time.time()
        last = self._notification_last.get(key, 0.0)
        if now - last < interval_sec:
            return False
        self._notification_last[key] = now
        self._notify(kind, text)
        return True

    # —— 内部：辅助 ——

    def _with_tr(self, state, tr):
        ns = dict(state)
        ns["transition"] = tr
        ns["global_state"] = (GS_BREAKER if (tr.get("breaker") and tr.get("step") == STEP_T5)
                               else GS_TRANSITION)
        return ns

    def _sync_accounts_obs(self, state, obs):
        acc = state.setdefault("accounts", {})
        details = self.store.latest_traffic_details()
        fresh = self.store.latest_traffic(self.cfg.th.traffic_stale_sec)
        thresh = self.cfg.th.traffic_threshold_gb
        for name, ob in obs.items():
            d = acc.setdefault(name, {})
            d["last_observed"] = ob.obs
            g, ts = details.get(name, (None, None))
            d["traffic_gb"] = g
            d["traffic_ts"] = ts
            d["traffic_stale"] = name in details and name not in fresh
            d["quota_exceeded"] = (g >= thresh) if g is not None else None

    def _tg_check(self) -> None:
        self.tg.send(self._summary_text("📋 运行状态报告"))

    def _summary_text(self, title: str) -> str:
        """生成 /check 与每日任务共用的状态、用量和阈值汇总。"""
        st = self.sj.load()
        lines = [
            title,
            "━━━━━━━━━━━━",
            f"系统状态：{self._state_label(st.get('global_state'))}",
            f"当前节点：{self._account_label(st.get('duty_account'))}",
        ]
        tr = st.get("transition")
        if tr:
            lines.extend([
                "",
                "🔄 切换进度",
                f"阶段：{self._step_label(tr.get('step'))}",
                f"路径：{self._account_label(tr.get('from'))} → "
                f"{self._account_label(tr.get('to'))}",
                f"原因：{self._reason_label(tr.get('meta', {}).get('reason'))}",
            ])
        runtimes = self.store.get_runtimes()
        traffic = self.store.latest_traffic()
        fresh_traffic = self.store.latest_traffic(self.cfg.th.traffic_stale_sec)
        thresh = self.cfg.th.traffic_threshold_gb
        tmax = self.cfg.th.tmax_hours * 3600
        lines.extend(["", "🖥 节点详情"])
        for name in self.cfg.accounts:
            ob = st.get("accounts", {}).get(name, {})
            rt = runtimes.get(name, 0)
            g = traffic.get(name)
            icon, status_text = self._obs_label(ob.get("last_observed"))
            traffic_text = (
                f"{g:.1f} / {thresh:.1f} GB"
                if g is not None else f"暂无数据（保护阈值 {thresh:.1f} GB）"
            )
            warnings = []
            if g is not None and g >= thresh:
                warnings.append("本月流量已达到保护阈值")
            if g is not None and name not in fresh_traffic:
                warnings.append("流量数据已过期，不会用于自动选择")
            if rt >= tmax:
                warnings.append("本月运行时长已达到上限")
            lines.extend([
                "",
                f"{icon} {self._account_label(name)}",
                f"   状态：{status_text}",
                f"   本月运行：{rt / 3600:.1f} 小时",
                f"   本月流量：{traffic_text}",
            ])
            if warnings:
                lines.append(f"   ⚠️ 提醒：{'；'.join(warnings)}")
        return "\n".join(lines)

    def _tg_traffic(self, traffic: Optional[dict[str, float]] = None,
                    errors: Optional[dict[str, str]] = None,
                    title: str = "📊 本月流量报告") -> None:
        using_snapshot = traffic is None
        traffic = self.store.latest_traffic() if using_snapshot else traffic
        fresh = (
            self.store.latest_traffic(self.cfg.th.traffic_stale_sec)
            if using_snapshot else traffic
        )
        errors = errors or {}
        thresh = self.cfg.th.traffic_threshold_gb
        lines = [title, "━━━━━━━━━━━━"]
        for name in self.cfg.accounts:
            label = self._account_label(name)
            if name in traffic:
                g = traffic[name]
                remaining = max(0.0, thresh - g)
                stale = name not in fresh
                icon = "🟠" if stale else ("🔴" if g >= thresh else "🟢")
                lines.extend([
                    "",
                    f"{icon} {label}",
                    f"   已使用：{g:.1f} GB",
                    f"   保护阈值：{thresh:.1f} GB",
                    f"   剩余缓冲：{remaining:.1f} GB",
                ])
                if g >= thresh:
                    lines.append("   ⚠️ 已达到保护阈值")
                if stale:
                    lines.append("   ⚠️ 数据已过期，不会用于自动选择")
            elif name in errors:
                lines.extend([
                    "",
                    f"🔴 {label}",
                    "   查询失败，请查看本地日志了解具体原因。",
                ])
            else:
                lines.extend([
                    "",
                    f"⚪️ {label}",
                    "   暂无数据",
                    f"   保护阈值：{thresh:.1f} GB",
                ])
        self.tg.send("\n".join(lines))

    def _tg_last(self) -> None:
        evs = self.store.recent_events(10)
        lines = ["🗂 最近事件", "━━━━━━━━━━━━"]
        if not evs:
            lines.extend(["", "暂时没有调度事件。"])
        for ts, kind, msg in evs:
            emoji, title = _NOTIFY_UI.get(kind, ("ℹ️", "系统通知"))
            friendly_msg = self._public_event_text(msg)
            lines.extend([
                "",
                f"{emoji} {title} · {ts.replace('T', ' ')}",
                *[f"   {line}" for line in friendly_msg.splitlines()],
            ])
        self.tg.send("\n".join(lines))

    def _public_event_text(self, text: str) -> str:
        """兼容旧数据库事件，避免 /last 再暴露内部状态码和字段名。"""
        if text == "/resume 无可用账号":
            return "无法恢复服务：当前没有符合条件的节点。"
        if text == "/resume 仅 BREAKER 可用":
            return "当前系统并未处于全局保护停机状态，无需执行恢复。"
        legacy_start = re.fullmatch(
            r"启动切换流水线 from=(.*?) to=(.*?) reason=([^ ]+)", text
        )
        if legacy_start:
            frm, to, reason = legacy_start.groups()
            return (
                f"原节点：{frm}\n"
                f"目标节点：{to}\n"
                f"触发原因：{self._reason_label(reason)}"
            )
        result = re.sub(
            r"obs=([A-Z_]+)",
            lambda match: f"状态：{self._obs_label(match.group(1))[1]}",
            text,
        )
        replacements = {
            "STOPPED_SC": "节省停机",
            "STOPPED_KM": "普通停机",
            "RUNNING": "运行中",
            "STARTING": "启动中",
            "STOPPING": "停止中",
            "NOT_FOUND": "实例不存在",
            "DEGRADED_ZERO": "当前没有在线节点",
            "DEGRADED_MULTI": "检测到多台在线",
            "BREAKER": "全局保护停机",
            "eligible": "可用节点列表",
            "duty": "当班",
            "I3": "自动巡检",
        }
        for raw, friendly in replacements.items():
            result = result.replace(raw, friendly)
        for step, friendly in _STEP_UI.items():
            result = re.sub(rf"\b{re.escape(step)}\b", friendly, result)
        result = re.sub(
            r"reason=([a-z_]+)",
            lambda match: f"原因：{self._reason_label(match.group(1))}",
            result,
        )
        result = re.sub(
            r"原因[ ：]([a-z_]+)",
            lambda match: f"原因：{self._reason_label(match.group(1))}",
            result,
        )
        result = re.sub(r"\bfrom=([^\s]+)", r"原节点：\1", result)
        result = re.sub(r"\bto=([^\s]+)", r"目标节点：\1", result)
        result = result.replace("->", " → ").replace("流水线", "切换流程")
        return result


# ===== Section 6: main 调度入口 =====

class ProcessLock:
    """Linux/macOS 进程锁。锁文件保留，锁由内核在退出/崩溃时释放。"""

    def __init__(self, paths):
        self.paths = sorted({os.path.realpath(path) + ".lock" for path in paths})
        self._fds: list[int] = []

    def __enter__(self):
        try:
            for path in self.paths:
                fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                self._fds.append(fd)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise RuntimeError(f"已有 CDT-switcher 使用此配置或状态文件：{path}") from exc
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_args):
        for fd in reversed(self._fds):
            os.close(fd)
        self._fds.clear()


def main() -> None:
    # 配置路径：允许 sys.argv[1] 覆盖
    path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    setup_logging()
    cfg = load_config(path)
    # 云 API 和数据库初始化之前先独占配置与持久化文件，绝不删除锁文件。
    try:
        with ProcessLock([path, cfg.th.db_path, cfg.th.state_path]):
            _run(cfg)
    except RuntimeError as exc:
        LOGGER.error("启动失败：%s", exc)
        raise SystemExit(1) from exc


def _run(cfg: Config) -> None:
    store = StateStore(cfg.th.db_path)
    sj = StateJson(cfg.th.state_path)
    tg = TGClient(cfg.tg)
    rotator = Rotator(cfg, store, sj, tg)

    # 启动即刷新 CDT，避免首次定时任务前把“未知流量”误当成 0。
    LOGGER.info("启动时刷新 CDT 流量")
    rotator.traffic_job()

    # 首轮 UNKNOWN reconcile（只读不写云侧）
    try:
        rotator.tick()
    except Exception:
        LOGGER.exception("首轮 reconcile 异常")

    # 稳态巡检保持 60s 级别；控制流水线按 probe_interval 快速推进，二者共用单飞锁。
    th = cfg.th
    scheduler = BackgroundScheduler(timezone=APP_TZ)
    scheduler.add_job(rotator.tick, "interval", seconds=th.patrol_interval_sec,
                      coalesce=True, max_instances=1, id="tick")
    scheduler.add_job(rotator.transition_tick, "interval",
                      seconds=max(1, th.probe_interval_sec),
                      coalesce=True, max_instances=1, id="transition")
    scheduler.add_job(rotator.traffic_job, "interval", minutes=th.traffic_interval_min,
                      coalesce=True, max_instances=1, id="traffic")
    scheduler.add_job(rotator.daily_report, CronTrigger(hour=23, minute=58, timezone=APP_TZ),
                      coalesce=True, max_instances=1, id="daily")
    scheduler.add_job(rotator.month_reset, CronTrigger(day=1, hour=0, minute=5, timezone=APP_TZ),
                      coalesce=True, max_instances=1, id="month")
    scheduler.add_job(rotator.maintenance_job, CronTrigger(hour=0, minute=30, timezone=APP_TZ),
                      coalesce=True, max_instances=1, id="maintenance")

    # TG 命令菜单 + 长轮询（回调只入队/只读）
    tg.register_commands()
    tg.start_polling(rotator.handle_tg_command)

    # 可选 webhook（默认关，EXPERIMENTAL）：POST 匹配 instance_id -> 入队 EV_SPOT_WARN
    srv: Optional[ThreadingHTTPServer] = None
    if th.enable_eventbridge_webhook:
        wb_host, wb_sep, wb_port_s = th.webhook_listen.partition(":")
        if not wb_sep:                       # 只给了端口："8787" -> 127.0.0.1:8787
            wb_host, wb_port_s = "127.0.0.1", wb_host
        wb_host = wb_host or "127.0.0.1"
        try:
            wb_port = int(wb_port_s) if wb_port_s else 8787
        except ValueError:
            LOGGER.warning("webhook_listen 非法: %s，回退 8787", th.webhook_listen)
            wb_port = 8787
        id_to_account = {a.instance_id: name for name, a in cfg.accounts.items()}

        class _WebhookHandler(BaseHTTPRequestHandler):
            MAX_BODY_SIZE = 64 * 1024

            def do_POST(self):
                self.connection.settimeout(5)
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    if length < 0 or length > self.MAX_BODY_SIZE:
                        self.send_response(413)
                        self.end_headers()
                        return
                    body = self.rfile.read(length) if length else b""
                    data = json.loads(body.decode("utf-8", "ignore")) if body else {}
                except Exception:
                    data = {}
                iid = data.get("instance_id") or (data.get("detail") or {}).get("instance_id")
                acc = id_to_account.get(iid) if iid else None
                accepted = True
                if acc:
                    accepted = rotator.submit_intent("SPOT_WARN", account=acc)
                    if accepted:
                        LOGGER.info("[EXPERIMENTAL] webhook %s -> 抢占预警 %s", iid, acc)
                self.send_response(200 if accepted else 503)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *args):
                pass

        try:
            srv = ThreadingHTTPServer((wb_host, wb_port), _WebhookHandler)
        except OSError:
            LOGGER.exception("webhook 启动失败，主状态机继续运行")
        else:
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            LOGGER.info("[EXPERIMENTAL] webhook 监听 %s:%s", wb_host, wb_port)

    # 主线程阻塞 + 信号优雅退出
    stop = threading.Event()

    def _sig(signum, _frame):
        LOGGER.info("收到信号 %s，准备退出", signum)
        stop.set()

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)
    try:
        scheduler.start()
        while not stop.wait(1.0):
            pass
    finally:
        # 等正在执行的任务退出，才可关闭数据库并由 main 释放进程锁。
        if scheduler.running:
            scheduler.shutdown(wait=True)
        try:
            tg.stop()
        except Exception:
            pass
        if srv is not None:
            try:
                srv.shutdown()
                srv.server_close()
            except Exception:
                pass
        try:
            store.close()
        except Exception:
            pass
        LOGGER.info("已优雅退出")


if __name__ == "__main__":
    main()
