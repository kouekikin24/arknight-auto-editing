# dsvc — 解码直通服务（Rust）

`dsvc.exe` 是预览直通档的解码服务：stdin 收 JSONL 命令（load/seek/stop/quit），
stdout 吐裸 I420 帧流（定长切分），stderr 吐 JSONL 事件（gen/loaded/span/eof/error）。

生产端客户端 = 仓库根 `dthrough_service.py`（协议、世代号、背压语义的权威实现）。

## 构建

```powershell
# 依赖：Rust 工具链 + ffmpeg 7.1 shared dev（FFMPEG_DIR 指向解压后的 shared 包）
cargo build --release
# 产物需放回生产查找路径：
#   .cache/v3ass/dsvc/target/release/dsvc.exe
```

ffmpeg shared dev 包获取：GyanD/codexffmpeg tag7.1.1（`ffmpeg-7.1.1-full_build-shared`）。
历史：bake-off 判定 `ffmpeg-the-third 3.0.2` 胜出（rsmpeg 0.18 实为 ffmpeg 8.0 专属）。
构建细节与踩坑见 ASSET_INVENTORY §六 1a 行。

## 版本

源码于 2026-10-08 从 `.cache/v3ass/dsvc/`（gitignore 域）迁入本目录，
内容与当时构建 `dsvc.exe` 的源码逐字节一致（sha256 见迁入提交）。

## 修订史

### 2026-10-10 代数竞态修复（load+seek 背靠背 → seek 饿死）

- **症状**（真机 T0）：末帧起播/播放中重定位时偶发「流从早先位置续推、
  永不报 eof、无 seek_ok/error」——GUI 表现为「红条钉末尾 + 按钮仍暂停 +
  FPS 仍亮」的持续假播放；`probe_pwp_repeat` 引擎级复现 2/6 超时。
- **根因**：reader 线程 `fetch_add(1)` 后才 `send(cmd)`，而 decode_loop
  `recv(cmd)` 后才读全局 `generation`。load+seek 背靠背入队（生产起播：
  play_dthrough 的 load 之后 mpv demuxer 立刻经 stream_cb seek_fn 发 seek）
  时，**load 出队后读到的是 seek 的代数**：load 以最新代运行、自身
  `generation != gen` 检查恒假 → 永不中断走全片；seek 在通道里饿死。
  `probe_dsvc_genrace` 服务级直证（无 seek_ok/eof、span 事件从低 si 递增）。
- **修复**：新增 `Queued { gen, cmd }`——代数在 reader **入队瞬间**用
  `fetch_add(1)+1` 绑定并随命令传递；decode_loop 不再读全局。背靠背命令
  各持其代，旧命令在新代下达时必被中断。
- **验证**：`probe_dsvc_genrace` 10/10 ok（修前 run 0 超时）；
  `probe_pwp_repeat` 0/6 超时、eof 恒 ~1.2s（修前 2/6 超时）；
  L2 门禁位级四项 `A_full_bytes_exact=True / C_discard_exact=True`
  全过（解码输出零变化）；单测 651 + L3 门禁全过。
