# 部署到新工作区

## 前提

- Windows、64 位 Python 3.12 或更新版本；本地已用 3.14.7 实测，其他版本按验证结果判断。
- SolidWorks 已合法安装，能手动新建和保存零件，有可用零件模板。
- Codex/兼容的本地 STDIO MCP 客户端。Computer Use 可选，CAD API 建模不依赖鼠标自动化。

先查询现有环境，不为满足示例而重装已有软件。所有路径通过参数或 `SW_AI_WORKSPACE` 配置。

## 准备

以下 `SKILL_DIR` 表示本技能安装目录。PowerShell 示例：

```powershell
$skillDir = Join-Path $env:USERPROFILE '.codex\skills\solidworks-windows'
python "$skillDir\scripts\setup_workspace.py" --workspace C:\AI-Workspace --dry-run
```

检查计划后，在用户授权范围内执行：

```powershell
python "$skillDir\scripts\setup_workspace.py" --workspace C:\AI-Workspace --install
& C:\AI-Workspace\.venv\Scripts\python.exe "$skillDir\scripts\diagnose.py" --workspace C:\AI-Workspace --save-report
```

脚本创建 `CAD/Scripts/Reports/Backups`，复制可移植服务与固定快照的上游源码，在独立 `.venv` 中安装依赖。依赖固定 MCP SDK 1.30.0、pywin32 312；升级后重验，不跨大版本盲目更新。

已有同名源码内容不同会被拒绝。更换工作区或审查差异后手动更新；不要覆盖现有部署来完成一次教程。

可选 `--sw-exe <实际 SLDWORKS.exe 路径>`。未指定时由上游读取安装注册信息。`--prepare-only` 只复制源码并生成配置，不安装依赖。

## 先注册查询模式

```powershell
python "$skillDir\scripts\configure_mcp.py" --workspace C:\AI-Workspace --mode query
python "$skillDir\scripts\configure_mcp.py" --workspace C:\AI-Workspace --mode query --apply
```

第一个命令输出配置预览；第二个备份并更新当前用户的 `config.toml`。其他 MCP 和顶层设置会保留。若同名服务已存在但内容不同，先检查它；已获得替换授权后使用 `--replace-existing`。可用 `--server-id` 避免冲突。

重载客户端后检查当前工具列表。手动启动 SolidWorks，再验证：

```powershell
& C:\AI-Workspace\.venv\Scripts\python.exe "$skillDir\scripts\probe_mcp.py" --workspace C:\AI-Workspace --mode query --live --save-report
```

不加 `--live` 只做协议握手与工具清单检查，不调用 SolidWorks。`codex mcp get solidworks --json` 只能证明配置可读，不能替代真实连接。

## 升级到建模模式

查询通过后，预览建模配置，然后应用：

```powershell
python "$skillDir\scripts\configure_mcp.py" --workspace C:\AI-Workspace --mode modeling
python "$skillDir\scripts\configure_mcp.py" --workspace C:\AI-Workspace --mode modeling --apply --replace-existing
& C:\AI-Workspace\.venv\Scripts\python.exe "$skillDir\scripts\probe_mcp.py" --workspace C:\AI-Workspace --mode modeling --live --save-report
```

客户端仍需重载才能使用新增工具；随后按 [建模与验收](modeling.md) 完成真实零件测试。

官方依据：[本地 MCP](https://learn.chatgpt.com/docs/extend/mcp)、[配置字段](https://learn.chatgpt.com/docs/config-file/config-reference)。客户端入口可能变化，以当前环境实际支持的方式为准。
