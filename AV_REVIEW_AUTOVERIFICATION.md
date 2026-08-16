# A/V Review-Only 自动预验证记录

日期：2026-08-13

本记录描述一次有界、review-only 的自动预验证。它不是 libmpv Gate 证据，也不创建
source observation、source anchor、split bundle 或代理。

## 结论

- 状态：`REQUIRES_EXTERNAL_TRUTH`
- scope：`review_only`
- `media_pts_authority=none`
- `can_register_source_anchor_directly=false`
- `production_consumer_allowed=false`
- `gate_approval=false`
- `proxy_created=false`
- G2 仍为 `BLOCKED`

自动验证证明三个窗口都同时存在画面变化和音频信号，但只能计算最近的 sample-grid
配对，不能证明二者属于同一个内容事件。因此没有自动宣布同步通过，也没有改变任何
G0-G6 状态。

## 输入与范围

复用已发布的样本 3 `[316,326)` schema v3 候选报告，不重跑 scanner：

```text
.cache/mpv_spike/pts_normalize/3_av_event_candidates_316_326.v3.json
SHA-256: d3bc2ead7cae489644d5a3dcb934525a425652d1a58f0c20590f479baa16b18b
```

源媒体和 FFmpeg 沿用候选报告的身份绑定：

```text
source: D:\qq下载\920\3.mp4
source SHA-256: c4bda334c64a68856caf19fb2f7ec396c9533bf7b18e4cc3316fbabac8e5d976
ffmpeg: C:\Users\AAA\AppData\Local\Programs\Python\Python311\Lib\site-packages\imageio_ffmpeg\binaries\ffmpeg-win-x86_64-v7.1.exe
ffmpeg SHA-256: 2ce797a0f88d7f067180338fb227f7b1928ea727bd9a4d7a1d022f7c52af71a3
```

## 生成物

结果 manifest：

```text
.cache/mpv_spike/av_event_review/3_316_326_review_v1/review_bundle.json
SHA-256: d197c07c057a96f70288ebe70682ca031b0f54a71fdd6d73619dd582493ca87
```

窗口：

| 窗口 | 范围 | 候选 | 视频帧 | 音频窗口 | 自动状态 |
|---|---|---|---:|---:|---|
| `full_window` | `[316.0,326.0)` | 316.1、316.2、325.4 | 100 | 500 | `REQUIRES_EXTERNAL_TRUTH` |
| `context_01` | `[312.15,320.15)` | 316.1、316.2 | 80 | 400 | `REQUIRES_EXTERNAL_TRUTH` |
| `context_02` | `[321.4,329.4)` | 325.4 | 80 | 400 | `REQUIRES_EXTERNAL_TRUTH` |

每个窗口均生成 MP4、WAV、波形图和联系图。所有产物仅用于复核，review MP4 的转码格式
不作为源媒体像素格式或生产代理证据。

## 实现与测试

新增：

```text
scripts/build_av_review_bundle.py
tests/test_build_av_review_bundle.py
```

工具约束：

- 只接受并复核既有 schema v3 候选报告；
- 校验源文件和候选报告 SHA-256；
- 输出目录和各文件采用只写一次/原子发布；
- 自动摘要只报告视频帧差、音频采样窗口和最近 sample-grid 配对；
- 明确禁止 source anchor、代理和 Gate 发布；
- 候选或信号不能自动升级为人工 observation。

验证结果：

```text
新增 review-only 专项：4 passed
review-only + scanner 专项：28 passed
全仓 unittest：294 passed
compileall：passed
git diff --check：passed（仅既存 LF/CRLF 提示）
```

命令行返回码 `10` 是该工具对 `REQUIRES_EXTERNAL_TRUTH` 的有意阻塞返回码，不是生成失败。

## 后续决策

如果不再争取真实样本的正向内容锚点，本次自动验证已经足以安全失败闭合：保持 G2
`BLOCKED`，不重复 `[316,326)`，不扩大盲扫，转生产线阶段 3。阶段 3 的下一张小工单是
用 ffprobe JSON 建立结构化 `MediaInfo`，随后收敛统一 `MediaExporter`。

如果仍要争取正向 G2，必须在这些 review 素材上获得独立外部真值，确认画面事件和声音
事件属于同一内容事件；确认前不得创建 source observation/anchor。即使确认成立，仍须
先登记 source-only anchor，再生成非零窗口带音频代理，并验证音频采样时钟、内容锚点和
EDL writer 的代理 PTS/tick 消费。

G0-G6 全部通过前继续保留 `VideoIOThread`，不得设计或接入生产 `MpvEngine`。
