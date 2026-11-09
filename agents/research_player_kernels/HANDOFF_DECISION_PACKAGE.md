# 预览流畅方案决策交接包

> **给接手 agent 的说明**：这是一个自包含的决策简报。你不需要读 180KB 的
> HANDOFF 历史。本文档包含：已证事实（带证据路径）、候选方案、已排除路线
> （带判死理由）、复现方法、工作区状态、以及需要你做的决策。
>
> 生成时间：2026-10-13。决策人：owner（用户）。执行者：上一轮 agent。
> **纪律要求（项目铁律，见 ASSET §七）**：
> - R6 结论分级：T0=位级实测/可复现，T1=多源验证，T2=单源推断，T3=猜测。
>   生产代码只准嵌入 T0/T1。
> - R8 探针同源：验证必须驱动生产真实入口，禁自建近似复刻。
> - 不 commit/push 除非 owner 明说「验收/提交/推送」。

---

## 1. 背景（30 秒版）

项目是《明日方舟》录播自动剪辑工具（Python + Tk + libmpv + Rust 解码服务）。
核心痛点：**预览要流畅，且预览节奏必须等于导出节奏**。owner 的工作流是
"2 小时录播 → 自动删暂停段 → 慢动作段加速"。

关键几何：剪辑后保留段极碎（中位 **11 帧/0.18 秒** 一个段）。owner 要
**UI 4x 播放**，其中 0.2x 慢动作段要再 ×10 = **40x 源帧速 = 2400 源帧/秒**。

**结论：这个问题已经解决了**（P1 物化代理，实测 40.0x 零停顿）。当前
待决策的是**优化档位**（磁盘 vs 画质），不是"能不能做到"。

---

## 2. 已证事实（T0，可直接引用）

### 2.1 物理墙（三条独立路线全部判死）

| 路线 | 实测上限 | 证据 |
|---|---|---|
| 直解源文件 | 2029fps 隔离 / 1380fps GUI 实载（40x 需 2400）| ASSET §六 |
| 显卡硬解 | NVDEC 976fps（**比软解慢**）；MDK 实测 660-951fps 也全劣于其自家软解 | ASSET §六 + `agents/research_player_kernels/01_mdk_bench.md` |
| 实时并行预取 | **4.8x 上限**（离 40x 差一个数量级）| `probe_parallel_refill` |
| 运行时跳解码（trick-play）| 碎段下恶化（停顿 85 次、EOF 空转）| `probe_biz_edl_skipframe` |
| 任何实时读源方案 | 封顶 **5-6x**（seek 率墙）| ASSET §六 |

### 2.2 物化代理已落地并验证（T0）

| 项 | 实测 | 证据 |
|---|---|---|
| 4.mp4 全片构建 | 233-265s / 1205MB（1080p cq22）| `probe_p1_prod_build.py` |
| 播放 4x | **40.0x 零停顿**（0.2x 段源帧 2400fps 达标）| `probe_proxy_gui_e2e.py` |
| 编辑后重建 | **0.15-10s**（批级增量，只重编受影响窗）| `probe_proxy_edit_rebuild.py` |
| 编辑期间播放 | 立即回落直通档**不卡**（20x）| 同上 |
| seek 精度 | **0 帧误差**（暂停态）| `probe_proxy_gui_e2e.py` |
| 跨源（2.mp4 重复 PTS+VFR）| 全链 PASS，40.0x 达标，seek 0 帧 | `probe_proxy_cross_source.py` + `02_proxy_cross_source.md` |
| 清晰度 | render 尺度 SSIM **0.998**（1080p cq22）| `probe_p1_clarity_results.json` |

### 2.3 行业对照（T1，调研结论）

- 四家成熟 NLE（Resolve/Premiere/FCP/FilmCraft）的预览流畅方案**全部是物化**
  （代理/渲染缓存），无一例外。
- 每小时代理占盘：行业 **40-187GB**；FilmCraft 4.25GB；**我们 cq22 约 12.9GB，
  cq24 约 10.4GB，cq26 约 8.5GB**（我们做的是"只物化保留段"，省 95%）。
- 「业务速度烘入代理」= 我们独有，业界无先例（三度确认）。

### 2.4 内核替换已三度否决（T0）

| 内核 | 实测结论 | 证据 |
|---|---|---|
| MDK | 顺序解码 1903fps（≈我们 2029）；seek 45-62ms（同量级）；"连续 seek 优化"实为 seek 合并（500 连发只兑现 2 个）→ **语义不兼容**；闭源+密钥制 | `01_mdk_bench.md` |
| liberika | seek 196ms（慢一档）；落点 0.75 帧（非帧精确）；无剪辑语义 | 同上附录 A |
| video_player | 无 Windows 支持 | NipaPlay 官方文档 |
| media_kit | = libmpv 的 Flutter 封装，无新能力 | NipaPlay 官方文档 |

---

## 3. 待决策项：cq 档位（磁盘 vs 画质）

### 3.1 数据（T0，4.mp4 全片 13619 段，生产入口构建）

| 档位 | 全片体积 | vs cq22 | render SSIM | phys SSIM | 解码 rtime |
|---|---|---|---|---|---|
| **cq22**（现状）| **1204.9MB** | — | 0.99732 | 0.99513 | 24.4s |
| **cq24** | **969.0MB** | **-20%** | 0.99648 | 0.99385 | 19.8s |
| **cq26** | **788.2MB** | **-35%** | 0.99530 | 0.99196 | 18.8s |

**关键性质**：
- 三档 `source_frames` 映射数组**逐位一致**（换档不改帧语义/时间轴）。
- 低 cq 解码**更快**（比特量小）——省盘与提速同向，唯一代价是画质。
- SSIM 口径：render = GUI 视频面逻辑尺寸（1000×647）下比；phys = 1080p 原始比。
- **代理画质不影响导出**：暂停静帧/逐帧/导出全部回源全质量。

### 3.2 决策选项

| 选项 | 操作 | 收益 | 代价 |
|---|---|---|---|
| A 维持 cq22 | 不动 | 最稳 | 每片 1.2GB |
| B 切 cq24 | 改 `ProxySpec.quality` 默认 6→4 + 重建 | 省 20%，解码快 19% | render SSIM -0.00084（肉眼不可辨）|
| C 切 cq26 | 改默认 2 + 重建 | 省 35%，解码快 23% | render SSIM -0.00202（仍属"基本看不出"下沿）|

**推荐 B（cq24）**：质量损失远低于人眼可辨阈值（SSIM 0.9965），同时省 20% 且更快。

### 3.3 若选 B/C 的执行步骤

> **实测修正（10-13 复核）**：cq24/cq26 的实验代理**就是用生产键函数
> `expected_proxy_key` 建的**，已验证三档键与代理文件一一匹配：
> `quality=6→27e8b0b0…` / `quality=4→e1ec8f11…` / `quality=2→6e2ddb26…`。
> **改默认值后立即命中，无需重建**（此前"需重建 208-250s"的说法是错的）。

1. 改 `proxy_cache.py` 的 `ProxySpec.quality` 默认值（6→4 或 2）。
   - 位置：`ProxySpec` 的 `quality: int = 6`（约 L175）。
   - **同时检查**：`preview_player._proxy_spec()` 是否读取设置面板参数覆盖默认
     （当前只读 `proxy_target_h`；若未来加 quality 设置项需同步）。
2. **无需重建**：目标档代理已在 `.cache/proxy/` 就位，键匹配即命中。
3. 跑 `tests/test_proxy_cache.py`（24 例）+ 全量 `pytest tests/`（基线 675+94）。
4. 跑 `probe_proxy_gui_e2e.py` 确认 40x 仍达标（约 1-2 分钟，缓存命中）。
5. 删除其余实验代理（见 §5.2）；**保留**选中档那份。
6. 若改的是 GUI 默认键，重启 GUI 生效（见 §5.4）。

---

## 4. 其他候选优化（按价值排序，均需 owner 批准「开工」）

| 项 | 收益 | 成本 | 状态 |
|---|---|---|---|
| **局部分析**（只分析框选区间）| 2 小时素材冷启动 337s → 按比例缩短 | 2-3 天 | 未做，待 owner 确认是否常态 |
| 540p 档 | 367MB/片（-70%），重建更快 | 画质 0.985（轻微软化）| 未拍板 |
| 分段录制（源头解决）| 素材变小，全链提速 | 零代码，改习惯 | 建议但未强制 |
| 全 I 帧代理 | scrub 手感更好 | 体积涨 30-44 倍 | 已否决（实测更慢）|

---

## 5. 工作区状态（交接时快照）

### 5.1 代码状态

- 分支：`fix/preview-pacing-metrics`，与 origin 同步（无未推提交）。
- **未提交产品码（P1 物化代理 + 双 bug 修复）**：
  - 新增：`proxy_cache.py`（1157 行）、`tests/test_proxy_cache.py`（561 行/24 例）
  - 修改：`mpv_engine.py`(+244)、`preview_player.py`(+454)、`settings_panel.py`(+17)、
    `tests/test_preview_fps_osd.py`(+1)、`tests/test_preview_dthrough_tier.py`(+93)、
    `tools/dsvc/src/main.rs`(+36/−10)、`tools/dsvc/README.md`(+21)
  - 文档：`ASSET_INVENTORY.md`、`HANDOFF_VERIFIEDSEEK_20260905.md`、`asset_index.json`
  - 未跟踪工具配置：`.zcodeignore`（建议不入库）
- **验证基线**：`pytest tests/` = **675 passed + 94 subtests**；门禁 L0-L2 7/7。
- **阻塞**：P1 待 owner 手检 →「验收」→ 提交。

### 5.2 磁盘状态（`.cache` 约 14GB）

| 项 | 大小 | 处置建议 |
|---|---|---|
| `.cache/proxy/27e8b0b0…mp4`（cq22）| 1205MB | **保留**（GUI 默认键，若选 B/C 则替换）|
| `.cache/proxy/e1ec8f11…mp4`（cq24 实验）| 925MB | 选定后删或启用 |
| `.cache/proxy/6e2ddb26…mp4`（cq26 实验）| 752MB | 选定后删 |
| `.cache/proxy/3dd921b9…`/`fbd3611f…`（2.mp4 测试）| 各 ~207MB | 可删（测试产物）|
| `.cache/proxy/batches/` | 2.4GB/256 文件 | 保留（增量重建用，有 8GB/256 上限）|
| `.cache/kernel_review/` | 表面 3.1GB / **实际 0.72GB**（含硬链接）| 可删（数字已固化）|
| 历史调研证据（research_phase3 等）| ~3.7GB | 10-12 已判定保留 |

**磁盘总量**：`.cache` 约 14GB；D 盘剩余 **133GB**（704G 总量，已用 82%）。

### 5.3 文档地图（接手先读这三个）

1. **本文档**（决策简报，自包含）
2. `ASSET_INVENTORY.md` §六（能力表）+ §七（铁律 R1-R10）+ §八（决策记录）
3. `checklist.md` 头部（当前工单，本地不提交）

专题报告：
- `agents/research_player_kernels/01_mdk_bench.md`（内核实测，含 liberika 附录）
- `agents/research_player_kernels/02_proxy_cross_source.md`（跨源冒烟）
- `agents/research_player_kernels/03_cq_slim_sweep.md`（cq 瘦身曲线）

---

### 5.4 GUI 运行状态（交接时）

- **当前无 GUI 进程运行**（`main.py` 已退出；上一轮手检用的 pid 17196 已不存在）。
- 启动命令：
  ```bash
  ARKNIGHT_PREVIEW_ENGINE=mpv PYTHONUNBUFFERED=1 python main.py
  ```
- **手检清单（owner 执行，5 项）**：
  1. 导入 4.mp4 → 控制台出 `[proxy] 物化代理就绪`（2-3s = 缓存命中）
  2. UI 4x 播放 → 角标「代理40.0x」、画面清晰、零卡顿
  3. 播放中改暂停段 → 立即续播不卡 → 约 10s 后角标自动回「代理40.0x」
  4. 拖/点时间轴 → 落位精确、红条跟随
  5. 关设置里「物化代理」开关 → 回落「直通Nx」
- 手检通过 → owner 说「验收」→ 跑 `python tools/acceptance_gate.py --level L3`
  （L3 需可见桌面，跑前先停 GUI）→ 按 §5.5 提交。

### 5.5 提交拆分方案（验收后执行）

**两笔提交**（材料已备，`.cache/_split_check/`）：

| 笔 | 内容 | 文件 |
|---|---|---|
| ① P1 物化代理 | `proxy_cache.py`(新) + `tests/test_proxy_cache.py`(新) + `mpv_engine.py` + `settings_panel.py` + `tests/test_preview_fps_osd.py` + `preview_player.py` 的 P1 hunks（14 个）| `.cache/_split_check/p1.patch` |
| ② 双 bug + dsvc 竞态 | `preview_player.py` 的 bugfix hunks（4 个）+ `tests/test_preview_dthrough_tier.py` + `tools/dsvc/src/main.rs` + `tools/dsvc/README.md` | `.cache/_split_check/bugfix.patch` |
| ③（建议）文档 | `ASSET_INVENTORY.md` + `HANDOFF` + `asset_index.json` | — |

**拆分已验证**：`base(HEAD) + p1.patch + bugfix.patch` **逐字节重建 == 工作区文件**
（sha256 `ba1a53da…`）。用法：
```bash
git apply --cached .cache/_split_check/p1.patch   # 暂存 P1 hunks
# 提交 ①，再 apply bugfix.patch 提交 ②
```

### 5.6 已知遗留缺陷（非本轮引入，如实登记）

| 缺陷 | 位置 | 影响 | 建议 |
|---|---|---|---|
| `_pan_move` 除零 | `timeline_widget.py:652` `winfo_width()` 未 guard | 窗口未映射时平移量爆炸 | 低风险，可随手加固 |
| `probe_p1_fullbuild_results.json` 非法 JSON | `.cache/v3ass/` | `json.load` 失败（需 `ast.literal_eval`）| 数字已固化在报告，可不修 |
| proxy npz 孤儿 | `.cache/proxy/*.npz` 对应已删 mp4 | 无害占位 | 可清理 |

### 5.7 dsvc 重建前提（仅当需要改 Rust 服务时）

```bash
# 编译环境（值见 checklist/棒 §4.0l）
export FFMPEG_DIR=.cache/v3ass/ffmpeg-dev/ffmpeg-7.1.1-full_build-shared
export LIBCLANG_PATH=~/AppData/Local/Programs/Python/Python311/Lib/site-packages/clang/native
# 换代前必须先停旧 exe（被锁会链接失败）
taskkill //IM dsvc.exe //F
# 产物：.cache/v3ass/dsvc/target/release/dsvc.exe（生产硬编码路径）
```

### 5.8 批缓存现状（影响"编辑重建 0.15s"复现）

- `.cache/proxy/batches/`：**256 文件 / 2.4GB，已达 `max_files=256` 上限**。
- 后果：新增批会按 LRU 淘汰旧批 → 若复现"编辑后 0.15s 重建"而恰好命中被淘汰
  的批，会退化为完整重编（数十秒）。**复现前先确认目标批仍在**。
- 上限常量：`_BatchCache.max_files=256` / `max_total_mb=8192`（`proxy_cache.py:314`）。

### 5.9 分析缓存现状

| 源 | key | 大小 | 状态 |
|---|---|---|---|
| 4.mp4 | `fe3e7bdf…` | 3.9MB | v3，热命中 0.0s |
| 2.mp4 | `789ea0564afa…` | 1.5MB | 10-13 冷跑落盘，热命中 0.02s |

## 6. 复现方法（所有 T0 数字可重跑）

```bash
# 环境
export ARKNIGHT_PREVIEW_ENGINE=mpv
# 测试基线
python -m pytest tests/ -q                      # 期望 675 passed + 94 subtests
python tools/acceptance_gate.py --level L2      # 门禁（L3 需可见桌面）

# cq 曲线（窗口扫描，5 档 × 2 窗，约 5 分钟）
python .cache/v3ass/probe_cq_sweep.py

# 全片构建（各约 4 分钟；注意会新增代理文件）
python .cache/v3ass/probe_p1_cq24_build.py
python .cache/v3ass/probe_p1_cq26_build.py

# P1 全链 E2E（真 GUI，需可见桌面，约 1-2 分钟含缓存命中）
python .cache/v3ass/probe_proxy_gui_e2e.py
python .cache/v3ass/probe_proxy_cross_source.py   # 2.mp4 跨源

# 内核实测（需 MSVC + MDK SDK，见 01_mdk_bench.md §4）
cd .cache/kernel_review && cmd //c build_probe.bat
```

**注意**：`probe_*` 探针会开真实窗口（R8 同源要求），跑前先关掉正在运行的
GUI，并确保是可见桌面会话。所有探针已打 messagebox 补丁（R9），不会挂死。

---

## 7. 给你的决策清单

请逐项给出结论（owner 已授权你决策的范围）：

1. **cq 档位**：A 维持 cq22 / B 切 cq24（推荐）/ C 切 cq26？
   - 三档代理都已在位，**切换=改一行默认值 + 重启，无需重建**（§3.3 已验证）。
2. **是否清理**：2.mp4 测试代理（414MB）、`.cache/kernel_review`（0.72GB 实际）？
   - 注意批缓存已满 256 文件（§5.8），清理时勿误删 batches。
3. **局部分析**：是否立项（2-3 天）？取决于"2 小时素材是否常态"——需问 owner。
4. **`.zcodeignore`**：入库 / 加入 `.git/info/exclude` / 保持未跟踪？
5. **P1 验收**：owner 手检通过后说「验收」→ 跑 L3 门禁 → 按 §5.5 两笔提交。
   **这是当前唯一阻塞项**，其余决策都不影响它。

**决策优先级建议**：先解 5（阻塞项）→ 再解 1（一行改动）→ 2/3/4 可延后。

**不要做的事**（已三度验证，勿重复调研）：
- 不要尝试换播放内核（MDK/liberika/VLC/GStreamer/WebCodecs 全部实测或源码级否决）
- 不要尝试"实时拼接提速"（物理墙，封顶 5-6x）
- 不要尝试全 I 帧代理（实测更慢、体积 30-44 倍）
- 不要重跑四个 frame-oracle 长扫描（AGENTS.md 明令）

---

## 8. 一句话总结

**方案已经是终局（物化代理，40x 实测达标）；剩下的只是档位权衡和工程收尾。**

---

## 9. 本包的自我验证（接手 agent 可先跑这五条）

```bash
# 1) 工作区基线仍绿（约 35s）→ 期望 675 passed + 94 subtests
python -m pytest tests/ -q

# 2) 资产审计零漂移（约 1s）→ 期望 295/295 一致
python tools/inventory_audit.py

# 3) 三档代理在位 → 期望 1205MB(cq22) / 969MB(cq24) / 788MB(cq26) + 2 份 2.mp4 测试
ls -la .cache/proxy/*.mp4

# 4) 三档键匹配（验证 §3.3「无需重建」的结论）
python - <<'EOF'
import json, sys, numpy as np
sys.path.insert(0, '.')
from pathlib import Path
from fractions import Fraction
from proxy_cache import expected_proxy_key, ProxySpec
from analysis_cache import _unpack_pauses
import media_info as mi, av
z = np.load('.cache/analysis/fe3e7bdf0a7b60efc768a34e41efabb7.npz', allow_pickle=True)
states = z['states']; meta = json.loads(str(z['meta_json']))
pauses = _unpack_pauses(meta['pauses'], z['masks_concat'], z['mask_lengths'])
E = Path('.cache/media_info/frame_pts/c2762556bde62e3402b227eeeb1371ba9d961d0441e39948dd739a7dfa2be117/v2-def34eddc6ac3507-2ce797a0f88d7f06.json')
pl = json.loads(E.read_text(encoding='utf-8'))
tb = Fraction(pl['time_base']['numerator'], pl['time_base']['denominator'])
rows = pl['pts_table']; n = len(rows); tool = pl['tools']['ffmpeg']
fi = mi.ToolInfo(Path(tool['path']), tool['sha256'], tool['version_line'], str(tool['verified'])=='True')
c = av.open(r"D:\qq下载\920.mp4"); vs = c.streams.video[0]; w0,h0 = vs.width,vs.height; c.close()
st = Path(r"D:\qq下载\920.mp4").stat()
df = Fraction(rows[-1]['pts'] + rows[-1]['duration'], tb.denominator)
stream = mi.VideoStreamInfo(index=0, codec_name='h264', codec_long_name='H.264', width=w0, height=h0,
    pixel_format='yuv420p', time_base=tb, start_time=Fraction(0,1), duration=df,
    avg_frame_rate=None, r_frame_rate=None, frame_count=n, start_pts=rows[0]['pts'],
    duration_ts=rows[-1]['pts']+rows[-1]['duration'])
media = mi.MediaInfo(source_path=Path(r"D:\qq下载\920.mp4"), source_sha256=pl['source']['sha256'],
    source_size=int(st.st_size), source_mtime_ns=int(st.st_mtime_ns), format_name='mp4',
    format_long_name='MP4', duration=df, start_time=Fraction(0,1), video_streams=(stream,),
    audio_streams=(), vfr_status='vfr', ffmpeg=fi)
cert = mi.FramePtsCertification.from_evidence(E, status='vfr', source_sha256=pl['source']['sha256'], time_base=tb)
media = media.certify_frame_pts(cert)
for q, exp in ((6,"27e8b0b0"), (4,"e1ec8f11"), (2,"6e2ddb26")):
    k = expected_proxy_key(media, states, pauses, meta['speeds'], [], speedup_1x=True,
        speedup_02=True, speedup_02_factor=10, fps=59.999717, spec=ProxySpec(target_h=1080, quality=q))
    print(f"quality={q}: {k[:8]} expected={exp} match={k.startswith(exp)}")
EOF

# 5) 分析缓存在位 → 期望 4.mp4(3.9MB) + 2.mp4(1.5MB)
ls -la .cache/analysis/*.npz
```

**预期**：① 675 passed + 94 subtests；② 295/295 一致；③ 五个 mp4
（cq22 1205MB / cq24 969MB / cq26 788MB / 2.mp4 测试代理两份各 ~207MB）。

**若数字不符**：以 `ASSET_INVENTORY.md` §六 与 `agents/research_player_kernels/`
三份报告为准；本包为 2026-10-13 快照。

**决策完成后请回写**：
1. 决策结论 → `ASSET_INVENTORY.md` §八（决策登记表，带日期/上下文/选择/后果）
2. 若改了默认档位 → `checklist.md` 头部 + 重跑 §9 三条
3. 若清理了缓存 → 更新 §5.2 的磁盘表
