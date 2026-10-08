#!/usr/bin/env python3
"""skim_cache 单元测试：命令形态、代数键、预算守卫、取消/超时。

不执行真实 ffmpeg：编码选择打桩，构建执行用假 Popen 验证看门狗语义。
命令形态断言对应 B0/T0 实证的生产形态：-ss/-t/-copyts 输入截断、
select 扁平 OR + tpad 尾克隆 + setts 哨兵、-frames:v kept+1、mpegts pipe:1。
"""
from __future__ import annotations

import tempfile
import threading
import time
import unittest
from fractions import Fraction
from pathlib import Path
from unittest import mock

import skim_cache
from skim_cache import (
    SkimBuildCancelled,
    SkimBuildError,
    SkimSpec,
    build_command,
    skim_key,
)
from timeline_plan import TimelinePlan


def _schedule():
    # 两段：帧 [10,20) tick [400,800)，帧 [30,38) tick [1200,1520)
    return [(10, 20, 400, 800), (30, 38, 1200, 1520)]


_TB = Fraction(1, 40)


class _FakeSleepPopen:
    """假 ffmpeg 进程：communicate 阻塞直到看门狗 kill（模拟长构建）。

    兜底 deadline 取 3x 并抛真实 TimeoutExpired 类型：看门狗失效时
    被测代码自己的 communicate(timeout=...) 分支也能接住，测试不悬挂。
    """

    def __init__(self, cmd, **_kwargs):
        self.cmd = cmd
        self.returncode = None
        self.killed = False

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9

    def communicate(self, timeout=None):
        import subprocess as _subprocess

        deadline = None if timeout is None else time.perf_counter() + timeout * 3
        while self.returncode is None:
            if deadline is not None and time.perf_counter() > deadline:
                raise _subprocess.TimeoutExpired(self.cmd, timeout)
            time.sleep(0.02)
        return b"", b""


class BuildCommandTests(unittest.TestCase):
    def _command(self, spec):
        with mock.patch.object(skim_cache.analyzer, "_pick_gpu_encoder",
                               return_value=None), \
             mock.patch.object(skim_cache.analyzer, "_video_encoder_args",
                               return_value=["-c:v", "libx264"]):
            return build_command("ffmpeg.exe", "src.mp4", _schedule(), _TB, spec)

    def test_command_shape_480p(self):
        cmd, kept, virtual_s = self._command(SkimSpec())
        self.assertEqual(kept, 18)
        self.assertAlmostEqual(virtual_s, 18.0)
        text = " ".join(cmd)
        # 输入侧：-ss(首段-1s) + -t(尾段余量截断) + -copyts，全部在 -i 前
        i_index = cmd.index("-i")
        self.assertIn("-ss", cmd[:i_index])
        self.assertIn("-t", cmd[:i_index])
        self.assertIn("-copyts", cmd[:i_index])
        self.assertEqual(cmd[cmd.index("-ss") + 1], "9.000000000")
        self.assertEqual(cmd[cmd.index("-t") + 1], "30.500000000")
        # 输出侧：帧数上限 kept+1、GOP/BF、时基、TS 管道
        self.assertEqual(cmd[cmd.index("-frames:v") + 1], "19")
        self.assertEqual(cmd[cmd.index("-g") + 1], "30")
        self.assertEqual(cmd[cmd.index("-bf") + 1], "0")
        self.assertEqual(cmd[cmd.index("-enc_time_base:v") + 1], "1/40")
        self.assertEqual(cmd[-4:], ["-an", "-f", "mpegts", "pipe:1"])
        # 滤镜图：扁平 between OR + setpts gap 减法 + tpad 克隆 + 480 缩放
        graph = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("between(pts,400,799)", graph)
        self.assertIn("between(pts,1200,1519)", graph)
        self.assertIn("tpad=stop_mode=clone:stop=1", graph)
        self.assertIn("scale=-2:480", graph)
        # 尾哨兵 setts bsf 在位
        self.assertIn("setts=pts=", text)

    def test_command_shape_source_resolution(self):
        cmd, _kept, _vs = self._command(SkimSpec(target_h=0))
        graph = cmd[cmd.index("-filter_complex") + 1]
        self.assertNotIn("scale=", graph)
        self.assertIn("[outv]", graph)

    def test_command_shape_graph_file(self):
        # G2：千段级表达式超 Windows 32K 命令行上限 → script 文件路径
        with mock.patch.object(skim_cache.analyzer, "_pick_gpu_encoder",
                               return_value=None), \
             mock.patch.object(skim_cache.analyzer, "_video_encoder_args",
                               return_value=["-c:v", "libx264"]):
            cmd, _kept, _vs = build_command(
                "ffmpeg.exe", "src.mp4", _schedule(), _TB, SkimSpec(),
                graph_file="G.txt")
        self.assertIn("-filter_complex_script", cmd)
        self.assertEqual(cmd[cmd.index("-filter_complex_script") + 1], "G.txt")
        self.assertNotIn("-filter_complex", cmd)

    def test_empty_schedule_rejected(self):
        with mock.patch.object(skim_cache.analyzer, "_pick_gpu_encoder",
                               return_value=None):
            with self.assertRaises(SkimBuildError) as ctx:
                build_command("ffmpeg.exe", "src.mp4", [], _TB, SkimSpec())
        self.assertEqual(ctx.exception.code, "EMPTY_SCHEDULE")


class SpecAndKeyTests(unittest.TestCase):
    def test_spec_rejects_gop_below_nvenc_floor(self):
        with self.assertRaises(ValueError):
            SkimSpec(gop=1)

    def test_key_head_matches_edl_cache_key_shape(self):
        key = skim_key("sha256x", "fpx", 3, 1, SkimSpec())
        self.assertEqual(key[:4], ("sha256x", "fpx", 3, 1))
        self.assertEqual(key[4:], (480, 30, 6))


class VerifyGuardTests(unittest.TestCase):
    def test_non_ts_output_rejected(self):
        with self.assertRaises(SkimBuildError) as ctx:
            skim_cache._verify_blob(bytearray(b"not-ts"), 10, 1.0, SkimSpec())
        self.assertEqual(ctx.exception.code, "NOT_MPEGTS")

    def test_budget_exceeded_rejected(self):
        blob = bytearray(b"\x47") + bytearray(2_100_000)
        with self.assertRaises(SkimBuildError) as ctx:
            skim_cache._verify_blob(blob, 10, 1.0,
                                    SkimSpec(max_blob_mb=2.0))
        self.assertEqual(ctx.exception.code, "BUDGET_EXCEEDED")


class BuildLifecycleTests(unittest.TestCase):
    def _fixture(self):
        from tests.test_preview_engine import _certified_media

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        media = _certified_media(
            root,
            [{"n": i, "pts": i * 40, "duration": 40} for i in range(8)],
        )
        plan = TimelinePlan.from_deleted_ranges(8, [(2, 4)])
        return media, plan

    def _patched_build(self, media, plan, **kwargs):
        stack = [
            mock.patch.object(skim_cache.analyzer, "_pick_gpu_encoder",
                              return_value=None),
            mock.patch.object(skim_cache.analyzer, "_video_encoder_args",
                              return_value=["-c:v", "libx264"]),
            mock.patch.object(skim_cache.subprocess, "Popen",
                              _FakeSleepPopen),
        ]
        for patcher in stack:
            patcher.start()
            self.addCleanup(patcher.stop)
        return skim_cache.build_skim_blob(media, plan, **kwargs)

    def test_cancel_event_kills_ffmpeg_fast(self):
        media, plan = self._fixture()
        cancel = threading.Event()
        cancel.set()
        t0 = time.perf_counter()
        with self.assertRaises(SkimBuildCancelled):
            self._patched_build(media, plan, cancel_event=cancel, timeout=60.0)
        self.assertLess(time.perf_counter() - t0, 5.0)

    def test_cancel_callable_supported(self):
        # G1：TaskContext 只暴露 cancelled 属性 → callable 也必须支持
        media, plan = self._fixture()
        t0 = time.perf_counter()
        with self.assertRaises(SkimBuildCancelled):
            self._patched_build(media, plan, cancel_event=lambda: True,
                                timeout=60.0)
        self.assertLess(time.perf_counter() - t0, 5.0)

    def test_timeout_kills_ffmpeg(self):
        media, plan = self._fixture()
        t0 = time.perf_counter()
        with self.assertRaises(SkimBuildError) as ctx:
            self._patched_build(media, plan, timeout=1.0)
        self.assertEqual(ctx.exception.code, "TIMEOUT")
        self.assertLess(time.perf_counter() - t0, 10.0)

    def test_uncertified_media_rejected(self):
        media, plan = self._fixture()
        bare = media_info_replace(media)
        with self.assertRaises(SkimBuildError) as ctx:
            skim_cache.build_skim_blob(bare, plan)
        self.assertEqual(ctx.exception.code, "CERTIFICATION_REQUIRED")

    def test_untrimmed_plan_rejected(self):
        media, _plan = self._fixture()
        whole = TimelinePlan.from_deleted_ranges(8, [])
        with self.assertRaises(SkimBuildError) as ctx:
            skim_cache.build_skim_blob(media, whole)
        self.assertEqual(ctx.exception.code, "NO_DELETED_RANGES")

    def test_source_changed_after_certification_rejected(self):
        # review 修复 F3 守卫：认证后源文件被替换（size/mtime 变化）→
        # stat 快照失配拒绝构建（fail-closed，不做全量 sha 重哈希）
        media, plan = self._fixture()
        Path(media.source_path).write_bytes(b"replaced-content")
        with self.assertRaises(SkimBuildError) as ctx:
            skim_cache.build_skim_blob(media, plan)
        self.assertEqual(ctx.exception.code, "SOURCE_CHANGED")


class AutoLadderTests(unittest.TestCase):
    def test_downshift_on_budget_then_success(self):
        calls = []

        def fake_build(media, plan, *, spec, **kwargs):
            calls.append(spec.target_h)
            if spec.target_h > 240:
                raise SkimBuildError("BUDGET_ESTIMATE_EXCEEDED", "too big")
            return "BLOB"

        with mock.patch.object(skim_cache, "build_skim_blob", fake_build):
            out = skim_cache.build_skim_blob_auto(None, None)
        self.assertEqual(out, "BLOB")
        self.assertEqual(calls, [480, 360, 240])

    def test_all_tiers_budget_exhausted_raises(self):
        def fake_build(media, plan, *, spec, **kwargs):
            raise SkimBuildError("BUDGET_EXCEEDED", "too big")

        with mock.patch.object(skim_cache, "build_skim_blob", fake_build):
            with self.assertRaises(SkimBuildError) as ctx:
                skim_cache.build_skim_blob_auto(None, None)
        self.assertEqual(ctx.exception.code, "BUDGET_EXCEEDED")

    def test_non_budget_error_not_retried(self):
        calls = []

        def fake_build(media, plan, *, spec, **kwargs):
            calls.append(spec.target_h)
            raise SkimBuildError("FFMPEG_FAILED", "boom")

        with mock.patch.object(skim_cache, "build_skim_blob", fake_build):
            with self.assertRaises(SkimBuildError):
                skim_cache.build_skim_blob_auto(None, None)
        self.assertEqual(calls, [480])

    def test_cancel_propagates_without_retry(self):
        calls = []

        def fake_build(media, plan, *, spec, **kwargs):
            calls.append(spec.target_h)
            raise SkimBuildCancelled()

        with mock.patch.object(skim_cache, "build_skim_blob", fake_build):
            with self.assertRaises(SkimBuildCancelled):
                skim_cache.build_skim_blob_auto(None, None)
        self.assertEqual(calls, [480])


def media_info_replace(media):
    """同数据但未认证的 MediaInfo（frame_pts_authoritative=False）。"""
    import dataclasses

    return dataclasses.replace(media, frame_pts_authoritative=False)


if __name__ == "__main__":
    unittest.main()
