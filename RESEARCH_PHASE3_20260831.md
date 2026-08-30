# RESEARCH —「分批寻址导出」阶段三前置调研（2026-08-31）

> 任务：为把 `.cache/exp_batched_seek.py` 原型整合进 `analyzer.export_pts_schedule`（阶段三）
> 做前置调研。只调研 + 实测，未改任何生产代码；所有脚本与产物在
> `.cache/research_phase3/`（本文引用的 JSON/日志都在其中）。计时实验全部串行执行。
>
> **TL;DR（五句话）：**
> 1. 原成本模型的核心推断"平铺 select 逐帧求值爆炸"**被实测推翻**：N=1600 仅比纯解码慢
>    18s（边际 ~25~45ns/项·帧，与解码完全重叠），N=2623 外推仅 ~4 分钟。
> 2. 千段规模真正随段数爆炸的是**音频 atrim/concat 图**（fork 同款结构实测：400 段 120s、
>    800 段 >600s 超时、2623 段 0 进展）——它才是生产单趟 3 小时超时的真凶（GUI 默认
>    `export_keep_audio=True`）。
> 3. 视频-only 生产完整形态单趟（2623 段 + 真实 x264 + 全部 vfr 参数）**660s 正常跑完**，
>    且与分批原型产物 **107434/107434 帧 PTS 逐帧一致**——样本 4 从此有了单趟基准，
>    分批=单趟等价在目标样本上完全成立。
> 4. 音频分批路线二选一已定：**路线 A（每批 AAC + `-c copy`）出局**（批边界累积漂移
>    +1024/+2304/+3584 样本），**路线 B（批内 PCM 中间件 + 最终统一 AAC）双 PASS**
>    （音频 ±0 样本、视频 PTS 均匀）；atrim `start_sample/end_sample` 实测样本级精确。
> 5. 分批原型 46.6 分钟里大头是**每批扫到 EOF 的解码浪费**（批 0 白扫 6900s 源=200s，
>    楼梯形累加 ≈46 分钟）——阶段三每批必须加 `-frames:v` 输出上限；并行编码被实测
>    支持（x264 同命令字节级确定 + 独立批拼接 PTS 全对），另发现一个**既有**的容器
>    时长元数据地雷需在阶段三加后检。

---

## 0. 环境与口径

- 样本：`D:\qq下载\920\{1,2,3,4}.mp4`。认证缓存全部命中（`.cache/media_info/frame_pts/`），
  未重跑任何 frame oracle 长扫描。四样本当前认证**全部为 `vfr`**、time_base 1/15360：

  | 样本 | 帧数 | 状态 | 时长 |
  |---|---|---|---|
  | 1 | 249097 | vfr | ≈69 分钟 |
  | 2 | 184293 | vfr | ≈51 分钟（坏时间戳） |
  | 3 | 29804 | vfr | ≈8.3 分钟 |
  | 4 | 424176 | vfr | ≈118 分钟（2623 段，本次目标） |

  ⚠️ 由此修正一个旧印象：**当前没有认证为 `cfr` 的样本**（DESIGN §6 "样本 1 走 CFR 逐段路"
  与现状不符——四个样本现在都走平铺 select 分支；CFR 逐段路只对"未来认证为 cfr 的源"生效，
  见 §B 的缩放结论）。
- FFmpeg：`tools/ffmpeg-7.1.0/bundle/.../ffmpeg.exe`（sha256 `2ce797a0…`，与 golden 时代一致）。
- 计时口径：`-f null -` 隔离编码（默认 wrapped_avframe，只测解码+滤镜）；生产形态实验除外
  （A2a 用真实 x264+mp4）。所有 ffmpeg 调用串行执行，无并发污染。

---

## A. 单次平铺 select 的耗时曲线 → 分批阈值

**方法**：`A_flat_select_curve.py`（日志 `A_flat_select_curve.log`、数据
`A_flat_select_results.json`）。复刻 `analyzer._pts_select_setpts_video_filter`（含 tpad 哨兵）
+ 生产命令参数（`-copyts -i` + `-fps_mode:v passthrough`），输出换 `-f null -`。
样本 4 的 N∈{100,200,400,800,1600} 子表从 2623 真实段**等步长抽取**（保持全片分布）；
样本 3 的 N∈{400,1600} 用时间表覆盖范围等分 50% 占空的合成 tick 段（108 真实段不够大）。

**原始数据（墙钟秒）**：

| 测点 | 样本 4（424176 帧） | 样本 3（29804 帧） |
|---|---|---|
| floor（纯解码，无滤镜） | 203.4 | 13.6 |
| N=100 | 203.5 | — |
| N=108（真实段） | — | 13.2 |
| N=200 | 202.0 | — |
| N=400 | 202.3 | 13.4（合成） |
| N=800 | 206.6 | — |
| N=1600 | 221.4 | 16.8（合成） |

**结论（二元：平铺 select 表达式在千段规模是否爆炸 → 否）**：
1. N≤400 与纯解码无差别（±1s），N=800 +3.2s，N=1600 +18.0s。折算边际成本约
   **25~45ns/项·帧**，且滤镜线程求值与多线程解码完全重叠。线性外推 N=2623 ≈
   **240~260s（约 4 分钟）**。样本 3 同形状（N=1600 仅 +3.2s）。**原"424k 帧 × 2623 段
   ≈ 2.2×10⁹ 次解释求值 → 3 小时"的推断不成立。**
2. **"单次 ≈ 分批"的表达式成本交叉点不存在**：null 域里单趟(2623)≈4.4 分钟，反而远低于
   分批原型的 46.6 分钟（那 46.6 分钟见下条）。分批的胜负手在音频图（A2）、解码量与并行化，
   不在表达式。
3. **附带发现（新优化点）：select 语义不会提前 EOF，而 trim/concat 图会。**
   trim 链在最后一段结束后整图 EOF、ffmpeg 停止读源（这解释了 §3.1 缩放测试"50 段 4s"——
   只解码到末段帧号）；select 只丢帧不 EOF，**必须解码到源 EOF**。实测：分批-null 单批
   （段 0..99，`-ss 35.3s`）耗时 202s ≈ 从 34.3s 扫到 EOF 的全量解码；段 1200..1299
   （`-ss 4032s`）90.4s ≈ 剩余 3048s 源。楼梯形累加（Σ≈46 分钟）与原型 27 批 46.6 分钟
   高度吻合，而真实编码只需 ~11 分钟（见 A2a）——**原型每批都在白扫批尾剩余全片**。
   → **阶段三必做：每批命令加输出上限 `-frames:v {批帧数+1}`（生产单趟本来就有，分批移植
   时不能丢），预计把样本 4 视频分批从 ~47 分钟压到 ~15 分钟以内（单线程）。**

**对阶段三设计的影响**：VFR 大段数启用分批的理由重写为：①音频图必须分批（A2 实锤）；
②解码可省（批尾上限 + 只解码批跨度）；③唯一能并行化的形态（F 实测支持）。
**建议阈值（二元结论）**：分批的启用不由视频表达式成本决定，由音频图决定——
实测音频图 400 段尚可（120s≈纯音频解码地板）、800 段即超时，**建议 N > 400 段即走分批**
（与批大小 100 组合）；无音频的短表（N≤400）可留单趟。

### A2 补充：生产形态定位真凶（音频图）+ 样本 4 首个单趟成品

**方法**：`A2_production_singlepass.py`（日志 `A2_production_singlepass.log`、数据
`A2_production_singlepass_results.json`）。

**A2b：fork VFR 分支同款音频 atrim/concat 图**（`_fraction_filter_seconds` tick→秒换算），
样本 4 真实音轨，`-f null`：

| N | 结果 |
|---|---|
| 400 | 120.0s 完成（≈纯音频解码地板） |
| 800 | **600s cap 超时被杀** |
| 2623 | **600s cap 超时被杀**（无输出进展） |

与 §3.1 上游缩放数据（音频图 50 段 2s → 200 段 14s → 800 段 >600s）互相印证：
**音频 atrim/concat 图随段数超线性爆炸，800 段即不可用。**

**A2a：视频-only 生产完整形态单趟**（真实 x264 crf18 + `-bf 0 -enc_time_base 1/15360
-video_track_timescale 15360 -frames:v 107434` + mp4）全量 2623 段：
**rc=0，660.0s（11 分钟）完成，输出 1,362,195,228 字节**（与分批原型总量一致）。

**等价性验证**（`A2a_vs_batched_check.py` / `A2a_vs_batched_check.json`）：
A2a（单趟）vs `4_batched.mp4`（分批）：**107434/107434 帧 PTS 逐帧一致，内容灰度最大差
0.0936 → PASS**。样本 4 从此拥有单趟基准；阶段二"4 号无基准、只能对认证表"的限制被解除。

**闭环**：GUI 导出默认 `export_keep_audio=True`（preview_player.py:1816），生产单趟在样本 4
上的 3 小时超时（回归脚本 `ffmpeg_timeout=10800`；GUI 默认 1800s）由**音频图爆炸**解释
（视频滤镜仅 ~4 分钟、视频-only 全程 11 分钟）。这也解释了为什么 v26.7.23（上游
trim/concat 双图）和 fork（平铺 select 视频 + 同款 atrim 音频图）在样本 4 上**都**死：
两条路的视频图病不同，音频图病相同。

---

## B. CFR 分支是否同病（千段 trim/concat 链）

**前提修正**：四个样本当前均认证 vfr（§0），无可挑的 cfr 样本。改用两个替代源：
①合成 CFR 源 `B_src_cfr_120s.mp4`（testsrc2 640x360@60fps 120s，构造即恒定帧率）；
②真实样本 3 只取前 5s 输出帧（300 帧，隔离"建图+解析"成本与解码量）。
结构完全复刻 fork CFR 分支：`[0:v:0]trim=start_pts=S:end_pts=E,setpts=PTS-STARTPTS[v_i]`
+ `concat=n=N:v=1:a=0`，段为等分 50% 占空。cap 600s/条。脚本 `B_cfr_branch_scaling.py`。

**原始数据（`B_cfr_scaling_results.json`）**：

| 测点 | N | 耗时 |
|---|---|---|
| 合成源全片（7200 帧） | 100 / 250 / 500 | 1.2 / 3.8 / 12.2 s |
| | 1000 / 2000 | 40.8 / **163.1 s** |
| 合成源只跑 5s（300 帧） | 500 / 1000 / 2000 | 2.0 / 6.8 / **26.9 s** |
| 真实样本3前5s（300 帧） | 1000 / 2000 | 2.1 / 7.9 s |

**结论（二元：CFR 分支千段是否爆炸 → 是，且比视频平铺 select 病得更重）**：
1. **建图/调度成本随段数超线性（≈平方级）**：只喂 300 帧时 N 翻倍耗时 ×3.4~×4.0
   （500→1000→2000：2.0→6.8→26.9s），与帧数无关——纯粹的图规模病。
2. **每（帧×链）成本 ≈ 9.4µs**（全片 163.1s − 建图 26.9s = 136s ÷ (7200×2000)），
   比平铺 select（25~45ns/项·帧）**高两个数量级以上（~250~350×）**——每条 trim 链是
   独立滤镜实例，帧要过队列调度，而平铺 select 是单滤镜内表达式求值。
3. 按样本 4 规模外推 CFR 分支：424176 帧 × 2623 链 × 9.4µs ≈ **2.9 小时**——与历史上
   "3 小时超时"的量级吻合（若该源走 CFR 逐段路）。
4. **分批有效**：批内 100 链 × 批内帧数 ≈ 9.4µs×(1616 帧×100) ≈ 1.5s/批 + 编码，
   完全无压力。

**对阶段三设计的影响**：CFR 逐段路虽然今天不被四个样本触发，但对"未来认证为 cfr 的源"
在 N≳500 后即进入平方病区。**建议分批路径的触发条件写成"vfr 且 N>400"或"cfr 且 N>500"
共用于同一分批机制**（批内每段 trim 链数量少，天然免疫）；不需要为 CFR 设计第二种批结构。

---

## C. golden 目录盘点

5 个 `PRODUCTION_PTS_GOLDEN_*/` 全部是 **12 帧合成小夹具**（`scripts/record_gate_decision*.py`
生成，服务 MPV gate 决策 v1–v4），**与四个真实样本无关**：

| 目录 | 源（sha256 前 8） | 分支（pts_consumer） | fps_mode | 音频 | 内容 |
|---|---|---|---|---|---|
| GOLDEN_20260816_v1 | 8deeb96f（合成 CFR 12帧, tb 1/1000） | —（无 manifest，早期迭代） | — | — | export.mp4 3.9KB + evidence |
| GOLDEN_20260816_v2 | 8deeb96f | —（**未完成**：仅 evidence，无 export.mp4） | — | — | evidence only |
| GOLDEN_20260816_v3 | 8deeb96f | ffmpeg_trim_pts_concat（CFR 逐段路） | cfr | muxed | export.mp4 + manifest/ffprobe/pts_oracle；2 段 [0,160)+[240,480) ticks |
| GOLDEN_VFR_20260816_v1 | cd311e79（合成 VFR 12帧, tb 1/1000） | ffmpeg_select_pts_setpts（平铺 select 路） | passthrough | no_stream | export.mp4 + manifest 等；2 段 [0,200)+[280,620) |
| GOLDEN_VFR_20260816_v2 | cd311e79 | 同 v1（v1 重录，export.mp4 sha 不同；gate v4 引用 v2） | passthrough | no_stream | 同上 |

**样本 4 是否有可用基准（明确答案）**：
- golden 目录**不能**当"分批 vs 单次"等价性基准（12 帧玩具夹具；源 hash 与 920 样本无交集）；
- 调研前，真实单趟基准只有 `.cache/exp_trim_concat_vfr/{2,3}_baseline.mp4`（阶段二生产导出
  产物），样本 4 **没有**单趟成品；
- **调研后情况改变**：A2a 以生产完整形态跑出了样本 4 的首个单趟成品，并与分批原型
  107434/107434 帧 PTS 逐帧一致（见 §A2）——它就是样本 4 现在的基准
  （`.cache/research_phase3/A2a_singlepass_v2623.mp4`）。阶段三回归策略可升级为：
  2/3 号对既有单趟基准、**4 号对 A2a** 逐帧比对。

---

## D. 音频分批路线实测（最高优先级）

### D1 路线 A：每批 AAC + concat demuxer `-c copy` → **出局**

**方法**：`D1_audio_route_aac_concat.py`（数据 `D1_aac_route_results.json`）。
基准 = 48kHz 单声道粉噪声 30s（seed 1234）PCM（`D_ref_raw.wav`）；单趟参照 = 一次 AAC 编码；
分批 = 3×10s 批，每批 `atrim` 切块后各自 AAC 编码 → concat demuxer `-c copy` → 解码回 PCM；
对齐 = 每批对基准做 FFT 互相关找最优整数样本偏移（噪声 ±1 样本即去相关，峰极锐；
单趟参照的对照偏移全 0，验证方法本身）。

**原始数据**：

| 批 | 单趟参照偏移 | 分批偏移 |
|---|---|---|
| 0 | +0 | **+1024 样本** |
| 1 | +0 | **+2304 样本**（较批 0 再 +1280） |
| 2 | +0 | **+3584 样本**（较批 1 再 +1280） |

解码长度：基准 1,440,000 样本；单趟 1,440,768；分批 1,443,840（多 3840 样本 = 每批边界
缝隙的累积）。

**结论（二元：路线 A 是否可行 → 否）**：每批 AAC 的帧量化（1024 样本/帧）与 priming/edit
list 在 `-c copy` 拼接时转化为**批边界系统性偏移，且线性累积**（批 2 累计 +3584 样本 =
74.7ms，远超 1 样本判据，也超过一个认证 tick 65.1µs 两个数量级以上）。**路线 A 出局，
走路线 B。**

### D2 路线 B：批内 PCM 中间件（.nut）+ 最终统一 AAC → **双 PASS**

**方法**：`D2_audio_route_pcm_intermediate.py`（数据 `D2_pcm_route_results.json`）。
每批 = .nut（h264 视频 + pcm_s16le 音频，各 10s，噪声与 D1 同源）→ concat demuxer
（**每批写精确 `duration 10.0` 指令**——阶段二已踩过的坑）→ `-c:v copy -c:a aac` 一次性
编码音频。验证音频批边界偏移 + 视频帧数/PTS 连续性。

**结果**：
- 音频：三批偏移 **全部 +0 样本**（峰锐利，零漂移）；
- 视频：900/900 帧，PTS 步进集合 = {2048}（30fps 完美均匀，tb=1/61440）；
- （对照：不写 duration 指令时批边界出现 5161/5489 的异常步进——再次确认 duration 指令
  是必需项。）

**结论（二元：路线 B 是否可行 → 是）**：批内 PCM 无损中间件 + 最终统一 AAC 编码，
批边界样本级透明。.nut 封装 h264+pcm_s16le 经 concat demuxer 正常工作。
**阶段三音频走路线 B**（每批音频 `atrim`/concat 后以 PCM 随批封装，最终一遍 AAC）。

### D3 atrim `start_sample/end_sample` 样本级精确性 → **是**

**方法**：`D3_audio_route_atrim_samples.py`（数据 `D3_atrim_sample_exact_results.json`）。
同一噪声源 asplit 三路：`start_sample=48000:end_sample=96000` vs `start=1.0:end=2.0` vs
分数秒版（1.0000104s，落在 48000.4992 样本处），对照全量解码后 numpy 切片真值
（48,000 样本）。

**结果**：三路长度全部 48,000；`start_sample` 版、整秒版、**分数秒版都与真值字节级相等**
（first_diff_index 全 null）。

**结论**：atrim 的 `start_sample/end_sample` 与整样本对齐的秒数版**等价且样本级精确**；
阶段三批内音频切分两种写法都安全（建议仍用秒数版与现生产代码一致，或换 sample 版省去
分数换算——二者实测无差）。

**对阶段三设计的影响（D 汇总）**：
- 音频分批结构定型：批内 `[0:a:0]atrim=…,asetpts=…`（≤100 条链，成本可忽略）→ 批内
  concat → **PCM** 随批进中间容器（.nut 实测可行）→ 最终 `concat -c copy -c:a aac` 一遍编码；
- concat 列表必须写精确 `duration`（视频批跨度照阶段二全局公式算）；
- 不采用每批 AAC 直编（D1 反证）。

---

## E. 生态调研（子代理网络调研，GLM-5.3-Flash ×2，并行于本地实测）

### E1 VapourSynth 千段 Trim/Splice + Windows 打包（子代理 1）

- **机制可行但无"更快"证据**：Trim/Splice 无段数上限，但官方 issue #37 承认 n 段拼接 =
  n 个 cache 实例（内存/管理开销随段数线性增长）；issue #561 用户原话"dozens or hundreds
  of clips 拼接不可行"；akarin 插件实测到 255 输入为止。**未找到任何千段级基准数据。**
  官方推荐替代是 FrameEval/RemapFrames（帧映射思路，与本项目分批思想同源）。
- **VFR PTS 保留是硬伤**：VS 核心按帧号工作，VFR 工作流要"编码后用 timecodes 重灌时间戳"
  （mkvmerge `--timestamps` 或 mp4fpsmod）——**两者都是新二进制依赖，直接违反本项目约束**。
- **打包成本有数字**：LGPL-2.1-or-later（无授权阻碍）；Windows wheel 15.2 MB、portable
  zip 22.1 MB；要求 Python ≥3.12；PyInstaller 有先例（FrameForge、charlotte），但插件
  自动加载在单 exe 内的行为需自行验证。
- **判定：不迁移。** 核心理由不是体积（15~22MB 可接受），是 VFR 链路必须引入新二进制 +
  千段性能无证据 + 放弃已验证的 ffmpeg 管线。

### E2 `-ss` 输入寻址帧精度的文档佐证（子代理 2）

- 官方文档原文：`-ss` 作输入选项时"seek to the closest seek point before position，
  **transcoding 且 `-accurate_seek`（默认）时，seek point 到目标位之间的段落被解码并丢弃**"
  → 重编码场景帧精确是**文档级保证**；`-c copy` 或 `-noaccurate_seek` 才不精确。
- `-copyts`："不处理输入时间戳、原样保留"→ 与本机 `probe_seek_pts.py` 实测一致；不加它
  输入寻址会把时间轴平移归零。
- 已知反例是**容器级**的：trac #5093（MPEG-PS/TS demuxer 关键帧索引错误导致 `-ss` 偏移），
  MP4/MKV 有关键帧索引不受影响。文档未明文"帧集合与不寻址全读完全一致"，边界舍入方向
  需实测兜底（阶段二已对 2 号实测 PASS；本调研 A2a 与分批产物逐帧一致再添一证）。
- **建议把"仅用于有关键帧索引的容器（MP4/MKV）"写进分批实现的前置条件。**

### E3 PyAV 编码侧成熟度（子代理 2）

- **参数无硬缺口**：libx264 参数经 `CodecContext.options` 全覆盖（crf/preset/bf/x264-params
  均有实例）；`-movflags`/`-video_track_timescale` 走 container options（#1959 实测生效）；
  `-enc_time_base` 无同名物（手工设 `time_base`）。
- **但调度层是真空**：PyAV 没有 `fps_mode passthrough` 的对应物，VFR PTS 正确性全靠手工
  维护 frame.pts 与 time_base 一致（#1959 的 0.73fps 事故至今 open）。
- **性能劣势是维护者明示的**（#535："I'd expect the ffmpeg command to generally be faster"；
  #1691 管线未优化；默认单线程需手工开线程）。
- 版本现状：PyPI 最新 18.1.0（2026-08-12，绑 FFmpeg 8，要求 Python ≥3.11）——与实测校准过
  的 ffmpeg 7.1 行为不保证一致，迁移等于重做整轮帧精度实测。
- **判定：不迁移，维持 ffmpeg.exe 子进程形态。**

### E4 现成剪辑工具适用性（子代理 1）

- **LosslessCut**：普通剪切只对齐关键帧；smart cut（实验性）= "段首到下一关键帧重编码 +
  其余 stream copy"的混合流，不支持批量、单视频轨、拼接点 glitch 风险（#126/#1216）——
  不能覆盖 2623 段全量帧精确重编码。其 batch 文档官方建议就是"拿到底层 ffmpeg 命令自己写"。
- **mkvmerge**：所有 split 模式只吸附关键帧（官方文档明文），非帧精确，且是新二进制。
- **mpv EDL**：播放器内虚拟拼接，不产出文件、不重编码，与导出无关。
- **Avidemux**：无千段级案例，GUI 工具形态不适合当导出后端。
- **备选架构（唯一值得记录）**：单遍编码 + `-/force_key_frames`（时间戳文件）在每个保留段
  起点强制 IDR + 按关键帧无损切分。代价：被丢弃段也被完整编码（保留率高才划算）、IDR 落点
  "取整到 encoder time base"有差一帧风险需实测、~29KB 时间戳列表必须走文件加载。判定：
  **中等迁移成本、收益不确定，暂不采用。**
- **总判定：在"帧精确 + VFR 保 PTS + Windows 单 exe + 只用现成 ffmpeg.exe"约束下，
  分批寻址没有被任何来源否定的硬伤，仍是约束下最优解；E 未发现更优方案。**

---

## F. 并行编码确定性

**方法**：`F_parallel_determinism.py`（数据 `F_parallel_results.json`）。
F1：同一条批命令（样本 2 段 0..59，分批寻址同款参数）跑两遍比 sha256；
F2：批 A=段 0..59、批 B=段 60..119 独立编码 → concat demuxer（含精确 duration 行）→
与单趟基准 `2_baseline.mp4` 的前 K 帧逐帧比 PTS + 内容（前 120 段是整表前缀 → 输出应为
基准输出的前缀）。

**结果**：
- **F1：字节级一致**（两遍 sha256 同为 `186252bcccadc0b2…`；单遍 90s）——
  **x264 同参同机器输出确定论成立，并行/重跑具备可复现性。**
- **F2：除尾帧外全部 PTS 一致**（0 mismatch / 4729 帧，内容最大灰度差 0.027）——
  **独立编码的批经 concat 后与单趟基准逐帧同源。**

**F2 尾帧异常的定位（新发现，重要）**：候选比基准少 1 帧开始；带 tpad 哨兵的批补齐帧数后，
尾帧 PTS 为 42,954,240（垃圾值）。包级定位：tpad 克隆帧**继承了源帧的 duration 元数据**，
样本 2（坏时间戳源）在该位置给出 **41,743,872 ticks（≈44 分钟）的病态 duration**，mov muxer
随之在容器层多写一个零时长幻影样本（pts = 克隆末 + 病态 duration）；`-c copy` 拼接后该幻影
变成可解码帧。三个关键事实：
1. 该病是**既有的、系统性的**：生产单趟基准 `2_baseline.mp4`、原型 `4_batched.mp4`、A2a 的
   容器声明时长全部是万亿 ticks 级垃圾值（解码帧序列全部正常）——**不是分批引入的回归**；
2. 单批文件里幻影不参与解码（decode 层验证看不见它），**concat 后才显形**——现有
   "解码对 PTS"验证法对此盲；
3. 原型全量表跑 2/3/4 号未触发，是因为哨兵只在整表末批、那些位置源 duration 健康；
   **"最后保留段之后源片还有大段跳过内容"的场景（尾部跳过）就可能触发**。
→ **阶段三必做**：①末批克隆帧 duration 归一化（或改哨兵机制）；②回归新增容器级后检
（输出流声明时长 vs 期望 tick 跨度）；③生产 `-frames:v` 帽子保留（它与本地雷无关但有独立价值，见 A）。

**结论（二元：并行化是否可行 → 可行）**：F1 确定论 + F2 拼接保真均成立；尾帧异常是
duration 元数据问题、与"并行/独立编码"本身无关（单趟产物同样带病态容器时长）。
阶段三可选 2~4 路并行批编码（预计样本 4 视频 11 分钟编码 → 3~6 分钟）。

---

## 汇总：对阶段三设计的修订建议

1. **架构维持"分批寻址"**（A/B/D/E/F 全部支持；E 的唯一备选 force_key_frames 不划算，
   PyAV/VapourSynth/现成工具均出局）。同时 A2a 证明**视频-only 单趟在 2623 段也只需
   11 分钟**——若不想动视频路径，"视频单趟 + 音频分批 + 混流"是合法的最小改动选项；
   但推荐统一分批（音频视频同批、并行化、批尾上限后总耗时更低且路径唯一）。
2. **触发条件改写**：不是"段数大 → 表达式贵"（实测近乎免费），而是
   **"N>400 → 音频图爆炸"**（vfr）；cfr 源建议 N>500 也走分批（B 的平方病）。
3. **阶段三新增必做项一：批尾输出上限**（`-frames:v 批帧数+1`）——消除每批扫到 EOF 的
   解码浪费（46.6 分钟里 ≈35 分钟是它，A 楼梯形实测）。
4. **阶段三新增必做项二：音频走路线 B**（PCM 中间件 + 最终一遍 AAC），concat 列表写精确
   `duration`；不用每批 AAC（D1 累积漂移反证）。
5. **阶段三新增必做项三：克隆哨兵 duration 地雷处理 + 容器时长后检**（F 定性；
   既有系统性状况，非分批回归，但 concat 会放大它）。
6. **可选并行**：2~4 路批编码（F 实测支持），样本 4 总耗时有望进入 **5~10 分钟**量级。
7. **等价性验收升级**：样本 4 现在有单趟基准 A2a（§A2），阶段三回归可以对它逐帧比对，
   不再只能对认证表。

## 附：脚本与数据索引（`.cache/research_phase3/`）

| 文件 | 说明 |
|---|---|
| `A_flat_select_curve.py` / `.log` / `A_flat_select_results.json` | A 主曲线（12 测点串行） |
| `A2_production_singlepass.py` / `.log` / `A2_production_singlepass_results.json` | 音频图缩放 + 视频-only 生产形态单趟 |
| `A2a_singlepass_v2623.mp4` | **样本 4 首个单趟基准**（660s，1.36GB） |
| `A2a_vs_batched_check.py` / `.json` | A2a vs 4_batched 逐帧比对（PASS） |
| `B_cfr_branch_scaling.py` / `.log` / `B_cfr_scaling_results.json` | CFR 分支缩放（10 测点） |
| `B_src_cfr_120s.mp4` | 合成 CFR 源（640x360@60, 120s） |
| `D1_audio_route_aac_concat.py` / `D1_aac_route_results.json` | 路线 A 实测（FAIL：累积漂移） |
| `D2_audio_route_pcm_intermediate.py` / `D2_pcm_route_results.json` | 路线 B 实测（PASS×2，duration 行已内置） |
| `D3_audio_route_atrim_samples.py` / `D3_atrim_sample_exact_results.json` | atrim 样本精度（精确） |
| `D_ref_raw.wav` / `D1_*.m4a` / `D2_*.nut` 等 | D 系列基准与批文件 |
| `F_parallel_determinism.py` / `F_parallel_results.json` | 并行确定性 + 地雷定位 |
| `F2_batch*.mp4` / `F2_concat*.txt` | F2 批文件与 concat 列表 |
