# P1 物化代理跨源冒烟：2.mp4（重复 PTS + VFR 疑难源）

> 日期：2026-10-13（owner「都补，开工」）
> 探针：`.cache/v3ass/probe_proxy_cross_source.py`（R8 同源：真 `main.main()`）
> 结果：`.cache/v3ass/probe_proxy_cross_source_results.json`
> 源：`D:\qq下载\920\2.mp4`（750MB / 3071.8s / 184293 帧 / 15.6Mbps）

## 0. 为什么测 2.mp4

P1 全链此前只在 **4.mp4**（正常源：CFR、PTS 单调）验证过。2.mp4 是工作集里
的"疑难形态"：**系统性重复 PTS**（~21% 帧卷入相邻同 PTS 对）+ VFR 抖动 +
片头密集重复段——认证/映射/代理构建的最难形态。跨源验证=「P1 通用可用」
的最后盲区。

## 1. 全链结果（T0）

| 步 | 实测 | 判定 |
|---|---|---|
| T1 导入 | 0.19s / 184293 帧 / 59.9993fps | ✓ |
| T2 media_info | 1.58s | ✓ |
| T3 分析 | 缓存命中 0.02s（首跑冷分析实跑并落缓存 1.5MB，key `789ea0564afa`）| ✓ |
| T4 代理就绪 | **1.09s**（缓存命中 key `3dd921b9…`；**首次全量构建 135s / 3337 帧 / 217MB / 55.6s 内容**）| ✓ |
| T5 4x 播放 | mode=**proxy**、speed=4.0、playing | ✓ |
| T6 源帧推进 | **3372-3444 源帧/秒 = 40.0x 达标**（0.2x 段，UI 4x）| ✓ **40x 在疑难源成立** |
| T7 编辑 | seg 0（帧 0-5143）mode auto→all，删除段 683 个 | ✓ |
| T7b 编辑后立即续播 | mode=**dthrough** speed=20.0（**回落直通不卡**）| ✓ |
| T8 重建 | **0.15s**（批级缓存命中，仅重编受影响批）→ 新 key `fbd3611f…`，自动回 proxy | ✓ |
| T9 暂停态 seek | 目标 20000 → **落点 20000，delta=0 帧** | ✓ 帧精确 |

## 2. 两条探针口径修正（教训留档）

**修正一：编辑触发必须是真实变更。** 首跑选中的段 mode 本来就是 `all`，
改 `all` 是空操作、不触发重建 → 改为「选第一个 mode≠all 的段改成 all」。
（症状=等待重建条件永不满足，探针挂死。）

**修正二：seek 目标必须选"确定保留帧"。** 首跑 T9 目标 50000 落在
暂停段 213（49974-50007，mode=all 全删）内部 → seek 按设计**钳到下一保留
帧 50008**（delta=8）。这**不是缺陷**，是「删除段内源帧的 seek 钳位」既有
语义（与 EDL 一致，ASSET §25.4 已登记）。改用确定保留帧 20000（0.2x 段）
后 delta=0。→ **探针口径缺陷两枚，产品零缺陷。**

**修正三：R9 补丁缺失（首跑被挂 20 分钟）。** 探针未替换 messagebox →
分析完成后的「已缓存」提示弹窗把进程挂死（CPU 空转 0.23s/5s）。修复=
补 `showinfo/showwarning/showerror/askyesno/askokcancel` 替换。

## 3. 与 4.mp4 的对照

| 指标 | 4.mp4（正常源）| 2.mp4（疑难源）|
|---|---|---|
| 全量构建 | 233-267s / 1.2GB | **135s / 217MB**（源更小）|
| 编辑重建 | ~10s（W=4k）| **0.15s**（批缓存命中）|
| 播放 40x | 40.0x 达标 | **40.0x 达标** |
| seek 精度 | 0 帧 | **0 帧** |
| 编辑后回落 | dthrough 不卡 | dthrough 20x 不卡 |

**结论：P1 物化代理在疑难源（重复 PTS + VFR）上全链成立**——认证/映射/
构建/播放/编辑/重建/seek 七环节零缺陷。「P1 通用可用」盲区关闭。

## 4. 复现

```bash
export ARKNIGHT_PREVIEW_ENGINE=mpv
python .cache/v3ass/probe_proxy_cross_source.py
# 结果 -> .cache/v3ass/probe_proxy_cross_source_results.json
```

副作用：探针在桌面上开真实窗口（R8 同源要求），已 `iconify()` 最小化；
结束时自动 destroy。首跑冷分析约 5-6 分钟（184k 帧 A_PT），此后缓存秒过。
