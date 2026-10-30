# Windows SolidWorks AI Skill

把 Windows + Codex + SolidWorks MCP 的部署、尺寸驱动建模和验收流程做成一个可安装的 skill。

**当前可用：**环境诊断、独立工作区部署、3 工具查询模式、19 工具受限建模模式，以及完全定义的五输入四孔安装板配方。支持创建原生 `.SLDPRT`、修改尺寸、检查孔位/体积/特征错误、保存并重新打开验证。

**实际边界：**默认使用原生尺寸与几何关系；D/E 重复尺寸由参数工具同步。SolidWorks 全局变量方程模式默认关闭，初始测试机器的 VBA/方程故障尚未修复。装配、工程图、导出和有限元不属于已验证功能。

初始真实 CAD 验证使用 SolidWorks 2023 SP0.1、Python 3.14.7。提供 Python 3.12+ 的兼容实现；本次本地离线测试使用 3.14.7，3.12 仍需独立测试。不能将离线测试当作所有软件组合的 CAD 验证。

## 安装 skill

在支持 skill-installer 的 Codex 中输入：

```text
使用 skill-installer 安装 https://github.com/LLLCCC-LSY/windows-solidworks-ai-skill/tree/main/skills/solidworks-windows
```

也可克隆仓库，将 `skills/solidworks-windows` 整个目录复制到当前客户端的用户技能目录，通常为 `$CODEX_HOME/skills`（未设置时为 `~/.codex/skills`）。重载客户端后使用：

```text
使用 $solidworks-windows 检查我的 Windows CAD 环境，先给出部署计划。
```

安装 skill 不会自动安装 SolidWorks、桌面控制插件，或修改 MCP 配置。使用者需有可用的 SolidWorks 安装和授权。

## 部署到新工作区

前提：Windows、64 位 Python 3.12+，SolidWorks 能手动新建和保存零件。

在仓库目录运行 PowerShell：

```powershell
python .\skills\solidworks-windows\scripts\setup_workspace.py --workspace C:\AI-Workspace --dry-run
python .\skills\solidworks-windows\scripts\setup_workspace.py --workspace C:\AI-Workspace --install
python .\skills\solidworks-windows\scripts\configure_mcp.py --workspace C:\AI-Workspace --mode query
python .\skills\solidworks-windows\scripts\configure_mcp.py --workspace C:\AI-Workspace --mode query --apply
```

手动打开 SolidWorks，重载客户端后查询连接。查询通过后按 [部署说明](skills/solidworks-windows/references/setup.md) 升级建模模式。

工作区、SolidWorks 路径都可配置；脚本会拒绝覆盖内容不同的现有源码。配置更新先备份，并保留其他 MCP 服务和用户设置。

## 安装板示例

```text
使用 $solidworks-windows 新建 120×80×10 mm 安装板，四个 Ø8 贯穿孔，孔中心距相邻边 15 mm。
使用原生尺寸方案，验证完全定义、孔位、体积和重建错误，保存到工作区中的新文件。
再只改长度到 150 mm，验证右侧孔自动移动到 x=135 mm，并保存另一个版本。
```

基线理论体积为 **93,989.3807 mm³**。

服务仅修改本进程新建的零件，旧文件只读打开；仅向工作区 CAD 内的新 `.SLDPRT` 路径保存，拒绝覆盖。限制用于减少误操作，不是 Windows 权限隔离。

## 检查与测试

```powershell
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -e .\skills\solidworks-windows\assets\vendor\solidworks-mcp pytest PyYAML
& .\.venv\Scripts\python.exe -m pytest tests skills/solidworks-windows/assets/vendor/solidworks-mcp/tests -q
```

本次本地 176 项测试通过，并在带空格路径的新工作区完成部署与 3/19 工具握手。

本轮审查修复了文档归属与活动窗口切换、尺寸输入偏好恢复、带引号 TOML 配置更新、诊断退出码、Windows 保留路径和已有虚拟环境校验。详见 [审查记录](REVIEW.md)。

离线测试和协议握手不需要启动 SolidWorks。真实 CAD 验收按 [建模说明](skills/solidworks-windows/references/modeling.md) 执行；本仓库未包含用户模型或运行日志。

可选的 GitHub Actions 模板位于 [examples/windows-tests.yml](examples/windows-tests.yml)，可在账号具备工作流权限时复制到 `.github/workflows/`，测试 Python 3.12 和 3.14。本发布没有启用远程 CI。

## 结构与来源

- [SKILL.md](skills/solidworks-windows/SKILL.md)：供 Codex 加载的核心流程。
- `skills/solidworks-windows/scripts/`：部署、配置、诊断和协议验证脚本。
- `skills/solidworks-windows/assets/runtime/`：可移植的受限 MCP 入口和安装板配方。
- `skills/solidworks-windows/assets/vendor/solidworks-mcp/`：固定提交的上游 MIT 源码及本地修正。
- [上游与验证范围](skills/solidworks-windows/references/upstream.md)、[故障定位](skills/solidworks-windows/references/troubleshooting.md)。

本技能新增代码采用 [MIT License](LICENSE)。上游版权及许可保留在其源码目录。

官方资料：[Skills](https://learn.chatgpt.com/docs/build-skills)、[MCP 配置](https://learn.chatgpt.com/docs/config-file/config-reference)、[SolidWorks VBA 故障说明](https://www.solidworks.com/support/frequently-asked-questions?page=1&term_id=417)。
