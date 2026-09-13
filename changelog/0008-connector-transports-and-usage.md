# 0008 — 连接方式与节点使用说明

日期：2026-09-13。类型：Added / Changed / Fixed / Docs。状态：已落地。

工作区与侧栏、右侧窗口增加自适应间距。所有内置插件在主表单选择驱动、MCP 或 CLI，非驱动方式不再显示原生配置的必填项。其他连接器提供可直接新建的自定义入口，明确 MCP 工具映射与 CLI JSON 输入输出协议。

插件实例增加独立的用途、适用场景、步骤和只读调用示例。说明脱敏后随 run 快照保存，通过有界 connector_guides 用户输入提供给兼容定位节点和原生 SDK loop；实际数据源自动绑定示例，当前配置更新不改变旧 run。说明不进入系统指令、不作为故障证据。非空 MCP 映射中遗漏的操作在连接前拒绝。

关联文件：根级 frontend-backend-contract.md、backend/app/environment.py、connector_guidance.py、plugins.py、agents.py、admin.py、transports.py，frontend/src/PluginInstanceEditor.tsx、ConnectorUsageFields.tsx、PluginConfigFields.tsx、styles.css。回归覆盖所有五个插件的 MCP/CLI 执行、实例隔离、不可变说明、脱敏、能力范围、大小上限和实际 SDK 输入；模型调用均使用 fake。
