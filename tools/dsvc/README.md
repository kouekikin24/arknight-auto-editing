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
