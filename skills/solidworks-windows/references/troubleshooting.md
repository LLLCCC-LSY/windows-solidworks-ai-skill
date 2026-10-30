# 故障定位

## 连接

`NOT_RUNNING` 表示未成功连接运行对象；确认实际进程、启动是否完成及两边权限级别，再判断原因。不要通过创建新的 COM 实例来绕过关闭的自动启动策略。

若 PowerShell/.NET 报 `0x80029C4A`，先测试随附 `diagnose.py`。Python 可能仍能晚绑定成功；这不证明注册表已修复。直接使用 `pythoncom.GetActiveObject` 时先 `QueryInterface(IID_IDispatch)`，不要将 `PyIUnknown` 当成 `IDispatch` 使用。

依赖缺少、COM 连接失败、MCP 握手失败分别处理。当前上游依赖必须限制 `mcp<2`，不能把 SDK 2.x 的接口删除误诊为 SolidWorks 故障。

## 尺寸与状态

- 标注尺寸可能弹出输入框。实现使用本机类型库核实 `swInputDimValOnCreate`，临时关闭并在 `finally` 恢复；无法核实常量或恢复失败时应明确报告。
- `swConstrainedStatus_e`：2 欠定义，3 完全定义，4 过定义。根据实际返回码解释，不凭成功消息推断。
- 特征尺寸链要通过拥有者的 `GetNextDisplayDimension` 遍历，并按实际所属特征去重；只返回首条尺寸会使参数检查失真。
- 成员能力存在不等于其建模操作已验证；探测不能通过 `getattr` 触发零参数方法。

## 方程 / VBA

一次本机实测中，最简单全局变量的 `Add2` 返回 -1，SolidWorks 自带方程管理器也报语法错误；相关 VBA 注册异常，完整根因未隔离。该现象不是所有机器的通用结论。

先在新测试零件中用原生界面对照。如果界面也失败，应排查 SolidWorks/VBA 前置组件，而不是反复改 MCP 表达式。需要的 VBA 7.1 组件应从可信安装源获取并核验签名；不使用其他 CAD 自带的旧版 32 位 VBA 替换它。

本包方程模式默认关闭。修复及验证前保持 `SOLIDWORKS_ENABLE_EQUATIONS=false`。原生尺寸配方可以独立使用。

安装/修复共享组件、修改系统注册或重启应用应按用户的明确任务范围处理；先准备诊断和具体修复方案，不能因使用本技能就推定已获许可。不要改动授权或许可机制。

官方参考：[VBA 前置组件故障说明](https://www.solidworks.com/support/frequently-asked-questions?page=1&term_id=417)。不把其他条目的系统安全设置建议当成本技能操作步骤。
