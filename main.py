"""
AstrBot 插件：VTube Studio Live2D 控制
通过 LLM 工具函数让 AI 能够控制 VTube Studio 中的 Live2D 模型
"""

import asyncio
import copy
from collections import deque
import json
import math
import os
import platform
import re
import time
import uuid
from typing import Any, Optional
from urllib.parse import unquote, urlparse
import wave

from astrbot.api.star import Star, Context, register
from astrbot.api import llm_tool, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api import logger
from astrbot.api.message_components import Plain, Record
from astrbot.api.provider import ProviderRequest
from astrbot.core.agent.message import AssistantMessageSegment
from astrbot.core.astr_main_agent import MainAgentBuildConfig, build_main_agent
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.provider.entities import LLMResponse
from astrbot.core.star.star_handler import EventType, star_handlers_registry

from .vts_client import (
    VTSClient,
    VTSClientError,
    VTSConnectionError,
    VTSTimeoutError,
)
from .vts_discovery import auto_discover, get_install_info
from .bilibili_live import (
    BilibiliBlivedmClient,
    BilibiliLaplaceClient,
    BilibiliLiveClient,
    BilibiliOpenLiveClient,
    LiveDanmakuEvent,
    probe_bilibili_live_room,
)
from .subtitle_server import SubtitleServer

# 默认配置
DEFAULT_HOST = "localhost"
DEFAULT_PORT = 8001
KV_KEY_TOKEN = "vts_auth_token"
KV_KEY_BILI_REPLY_SESSION = "bili_live_reply_session"
L2D_TAG_PATTERN = re.compile(
    r"<l2d\s*:\s*([^<>]+?)\s*/?>|<l2d>\s*([^<>]+?)\s*</l2d>",
    re.IGNORECASE,
)


class SyntheticBiliLiveWakeEvent(AstrMessageEvent):
    def __init__(
        self,
        *,
        template_event: Optional[AstrMessageEvent],
        context: Context,
        session: MessageSession,
        message: str,
    ) -> None:
        message_obj = AstrBotMessage()
        message_obj.type = session.message_type
        message_obj.self_id = session.session_id
        message_obj.session_id = session.session_id
        message_obj.message_id = f"bili_live_auto_{uuid.uuid4().hex}"
        message_obj.sender = MessageMember(user_id=session.session_id, nickname="BiliLive")
        message_obj.message = [Plain(message)]
        message_obj.message_str = message
        message_obj.raw_message = message
        message_obj.timestamp = int(time.time())

        platform_meta = None
        if template_event:
            try:
                platform_meta = template_event.get_platform_metadata()
            except Exception:
                platform_meta = getattr(template_event, "platform_meta", None)
        if platform_meta is None:
            platform_meta = PlatformMetadata(
                name=session.platform_id,
                description="SyntheticBiliLiveWake",
                id=session.platform_id,
            )
        super().__init__(message, message_obj, platform_meta, session.session_id)
        self.session = session
        self.context_obj = context
        self.is_at_or_wake_command = True
        self.is_wake = True


@register(
    "astrbot_plugin_vtube_studio",
    "EterUltimate",
    "vtube_studio连接支持",
    "1.4.2",
    "https://github.com/EterUltimate/astrbot_plugin_vtube_studio",
)
class VTubeStudioPlugin(Star):
    """VTube Studio Live2D 控制插件"""

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}

        self._auto_discover: bool = self.config.get("auto_discover", True)
        self._manual_host: Optional[str] = self.config.get("vts_host") or None

        # 安全解析端口，防止非数字字符串导致 ValueError
        port_val = self.config.get("vts_port")
        self._manual_port: Optional[int] = self._safe_parse_port(port_val)

        self._auto_connect: bool = self.config.get("auto_connect", True)
        self._debug_mode: bool = self.config.get("debug_mode", False)
        self._bili_debug_mode: bool = bool(self.config.get("bili_live_debug_log", False))
        self._l2d_tasks: set[asyncio.Task] = set()
        self._mouth_sync_tasks: set[asyncio.Task] = set()
        self._bili_live_client: Optional[
            BilibiliBlivedmClient
            | BilibiliLaplaceClient
            | BilibiliLiveClient
            | BilibiliOpenLiveClient
        ] = None
        self._bili_live_task: Optional[asyncio.Task] = None
        cache_size = max(10, int(self.config.get("bili_live_cache_size", 80) or 80))
        self._bili_events: deque[LiveDanmakuEvent] = deque(maxlen=cache_size)
        self._bili_pending_reply_events: deque[LiveDanmakuEvent] = deque(maxlen=50)
        self._bili_auto_reply_task: Optional[asyncio.Task] = None
        self._bili_last_auto_reply_at = 0.0
        self._bili_reply_event_template: Optional[AstrMessageEvent] = None
        self._subtitle_server: Optional[SubtitleServer] = None
        self._warned_bili_blivedm_fallback = False

        self.vts = VTSClient(
            host=self._manual_host or DEFAULT_HOST,
            port=self._manual_port or DEFAULT_PORT,
            plugin_name="AstrBot VTS Plugin",
            plugin_developer="EterUltimate",
        )
        self._connected = False

    def _safe_parse_port(self, port_val) -> Optional[int]:
        """安全解析端口值，防止非数字字符串导致异常"""
        if port_val is None:
            return None
        try:
            return int(port_val)
        except (ValueError, TypeError):
            logger.warning(f"[VTS] 无效的端口配置值: {port_val}，将使用默认端口")
            return None

    # ------------------------------------------------------------------ #
    #  插件生命周期
    # ------------------------------------------------------------------ #

    async def initialize(self):
        """插件启动时：自动发现 VTS 位置，然后尝试认证连接"""
        try:
            host, port = await self._discover()
            self.vts.url = f"ws://{host}:{port}"
            # 使用公开方法重置连接，不直接操作私有属性
            await self.vts.reset_connection()

            if self._auto_connect:
                await self._try_connect()
            else:
                logger.info("[VTS] auto_connect 关闭，跳过自动连接")

            await self._start_subtitle_server_if_enabled()

            if self._is_bili_live_enabled() and self.config.get(
                "bili_live_auto_start", True
            ):
                bili_type = self._get_bili_live_type()
                room_id = self._get_config_room_id()
                if room_id or bili_type in {"laplace", "open_live"}:
                    await self._start_bili_live(room_id)
                else:
                    logger.warning("[B站直播] 已开启自动启动，但未配置房间号")
        except Exception as e:
            logger.error(f"[VTS] 初始化失败: {e}")

    async def terminate(self):
        """插件卸载/停用时：断开 VTS 连接，清理资源"""
        try:
            for task in list(self._l2d_tasks):
                task.cancel()
            self._l2d_tasks.clear()
            for task in list(self._mouth_sync_tasks):
                task.cancel()
            self._mouth_sync_tasks.clear()
            await self._stop_bili_live()
            await self._stop_subtitle_server()
            await self.vts.disconnect()
            logger.info("[VTS] 插件已卸载，VTS 连接已关闭")
        except Exception as e:
            logger.warning(f"[VTS] 卸载时断开连接失败: {e}")

    async def _discover(self) -> tuple:
        """确定要连接的 host:port"""
        if self._manual_host and self._manual_port:
            logger.info(f"[VTS] 使用手动配置：{self._manual_host}:{self._manual_port}")
            return self._manual_host, self._manual_port

        if self._auto_discover:
            logger.info(f"[VTS] 开启自动发现（平台: {platform.system()}）")

        host, port = await auto_discover(host=self._manual_host or DEFAULT_HOST)
        logger.info(f"[VTS] 自动发现结果：{host}:{port}")
        return host, port

    async def _try_connect(self):
        """尝试连接并使用已保存的 Token 认证"""
        try:
            saved_token = await self._load_token()
            if saved_token:
                ok = await self.vts.authenticate(saved_token)
                if ok:
                    self._connected = True
                    logger.info("[VTS] 使用已保存 Token 认证成功")
                    return
            logger.info("[VTS] 未找到有效 Token，请发送 /vts_auth 进行认证")
        except VTSConnectionError as e:
            logger.warning(f"[VTS] 连接失败: {e}")
        except VTSTimeoutError as e:
            logger.warning(f"[VTS] 连接超时: {e}")
        except Exception as e:
            logger.warning(f"[VTS] 自动连接失败（VTube Studio 可能未启动）: {e}")

    async def _check_and_reconnect(self) -> bool:
        """检查连接状态，必要时尝试重连"""
        if self.vts.is_connected:
            return True
        try:
            saved_token = await self._load_token()
            if saved_token:
                ok = await self.vts.authenticate(saved_token)
                if ok:
                    self._connected = True
                    return True
        except Exception:
            pass
        self._connected = False
        return False

    # ------------------------------------------------------------------ #
    #  打字机字幕 overlay
    # ------------------------------------------------------------------ #

    def _is_subtitle_enabled(self) -> bool:
        return bool(self.config.get("subtitle_enabled", False))

    def _get_subtitle_style(self) -> dict[str, Any]:
        return {
            "typing_speed_ms": max(
                1, self._safe_parse_int(self.config.get("subtitle_typing_speed_ms"), 45)
            ),
            "hold_seconds": max(
                0.0,
                self._safe_parse_float(self.config.get("subtitle_hold_seconds"), 4.0),
            ),
            "font_size": max(
                12, self._safe_parse_int(self.config.get("subtitle_font_size"), 42)
            ),
            "font_weight": self._safe_parse_int(
                self.config.get("subtitle_font_weight"), 700
            ),
            "text_color": str(self.config.get("subtitle_text_color") or "#ffffff"),
            "stroke_color": str(self.config.get("subtitle_stroke_color") or "#111111"),
            "stroke_size": max(
                0, self._safe_parse_int(self.config.get("subtitle_stroke_size"), 4)
            ),
            "cursor_color": str(
                self.config.get("subtitle_cursor_color")
                or self.config.get("subtitle_text_color")
                or "#ffffff"
            ),
            "show_cursor": bool(self.config.get("subtitle_show_cursor", True)),
            "fade_out": bool(self.config.get("subtitle_fade_out", True)),
            "position": str(self.config.get("subtitle_position") or "bottom"),
            "padding": max(
                0, self._safe_parse_int(self.config.get("subtitle_padding"), 48)
            ),
            "max_width": max(
                200, self._safe_parse_int(self.config.get("subtitle_max_width"), 1100)
            ),
        }

    async def _start_subtitle_server_if_enabled(self) -> None:
        if not self._is_subtitle_enabled():
            return
        host = str(self.config.get("subtitle_host") or "127.0.0.1")
        port = self._safe_parse_int(self.config.get("subtitle_port"), 18081)
        self._subtitle_server = SubtitleServer(host, port, self._get_subtitle_style())
        try:
            await self._subtitle_server.start()
        except Exception as e:
            logger.error(f"[字幕] 启动字幕 overlay 失败: {e}")
            self._subtitle_server = None

    async def _stop_subtitle_server(self) -> None:
        if self._subtitle_server:
            await self._subtitle_server.stop()
            self._subtitle_server = None

    def _clean_subtitle_text(self, text: str) -> str:
        cleaned = text or ""
        if self.config.get("subtitle_strip_l2d_tags", True):
            _tags, cleaned = self._parse_l2d_tags(cleaned)
        if self.config.get("subtitle_strip_tts_blocks", True):
            cleaned = re.sub(r"(?is)<tts>.*?</tts>", "", cleaned)
        if self.config.get("subtitle_strip_html_tags", True):
            cleaned = re.sub(r"<[^>\n]{1,80}>", "", cleaned)
        cleaned = re.sub(r"\[CQ:[^\]]+\]", "", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
        max_length = self._safe_parse_int(self.config.get("subtitle_max_length"), 120)
        if max_length > 0 and len(cleaned) > max_length:
            cleaned = cleaned[:max_length].rstrip() + "..."
        return cleaned

    async def _push_subtitle(self, text: str) -> None:
        if not self._is_subtitle_enabled():
            return
        if not self._subtitle_server:
            await self._start_subtitle_server_if_enabled()
        if not self._subtitle_server:
            return
        cleaned = self._clean_subtitle_text(text)
        if cleaned:
            self._subtitle_server.style = self._get_subtitle_style()
            await self._subtitle_server.show(cleaned)

    # ------------------------------------------------------------------ #
    #  TTS 语音嘴型联动
    # ------------------------------------------------------------------ #

    def _is_mouth_sync_enabled(self) -> bool:
        return bool(self.config.get("mouth_sync_enabled", False))

    def _create_mouth_sync_task(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._mouth_sync_tasks.add(task)
        task.add_done_callback(self._mouth_sync_tasks.discard)

    def _extract_record_audio_paths(self, result) -> list[str]:
        chain = getattr(result, "chain", None)
        if not chain:
            return []

        paths: list[str] = []
        for comp in chain:
            if not isinstance(comp, Record):
                continue
            for attr in ("file", "path", "url"):
                value = getattr(comp, attr, None)
                path = self._normalize_local_audio_path(value)
                if path and path not in paths:
                    paths.append(path)
        return paths

    def _normalize_local_audio_path(self, value) -> str:
        if not value:
            return ""
        text = str(value).strip()
        if not text:
            return ""
        parsed = urlparse(text)
        if parsed.scheme in {"http", "https"}:
            return ""
        if parsed.scheme == "file":
            text = unquote(parsed.path)
            if os.name == "nt" and re.match(r"^/[A-Za-z]:/", text):
                text = text[1:]
        return text if os.path.exists(text) else ""

    async def _start_mouth_sync_for_result(self, result) -> None:
        if not self._is_mouth_sync_enabled():
            return

        audio_paths = self._extract_record_audio_paths(result)
        if not audio_paths:
            logger.debug("[嘴型] 未找到可读取的本地语音文件，跳过嘴型联动")
            return
        if not await self._check_and_reconnect():
            logger.debug("[嘴型] VTS 未连接，跳过语音嘴型联动")
            return

        for audio_path in audio_paths[:1]:
            self._create_mouth_sync_task(self._run_mouth_sync(audio_path))

    async def _run_mouth_sync(self, audio_path: str) -> None:
        try:
            envelope, interval = await asyncio.to_thread(
                self._build_mouth_sync_envelope, audio_path
            )
            if not envelope:
                logger.debug(f"[嘴型] 暂不支持或无法读取音频文件: {audio_path}")
                return
            await self._drive_mouth_parameters(envelope, interval)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[嘴型] 语音嘴型联动失败: {e}")
        finally:
            await self._reset_mouth_parameters()

    async def _run_mouth_sync_envelope(self, envelope: list[float], interval: float) -> None:
        try:
            await self._drive_mouth_parameters(envelope, interval)
        finally:
            await self._reset_mouth_parameters()

    def _build_mouth_sync_envelope(self, audio_path: str) -> tuple[list[float], float]:
        fps = max(5, min(60, self._safe_parse_int(self.config.get("mouth_sync_fps"), 30)))
        gain = max(
            0.1, self._safe_parse_float(self.config.get("mouth_sync_gain"), 1.6)
        )
        noise_gate = max(
            0.0, self._safe_parse_float(self.config.get("mouth_sync_noise_gate"), 0.03)
        )
        with wave.open(audio_path, "rb") as wav:
            channels = max(1, wav.getnchannels())
            sample_width = wav.getsampwidth()
            rate = max(1, wav.getframerate())
            frames_per_step = max(1, int(rate / fps))
            max_amplitude = float((1 << (sample_width * 8 - 1)) - 1)
            values: list[float] = []

            while True:
                frame_bytes = wav.readframes(frames_per_step)
                if not frame_bytes:
                    break
                rms = self._pcm_rms(frame_bytes, sample_width, channels)
                value = min(1.0, max(0.0, (rms / max_amplitude) * gain))
                if value < noise_gate:
                    value = 0.0
                values.append(value)

        return values, 1.0 / fps

    def _pcm_rms(self, data: bytes, sample_width: int, channels: int) -> float:
        if sample_width not in {1, 2, 3, 4} or not data:
            return 0.0
        frame_width = sample_width * channels
        if frame_width <= 0:
            return 0.0

        total = 0.0
        count = 0
        for offset in range(0, len(data) - frame_width + 1, frame_width):
            channel_total = 0.0
            for channel in range(channels):
                start = offset + channel * sample_width
                sample = data[start : start + sample_width]
                if sample_width == 1:
                    value = sample[0] - 128
                else:
                    value = int.from_bytes(sample, "little", signed=True)
                channel_total += value
            mono = channel_total / channels
            total += mono * mono
            count += 1
        return math.sqrt(total / count) if count else 0.0

    async def _drive_mouth_parameters(self, envelope: list[float], interval: float) -> None:
        open_param = str(
            self.config.get("mouth_sync_open_parameter") or "ParamMouthOpenY"
        ).strip()
        form_param = str(self.config.get("mouth_sync_form_parameter") or "").strip()
        mode = str(self.config.get("mouth_sync_mode") or "set").strip() or "set"
        smoothing = min(
            0.95,
            max(0.0, self._safe_parse_float(self.config.get("mouth_sync_smoothing"), 0.45)),
        )
        form_strength = max(
            0.0,
            self._safe_parse_float(self.config.get("mouth_sync_form_strength"), 0.18),
        )

        smoothed = 0.0
        for index, value in enumerate(envelope):
            if not await self._check_and_reconnect():
                return
            smoothed = smoothed * smoothing + value * (1.0 - smoothing)
            parameters = [{"id": open_param, "value": smoothed}]
            if form_param and form_strength > 0:
                form_value = math.sin(index * 0.75) * form_strength * min(1.0, smoothed * 1.4)
                parameters.append({"id": form_param, "value": form_value})
            await self.vts.inject_parameters(parameters=parameters, mode=mode)
            await asyncio.sleep(interval)

    async def _reset_mouth_parameters(self) -> None:
        if not self._is_mouth_sync_enabled():
            return
        if not await self._check_and_reconnect():
            return
        open_param = str(
            self.config.get("mouth_sync_open_parameter") or "ParamMouthOpenY"
        ).strip()
        form_param = str(self.config.get("mouth_sync_form_parameter") or "").strip()
        parameters = [{"id": open_param, "value": 0.0}]
        if form_param:
            parameters.append({"id": form_param, "value": 0.0})
        try:
            await self.vts.inject_parameters(
                parameters=parameters,
                mode=str(self.config.get("mouth_sync_mode") or "set").strip() or "set",
            )
        except Exception as e:
            logger.debug(f"[嘴型] 重置嘴型参数失败: {e}")

    def _extract_subtitle_text_from_result(self, result) -> str:
        chain = getattr(result, "chain", None)
        if not chain:
            return ""

        has_voice = any(isinstance(comp, Record) for comp in chain)
        plain_parts: list[str] = []
        seen_voice = False

        for comp in chain:
            if isinstance(comp, Record):
                seen_voice = True
                continue
            if not isinstance(comp, Plain):
                continue
            text = (comp.text or "").strip()
            if not text:
                continue
            if has_voice and self.config.get("subtitle_voice_use_following_plain", True):
                if seen_voice:
                    plain_parts.append(text)
            else:
                plain_parts.append(text)

        if has_voice and self.config.get("subtitle_voice_use_following_plain", True) and not plain_parts:
            for comp in chain:
                if isinstance(comp, Plain) and (comp.text or "").strip():
                    text = comp.text.strip()
                    if not re.search(r"[\u3040-\u30ff]", text):
                        plain_parts.append(text)

        text = "\n".join(plain_parts).strip()
        return self._prefer_subtitle_display_text(text, voice_context=has_voice)

    def _prefer_subtitle_display_text(self, text: str, voice_context: bool = False) -> str:
        if not text:
            return ""
        cleaned = re.sub(r"(?is)<tts>.*?</tts>", "", text).strip()
        cleaned = re.sub(r"\[[^\]\n]{1,40}\]", "", cleaned).strip()
        cleaned = re.sub(r"^[\s.。…!！?？,，、~～-]+", "", cleaned)

        prefer_chinese = voice_context or bool(
            self.config.get("subtitle_prefer_chinese_text", True)
        )
        if not prefer_chinese:
            return cleaned

        has_kana = bool(re.search(r"[\u3040-\u30ff]", cleaned))
        if has_kana and re.search(r"[\u4e00-\u9fff]", cleaned):
            kana_matches = list(re.finditer(r"[\u3040-\u30ff]", cleaned))
            tail = cleaned[kana_matches[-1].end():]
            tail = re.sub(r"^[\s.。…!！?？,，、~～-]+", "", tail)
            first_tail_chinese = re.search(r"[\u4e00-\u9fff]", tail)
            if first_tail_chinese:
                return tail[first_tail_chinese.start():].strip()

        lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
        chinese_lines = [
            line for line in lines
            if re.search(r"[\u4e00-\u9fff]", line)
        ]
        if chinese_lines:
            best_lines = []
            for line in chinese_lines:
                first_chinese = re.search(r"[\u4e00-\u9fff]", line)
                if not first_chinese:
                    continue
                best_lines.append(line[first_chinese.start():].strip())
            if best_lines:
                return "\n".join(best_lines)

        first_chinese = re.search(r"[\u4e00-\u9fff]", cleaned)
        if first_chinese:
            return cleaned[first_chinese.start():].strip()

        if voice_context and re.search(r"[\u3040-\u30ff]", cleaned):
            return ""
        return cleaned

    @filter.command("subtitle_status")
    async def cmd_subtitle_status(self, event: AstrMessageEvent):
        """查看字幕 overlay 状态。"""
        enabled = self._is_subtitle_enabled()
        running = self._subtitle_server is not None
        url = self._subtitle_server.url if self._subtitle_server else (
            f"http://{self.config.get('subtitle_host') or '127.0.0.1'}:"
            f"{self._safe_parse_int(self.config.get('subtitle_port'), 18081)}/"
        )
        yield event.plain_result(
            f"字幕功能：{'已启用' if enabled else '未启用'}\n"
            f"字幕服务：{'运行中' if running else '未运行'}\n"
            f"Overlay 地址：{url}"
        )

    @filter.command("subtitle_test")
    async def cmd_subtitle_test(self, event: AstrMessageEvent, text: str = ""):
        """测试打字机字幕。"""
        if not self._is_subtitle_enabled():
            yield event.plain_result("字幕功能未启用，请先在插件配置中开启 subtitle_enabled。")
            return
        await self._push_subtitle(text or "这是一条打字机字幕测试。")
        yield event.plain_result("已发送字幕测试。")

    @filter.command("subtitle_clear")
    async def cmd_subtitle_clear(self, event: AstrMessageEvent):
        """清空字幕 overlay。"""
        if self._subtitle_server:
            await self._subtitle_server.clear()
        yield event.plain_result("已清空字幕。")

    @filter.command("mouth_sync_test")
    async def cmd_mouth_sync_test(self, event: AstrMessageEvent, duration: float = 2.0):
        """测试 VTS 嘴部开闭参数联动。"""
        if not self._is_mouth_sync_enabled():
            yield event.plain_result("嘴型联动未启用，请先在插件配置中开启 mouth_sync_enabled。")
            return
        if not await self._check_and_reconnect():
            yield event.plain_result("VTube Studio 未连接，无法测试嘴型联动。")
            return

        duration = max(0.5, min(10.0, self._safe_parse_float(duration, 2.0)))
        fps = max(5, min(60, self._safe_parse_int(self.config.get("mouth_sync_fps"), 30)))
        steps = max(1, int(duration * fps))
        envelope = [
            max(0.0, math.sin(index * 0.48))
            * (0.35 + 0.45 * math.sin(index * 0.13) ** 2)
            for index in range(steps)
        ]
        self._create_mouth_sync_task(
            self._run_mouth_sync_envelope(envelope, 1.0 / fps)
        )
        yield event.plain_result(f"已启动 {duration:g} 秒嘴型联动测试。")

    # ------------------------------------------------------------------ #
    #  B站直播弹幕读取
    # ------------------------------------------------------------------ #

    def _safe_parse_int(self, value, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _safe_parse_float(self, value, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _is_bili_live_enabled(self) -> bool:
        return bool(self.config.get("bilibili_enabled", False))

    def _get_config_room_id(self) -> int:
        return self._safe_parse_int(
            self.config.get("bilibili_room_id")
            or self.config.get("bili_live_room_id"),
            0,
        )

    def _get_bili_live_type(self) -> str:
        return str(
            self.config.get("bilibili_type")
            or self.config.get("bili_live_type")
            or "web"
        ).strip()

    def _get_bili_sessdata(self) -> str:
        return str(
            self.config.get("bilibili_sessdata")
            or self.config.get("bili_live_sessdata")
            or ""
        ).strip()

    def _get_bili_web_backend(self) -> str:
        configured = str(
            self.config.get("bilibili_web_backend") or "builtin"
        ).strip().lower()
        if configured == "blivedm":
            if not self._warned_bili_blivedm_fallback:
                self._warned_bili_blivedm_fallback = True
                logger.warning(
                    "[B站直播] blivedm 后端在当前环境中可能无法收到事件，已自动切换到 builtin 后端。"
                )
            return "builtin"
        return configured

    def _get_bili_open_live_config(self) -> dict[str, Any]:
        return {
            "access_key_id": str(
                self.config.get("bilibili_ACCESS_KEY_ID") or ""
            ).strip(),
            "access_key_secret": str(
                self.config.get("bilibili_ACCESS_KEY_SECRET") or ""
            ).strip(),
            "app_id": self._safe_parse_int(self.config.get("bilibili_APP_ID"), 0),
            "room_owner_auth_code": str(
                self.config.get("bilibili_ROOM_OWNER_AUTH_CODE") or ""
            ).strip(),
        }

    def _get_laplace_config(self) -> dict[str, Any]:
        bridge_url = str(
            self.config.get("laplace_event_bridge_url")
            or self.config.get("bili_live_laplace_url")
            or ""
        ).strip()
        if not bridge_url:
            host = str(self.config.get("laplace_event_bridge_host") or "localhost").strip()
            port = self._safe_parse_int(
                self.config.get("laplace_event_bridge_port"), 9696
            )
            bridge_url = f"ws://{host}:{port}"
        return {
            "bridge_url": bridge_url,
            "token": str(
                self.config.get("laplace_event_bridge_token")
                or self.config.get("bili_live_laplace_token")
                or ""
            ).strip(),
        }

    async def _start_bili_live(self, room_id: int) -> str:
        if not self._is_bili_live_enabled():
            return "B站直播功能未启用，请先在插件配置中开启 bilibili_enabled。"

        if self._bili_live_task and not self._bili_live_task.done():
            return "B站直播弹幕监听已在运行。"

        bili_type = self._get_bili_live_type()
        if bili_type == "laplace":
            laplace_cfg = self._get_laplace_config()
            self._bili_live_client = BilibiliLaplaceClient(
                bridge_url=laplace_cfg["bridge_url"],
                room_id=room_id,
                token=laplace_cfg["token"],
                on_event=self._on_bili_live_event,
                debug_log=self._bili_debug_mode,
            )
        elif bili_type == "web":
            sessdata = self._get_bili_sessdata()
            web_backend = self._get_bili_web_backend()
            if web_backend == "laplace":
                laplace_cfg = self._get_laplace_config()
                self._bili_live_client = BilibiliLaplaceClient(
                    bridge_url=laplace_cfg["bridge_url"],
                    room_id=room_id,
                    token=laplace_cfg["token"],
                    on_event=self._on_bili_live_event,
                    debug_log=self._bili_debug_mode,
                )
            elif web_backend == "builtin":
                self._bili_live_client = BilibiliLiveClient(
                    room_id=room_id,
                    sessdata=sessdata,
                    on_event=self._on_bili_live_event,
                    debug_log=self._bili_debug_mode,
                    history_poll_interval=self._safe_parse_float(
                        self.config.get("bili_live_history_poll_interval"), 3.0
                    ),
                )
            else:
                self._bili_live_client = BilibiliBlivedmClient(
                    room_id=room_id,
                    sessdata=sessdata,
                    on_event=self._on_bili_live_event,
                    debug_log=self._bili_debug_mode,
                )
        elif bili_type == "open_live":
            open_cfg = self._get_bili_open_live_config()
            missing = [
                key
                for key, value in open_cfg.items()
                if not value
            ]
            if missing:
                return (
                    "B站开放平台配置不完整，请填写："
                    + ", ".join(missing)
                )
            self._bili_live_client = BilibiliOpenLiveClient(
                access_key_id=open_cfg["access_key_id"],
                access_key_secret=open_cfg["access_key_secret"],
                app_id=open_cfg["app_id"],
                room_owner_auth_code=open_cfg["room_owner_auth_code"],
                on_event=self._on_bili_live_event,
            )
        else:
            return f"不支持的 B站直播监听类型: {bili_type}"

        self._bili_live_task = asyncio.create_task(self._bili_live_client.run_forever())
        self._bili_live_task.add_done_callback(self._on_bili_live_task_done)
        backend_text = (
            f"/{self._get_bili_web_backend()}" if bili_type == "web" else ""
        )
        logger.info(f"[B站直播] 已启动 {bili_type}{backend_text} 弹幕监听")
        room_text = f"，房间号：{room_id}" if bili_type == "web" else ""
        return f"已启动 B站直播弹幕监听（{bili_type}{backend_text}）{room_text}"

    async def _stop_bili_live(self) -> str:
        if self._bili_live_client:
            await self._bili_live_client.stop()
            self._bili_live_client = None

        if self._bili_live_task:
            if not self._bili_live_task.done():
                self._bili_live_task.cancel()
                try:
                    await self._bili_live_task
                except asyncio.CancelledError:
                    pass
            else:
                try:
                    self._bili_live_task.exception()
                except BaseException:
                    pass
            self._bili_live_task = None

        return "已停止 B站直播弹幕监听。"

    def _on_bili_live_task_done(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.warning(f"[B站直播] 弹幕监听任务结束: {exc}")

    async def _on_bili_live_event(self, event: LiveDanmakuEvent) -> None:
        self._bili_events.append(event)
        if self._should_collect_for_auto_reply(event):
            self._bili_pending_reply_events.append(event)
            self._schedule_bili_auto_reply()
        if self.config.get("bili_live_log_events", True):
            logger.info(
                f"[B站直播] 捕获事件 room={self._get_current_bili_room_text()} "
                f"type={event.event_type} {event.display_text()}"
            )
        elif self._debug_mode or self._bili_debug_mode:
            logger.debug(f"[B站直播] {event.event_type}: {event.display_text()}")

    def _get_current_bili_room_text(self) -> str:
        if not self._bili_live_client:
            return str(self._get_config_room_id() or "未知")
        room_id = getattr(self._bili_live_client, "real_room_id", None)
        if room_id:
            return str(room_id)
        return str(self._get_config_room_id() or "未知")

    def _should_collect_for_auto_reply(self, event: LiveDanmakuEvent) -> bool:
        if not self.config.get("bili_live_auto_reply_enabled", False):
            return False
        event_types = self.config.get("bili_live_auto_reply_event_types", ["danmaku"])
        if not isinstance(event_types, list):
            event_types = ["danmaku"]
        return event.event_type in {str(item).strip() for item in event_types}

    def _schedule_bili_auto_reply(self) -> None:
        if self._bili_auto_reply_task and not self._bili_auto_reply_task.done():
            return
        self._bili_auto_reply_task = asyncio.create_task(self._bili_auto_reply_worker())

    async def _bili_auto_reply_worker(self) -> None:
        try:
            cooldown = max(
                1.0,
                self._safe_parse_float(
                    self.config.get("bili_live_auto_reply_cooldown_seconds"), 12.0
                ),
            )
            elapsed = time.time() - self._bili_last_auto_reply_at
            if elapsed < cooldown:
                await asyncio.sleep(cooldown - elapsed)

            min_events = max(
                1,
                self._safe_parse_int(
                    self.config.get("bili_live_auto_reply_min_events"), 1
                ),
            )
            if len(self._bili_pending_reply_events) < min_events:
                return

            events = list(self._bili_pending_reply_events)
            self._bili_pending_reply_events.clear()
            await self._reply_to_bili_live_events(events)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[B站直播] 自动回应弹幕失败: {e}")

    async def _get_bili_reply_session(self) -> str:
        configured = str(self.config.get("bili_live_auto_reply_session_id") or "").strip()
        if configured:
            return configured
        return await self.get_kv_data(KV_KEY_BILI_REPLY_SESSION, "")

    async def _reply_to_bili_live_events(self, events: list[LiveDanmakuEvent]) -> None:
        session_id = await self._get_bili_reply_session()
        if not session_id:
            logger.warning(
                "[B站直播] 已收到弹幕，但未绑定自动回应会话。请在目标聊天发送 /bili_live_bind_here。"
            )
            return

        reply_mode = str(
            self.config.get("bili_live_auto_reply_mode") or "native"
        ).strip()
        if reply_mode == "native":
            if await self._reply_to_bili_live_events_via_framework(events, session_id):
                return
            logger.warning("[B站直播] 框架式原生自动回应失败，回退到事件队列投递。")
            await self._dispatch_bili_live_native_event(events, session_id)
            return

        provider = None
        try:
            provider = self.context.get_using_provider(session_id)
        except Exception:
            try:
                provider = self.context.get_using_provider()
            except Exception:
                provider = None
        if not provider:
            logger.warning("[B站直播] 自动回应弹幕失败：未找到可用 LLM Provider")
            return

        max_events = max(
            1,
            self._safe_parse_int(self.config.get("bili_live_auto_reply_max_events"), 5),
        )
        selected = events[-max_events:]
        formatted = self._format_bili_events(selected)
        if not formatted:
            return

        system_prompt = str(
            self.config.get("bili_live_auto_reply_system_prompt")
            or "你是正在直播中的虚拟主播助手。请根据观众最近的弹幕自然回应，语气像实时聊天，不要逐条复读。"
        )
        prompt = (
            "请根据以下 B站直播间最新互动生成一句自然回复。\n"
            "要求：中文；像主播现场回应；不要说自己看不到弹幕；不要列清单；"
            "优先回应具体问题或反馈；控制在 15 到 60 个字；"
            "只输出要发给直播间观众的话，不要描述发送状态、处理过程或自己的回应策略。\n\n"
            f"{formatted}"
        )
        prompt += self._build_bili_support_reply_hint(selected)
        response = await provider.text_chat(
            prompt=prompt,
            system_prompt=system_prompt,
            session_id=f"{session_id}:bili_live_auto_reply",
            persist=False,
        )
        reply_text = self._extract_provider_text(response)
        reply_text = self._clean_auto_reply_text(reply_text)
        if not reply_text:
            return

        await self.context.send_message(session_id, MessageChain([Plain(reply_text)]))
        self._bili_last_auto_reply_at = time.time()
        await self._push_subtitle(reply_text)
        logger.info(f"[B站直播] 已自动回应弹幕 -> {session_id}: {reply_text}")

    async def _dispatch_bili_live_native_event(
        self, events: list[LiveDanmakuEvent], session_id: str
    ) -> None:
        if not self._bili_reply_event_template:
            logger.warning(
                "[B站直播] 自动回应设置为原生路径，但当前进程没有绑定事件模板。"
                "请在目标聊天重新发送 /bili_live_bind_here。"
            )
            return

        max_events = max(
            1,
            self._safe_parse_int(self.config.get("bili_live_auto_reply_max_events"), 5),
        )
        formatted = self._format_bili_events(events[-max_events:])
        if not formatted:
            return

        prompt = (
            "【B站直播间弹幕事件】\n"
            "请像正常收到这条消息一样，按照你当前的人格、记忆、世界书和所有 AstrBot 插件规则回应直播间观众。\n"
            "要求：自然回应，不要逐条复读；优先回应具体问题或反馈；不要说自己看不到弹幕；"
            "只输出要发给直播间观众的话，不要描述发送状态、处理过程或自己的回应策略。\n\n"
            f"{formatted}"
        )
        prompt += self._build_bili_support_reply_hint(events[-max_events:])
        if self.config.get("bili_live_auto_reply_force_full_tts", True):
            prompt += (
                "\n\n本次回复需要包含语音消息。"
            )

        try:
            evt = copy.copy(self._bili_reply_event_template)
            evt.message_obj = copy.copy(self._bili_reply_event_template.message_obj)
            evt._extras = dict(self._bili_reply_event_template.get_extra())
            evt.clear_result()
            evt.message_obj.message = [Plain(prompt)]
            evt.message_obj.message_str = prompt
            evt.message_str = prompt
            evt.is_at_or_wake_command = True
            evt.should_call_llm(True)
            evt.set_extra("bili_live_auto_reply", True)
            evt.set_extra("bili_live_events", [event.raw for event in events[-max_events:]])
            self.context.get_event_queue().put_nowait(evt)
            self._bili_last_auto_reply_at = time.time()
            logger.info(
                f"[B站直播] 已投递原生自动回应事件 -> {session_id}: {len(events[-max_events:])} 条事件"
            )
        except Exception as e:
            logger.warning(f"[B站直播] 投递原生自动回应事件失败: {e}")

    async def _reply_to_bili_live_events_via_framework(
        self, events: list[LiveDanmakuEvent], session_id: str
    ) -> bool:
        max_events = max(
            1,
            self._safe_parse_int(self.config.get("bili_live_auto_reply_max_events"), 5),
        )
        formatted = self._format_bili_events(events[-max_events:])
        if not formatted:
            return False

        try:
            session = MessageSession.from_str(session_id)
        except Exception as e:
            logger.warning(f"[B站直播] 无法解析自动回应会话: {session_id} err={e}")
            return False

        try:
            curr_cid = await self.context.conversation_manager.get_curr_conversation_id(session_id)
            if not curr_cid:
                logger.warning(f"[B站直播] 自动回应会话没有活动对话: {session_id}")
                return False
            conv = await self.context.conversation_manager.get_conversation(session_id, curr_cid)
            if not conv:
                logger.warning(f"[B站直播] 自动回应会话无法读取对话: {session_id}")
                return False
        except Exception as e:
            logger.warning(f"[B站直播] 读取自动回应会话对话失败: {e}")
            return False

        prompt = (
            "【B站直播间弹幕事件】\n"
            "请像正常收到这条消息一样，按照你当前的人格、记忆、世界书和所有 AstrBot 插件规则回应直播间观众。\n"
            "要求：自然回应，不要逐条复读；优先回应具体问题或反馈；不要说自己看不到弹幕；"
            "只输出要发给直播间观众的话，不要描述发送状态、处理过程或自己的回应策略。\n\n"
            f"{formatted}"
        )
        prompt += self._build_bili_support_reply_hint(events[-max_events:])
        if self.config.get("bili_live_auto_reply_force_full_tts", True):
            prompt += "\n\n本次回复需要包含语音消息。"

        try:
            synthetic_event = SyntheticBiliLiveWakeEvent(
                template_event=self._bili_reply_event_template,
                context=self.context,
                session=session,
                message="bili_live_auto_reply_wakeup",
            )
            synthetic_event.set_extra("bili_live_auto_reply", True)
            synthetic_event.set_extra(
                "bili_live_events", [event.raw for event in events[-max_events:]]
            )
            cfg = self.context.get_config(umo=session_id)
            provider_settings = cfg.get("provider_settings", {}) if isinstance(cfg, dict) else {}
            build_cfg = MainAgentBuildConfig(
                tool_call_timeout=int(provider_settings.get("tool_call_timeout", 120) or 120),
                llm_safety_mode=False,
                streaming_response=False,
            )
            req = ProviderRequest(
                prompt=prompt,
                conversation=conv,
                session_id=session_id,
            )
            result = await build_main_agent(
                event=synthetic_event,
                plugin_context=self.context,
                config=build_cfg,
                req=req,
            )
            if not result:
                return False
            runner = result.agent_runner
            async for _ in runner.step_until_done(20):
                pass
            llm_resp = runner.get_final_llm_resp()
            if not llm_resp or llm_resp.role != "assistant":
                return False
            reply_text = self._clean_auto_reply_text(llm_resp.completion_text or "")
            if not reply_text:
                return False
            chain = await self._decorate_bili_live_reply_chain(
                session_id,
                [Plain(reply_text)],
                force_voice=bool(self.config.get("bili_live_auto_reply_force_full_tts", True)),
            )
            chain = self._strip_tts_blocks_from_plain_chain(chain)
            await self.context.send_message(session_id, MessageChain(chain))
            self._bili_last_auto_reply_at = time.time()
            logger.info(f"[B站直播] 已通过完整框架链路自动回应弹幕 -> {session_id}: {reply_text}")
            return True
        except Exception as e:
            logger.warning(f"[B站直播] 框架式原生自动回应失败: {e}")
            return False

    async def _decorate_bili_live_reply_chain(
        self, session_id: str, chain: list[Any], force_voice: bool = False
    ) -> list[Any]:
        if not chain:
            return chain
        try:
            session = MessageSession.from_str(session_id)
            message_obj = AstrBotMessage()
            message_obj.type = session.message_type
            message_obj.self_id = session.session_id
            message_obj.session_id = session.session_id
            message_obj.message_id = f"bili_live_reply_{uuid.uuid4().hex}"
            message_obj.sender = MessageMember(user_id=session.session_id)
            message_obj.message = chain
            message_obj.message_str = ""
            message_obj.raw_message = None
            message_obj.timestamp = int(time.time())
            platform_meta = None
            if self._bili_reply_event_template:
                try:
                    platform_meta = self._bili_reply_event_template.get_platform_metadata()
                except Exception:
                    platform_meta = None
            if platform_meta is None:
                platform_meta = PlatformMetadata(
                    name=session.platform_id,
                    description="SyntheticBiliLiveReply",
                    id=session.platform_id,
            )
            event = AstrMessageEvent("", message_obj, platform_meta, message_obj.session_id)
            event.set_result(self._build_message_result_from_chain(chain))
        except Exception as e:
            logger.debug(f"[B站直播] 构造自动回应装饰事件失败，跳过 hooks: {e}")
            return chain

        try:
            handlers = star_handlers_registry.get_handlers_by_event_type(
                EventType.OnDecoratingResultEvent
            )
        except Exception as e:
            logger.debug(f"[B站直播] 获取装饰 hooks 失败: {e}")
            return chain
        if force_voice:
            self._mark_tts_modify_forced_voice(event, handlers)
        for handler in handlers:
            try:
                await handler.handler(event)
            except Exception as e:
                logger.warning(
                    "[B站直播] 自动回应装饰 hook 失败: %s: %s",
                    getattr(handler, "handler_full_name", "unknown"),
                    e,
                )
        result = event.get_result()
        processed = getattr(result, "chain", None) if result is not None else None
        return list(processed or chain)

    def _mark_tts_modify_forced_voice(self, event: AstrMessageEvent, handlers: list[Any]) -> None:
        for handler in handlers:
            owner = getattr(getattr(handler, "handler", None), "__self__", None)
            if owner is None:
                continue
            mark_llm = getattr(owner, "_mark_pending_llm_response_event", None)
            mark_voice = getattr(owner, "_mark_pending_forced_voice_event", None)
            if not callable(mark_llm) or not callable(mark_voice):
                continue
            try:
                mark_llm(event)
                mark_voice(event)
                logger.debug("[B站直播] 已为自动回应标记 TTS 强制语音。")
                return
            except Exception as e:
                logger.debug(f"[B站直播] 标记 TTS 强制语音失败: {e}")
                return

    def _build_message_result_from_chain(self, chain: list[Any]) -> Any:
        try:
            from astrbot.api.event import MessageEventResult
        except ImportError:
            from astrbot.core.message.message_event_result import MessageEventResult
        try:
            result = MessageEventResult(chain=chain)
        except TypeError:
            result = MessageEventResult().chain_result(chain)
        if hasattr(result, "use_t2i"):
            try:
                result = result.use_t2i(False)
            except Exception:
                pass
        elif hasattr(result, "use_t2i_"):
            try:
                result.use_t2i_ = False
            except Exception:
                pass
        return result

    def _extract_provider_text(self, response) -> str:
        if response is None:
            return ""
        if isinstance(response, str):
            return response.strip()
        for attr in ("completion_text", "content", "text", "message"):
            value = getattr(response, attr, None)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return str(response).strip()

    def _clean_auto_reply_text(self, text: str) -> str:
        cleaned = (text or "").strip()
        cleaned = re.sub(r"^```[A-Za-z0-9_-]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        cleaned = cleaned.strip().strip('"“”')
        cleaned = self._strip_bili_meta_reply_lines(cleaned)
        if "<tts>" in cleaned.lower():
            return cleaned
        max_length = self._safe_parse_int(
            self.config.get("bili_live_auto_reply_max_length"), 80
        )
        if max_length > 0 and len(cleaned) > max_length:
            cleaned = cleaned[:max_length].rstrip() + "..."
        return cleaned

    def _strip_bili_meta_reply_lines(self, text: str) -> str:
        lines = [line.strip() for line in str(text or "").splitlines()]
        kept: list[str] = []
        for line in lines:
            if not line:
                continue
            if self._is_bili_meta_reply_line(line):
                continue
            kept.append(line)
        return "\n".join(kept).strip()

    def _is_bili_meta_reply_line(self, line: str) -> bool:
        compact = re.sub(r"\s+", "", str(line or ""))
        if not compact:
            return True
        meta_patterns = (
            "消息已经发出",
            "消息已发出",
            "已经发出去了",
            "已经发送",
            "已发送",
            "我已经回应",
            "我刚刚回应",
            "温柔地回应",
            "希望没有冷落",
            "不要冷落",
            "处理了这条弹幕",
            "这条弹幕我没太看懂",
            "这条弹幕我没有太看懂",
            "弹幕我没太看懂",
            "弹幕我没有太看懂",
        )
        return any(pattern in compact for pattern in meta_patterns)

    def _strip_tts_blocks_from_plain_chain(self, chain: list[Any]) -> list[Any]:
        cleaned_chain: list[Any] = []
        for component in chain:
            if isinstance(component, Plain):
                text = self._strip_tts_blocks_from_text(getattr(component, "text", "") or "")
                text = self._dedupe_repeated_plain_text(text)
                if text:
                    component.text = text
                    cleaned_chain.append(component)
                continue
            cleaned_chain.append(component)
        return cleaned_chain or chain

    def _strip_tts_blocks_from_text(self, text: str) -> str:
        cleaned = re.sub(r"(?is)<tts>.*?</tts>", "", str(text or ""))
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned

    def _dedupe_repeated_plain_text(self, text: str) -> str:
        cleaned = str(text or "").strip()
        if not cleaned:
            return ""
        compact = re.sub(r"\s+", "", cleaned)
        if len(compact) % 2 == 0:
            half = len(compact) // 2
            if compact[:half] == compact[half:]:
                return cleaned[: max(1, len(cleaned) // 2)].strip()
        lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
        if len(lines) == 2 and lines[0] == lines[1]:
            return lines[0]
        return cleaned

    def _is_bili_live_running(self) -> bool:
        return bool(self._bili_live_task and not self._bili_live_task.done())

    def _get_bili_live_task_error(self) -> str:
        if not self._bili_live_task or not self._bili_live_task.done():
            return ""
        try:
            exc = self._bili_live_task.exception()
        except asyncio.CancelledError:
            return "任务已取消"
        except Exception as e:
            return str(e)
        return str(exc) if exc else ""

    def _recent_bili_events(
        self,
        limit: Optional[int] = None,
        include_events: Optional[list[str]] = None,
    ) -> list[LiveDanmakuEvent]:
        if limit is None:
            limit = int(self.config.get("bili_live_inject_max_events", 8) or 8)
        limit = max(1, limit)
        allowed = {item.strip() for item in include_events or [] if str(item).strip()}
        events = list(self._bili_events)
        if allowed:
            events = [event for event in events if event.event_type in allowed]
        return events[-limit:]

    def _format_bili_events(self, events: list[LiveDanmakuEvent]) -> str:
        if not events:
            return ""
        now = time.time()
        lines: list[str] = []
        for event in events:
            age = max(0, int(now - event.ts))
            lines.append(f"- [{event.event_type}，{age}秒前] {event.display_text()}")
        return "\n".join(lines)

    def _build_bili_support_reply_hint(self, events: list[LiveDanmakuEvent]) -> str:
        if not any(event.event_type in {"gift", "super_chat"} for event in events):
            return ""
        return (
            "\n\n本批直播事件包含礼物或醒目留言。请优先感谢送礼物/SC 的观众，"
            "自然提到观众名和礼物或 SC 内容；不要机械复读数量，不要像播报清单。"
        )

    async def _inject_bili_live_context(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        if not self.config.get("bili_live_inject_enabled", True):
            return
        if not self._is_bili_live_enabled():
            return
        if not self._is_bili_live_running():
            return

        include_events = self.config.get("bili_live_inject_event_types", ["danmaku"])
        if not isinstance(include_events, list):
            include_events = ["danmaku"]
        events = self._recent_bili_events(include_events=include_events)
        formatted = self._format_bili_events(events)
        if not formatted:
            return

        prompt = (
            "## B站直播间实时信息\n"
            "以下是你当前可以读取到的最近 B站直播间事件。它们是实时上下文，不一定需要逐条回应；"
            "当用户要求你看弹幕、回应直播间观众，或当前对话和直播互动相关时，可以自然引用。\n"
            "不要伪造未列出的弹幕、礼物或观众行为。\n"
            f"{formatted}"
        )
        req.system_prompt += "\n\n" + prompt + "\n"

    @filter.command("bili_live_start")
    async def cmd_bili_live_start(self, event: AstrMessageEvent, room_id: int = 0):
        """启动 B站直播弹幕监听，可传入房间号，否则使用配置项。"""
        if not self._is_bili_live_enabled():
            yield event.plain_result(
                "B站直播功能未启用，请先在插件配置中开启 bilibili_enabled。"
            )
            return

        bili_type = self._get_bili_live_type()
        target_room_id = room_id or self._get_config_room_id()
        if bili_type == "web" and not target_room_id:
            yield event.plain_result(
                "请提供 B站直播房间号，例如 /bili_live_start 123456，或在插件配置中填写。"
            )
            return
        message = await self._start_bili_live(target_room_id)
        yield event.plain_result(message)

    @filter.command("bili_live_stop")
    async def cmd_bili_live_stop(self, event: AstrMessageEvent):
        """停止 B站直播弹幕监听。"""
        message = await self._stop_bili_live()
        yield event.plain_result(message)

    @filter.command("bili_live_status")
    async def cmd_bili_live_status(self, event: AstrMessageEvent):
        """查看 B站直播弹幕监听状态。"""
        enabled = self._is_bili_live_enabled()
        status = "运行中" if self._is_bili_live_running() else "未运行"
        room_id = (
            self._bili_live_client.real_room_id
            if self._bili_live_client and self._bili_live_client.real_room_id
            else self._get_config_room_id()
        )
        latest = self._bili_events[-1].display_text() if self._bili_events else "暂无"
        backend_text = (
            f"{self._get_bili_live_type()}/{self._get_bili_web_backend()}"
            if self._get_bili_live_type() == "web"
            else self._get_bili_live_type()
        )
        last_error = (
            getattr(self._bili_live_client, "last_error", "")
            or self._get_bili_live_task_error()
            or "无"
        )
        yield event.plain_result(
            f"B站直播功能：{'已启用' if enabled else '未启用'}\n"
            f"B站直播弹幕监听：{status}\n"
            f"监听后端：{backend_text}\n"
            f"房间号：{room_id or '未配置'}\n"
            f"已缓存事件：{len(self._bili_events)} 条\n"
            f"最近事件：{latest}\n"
            f"最近错误：{last_error}"
        )

    @filter.command("bili_live_debug")
    async def cmd_bili_live_debug(self, event: AstrMessageEvent, enabled: bool = True):
        """开启/关闭 B站直播调试日志。"""
        self._bili_debug_mode = bool(enabled)
        if isinstance(
            self._bili_live_client,
            (BilibiliLiveClient, BilibiliBlivedmClient, BilibiliLaplaceClient),
        ):
            self._bili_live_client.debug_log = self._bili_debug_mode
        yield event.plain_result(
            f"B站直播调试日志已{'开启' if self._bili_debug_mode else '关闭'}。"
            "如果需要看到 debug 级别日志，请同时确认 AstrBot 日志级别允许 debug 输出。"
        )

    @filter.command("bili_live_bind_here")
    async def cmd_bili_live_bind_here(self, event: AstrMessageEvent):
        """将当前聊天绑定为 B站直播自动回应输出会话。"""
        await self.put_kv_data(KV_KEY_BILI_REPLY_SESSION, event.unified_msg_origin)
        self._bili_reply_event_template = copy.copy(event)
        self._bili_reply_event_template.message_obj = copy.copy(event.message_obj)
        yield event.plain_result(
            "已将当前聊天绑定为 B站直播自动回应会话。开启 bili_live_auto_reply_enabled 后，"
            "直播弹幕会以 AstrBot 原生消息事件的方式触发 Bot 在这里回复。"
        )

    @filter.command("bili_live_probe")
    async def cmd_bili_live_probe(self, event: AstrMessageEvent, room_id: int = 0):
        """诊断 B站直播间信息和弹幕服务器信息。"""
        target_room_id = room_id or self._get_config_room_id()
        if not target_room_id:
            yield event.plain_result("请提供房间号，例如 /bili_live_probe 123456。")
            return
        try:
            info = await probe_bilibili_live_room(
                target_room_id,
                sessdata=self._get_bili_sessdata(),
            )
            lines = [
                "B站直播间诊断结果：",
                f"输入房间号：{target_room_id}",
                f"真实房间号：{info.get('real_room_id')}",
                f"直播状态：{info.get('live_status')}（0未开播，1直播中，2轮播）",
                f"房间接口：code={info.get('room_init_code')} message={info.get('room_init_message')}",
                f"弹幕接口：code={info.get('danmu_info_code')} message={info.get('danmu_info_message')}",
                f"弹幕 token：{'有' if info.get('danmu_token_present') else '无'}",
                f"弹幕服务器数：{info.get('danmu_host_count')}",
                f"服务器示例：{', '.join(info.get('danmu_hosts') or []) or '无'}",
            ]
            yield event.plain_result("\n".join(lines))
        except Exception as e:
            logger.warning(f"[B站直播] 直播间诊断失败: {e}")
            yield event.plain_result(f"B站直播间诊断失败：{e}")

    @filter.command("bili_live_recent")
    async def cmd_bili_live_recent(self, event: AstrMessageEvent, limit: int = 10):
        """查看最近缓存的 B站直播弹幕/事件。"""
        if not self._is_bili_live_enabled():
            yield event.plain_result(
                "B站直播功能未启用，请先在插件配置中开启 bilibili_enabled。"
            )
            return

        events = self._recent_bili_events(limit=limit, include_events=[])
        formatted = self._format_bili_events(events)
        if not formatted:
            formatted = await self._format_bili_history_fallback(limit)
        yield event.plain_result(formatted or "暂时还没有读取到 B站直播事件。")

    @llm_tool(name="bili_live_recent_danmaku")
    async def tool_bili_live_recent_danmaku(
        self, event: AstrMessageEvent, limit: int = 8
    ):
        """
        读取最近的 B站直播弹幕和直播间事件。适合在用户询问直播弹幕、要求回应观众、
        或需要了解直播间实时互动时调用。

        Args:
            limit(number): 返回最近多少条事件，默认 8，最大 30。
        """
        if not self._is_bili_live_enabled():
            return "B站直播功能未启用，请先在插件配置中开启 bilibili_enabled。"

        limit = min(30, max(1, int(limit or 8)))
        events = self._recent_bili_events(limit=limit, include_events=[])
        formatted = self._format_bili_events(events)
        if not formatted:
            formatted = await self._format_bili_history_fallback(limit)
        if not formatted:
            if self._is_bili_live_running():
                return "B站直播弹幕监听正在运行，但暂时还没有读取到事件。"
            return "B站直播弹幕监听未运行，请先使用 /bili_live_start <房间号> 启动。"
        return "最近的 B站直播间事件：\n" + formatted

    async def _format_bili_history_fallback(self, limit: int = 10) -> str:
        client = self._bili_live_client
        fetcher = getattr(client, "fetch_recent_history_events", None)
        if not fetcher:
            return ""
        try:
            events = await fetcher(limit)
        except Exception as e:
            logger.debug(f"[B站直播] 读取历史弹幕兜底失败: {e}")
            return ""
        return self._format_bili_events(events)

    # ------------------------------------------------------------------ #
    #  自主 Live2D 标签机制
    # ------------------------------------------------------------------ #

    def _get_l2d_entries(self) -> list[dict[str, Any]]:
        entries = self.config.get("l2d_hotkeys", [])
        if not isinstance(entries, list):
            return []

        normalized: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict) or not entry.get("enabled", True):
                continue
            name = str(
                entry.get("name")
                or entry.get("expression_name")
                or entry.get("tag")
                or ""
            ).strip()
            tag = str(entry.get("tag") or name).strip()
            hotkey_id = str(entry.get("hotkey_id", "")).strip()
            if not tag or not hotkey_id:
                continue
            try:
                duration = max(0.0, float(entry.get("duration", 0) or 0))
            except (TypeError, ValueError):
                duration = 0.0
            normalized.append(
                {
                    "name": name or tag,
                    "tag": tag,
                    "hotkey_id": hotkey_id,
                    "description": str(entry.get("description", "")).strip(),
                    "duration": duration,
                    "release_after_duration": bool(
                        entry.get("release_after_duration", True)
                    ),
                }
            )
        return normalized

    def _l2d_entry_map(self) -> dict[str, dict[str, Any]]:
        return {entry["tag"].lower(): entry for entry in self._get_l2d_entries()}

    def _parse_l2d_tags(self, text: str) -> tuple[list[str], str]:
        tags: list[str] = []

        def collect(match: re.Match) -> str:
            raw = (match.group(1) or match.group(2) or "").strip()
            for item in re.split(r"[\s,，、|/]+", raw):
                tag = item.strip()
                if tag:
                    tags.append(tag)
            return ""

        cleaned = L2D_TAG_PATTERN.sub(collect, text)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
        return tags, cleaned

    def _create_l2d_task(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._l2d_tasks.add(task)
        task.add_done_callback(self._l2d_tasks.discard)

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """在模型回复前注入直播弹幕上下文和可选 Live2D 标签说明。"""
        await self._inject_bili_live_context(event, req)

        if not self.config.get("autonomous_l2d_enabled", True):
            return

        entries = self._get_l2d_entries()
        if not entries:
            return
        if not await self._check_and_reconnect():
            logger.debug("[VTS] 未连接 Live2D，跳过 L2D 标签提示词注入")
            return

        max_tags = int(self.config.get("l2d_max_tags_per_reply", 1) or 1)
        max_tags = max(1, max_tags)
        lines = [
            "## Live2D 表情控制",
            "你可以通过在回复末尾输出 Live2D 标签来控制当前 Live2D 模型表情。",
            "标签只用于控制表情，不是给用户看的内容。正常回答用户，然后在最后单独输出一行标签。",
            f"格式：<l2d:标签名>。最多选择 {max_tags} 个；多个标签可写成 <l2d:标签1,标签2>。",
            "如果本次回复不适合使用表情，输出 <l2d:none>。",
            "不要解释标签，不要编造未列出的标签。",
            "",
            "可选表情按键：",
        ]
        for entry in entries:
            desc = entry["description"] or "无额外说明"
            duration = entry["duration"]
            duration_text = f"{duration:g} 秒" if duration > 0 else "不自动结束"
            lines.append(
                f"- {entry['tag']}（{entry['name']}）: {desc}；持续时间：{duration_text}；热键ID：{entry['hotkey_id']}"
            )

        req.system_prompt += "\n\n" + "\n".join(lines) + "\n"

    @filter.on_llm_response(priority=2000)
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        """处理模型回复中的 Live2D 标签。字幕会在最终消息链阶段推送。"""
        completion_text = getattr(resp, "completion_text", None)
        if not isinstance(completion_text, str) or not completion_text.strip():
            return

        if self.config.get("autonomous_l2d_enabled", True) and "<l2d" in completion_text.lower():
            tags, cleaned = self._parse_l2d_tags(completion_text)
            if cleaned != completion_text:
                resp.completion_text = cleaned

            tags = [tag for tag in tags if tag.lower() not in {"none", "无", "null", "no"}]
            if tags:
                max_tags = int(self.config.get("l2d_max_tags_per_reply", 1) or 1)
                self._create_l2d_task(self._trigger_l2d_tags(tags[: max(1, max_tags)]))

    @filter.on_decorating_result(priority=100000000000000000)
    async def on_subtitle_decorating_result(self, event: AstrMessageEvent):
        """在 TTS 语音生成完成后，同步启动字幕和嘴型联动。"""
        if not self._is_subtitle_enabled() and not self._is_mouth_sync_enabled():
            return
        result = event.get_result()
        if not result or not getattr(result, "chain", None):
            return

        if not getattr(result, "__vts_mouth_sync_processed", False):
            setattr(result, "__vts_mouth_sync_processed", True)
            await self._start_mouth_sync_for_result(result)

        if not self._is_subtitle_enabled():
            return
        if getattr(result, "__vts_subtitle_processed", False):
            return

        setattr(result, "__vts_subtitle_processed", True)

        text = self._extract_subtitle_text_from_result(result)
        await self._push_subtitle(text)

    async def _trigger_l2d_tags(self, tags: list[str]) -> None:
        entries = self._l2d_entry_map()
        if not entries:
            return

        if not await self._check_and_reconnect():
            logger.warning("[VTS] 收到 L2D 标签，但 VTube Studio 未连接，已跳过触发")
            return

        for tag in tags:
            entry = entries.get(tag.lower())
            if not entry:
                logger.warning(f"[VTS] 未配置的 L2D 标签: {tag}")
                continue
            await self._trigger_l2d_entry(entry)

    async def _trigger_l2d_entry(self, entry: dict[str, Any]) -> None:
        hotkey_id = entry["hotkey_id"]
        try:
            await self.vts.trigger_hotkey(hotkey_id)
            logger.info(f"[VTS] L2D 标签 {entry['tag']} 已触发热键 {hotkey_id}")
        except Exception as e:
            logger.warning(f"[VTS] L2D 标签 {entry['tag']} 触发失败: {e}")
            return

        duration = entry["duration"]
        if duration > 0 and entry["release_after_duration"]:
            self._create_l2d_task(self._release_l2d_entry(entry, duration))

    async def _release_l2d_entry(self, entry: dict[str, Any], duration: float) -> None:
        try:
            await asyncio.sleep(duration)
            if not await self._check_and_reconnect():
                return
            await self.vts.trigger_hotkey(entry["hotkey_id"])
            logger.info(
                f"[VTS] L2D 标签 {entry['tag']} 持续 {duration:g} 秒后已再次触发热键"
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[VTS] L2D 标签 {entry['tag']} 自动结束失败: {e}")

    @filter.command("vts_l2d_list")
    async def cmd_vts_l2d_list(self, event: AstrMessageEvent):
        """列出自主 Live2D 标签配置。"""
        entries = self._get_l2d_entries()
        if not entries:
            yield event.plain_result("当前没有启用的 L2D 标签条目，请先在插件配置中添加。")
            return

        lines = ["当前启用的 L2D 标签："]
        for entry in entries:
            duration = entry["duration"]
            duration_text = f"{duration:g} 秒" if duration > 0 else "不自动结束"
            lines.append(
                f"• {entry['name']}：<l2d:{entry['tag']}> -> {entry['hotkey_id']} | {duration_text} | "
                f"{entry['description'] or '无说明'}"
            )
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------------ #
    #  Token 持久化（使用框架 KV 存储）
    # ------------------------------------------------------------------ #

    async def _load_token(self) -> Optional[str]:
        """从框架 KV 存储加载 Token"""
        return await self.get_kv_data(KV_KEY_TOKEN, None)

    async def _save_token(self, token: str):
        """保存 Token 到框架 KV 存储"""
        await self.put_kv_data(KV_KEY_TOKEN, token)

    async def _ensure_connection(self) -> str:
        """确保连接可用，返回错误消息或空字符串"""
        if not await self._check_and_reconnect():
            return "❌ 未连接到 VTube Studio，请先发送 /vts_auth 进行认证。"
        return ""

    # ------------------------------------------------------------------ #
    #  命令
    # ------------------------------------------------------------------ #

    @filter.command("vts_auth")
    async def cmd_vts_auth(self, event: AstrMessageEvent):
        """发送 /vts_auth 触发 VTube Studio 认证流程"""
        yield event.plain_result(
            "正在向 VTube Studio 申请认证 Token，请在 VTS 界面点击【允许】按钮..."
        )
        try:
            token = await self.vts.request_auth_token()
            ok = await self.vts.authenticate(token)
            if ok:
                await self._save_token(token)
                self._connected = True
                yield event.plain_result(
                    "✅ VTube Studio 认证成功！Token 已保存。\n"
                    "现在 LLM 可以控制你的 Live2D 模型了。"
                )
            else:
                yield event.plain_result("❌ 认证失败，请确认已在 VTS 界面点击允许。")
        except VTSConnectionError as e:
            yield event.plain_result(f"❌ 连接失败：{e}")
        except VTSTimeoutError as e:
            yield event.plain_result(f"❌ 连接超时：{e}")
        except Exception as e:
            yield event.plain_result(
                f"❌ 认证出错：{e}\n"
                "请确保 VTube Studio 已启动并开启了 API。\n"
                "可先发送 /vts_discover 重新扫描。"
            )

    @filter.command("vts_discover")
    async def cmd_vts_discover(self, event: AstrMessageEvent):
        """重新扫描并自动发现 VTube Studio 的运行地址"""
        yield event.plain_result(f"🔍 正在扫描 VTube Studio（{platform.system()} 平台）...")
        try:
            info = get_install_info()
            host, port = await auto_discover()

            self.vts.url = f"ws://{host}:{port}"
            await self.vts.reset_connection()

            lines = [
                f"🖥️ 操作系统：{info['os']}",
                f"📂 安装路径：{info['install_path'] or '未找到'}",
                f"⚙️ 配置文件端口：{info['config_port'] or '未读取到'}",
                f"🔄 进程运行中：{'是' if info['process_running'] else '否（需要 psutil）'}",
                "",
                f"✅ 已将连接地址更新为 ws://{host}:{port}",
                "",
                "如需认证请发送 /vts_auth",
            ]
            yield event.plain_result("\n".join(lines))

            saved_token = await self._load_token()
            if saved_token:
                ok = await self.vts.authenticate(saved_token)
                if ok:
                    self._connected = True
                    yield event.plain_result("🔗 已用保存的 Token 重新认证成功！")
        except Exception as e:
            yield event.plain_result(f"❌ 自动发现失败：{e}")

    @filter.command("vts_status")
    async def cmd_vts_status(self, event: AstrMessageEvent):
        """查询 VTube Studio 连接状态和当前模型信息"""
        if not await self._check_and_reconnect():
            yield event.plain_result(
                "❌ 未连接到 VTube Studio。\n"
                "• 发送 /vts_discover 自动扫描\n"
                "• 发送 /vts_auth 进行认证"
            )
            return
        try:
            model_info = await self.vts.get_model_info()
            hotkeys = await self.vts.get_hotkeys()
            expressions = await self.vts.get_expressions()

            hotkey_names = [h.get("name", h.get("hotkeyID", "?")) for h in hotkeys]
            expr_names = [e.get("file", "?") for e in expressions]

            msg = (
                f"✅ VTube Studio 已连接（{self.vts.url}）\n"
                f"🖥️ 平台：{platform.system()}\n"
                f"📦 当前模型：{model_info.get('modelName', '未知')}\n"
                f"🎬 可用热键（{len(hotkeys)} 个）：{', '.join(hotkey_names[:10]) or '无'}\n"
                f"😊 可用表情（{len(expressions)} 个）：{', '.join(expr_names[:10]) or '无'}"
            )
            yield event.plain_result(msg)
        except VTSConnectionError as e:
            self._connected = False
            yield event.plain_result(f"❌ 连接已断开：{e}")
        except Exception as e:
            yield event.plain_result(f"❌ 查询失败：{e}")

    @filter.command("vts_list")
    async def cmd_vts_list(self, event: AstrMessageEvent):
        """列出所有热键和表情"""
        if not await self._check_and_reconnect():
            yield event.plain_result("❌ 未连接到 VTube Studio，请先发送 /vts_auth 进行认证。")
            return
        try:
            hotkeys = await self.vts.get_hotkeys()
            expressions = await self.vts.get_expressions()

            lines = ["🎬 **热键列表**"]
            for h in hotkeys:
                lines.append(
                    f"  • {h.get('name', '?')}  "
                    f"(ID: {h.get('hotkeyID', '?')}，类型: {h.get('type', '?')})"
                )
            lines.append("\n😊 **表情列表**")
            for e in expressions:
                active_mark = "✅" if e.get("active") else "⬜"
                lines.append(f"  {active_mark} {e.get('file', '?')}")

            yield event.plain_result("\n".join(lines))
        except VTSConnectionError as e:
            self._connected = False
            yield event.plain_result(f"❌ 连接已断开：{e}")
        except Exception as e:
            yield event.plain_result(f"❌ 查询失败：{e}")

    # ------------------------------------------------------------------ #
    #  LLM 工具函数
    # ------------------------------------------------------------------ #

    @llm_tool(name="vts_trigger_hotkey")
    async def tool_trigger_hotkey(self, event: AstrMessageEvent, hotkey_id: str):
        """
        触发 VTube Studio 中的热键，可以播放动作动画、切换表情、改变待机动画等。
        使用前建议先用 vts_get_hotkeys 获取可用热键列表。

        Args:
            hotkey_id(string): 热键的名称或唯一ID，例如 "wave" 或 "Smile"
        """
        err = await self._ensure_connection()
        if err:
            return err
        try:
            result = await self.vts.trigger_hotkey(hotkey_id)
            return f"✅ 已触发热键「{hotkey_id}」。结果：{json.dumps(result, ensure_ascii=False)}"
        except VTSConnectionError as e:
            self._connected = False
            return f"❌ 连接已断开：{e}"
        except VTSTimeoutError as e:
            return f"❌ 请求超时：{e}"
        except Exception as e:
            return f"❌ 触发热键失败：{e}"

    @llm_tool(name="vts_get_hotkeys")
    async def tool_get_hotkeys(self, event: AstrMessageEvent):
        """
        获取 VTube Studio 当前模型可用的所有热键列表（包括动作、表情热键等）。
        """
        err = await self._ensure_connection()
        if err:
            return err
        try:
            hotkeys = await self.vts.get_hotkeys()
            if not hotkeys:
                return "当前模型没有可用热键。"
            lines = ["当前模型可用热键："]
            for h in hotkeys:
                lines.append(
                    f"• 名称: {h.get('name','?')}, "
                    f"ID: {h.get('hotkeyID','?')}, "
                    f"类型: {h.get('type','?')}"
                )
            return "\n".join(lines)
        except VTSConnectionError as e:
            self._connected = False
            return f"❌ 连接已断开：{e}"
        except Exception as e:
            return f"❌ 获取热键列表失败：{e}"

    @llm_tool(name="vts_set_expression")
    async def tool_set_expression(
        self,
        event: AstrMessageEvent,
        expression_file: str,
        active: bool = True,
        fade_time: float = 0.25,
    ):
        """
        激活或停用 VTube Studio 中的指定表情。
        使用前建议先用 vts_get_expressions 获取可用表情列表。

        Args:
            expression_file(string): 表情文件名，例如 "happy.exp3.json"
            active(boolean): true 表示激活表情，false 表示停用表情，默认 true
            fade_time(number): 淡入淡出时间（秒），默认 0.25
        """
        err = await self._ensure_connection()
        if err:
            return err
        try:
            result = await self.vts.set_expression(expression_file, active, fade_time)
            action = "激活" if active else "停用"
            return f"✅ 已{action}表情「{expression_file}」。结果：{json.dumps(result, ensure_ascii=False)}"
        except VTSConnectionError as e:
            self._connected = False
            return f"❌ 连接已断开：{e}"
        except VTSTimeoutError as e:
            return f"❌ 请求超时：{e}"
        except Exception as e:
            return f"❌ 设置表情失败：{e}"

    @llm_tool(name="vts_get_expressions")
    async def tool_get_expressions(self, event: AstrMessageEvent):
        """
        获取 VTube Studio 当前模型的所有可用表情列表及其激活状态。
        """
        err = await self._ensure_connection()
        if err:
            return err
        try:
            expressions = await self.vts.get_expressions()
            if not expressions:
                return "当前模型没有可用表情。"
            lines = ["当前模型可用表情："]
            for e in expressions:
                status = "✅ 激活中" if e.get("active") else "⬜ 未激活"
                lines.append(f"• {e.get('file', '?')} [{status}]")
            return "\n".join(lines)
        except VTSConnectionError as e:
            self._connected = False
            return f"❌ 连接已断开：{e}"
        except Exception as e:
            return f"❌ 获取表情列表失败：{e}"

    @llm_tool(name="vts_move_model")
    async def tool_move_model(
        self,
        event: AstrMessageEvent,
        position_x: float = 0.0,
        position_y: float = 0.0,
        rotation: float = 0.0,
        size: float = 0.0,
        duration: float = 0.5,
    ):
        """
        移动、旋转或缩放 VTube Studio 中的 Live2D 模型。

        Args:
            position_x(number): 水平位置，范围 -1.0（最左）到 1.0（最右），0 为居中
            position_y(number): 垂直位置，范围 -1.0（最下）到 1.0（最上），0 为居中
            rotation(number): 旋转角度，范围 -360 到 360 度，0 为不旋转
            size(number): 缩放大小，范围 -100 到 100，0 为不变
            duration(number): 动画持续时间（秒），默认 0.5
        """
        err = await self._ensure_connection()
        if err:
            return err
        try:
            await self.vts.move_model(
                position_x=position_x,
                position_y=position_y,
                rotation=rotation,
                size=size,
                time_in_seconds=duration,
            )
            return (
                f"✅ 已移动模型：位置({position_x:.2f}, {position_y:.2f}), "
                f"旋转{rotation}°, 大小变化{size}。"
            )
        except VTSConnectionError as e:
            self._connected = False
            return f"❌ 连接已断开：{e}"
        except VTSTimeoutError as e:
            return f"❌ 请求超时：{e}"
        except Exception as e:
            return f"❌ 移动模型失败：{e}"

    @llm_tool(name="vts_inject_parameter")
    async def tool_inject_parameter(
        self,
        event: AstrMessageEvent,
        parameter_id: str,
        value: float,
        mode: str = "set",
    ):
        """
        向 VTube Studio 注入 Live2D 参数值，可以精细控制模型的面部表情参数。
        常用参数：FaceAngleX（水平转头）、FaceAngleY（点头）、FaceAngleZ（倾头）、
        MouthOpen（开嘴）、MouthSmile（微笑）、EyeOpenLeft/Right（眼睛睁开程度）。

        Args:
            parameter_id(string): 参数名称，例如 "MouthSmile" 或 "FaceAngleX"
            value(number): 参数值（通常为 -1.0 ~ 1.0）
            mode(string): 控制模式，"set" 表示直接设置，"add" 表示叠加，默认 "set"
        """
        err = await self._ensure_connection()
        if err:
            return err
        try:
            await self.vts.inject_parameters(
                parameters=[{"id": parameter_id, "value": value}],
                mode=mode,
            )
            return f"✅ 已设置参数「{parameter_id}」= {value}（模式: {mode}）"
        except VTSConnectionError as e:
            self._connected = False
            return f"❌ 连接已断开：{e}"
        except VTSTimeoutError as e:
            return f"❌ 请求超时：{e}"
        except Exception as e:
            return f"❌ 注入参数失败：{e}"

    @llm_tool(name="vts_get_parameters")
    async def tool_get_parameters(self, event: AstrMessageEvent):
        """
        获取 VTube Studio 当前模型所有可用的 Live2D 输入参数列表。
        """
        err = await self._ensure_connection()
        if err:
            return err
        try:
            params = await self.vts.get_input_parameters()
            if not params:
                return "没有可用参数。"
            lines = [f"当前模型可用参数（共 {len(params)} 个，显示前30个）："]
            for p in params[:30]:
                lines.append(
                    f"• {p.get('name','?')} "
                    f"范围:[{p.get('min','?')}, {p.get('max','?')}] "
                    f"当前值:{p.get('value','?')}"
                )
            return "\n".join(lines)
        except VTSConnectionError as e:
            self._connected = False
            return f"❌ 连接已断开：{e}"
        except Exception as e:
            return f"❌ 获取参数列表失败：{e}"

    @llm_tool(name="vts_model_info")
    async def tool_model_info(self, event: AstrMessageEvent):
        """
        获取 VTube Studio 当前加载的 Live2D 模型的基本信息。
        """
        err = await self._ensure_connection()
        if err:
            return err
        try:
            info = await self.vts.get_model_info()
            return (
                f"当前模型信息：\n"
                f"• 名称：{info.get('modelName', '未知')}\n"
                f"• 文件：{info.get('modelFileName', '未知')}\n"
                f"• VTS模型ID：{info.get('modelID', '未知')}"
            )
        except VTSConnectionError as e:
            self._connected = False
            return f"❌ 连接已断开：{e}"
        except Exception as e:
            return f"❌ 获取模型信息失败：{e}"
