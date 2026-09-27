"""设备点检工单看板 —— 核心数据模型

闭环:设备台账(Equipment) → 点检项(CheckItem) → 扫码点检(CheckRecord/CheckResult)
      → 异常派修工单(WorkOrder) → 超期红牌预警。

所有演示数据一律脱敏(某企业 / 一车间 / 张师傅(化名) 等占位),严禁真实企业/人名/地名。
"""

import math
import uuid
from datetime import timedelta

from django.db import models
from django.utils import timezone


class Equipment(models.Model):
    """设备台账:一台设备一条记录,携带自己的点检二维码 token。"""

    code = models.CharField('设备编号', max_length=32, unique=True)
    name = models.CharField('设备名称', max_length=100)
    workshop = models.CharField('所在车间', max_length=50, db_index=True)
    # 点检周期(天):到期未点检即触发超期红牌
    cycle_days = models.PositiveIntegerField('点检周期(天)', default=1)
    owner = models.CharField('负责人', max_length=50, blank=True)
    # 扫码点检入口凭证:每台设备一枚二维码,内容指向 /inspect/<qr_token>/
    qr_token = models.CharField('二维码token', max_length=32, unique=True, editable=False)
    is_active = models.BooleanField('启用', default=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        verbose_name = verbose_name_plural = '设备'
        ordering = ['workshop', 'code']

    def __str__(self):
        return f'{self.code} {self.name}'

    def save(self, *args, **kwargs):
        if not self.qr_token:
            self.qr_token = uuid.uuid4().hex[:16]
        super().save(*args, **kwargs)

    @property
    def last_record(self):
        """最近一次点检记录(可能为空)。"""
        return self.records.order_by('-checked_at').first()

    @property
    def next_due_at(self):
        """下次应点检时间;从未点检则返回 None(视为立即到期)。"""
        rec = self.last_record
        if rec is None:
            return None
        return rec.checked_at + timedelta(days=self.cycle_days)

    @property
    def is_overdue(self):
        """是否超期未点检(红牌)。从未点检也算超期。"""
        due = self.next_due_at
        if due is None:
            return True
        return timezone.now() > due

    @property
    def days_until_due(self):
        """距下次应点检还有几天。负数=已超期;None=从未点检(视为立即到期)。"""
        due = self.next_due_at
        if due is None:
            return None
        delta = due - timezone.now()
        # 向下取整到天:剩 0.3 天算"今天到期(0)",-0.3 天算"超期(-1)"
        return math.floor(delta.total_seconds() / 86400)

    @property
    def overdue_days(self):
        """已超期天数(未超期或从未点检返回 0 / None 语义由调用方处理)。"""
        due = self.next_due_at
        if due is None:
            return None  # 从未点检
        delta = timezone.now() - due
        return max(0, math.ceil(delta.total_seconds() / 86400))

    # 「即将到期」预警窗口(天):未来这么多天内到期的,提前进本期计划提醒
    SOON_WINDOW_DAYS = 2

    @property
    def plan_status(self):
        """本期点检计划分档:
        never   从未点检(最高优先,立即应检)
        overdue 已超期(红牌)
        due     今日应检
        soon    即将到期(未来 SOON_WINDOW_DAYS 天内)
        ok      未到期
        """
        due = self.next_due_at
        if due is None:
            return 'never'
        days = self.days_until_due
        if days < 0:
            return 'overdue'
        if days == 0:
            return 'due'
        if days <= self.SOON_WINDOW_DAYS:
            return 'soon'
        return 'ok'

    @property
    def should_check_now(self):
        """是否属于「本期应点检」(从未点检 / 超期 / 今日应检 / 即将到期)。"""
        return self.plan_status in ('never', 'overdue', 'due', 'soon')


class CheckTemplate(models.Model):
    """点检标准模板:一份可复用的标准点检清单(如"注塑机通用点检标准")。

    维护一次即可批量套用到多台同类设备,套用时把模板项复制成各设备自己的
    CheckItem。设备与模板不做强绑定,套用后可各自增删,互不影响。
    """

    name = models.CharField('模板名称', max_length=100, unique=True)
    category = models.CharField('适用设备类型', max_length=50, blank=True,
                                help_text='如 注塑机 / 空压机(仅作分类提示)')
    description = models.CharField('说明', max_length=200, blank=True)
    is_active = models.BooleanField('启用', default=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        verbose_name = verbose_name_plural = '点检标准模板'
        ordering = ['-is_active', 'name']

    def __str__(self):
        return self.name

    @property
    def item_count(self):
        return self.items.count()


class CheckTemplateItem(models.Model):
    """模板中的一个标准点检项。套用时复制到设备的 CheckItem。"""

    template = models.ForeignKey(
        CheckTemplate, on_delete=models.CASCADE, related_name='items', verbose_name='模板'
    )
    name = models.CharField('点检项', max_length=100)
    standard = models.CharField('标准值/说明', max_length=200, blank=True)
    order = models.PositiveIntegerField('排序', default=0)

    class Meta:
        verbose_name = verbose_name_plural = '模板点检项'
        ordering = ['template', 'order', 'id']

    def __str__(self):
        return f'{self.template.name} - {self.name}'


class CheckItem(models.Model):
    """点检项:某台设备点检表单中的一行(如"油位是否正常")。"""

    equipment = models.ForeignKey(
        Equipment, on_delete=models.CASCADE, related_name='items', verbose_name='设备'
    )
    name = models.CharField('点检项', max_length=100)
    # 标准/参考值,给点检人看的提示
    standard = models.CharField('标准值/说明', max_length=200, blank=True)
    order = models.PositiveIntegerField('排序', default=0)
    is_active = models.BooleanField('启用', default=True)

    class Meta:
        verbose_name = verbose_name_plural = '点检项'
        ordering = ['equipment', 'order', 'id']

    def __str__(self):
        return f'{self.equipment.code} - {self.name}'


class CheckRecord(models.Model):
    """一次点检记录:一次扫码提交对应一条,聚合该次所有点检项结果。"""

    equipment = models.ForeignKey(
        Equipment, on_delete=models.CASCADE, related_name='records', verbose_name='设备'
    )
    inspector = models.CharField('点检人', max_length=50, blank=True)
    checked_at = models.DateTimeField('点检时间', default=timezone.now, db_index=True)
    # 本次是否存在异常项(冗余标记,便于大屏快速统计)
    has_abnormal = models.BooleanField('存在异常', default=False)
    remark = models.CharField('备注', max_length=200, blank=True)

    class Meta:
        verbose_name = verbose_name_plural = '点检记录'
        ordering = ['-checked_at']

    def __str__(self):
        return f'{self.equipment.code} @ {self.checked_at:%Y-%m-%d %H:%M}'


class CheckResult(models.Model):
    """点检记录中的单项结果:正常 / 异常。异常项是派工单的来源。"""

    NORMAL = 'normal'
    ABNORMAL = 'abnormal'
    STATUS_CHOICES = [
        (NORMAL, '正常'),
        (ABNORMAL, '异常'),
    ]

    record = models.ForeignKey(
        CheckRecord, on_delete=models.CASCADE, related_name='results', verbose_name='点检记录'
    )
    item = models.ForeignKey(
        CheckItem, on_delete=models.CASCADE, related_name='results', verbose_name='点检项'
    )
    status = models.CharField('结果', max_length=10, choices=STATUS_CHOICES, default=NORMAL)
    note = models.CharField('异常描述', max_length=200, blank=True)

    class Meta:
        verbose_name = verbose_name_plural = '点检结果'

    def __str__(self):
        return f'{self.item.name}: {self.get_status_display()}'

    @property
    def is_abnormal(self):
        return self.status == self.ABNORMAL


class WorkOrder(models.Model):
    """维修工单:由点检异常一键生成并指派,状态流转 待处理→处理中→已完成。"""

    PENDING = 'pending'
    PROCESSING = 'processing'
    DONE = 'done'
    STATUS_CHOICES = [
        (PENDING, '待处理'),
        (PROCESSING, '处理中'),
        (DONE, '已完成'),
    ]

    code = models.CharField('工单编号', max_length=32, unique=True, editable=False)
    equipment = models.ForeignKey(
        Equipment, on_delete=models.CASCADE, related_name='work_orders', verbose_name='设备'
    )
    # 触发工单的异常点检结果(可为空:也允许手动建单)
    source_result = models.ForeignKey(
        CheckResult, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='work_orders', verbose_name='来源异常项'
    )
    title = models.CharField('故障/工单标题', max_length=200)
    description = models.TextField('详情', blank=True)
    assignee = models.CharField('指派给', max_length=50, blank=True)
    status = models.CharField('状态', max_length=12, choices=STATUS_CHOICES,
                              default=PENDING, db_index=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)
    finished_at = models.DateTimeField('完成时间', null=True, blank=True)

    class Meta:
        verbose_name = verbose_name_plural = '维修工单'
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.code} {self.title}'

    def save(self, *args, **kwargs):
        if not self.code:
            # 虚构编号:WO + 年月日 + 4位随机,保证脱敏且唯一
            self.code = 'WO{:%Y%m%d}{}'.format(timezone.now(), uuid.uuid4().hex[:4].upper())
        if self.status == self.DONE and self.finished_at is None:
            self.finished_at = timezone.now()
        # 从「已完成」重新打开时清空完成时间,避免残留旧时间
        if self.status != self.DONE and self.finished_at is not None:
            self.finished_at = None
        super().save(*args, **kwargs)

    # 状态流转图:每个状态可切换到的下一批状态(含回退/重开)
    _TRANSITIONS = {
        PENDING: [PROCESSING, DONE],
        PROCESSING: [DONE, PENDING],
        DONE: [PROCESSING],
    }

    @property
    def is_open(self):
        """是否未完结(待处理 / 处理中)。"""
        return self.status != self.DONE

    @property
    def next_statuses(self):
        """当前状态允许切换到的 [(value, label), ...],供详情页渲染流转按钮。"""
        return [(s, dict(self.STATUS_CHOICES)[s]) for s in self._TRANSITIONS.get(self.status, [])]

    def can_transition_to(self, target):
        return target in self._TRANSITIONS.get(self.status, [])

    @classmethod
    def from_result(cls, result, assignee=''):
        """由一条异常点检结果一键生成维修工单。

        标题取「设备-点检项」,详情带上异常说明与来源点检时间;负责人默认取
        设备台账负责人,可由调用方覆盖。设备与来源异常项都记录在案,便于追溯。
        """
        eq = result.item.equipment
        note = (result.note or '').strip()
        title = f'{eq.name}-{result.item.name} 异常'
        desc_lines = [f'来源点检项:{result.item.name}']
        if result.item.standard:
            desc_lines.append(f'标准/参考:{result.item.standard}')
        if note:
            desc_lines.append(f'异常说明:{note}')
        rec = result.record
        desc_lines.append('点检时间:{:%Y-%m-%d %H:%M}'.format(rec.checked_at))
        if rec.inspector:
            desc_lines.append(f'点检人:{rec.inspector}')
        return cls.objects.create(
            equipment=eq,
            source_result=result,
            title=title,
            description='\n'.join(desc_lines),
            assignee=(assignee or eq.owner or '').strip(),
        )
