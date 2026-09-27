from django.contrib import admin

from .models import (
    Equipment,
    CheckItem,
    CheckTemplate,
    CheckTemplateItem,
    CheckRecord,
    CheckResult,
    WorkOrder,
)


class CheckItemInline(admin.TabularInline):
    model = CheckItem
    extra = 1


class CheckTemplateItemInline(admin.TabularInline):
    model = CheckTemplateItem
    extra = 3


@admin.register(CheckTemplate)
class CheckTemplateAdmin(admin.ModelAdmin):
    list_display = ('name', 'category', 'item_count', 'is_active', 'created_at')
    list_filter = ('is_active',)
    search_fields = ('name', 'category')
    inlines = [CheckTemplateItemInline]


@admin.register(Equipment)
class EquipmentAdmin(admin.ModelAdmin):
    list_display = ('code', 'name', 'workshop', 'cycle_days', 'owner',
                    'is_overdue', 'is_active')
    list_filter = ('workshop', 'is_active')
    search_fields = ('code', 'name', 'owner')
    inlines = [CheckItemInline]


class CheckResultInline(admin.TabularInline):
    model = CheckResult
    extra = 0


@admin.register(CheckRecord)
class CheckRecordAdmin(admin.ModelAdmin):
    list_display = ('equipment', 'inspector', 'checked_at', 'has_abnormal')
    list_filter = ('has_abnormal', 'equipment__workshop')
    date_hierarchy = 'checked_at'
    inlines = [CheckResultInline]


@admin.register(CheckItem)
class CheckItemAdmin(admin.ModelAdmin):
    list_display = ('equipment', 'name', 'standard', 'order', 'is_active')
    list_filter = ('equipment__workshop', 'is_active')
    search_fields = ('name',)


@admin.register(WorkOrder)
class WorkOrderAdmin(admin.ModelAdmin):
    list_display = ('code', 'equipment', 'title', 'assignee', 'status',
                    'created_at', 'finished_at')
    list_filter = ('status', 'equipment__workshop')
    search_fields = ('code', 'title', 'assignee')
