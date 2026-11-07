# 小米 14 的 iOS26 迁移

`codex/houji-ios26` 从 iOS16 现有源码快照 `287c653e` 开始。用户在 r307 后结束
iOS16 调试并要求转向小米 15 参考项目所用的 iOS26。当前已经编译并链接 XNU12377 arm64e / HOUJI 16 KiB 内核和新 DarwinBoot。
2026-11-06 的 RAM 静态 PID1 镜像已在小米 14 实机运行，屏幕显示 Darwin 25.6.0 和
`HOUJI IOS26 ARM64E PID1 REACHED`。UEFI、标准 PAC、单核 XNU、RAM root 和最小
用户进程已有屏幕证据。完整系统已在单核配置下挂载 HFS 根卷并进入原版 launchd
主程序，桌面、完整图形链路和旧功能仍在验证。

## 固定输入

机器可读清单为 [`targets/ios26.json`](../targets/ios26.json)。参考项目固定在
[`f1ae906f`](https://github.com/superboyyy/hakios-arm64e-dada/tree/f1ae906fcc43ba8f763663257724ba938fb820ab)。
其 [ARM64E 说明](https://github.com/superboyyy/hakios-arm64e-dada/blob/f1ae906fcc43ba8f763663257724ba938fb820ab/base/docs/ARM64E.md)
记录 iOS **26.6.2 / 23G90**，早期使用 iPhone18,2；实际
[dada 启动脚本](https://github.com/superboyyy/hakios-arm64e-dada/blob/f1ae906fcc43ba8f763663257724ba938fb820ab/base/tools/dada-ios.sh)
使用 `23G90__iPhone18,1`，本分支按这个用户空间供体准备。

| 输入 | 固定值 |
| --- | --- |
| IPSW | `iPhone18,1_26.6.2_23G90_Restore.ipsw` |
| 大小 | 11,518,964,040 字节 |
| SHA-256 | `0ca5a8e383df508b464951410e9e8d627ead8454e47b57c21d06a27d0d04598c` |
| 用户空间架构 | arm64e |
| 参考项目源码内核 | `xnu-12377.121.6` |
| 实际板级目标 | houji / SM8650 / Adreno 750 |

内核标签来自参考项目的源码构建入口，不表示它等于供体 IPSW 自带的 kernelcache
版本。镜像从清单中的 Apple CDN 地址取得，放在仓库外；镜像、解密密钥、SDK、
固件及生成的系统卷均不能提交。

## 离线校验与组件清单

```bash
python3 tools/inspect-ios26-image.py \
  --image /path/to/iPhone18,1_26.6.2_23G90_Restore.ipsw \
  --output /path/to/ios26-inventory --check-crc
```

该入口只读镜像，检查完整大小、SHA-256、SHA-1、BuildManifest 版本、构建号与产品，
并按每个 build identity 枚举实际存在的组件路径、长度和 ZIP CRC。`--check-crc`
额外读取所有 ZIP 成员校验 CRC。输出目录必须全新，位于仓库外或忽略的 `.local/`、
`out/` 内。它不挂载卷、不解密 AEA，也不连接设备。

已对下载的完整镜像执行上述校验，三个 build identity 的组件均存在，全部 ZIP CRC
通过。迁移输入固定为 `Customer Erase Install (IPSW)` / `v53ap`，ApBoardID `0x0C`、
ApChipID `0x8150`；校验入口同时检查该身份和下列路径：

| 组件 | 此镜像实际成员 |
| --- | --- |
| OS | `094-95416-090.dmg.aea` |
| Cryptex1,SystemOS | `094-97238-092.dmg.aea` |
| Cryptex1,AppOS | `094-96441-094.dmg` |
| KernelCache | `kernelcache.release.v53` |
| DeviceTree | `Firmware/all_flash/DeviceTree.v53ap.im4p` |

后续据此提取组件，不要使用 Recovery identity 的 OS，也不要沿用 iOS16 的 DMG
文件名或 iPhone18,2 的设备身份。参考项目的系统卷和 cryptex 使用
AEA，需在本地取得对应 FCS 密钥并使用 macOS `aea` 解密，再核对 arm64e dyld cache
及子缓存。解密仍由本机工具完成；解密后的只读挂载可交给本分支的用户空间和系统卷
构建入口，镜像及密钥仍保留在仓库外。

## 迁移顺序和当前边界

1. 将参考项目的标准 PAC、arm64e 装载、chained fixup 与 XNU 12377 适配分离出来，
   保留其来源。先处理 XNU 8796 到 12377 的接口变化与构建依赖。
2. 将 houji 的 CPU/GIC、内存交接、UFS、触控、framebuffer、恢复计时及本机固件接口
   迁移到新内核。dada 是 SM8750 / Adreno 830；不能复制它的 MMIO、设备树、频率或固件。
3. 先验证新内核启动、只读文件系统和最小 arm64e 进程，再组装完整用户空间、系统
   cryptex 与 launchd。启动恢复路径验证完成后，才进入桌面测试。
4. 最后迁移并分别验证 NativeAdreno、Turnip、metal2vulkan 和 AdrenoMetal 的 ABI、
   渲染与显示路径。升级系统版本不构成紫色区域或动画卡顿已修复的证据。

当前 `board.toml`、`sources.lock.json` 的 `components` 以及 `tools/build-kernel.sh`
仍描述保留的 **iOS16 / XNU8796** 实现。它们不能用于宣称已经构建出 iOS26。
新目标由 `sources.lock.json` 的 `ios26` 显式关联；迁移完成前不将旧补丁
重标为新内核补丁。

iOS16 工作目录及暂存区保留。当前仅通过 `fastboot boot` 临时运行 iOS26 的 RAM
镜像，尚未安装完整系统卷；运行期日志搜集器保持关闭。主机检查不证明 GPU 正确性或帧率。


## 当前实现与验证范围

新入口为 `tools/prepare-ios26-sources.py`，输入固定在 `sources.lock.json` 的
`ios26` 节。它从 Apple XNU12377 和固定的 dada 补丁组装，按显式清单复用
SM8650/A750 驱动，随后应用新平台适配；不修改旧 iOS16 工作目录。
DarwinBoot 先应用旧版板级补丁，再应用 `bootloader/ios26`。

内核采用 arm64e、16 KiB CPU 页；启动器支持 Darwin 25 的 1024 字节命令行，
在真实内核上校验 format-7 chained fixups，先验证全部链再写入，生成每次启动的
PAC 密钥并验证硬件读回和签名指令。固件回调期间恢复固件密钥，退出固件后
交接内核密钥，副核入口也独立安装密钥。内核及主机输出不记录密钥。

HOUJI 平台保留自己的 GIC 地址/stride、禁止已知会 abort 的 redistributor
分组寄存器读取，并对唤醒/RWP 等待设置超时。已编译原生 UFS、A750 传输、
电池/ADSP、触摸、背光和 DVFS 单元；编译不代表它们在 iOS26 上已工作。
新 framebuffer 与原生 GPU 共享一个 IOAcceleratorES 类型，避免重复类定义。

首轮镜像由 `tools/build-ios26-first-entry.py`、`tools/make-ios26-adt.py` 和
`tools/make-ios26-first-entry.py` 产生，只运行单核内核和自编静态 arm64e PID1。
它使用 RAM root，不运行 StorageProbe，不请求 GPU/UFS，不改原系统卷，也不
启用运行期日志搜集器。定时返回 Recovery 的内核线程仅调用已验证为 SMC 的
PSCI reset，启动前必须读取并核实无 wipe 的 Recovery BCB。

内核实际链接成功且无未解析符号，UEFI 编译成功，真实内核静态重定位检查通过。
XNU 的配套 SDK 符号导出步骤仍报告参考工程缺少的 Apple 私有设备符号；这不是
完整 SDK 打包验证通过。PAC 与最小 PID1 的实机证据来自本节记录的手机画面；
主机测试不能替代手机验证，也不证明进入桌面或帧率。

## 功能对齐清单

| 功能 | iOS26 当前状态 |
| --- | --- |
| arm64e 内核、PAC、启动参数和 RAM root | 已实机运行至自编 arm64e PID1；屏幕证据确认 |
| 单核/PSCI/GIC | 单核内核和调度器已运行，GIC 初始化可见；多核仍待验证 |
| UFS/持久系统卷 | 原生 UFS 入口与 HFS715 已链接；完整 iOS26 root 已安装且 256 块读回一致，内核挂载与用户空间启动待验 |
| A750/Metal/Turnip | arm64e Metal frontend、bridge/std 和 Turnip 已实际构建、链接、签名；设备 GPU 待验证 |
| 多核、CPU/GPU DVFS、热控 | 策略保留，iOS26 启用与实机验证待完成 |
| 触摸、亮度、电池和 USB 充电状态 | 硬件适配已编译，完整系统服务待验证 |
| 状态栏、系统应用、键盘及旧用户空间兼容 | 键盘/状态栏库与供体应用注册工具已构建 arm64e 并接入系统卷；运行期和服务迁移待验证 |
| Wi-Fi、音频 | 未作为已实现功能；缺少 Wi-Fi 后端会明确返回不可用 |

本阶段不以编译通过、CPU framebuffer 拷贝计数或用户感觉代替 GPU/高刷证据。


## 启动修正与实机结果

早期 fastboot 确认接收并执行 RAM 镜像，但只看到 UEFI 和长倒计时。恢复测试后，
屏幕先定位到 `PAC setup: Not Ready`：当前固件未提供可用 RNG 服务，RNDR 也未供给
随机数。临时测试镜像现在独立生成完整的 80 字节 PAC 随机材料；加载器优先使用
固件 RNG/RNDR，缺失时消费该材料，并清零临时副本和交给内核的 ADT 属性。它不复用
内核的 random-seed，也不使用时钟伪随机。生成的 ADT/FAT 必须私有保存，每次测试
重新生成；本地启动器以独占消费记录阻止重复发送。可重复安装的正式启动仍需要接入
本机硬件随机源，此回退仅用于一次性 bring-up。

下一次画面显示 PAC fixup 完成，但 `secondary PAC trampoline: Load Error`。
`ASM_FUNC` 为起点、密钥槽和终点各生成了独立 COFF 段，链接器将它们重排。
密钥槽和终点改为同段标签后，实际 UEFI object 检查得到 104 字节代码、96 字节
槽偏移、16 字节段对齐、局部 ADR 和零外部重定位。

随后 UEFI 成功退出并执行交接汇编，绿色横条不是 XNU 已完成初始化的充分证据。
发现 `-v` 没有改变 Boot_Video.v_display，内核仍按图形启动隐藏文字。启动器现在
按完整参数 token 识别 `-v`，首轮配置使用 serial=0。修正后的实际画面显示
Darwin 25.6.0、GIC/VM/IOKit/HFS 初始化、mockfs RAM root，以及
`HOUJI IOS26 ARM64E PID1 REACHED`。这确认最小启动链已通过，尚非完整系统桌面。
全过程保持运行期日志搜集器关闭，没有改写物理系统卷、启动分区或 GPT。

菜单配置原有 `fallback_timeout = 120`。UniXNU 在启动项返回、又没有输入设备时
会使用它再次倒计时；它独立于内核的 90 秒恢复定时器。新最小配置改为首次 3 秒、
回退 5 秒，并用显式 `on_return = "reset"` / `return_timeout = 10`：保留失败画面，
显示错误码 10 秒后执行一次重启，不再反复重试。默认的普通 UniXNU 配置仍回菜单。
DarwinBoot 增加 Mach-O、ADT、内存分配/校验、PAC 等阶段的屏幕错误提示。最小 RAM
窗口改为 2 GiB，仍执行所有权和完整内存验证；完整系统的内存配置另行确定。

Mach-O、ADT 解析代码已提取为启动器/主机共同编译的同一份头文件；实际内核和
实际 ADT 及所有截断边界通过 ASan/UBSan。单次重启策略也通过完整 1..30 秒范围、
错误状态保留、无效参数和不重试检查。这些不替代手机验证。

HFS 改用 Apple `hfs-715.120.4`，固定在 `124b970f`，保留旧系统卷格式所需功能并
接回原生 UFS 根设备入口。SDK 的 TargetConditionals.h、vendor 固件和运行绑定头
均留在私有组装目录。`tools/build-ios26-kernel.sh` 明确构建内置驱动的独立内核，
不生成外部 kext SDK；原有 SDK 符号导出诊断不能因此视作已修复或已验证。

GPU 呈现从参考 RedwoodGPU fence 改接 houji 的 producer/current-tail fence。提交锁
覆盖缓存获取与 CPU 拷贝，挂起的帧保留引用并重试，不显示未完成的缓冲；释放锁
后才等待或回收。缓存范围必须落在保留的 IOSurface descriptor/map 内，逐 CPU 页
验证 Normal-WB/Normal-NC 属性，拒绝 Device/故障映射。HOUJI 不再额外发布参考用的
空 IOAcceleratorES，以免和实际 A750 provider 竞争。此修正只解决新平台适配缺口，
不是对旧紫色区域或高刷问题已经解决的声明。

arm64e Rust bridge 使用固定 nightly-2026-11-03、参考 PAC target 和重建的 std，
开启认证调用、认证返回、认证陷阱及 intrinsics。`sha2` 使用 Rust 实现以避免
cc-rs 把实验 ABI 当成 musl。实际归档代码检出 4259 个认证间接调用和 12791 个
认证返回（含 Apple 的 RETAB），不以 target JSON 声明代替机器码检查。
完整 Metal frontend 与 Turnip 已按 arm64e / iOS26 实际构建并签名。Turnip 固定
houji ABI v1，shader cache ID 包含源码树、目标与 ABI 头身份；拒绝普通 arm64 输入。
Metal 构建核对 bridge/std 与 Turnip 归档身份，关闭运行期日志、采样和帧追踪，
保留 sysmem,nocb 策略。主机链接和 PAC 指令不构成 GPU 渲染或帧率证明。

用户空间已构建带数字签名的 arm64e stage0/init/inject26，直接使用本机 HFS root
中的 /redwood，不挂载供体分区或 md1。Data 模板来自当前 iOS26 供体，Setup/迁移
记录使用 23G90；初次完整系统采用 ramfs Data，持久化和旧数据迁移尚未验证。
完整 HFS 镜像已独立组装，保留 stock launchd、系统应用、OS/App cryptex 和
ExclaveOS 资源，并接入本机 Metal bundle、键盘/状态栏兼容及应用注册服务。
校验分别核对供体 OS 与实际 graft 的各 cryptex，避免将已替换的占位目录与真实
cryptex 的所有者和权限误作比较；原版供体挂载保持只读。
触摸/亮度/电池/多核、完整桌面与应用运行期验证仍未完成。

## 完整系统卷的离线结果

2026-11-06 已生成独立的 32 GiB HFSX 镜像。共核对 462,899 个供体条目的类型、
权限、数字所有者、符号链接及普通文件内容；HFS 文件系统检查通过，剩余空间
12,469,788,672 字节。镜像包含 548 个 launchd job，运行期日志搜集器保持关闭。
生成后镜像设为只读，SHA-256 为
`79540c8a19dcc9fa0c1961283e6966ad908166d6ebdb5edfe3d1b8228f5319a2`。
以上离线检查不证明 launchd、系统应用或 GPU 已在手机运行。用户恢复测试授权后，
已在 OrangeFox 将镜像写入既有的 32 GiB 窗口，256 个数据块全部直接读回一致，
启动分区、各 GPT、misc 及系统卷上边界保持不变，原 iOS16 完整快照保留。

完整系统测试需要重新绑定本次 UFS/GPU 启动资格材料、核对 Recovery 恢复入口，
并准备与新内核配套的多核和内存交接配置；不能直接复用单核 RAM PID1 镜像或
旧轮次 GPU 绑定。当前八核 ADT 保留 houji 的全部硬件节点，仅取供体的非唯一
用户空间型号标识。完整系统 UEFI 补丁采用 Darwin 25 的 1024 字节命令行，
保留 UFS/GPU 启动资格检查，关闭运行期追踪，定时恢复设为 300 秒。
完整系统 ADT 还生成当前主机时间的 64 位 `boot-epoch`，供 GenericARMPE 初始化
系统时钟；移除该平台不识别的旧 `houji_epoch` 参数。这不提供硬件 RTC 或跨重启校时。
首轮完整系统测试在 UEFI 的检查文件创建处返回 `Already started`，屏幕确认 UFS、
GPT 和 FAT 已通过，但 XNU 尚未进入：上一轮 `UniXNU.log` 仍存在，防覆盖检查拒绝
创建新文件。每次测试准备必须保存稳定的原分区备份、腾出旧检查文件名，并在发送
镜像前再次确认该文件不存在；不能通过跳过检查继续启动。修正版在任何 GPU 准备
之前读取并核对精确的 Recovery BCB，若检查文件阶段失败则保留 UFS 异常处理器，
显示错误 10 秒后直接重启。无法取得实际 BCB 证明时仍停止，不卸载持有处理器的应用。
第二次完整系统画面已确认触摸交接、8 GiB 内存校验和 PAC 成功，并执行到加载器
绿色横条；尚无 XNU 输出。退出 UEFI 时的零载荷缓存刷新被关闭后的数据写入门拒绝，
现将精确的 LUN0 `SYNCHRONIZE CACHE` 与仍需 armed 的 `WRITE10` 分开处理。
下一轮以启动期色条区分缓存清理、MMU 关闭、真实内核入口及初始页表准备；颜色
解释与已观察边界见[设备测试](device-tests.md)。运行期日志搜集器继续关闭。
阶段标记版已实机执行到白色，即真实内核入口和初始页表构造。保持 8 GiB 的单核
对照进一步通过原生 UFS、HFSX 身份及单核定时器检查，但在保留的 ZAP 元数据页
Normal-NC 校验处拒绝；未进入桌面。预留页映射策略仅登记本轮已完整验证的两个
16 KiB 页，未放开周围地址。随后同一单核 / 8 GiB 配置已实机通过 PAS 认证、三种
原生 2D 提交和 1,024 像素 / 8 标记回读，CRC 为 `e843b26a`；原 GPU owner、root 和
ring 保持有效，HFS 根卷挂载后开始尝试 `/sbin/launchd`。这不证明 Metal shader、
桌面或高刷新率。用户报告自动返回 OrangeFox，最后终止原因尚未取得。

终止路径新增有界屏幕输出：复用已有 console 虚拟映射与内核字体，区别测试到期、
panic 和系统终止，panic 显示已有错误文本，最多保留 90 秒，且不超过原测试期限。不在该路径分配内存、
重映射 MMIO、写 UFS 或恢复已退出的 UEFI；测试到期的显示时间计入原定时上限。
该显示路径已编译并通过像素 / stride / 容量边界检查，实机可见性仍需验证。
八核问题与完整用户空间启动分别验证，运行期日志搜集器保持关闭。

本机 ADT 不提供 iBoot panic 缓冲区，因此同时从现有的两处 panic formatter 保存
首条错误，后续嵌套错误不能覆盖它。记录只位于当轮已分配的内核 RAM，带本轮
标识和 CRC，panic 只维护该内存的缓存。正常启动检查文件的保留头部记录精确定位，
不增加文件、写入次数或 panic I/O。正常启动时先初始化带校验的空记录，受控终止时
保留对应原因，避免将“未捕获 panic”与记录丢失混为一谈。可选的下轮 UEFI 读取默认关闭；私有测试必须
提供上轮校验过的精确定位和标识，重新验证 GCD SystemMemory、owned 范围、
空闲页保留和真实 Normal-memory 翻译后，只读取 544 字节。标识或 CRC 不符时
不接受任何错误文本。两次独立读取中，UEFI 的 GCD / 空闲页 / Normal-WB 翻译检查
通过，但实际 RAM 内容不再匹配本轮标识与 CRC；当前 Recovery 路径不能依赖该机制
恢复错误，不能由记录缺失推断没有 panic。
独立读取配置在保存启动检查后、任何 GPU 准备之前，通过已核对 Recovery BCB 的
复位路径结束；其 RAM payload 删除 kernel / ADT 文件，并清空对应空闲 FAT cluster，
不将该镜像用作新的完整系统启动结果。

`houji_pid1_startup=1` 是独立、默认关闭的启动资格选项，仅在 native root 与有界
Recovery 测试同时启用时生效。它最多保存八条固定路径的 PID1 exec 开始 / 返回、任务创建、Mach-O 激活、签名与交接结果、
`initproc_spawned` 接受及已授权用户空间重启请求；重启消息沿用内核已检查的副本。
写入复用既有启动文件和范围检查，仅在未进入 panic、允许中断的正常上下文进行。
stock launchd 的 main 通知在独立的一次性线程保存，避免在 sysctl 锁下做存储操作。
另设 `houji_pid1_services=1` 后，该线程在 main 检查点之后等待约 30 秒，再多保存一条
服务状态。记录包含五个指定进程在该时刻的存在情况和一次 GPU 提交计数查询结果。
查询返回 0 时，提交数和完成 fence 有效。首条终止原因仍有独立额度，测试没有周期采集。
启用该选项的 mode 3 测试还在退出循环后保存一次 `gpu-runtime-end`。它记录退出码和
退出前的 GPU 计数，供下一次 Recovery 自动回读。成功停止 provider 后显示正常回收。
首次完整系统使用的 Data 仍是 ramfs，持久化、旧数据迁移和所有既有功能的 iOS26
运行期验收尚未完成。

## 本地构建入口

- `tools/prepare-ios26-sources.py kernel`：提供 Apple XNU、固定 hakios、固定 HFS
  的 Git checkout；可通过 `--target-conditionals` 提供本机 SDK 头文件。
- `tools/prepare-ios26-sources.py bootloader` 与 `boot_manager`：分别组装 DarwinBoot
  和带单次返回策略的 UniXNU；上游目录只读，输出必须是新目录。
- `tools/build-ios26-kernel.sh`：使用 `HOUJI_XNU`、`HOUJI_REFERENCE` 指定私有依赖。
- `tools/make-ios26-system-adt.py`：从已验证的本机八核 ADT 生成完整 HFS 系统配置，
  保留物理节点和空 GPU 交接槽，每次产生独立 PAC 材料；仍需本次设备启动绑定。
- `uefi/ios26/patches/local.patch`：应用到已组装 `uefi/overlay` 的私有 UEFI 树，
  扩展完整系统的命令行预算并设置有界恢复；配套 `uefi/ios26/overlay` 同步到私有
  UEFI 树。首次 PID1 配置不使用此入口。
- `tools/build-ios26-bridge.py`：本地 nightly prefix、作者提供的 AIR 和独立 Cargo cache；
  工具链分发版本/hash 与 PAC target 记录在 `sources.lock.json` 的 `ios26.rust`。
- `tools/build-userland-compat.py --profile ios26`：键盘与状态栏的 arm64e 库。
- `tools/build-system-apps.py --profile ios26 --system-root ...`：只读扫描供体实际应用，
  生成专用目录并构建注册工具，不复用 iOS16 固定目录，也不安装或注册。

- `tools/build-ios26-turnip.py`：从独立的固定 Mesa 源码树编译 arm64e Darwin ABI v1 库，校验导出与签名。
- `tools/build-ios26-metal.py`：仅接受匹配的 arm64e bridge/std 与 Turnip，生成关闭运行期诊断的 Metal bundle。
- `tools/build-ios26-userland.py`：从固定参考源码编译最小 HFS stage0、init、inject26 和当前供体 Data 模板。
- `tools/build-ios26-root.py --plan`：核对稳定 Recovery 备份、只读供体挂载及已构建组件，输出本地组装方案。
  实际组装需要 macOS 管理员认证以保存 iOS 数字所有者；只操作新建的本地 HFS 镜像，不能安装手机分区。

完整系统的可选 coherence mode 5 不再把 status 11 的不确定脏缓存前提当成 GPU
资格失败。iOS26 专用回退要求 phase 3、八次尝试、合法的 CPU 别名、没有提交
copy/render 和空闲的原 owner；两组探针映射移除及 owned scratch 的 clean/invalidate
全部成功后，保留资源并继续原模式 3，显式缓存维护保持启用。其他错误仍走原停止
路径。此改动不宣称硬件一致性，也不改动 iOS16 的共享实现。

ADSP 沿用的固件写入在 iOS26 单独适配：外部 carveout 的 WriteCombine 请求可能
成为 Device 映射，因此段复制和尾部清零使用范围验证的 volatile 32 位访问，保留
首尾部分字的相邻字节，拒绝地址/长度溢出。实际 AArch64 编译检查无 memcpy、
bzero 或 DC ZVA。映射范围、MDT/PAS 资格和 GPU Normal-NC 白名单保持不变。
DTB 元数据认证前后各有一次启动检查，运行期采集器仍关闭。

根挂载证据修正：v13 的程序头与文件长度表明原生块设备曾被 mockfs 抢先接受，
先前仅凭 HFSX 签名与 BSD root 设备名推断 HFS 已挂载并不成立。完整系统现在
对 native_root=1 只选择动态注册的 hfs；HFS 错误不能静默落入 mockfs。最小
RAM 单程序探针仍用原选择。只有实机出现 HFS callback 成功、正常文件长度及
Mach-O 程序头，才能认定实际文件系统链条通过。

v14 已确认 HFS callback 成功、首个文件的正常长度/arm64e Mach-O 头及
EL0 辅助链；原版 launchd 进入 dyld 后因 libSystem 不可用终止，尚未到 main。
Cryptex 内容需放在 `/private/preboot/Cryptexes/{OS,App}`，并保留供体的
`/System/Cryptexes` 相对链接；只将内容放进替换后的 System 实体目录会遗漏
canonical preboot 视图。镜像作者现在检查这两个视图和 arm64e cache header。
有限启动记录改为七个普通节点加一个首条 critical-exit/reboot 原因，仍最多八条；
退出描述在内核已验证 reason buffer 内截断，只在正常上下文保存，panic 不写盘。

`houji_pid1_detail=1` 才开启已用于定位初始程序头的深层装载节点。默认有限记录
仅覆盖三个 exec 的开始/返回与 stock main，给后续链条留出七条普通额度；退出
原因始终有一条独立保留名额。新本地 v4 镜像已经验证两处 canonical Cryptex
视图、原始链接/数字所有者、86 个未改动的共享缓存文件及文件系统。v15 已安装并
回读验证，但 stock launchd 仍报 `no such file, no dyld cache`；新保留的退出记录
与用户照片一致，已经取得完整原因，尚未到 launchd main。

扁平 HFS 还需提供 dyld 的普通目录入口：
`/System/Library/Caches/com.apple.dyld` 相对链接到现有 OS Cryptex 的缓存目录。
这让未能经 ignition 发现缓存的加载器仍可尝试系统默认路径；不改写缓存字节，
不绕过共享区域或签名验证。镜像作者拒绝覆盖该位置的既有目录/其他链接，并验证
最终目标仍在 canonical Cryptex 内。
查找行为参考 [Apple CacheFinder](https://github.com/apple-oss-distributions/dyld/blob/fd8d0c4d52320ebf64db34f3cb280310d905c5ae/dyld/DyldProcessConfig.cpp)；
供体实际 dyld-1387 同时含上述默认路径及 ignition 诊断字符串，不能将参考源码
本身当作供体运行轨迹。

v16 使用新增默认缓存入口的 v5 根卷。自动回读通过本轮标识及 CRC 校验，内核时间
约 86.8 秒记录 `stock-launchd-main`，`initproc_spawned` 已由内核接受。此轮越过了
之前的缺库退出。v17 的单次回读确认五个系统进程存在，包括 backboardd 和 SpringBoard。
当时 GPU fence 为启动自检的 98，尚无 backboardd 提交或 framebuffer 交换。

完整系统配置按 iPhone18,1 供体报告 1206×2622 的逻辑显示尺寸。小米 14 的实际
framebuffer 几何继续从启动交接取得，由显示驱动负责缩放。驱动复用设备树中类型
匹配的 `/arm-io/disp0`，缺少该节点时才添加。固定启动文件同时保留节点状态、
逻辑尺寸、framebuffer 客户端启动数与已调用 selector 的位图。位图使用原有的有限
`fbtrace` 入口，便于把等待显示服务与连接之后的初始化分开定位。
