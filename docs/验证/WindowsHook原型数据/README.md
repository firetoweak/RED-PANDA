# Windows hook 原型数据

这是探索性源码和断言，未接入产品，也不是生产测试目录。结论与支持范围见[验证记录](../WindowsHook原型.md)。所有试验文件都在 `.tools/windows-hook-probe/` 内新建，不对当前项目文件实施拒写测试。

从项目根目录使用现有 LLVM MinGW、Python 环境和 WinFsp 运行：

```powershell
& './docs/验证/WindowsHook原型数据/build.ps1'
if ($LASTEXITCODE) { throw 'build failed' }
foreach ($script in @('run_probe.py', 'git_control.py', 'limits_probe.py', 'metadata.py')) {
    & './redpanda-env/Scripts/python.exe' ".tools/windows-hook-probe/$script"
    if ($LASTEXITCODE) { throw "probe failed: $script" }
}
# 可选：重测小样本耗时。
& './redpanda-env/Scripts/python.exe' '.tools/windows-hook-probe/benchmark.py'
```

构建脚本固定上游 revision，首次获取 Microsoft Detours 需要网络。上游使用 [MIT 许可](https://github.com/microsoft/Detours/blob/1dbd4ea281d73d478aa8d09a8b83fbd265f715f7/LICENSE.md)。编译器默认为项目现有 `.tools/llvm-mingw-20261006-ucrt-x86_64/bin`；原生文件服务需要已经构建在 `redpanda/filesystem/bin`。

`hook.cpp`、`launcher.cpp`、`policy.h` 与 `hook.def` 是原型；`build.ps1` 额外构建缺失策略和故意接入失败的制品，只供故障断言。`cases.py` 在受管进程内执行断言；`run_probe.py` 从外部验证退出码、宿主状态、文件证据和进程回收。`git_control.py` 隔离现有隐藏属性限制；`limits_probe.py` 守住初版暴露的硬链接别名问题。`benchmark.py` 记录偏文件创建负载的探索性耗时，`metadata.py` 记录实际环境和 SHA256。

归档 JSON 为本轮最终结果。复现只更新 `.tools/` 下的结果，不自动覆盖这里的历史证据。
