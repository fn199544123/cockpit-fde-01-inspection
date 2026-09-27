from django.urls import path

from . import views

app_name = 'board'

urlpatterns = [
    path('', views.board_home, name='home'),

    # 设备台账管理
    path('equipment/', views.equipment_list, name='equipment_list'),
    path('equipment/new/', views.equipment_create, name='equipment_create'),
    path('equipment/<int:pk>/', views.equipment_detail, name='equipment_detail'),
    path('equipment/<int:pk>/edit/', views.equipment_edit, name='equipment_edit'),
    path('equipment/<int:pk>/qr.svg', views.equipment_qr, name='equipment_qr'),

    # 点检标准与模板
    path('templates/', views.template_list, name='template_list'),
    path('templates/new/', views.template_create, name='template_create'),
    path('templates/<int:pk>/', views.template_detail, name='template_detail'),
    path('templates/<int:pk>/edit/', views.template_edit, name='template_edit'),
    path('templates/<int:pk>/apply/', views.template_apply, name='template_apply'),

    # 本期应点检计划
    path('plan/', views.inspection_plan, name='plan'),

    # 超期红牌预警
    path('alerts/', views.redcard_alerts, name='alerts'),

    # 扫码点检入口(二维码指向此处)
    path('check/<str:code>/', views.check_entry, name='check'),

    # 点检记录详情 + 异常项一键派修
    path('records/<int:pk>/', views.record_detail, name='record_detail'),
    path('results/<int:pk>/dispatch/', views.result_dispatch, name='result_dispatch'),

    # 维修工单管理
    path('workorders/', views.workorder_list, name='workorder_list'),
    path('workorders/new/', views.workorder_create, name='workorder_create'),
    path('workorders/<int:pk>/', views.workorder_detail, name='workorder_detail'),
    path('workorders/<int:pk>/edit/', views.workorder_edit, name='workorder_edit'),
    path('workorders/<int:pk>/status/', views.workorder_transition, name='workorder_transition'),
]
