# HANDOFF_NATIVESTEP_20260905 — 原生步进会话交接

> **§0：新会话先读 ASSET_INVENTORY.md**（资产/能力/铁律/决策唯一权威），
> 再读 checklist.md（当前工单，本地不提交），本文件为冷档交接棒。
> 前棒：HANDOFF_C4STILL_20260904.md。

## 1. 本会话成果（已提交，等推送）

| 项 | 内容 | 验证 |
|---|---|---|
| ① mpv 业务加速 | 拆拒播门；`_biz_speed` 段表钉入引擎，time-pos 观察事件按上游 `speed_step` 同语义（闭区间、首中段优先：1X→2、0.2X→max(2,factor)）在段边界切 mpv speed；EDL 经 `_frame_for_position_locked` 折回源帧同表生效；preview_player 拆掉 ignore_biz 截流 | pytest 段切速两测试 |
| ②G 选项1 原生帧步进 | `MpvEngine.step_frame(±1)` 直发 `("frame-step",)/("frame-back-step",)`（播放中先落暂停）；preview_player `←/→` 命中 `_native_step_enabled`（原生引擎+暂停+step_frame 可用）→ `_native_step`：撤贴图层 + `_still_frame_idx` 收敛标记（防渲染循环追帧重贴 C4）+ `_osd_pending`；失败/异常回退旧 seek+贴帧路径；长按跳过 `_schedule_preview` 周期预览寻址与松手末拍寻址 | pytest 445+90 全绿；集成烟测 smoke_engine_step PASS（真 libmpv 6进+1退 1.5-50ms） |
| 探针D（原生步进验证） | 裸命令位级精确：6步+后退全 bit-exact（mad≤0.024），0.3-17.2ms（均值8.6），VFR 源成立；校准偏移 0.0 即精确落帧 | probe_frame_step.py，T0 |
| 探针E（seek落点读回） | **暂停 exact seek 后 time-pos 报实际呈现帧 PTS（非目标值）**：52 seek×26点+16静止点，可分辨点 12/13 支持、唯1在16.7ms模糊带；偏离落点∈[-2,+2]帧；+5/120 修正实测落 +1~+2 帧（60fps 区） | probe_seek_timepos.py + probe_static_identity.py，T0 |

## 2. 关键新事实（均已登记 ASSET_INVENTORY §六）

1. **libmpv 身份**：`tools/libmpv/dll/libmpv-2.dll` = mpv **v0.41.0-926-ge034d612c**（zhongfly winbuild，见 build-record）。此前"老 libmpv"认知有误。
2. **python-mpv 陷阱**：site-packages 版**无 `MPV.get_property()` 方法**——调用走 `__getattr__` 误读 "get-property" 属性→AttributeError(-8)，与具体属性名无关。读状态用**属性访问**（`p.time_pos`/`p.pause`）或 `observe_property`（均实测可用）；生产引擎只走这两条路，不受影响。
3. **命令选型**：裸 `frame-step`/`frame-back-step` 可用；`input-command` 元命令此构建 **-4 不可用**；`frame-end` 也不存在。
4. **VFR 注意**：源为 VFR（该 750MB 源平均 54.4fps，帧差不均）；重复 PTS 帧步进时 time-pos 不变属正常（落帧按帧计数精确）；**像素身份判定需内容可分辨性过滤**（该源邻帧 mad 差常 <0.001，平局时 argmin 不可信——探针E 教训，probe_guard 类探针须带 mad 分离度检查）。
5. **事件等待姿势**：python-mpv 无 `event_wait`；等 `file-loaded` 用 `prepare_and_wait_for_event("file-loaded")`（先注册回调再发命令，免竞态）。loadfile 大文件（750MB 冷缓存）事件可等 90s。

## 3. 未决事项

- **owner 手检**：GUI（`ARKNIGHT_PREVIEW_ENGINE=mpv python main.py`，验收时 pid 26024）——←/→ 瞬时步进 / 长按顺滑 / 点时间轴暂停仍 C4 / EDL 模式步进跳删除段。
- **选项C（拟立项）**：C4 链路挤水分（`_still_render_mpv` 固定 sleep(0.25)→事件等待、shot 轮询收紧、追帧事件驱动），点轴 ~0.8s→~0.3-0.5s，半天，低风险。
- **选项2（缓项，已具前置）**：全原生化（seek+看+补取代 C4）——探针E 已证 time-pos「看」有信号，但「跳最多差 2 帧」需黄金集全边界验证（1-2 天）；owner 拟先做 C4 优化后再定。
- **推送**：待 owner「推送」口令。

## 4. 会话口令史（授权链）

「开工」①+②G → 中途「停止」（当时无新改动）→ 探针D「要」→「继续」→ ②G 路线 owner 未选 → 按既定门槛判据（frame-step 精确则简化 ②G）实现选项1 → 「跑」探针E → 「验收」本批提交。
