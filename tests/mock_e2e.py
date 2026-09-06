# mock_e2e.py — 离线端到端演练：用假云客户端驱动 Rotator 状态机跑关键场景。
# 不碰任何真实云服务；在真实账号联调前把语义 bug 挤出来。
# 运行：python3 tests/mock_e2e.py   （退出码 0 = 全部通过）

import os
import re
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cdt_switcher as m  # noqa: E402

m.setup_logging("WARNING")  # 测试时静默日志


# ---------- 假客户端（模拟云侧真实状态机） ----------

class FakeAliyun:
    """模拟一台 Spot 实例 + 账号级 CDT 流量。状态转换立即完成（测试速度），故障可注入。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.cloud_instance_name = cfg.instance_name
        self.status = "Stopped"
        self.stopped_mode = "StopCharging"
        self.lock_recycling = False
        self.duty = False
        self.traffic_gb = 0.0
        self.traffic_calls = 0
        self.stop_calls = 0
        self.exists = True
        # 故障注入
        self.nostock_times = 0      # 接下来 N 次 start 抛 NoStockError
        self.km_stop_times = 0      # 接下来 N 次 stop 退化为 KeepCharging

    def get_instance_obs(self):
        if not self.exists:
            return m.InstanceObs(exists=False, obs=m.OBS_NOT_FOUND)
        obs = m.InstanceObs(exists=True, status=self.status,
                            stopped_mode=self.stopped_mode,
                            lock_recycling=self.lock_recycling,
                            eip_bound=True, duty_on=self.duty,
                            instance_name=self.cloud_instance_name)
        if self.status == "Running":
            obs.obs = m.OBS_RUNNING
        elif self.status == "Starting":
            obs.obs = m.OBS_STARTING
        elif self.status == "Stopping":
            obs.obs = m.OBS_STOPPING
        elif self.status == "Stopped":
            obs.obs = m.OBS_STOPPED_SC if self.stopped_mode == "StopCharging" else m.OBS_STOPPED_KM
        return obs

    def start_instance(self):
        if self.status in ("Running", "Starting"):
            return
        if self.nostock_times > 0:
            self.nostock_times -= 1
            raise m.NoStockError("mock: 库存不足")
        self.status = "Running"     # 立即 Running（简化过渡态）
        self.lock_recycling = False

    def stop_instance_stopcharging(self):
        self.stop_calls += 1
        # 仅「已确认节省停机」才是幂等空操作；Stopped+KeepCharging 时重发应纠偏计费模式
        if self.status == "Stopped" and self.stopped_mode == "StopCharging":
            return
        self.status = "Stopped"
        if self.km_stop_times > 0:  # 模拟静默退化：API 成功但 KeepCharging
            self.km_stop_times -= 1
            self.stopped_mode = "KeepCharging"
        else:
            self.stopped_mode = "StopCharging"

    def set_duty(self, on: bool):
        self.duty = on

    def get_traffic_gb(self):
        self.traffic_calls += 1
        return self.traffic_gb


class FakeTG:
    def __init__(self, cfg):
        self.cfg = cfg
        self.messages = []

    def send(self, text):
        self.messages.append(text)

    def register_commands(self, billing_enabled=False):
        pass

    def start_polling(self, cb):
        pass

    def stop(self):
        pass


# ---------- 测试设施 ----------

def make_world(seed_traffic=True, account_names=("A", "B"), instance_names=None):
    d = tempfile.mkdtemp()
    instance_names = instance_names or {}
    acc = {}
    for idx, name in enumerate(account_names, start=1):
        acc[name] = m.AccountCfg(
            name=name, region="cn-hongkong", access_key_id="k",
            access_key_secret="s", instance_id=f"i-{name.lower()}",
            eip=f"{idx}.{idx}.{idx}.{idx}", service_port=443,
            instance_name=instance_names.get(name, ""),
        )
    th = m.Thresholds(probe_interval_sec=0, nostock_backoff_sec=[0],
                      stop_retry_interval_sec=0, stop_alarm_interval_sec=10 ** 9)
    cfg = m.Config(accounts=acc, tg=m.TGCfg(bot_token="b", chat_ids=[1]), th=th)
    store = m.StateStore(os.path.join(d, "r.db"))
    if seed_traffic:
        for name in acc:
            store.add_traffic(name, 0.0)
    sj = m.StateJson(os.path.join(d, "s.json"))
    tg = FakeTG(cfg.tg)
    r = m.Rotator(cfg, store, sj, tg)
    fakes = {name: FakeAliyun(ac) for name, ac in acc.items()}
    r.clients = fakes
    r._probe_service = lambda a: True   # 跳过真实 TCP
    return r, fakes, store, sj, tg


def ticks(r, n=30):
    for _ in range(n):
        r.tick()


def steady_duty(sj):
    st = sj.load()
    assert st.get("global_state") == m.GS_STEADY, f"期望 STEADY，实际 {st}"
    return st["duty_account"]


def assert_one_running(fakes, duty):
    running = [a for a, f in fakes.items() if f.status == "Running"]
    assert running == [duty], f"在线实例应为 [{duty}]，实际 {running}"
    for a, f in fakes.items():
        if a != duty:
            assert f.status == "Stopped" and f.stopped_mode == "StopCharging", \
                f"{a} 应为节省停机，实际 {f.status}/{f.stopped_mode}"
            assert f.duty is False, f"{a} 标签应为 off"
    assert fakes[duty].duty is True, "当班标签应为 on"


# ---------- 场景 ----------

def s1_cold_start():
    r, fakes, store, sj, tg = make_world()
    ticks(r, 1)                     # 首轮只读
    assert sj.load().get("global_state") == m.GS_ZERO, "首轮应分类为 ZERO"
    ticks(r)                        # ZERO -> 选目标拉起 -> 走完流水线
    duty = steady_duty(sj)
    assert_one_running(fakes, duty)
    print("S1 冷启动->STEADY 通过 (duty=%s)" % duty)
    return duty


def s2_spot_warn_switch():
    r, fakes, store, sj, tg = make_world()
    ticks(r)
    old = steady_duty(sj)
    new = "B" if old == "A" else "A"
    fakes[old].lock_recycling = True    # 注入抢占 5min 预警
    ticks(r)
    duty = steady_duty(sj)
    assert duty == new, f"应切换到 {new}，实际 {duty}"
    assert_one_running(fakes, duty)
    assert any(("当前节点：%s" % new) in x for x in tg.messages), \
        "应有切换完成通知（文本：当前节点：<账号>）"
    print("S2 抢占预警切换 通过 (%s->%s)" % (old, new))


def s3_switch_to_self():
    r, fakes, store, sj, tg = make_world()
    ticks(r)
    duty = steady_duty(sj)
    r.handle_tg_command("switch %s" % duty, 1)   # from==to 自救路径（P0 回归）
    ticks(r)
    assert steady_duty(sj) == duty
    assert fakes[duty].status == "Running", "P0 回归：目标机绝不可被停"
    assert_one_running(fakes, duty)
    print("S3 /switch 当前账号（from==to 不停目标机）通过")


def s4_t5_km_retry():
    r, fakes, store, sj, tg = make_world()
    ticks(r)
    old = steady_duty(sj)
    fakes[old].km_stop_times = 1            # 第一次停机静默退化为普通停机
    r.handle_tg_command("switch", 1)
    ticks(r)
    duty = steady_duty(sj)
    assert duty != old
    assert fakes[old].stopped_mode == "StopCharging", \
        "KM 应被 T4 重发纠偏为 StopCharging"
    assert_one_running(fakes, duty)
    print("S4 T4 普通停机重发纠偏 通过")


def s5_breaker_and_resume():
    r, fakes, store, sj, tg = make_world()
    ticks(r)
    duty = steady_duty(sj)
    for f in fakes.values():                # 全员流量超限
        f.traffic_gb = 999.0
    r.traffic_job()                         # 快照 + 入队 TRAFFIC 意图
    ticks(r)
    st = sj.load()
    assert st.get("global_state") == m.GS_BREAKER, f"应熔断，实际 {st.get('global_state')}"
    assert all(f.status == "Stopped" and f.stopped_mode == "StopCharging"
               for f in fakes.values()), "熔断后全部实例应 STOPPED_SC"
    assert all(f.duty is False for f in fakes.values()), "熔断后全部 duty=off"
    assert any("全局保护" in x for x in tg.messages)
    # 流量恢复 + 月重置 -> 自动拉起
    for f in fakes.values():
        f.traffic_gb = 10.0
    r.traffic_job()                         # 刷新快照（解除 eligible 硬门）
    r.month_reset()
    ticks(r)
    duty2 = steady_duty(sj)
    assert_one_running(fakes, duty2)
    print("S5 全局熔断 + 月重置自动拉起 通过")


def s6_crash_resume():
    r, fakes, store, sj, tg = make_world()
    ticks(r)
    old = steady_duty(sj)
    fakes[old].lock_recycling = True
    r.tick()                                # 启动流水线（游标落盘）
    assert sj.load().get("transition"), "应有进行中游标"
    # 模拟崩溃：丢弃旧 Rotator，用同一 state.json/db/云侧重建
    r2 = m.Rotator(r.cfg, store, sj, tg)
    r2.clients = fakes
    r2._probe_service = lambda a: True
    ticks(r2)
    duty = steady_duty(sj)
    assert duty != old, "崩溃续跑后应完成切换"
    assert_one_running(fakes, duty)
    print("S6 崩溃后游标幂等续跑 通过")


def s7_nostock_backoff():
    r, fakes, store, sj, tg = make_world()
    ticks(r)
    old = steady_duty(sj)
    new = "B" if old == "A" else "A"
    fakes[new].nostock_times = 2            # 新机前两次拉起库存不足
    r.handle_tg_command("switch", 1)
    ticks(r, 40)
    duty = steady_duty(sj)
    assert duty == new, "退避重试后应成功拉起"
    assert fakes[new].nostock_times == 0
    assert_one_running(fakes, duty)
    print("S7 NoStock 退避重试 通过")


def s8_rollback_stops_new():
    r, fakes, store, sj, tg = make_world()
    ticks(r)
    old = steady_duty(sj)
    new = "B" if old == "A" else "A"
    r._probe_service = lambda a: a != new      # 新机服务永远探测失败 -> T3 超时回滚
    r.handle_tg_command("switch", 1)
    tr = None
    for _ in range(30):                         # 等流水线正常推进到 T3 探测阶段
        r.tick()
        tr = sj.load().get("transition")
        if tr and tr["step"] == m.STEP_T3:
            break
    assert tr and tr["step"] == m.STEP_T3, "应已进入 T3 探测阶段"
    tr.setdefault("meta", {})["t3_started_at"] = time.time() - 10 ** 6
    # 拨快 T3 自己的时钟，直接越过服务探测超时
    st = sj.load()
    st["transition"] = tr
    sj.save(st)
    ticks(r)
    assert steady_duty(sj) == old, "回滚后应回到旧机"
    assert fakes[old].status == "Running"
    assert fakes[new].status == "Stopped" and fakes[new].stopped_mode == "StopCharging", \
        "P0 回归：回滚必须停掉新机（rollback 流水线不过 T1，顶层 stop_list 缺失会漏停）"
    assert any("回滚" in x for x in tg.messages)
    print("S8 T3 超时回滚停新机 通过")


def s9_breaker_running_fix():
    r, fakes, store, sj, tg = make_world()
    ticks(r)
    for f in fakes.values():
        f.traffic_gb = 999.0
    r.traffic_job()
    ticks(r)
    assert sj.load().get("global_state") == m.GS_BREAKER
    assert all(f.status == "Stopped" for f in fakes.values())
    # 云侧异常/人工误启：BREAKER 态一台机突然 Running
    bad = "A"
    fakes[bad].status = "Running"
    ticks(r, 5)
    assert fakes[bad].status == "Stopped" and fakes[bad].stopped_mode == "StopCharging", \
        "BREAKER 稳态应把仍在运行的实例纠偏回 STOPPED_SC"
    assert sj.load().get("global_state") == m.GS_BREAKER, "纠偏后应保持熔断态"
    print("S9 BREAKER 稳态运行中实例纠偏 通过")


def s10_cdt_parse():
    G = 1024 ** 3
    # 香港 4GB + 内地 3GB 混合：只统计非内地池，内地不得污染
    r = m._parse_traffic_gb({"TrafficDetails": [
        {"BusinessRegionId": "cn-hangzhou", "Traffic": 1 * G},
        {"BusinessRegionId": "cn-beijing", "Traffic": 2 * G},
        {"BusinessRegionId": "cn-hongkong", "Traffic": 4 * G},
    ]})
    assert r == 4.0, f"混合流量应只计非内地 4GB，实际 {r}"
    # 仅内地流量：权威结构返回 0，不得被兜底扫描把内地流量捡回来
    r = m._parse_traffic_gb({"TrafficDetails": [
        {"BusinessRegionId": "cn-hangzhou", "Traffic": 5 * G}]})
    assert r == 0.0, f"仅内地流量应返回 0，实际 {r}"
    # Data 嵌套结构
    r = m._parse_traffic_gb({"Data": {"TrafficDetails": [
        {"BusinessRegionId": "ap-southeast-1", "Traffic": 2 * G}]}})
    assert r == 2.0, f"Data 嵌套应解析出 2GB，实际 {r}"
    # 空响应 / 非字典必须视为未知，不能伪装成 0GB
    for invalid in ({}, None):
        try:
            m._parse_traffic_gb(invalid)
        except ValueError:
            pass
        else:
            raise AssertionError("空响应不得伪装成 0GB")
    # 结构漂移时没有地域信息，不得推测流量池。
    try:
        m._parse_traffic_gb({"Items": [{"TrafficBytes": 3 * G}]})
    except ValueError:
        pass
    else:
        raise AssertionError("缺少地域明细时必须报错")
    print("S10 CDT 响应分类解析（内地/非内地分池）通过")


def s11_openapi_transport_contract():
    """回归 Tea 通用 OpenAPI 的查询参数、超时、响应包装与标签类型契约。"""
    acc = m.AccountCfg(name="A", region="cn-hongkong", access_key_id="k",
                       access_key_secret="s", instance_id="i-a",
                       eip="1.1.1.1", service_port=443)
    client = m.AliyunAccountClient(acc)

    class FakeOpenAPI:
        def __init__(self):
            self.calls = []

        def call_api(self, params, request, runtime):
            self.calls.append((params, request, runtime))
            return {"body": {"ok": True}, "headers": {}, "statusCode": 200}

    fake = FakeOpenAPI()
    result = client._call(fake, "DescribeInstances", "2014-05-26", {"PageSize": 10})
    _, request, runtime = fake.calls[-1]
    assert request.query["PageSize"] == "10", "RPC 查询参数必须字符串化"
    assert runtime.read_timeout == 10_000 and runtime.connect_timeout == 5_000, \
        "Tea SDK 超时必须使用毫秒"
    assert result == {"ok": True}, "通用 OpenAPI 响应必须解包 body"

    client.ecs_client = fake
    client.set_duty(True)
    assert fake.calls[-1][1].query["ResourceType"] == "instance", \
        "TagResources 的 ResourceType 必须为小写 instance"
    client.set_duty(False)
    assert fake.calls[-1][1].query["ResourceType"] == "instance", \
        "UntagResources 的 ResourceType 必须为小写 instance"
    print("S11 OpenAPI 传输与标签参数契约 通过")


def s12_probe_gate_before_stop():
    """目标服务未连续探测成功前，旧机必须保持 Running。"""
    r, fakes, store, sj, tg = make_world()
    ticks(r)
    old = steady_duty(sj)
    new = "B" if old == "A" else "A"
    results = [False, True, True, True]

    def controlled_probe(account):
        return results.pop(0) if account == new and results else True

    r._probe_service = controlled_probe
    r.handle_tg_command("switch %s" % new, 1)
    for _ in range(10):
        r.tick()
        tr = sj.load().get("transition")
        if tr and tr["step"] == m.STEP_T3:
            break
    assert tr and tr["step"] == m.STEP_T3, "应进入目标服务探测阶段"

    r.tick()  # 失败：连续成功计数归零
    assert fakes[old].status == "Running", "探测失败时旧机不得停机"
    r.tick()  # 成功 1/3
    assert fakes[old].status == "Running", "仅成功 1 次时旧机不得停机"
    r.tick()  # 成功 2/3
    assert fakes[old].status == "Running", "仅成功 2 次时旧机不得停机"
    r.tick()  # 成功 3/3，仅推进游标
    assert sj.load()["transition"]["step"] == m.STEP_T4
    assert fakes[old].status == "Running", "第三次成功所在 tick 也不得越门停机"
    r.tick()  # 下一 tick 才进入 T4 停旧机
    assert fakes[old].status == "Stopped", "连续 3 次成功后才允许停旧机"
    ticks(r)
    assert steady_duty(sj) == new
    assert_one_running(fakes, new)
    print("S12 目标服务连续探测成功后才停旧机 通过")


def s13_v1_cursor_migration():
    """升级时旧版 T5（停机）不能被新版误判成收尾。"""
    r, fakes, store, sj, tg = make_world()
    ticks(r)
    old = steady_duty(sj)
    new = "B" if old == "A" else "A"
    fakes[new].status = "Running"
    sj.save({
        "global_state": m.GS_TRANSITION,
        "duty_account": old,
        "dns": {"record_id": "legacy", "current_eip": fakes[new].cfg.eip},
        "transition": {
            "from": old, "to": new, "step": "T5",
            "started_at": 0, "attempts": {}, "breaker": False,
            "stop_list": [old],
            "meta": {"reason": "upgrade", "stop_done": {},
                     "dns_record_id": "legacy", "target_eip": fakes[new].cfg.eip},
        },
    })
    assert "dns" not in sj.load(), "保存时应清理旧版顶层 DNS 状态"
    r.tick()
    tr = sj.load()["transition"]
    assert tr["version"] == m.TRANSITION_VERSION and tr["step"] == m.STEP_T4
    assert fakes[old].status == "Stopped", "旧版 T5 恢复后仍必须执行停机确认"
    ticks(r)
    assert steady_duty(sj) == new
    assert_one_running(fakes, new)
    print("S13 旧版流水线游标安全迁移 通过")


def s14_tg_traffic_refresh():
    """TG 线程只入队，下一 tick 才串行调用 CDT API 并返回本轮结果。"""
    r, fakes, store, sj, tg = make_world()
    ticks(r)
    fakes["A"].traffic_gb = 12.3
    fakes["B"].traffic_gb = 45.6
    before = {name: f.traffic_calls for name, f in fakes.items()}
    r.handle_tg_command("traffic", 1)
    assert {name: f.traffic_calls for name, f in fakes.items()} == before, \
        "TG 回调线程不得直接调用云 API"
    assert "已收到流量查询请求" in tg.messages[-1]
    r.tick()
    assert all(f.traffic_calls == before[name] + 1 for name, f in fakes.items()), \
        "下一 tick 应对每个账号实时查询一次"
    latest = store.latest_traffic()
    assert latest == {"A": 12.3, "B": 45.6}
    assert "实时流量报告" in tg.messages[-1]
    assert "12.3" in tg.messages[-1] and "45.6" in tg.messages[-1]
    print("S14 /traffic 入队实时刷新 通过")


def s15_unknown_traffic_is_not_zero():
    """无 CDT 快照时显示暂无数据，并阻止该账号成为新目标。"""
    r, fakes, store, sj, tg = make_world(seed_traffic=False)
    assert r._select_target({"A": 0, "B": 0}) is None, "未知流量账号不得进入 eligible"
    r._tg_traffic()
    assert "暂无数据" in tg.messages[-1]
    assert "0.0/" not in tg.messages[-1]
    print("S15 未知流量不再伪装成 0GB 通过")


def s16_fast_transition_loop():
    """控制循环快速推进切换，且不得重复累计 60 秒运行时。"""
    r, fakes, store, sj, tg = make_world()
    ticks(r)
    duty = sj.load()["duty_account"]
    target = "B" if duty == "A" else "A"
    runtime_before = store.get_runtimes().copy()
    r.submit_intent("SWITCH", account=target)

    r.transition_tick()                    # 消费 SWITCH 并通过 T1
    assert sj.load()["transition"]["step"] == m.STEP_T2
    assert fakes[target].status == "Stopped"
    r.transition_tick()                    # T2 立即调用 StartInstance
    assert fakes[target].status == "Running"
    assert store.get_runtimes() == runtime_before, "快速控制循环不得做巡检时长记账"
    for _ in range(12):
        if not sj.load().get("transition"):
            break
        r.transition_tick()
    assert not sj.load().get("transition"), "快速控制循环应能独立完成 T2-T5"
    assert sj.load()["duty_account"] == target
    assert fakes[duty].status == "Stopped" and fakes[duty].stopped_mode == "StopCharging"
    assert store.get_runtimes() == runtime_before, "整条快速流水线不得重复做运行时记账"
    print("S16 独立快速控制循环 通过")


def s17_t3_has_own_timeout_clock():
    """T3 超时必须从进入服务探测时计算，而不是从整条切换创建时计算。"""
    r, fakes, store, sj, tg = make_world()
    ticks(r)
    fakes["B"].status = "Running"  # T3 的前置条件：目标已启动
    old = time.time() - 10 ** 6
    sj.save({
        "global_state": m.GS_TRANSITION,
        "duty_account": "A",
        "transition": {
            "version": m.TRANSITION_VERSION,
            "from": "A", "to": "B", "step": m.STEP_T3,
            "started_at": old, "attempts": {}, "breaker": False,
            "stop_list": ["A"],
            "meta": {"reason": "manual", "t3_success": 0, "t3_last": 0},
        },
        "accounts": {},
    })
    r.transition_tick()
    tr = sj.load()["transition"]
    assert tr["step"] == m.STEP_T3, "旧的流水线 started_at 不应让刚进入 T3 就超时"
    assert tr["meta"]["t3_success"] == 1
    assert tr["meta"]["t3_started_at"] > old
    print("S17 T3 独立超时计时 通过")


def s18_tg_retry_and_token_redaction():
    """TG 网络失败应异步补发，且任何异常文本不得泄露 bot token。"""
    token = "123456:TOP_SECRET_TOKEN"
    tg = m.TGClient(m.TGCfg(bot_token=token, chat_ids=[123]))
    tg.RETRY_BASE_SEC = 0.01
    tg.RETRY_MAX_SEC = 0.02
    tg.WARN_INTERVAL_SEC = 0.01
    calls = []
    original_post = m.requests.post

    class OkResponse:
        ok = True
        status_code = 200

    def flaky_post(url, **kwargs):
        calls.append((url, kwargs))
        if len(calls) == 1:
            raise m.requests.ConnectionError(f"failed URL {url}")
        return OkResponse()

    try:
        m.requests.post = flaky_post
        tg.send("切换完成")
        deadline = time.time() + 1.0
        while len(calls) < 2 and time.time() < deadline:
            time.sleep(0.01)
        assert len(calls) >= 2, "首次网络失败后应从队列自动补发"
        leaked = tg._safe_error(Exception(f"https://api.telegram.org/bot{token}/sendMessage"))
        assert token not in leaked and "<redacted>" in leaked
    finally:
        tg.stop()
        m.requests.post = original_post
    print("S18 TG 自动补发与 token 脱敏 通过")


def s19_check_matches_daily_summary():
    """每日任务保留 /check 状态正文，并附加账单。"""
    r, fakes, store, sj, tg = make_world()
    r.cfg.billing.enabled = True
    ticks(r)
    store.add_traffic("A", 999.0)
    r._tg_check()
    manual = tg.messages[-1]
    with patch.object(m.billing, "query_overview", return_value=[]):
        r.daily_report()
    daily = tg.messages[-1]
    daily_status, bill = daily.split("\n\n💰 本月账单", 1)
    assert manual.splitlines()[1:] == daily_status.splitlines()[1:]
    assert "暂无账单条目" in bill
    assert manual.startswith("📋 运行状态报告")
    assert daily.startswith("🕘 每日运行报告")
    assert "系统状态：" in manual and "当前节点：" in manual
    assert "状态：" in manual and "本月流量已达到保护阈值" in manual
    assert "obs=" not in manual
    print("S19 每日汇总保留状态正文并附加账单 通过")


def s20_n_accounts_and_instance_names():
    """四账号完整切换收敛；TG 以实例友好名为主并保留内部账号键。"""
    names = {"A": "香港一号", "B": "东京二号", "C": "新加坡三号", "D": "首尔四号"}
    r, fakes, store, sj, tg = make_world(
        account_names=("A", "B", "C", "D"), instance_names=names,
    )
    ticks(r)
    r.submit_intent("SWITCH", account="D")
    for _ in range(16):
        r.transition_tick()
        if not sj.load().get("transition"):
            break
    assert sj.load()["duty_account"] == "D"
    assert fakes["D"].status == "Running"
    assert all(fakes[a].status == "Stopped" for a in ("A", "B", "C"))
    r._tg_check()
    report = tg.messages[-1]
    assert all(f"{friendly} [{key}]" in report for key, friendly in names.items())

    # 未在 YAML 指定时，首次 ECS 观测得到的 InstanceName 也应成为显示名。
    r2, f2, store2, sj2, tg2 = make_world()
    f2["A"].cloud_instance_name = "云端实例名称"
    ticks(r2, 1)
    assert r2._account_label("A") == "云端实例名称 [A]"
    print("S20 N 账号切换与实例名称显示 通过")


def s21_tg_messages_are_human_friendly():
    """所有 TG 展示层都应使用中文语义，不得泄漏内部状态码或字段名。"""
    names = {"A": "香港主节点", "B": "东京备用节点"}
    r, fakes, store, sj, tg = make_world(instance_names=names)
    ticks(r)
    r._tg_check()
    r._tg_traffic()
    r.handle_tg_command("switch B", 1)
    ticks(r)

    # 旧版本已写入数据库的事件也必须在 /last 展示时转换成人类可读文本。
    store.log_event("临时调度", "启动切换流水线 from=A to=B reason=manual")
    store.log_event("切换异常", "T3 探测 B 超时，obs=RUNNING")
    r._tg_last()

    rendered = "\n".join(tg.messages)
    forbidden = (
        "obs=", "from=", "to=", "reason=", "RUNNING", "STARTING",
        "STOPPING", "STOPPED_SC", "STOPPED_KM", "NOT_FOUND",
        "DEGRADED_ZERO", "DEGRADED_MULTI", "BREAKER",
    )
    assert all(token not in rendered for token in forbidden), rendered
    assert not re.search(r"\bT[1-5]\b", rendered)
    assert "香港主节点 [A]" in rendered and "东京备用节点 [B]" in rendered
    assert any(icon in rendered for icon in ("🟢", "🔄", "✅", "📊"))
    print("S21 TG 文案中文化与旧事件兼容 通过")


def s22_observation_failure_is_not_not_found():
    """云 API 异常必须冻结自动启停，绝不能冒充实例已回收。"""
    r, fakes, store, sj, tg = make_world()
    ticks(r)
    duty = steady_duty(sj)
    before = {name: (f.status, f.duty) for name, f in fakes.items()}
    original = fakes[duty].get_instance_obs

    def fail_observation():
        raise m.CloudAPIError("mock API unavailable")

    fakes[duty].get_instance_obs = fail_observation
    r.tick()
    st = sj.load()
    assert st["accounts"][duty]["last_observed"] == m.OBS_UNKNOWN
    assert st.get("transition") is None
    assert {name: (f.status, f.duty) for name, f in fakes.items()} == before
    assert any("暂停自动启停" in msg for msg in tg.messages)

    # T4 看到 UNKNOWN 也不能把旧机标成已停。
    tr = {
        "version": m.TRANSITION_VERSION, "from": duty, "to": duty,
        "step": m.STEP_T4, "started_at": time.time(), "attempts": {},
        "breaker": False, "stop_list": [duty],
        "meta": {"reason": "manual", "stop_done": {}, "next_retry_at": time.time() + 999},
    }
    out = r._gate_t4(tr, {duty: m.InstanceObs(exists=None, obs=m.OBS_UNKNOWN)}, st)
    assert not out["transition"]["meta"]["stop_done"].get(duty)
    fakes[duty].get_instance_obs = original
    print("S22 观测异常与实例不存在严格区分 通过")


def s23_database_latest_stale_and_retention():
    """最新流量按 id 唯一取值，过期快照不可参与调度，历史可清理。"""
    d = tempfile.mkdtemp()
    store = m.StateStore(os.path.join(d, "r.db"))
    with store._lock:
        store._conn.executemany(
            "INSERT INTO traffic_snapshots (ts, account, gb) VALUES (?, ?, ?)",
            [
                ("2000-01-01T00:00:00", "A", 1.0),
                (m.now_iso(), "A", 2.0),
                (m.now_iso(), "A", 3.0),
            ],
        )
        store._conn.execute(
            "INSERT INTO events (ts, kind, message) VALUES (?, ?, ?)",
            ("2000-01-01T00:00:00", "old", "old"),
        )
        store._conn.execute(
            "INSERT INTO runtime_counters (account, month, runtime_seconds) VALUES (?, ?, ?)",
            ("A", "2000-01", 1),
        )
        store._conn.commit()
    assert store.latest_traffic()["A"] == 3.0, "同秒快照应按最大 id 取唯一最新值"
    assert store.latest_traffic(max_age_sec=3600)["A"] == 3.0
    deleted = store.cleanup_history(180, 180, 24)
    assert deleted["traffic"] == 1 and deleted["events"] == 1 and deleted["runtime"] == 1
    assert store.latest_traffic()["A"] == 3.0
    with store._lock:
        store._conn.execute(
            "INSERT INTO traffic_snapshots (ts, account, gb) VALUES (?, ?, ?)",
            ("2099-01-01T00:00:00", "A", 4.0),
        )
        store._conn.commit()
    assert "A" not in store.latest_traffic(max_age_sec=3600), "未来时间戳不得视为新鲜"
    store.close()

    r, fakes, store2, sj, tg = make_world()
    ticks(r)
    with store2._lock:
        store2._conn.execute("UPDATE traffic_snapshots SET ts='2000-01-01T00:00:00'")
        store2._conn.commit()
    assert r._select_target(store2.get_runtimes()) is None
    r.tick()
    assert sj.load().get("transition") is None
    assert any("流量数据已超过" in msg for msg in tg.messages)
    print("S23 数据库最新查询、过期保护与历史清理 通过")


def s24_bounded_queues_and_alert_throttle():
    """网络断开或异常持续时，消息队列有硬上限且同类告警不会每分钟重复。"""
    tg_client = m.TGClient(m.TGCfg(bot_token="x", chat_ids=[1]))
    tg_client._stop_event.set()  # 不启动发送线程，仅验证内存队列容量
    for i in range(tg_client.MAX_QUEUE_SIZE + 25):
        tg_client.send(f"message {i}")
    assert tg_client._send_queue.qsize() == tg_client.MAX_QUEUE_SIZE

    r, fakes, store, sj, tg = make_world()
    ticks(r)
    duty = steady_duty(sj)
    tg.messages.clear()
    original = fakes[duty].get_instance_obs

    def eip_missing():
        ob = original()
        ob.eip_bound = False
        return ob

    fakes[duty].get_instance_obs = eip_missing
    ticks(r, 10)
    assert sum("公网 IP" in msg for msg in tg.messages) == 1
    print("S24 队列限长与重复告警节流 通过")


def s25_state_json_last_good_and_write_failure():
    """运行中状态文件损坏时回退最后有效副本，写失败必须向上抛出。"""
    d = tempfile.mkdtemp()
    path = os.path.join(d, "state.json")
    sj = m.StateJson(path)
    sj.save({"global_state": m.GS_STEADY, "duty_account": "A"})
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{broken")
    recovered = sj.load()
    assert recovered["global_state"] == m.GS_STEADY and recovered["duty_account"] == "A"

    bad = m.StateJson(os.path.join(d, "missing", "state.json"))
    try:
        bad.save({"global_state": m.GS_ZERO})
    except OSError:
        pass
    else:
        raise AssertionError("state.json 写失败必须向上抛出")
    print("S25 state.json 最后有效状态与写失败保护 通过")


def s26_month_reset_is_serialized_intent():
    """月度重置只能经由状态机队列执行，不得与切换线程并发写状态。"""
    r, fakes, store, sj, tg = make_world()
    ticks(r)
    duty = steady_duty(sj)
    store.set_runtime(duty, 1234)
    r.month_reset()
    assert store.get_runtimes()[duty] == 1234, "调度器线程不得直接重置"
    assert r.intents.qsize() == 1
    r.transition_tick()
    assert all(value == 0 for value in store.get_runtimes().values())
    print("S26 月度重置进入单写者队列 通过")


def s27_notification_error_branches():
    """标签回读异常和非法节点请求必须可告警，不得因告警代码本身崩溃。"""
    r, fakes, store, sj, tg = make_world()
    ticks(r)
    current = steady_duty(sj)
    target = next(name for name in fakes if name != current)

    # 模拟云端接受 SetTag 请求但回读仍为 false。
    fakes[current].status = "Stopped"
    fakes[current].stopped_mode = "StopCharging"
    fakes[target].status = "Running"
    fakes[target].set_duty = lambda on: None
    tr = {
        "version": m.TRANSITION_VERSION, "from": current, "to": target,
        "step": m.STEP_T5, "started_at": time.time(), "attempts": {},
        "breaker": False, "stop_list": [current],
        "meta": {"reason": "manual", "stop_done": {current: True}},
    }
    out = r._gate_t5(tr, r._observe_all(), sj.load())
    assert out.get("transition"), "标签未确认时不得结束流水线"
    assert any("当班标签尚未确认更新" in msg for msg in tg.messages)

    # 手动输入不存在的节点键时应友好拒绝，且不创建流水线。
    r._start_transition(to="NOT-A-NODE", reason="manual")
    assert sj.load().get("transition") is None
    assert any("找不到节点键" in msg for msg in tg.messages)
    print("S27 异常告警分支不崩溃 通过")


def s28_config_validation():
    """会造成误调度或永久失效的配置应在启动前被拒绝。"""
    r, fakes, store, sj, tg = make_world()
    account = r.cfg.accounts["A"]
    thresholds = m.Thresholds()

    account.service_port = 0
    try:
        m._validate_config(r.cfg.accounts, thresholds)
    except ValueError as exc:
        assert "service_port" in str(exc)
    else:
        raise AssertionError("非法服务端口必须被拒绝")
    account.service_port = 443

    try:
        m._validate_config({" A ": account}, thresholds)
    except ValueError as exc:
        assert "首尾空格" in str(exc)
    else:
        raise AssertionError("含首尾空格的账号键必须被拒绝")

    try:
        m._validate_config({}, thresholds)
    except ValueError as exc:
        assert "accounts 为空" in str(exc)
    else:
        raise AssertionError("空账号配置必须被拒绝")

    thresholds.traffic_threshold_gb = float("nan")
    try:
        m._validate_config(r.cfg.accounts, thresholds)
    except ValueError as exc:
        assert "流量阈值" in str(exc)
    else:
        raise AssertionError("非有限阈值必须被拒绝")
    print("S28 启动配置校验 通过")


def s29_per_account_stop_retry():
    """T4 应在同一轮处理全部旧机，并按实例分别限制重试频率。"""
    r, fakes, store, sj, tg = make_world(account_names=("A", "B", "C"))
    r.cfg.th.stop_retry_interval_sec = 300
    fakes["C"].status = "Running"
    for name in ("A", "B"):
        fakes[name].status = "Stopped"
        fakes[name].stopped_mode = "KeepCharging"
        fakes[name].km_stop_times = 10
    tr = {
        "version": m.TRANSITION_VERSION, "from": "C", "to": "C",
        "step": m.STEP_T4, "started_at": time.time(), "attempts": {},
        "breaker": False, "stop_list": ["A", "B"],
        "meta": {"reason": "cleanup_multi", "stop_done": {}},
    }
    obs = r._observe_all()
    out = r._gate_t4(tr, obs, {})
    assert fakes["A"].stop_calls == 1 and fakes["B"].stop_calls == 1
    r._gate_t4(out["transition"], r._observe_all(), out)
    assert fakes["A"].stop_calls == 1 and fakes["B"].stop_calls == 1
    print("S29 多实例独立停机与 API 重试节流 通过")


def s30_target_lost_before_t5_completion():
    """目标在 T3 之后被回收时不得被宣布为新的稳定当班节点。"""
    r, fakes, store, sj, tg = make_world()
    ticks(r)
    current = steady_duty(sj)
    target = next(name for name in fakes if name != current)
    fakes[target].exists = False
    tr = {
        "version": m.TRANSITION_VERSION, "from": current, "to": target,
        "step": m.STEP_T5, "started_at": time.time(), "attempts": {},
        "breaker": False, "stop_list": [current],
        "meta": {"reason": "manual", "stop_done": {current: True}},
    }
    out = r._gate_t5(tr, r._observe_all(), sj.load())
    assert out["global_state"] == m.GS_ZERO
    assert out.get("transition") is None and out.get("duty_account") is None
    assert not any("切换完成" in msg for msg in tg.messages[-1:])
    print("S30 收尾前目标丢失不误报成功 通过")


def main():
    s1_cold_start()
    s2_spot_warn_switch()
    s3_switch_to_self()
    s4_t5_km_retry()
    s5_breaker_and_resume()
    s6_crash_resume()
    s7_nostock_backoff()
    s8_rollback_stops_new()
    s9_breaker_running_fix()
    s10_cdt_parse()
    s11_openapi_transport_contract()
    s12_probe_gate_before_stop()
    s13_v1_cursor_migration()
    s14_tg_traffic_refresh()
    s15_unknown_traffic_is_not_zero()
    s16_fast_transition_loop()
    s17_t3_has_own_timeout_clock()
    s18_tg_retry_and_token_redaction()
    s19_check_matches_daily_summary()
    s20_n_accounts_and_instance_names()
    s21_tg_messages_are_human_friendly()
    s22_observation_failure_is_not_not_found()
    s23_database_latest_stale_and_retention()
    s24_bounded_queues_and_alert_throttle()
    s25_state_json_last_good_and_write_failure()
    s26_month_reset_is_serialized_intent()
    s27_notification_error_branches()
    s28_config_validation()
    s29_per_account_stop_retry()
    s30_target_lost_before_t5_completion()
    print("\n全部 30 个场景通过 ✅")


if __name__ == "__main__":
    main()
