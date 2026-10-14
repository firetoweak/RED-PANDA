# Windows 命令 hook 选型与原型验证

2026-10-14，Windows x64，普通权限。目标是保持实际路径身份，让受管命令访问文件视图，并拒绝直接写入宿主工作区。

| 候选 | 当时的判断 |
|---|---|
| Microsoft Detours | 复用函数拦截与进程接入能力，独立原型编译和实测通过，作为首选 |
| BuildXL DetoursServices | 值得借鉴覆盖范围；直接移植会连带引入策略、报告等协作模块 |
| Mod Organizer USVFS | 主要做路径重定向，与保持路径身份的目标不同，也与既有文件视图职责重叠 |

原型验证了宿主读取与拒写、挂载内写后读取、支持的子进程继承，以及主进程结束后的进程树回收。缺少必要组件时，任务入口没有执行。目录外硬链接别名曾暴露绕过，补充约束后对应回归通过。

当时默认 Git 初始化失败，独立对照将原因定位到 VFS 隐藏属性缺口；没有通过修改用户 Git 配置将它算作成功。后续处理见[隐藏属性验证](Windows隐藏属性.md)。

原型有额外执行成本，小样本文件创建测试不能代表完整构建性能。它也没有证明对主动绕过、代理服务或所有进程创建入口都有效，不能作为对抗性沙箱。

原型随后接入[生产命令链路](Windows命令链路接入.md)，不再单独维护实验版执行器。库选型参考[Detours](https://github.com/microsoft/Detours)与[BuildXL Windows sandbox](https://github.com/microsoft/BuildXL/blob/main/Documentation/Specs/Sandboxing.md)。
