# TODO

这里只记录尚未完成的工作；已完成事项见 [changelog/](changelog/README.md)，验证步骤见[后端测试指南](backend/docs/testing.md)。

- [ ] 使用专用测试凭据执行真实模型端到端验证，记录基本诊断、澄清与 Skip、HTTP/SSE 追赶、失败 Resume、工具审批恢复和两套环境隔离的结果。
- [ ] 增加前端 fake API/组件测试，覆盖事件去重、断流后快照校准、revision 冲突和可用操作展示。

真实模型验证不进入默认测试，也不使用生产数据。结果只记录日期、provider/model、配置版本、run ID、最终状态、事件范围和脱敏问题；不提交凭据或完整模型消息。
