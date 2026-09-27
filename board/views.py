from collections import defaultdict
from datetime import timedelta

from django.contrib import messages
from django.db import transaction
from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

import qrcode
from qrcode.image.svg import SvgPathImage

from .forms import (
    CheckTemplateForm, CheckTemplateItemFormSet, EquipmentForm, WorkOrderForm,
)
from .models import (
    CheckItem, CheckRecord, CheckResult, CheckTemplate, Equipment, WorkOrder,
)


def _redcard_sort_key(e):
    """红牌排序:从未点检最紧急排最前,其余按超期天数从多到少。"""
    if e.plan_status == 'never':
        return (0, 0)
    return (1, -(e.overdue_days or 0))


def collect_redcards(qs=None):
    """收集「超期红牌」设备:超过点检周期仍未点检(含从未点检)自动判红牌。

    只统计启用设备;返回按紧急度排好序的列表(从未点检 → 超期天数多 → 少)。
    红牌判定复用 Equipment.is_overdue(next_due_at 由最近点检记录推导)。
    """
    if qs is None:
        qs = Equipment.objects.filter(is_active=True)
    cards = [e for e in qs if e.is_overdue]
    cards.sort(key=_redcard_sort_key)
    return cards


# ---------------------------------------------------------------------------
# 大屏数据可视化:把时序指标算成可直接渲染的 SVG 几何(纯手写,不引任何前端库/CDN)
# ---------------------------------------------------------------------------

# 统一画布尺寸(viewBox 坐标,模板里用 width:100% 自适应缩放)
_CW, _CH = 720, 210            # 画布宽高
_PL, _PR, _PT, _PB = 40, 16, 16, 30  # 内边距:左/右/上/下


def _plot_box():
    """返回绘图区(去掉内边距)左上角与宽高。"""
    return _PL, _PT, _CW - _PL - _PR, _CH - _PT - _PB


def _xs(n):
    """把 n 个点均匀铺在绘图区横向,返回各点 x 坐标列表。"""
    x0, _, iw, _ = _plot_box()
    if n <= 1:
        return [x0 + iw / 2]
    return [round(x0 + iw * i / (n - 1), 1) for i in range(n)]


def _y_of(v, vmax):
    """把数值 v(0..vmax)映射到绘图区纵向像素 y(越大越靠上)。"""
    _, y0, _, ih = _plot_box()
    vmax = max(vmax, 1)
    return round(y0 + ih * (1 - v / vmax), 1)


def _line_series(values, vmax, xs=None):
    """折线序列 → {points:[{x,y,v}], poly:'x,y ...', area:'M... Z'}。"""
    xs = xs or _xs(len(values))
    _, y0, _, ih = _plot_box()
    base = round(y0 + ih, 1)
    pts = [{'x': xs[i], 'y': _y_of(v, vmax), 'v': v} for i, v in enumerate(values)]
    poly = ' '.join(f"{p['x']},{p['y']}" for p in pts)
    area = ''
    if pts:
        area = (f"M{pts[0]['x']},{base} "
                + ' '.join(f"L{p['x']},{p['y']}" for p in pts)
                + f" L{pts[-1]['x']},{base} Z")
    return {'points': pts, 'poly': poly, 'area': area, 'base': base}


def _bar_series(values, vmax, xs=None):
    """柱状序列 → [{x,y,w,h,cx,v}](柱宽取槽宽 55%)。"""
    x0, y0, iw, ih = _plot_box()
    n = len(values) or 1
    slot = iw / n
    bw = slot * 0.55
    vmax = max(vmax, 1)
    xs = xs or [round(x0 + slot * (i + 0.5), 1) for i in range(len(values))]
    bars = []
    for i, v in enumerate(values):
        h = round(ih * v / vmax, 1)
        bars.append({'x': round(xs[i] - bw / 2, 1), 'w': round(bw, 1),
                     'y': round(y0 + ih - h, 1), 'h': h, 'cx': xs[i], 'v': v})
    return bars


def _gridlines(vmax, divs=4, pct=False):
    """水平网格线 + 左侧刻度标签(等分 vmax)。"""
    _, y0, _, ih = _plot_box()
    vmax = max(vmax, 1)
    out = []
    for k in range(divs + 1):
        frac = k / divs
        y = round(y0 + ih * (1 - frac), 1)
        val = vmax * frac
        label = f'{round(val)}%' if pct else f'{round(val)}'
        out.append({'y': y, 'label': label})
    return out


def _dashboard_charts(equipments):
    """构建大屏所有时序图所需的数据与 SVG 几何(近 14 天)。

    - 完成率趋势:每天「处于点检有效周期内」的设备占比
    - 每日点检量 + 异常:柱(点检次数)+ 折线(异常项数)
    - 工单燃尽:累计新建 / 累计完成 双线,间距即未完结存量
    - 设备健康度分布:健康 / 亚健康(有未完结工单)/ 红牌(超期)
    """
    DAYS = 14
    today = timezone.localdate()
    days = [today - timedelta(days=DAYS - 1 - i) for i in range(DAYS)]
    idx = {d: i for i, d in enumerate(days)}
    start = days[0]
    active = list(equipments)

    # --- 每日点检次数 / 异常项数 -------------------------------------------
    checks = [0] * DAYS
    abnl = [0] * DAYS
    for r in (CheckRecord.objects.filter(checked_at__date__gte=start)
              .annotate(na=Count('results',
                                 filter=Q(results__status=CheckResult.ABNORMAL)))):
        d = timezone.localtime(r.checked_at).date()
        i = idx.get(d)
        if i is not None:
            checks[i] += 1
            abnl[i] += r.na

    # --- 完成率趋势:每天有多少设备处于点检有效周期内 ----------------------
    eq_dates = defaultdict(list)  # equipment_id -> [点检日期,...] 升序
    for eid, ca in (CheckRecord.objects.filter(equipment__is_active=True)
                    .values_list('equipment_id', 'checked_at')):
        eq_dates[eid].append(timezone.localtime(ca).date())
    for lst in eq_dates.values():
        lst.sort()
    comp = [0] * DAYS
    n_eq = len(active)
    for i, d in enumerate(days):
        if not n_eq:
            comp[i] = 100
            continue
        on_time = 0
        for e in active:
            past = [x for x in eq_dates.get(e.pk, []) if x <= d]
            if past and (d - past[-1]).days <= e.cycle_days:
                on_time += 1
        comp[i] = round(on_time / n_eq * 100)

    # --- 工单燃尽:累计新建 vs 累计完成(差值 = 未完结存量) ----------------
    wos = list(WorkOrder.objects.values('created_at', 'status', 'finished_at'))
    cum_created = [0] * DAYS
    cum_done = [0] * DAYS
    open_cnt = [0] * DAYS
    for i, d in enumerate(days):
        c = done = 0
        for w in wos:
            if timezone.localtime(w['created_at']).date() <= d:
                c += 1
                fin = w['finished_at']
                fdate = timezone.localtime(fin).date() if fin else None
                if w['status'] == WorkOrder.DONE and fdate and fdate <= d:
                    done += 1
        cum_created[i] = c
        cum_done[i] = done
        open_cnt[i] = c - done

    # --- 设备健康度分布 -----------------------------------------------------
    open_eq = set(WorkOrder.objects.exclude(status=WorkOrder.DONE)
                  .values_list('equipment_id', flat=True))
    h_red = h_sub = h_ok = 0
    for e in active:
        if e.is_overdue:
            h_red += 1
        elif e.pk in open_eq:
            h_sub += 1
        else:
            h_ok += 1
    h_total = max(n_eq, 1)

    # --- 组装 SVG 几何 ------------------------------------------------------
    xs = _xs(DAYS)
    xlabels = [{'x': xs[i], 't': days[i].strftime('%m-%d')}
               for i in range(DAYS) if i % 3 == 0 or i == DAYS - 1]

    vol_max = max(max(checks), max(abnl), 1)
    burn_max = max(max(cum_created), 1)

    return {
        'chart_w': _CW, 'chart_h': _CH,
        'chart_x0': _PL, 'chart_x1': _CW - _PR,
        'chart_y0': _PT, 'chart_y1': _CH - _PB,
        'chart_xlabels': xlabels,
        # 完成率趋势
        'comp_line': _line_series(comp, 100, xs),
        'comp_grid': _gridlines(100, pct=True),
        'comp_last': comp[-1] if comp else 0,
        'comp_avg': round(sum(comp) / len(comp)) if comp else 0,
        # 每日点检量 + 异常
        'vol_bars': _bar_series(checks, vol_max),
        'vol_abn_line': _line_series(abnl, vol_max,
                                     [b['cx'] for b in _bar_series(checks, vol_max)]),
        'vol_grid': _gridlines(vol_max),
        'vol_total': sum(checks),
        'vol_abn_total': sum(abnl),
        # 工单燃尽
        'burn_created': _line_series(cum_created, burn_max, xs),
        'burn_done': _line_series(cum_done, burn_max, xs),
        'burn_grid': _gridlines(burn_max),
        'burn_open_now': open_cnt[-1] if open_cnt else 0,
        'burn_created_now': cum_created[-1] if cum_created else 0,
        'burn_done_now': cum_done[-1] if cum_done else 0,
        # 设备健康度分布
        'health_ok': h_ok, 'health_sub': h_sub, 'health_red': h_red,
        'health_ok_pct': round(h_ok / h_total * 100),
        'health_sub_pct': round(h_sub / h_total * 100),
        'health_red_pct': round(h_red / h_total * 100),
        'health_score': round(h_ok / h_total * 100),
    }


def board_home(request):
    """大屏看板入口:各车间点检完成率 · 今日异常 TOP · 工单状态分布 · 超期红牌。

    全部指标来自真实数据库(启用设备/点检记录/异常结果/工单),数据脱敏。
    """
    equipments = list(Equipment.objects.filter(is_active=True))
    total = len(equipments)
    redcards = [e for e in equipments if e.is_overdue]
    redcards.sort(key=_redcard_sort_key)
    overdue = len(redcards)

    # 按车间统计红牌数(大屏突出「哪个车间最危险」),从多到少
    ws_counter = {}
    for e in redcards:
        ws_counter[e.workshop] = ws_counter.get(e.workshop, 0) + 1
    redcard_by_workshop = sorted(ws_counter.items(), key=lambda kv: kv[1], reverse=True)

    # --- 各车间点检完成率 ---------------------------------------------------
    # 完成率 = 该车间「周期内已点检(未超期)」设备数 / 启用设备数。
    # 未超期(is_overdue 为假)即代表在有效点检周期内,视为「本期已完成点检」。
    ws_total, ws_done = {}, {}
    for e in equipments:
        ws_total[e.workshop] = ws_total.get(e.workshop, 0) + 1
        if not e.is_overdue:
            ws_done[e.workshop] = ws_done.get(e.workshop, 0) + 1
    workshop_rates = []
    for ws in sorted(ws_total):
        t = ws_total[ws]
        d = ws_done.get(ws, 0)
        workshop_rates.append({
            'workshop': ws, 'total': t, 'done': d,
            'rate': round(d / t * 100) if t else 0,
        })
    # 完成率低的车间排前面(最需要关注),同率按设备多的在前
    workshop_rates.sort(key=lambda r: (r['rate'], -r['total']))

    # --- 今日点检 / 今日异常 TOP -------------------------------------------
    today = timezone.localdate()
    records_today = CheckRecord.objects.filter(checked_at__date=today)
    today_check_count = records_today.count()
    abn_today = CheckResult.objects.filter(
        status=CheckResult.ABNORMAL, record__checked_at__date=today,
    )
    today_abnormal_count = abn_today.count()
    # 今日异常按设备聚合 TOP(哪台设备今天异常项最多)
    abnormal_top = list(
        abn_today.values(
            'record__equipment__pk',
            'record__equipment__code',
            'record__equipment__name',
            'record__equipment__workshop',
        )
        .annotate(n=Count('id'))
        .order_by('-n', 'record__equipment__code')[:8]
    )
    abnormal_top_max = abnormal_top[0]['n'] if abnormal_top else 0

    # --- 工单状态分布 -------------------------------------------------------
    wo_qs = WorkOrder.objects.aggregate(
        pending=Count('id', filter=Q(status=WorkOrder.PENDING)),
        processing=Count('id', filter=Q(status=WorkOrder.PROCESSING)),
        done=Count('id', filter=Q(status=WorkOrder.DONE)),
    )
    wo_pending = wo_qs['pending'] or 0
    wo_processing = wo_qs['processing'] or 0
    wo_done = wo_qs['done'] or 0
    wo_total = wo_pending + wo_processing + wo_done
    wo_open = wo_pending + wo_processing

    # 综合点检完成率(全厂启用设备中未超期占比)
    overall_rate = round((total - overdue) / total * 100) if total else 100

    ctx = {
        'total_equipment': total,
        'overdue_count': overdue,
        'ok_count': total - overdue,
        'health_rate': overall_rate,
        'overall_rate': overall_rate,
        # 大屏红牌预警滚动榜(最紧急的前若干台)
        'redcard_top': redcards[:8],
        'redcard_by_workshop': redcard_by_workshop,
        # 各车间点检完成率
        'workshop_rates': workshop_rates,
        # 今日点检 / 异常
        'today_check_count': today_check_count,
        'today_abnormal_count': today_abnormal_count,
        'abnormal_top': abnormal_top,
        'abnormal_top_max': abnormal_top_max,
        # 工单状态分布
        'wo_pending': wo_pending,
        'wo_processing': wo_processing,
        'wo_done': wo_done,
        'wo_total': wo_total,
        'wo_open': wo_open,
    }
    # 大屏数据可视化(近 14 天趋势 / 燃尽 / 健康度分布)
    ctx.update(_dashboard_charts(equipments))
    return render(request, 'board/home.html', ctx)


def redcard_alerts(request):
    """超期红牌预警:超过点检周期仍未点检的设备清单 + 计数,可按车间筛选。

    红牌 = 从未点检(立即应检)或已超期。每台给出超期天数、负责人、最近点检
    时间与「去点检」直达入口,便于现场立即处置。
    """
    qs = Equipment.objects.filter(is_active=True)
    workshop = request.GET.get('workshop', '').strip()
    if workshop:
        qs = qs.filter(workshop=workshop)

    redcards = collect_redcards(qs)
    never_n = sum(1 for e in redcards if e.plan_status == 'never')
    overdue_n = sum(1 for e in redcards if e.plan_status == 'overdue')
    # 最狠的一台超期天数(从未点检不计天数),给大屏一个"最长超期"指标
    worst = max((e.overdue_days or 0 for e in redcards if e.plan_status == 'overdue'),
                default=0)

    total_active = qs.count()
    ctx = {
        'redcards': redcards,
        'redcard_count': len(redcards),
        'never_count': never_n,
        'overdue_count': overdue_n,
        'worst_days': worst,
        'total_active': total_active,
        'ok_count': total_active - len(redcards),
        'workshops': list(
            Equipment.objects.order_by('workshop')
            .values_list('workshop', flat=True).distinct()
        ),
        'cur_workshop': workshop,
    }
    return render(request, 'board/alerts.html', ctx)


# ---------------------------------------------------------------------------
# 设备台账管理:列表 / 新增 / 编辑 / 二维码
# ---------------------------------------------------------------------------

def equipment_list(request):
    """设备台账列表:支持按车间 / 关键字筛选,展示点检状态与二维码入口。"""
    qs = Equipment.objects.all()

    workshop = request.GET.get('workshop', '').strip()
    keyword = request.GET.get('q', '').strip()
    if workshop:
        qs = qs.filter(workshop=workshop)
    if keyword:
        qs = qs.filter(Q(code__icontains=keyword) | Q(name__icontains=keyword)
                       | Q(owner__icontains=keyword))

    equipments = list(qs)
    total = len(equipments)
    overdue = sum(1 for e in equipments if e.is_overdue)

    ctx = {
        'equipments': equipments,
        'total': total,
        'overdue': overdue,
        'ok': total - overdue,
        # 车间下拉选项(全量,不受当前筛选影响)
        'workshops': list(
            Equipment.objects.order_by('workshop')
            .values_list('workshop', flat=True).distinct()
        ),
        'cur_workshop': workshop,
        'keyword': keyword,
    }
    return render(request, 'board/equipment_list.html', ctx)


def equipment_create(request):
    """新增设备。保存时模型自动生成 qr_token。"""
    if request.method == 'POST':
        form = EquipmentForm(request.POST)
        if form.is_valid():
            eq = form.save()
            messages.success(request, f'设备 {eq.code} 已创建,二维码已生成。')
            return redirect('board:equipment_detail', pk=eq.pk)
    else:
        form = EquipmentForm()
    return render(request, 'board/equipment_form.html',
                  {'form': form, 'title': '新增设备', 'is_create': True})


def equipment_edit(request, pk):
    """编辑设备。"""
    eq = get_object_or_404(Equipment, pk=pk)
    if request.method == 'POST':
        form = EquipmentForm(request.POST, instance=eq)
        if form.is_valid():
            form.save()
            messages.success(request, f'设备 {eq.code} 已更新。')
            return redirect('board:equipment_detail', pk=eq.pk)
    else:
        form = EquipmentForm(instance=eq)
    return render(request, 'board/equipment_form.html',
                  {'form': form, 'title': f'编辑设备 · {eq.code}',
                   'is_create': False, 'equipment': eq})


def equipment_detail(request, pk):
    """设备详情:展示台账信息 + 大图二维码 + 扫码点检链接,可打印张贴。"""
    eq = get_object_or_404(Equipment, pk=pk)
    check_url = request.build_absolute_uri(reverse('board:check', args=[eq.code]))
    ctx = {
        'equipment': eq,
        'check_url': check_url,
        'last_record': eq.last_record,
        'next_due_at': eq.next_due_at,
        # 最近几次点检记录(含异常项数),给详情页一个点检履历
        'recent_records': eq.records.order_by('-checked_at')
            .annotate(n_abnormal=Count('results', filter=Q(results__status=CheckResult.ABNORMAL)))[:8],
    }
    return render(request, 'board/equipment_detail.html', ctx)


def equipment_qr(request, pk):
    """返回该设备点检二维码的 SVG(内容为绝对 /check/<设备编号> 链接)。

    用 SVG 无需 PIL/Pillow;可直接内嵌 <img> 或打印,矢量不失真。
    """
    eq = get_object_or_404(Equipment, pk=pk)
    check_url = request.build_absolute_uri(reverse('board:check', args=[eq.code]))

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=2,
    )
    qr.add_data(check_url)
    qr.make(fit=True)
    img = qr.make_image(image_factory=SvgPathImage)

    return HttpResponse(img.to_string(encoding='unicode'),
                        content_type='image/svg+xml')


# ---------------------------------------------------------------------------
# 点检标准与模板:模板维护 + 一键套用到设备
# ---------------------------------------------------------------------------

def template_list(request):
    """点检标准模板列表:每份模板含若干标准点检项,可套用到多台设备。"""
    templates = (
        CheckTemplate.objects.annotate(n_items=Count('items'))
        .order_by('-is_active', 'name')
    )
    return render(request, 'board/template_list.html', {
        'templates': templates,
        'total': templates.count(),
    })


def template_create(request):
    """新增点检标准模板(表头 + 点检项一屏维护)。"""
    if request.method == 'POST':
        form = CheckTemplateForm(request.POST)
        formset = CheckTemplateItemFormSet(request.POST)
        if form.is_valid() and formset.is_valid():
            tpl = form.save()
            formset.instance = tpl
            formset.save()
            messages.success(request, f'模板「{tpl.name}」已创建,共 {tpl.item_count} 个点检项。')
            return redirect('board:template_detail', pk=tpl.pk)
    else:
        form = CheckTemplateForm()
        formset = CheckTemplateItemFormSet()
    return render(request, 'board/template_form.html', {
        'form': form, 'formset': formset, 'title': '新增点检标准模板', 'is_create': True,
    })


def template_edit(request, pk):
    """编辑点检标准模板及其点检项。"""
    tpl = get_object_or_404(CheckTemplate, pk=pk)
    if request.method == 'POST':
        form = CheckTemplateForm(request.POST, instance=tpl)
        formset = CheckTemplateItemFormSet(request.POST, instance=tpl)
        if form.is_valid() and formset.is_valid():
            form.save()
            formset.save()
            messages.success(request, f'模板「{tpl.name}」已更新。')
            return redirect('board:template_detail', pk=tpl.pk)
    else:
        form = CheckTemplateForm(instance=tpl)
        formset = CheckTemplateItemFormSet(instance=tpl)
    return render(request, 'board/template_form.html', {
        'form': form, 'formset': formset, 'title': f'编辑模板 · {tpl.name}',
        'is_create': False, 'template': tpl,
    })


def template_detail(request, pk):
    """模板详情:展示标准点检项,并提供「套用到设备」入口。"""
    tpl = get_object_or_404(CheckTemplate, pk=pk)
    equipments = Equipment.objects.filter(is_active=True)
    workshop = request.GET.get('workshop', '').strip()
    if workshop:
        equipments = equipments.filter(workshop=workshop)
    return render(request, 'board/template_detail.html', {
        'template': tpl,
        'items': tpl.items.all(),
        'equipments': equipments,
        'workshops': list(
            Equipment.objects.order_by('workshop')
            .values_list('workshop', flat=True).distinct()
        ),
        'cur_workshop': workshop,
    })


def template_apply(request, pk):
    """把模板的标准点检项套用(复制)到所选设备的点检项中。

    - 按设备逐项复制:同名点检项已存在则跳过(幂等,可重复套用不生成重复项)。
    - 设备与模板不强绑定:套用后各设备可独立增删,互不影响。
    """
    tpl = get_object_or_404(CheckTemplate, pk=pk)
    if request.method != 'POST':
        return redirect('board:template_detail', pk=pk)

    eq_ids = request.POST.getlist('equipment_ids')
    equipments = Equipment.objects.filter(pk__in=eq_ids)

    applied_eq = 0
    created_items = 0
    for eq in equipments:
        existing = set(eq.items.values_list('name', flat=True))
        max_order = max(
            (eq.items.order_by('-order').values_list('order', flat=True).first() or 0), 0
        )
        added_here = 0
        for titem in tpl.items.all():
            if titem.name in existing:
                continue
            max_order += 1
            CheckItem.objects.create(
                equipment=eq, name=titem.name, standard=titem.standard,
                order=max_order, is_active=True,
            )
            existing.add(titem.name)
            created_items += 1
            added_here += 1
        if added_here:
            applied_eq += 1

    if not eq_ids:
        messages.warning(request, '未选择任何设备,请勾选后再套用。')
    else:
        messages.success(
            request,
            f'已把模板「{tpl.name}」套用到 {applied_eq} 台设备,新增 {created_items} 个点检项'
            f'(已存在的同名项自动跳过)。'
        )
    return redirect('board:template_detail', pk=pk)


# ---------------------------------------------------------------------------
# 本期应点检计划:按各设备点检周期,自动算出「哪些到期该检」
# ---------------------------------------------------------------------------

def inspection_plan(request):
    """本期应点检计划:遍历启用设备,按各自 cycle_days 计算下次应检时间,
    自动归档为 从未点检 / 已超期 / 今日应检 / 即将到期,汇总成一份计划清单。
    """
    qs = Equipment.objects.filter(is_active=True)
    workshop = request.GET.get('workshop', '').strip()
    if workshop:
        qs = qs.filter(workshop=workshop)

    buckets = {'never': [], 'overdue': [], 'due': [], 'soon': [], 'ok': []}
    for eq in qs:
        buckets[eq.plan_status].append(eq)

    # 超期按超期天数从多到少排;即将到期按剩余天数从少到多排
    buckets['overdue'].sort(key=lambda e: e.overdue_days or 0, reverse=True)
    buckets['soon'].sort(key=lambda e: e.days_until_due)

    # 「本期应点检」= 从未点检 + 超期 + 今日 + 即将到期
    due_list = buckets['never'] + buckets['overdue'] + buckets['due'] + buckets['soon']
    total = qs.count()
    due_count = len(due_list)
    # 完成率:非应检(未到期)占比,直观反映"本期点检压力"
    ok_count = len(buckets['ok'])
    completion = round(ok_count / total * 100) if total else 0

    ctx = {
        'never': buckets['never'],
        'overdue': buckets['overdue'],
        'due': buckets['due'],
        'soon': buckets['soon'],
        'ok_count': ok_count,
        'due_list': due_list,
        'total': total,
        'due_count': due_count,
        # 红牌 = 从未点检 + 已超期
        'redcard_count': len(buckets['never']) + len(buckets['overdue']),
        'completion': completion,
        'soon_window': Equipment.SOON_WINDOW_DAYS,
        'workshops': list(
            Equipment.objects.order_by('workshop')
            .values_list('workshop', flat=True).distinct()
        ),
        'cur_workshop': workshop,
    }
    return render(request, 'board/plan.html', ctx)


def check_entry(request, code):
    """扫码点检入口 /check/<设备编号>:进入该设备的点检表单并落库。

    GET  展示设备信息 + 逐个点检项(正常/异常单选,异常可填说明)。
    POST 校验并落库为一条 CheckRecord + 若干 CheckResult;创建记录即刷新
         设备「最近点检时间」(next_due_at 由最近记录推导,无需另存字段)。
    异常项一键生成维修工单留给工单模块,这里只保证点检执行闭环。
    """
    eq = get_object_or_404(Equipment, code=code.strip().upper())
    items = list(eq.items.filter(is_active=True).order_by('order', 'id'))

    if request.method == 'POST':
        if not items:
            messages.warning(request, '该设备暂无启用的点检项,请先在设备详情/点检标准中配置。')
            return redirect('board:check', code=eq.code)

        inspector = request.POST.get('inspector', '').strip()
        remark = request.POST.get('remark', '').strip()

        # 先做整表校验:每个点检项必须选择结果;异常项必须给出说明。
        parsed = []          # [(item, status, note), ...]
        errors = {}          # item.pk -> 错误文案
        posted_status = {}   # 回填用:item.pk -> 已选状态
        posted_note = {}     # 回填用:item.pk -> 已填说明
        for it in items:
            status = request.POST.get(f'status_{it.pk}', '')
            note = request.POST.get(f'note_{it.pk}', '').strip()
            posted_status[it.pk] = status
            posted_note[it.pk] = note
            if status not in (CheckResult.NORMAL, CheckResult.ABNORMAL):
                errors[it.pk] = '请选择正常或异常'
                continue
            if status == CheckResult.ABNORMAL and not note:
                errors[it.pk] = '异常项请填写具体说明'
                continue
            parsed.append((it, status, note if status == CheckResult.ABNORMAL else ''))

        if errors:
            messages.warning(request, '提交未通过:请为每个点检项选择结果,异常项需填写说明。')
            ctx = _check_ctx(request, eq, items, posted_status, posted_note,
                             errors, inspector, remark)
            return render(request, 'board/check_form.html', ctx)

        has_abnormal = any(s == CheckResult.ABNORMAL for _, s, _ in parsed)
        with transaction.atomic():
            record = CheckRecord.objects.create(
                equipment=eq, inspector=inspector, has_abnormal=has_abnormal,
                remark=remark,
            )
            CheckResult.objects.bulk_create([
                CheckResult(record=record, item=it, status=status, note=note)
                for it, status, note in parsed
            ])

        abnormal_n = sum(1 for _, s, _ in parsed if s == CheckResult.ABNORMAL)
        if abnormal_n:
            messages.success(
                request,
                f'点检已提交:{eq.code} 共 {len(parsed)} 项,其中 {abnormal_n} 项异常已记录,'
                f'可在下方对异常项一键派修生成工单。'
            )
            # 有异常:跳记录详情,直接引导派修
            return redirect('board:record_detail', pk=record.pk)
        messages.success(request, f'点检已提交:{eq.code} 共 {len(parsed)} 项全部正常。')
        return redirect('board:equipment_detail', pk=eq.pk)

    # GET:空表单
    ctx = _check_ctx(request, eq, items, {}, {}, {}, '', '')
    return render(request, 'board/check_form.html', ctx)


def _check_ctx(request, eq, items, posted_status, posted_note, errors,
               inspector, remark):
    """组装点检表单上下文:把每个点检项连同已选状态/说明/错误打包给模板。"""
    rows = [{
        'item': it,
        'status': posted_status.get(it.pk, ''),
        'note': posted_note.get(it.pk, ''),
        'error': errors.get(it.pk, ''),
    } for it in items]
    return {
        'equipment': eq,
        'rows': rows,
        'has_items': bool(items),
        'inspector': inspector,
        'remark': remark,
        'last_record': eq.last_record,
        'next_due_at': eq.next_due_at,
    }


# ---------------------------------------------------------------------------
# 点检记录详情:逐项结果 + 异常项一键派修
# ---------------------------------------------------------------------------

def record_detail(request, pk):
    """一次点检记录详情:展示每个点检项的结果,异常项提供「一键派修」入口。

    已派过工单的异常项直接展示工单链接与状态(幂等,不重复建单)。
    """
    rec = get_object_or_404(
        CheckRecord.objects.select_related('equipment'), pk=pk
    )
    results = (
        rec.results.select_related('item')
        .prefetch_related('work_orders')
        .order_by('item__order', 'item__id')
    )
    rows = []
    abnormal_open = 0
    for r in results:
        wo = r.work_orders.all().first()  # 一条异常项最多派一张单(建单时防重)
        if r.is_abnormal and wo is None:
            abnormal_open += 1
        rows.append({'result': r, 'work_order': wo})

    return render(request, 'board/record_detail.html', {
        'record': rec,
        'equipment': rec.equipment,
        'rows': rows,
        'abnormal_open': abnormal_open,
    })


def result_dispatch(request, pk):
    """把一条异常点检结果一键生成维修工单并指派(默认指派给设备负责人)。"""
    result = get_object_or_404(
        CheckResult.objects.select_related('item__equipment', 'record'), pk=pk
    )
    if request.method != 'POST':
        return redirect('board:record_detail', pk=result.record_id)

    if not result.is_abnormal:
        messages.warning(request, '该点检项结果正常,无需派修。')
        return redirect('board:record_detail', pk=result.record_id)

    existing = result.work_orders.first()
    if existing is not None:
        messages.warning(request, f'该异常项已生成工单 {existing.code},请勿重复派修。')
        return redirect('board:workorder_detail', pk=existing.pk)

    assignee = request.POST.get('assignee', '').strip()
    wo = WorkOrder.from_result(result, assignee=assignee)
    who = wo.assignee or '(未指派)'
    messages.success(
        request, f'已生成维修工单 {wo.code},指派给 {who},状态「待处理」。'
    )
    return redirect('board:workorder_detail', pk=wo.pk)


# ---------------------------------------------------------------------------
# 维修工单:列表 / 详情 / 手动新建 / 编辑 / 状态流转
# ---------------------------------------------------------------------------

def workorder_list(request):
    """维修工单管理页:按状态 / 车间筛选,顶部状态统计卡。"""
    qs = WorkOrder.objects.select_related('equipment')

    status = request.GET.get('status', '').strip()
    workshop = request.GET.get('workshop', '').strip()
    keyword = request.GET.get('q', '').strip()
    if status in dict(WorkOrder.STATUS_CHOICES):
        qs = qs.filter(status=status)
    if workshop:
        qs = qs.filter(equipment__workshop=workshop)
    if keyword:
        qs = qs.filter(
            Q(code__icontains=keyword) | Q(title__icontains=keyword)
            | Q(assignee__icontains=keyword)
        )

    counts = WorkOrder.objects.aggregate(
        total=Count('id'),
        pending=Count('id', filter=Q(status=WorkOrder.PENDING)),
        processing=Count('id', filter=Q(status=WorkOrder.PROCESSING)),
        done=Count('id', filter=Q(status=WorkOrder.DONE)),
    )

    ctx = {
        'work_orders': list(qs),
        'counts': counts,
        'cur_status': status,
        'cur_workshop': workshop,
        'keyword': keyword,
        'status_choices': WorkOrder.STATUS_CHOICES,
        'workshops': list(
            Equipment.objects.order_by('workshop')
            .values_list('workshop', flat=True).distinct()
        ),
    }
    return render(request, 'board/workorder_list.html', ctx)


def workorder_detail(request, pk):
    """工单详情:基础信息 + 来源异常项 + 状态流转按钮。"""
    wo = get_object_or_404(
        WorkOrder.objects.select_related('equipment', 'source_result__item'), pk=pk
    )
    return render(request, 'board/workorder_detail.html', {
        'wo': wo,
        'next_statuses': wo.next_statuses,
        'source_result': wo.source_result,
    })


def workorder_create(request):
    """手动新建维修工单(无点检来源时使用)。"""
    if request.method == 'POST':
        form = WorkOrderForm(request.POST)
        if form.is_valid():
            wo = form.save()
            messages.success(request, f'工单 {wo.code} 已创建,状态「待处理」。')
            return redirect('board:workorder_detail', pk=wo.pk)
    else:
        form = WorkOrderForm()
    return render(request, 'board/workorder_form.html', {
        'form': form, 'title': '手动新建工单', 'is_create': True,
    })


def workorder_edit(request, pk):
    """编辑工单(设备 / 标题 / 详情 / 指派人);状态走流转按钮不在此改。"""
    wo = get_object_or_404(WorkOrder, pk=pk)
    if request.method == 'POST':
        form = WorkOrderForm(request.POST, instance=wo)
        if form.is_valid():
            form.save()
            messages.success(request, f'工单 {wo.code} 已更新。')
            return redirect('board:workorder_detail', pk=wo.pk)
    else:
        form = WorkOrderForm(instance=wo)
    return render(request, 'board/workorder_form.html', {
        'form': form, 'title': f'编辑工单 · {wo.code}', 'is_create': False, 'wo': wo,
    })


def workorder_transition(request, pk):
    """工单状态流转:待处理 ⇄ 处理中 → 已完成(可重开)。仅接受合法目标态。"""
    wo = get_object_or_404(WorkOrder, pk=pk)
    if request.method != 'POST':
        return redirect('board:workorder_detail', pk=pk)

    target = request.POST.get('status', '').strip()
    if target not in dict(WorkOrder.STATUS_CHOICES):
        messages.warning(request, '无效的目标状态。')
    elif target == wo.status:
        messages.warning(request, '工单已处于该状态。')
    elif not wo.can_transition_to(target):
        messages.warning(request, '不允许的状态流转。')
    else:
        label = dict(WorkOrder.STATUS_CHOICES)[target]
        wo.status = target
        wo.save()  # 模型 save 负责 完成时间 的记录/清空
        messages.success(request, f'工单 {wo.code} 状态已更新为「{label}」。')
    return redirect('board:workorder_detail', pk=wo.pk)
