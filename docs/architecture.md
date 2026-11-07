# 代码结构与运行路径

本分支正在迁移小米 14 的 iOS26 / arm64e 内核与启动链，目标与迁移状态见
[iOS26 迁移](ios26-bringup.md)及 `targets/ios26.json`。XNU12377 与最小 arm64e PID1
已经实机运行。单核完整系统测试已挂载 HFS 根卷，完成原生 GPU 2D 启动自检并进入
原版 launchd 主程序。单次记录已确认 SpringBoard 等五个进程存在，桌面画面、Metal
shader 和高刷新率仍待实机验证。
下文描述保留的 Darwin 22 / iOS16 实现，其功能状态不能直接当作 iOS26 的验证结果。
它与最新 UniXNU 的板级模块格式并非完全相同；
不要将其他 SoC 的设备树、内存地址、频点或 MMIO 写入直接用于 houji。

## 系统组成

```text
Qualcomm bootloader
  └─ Mu-Silicium UEFI + houji board package
       └─ DarwinBoot：内存布局、ADT、固件和设备状态交接
            └─ XNU 8796.141.3 / SWEET
                 ├─ CPU、GIC、定时器、调频
                 ├─ UFS / HFSX、触控、framebuffer
                 └─ NativeAdreno 内核驱动
                      └─ Turnip Darwin → metal2vulkan → AdrenoMetal → iOS compositor
```

图形调用由 Metal 前端翻译到 Vulkan，再由 Turnip 生成 Adreno 命令；内核负责内存映射、提交、完成同步和设备状态。箭头表示依赖关系，不表示所有图形特性均已实现。

## 有效源码

- `kernel/patches/local.patch` 修改固定版本 XNU 原有文件；`kernel/overlay/` 提供新增驱动和兼容实现。
- `kernel/overlay/iokit/Kernel/houji-ufs-root/` 包含 UFS 及 GPU 运行路径，`Adreno740/` 子目录是该路径使用的 NativeAdreno 组件。目录沿用上游命名，实际目标是 A750。
- `kernel/overlay/iokit/Kernel/arm/` 包含 CPU、framebuffer、HID 与平台适配；既有汇编和调度器改动在内核补丁中。
- `uefi/overlay/` 是 Mu-Silicium 的板级覆盖层。主树、Mu_Basecore、Silicium-ACPI 的修改分别保存为补丁。
- `bootloader/` 对应 DarwinBoot 的固定版本补丁与新增头文件。
- `graphics/metal/` 是当前使用的 Objective-C++ / Rust 桥接源码；`graphics/include/` 保存共享 ABI、PM4 辅助代码及自有测试 shader。
- `graphics/runtime/overlay/` 是固定版本 metal2vulkan 的替换文件，不是完整依赖副本。
- 运行时版本由 `sources.lock.json` 固定；覆盖文件保留本机适配。`tools/build-metal.py` 校验源码身份并使用桥接 Cargo 锁构建。
- `graphics/turnip/` 包含 Mesa 修改以及 Darwin 内核后端，含 BO 释放顺序修复。
- `userland/` 保存键盘几何等适配。显示驱动的当前有效版本以 `kernel/` 为准，用户空间目录内的显示调查副本供对照参考。
- `userland/apps/` 保存原版系统应用的安装清单和一次性注册程序；离线迁移、启动接入及验证边界见[系统应用恢复](system-apps.md)。仓库不包含原版应用文件。

`sources.lock.json` 固定上游提交；`source-manifest.json` 固定本次发布的文件校验和。更新源码后应同步更新清单，不能只修改历史实验中的同名副本。

## GPU 内存与提交

NativeAdreno 使用共享 GPU 地址空间和五个客户端槽位，主合成客户端窗口为 2 GiB，
对象上限为 4,096。Preferences 使用独立的第五槽位，需要配套的
`graphics/metal/NativeAdrenoMetal.clients`。内核句柄索引加快对象查找，仍保留
调用者、权限、范围和生命周期检查。地址布局与限制见 [GPU 地址空间](gpu-address-space.md)。

提交 ABI 通过能力查询选择：旧接口最多 16 段、结构 416 字节，新接口最多 128 段、
结构 3,104 字节。扩展批量大小不改变 fence、缓存维护或资源退休条件。

当前用户态采用 [完整替换图形栈](gpu-replacement.md)：完整 AdrenoMetal 前端及 Rust bridge、
`624bab39` 开发版 metal2vulkan 与配套 28 个补丁，以及重新从固定 Mesa 树组装的 Turnip。
不再以 `219f0136` 上的局部方法回填作为当前实现。内核继续使用 houji 的 A750 ABI v1。
r303 确认启用 GPU，但颜色闪烁与紫色问题导致画面验收失败；当前实现不具有渲染或
高帧率资格，见 [r303 分析](gpu-r303-analysis.md)。

Turnip 整块 BO 缓存保留已完成自有分配的句柄、映射和 CPU lease，按尺寸和权限匹配，
在内存压力及分配失败时清退，并清退超过一秒未复用的条目。队列与条目数组动态增长；
旧实现的 128 项 / 64 MiB 上限不适用于当前版本。释放失败保留所有权和 IOVA。
稀疏 BO 槽的最终清零仍在退休锁内，避免并发分配重用后被旧析构覆盖。
运行时独立保留最多 256 MiB 图片备用内存，以及最多 128 MiB 待释放内存。

ABI v1 的 selector 17 / flag 2 表示批量提交；新上游 ABI v2 在这些位置表示优先级和
owned coherence。当前适配显式拒绝 v2 优先级/日志请求，自有 BO 继续按 v1 的 WC 策略映射，
不会把批量提交能力误当作缓存一致性能力。

主机内存导入使用原调用者 task 固定页面。GPU 未完成时释放 issuer 锁睡眠，保留
mutation reservation；重新取得锁并确认可执行后，仍检查设备静止及 VM 状态。
超时、关闭、停机或导入失败均不重试已执行的固定页面操作。

Metal no-copy 桥接检查 VM 对象及偏移，避免同一虚拟地址重新映射后继续使用旧页面。
检查在固定页面前后进行，兼顾写时复制；记录最多 8,192 项，单次最多遍历 4,096 个区域。
未知身份使复用失效，旧资源仍由原命令生命周期持有。

## 缓存一致性与完成同步

桌面测试模式在每次启动执行双向 CP 拷贝和八次固定图像回读，全部通过才为符合条件的
WB / inner-shareable 导入启用一致性优化。其他映射继续维护缓存。自检失败不发布该快路。
这是有界启动资格检查，不替代完整 IOSurface、shader 或颜色正确性验证。

SMMU 撤销使用 ASID invalidate 和 TLBSYNC；周期检查核对路由及 context 冲突，
初次附着和显式审计仍遍历完整页表，每次事务仍验证变更叶子。

pending fence 的锁外读取只提供“仍未完成”的提示。完成字必须先验证精确 PA 与
Normal-NC 映射；真正完成、超时或资格不足时仍取得 issuer 锁验证。
设备停止撤销提示资格，不跳过错误传播和退休检查。

## 显示与交互调度

当前显示路径通过 CPU 复制 framebuffer。已有原生调用观察到 fence 推进时唤醒复制线程，
等待预算为 8.333 ms；唤醒后重新检查 producer 和当前 tail，复制期间持有 issuer 锁。
该路径没有新增 GPU IRQ 或面板 vblank 同步。软件 120 Hz 通知不代表面板扫描或桌面 FPS。

CPU/GPU 使用本机工厂表和有效热上限。CPU 交互下限要求近期域负载，后台不维持额外下限。
GPU 仅对空闲至少 80 ms 后的新触控段，在报告不足 50 ms 时请求短时预升频；连续事件不续期。
HFI 确认、频率表校验及 TSENS 结温限制保持优先。机身、电池温控与紧急关机尚不完整。

## 电池与系统服务

`kernel/overlay/iokit/Kernel/houji-ufs-root/battery/` 包含 ADSP、PMIC GLINK 协议及
`IOPMPowerSource` 实现。当前仅在显式 `houji_battery_probe=4` 模式启动。
固件从本机校验范围内的 `modem_a` 读取，资源来自本机设备树；电池通路只读标准属性。
真实电量与 USB 插拔状态已经实机验证，充电器设置未接入。详见 [电池接入](battery.md)。

所有本机驱动编入内核，SWEET 目标启用 `no_kextd`，避免等待当前 iOS 根卷不存在的
macOS IOKit daemon。普通服务匹配和 busy 状态保留。`HoujiNetworkStack.cpp` 提供
InterfaceNamer 服务的受限入口，尚未实现接口命名，也不代表 Wi-Fi 已适配。

Metal 对象提供稳定的 `globalTraceObjectID`，命令缓冲提供实例独有的 `userDictionary`。
对象还声明对应的 Metal 协议，供框架在调用接口前检查类型。iOS 16 的
`MPSCommandBufferImageCache` 会拒绝未声明 `MTLCommandBuffer` 的对象，即使对象已有
命令方法和字典；r296 的壁纸进程在后续使用空缓存时崩溃。
协议声明参考 [AdrenoMetal 的对象兼容修复](https://github.com/UniXNU/AdrenoMetal/commit/01bfb259b19c28739068357dd5a82f4d7e028201)，
不改变 GPU 地址、同步语义或功能查询答案，未知 selector 按新上游签名转发并记录首个缺失项。r299 的按名称查找
只注册了 17/19 项；后续候选改为编译期 `@protocol` 引用，使协议定义随镜像加载。
主机检查覆盖全部引用、类型检查和字典生命周期；r300 已记录 19/19 注册，但用户确认卡顿和紫色区域毫无改善。该结果不能作为新栈的硬件验证。

系统应用恢复保留原版 bundle 内容和所有权，由一次性服务完成注册。AMFI 的 dyld 路径
兼容仅允许清单中的平台应用展开包内路径。TCC 兼容仅覆盖 tccd 的精确 MediaLibrary /
Photos 文件扩展调用，并保留原授权判断；当前 SWEET 无沙箱隔离，不能据此宣称完整权限
隔离已实现。接口范围和设备验证状态见 [系统应用](system-apps.md)。

## 诊断与验证

`NativeAdrenoLatency.h` 区分锁等待、board gate、core dispatch 及其他耗时，保留有界慢调用。
固定日志前缀保存 GPU、VM、显示、调频和服务计数，避免退出消息覆盖关键证据。
可选 CPU 采样器观察 backboardd 合成线程；采样会暂停线程，需在结果中说明其扰动。

[主机检查](development.md)验证实际代码的 ABI、边界、并发和错误路径；
[设备测试](device-tests.md)记录日志校验、场景与用户观察。GPU 命令时间、软件刷新次数及
主机测试均不能单独证明端到端帧率或画面正确性。

## 历史资料

`archive/` 保留早期实验与调查资料，不是默认构建入口。其中脚本可能绑定旧镜像、分区
布局和工具链，应按当前开发指南重新核对依赖。设备测试编号用于关联证据，不是发行版本；
项目尚未提供经过全新环境验证的通用刷机流程。
