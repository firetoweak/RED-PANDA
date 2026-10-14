# Windows 文件视图强制接入验证

2026-10-14，工作区基线 `007ff9b`。本轮验证命令进程能否通过原逻辑路径强制访问 Agent 文件视图，且不改变宿主编辑器看到的工作区。只做独立实验，没有接入生产命令启动器，也没有修复现有绝对路径绕过缺口。

## 结论

**本机未通过 `Job Object → Silo → Bind Filter → 当前 WinFsp 挂载` 的接入验证，不能据此进入生产实现。** Silo 创建与进程启动成功；文件绑定在启动测试命令之前失败。提权、补充进程权限、使用 BuildXL 的容器初始化步骤，以及缩小 WinFsp 绑定范围都没有消除失败。

这不否定 Agent 文件系统与沙箱的职责拆分。它否定的是“当前 Windows 挂载可以直接接上这一组机制”的实施前提。命令启动前必须接入指定视图、接入失败就不启动任务，仍是后续实现应满足的契约；仅改 cwd 仍不能满足它。

本轮之后已确认调整方向：保留按操作分流，独立只读操作读已发布宿主，可能写入的操作整次在挂载任务根中读写；保留实际路径身份，以进程 hook 拒绝任务命令直接写入宿主工作区，不再追求原绝对路径的透明映射。hook 尚未验证或接入，见[命令文件视图接入](../架构/能力/命令文件视图接入.md)。下文仍保留 Silo / Bind Filter 实验的原始结论，不将它改写成新方案的验证证据。

## 环境与方法

本机 Windows build 26300；`bindfltapi.dll` 为 10.0.26100.8655，`bindflt.sys` 为 10.0.26100.9549，绑定筛选器服务已经运行。WinFsp DLL 为 2.2.26215.fde790d / 2026 Beta4。临时目录所在 C 盘为 NTFS。

使用项目现有原生文件服务，程序 SHA256 为 `d1165f951c86d97fd9327220b01ae86a2813014d9f447a063d356c9ba9fdbcf2`。它建立的是 WinFsp 磁盘文件系统挂载。本轮未重建原生程序、安装组件、修改系统配置或文件权限，也未建立全局路径绑定。

原型按 [hcsshim 的任务范围绑定实现](https://github.com/microsoft/hcsshim/blob/main/internal/jobobject/jobobject.go#L446)创建空 Job，设置关闭时终止进程，提升为 Silo，再以非空 Job 句柄和 `BINDFLT_FLAG_USE_CURRENT_SILO_MAPPING` 调用 `BfSetupFilter`。进程创建属性同时指定 Job 和可继承的标准输入输出句柄，避免先运行后归入 Job。进程结束后终止并等待整个 Job 清空，再释放资源。

对照目录用不同标记区分宿主文件和视图文件。绑定成功后，脚本才会检查原绝对路径读写、扩展路径、子进程继承与宿主不受影响；本轮绑定均失败，这些检查均未执行。

## 实测结果

| 场景 | 普通权限 | 管理员权限 |
|---|---|---|
| Silo 创建、通过创建属性启动 Python | 成功 | 成功 |
| 普通 NTFS 目录绑定 | `0x80070005`，拒绝访问 | `0x80070005`，拒绝访问 |
| 当前 WinFsp 挂载绑定 | `0x80070005`，拒绝访问 | `0x801f000f`，筛选器拒绝附加到卷 |
| 直接读写 WinFsp 挂载并封存变化证据 | 成功 | 成功 |
| 丢弃临时文件操作、释放挂载、宿主标记保持原值 | 成功 | 成功 |

管理员对照进一步覆盖了已有／不存在的逻辑目录、NT 路径形式，以及在验证进程中启用备份和恢复权限，普通目录绑定仍拒绝访问。另一次运行分别启用驱动加载、卷管理权限，结果不变；这些权限仅影响临时进程令牌，执行结束前恢复原值。

按照 [BuildXL 的初始化实现](https://github.com/microsoft/BuildXL/blob/a6dce509f0d4f774255e5fbfb75fa6d5290ed163/Public/Src/Utilities/Native/Processes/Windows/ProcessUtilities.Win.Container.cs#L95)，增加空容器描述和 `WcCreateContainer`，创建与清理成功，但普通目录绑定仍拒绝访问。WinFsp 根和挂载内子目录都返回 `0x801f000f`，增加容器初始化也不改变结果。

`0x801f000f` 是 [ERROR_FLT_DO_NOT_ATTACH](https://learn.microsoft.com/en-us/windows/win32/com/com-error-codes-5)，不能按低 16 位解释成“找不到驱动器”。首轮脚本曾错误解码这个 HRESULT，保留的原始输出与校正副本明确分开，后续脚本已经修正消息解码。

微软在 [Windows Containers 的相关问题回复](https://github.com/microsoft/Windows-Containers/issues/335#issuecomment-1489497360)中说明，当时 Bind Filter 不支持这类自定义文件系统；[WinFsp 上游问题](https://github.com/winfsp/winfsp/issues/498)也记录了相同的卷附加失败。本机当前版本实测与该限制相符。该历史说明不能替代当前实测，也不能推导出所有 Windows 文件视图方案都不可行。

普通 NTFS 目录为何仍返回拒绝访问，本轮没有定位根因。不能简单归因为“用户没有点击 UAC”，也不能据此承诺换成服务账号就能工作。WinFsp 的附加失败是另一项独立阻碍。

## 验证边界与选型影响

原路径透明映射、任务子进程继承、路径别名、链接以及继承的宿主文件句柄均未通过验证。脚本使用句柄白名单，但没有完成绕过验证；Silo 启动成功也不代表文件接入成功，更不代表完整沙箱隔离。

现有原生程序的变化证据缺少当前 Python 发布层要求的 `mode` / `target` 字段。本轮只直接访问、封存并丢弃临时操作，没有执行发布或回退，不能宣称完整 Agent 文件系统链路通过。这个版本不一致与文件绑定失败分别记录。

| 候选 | 本轮判断 |
|---|---|
| hcsshim 的 Job / Silo / Bind Filter | 机制值得参考；直接连接现有 WinFsp 的方案未通过，暂停按它设计生产接口 |
| [BuildXL Detours](https://github.com/microsoft/BuildXL/blob/main/Documentation/Specs/Sandboxing.md) | 可参考进程拦截与文件访问语义；其容器初始化已补测，不能解决本轮 WinFsp 问题。用户态拦截需要另外证明覆盖范围 |
| [ModOrganizer2 USVFS](https://github.com/ModOrganizer2/usvfs) | 可参考按进程重定向文件路径；未进行本机原型验证，不能当作已验证的强制接入替代品 |
| Sandboxie | 用户已因重量排除，不继续评估 |

代码拆分仍按变化原因划分：Agent 文件系统拥有文件视图、证据、历史以及原生文件服务的生命周期；执行能力拥有任务命令及其进程树，并负责启动前接合指定视图；Sandbox 独立提供执行限制。平台接入方式未验证之前，不把某个 Windows API 或第三方沙箱写成这些边界的必需依赖。

## 证据与复现

数据目录为 [Windows文件视图接入数据](Windows文件视图接入数据/probe.py)：

- [首轮脚本](Windows文件视图接入数据/probe.py)、[路径与权限对照](Windows文件视图接入数据/controls.py)、[初始化与窄范围对照](Windows文件视图接入数据/prerequisites.py)。这些是当时执行的一次性脚本，不属于生产代码或默认测试集。
- [普通权限结果](Windows文件视图接入数据/unelevated.json)、[管理员原始结果](Windows文件视图接入数据/elevated-readable.json)、[管理员错误消息校正副本](Windows文件视图接入数据/elevated-corrected.json)。校正只修正 WinFsp 错误消息，保留原 HRESULT。
- [路径与权限对照结果](Windows文件视图接入数据/controls.json)、[初始化与窄范围对照结果](Windows文件视图接入数据/prerequisites.json)。

复现应先将脚本复制到新的临时目录，核对 `probe.py` 中固定的工作区路径及原生程序版本；`controls.py` 还需要按脚本中的名称准备目录和 HOST / VIEW 标记。管理员运行前先由普通权限创建输出 JSON，以便结果可由普通进程读取。不要直接在证据目录运行，也不要把历史输出当成新运行结果。

本轮实际运行目录为 `C:\Users\19403\AppData\Local\Temp\redpanda-silo-probe-44lj92tz`。最终 `fsptool-x64.exe lsvol` 成功退出且无卷输出；没有残留的 `redpanda-sandbox` 进程。记录中所有临时挂载均已释放，临时操作均已丢弃，宿主标记保持原值。
