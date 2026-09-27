"""设备台账 / 点检标准模板 / 维修工单表单。"""

from django import forms

from .models import CheckTemplate, CheckTemplateItem, Equipment, WorkOrder


class EquipmentForm(forms.ModelForm):
    """设备新增/编辑表单。qr_token 由模型自动生成,不在表单中暴露。"""

    class Meta:
        model = Equipment
        fields = ['code', 'name', 'workshop', 'cycle_days', 'owner', 'is_active']
        widgets = {
            'code': forms.TextInput(attrs={'placeholder': '如 EQ-A-001'}),
            'name': forms.TextInput(attrs={'placeholder': '如 注塑机'}),
            'workshop': forms.TextInput(attrs={'placeholder': '如 一车间'}),
            'cycle_days': forms.NumberInput(attrs={'min': 1}),
            'owner': forms.TextInput(attrs={'placeholder': '如 张师傅(化名)'}),
        }

    def clean_code(self):
        # 统一大写、去空格,避免同一编号大小写重复
        return self.cleaned_data['code'].strip().upper()


class CheckTemplateForm(forms.ModelForm):
    """点检标准模板 新增/编辑表单(表头部分,点检项用下面的 formset)。"""

    class Meta:
        model = CheckTemplate
        fields = ['name', 'category', 'description', 'is_active']
        widgets = {
            'name': forms.TextInput(attrs={'placeholder': '如 注塑机通用点检标准'}),
            'category': forms.TextInput(attrs={'placeholder': '如 注塑机(选填)'}),
            'description': forms.TextInput(attrs={'placeholder': '一句话说明适用范围(选填)'}),
        }

    def clean_name(self):
        return self.cleaned_data['name'].strip()


class CheckTemplateItemForm(forms.ModelForm):
    """模板点检项行表单。"""

    class Meta:
        model = CheckTemplateItem
        fields = ['name', 'standard', 'order']
        widgets = {
            'name': forms.TextInput(attrs={'placeholder': '如 油位是否正常'}),
            'standard': forms.TextInput(attrs={'placeholder': '标准值/说明(选填)'}),
            'order': forms.NumberInput(attrs={'min': 0, 'style': 'width:80px'}),
        }


# 模板 ↔ 模板项 的内联表单集:一屏维护整份点检清单,可增删行。
CheckTemplateItemFormSet = forms.inlineformset_factory(
    CheckTemplate,
    CheckTemplateItem,
    form=CheckTemplateItemForm,
    extra=3,
    can_delete=True,
)


class WorkOrderForm(forms.ModelForm):
    """维修工单 手动新建 / 编辑表单。

    工单编号由模型自动生成;状态流转走详情页的专用按钮,不在此表单里改,
    避免绕过流转逻辑(如完成时间的记录)。
    """

    class Meta:
        model = WorkOrder
        fields = ['equipment', 'title', 'description', 'assignee']
        widgets = {
            'title': forms.TextInput(attrs={'placeholder': '如 注塑机油位偏低需补油'}),
            'description': forms.Textarea(attrs={'rows': 4, 'placeholder': '故障现象 / 处理要求(选填)'}),
            'assignee': forms.TextInput(attrs={'placeholder': '如 李师傅(化名)'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 只在启用设备里建单;编辑既有工单时保留原设备可选
        self.fields['equipment'].queryset = Equipment.objects.filter(is_active=True)
        self.fields['equipment'].empty_label = '— 选择设备 —'

    def clean_title(self):
        title = self.cleaned_data['title'].strip()
        if not title:
            raise forms.ValidationError('请填写工单标题。')
        return title
