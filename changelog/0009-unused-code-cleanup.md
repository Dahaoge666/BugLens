# 0009 — 清理未使用代码

日期：2026-09-13。类型：Removed / Changed。状态：已落地。

移除旧环境页、凭据编辑器、SQLite 试用卡和旧数据源列表遗留的 35 个未引用样式类，清理相同选择器下重复的相同声明。现有环境与子服务配置、插件接入表单和响应式间距沿用当前样式。

移除无调用且未导出的 command_from_json、event_to_json 协议辅助函数；协议类型和现有适配器的解析、序列化保持不变。插件表单统一复用数据源标签映射，直接读取插件默认连接方式，取消重复参数和操作标签别名。

关联文件：backend/app/protocol/commands.py、events.py，frontend/src/styles.css、EnvironmentPage.tsx、PluginInstanceEditor.tsx、PluginConfigFields.tsx、pluginForm.ts。验证使用现有后端与前端测试、静态检查和构建，不调用真实模型。
