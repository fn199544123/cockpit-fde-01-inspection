"""灌入高度脱敏的演示数据。

用法(项目根目录):
     # 在项目虚拟环境执行
    .venv/bin/python scripts/seed_demo.py

铁律:严禁真实企业/人名/地名。全部使用「某企业 / 一车间 / 张师傅(化名)」等占位。
本脚本可重复运行:按编号/名称 get_or_create,并重置点检记录以命中各计划分档。
"""

import os
import random
import sys
from datetime import datetime, time as dtime, timedelta

import django

# 让脚本在项目根直接运行
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'inspection.settings')
django.setup()

from django.utils import timezone  # noqa: E402

from board.models import (  # noqa: E402
    CheckItem, CheckRecord, CheckResult, CheckTemplate, CheckTemplateItem, Equipment,
    WorkOrder,
)


# --- 点检标准模板(脱敏) -------------------------------------------------
TEMPLATES = [
    {
        'name': '注塑机通用点检标准',
        'category': '注塑机',
        'description': '开机前逐项目视/手感检查,异常即报修',
        'items': [
            ('液压油位是否正常', '油标 1/2 ~ 2/3'),
            ('模具锁模力是否正常', '≤ 额定值'),
            ('冷却水管有无渗漏', '无渗漏'),
            ('急停按钮是否有效', '按下即停'),
            ('电控柜有无异响异味', '无异响无焦味'),
        ],
    },
    {
        'name': '空压机日常点检标准',
        'category': '空压机',
        'description': '每班次点检,关注压力与温度',
        'items': [
            ('排气压力是否在区间', '0.6 ~ 0.8 MPa'),
            ('润滑油位是否正常', '油窗中线附近'),
            ('排气温度是否正常', '≤ 95℃'),
            ('皮带松紧是否合适', '压下约 10mm'),
        ],
    },
    {
        'name': '数控机床点检标准',
        'category': '数控机床',
        'description': '开机前点检,关注导轨润滑与主轴',
        'items': [
            ('导轨润滑油位是否正常', '油标线以上'),
            ('主轴运转有无异响', '无异响'),
            ('冷却液液位是否充足', '≥ 液位下限'),
            ('气压是否正常', '0.5 ~ 0.7 MPa'),
            ('防护门联锁是否有效', '开门即停'),
        ],
    },
    {
        'name': '行车(起重机)点检标准',
        'category': '行车',
        'description': '每班使用前点检,关注制动与钢丝绳',
        'items': [
            ('钢丝绳有无断丝毛刺', '无断丝'),
            ('制动器是否可靠', '空载试刹有效'),
            ('限位开关是否有效', '触发即停'),
            ('吊钩防脱装置是否完好', '完好无变形'),
            ('声光报警是否正常', '行走有声光'),
        ],
    },
    {
        'name': '包装线点检标准',
        'category': '包装线',
        'description': '每班点检,关注传送与封口温度',
        'items': [
            ('传送皮带有无跑偏', '居中运行'),
            ('封口温度是否正常', '160 ~ 180℃'),
            ('光电传感器是否灵敏', '遮挡即触发'),
            ('急停按钮是否有效', '按下即停'),
        ],
    },
    {
        'name': '叉车日常点检标准',
        'category': '叉车',
        'description': '出车前点检,关注制动与液压',
        'items': [
            ('制动是否灵敏', '踩下即制动'),
            ('液压油位是否正常', '油标 1/2 以上'),
            ('轮胎气压/磨损是否正常', '无明显磨损'),
            ('喇叭与灯光是否正常', '正常发声发光'),
            ('货叉有无裂纹变形', '无裂纹'),
        ],
    },
]


# --- 设备台账(脱敏) + 计划分档场景 --------------------------------------
# scenario 决定最近一次点检时间,从而命中不同「本期应点检」分档:
#   never   从未点检   overdue 已超期   due 今日应检   soon 即将到期   ok 未到期
EQUIPMENTS = [
    # code,          name,      workshop, cycle, owner,         scenario
    # 一车间:注塑 + 空压机群
    ('EQ-A-001', '注塑机',   '一车间', 1, '张师傅(化名)', 'overdue'),
    ('EQ-A-002', '注塑机',   '一车间', 3, '李师傅(化名)', 'due'),
    ('EQ-A-003', '空压机',   '一车间', 7, '王师傅(化名)', 'ok'),
    ('EQ-A-004', '注塑机',   '一车间', 2, '张师傅(化名)', 'soon'),
    ('EQ-A-005', '注塑机',   '一车间', 1, '李师傅(化名)', 'due'),
    ('EQ-A-006', '空压机',   '一车间', 7, '王师傅(化名)', 'ok'),
    ('EQ-A-007', '数控机床', '一车间', 2, '钱师傅(化名)', 'overdue'),
    # 二车间:机加工 + 起重
    ('EQ-B-001', '数控机床', '二车间', 2, '赵师傅(化名)', 'never'),
    ('EQ-B-002', '行车',     '二车间', 5, '孙师傅(化名)', 'soon'),
    ('EQ-B-003', '空压机',   '二车间', 7, '周师傅(化名)', 'overdue'),
    ('EQ-B-004', '数控机床', '二车间', 2, '赵师傅(化名)', 'due'),
    ('EQ-B-005', '数控机床', '二车间', 3, '冯师傅(化名)', 'ok'),
    ('EQ-B-006', '行车',     '二车间', 5, '孙师傅(化名)', 'ok'),
    ('EQ-B-007', '叉车',     '二车间', 3, '陈师傅(化名)', 'overdue'),
    # 三车间:包装 + 物流
    ('EQ-C-001', '包装线',   '三车间', 1, '吴师傅(化名)', 'ok'),
    ('EQ-C-002', '叉车',     '三车间', 3, '郑师傅(化名)', 'due'),
    ('EQ-C-003', '包装线',   '三车间', 1, '吴师傅(化名)', 'soon'),
    ('EQ-C-004', '包装线',   '三车间', 2, '褚师傅(化名)', 'overdue'),
    ('EQ-C-005', '叉车',     '三车间', 3, '郑师傅(化名)', 'never'),
    ('EQ-C-006', '空压机',   '三车间', 7, '卫师傅(化名)', 'ok'),
]

# 今日异常演示:key=设备编号,value=(异常项数, 异常说明)。
# 仅对「今日已点检(scenario=ok,记录落在今天)」的设备生效,不影响红牌/计划分档场景。
# 让今日异常 TOP 与工单状态分布有真实数据可展示。
ABNORMAL_TODAY = {
    'EQ-A-003': (2, ['油位偏低,低于油窗中线', '排气温度偏高约 98℃']),
    'EQ-C-001': (1, ['传送皮带有轻微跑偏']),
    'EQ-A-006': (1, ['皮带偏松,压下约 15mm']),
    'EQ-B-005': (2, ['导轨润滑油位偏低', '冷却液液位接近下限']),
    'EQ-B-006': (1, ['钢丝绳局部有毛刺,建议关注']),
    'EQ-C-006': (1, ['排气压力略低,约 0.55 MPa']),
}


def checked_at_for(scenario, cycle):
    """按场景反推最近一次点检时间,使 next_due = checked_at + cycle 落在目标分档。"""
    now = timezone.now()
    if scenario == 'overdue':
        return now - timedelta(days=cycle + 3)          # 超期约 3 天
    if scenario == 'due':
        return now - timedelta(days=cycle) + timedelta(hours=6)  # 半天内到期 → 今日应检
    if scenario == 'soon':
        return now - timedelta(days=cycle - 2)          # 约 2 天后到期 → 即将到期
    if scenario == 'ok':
        return now - timedelta(hours=6)                 # 刚点检过 → 未到期
    return None  # never


def _spread_workorder_status():
    """把「今日新建」的工单铺到 待处理/处理中/已完成 三档,让大屏工单分布有层次。

    只处理今天(scenario 异常派生)的工单,历史工单的状态/完成时间保持
    seed_history 造好的样子(供燃尽图使用),避免被这里轮转覆盖。
    保持幂等:每次按创建顺序轮转赋状态(pending→processing→done→...)。
    """
    cycle = [WorkOrder.PENDING, WorkOrder.PROCESSING, WorkOrder.DONE]
    today = timezone.localdate()
    today_wos = [wo for wo in WorkOrder.objects.order_by('created_at', 'id')
                 if timezone.localtime(wo.created_at).date() == today]
    for i, wo in enumerate(today_wos):
        wo.status = cycle[i % len(cycle)]
        wo.save()  # save() 会按状态自动写/清 finished_at


# --- 14 天历史(供大屏趋势/燃尽图有信号) --------------------------------
HISTORY_DAYS = 14


def seed_history(now):
    """为启用设备补造过去 ~14 天的点检历史与维修工单,让大屏的
    「完成率趋势 / 每日点检量 / 工单燃尽」等趋势图有真实曲线可看。

    关键约束:所有历史记录都必须比该设备「场景记录(最近一次)」更早,
    从而不改变已算好的 计划分档 / 红牌判定;`never` 设备保持零记录。
    确定性随机(固定种子),脚本可重复运行——每次先清历史再重造。
    """
    random.seed(20260923)
    today = timezone.localdate()

    for code, name, workshop, cycle, owner, scenario in EQUIPMENTS:
        if scenario == 'never':
            continue  # 从未点检:保持零记录
        eq = Equipment.objects.get(code=code)
        latest = eq.records.order_by('-checked_at').first()
        latest_date = (timezone.localtime(latest.checked_at).date()
                       if latest else today)
        items = list(eq.items.all())
        step = max(cycle, 1)

        offset = step
        while offset <= HISTORY_DAYS:
            d = today - timedelta(days=offset)
            offset += step
            if d >= latest_date:
                continue  # 只造比场景记录更早的历史,保住最近一次点检
            ca = timezone.make_aware(datetime.combine(d, dtime(10, 0)))
            rec = CheckRecord.objects.create(
                equipment=eq, inspector=owner, checked_at=ca,
            )
            abnormal = bool(items) and random.random() < 0.22
            for idx, it in enumerate(items):
                if abnormal and idx == 0:
                    res = CheckResult.objects.create(
                        record=rec, item=it, status=CheckResult.ABNORMAL,
                        note='历史点检发现异常(演示)',
                    )
                    wo = WorkOrder.from_result(res)
                    # 工单创建时间回填到当天(created_at 为 auto_now_add,需 update 绕过)
                    WorkOrder.objects.filter(pk=wo.pk).update(created_at=ca)
                    # 约 70% 的历史工单在数天内闭环,让燃尽曲线有升有降
                    if random.random() < 0.7:
                        fin = ca + timedelta(days=random.randint(1, 3))
                        if fin < now:
                            WorkOrder.objects.filter(pk=wo.pk).update(
                                status=WorkOrder.DONE, finished_at=fin,
                            )
                else:
                    CheckResult.objects.create(
                        record=rec, item=it, status=CheckResult.NORMAL,
                    )
            if abnormal:
                rec.has_abnormal = True
                rec.save(update_fields=['has_abnormal'])


def run():
    # 模板
    for spec in TEMPLATES:
        tpl, _ = CheckTemplate.objects.get_or_create(
            name=spec['name'],
            defaults={'category': spec['category'], 'description': spec['description']},
        )
        tpl.category = spec['category']
        tpl.description = spec['description']
        tpl.is_active = True
        tpl.save()
        tpl.items.all().delete()
        for i, (name, std) in enumerate(spec['items'], start=1):
            CheckTemplateItem.objects.create(template=tpl, name=name, standard=std, order=i)
    print(f'✓ 点检标准模板 {CheckTemplate.objects.count()} 份')

    # 便于按设备类型套用模板项
    tpl_by_cat = {t.category: t for t in CheckTemplate.objects.all()}

    for code, name, workshop, cycle, owner, scenario in EQUIPMENTS:
        eq, _ = Equipment.objects.get_or_create(
            code=code,
            defaults={'name': name, 'workshop': workshop,
                      'cycle_days': cycle, 'owner': owner},
        )
        eq.name, eq.workshop, eq.cycle_days, eq.owner = name, workshop, cycle, owner
        eq.is_active = True
        eq.save()

        # 套用对应模板的点检项到设备(幂等:同名跳过)
        tpl = tpl_by_cat.get(name) or tpl_by_cat.get('注塑机')
        if tpl:
            existing = set(eq.items.values_list('name', flat=True))
            order = eq.items.count()
            for titem in tpl.items.all():
                if titem.name in existing:
                    continue
                order += 1
                CheckItem.objects.create(
                    equipment=eq, name=titem.name, standard=titem.standard, order=order,
                )

        # 重置点检记录 + 由其派生的工单,按场景造一条历史记录
        WorkOrder.objects.filter(equipment=eq).delete()
        eq.records.all().delete()
        ca = checked_at_for(scenario, cycle)
        if ca is not None:
            rec = CheckRecord.objects.create(equipment=eq, inspector=owner, checked_at=ca)
            # 今日异常配置:仅当本条记录确实落在今天时,前 n 项标为异常
            abn_n, abn_notes = 0, []
            if timezone.localtime(ca).date() == timezone.localdate():
                abn_n, abn_notes = ABNORMAL_TODAY.get(code, (0, []))
            items = list(eq.items.all())
            for idx, it in enumerate(items):
                if idx < abn_n:
                    note = abn_notes[idx] if idx < len(abn_notes) else '点检发现异常'
                    res = CheckResult.objects.create(
                        record=rec, item=it, status=CheckResult.ABNORMAL, note=note,
                    )
                    # 异常项一键派修:生成工单,让工单状态分布有数据
                    WorkOrder.from_result(res)
                else:
                    CheckResult.objects.create(record=rec, item=it, status=CheckResult.NORMAL)
            if abn_n:
                rec.has_abnormal = True
                rec.save(update_fields=['has_abnormal'])

    # 补造 14 天点检/工单历史(趋势图信号),再铺开今日工单状态分布
    seed_history(timezone.now())
    _spread_workorder_status()

    print(f'✓ 设备台账 {Equipment.objects.count()} 台 · '
          f'点检项 {CheckItem.objects.count()} 条 · 历史记录 {CheckRecord.objects.count()} 条 · '
          f'工单 {WorkOrder.objects.count()} 单')

    # 分档概览
    from collections import Counter
    c = Counter(e.plan_status for e in Equipment.objects.filter(is_active=True))
    print('✓ 本期计划分档:', dict(c))


if __name__ == '__main__':
    run()
