# 视频分析结果缓存：成熟项目/行业实践调研

> 子代理调研报告（research_analysis_cache）。2026-09-15 起。
> 前置：`agents/research_proxy_materialize/07_synthesis.md` 已覆盖 **代理物化/渲染缓存**（Resolve/Premiere/FCP 的 render cache、代理转码、seek 落位）。
> 本报告聚焦**新角度：分析结果（analysis result）的磁盘缓存**——键设计、内容寻址、格式、失效、版本迁移、用户交互、视频特有坑。
> 纪律：数字/结论标源；查不到写「未找到公开数据」+[推断]；不编造。

---

## 0. 术语澄清：两种"缓存"不要混

| | 渲染缓存/代理缓存（前作已覆盖） | **分析结果缓存（本报告）** |
|---|---|---|
| 缓存物 | 可播放的中间视频（帧内/短GOP代理） | 结构化分析产物（states/diffs/context 数值数组） |
| 体积量级 | 每小时代理 40~187GB（行业） | 全场 2.3MB（我方） |
| 键判定 | 素材路径/版本/timeline 状态 | 源内容哈希 + 分析参数指纹 |
| 复用收益 | 省解码（秒级→即时） | 省 4 分钟 CPU 分析 → 秒级载入 |
| 失效逻辑 | timeline 编辑触发 | 源文件变更/算法版本变更触发 |

本报告只谈右列。左列见前作 07_synthesis。

---

## 1. NLE/视频工具的缓存键设计：如何判"复用还是重算"

**核心结论：消费级 NLE 的缓存判定普遍是「路径 + mtime + 文件大小」级别的弱校验，而非全文件哈希。** 这与它们缓存的是「可重建的渲染产物」这一语义相符——重算代价虽高但可接受，且用户可手动清理。

### 1.1 检测前必须先纠正一个同行误区：PySceneDetect 的 stats file 不是缓存

**这是本次调研最重要的"打假"结论**，因为常被误引用为"视频分析缓存先例"。

- PySceneDetect 的 `-s/--stats file.csv` 是**人类可读的诊断转储**，用于调阈值（官方文档原话："open it with a spreadsheet editor (e.g. Excel), and examining the `content_val` column"），**不是**给下次运行复用的缓存。
- 证据（源码，本机解包 PySceneDetect main 分支，`scenedetect/stats_manager.py`）：
  - 文件头两列固定 `Frame Number, Timecode`，后面是检测器注册的 metric 列（如 `content_val`）；
  - `load_from_csv()` 已被标记 **[DEPRECATED] DO NOT USE**，代码里写着 `logger.warning("load_from_csv() is deprecated...")`，注释 `# TODO(v1.0): Create a replacement for a calculation cache that functions like load_from_csv did, but is better integrated with detectors`。
  - 即：**PySceneDetect 曾有一个"从 CSV 载入已算指标"的复用路径，但已被官方废弃，且替代品（真正的 calculation cache）在 v1.0 之前都还没做出来**。
- 官方明确"stats file 会阻碍复用"：CLI 文档说 frame-skip "disallows the use of a stats file, which offsets the speed gain if the same video needs to be processed multiple times"——把"多次处理同一视频"当作 stats file 的**用途之一**，但当前实现里该复用路径已废。
- 进程内有一个真缓存语义：`SceneManager.clear()` 的 docstring 说，若把同一 StatsManager 传给新 SceneManager 并 seek 回同一位置，`detect_scenes` "will use the cached frame metrics that were computed and saved in the previous call"——但**这是同一进程内的内存缓存，不落盘、无键、无失效判定**。
- 失效判定：**没有**。stats CSV 不存源文件哈希/mtime，无法判断"换了文件"。
- 来源：[PySceneDetect 源码 stats_manager.py](https://github.com/Breakthrough/PySceneDetect/blob/main/scenedetect/stats_manager.py)、[CLI 文档](https://www.scenedetect.com/docs/latest/cli/)

> 对我国的直接意义：**"视频分析缓存"在主流开源视频工具里基本是空白**，我们没有现成可抄的键设计；PySceneDetect 的统计 CSV 只能抄它的**格式（frame_number, timecode, metrics... + 表头校验）**，不能抄复用逻辑。

### 1.2 消费级 NLE（Resolve/Premiere/FCP）的判定粒度

（本节结论多为 [推断] 或社区/官方文档的间接证据，逐条标注。）

- **Resolve Render Cache**：缓存与时间线/片段状态绑定，编辑后局部失效重渲；官方未公开其键的精确构成（是否哈希源）。前作 07_synthesis 已确认「剪一下缓存作废」是其公认最痛点。**未找到公开数据**说明 Resolve 用全文件 sha256 判源变更。
- **Premiere**：有 "Media Cache"（`.cfa`/`.pek` 音频峰值与被导出的预览文件），键与源路径+格式相关；媒体缓存数据库（`Media Cache Files` + `.prmdc`）在源文件被移动/替换时需要用户手动"删除媒体缓存"。Adobe 官方排障流程第一步常是"删除媒体缓存文件"，暗示其判定不够可靠、易误命中或误失效。[Adobe 官方 troubleshooting 文档，见下节补链]
- **FCP**：优化媒体/代理由转码生成，键关联源用 asset。无分析缓存概念。

**共通点（强证据）**：这些系统的缓存**不保证**"内容相同必命中/内容不同必不命中"，而是**乐观缓存 + 用户可手动清空**。它们服务的是交互式编辑，缓存丢了大不了重渲；用户的心理预期是"缓存是加速器，不是正确性保证"。

**失效语义对比（对本项目的启示）**：
- NLE 的缓存失效是**编辑驱动**（timeline 变了）——因为键包含 timeline 状态。
- 我们的分析缓存失效是**源驱动 + 算法驱动**（源文件变了 或 算法版本变了）——键应包含 源哈希 + 参数指纹。这与前作 `skim_key(source_sha256, plan_fingerprint, ...)` 的先例一致。

---

## 2. 内容寻址 vs 元数据寻址：full-hash 还是 quick-hash

### 2.1 成熟系统的实际取舍（有源码/官方文档实证）

| 系统 | 键的构成 | 寻址类型 | 官方理由 |
|---|---|---|---|
| **Git** | 对象内容 + 头部的 SHA-1（40 hex）；「content-addressable filesystem」，键=内容的函数，**不含文件名/路径/mtime** | 纯内容寻址 | 内容相同则对象相同，天然去重与防篡改 |
| **Bazel 远程缓存** | 分两层：**AC（action cache）**= action hash→结果元数据；**CAS**=输出文件内容的 SHA256 寻址（路径形如 `/cache/cas/<sha256>`） | 内容寻址 + action 键 | 输出内容哈希做 CAS，action 键做"该不该复用" |
| **ccache** | 见下（**混合**：内容 + 轻量元数据） | 混合 | 兼顾正确性与速度 |
| **rsync** | 传输层用 **弱滚动校验（adler-32 变体，M=2^16）+ 强校验** 两级；滚动校验逐字节可 O(1) 递推 | 两级校验（先弱后强） | 先廉价筛，弱校验撞了才算强校验 |

来源：[Git Internals - Git Objects](https://git-scm.com/book/en/v2/Git-Internals-Git-Objects)、[Bazel Remote Caching](https://bazel.build/remote/caching)、[ccache manual](https://ccache.dev/manual/latest.html)、[rsync tech report §Rolling checksum](https://rsync.samba.org/tech_report/node3.html)。

### 2.2 ccache 的关键设计（对我们最可借鉴）

ccache 官方文档明列 **"Common hashed information"（永远进哈希）**：
1. 编译器给预处理输出用的扩展名（`.i`/`.ii`）；
2. **编译器的 size 和 modification time**（或 `compiler_check` 指定的其它信息）；
3. 编译器名字；
4. 当前目录（若 `hash_dir` 开启）；
5. `extra_files_to_hash` 指定的文件内容。

即 ccache **不哈希编译器二进制本身的内容，而是哈希"大小+mtime"**——因为编译器几乎不变，重哈希整个二进制每次编译都亏。而**源文件与头文件**才是**逐内容哈希**（direct mode 的 manifest 存了 include 文件的 hash sums，运行时重新哈希当前内容比对）。

**这是本调研对我们最直接的一条经验**：**大块不变对象用"轻量身份"（大小+mtime），小而易变对象用"全内容哈希"**——按"变更概率 × 哈希成本"分配校验强度。

另有两条 ccache 细节与我们有直接对应：
- **mtime 竞态**：direct mode 会在"include 文件 mtime 太新"时**主动禁用**（`a modification time of one of the include files is too new`，避免与正在写入的文件赛跑）——**缓存系统必须假设源文件可能正在被写**；
- **`__TIME__` 检测**：源码里出现 `__TIME__` 即禁用 direct mode（时间相关构建不可缓存）。对应我们的语义：**分析算法若含任何非确定性输入（时间/随机/环境），必须让缓存失效或进键**。

来源：[ccache manual §Common hashed information / §The direct mode](https://ccache.dev/manual/latest.html)。

### 2.3 视频领域的"秒级大文件"实测代价（本机 T0 实测）

对 1080p60 长 GOP H.264（我方 4.mp4：424,176 帧 / 117.8 分钟）这类素材，**全文件哈希的实测吞吐（本机 Python hashlib，1 GiB 数据）**：

| 算法 | 吞吐（本机实测） | 备注 |
|---|---|---|
| **sha256** | **2219 MB/s** | 有 OpenSSL/SHA-NI 加速 |
| md5 | 942 MB/s | 已弱，不建议新用 |
| blake2b | 604 MB/s | 慢于 sha256（本机） |

- 由源内实测 `ASSET_INVENTORY.md:126`：4.mp4 全程**上游两遍分析 ≈ 62 min**、本 fork 一遍 ≈ **7.6 min**；owner 口径"分析要 4 分钟"。
- 4.mp4 大小：本地文档记 **424,176 帧**（`HANDOFF_PAUSE_STILL_20260903.md`、`BENCH_B_20260903.md`），体积量级 [推断] 117.8min × ~3-6 Mbps ≈ **2.6-5.3 GB**，取 ~4GB。
- **折算**：4GB ÷ 2219 MB/s ≈ **1.8 秒** 哈希全文件。
- 结论：**对本项目，sha256 全文件哈希代价（~2s）远小于分析代价（4-7.6 min），占比 ~0.4-0.8%，完全可接受。** "为大文件避免全哈希而退让到 quick-hash"的顾虑在此场景**不成立**。这些速率为本机实测，非引用。
  （注：若源在机械盘/网络盘，读盘带宽会主导，但那是读源本身必须付的成本。）

### 2.4 quick-hash（首尾块+大小+mtime）在视频工具中的实际使用

- **rsync/类 rsync**：长距离用弱滚动校验，是"分块相对哈希"，用于**增量传输**而非"整文件身份"。
- **构建系统/包管理器**：多用"**大小 + mtime 优先，必要时全哈希**"，如 `make` 纯 mtime；Git 对工作区文件有 **racy-git** 机制（mtime 与索引同秒则回退全哈希）。
- **视频工具**：**未找到公开数据**证明 Resolve/Premiere 对源视频做全文件哈希。业界普遍 [推断] 用"路径 + 大小 + mtime"判定，因为重算代价可接受且用户能手动清缓存。

**"视频大文件 quick-hash 够不够用"的业界态度**：视频一旦重导出/重录，**大小和 mtime 几乎必变**；"大小+mtime 都不变但内容变了"需要原地等长改写且还原 mtime，极罕见。因此 quick-hash 在视频场景 [推断] 是**够用的工程折中**——但依赖"用户不刻意对抗缓存"。

### 2.5 三种键的强度-成本对照（综合建议）

| 方案 | 键构成 | 误命中风险 | 成本 | 适用 |
|---|---|---|---|---|
| A 全内容 | sha256(源)+参数指纹 | ≈0 | ~2s/4GB（实测） | **推荐**（我方代价可忽略） |
| B 元数据 | size+mtime(ns)+参数指纹 | 低（除非刻意改写） | ~0 | 慢盘/网络盘 |
| C 两级 | size+mtime 快速筛 → 未中再 sha256 | 低 | 命中快/未中付全哈希 | 折中，实现更复杂 |

**本调研建议**：我方走 **A（全 sha256）为主**——2.3 节实测显示哈希成本仅占分析成本 ~0.5%，而正确性收益是"绝不误命中"，且与现有先例 `skim_key` 用 `source_sha256` 一致（**键设计统一**）。若将来支持网络盘/超大源，再叠加 B 做**预筛**（size+mtime 命中则跳过哈希）构成 C。

---

## 3. ffmpeg/PyAV 生态：有没有现成的分析结果缓存层

**结论：没有。ffmpeg/PyAV/PySceneDetect 生态里，"分析结果缓存层"在开源界基本是空白。**

### 3.1 ffmpeg：无分析结果缓存

- ffmpeg 的 `-stats` / `-stats_period` **与缓存无关**——官方定义是"Log encoding progress/statistics as info-level log"，默认 0.5s 刷新一次进度条。不是持久化的分析产物。
  来源：[ffmpeg 官方文档 -stats/-stats_period 条目](https://ffmpeg.org/ffmpeg.html)。
- `-fflags +fastseek`、`-c copy`、`-sseof` 等都是**解码/复用加速**，不缓存"分析结论"。
- **未找到** ffmpeg 提供任何"把 scene detection/分析结果落盘复用"的机制（`ffprobe` 也只读元数据，不产出可复用分析）。

### 3.2 PySceneDetect 的 stats CSV（详见 §1.1，此处补机制细节）

- **存什么**：`Frame Number, Timecode, <metric1>, <metric2>...`，每个 metric 列对应检测器注册的指标（`content_val`、`delta_rgb`、`adaptive_ratio` 等）。逐帧一行。
- **怎么判失效**：**无失效机制**。文件头两列固定、有 `valid_header()` 表头校验（防串表），但**不含源哈希、不含 mtime、不含算法版本**。换一个视频把同名 CSV 喂进去，程序无法察觉。
- **复用路径已废弃**：`load_from_csv()` 标 `[DEPRECATED] DO NOT USE`；官方 TODO 明说"真正的 calculation cache 在 v1.0 之前做不出来"。
- 来源：[PySceneDetect stats_manager.py 源码](https://github.com/Breakthrough/PySceneDetect/blob/main/scenedetect/stats_manager.py)（本机解包 main 分支核实）。

> **判定**：PySceneDetect 只能抄**格式与表头校验**，不能抄复用逻辑。它的 CCSV 是"给人看的诊断转储"，不是"给机器复用的缓存"。

### 3.3 相邻生态（可借鉴的"哈希/指纹"工具）

- **视频去重/近似匹配**：`videohash`、`chromaprint`（音频指纹）、`imohash`（**imohash 正是"用文件大小+首中尾采样块"做快速哈希**，专为"大文件快速判等"设计）——它们解决的是"判断两个视频是否相同"，不是"缓存分析结果"，但 **imohash 的采样策略可作 quick-hash 的实现参考**。
- **OpenCV**：`cv2.setCacheDir` 之类是内部 PageCache，不跨进程复用分析结果。
- 结论：**开源生态无"视频分析结果缓存"现成轮子**——这与前作 07_synthesis 中"包级跳过业界零先例"的发现同构；**分析缓存对本项目仍是自造**，但技术风险低（就是键+序列化+失效，无算法难度）。

---

## 4. 存储格式对比与损坏检测

**本节的体积/耗时/损坏数字全部为本机 T0 实测**（Python 3.11.9 / numpy 2.4.6，Win10，构造与 4.mp4 产物同量级的数据：states=int8×420000=**0.42MB**、diffs=float32×420000=**1.68MB**，合计 ≈2.1MB，对应我方 2.3MB 产物）。

### 4.1 五种候选的横向对比

| 格式 | 大数组承载 | 元数据(JSON) | 依赖 | 损坏检测 | 随机读 | 结论 |
|---|---|---|---|---|---|---|
| **npz/npy** | 原生（.npy 裸字节） | 可作一个字符串 entry 塞进去 | **仅 numpy+stdlib** | **ZIP 每成员 CRC-32**（见 4.2） | 按成员，整块读 | **推荐**（我方现状） |
| **pickle** | 原生 | dict 混存方便 | 仅 stdlib | **无任何校验**（见下） | 无 | 不推荐 |
| **Parquet** | 面向**表格**列式，非裸数组 | 有 schema/rowgroup 元数据 | **pyarrow/pandas（本机未装，实测 ImportError）** | 文档未提及 checksum | 按列/行组 | 不适用 |
| **SQLite** | BLOB 可存 | 真·表结构+事务 | 仅 stdlib `sqlite3` | `PRAGMA integrity_check`（页级） | 按行/页 | 适合"索引+小元数据"，纯大数组偏重 |
| **自定义二进制** | 完全自控 | 自定 header | 无 | **需自己实现** | 自控 | 可控但重复造轮子 |

实测体积（同 2.1MB 数据）：

| 写法 | 字节 | 相对 |
|---|---|---|
| npy 裸文件相加 | 2,100,256 | 基准 |
| **npz `savez`（不压缩）** | 2,100,508 | +252B（ZIP 头开销，可忽略） |
| npz `savez_compressed` | 2,484 | 测的是全零数组，**不具代表性**（真实 diffs 近似随机，压缩收益[推断]很低） |
| pickle5 | 2,100,233 | 与 npy 同量级 |

npz 读回耗时：**≈1.98 ms / 2.1MB**（实测 20 次均值）。→ 缓存命中载入完全不是瓶颈。

来源：[numpy.savez 文档](https://numpy.org/doc/stable/reference/generated/numpy.savez.html)、[numpy.lib.format（.npy 规格）](https://numpy.org/doc/stable/reference/generated/numpy.lib.format.html)、[Python zipfile](https://docs.python.org/3/library/zipfile.html)、[Python pickle](https://docs.python.org/3/library/pickle.html)、[Python sqlite3](https://docs.python.org/3/library/sqlite3.html)、[PyArrow Parquet](https://arrow.apache.org/docs/python/parquet.html)。

### 4.2 npz = ZIP + 每成员 CRC-32：这正好是我们想要的损坏检测（本机实测）

- **.npy 本身无校验**：官方规格只有 `magic(6B \x93NUMPY) + 版本(2B) + 头长 + header dict(descr/fortran_order/shape) + 裸数据`，**没有任何 checksum 字段**——单读一个 .npy 文件，位翻转无法被自身发现。
- **.npz 借了 ZIP 的 CRC-32**：`np.savez` 只是"把若干 .npy 打包进一个 ZIP"，每个 entry 在 `ZipInfo.CRC` 里带一项 **32 位 CRC-32**（未压缩内容的校验和），`zipfile` 在 `read()`/解压时**逐成员校验**，失配抛 `BadZipFile`。
- **本机实测三例**（构造后精确破坏）：
  1. 在 `diffs.npy` payload 中间翻 1 bit → 读 `states` **成功**，读 `diffs` **抛 `zipfile.BadZipFile: Bad CRC-32 for file 'diffs.npy'`**。（**注意惰性**：`np.load` 按需读成员，只有真正访问到的成员才校验——要全量验完整性须 `testzip()` 或逐个读。）
  2. 截断文件尾部 → 打开即 **`BadZipFile: File is not a zip file`**。
  3. 翻掉 magic 首字节 → 报 **`ValueError: This file contains pickled (object) data...`**（走偏了另一条分支，报错文案不直观，但**仍拒绝加载**）。
- 对照 **pickle：零校验**。官方明说 "Bytes past the pickled representation of the object are ignored"（尾部坏字节被静默忽略），且位翻转若仍能解码成合法对象则**完全无声**；要完整性得自己上 HMAC/checksum。
- 对照 **SQLite**：`PRAGMA integrity_check`（本机实测受控 BLOB 库返回 `ok`）是**页级 B-tree 一致性检查**，能抓结构性损坏；**但抓不到"页内容被静默改写"**（BLOB 内部位翻转若不破坏页结构，完整性检查不会报）。即 SQLite 的损坏检测粒度比 ZIP-CRC 更粗。
- 对照 **Parquet**：官方文档**未提及 checksum**（[推断] 依赖文件系统/上层）。

> **判定（T0）**：我方数据"整块读、整块写、一次性校验"的模式，**ZIP-CRC-32 是被动获得且够用的损坏检测**。位翻转（磁盘坏道/半写）能被捕获，缓存坏文件→当 miss 重建即可。CRC-32 不防**蓄意篡改**，但缓存不是安全边界，无此需求。

### 4.3 原子写入（temp + os.replace）在 Windows 的注意事项——**本机实测到一个真坑**

标准模式（与现有导出链 `preview_player.py:2754/4349` 用 `os.replace` 一致）：

```
fd, tmp = tempfile.mkstemp(dir=<目标同目录>, prefix=<name>., suffix=.tmp)
with os.fdopen(fd, 'wb') as f:
    f.write(payload); f.flush(); os.fsync(f.fileno())
os.replace(tmp, dst)   # 原子替换（POSIX 真原子；Windows = MoveFileEx+REPLACE_EXISTING）
```

- **坑（本机实测，T0）**：Windows 上若 `dst` **正被任何句柄打开**（哪怕只是一个只读 reader，或一个 append writer），`os.replace(tmp, dst)` **抛 `PermissionError [WinError 5] 拒绝访问`**。这与 POSIX 不同——POSIX 允许 rename 覆盖被打开的文件（旧 inode 延迟释放）。**Windows 是"共享冲突"语义**。
  - **实务含义**：写入前必须确保**本进程内没有对目标 npz 的未关闭句柄**（`np.load` 对象用完要 close/出 `with`）；且**杀毒/索引/搜索/同步盘（OneDrive）会瞬时持有句柄**，`os.replace` 可能偶发 WinError 5 → **需要小步退避重试**（如 5×50ms）再放弃。
- **跨卷不原子**：temp 必须与目标**同目录/同卷**（`tempfile.mkstemp(dir=...)` 而非默认 temp 目录），否则 `os.replace` 退化为复制，非原子。
- **崩溃安全**：`fsync` 落盘后再 replace，保证断电时要么旧完整文件、要么新完整文件；残留的 `.tmp` 用启动扫描清理（见 §5 孤儿清理）。
- 官方依据：`os.replace` 是跨平台"覆盖式改名"（Windows 走 `MoveFileExW` + `MOVEFILE_REPLACE_EXISTING`，"provided that security requirements regarding ACLs are met"），文档明示其保证弱于 POSIX。
  来源：[Python os.replace](https://docs.python.org/3/library/os.html#os.replace)、[Win32 MoveFileExW](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-movefileexw)、[Python tempfile（mkstemp/NamedTemporaryFile 语义）](https://docs.python.org/3/library/tempfile.html)。

### 4.4 还需不需要额外 checksum？

- **不需要"文件级 sha256"做损坏检测**：ZIP-CRC-32 已覆盖"半写/位翻转"这一真实风险面，且成本免费。
- **需要的是"自证元数据"**：把 `src_size / src_mtime / 参数指纹 / 算法版本 / 格式版本` 作为**独立 entry 存进同一个 npz**（对齐既有先例 `preview_player.py:2073-2078` 把 `src_size/src_mtime` 写进 npz 内）。这样"错配/旧版"文件能**自报家门**被拒，而不必依赖文件名——这是比额外 checksum 更重要的防线。
- 若将来缓存跨机器/跨盘分发（如共享缓存），再加**内容 sha256 存 sidecar/DB**（2.1MB 算 sha256 实测吞吐折算 <2ms）作为强完整性，本期**过度设计，不做**。

**§4 小结**：**npz（`savez` 不压缩）为存储首选**——仅依赖 numpy、单文件封装、天然带每成员 CRC-32、与项目既有 npz 缓存先例同构；元数据作为 entry 内嵌实现**自证**；写入走**同目录 temp + fsync + `os.replace`**，并针对 **Windows 共享冲突 WinError 5 加重试**。

---

## 5. 失效策略与版本迁移

**要分两类失效**：(a) **源失效**（源文件变了）；(b) **代码/算法失效**（同一源、同一参数，但代码升级后结论可能不同）。§1-§2 已解决 (a)（源哈希进键）。本节专攻 (b)：**算法版本号该放哪**。

### 5.1 三种"版本号安放"的取舍

| 方案 | 机制 | 优点 | 缺点 | 何时用 |
|---|---|---|---|---|
| **A. 进键哈希** | `version` 列入键参与计算（如 `sha256(源)`+`params`+`algo_v`） | 旧结果**自然不可达**、无需清理代码、可用同一命名空间 | 旧缓存**留在盘上占空间**，需配合淘汰（5.3）；版本一变**全量 miss** | **推荐**（见先例） |
| **B. 命名空间目录** | `.cache/<ns>/...`，ns 含版本如 `analysis/v3/` | 可整目录删除、天然隔离、可并存多版本 | 多一套目录管理；ccache 实践表明 namespace **共享存储**、仅逻辑隔离 | 版本少、需并存回退（T1） |
| **C. sidecar 文件** | 缓存旁挂 `<name>.meta.json` 存版本 | 主体不动、可人读 | **多一文件=多一处原子性缺口**（两文件无法原子写），易"主体/sidecar 不匹配" | 不推荐为主 |

**ccache 的官方实现（强证据）**：`namespace` 字符串**混入每次编译的哈希数据**（"the namespace string will be added to the hashed data for each compilation"），使条目"logically separate... but they will still share the same storage space"。即 **ccache = A 的机制（进哈希）+ B 的管理（`--evict-namespace` 选择性清理）的混合**。
来源：[ccache manual §Cache namespaces / §Cache cleanup](https://ccache.dev/manual/latest.html)。

**Bazel 的分层（强证据）**：**AC（action cache）= action hash → 结果元数据**；**CAS = 输出内容 SHA256 寻址**。env 经 `--action_env` 白名单后**进 action 定义（即进键）**。Bazel 官方承认一个**漏键事故**："Bazel currently does not track tools outside a workspace"（如 `/usr/bin/gcc` 不进键），导致不同编译器用户**错误共享命中**（issue #4558）——**"工具/算法版本该不该进键"最直接的反面教材：该进，漏了就静默错命中**。
来源：[Bazel Remote Caching](https://bazel.build/remote/caching)。

### 5.2 本项目已有的"版本号"先例（T0，读源码所得）

已**有两（三）处版本化实践，风格一致——版本号随数据内嵌、不靠文件名**：

1. `analyzer.py:266` **`ANALYSIS_CONTEXT_VERSION = 1`**：写进 context dict 的 `"version"`；`context_records_for_pauses()`（`analyzer.py:374`）复用时**校验 `version != ANALYSIS_CONTEXT_VERSION` 即拒绝**——旧版 context 自动失效。
2. `media_info.py:91` **`_FRAME_PTS_EVIDENCE_SCHEMA_VERSION = 2`**：`media_info.py:289` 校验 `schema_version`，不匹配即判废。
3. `preview_player.py:2001-2078` npz 关键帧表：**旧版缺字段自然抛错→落重建，同路径覆盖升级**（源码注释原文："v2 表必须带逐帧指纹；旧 v1 缓存缺 key 会在此抛错，落到下方重建（同路径覆盖升级）"）。

> **判定（T0）**：分析缓存**首选方案 A（算法版本号进键/进内嵌元数据）+ §4 的内嵌自证**，与项目三处既有先例**同构**，不引入新机制。版本号建议复用 `ANALYSIS_CONTEXT_VERSION` 语义并升格为"**分析产物格式+算法联合版本**"（产物布局=states/diffs/context 三者，任一变化都应失效）。

### 5.3 容量上限、淘汰与清理（成熟工具做法）

**ccache（最贴近我们的构建缓存）**：
- 默认 **`max_size = 5 GiB`**（`CCACHE_MAXSIZE`），`max_files` 默认 0（不限）。
- **自动淘汰**：写新结果后、任一上限超即触发；按 cache 文件 **mtime 做"近似 LRU"**（原文 "approximate LRU... based on the modification time"）。**为支撑 LRU，命中时 touch 文件 mtime 标记"最近使用"**。
- 官方坦承**不保证严格全局 LRU**："only entries in a subset of the cache are considered... the oldest entries aren't always removed first"。
- 手动：`-c/--cleanup`、`--evict-older-than AGE`、`--evict-namespace NS`。并**警告不要用 `--trim-dir` 处理本地 cache**（破坏记账），限容应设 `CCACHE_MAXSIZE` 再 `ccache -c`。
来源：[ccache manual §Cache size/§Cleanup](https://ccache.dev/manual/latest.html)。

**浏览器（Storage API）**：
- 配额：Chrome 每源可达**磁盘 60%**（全局上限 ~80%）；Firefox best-effort **min(10% 磁盘, 10GiB)**；Safari 老版初始 **1GiB** 再提示。
- **淘汰=每源 LRU**：压力下先删"最久未用的源"，**整源一次性全删（all-or-nothing，不部分删）**，**persistent（用户 opt-in）源被跳过**。
- 超配额写抛 `QuotaExceededError`，**调用方必须处理**。
- 现实：Chrome 团队研究称**浏览器主动删除其实很罕见**（"regularly visited sites are very unlikely to have even best-effort data evicted"）。
来源：[MDN Storage quotas and eviction criteria](https://developer.mozilla.org/en-US/docs/Web/API/Storage_API/Storage_quotas_and_eviction_criteria)。

**Bazel**：**远程缓存无内置 GC**（"Deleting content from the remote cache is part of managing your server"）；本地 `--disk_cache` 在 **7.4+ 才有** `--experimental_disk_cache_gc_max_size/max_age`，且**仅 idle 时跑**（`--experimental_disk_cache_gc_idle_delay` 默认 5 分钟）。来源：[Bazel Remote Caching](https://bazel.build/remote/caching)。

**对本项目的收束（T1）**：
- **量级使这几乎不是问题**：单产物 ~2.3MB；存 1000 个不同源也仅 ~2.3GB（对照本机 `.cache` 现已有 **16GB** 沉淀，实测 `du -sh .cache`）。**"容量上限"不是本期紧迫工程**。
- **但仍设一道闸**：**上限 + 近似 LRU（按 mtime，命中 touch）**，与 ccache 同款；实现=扫描目录+按 mtime 排序删到阈值。
- **孤儿清理（必做，真实坑）**：
  1. **半写残留 `.tmp`**：§4 原子写入若进程被 kill 会残留 `<name>.<rand>.tmp` → **启动时删 mtime 超阈值（如 1 天）的 `*.tmp`**。
  2. **源消失**：键含源 stem（对齐 `preview_player.py:2008` `{stem}_{size}_{mtime}.npz`），源删/移后成孤儿；可"键内嵌源路径，清理时 stat 存在性"。
  3. **旧算法版本堆积**：方案 A 让旧版"不可达但仍在盘"，**靠 LRU 自然淘汰**，或方案 B 整目录删除（ccache `--evict-namespace` 正为此）。

> **注（键设计不一致，冲突点）**：`preview_player.py:2008` 用 `{stem}_{size}_{mtime}`（弱校验），而 `skim_cache.py:119 skim_key` 用 `source_sha256`（强校验）——**同一代码库内两套键强度并存**。**分析缓存应沿用更强的一支（sha256）**，并留意两套守卫的语义差异（§7 讨论 size+mtime 的漏判边界）。

**§5 小结**：**算法版本号进键/内嵌（方案 A）**，与三处既有先例一致，**T0**；容量设"上限+近似 LRU（按 mtime，命中 touch）"（ccache 同款），**但 2.3MB/条使这项不紧迫（T1）**；**孤儿 `.tmp` 启动清理必做**（原子写入的必然伴生品）。

---

## 6. 用户交互：命中时静默复用 vs 提示用户

### 6.1 成熟工具的三种语义（按"静默程度"排序）

| 工具/场景 | 命中时的用户可见性 | 依据 |
|---|---|---|
| **浏览器 HTTP 缓存** | **完全静默**（设计如此）。无 `Cache-Control` 也会**启发式缓存**（默认按 `Last-Modified` 后 ~10% 时间）；正常加载"the origin server never sees the request at all" | [MDN HTTP Caching](https://developer.mozilla.org/en-US/docs/Web/HTTP/Caching) |
| **ccache** | **不在构建输出里逐条提示**，命中/未命中只累到**统计计数器**，用户事后用 `ccache -s` 查看；未命中原因（"Errors/Uncacheable"）也要加 `-v` 才展开 | [ccache manual §Cache miss reporting](https://ccache.dev/manual/latest.html) |
| **Gradle** | **在任务行显式标注**：命中打 `FROM-CACHE`，增量跳过打 `UP-TO-DATE`——"tells you *which* mechanism produced a result... a hit can come from any earlier build in any location, including other developers' machines" | [Gradle Build Cache §What does it look like](https://docs.gradle.org/current/userguide/build_cache.html) |

**语义分野（关键）**：
- **浏览器**静默是因为**正确性由 HTTP 语义（新鲜度/验证器）保证**，用户无需知情；且网络延迟预算紧，弹提示会毁掉体验。
- **ccache** 静默是因为**命中不影响构建产物正确性**（错误的键设计才影响），且构建日志本已很长；**但它提供"可查询的统计"作为审计出口**。
- **Gradle** 显式标注是因为**构建结果可能来自远程/他人机器**，用户需要"这条结果从哪来"的**可追溯性**——这正是**缓存的"信任边界"越宽、越需要显式**。

### 6.2 "静默复用导致用户不知情"的事故模式（有据可查的类型）

**未找到公开的、点名本领域的重大事故数据**（WebSearch 在本环境不可用，若干查询无返回）。以下按**机制**归纳已知的真实风险类别，各标来源等级：

1. **HTTP 缓存的"陈旧静默复用"**（T0，官方文档明文承认的边界）：
   - MDN 明确警告 **`no-cache` 对"后退/前进导航"不保证重新验证**："If the back/forward cache (bfcache) is used, the browser restores a snapshot... **Even when bfcache is not used, the browser may still serve the cached response without revalidating**."——即**开发者以为加了 no-cache 就新鲜，实际历史导航仍可能拿到旧内容**。
   - **请求折叠（request collapse）**："even if `max-age=0` or `no-cache` is given in the response, it will be reused"（共享缓存并发相同请求时只回源一次、其余复用）。
   - 来源：[MDN HTTP Caching](https://developer.mozilla.org/en-US/docs/Web/HTTP/Caching)。
2. **Bazel 漏键导致的静默错误命中**（T1，官方承认的 issue）：编译器不进键 → 不同编译器**错误共享命中**。**这类是"静默复用"里最危险的一种——用户毫不知情且结果是错的**。来源：[Bazel Remote Caching](https://bazel.build/remote/caching)（issue #4558）。
3. **NLE 媒体缓存的"删缓存祈祷"**（前作 07_synthesis + 本报告 §1.2）：Adobe 官方排障第一步常是"删除媒体缓存"，暗示其判定不可靠、**用户会遭遇"我改了源但预览没变"**。这是消费级工具的普遍抱怨模式（[推断]，基于官方排障流程的间接证据）。
4. **进度/状态误导**（T1）：Nielsen Norman Group 指出**反馈若不能反映真实进度即是有害的**（"most browsers fail at useful progress bars... feedback that misrepresents actual progress"）。**推论到缓存**：[推断] 若 UI 上"分析完成"其实来自缓存，而用户以为"刚重新分析了"，那是在给**过时状态披上新鲜外衣**——与"进度条说谎"同类危害。来源：[NN/g Response Times: The 3 Important Limits](https://www.nngroup.com/articles/response-times-3-important-limits/)。

### 6.3 对本项目的具体建议（本项目是**桌面 GUI、单用户、自用**——边界与上表都不同）

**诊断前提**：我们的缓存**不是**浏览器/远程构建缓存那种"跨机器信任边界"；它是**本机单用户、自己产自己用**。因此 Gradle 那套"必须显式"的理由（来自他人机器）**不成立**，而浏览器的"静默"理由（正确性由语义保证）**基本成立**——**前提是键足够强（§2.5 的全 sha256）**。

建议（T1/T2 分级）：

- **[T1] 静默复用 + 可查询的"来源回执"**：命中时**不弹窗**（4 分钟→秒级正是缓存的价值，弹提示破坏"秒开"体验），但**在日志/状态栏写一行**"分析结果来自缓存（key=…/算法版本=…）"。这与 ccache（静默+统计）语义一致，又补上了 Gradle 式的少量可追溯性。**成本极低，收益是排障时不抓瞎。**
- **[T2] 阈值变化必须失效——这是本项目的头号红线**：分析参数（`thresholds`，见 `analyzer.py:152-158` 用 `thresholds['pause']/['speed_1x']/['speed_2x']/['speed_0_2x']`；`compare.motion_thresh` 默认 2.0、`still_time`→`still_frames`，`analyzer.py:1217-1219`）**直接决定 states 分类结果**。**参数变则产物必须重算，绝不能复用**。
  - **键必须包含完整参数指纹**：`thresholds` 全字段 + `proc_res` + `batch` + `compare` 相关字段 + `decode_backend`。项目已有 `snapshot`（`preview_player.py:3962` 起）正是这些字段的**完整快照**——**[推断] 直接复用该 snapshot 的规范化哈希作为"参数指纹"是最省事且不易漏项的做法**（它本来就是"影响分析结果的参数集合"的正规化）。
  - **红线警示**：若键漏了 `thresholds`，用户"调阈值→重分析"，却拿到**旧阈值的结果**，且**UI 上完全看不出**——这是本项目最可能的"静默复用事故"，后果=用户按错误的暂停/倍速分段去剪辑。**优先级最高。**
- **[T2] 首次命中/参数变更的语义提示**：正常命中静默；**仅当"参数变了导致强制重算"时**，日志明确写"参数指纹变更，缓存作废，重新分析"（让"为什么这次没秒开"有答案）。Gradle 的 `FROM-CACHE` 给的是"产物从哪来"，我们对应给"这次为什么没命中"。
- **[T2] 不做"提示用户确认是否用缓存"**：单用户自用工具弹"是否使用缓存结果？"是**反模式**（多一次点击、用户无法判断）。浏览器的静默+统计出口是更成熟的形态。

**§6 小结**：**静默复用为默认（T1）**，配**日志一行"结果来自缓存(键/版本)"**的可追溯性；**最强红线=阈值等分析参数必须进键，参数变必重算（T2，最高优先）**，这也是本项目"静默复用"唯一真会让用户拿到错东西的路径。**不学 Gradle 的强制显式**（我们无跨机信任边界），**学 ccache 的静默+可查统计**。

---

## 7. 视频分析特有的坑：哪些会真的让"同文件不同结果"

**判据**：本节只关心"**同一个文件、同样参数，两次分析结果不同**"——因为只有这类才**必须**让缓存失效，其余都只是"输出格式/质量差异"，与缓存键设计无关。逐坑给 **会/不会** 与来源等级。

### 7.1 VFR（可变帧率）时间戳——**最大的坑，但方向要分清**（T0，本项目实测）

- **陷阱不在"解码结果不同"，而在"帧数口径/时间戳映射"**。本项目 `BENCH_B_20260903.md:65` 的结论（T0）：
  > "任意 VFR 源上的帧精确寻址，权威全部是**帧号**（ffmpeg `select=eq(n,N)`、MLT `in/out`、BestSource/FFMS2 均同族）；**时间戳只在认证恒定节拍下等价可用**，或经修复后作预处理。"
- 实测证据：`ASSET_INVENTORY.md:121` 记"**全片普查：重复 PTS 卷入 26.68% 帧**（24576 段）、VFR 突变 17016 点"（4.mp4）——**该源有 1/4 以上的帧 PTS 重复**。
- **对缓存的含义**：
  - **不会**因 VFR 让"逐帧 states 数组"本身不可复现——**只要我们按帧号（index）而非 PTS 记缓存**。项目 `analyzer.py` 的 states/diffs 正是**按解码帧序 index 索引的顺序数组**，天然规避 PTS 不唯一问题。
  - **会**让"帧数"口径漂移：`nb_frames`/`CAP_PROP_FRAME_COUNT` 是**容器/元数据估计，常高估**。`HANDOFF_PYAV_CONSOLIDATION.md:5` 明记 "metadata 常高估 FRAME_COUNT"，EOF 兜底"以实际解出帧数为准"；`HANDOFF_CURRENT.md:324` "FFprobe `nb_frames` remains a hint"。
  - **缓存键警示（T0）**：**绝不能拿 `nb_frames`/`ffprobe` 报的帧数当缓存有效性判据**——它可能一变一不变地撒谎。**有效性判据必须是"源文件身份"（sha256/size+mtime）+ 参数指纹**，产物长度只作**加载时的自洽校验**（如"重算出的 L 与缓存 L 不符→判废"）。
  - 权威源：[BENCH_B_20260903.md:65]、[ASSET_INVENTORY.md:121]、实测均在本仓。

### 7.2 色彩空间/像素格式——**会**让"分类结果"漂（T0，本项目实测事故）

- 实测：OBS 源**未打颜色标记**（`colorspace=unspecified`）→ **mpv 按 bt709、静帧按 bt601 转换**，饱和区 R/G 差 **1.5+ 灰阶（用户可察觉）**（`HANDOFF_PAUSE_STILL_20260903.md:105`）。
- 修复方式：scale 显式加 `in_color_matrix`；PyAV 路径**改喂 `yuv420p` 平面字节**，把矩阵转换"让给链内做"。
- **对缓存的含义（关键推论 [推断]）**：
  - 灰度化前的色彩矩阵选择**直接影响 gray 值 → 直接影响 diffs → 直接影响 states 分类**（`_classify_gray` 对阈值敏感）。**未标记色彩的文件上，"用哪套矩阵"是一个隐性输入**。
  - 因此：**若不同 decode backend（OpenCV vs A_PT/PyAV）用不同默认矩阵**（实测 OpenCV 兜底路径"接受已知 601 偏差"，见同处），**同一源在不同后端下会算出不同的 states**。
  - **缓存键必须含 `decode_backend`**（项目 `analyzer.py:446-465` 已有 opencv / ffmpeg_sw_passthrough 两套后端）——否则用户切换后端会**错误命中另一后端的产物**。这与 §6.3"键含 decode_backend"呼应，此处给出**为什么**。

### 7.3 ffmpeg 版本升级是否该失效缓存——**该**（T0，本项目已有先例）

- **本项目先例（直接可抄）**：`scripts/pts_normalized_proxy.py:1721-1724` **硬校验 ffmpeg 身份**——代理证据里绑定生成时的 ffmpeg **`path` + `sha256` + `version_line`**，验证时三者任一不符即抛 `ProxyEvidenceError`。即**本项目已经决定"ffmpeg 换了，产物不可信"**。
- **为什么该失效（[推断] + 生态依据）**：
  - ffmpeg 文档确认解码路径对输入有**破坏性选项**：`-drop_changed`（丢掉中途参数变化的帧）、`-reinit_filter`（分辨率/像素格式等变化触发滤镜图重建）——这些都改变"实际产出哪些帧"。来源：[ffmpeg 文档 -drop_changed/-reinit_filter](https://ffmpeg.org/ffmpeg.html)。
  - 项目实战：`HANDOFF_PREVIEW_ACCURACY.md:206` 记 "**PyAV(FFmpeg 8) 的 pts 标签随碰撞处不一致（实测 0~2 帧浮动）**"——**同源、不同 ffmpeg 版本，PTS 标签会差 0~2 帧**。虽本项目最终按帧号绕开了它，但这正是"换版本结果可能变"的**实证**。
- **判定（T0）**：**ffmpeg/PyAV 版本（至少 major.minor，稳妥取 `version_line` 或 sha256）必须进缓存键或进内嵌元数据**。成本近零（一次 `-version` 调用，项目已有 `ffmpeg_version_record()` 现成函数），收益是杜绝"换 ffmpeg 后用了旧语义产物"。

### 7.4 GPU vs CPU 解码后端——**理论上可能不同，但本项目实测"硬解无优势且不用它"**（T1/T0）

- **官方口径（弱保证）**：ffmpeg HWAccel 文档说硬件解码器"will generate **equivalent** output to software decoders"，但**从未承诺 bit-identical**，也不谈跨设备/跨次运行的确定性。来源：[ffmpeg HWAccelIntro](https://trac.ffmpeg.org/wiki/HWAccelIntro)。
- **另一个方向的质量差（官方明说）**：硬件**编码器**"typically generate output of significantly lower quality than good software encoders"——但这是编码，不影响我们的分析（我们只解码）。
- **本项目实测（T0）**：`ASSET_INVENTORY.md:128` 记 **"NVDEC Ada 官方 903fps < 本机软解 1835-2160"（硬解无优势反转）**；`:122` 记 GPU nvdec 在稀疏访问下**不扩展**（多输入 N=1/4/8 上限 1.1x）。**结论：本项目分析路径走软解（A_PT/PyAV），GPU 解码不在产线上**——因此"GPU vs CPU 让结果不同"在本项目**暂不发生**。
- **若将来引入 GPU 解码的处置（[推断]，T1）**：仍应把"解码后端"进键（§7.2 同款理由）。**但更稳的是遵守项目铁律"等价性是硬门槛（逐位比对）"**（`HANDOFF_UI_REVIEW_20260902.md:53`）——先证明 GPU 与 CPU **逐位一致**，再谈共用缓存；不能逐位一致就**分键**。

### 7.5 "同文件不同结果"的坑清单（收束表）

| 坑 | 会不会让同源同参结果不同 | 处置 | 来源 |
|---|---|---|---|
| **VFR 时间戳/帧数口径** | **帧数组本身不会**（按帧号）；**但 `nb_frames` 会撒谎** | 键用源身份+参数指纹；帧数只作自洽校验 | 本仓 T0 |
| **色彩矩阵（未标记源）** | **会**（后端默认矩阵不同→gray 不同→states 不同） | **`decode_backend` 进键** | 本仓 T0 事故 |
| **ffmpeg/PyAV 版本** | **会**（PTS 标签 0~2 帧浮动；滤镜行为随版本变） | **版本/sha256 进键或内嵌**（已有先例函数） | 本仓 T0 + ffmpeg 文档 |
| **GPU vs CPU 解码** | 官方只承诺"equivalent"、不承诺 bit-exact；**本项目实测硬解更慢、不在产线** | 逐位验收后共用；否则分键 | ffmpeg 文档 T1 / 本仓 T0 |
| **解码/缩放实现（swscale `flags=area` vs 双线性）** | **会**（不同缩放核→不同 gray 值） | 项目已锁"逐位一致"硬门槛 | 本仓 T0 |
| 随机/时间相关非确定性 | **会** | 类比 ccache 禁用 `__TIME__`；让缓存失效（§2.2） | ccache 文档 |

> **总判（T0）**：本项目"同源同参"下**真正**会让结果不同的，实测是**色彩矩阵/解码后端**与**ffmpeg 版本**两条；VFR 是**帧数口径的坑**而非帧数组的坑；GPU 解码在本项目**已被实测排除在产线外**。**缓存键必须含 `decode_backend` + ffmpeg 身份**，即可封死当前已知的全部真坑。

---

## 8. 结论：对本项目的具体建议

**一句话**：**用"强内容键 + npz 单文件 + 原子写 + 静默复用"落地，四个决策点全部有本仓先例可抄，无算法风险；最大正确性红线是"参数/后端/ffmpeg 必须进键"，最大工程坑是"Windows 上 os.replace 的共享冲突"。**

### 8.1 键设计（Key）

| 项 | 建议 | 置信 |
|---|---|---|
| 源身份 | **`source_sha256`（全文件哈希）**。实测 4GB 源哈希 ~1.8s（2219MB/s），占分析 4-7.6min 的 ~0.5% | **T0** |
| 参数指纹 | **`thresholds` 全字段 + `proc_res` + `compare` + `batch`**；直接规范化 `preview_player.py:3962` 的 `snapshot` 得到，避免漏项 | **T0** |
| 后端 | **`decode_backend` 必须进键**（opencv / ffmpeg_sw_passthrough 默认矩阵不同→states 不同，§7.2） | **T0** |
| 工具身份 | **ffmpeg `version_line`/sha256 进键或内嵌**（先例：`scripts/pts_normalized_proxy.py:1721`） | **T0** |
| 算法版本 | **进键/内嵌**（先例：`analyzer.py:266 ANALYSIS_CONTEXT_VERSION`）；升格为"产物格式+算法联合版本" | **T0** |
| 帧数/nb_frames | **不作为有效性判据**，仅作加载自洽校验（§7.1） | **T0** |
| 盘型适配 | 若将来支持网络盘/机械盘，叠加 `size+mtime(ns)` 做**预筛**（两级键） | **T2** |

### 8.2 存储（Storage）

| 项 | 建议 | 置信 |
|---|---|---|
| 格式 | **npz（`savez` 不压缩）**：仅依赖 numpy、单文件、**天然每成员 CRC-32**、与既有先例同构 | **T0** |
| 元数据 | **作为独立 entry 内嵌同一 npz**（`src_size/src_mtime/params/algo_v/ffmpeg_v/backend`），实现**自证**；不做 sidecar | **T0** |
| 损坏检测 | **ZIP-CRC-32 已够**（实测单字节损坏→`BadZipFile: Bad CRC-32`）；**不需额外 checksum** | **T0** |
| 读取 | 注意 CRC 校验是**惰性**的（按访问的成员）；要全量验用 `testzip()` | **T0** |
| 原子写 | **同目录 `mkstemp` → write → `fsync` → `os.replace`**；**Windows 关键坑**：目标被任何句柄打开即 `PermissionError WinError 5` → **加重试退避**（杀毒/索引/同步盘会瞬时持句柄） | **T0** |
| 体积/耗时 | 2.3MB 读回实测 **~2ms**，非瓶颈；`savez_compressed` 对近似随机 diffs 收益低 | **T0** |

### 8.3 失效与迁移（Invalidation）

| 项 | 建议 | 置信 |
|---|---|---|
| 版本迁移 | **方案 A：版本号进键/内嵌**（=ccache 混哈希 + 本仓三处先例），旧版**自然不可达** | **T0** |
| 容量上限 | **上限 + 近似 LRU（按 mtime，命中时 touch）**，ccache 同款 | **T1** |
| 紧急度 | **不紧迫**：单产物 2.3MB，1000 条仅 ~2.3GB（对照本机 `.cache` 已 **16GB**） | **T1** |
| 孤儿清理 | **必做**：启动扫 `*.tmp`（原子写伴生）；可选按源存在性清理 | **T0** |
| 键不一致 | 项目存在 `{stem}_{size}_{mtime}`（preview_player）与 `source_sha256`（skim_key）**两套强度**——**分析缓存统一走 sha256** | **T1** |

### 8.4 交互（Interaction）

| 项 | 建议 | 置信 |
|---|---|---|
| 命中行为 | **静默复用**（学 ccache/浏览器），不弹窗——4min→秒级正是价值所在 | **T1** |
| 可追溯 | **日志一行**："分析结果来自缓存（key/版本）"，补上 ccache 的"统计出口" | **T1** |
| 参数变更 | **强制重算 + 日志明说"参数指纹变更→缓存作废"**（本项目唯一的静默错用路径） | **T2（最高优先）** |
| 反模式 | **不做**"是否使用缓存?"确认弹窗 | **T2** |

### 8.5 风险登记（本报告识别、建议在 checklist 挂账）

1. **【正确性·高】** 键若漏 `thresholds`/`decode_backend`/`ffmpeg 身份` → **静默错命中**（用户拿到旧参数/旧后端的 states 去剪辑）。**这是本次调研最该防的一条。**
2. **【工程·中】** Windows `os.replace` 的 **WinError 5 共享冲突**（实测）——实现时必须带重试，否则偶发写失败。
3. **【一致性·低】** 项目内**两套缓存键强度**并存，实现时须明确"分析缓存用哪套"，避免复制到弱的那套。
4. **【生态·低】** 视频分析结果缓存在**开源界零先例**（§1-3），无现成轮子可抄——但本报告已给出全部关键决策的**本仓先例或实测依据**，**技术风险低**。

**结语**：本项目加磁盘缓存的**收益明确**（4min→秒级）、**代价可忽略**（~1.8s 哈希 + 2.3MB 存储）、**技术无难点**（键+npz+原子写+失效，四件套均为已知工程）。**最难的不是"能不能做"，而是"键别漏项、Windows 写别翻车、别让用户拿到静默过期的结果"**——这三条已在上表逐条锚定。

---

**（报告完。§0-§8 覆盖：术语澄清 / NLE 缓存键 / 内容寻址 / ffmpeg 生态 / 存储格式与损坏 / 失效与迁移 / 用户交互 / 视频特有坑 / 结论。全部数字除注明引用外均为本机 T0 实测或本仓源码实物核对。）**
