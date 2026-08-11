import asyncio
import base64
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register


APOD_API_URL = "https://api.nasa.gov/planetary/apod"
APOD_TIMEOUT_SECONDS = 8.0
APOD_MAX_ATTEMPTS = 3


@register(
    "apod_daily",
    "Akina",
    "每日推送 NASA APOD 天文图片",
    "1.0.0",
)
class ApodDailyPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.context = context
        self.config = config or {}

        self.api_key = str(self._config_value("api_key", "DEMO_KEY") or "DEMO_KEY")
        self.translator_provider_id = str(
            self._config_value("translator_provider_id", "") or ""
        ).strip()
        self.push_time = str(self.config.get("push_time", "14:00"))
        self.push_hour, self.push_minute = self.parse_push_time(self.push_time)
        try:
            self.explanation_word_limit = int(
                self.config.get("explanation_word_limit", 250)
            )
        except (TypeError, ValueError):
            logger.warning("explanation_word_limit 配置无效，将回退到 250")
            self.explanation_word_limit = 250

        self.plugin_dir = Path(__file__).parent
        self.data_dir = self.plugin_dir / "data"
        self.images_dir = self.data_dir / "images"
        self.cache_path = self.data_dir / "apod_cache.json"
        self.card_template_path = self.plugin_dir / "apod_card.html"
        self._ensure_cache_files()
        self.platform_id = str(self.load_cache().get("platform_id") or "").strip()

        self._daily_task = None
        try:
            self._daily_task = asyncio.create_task(self._daily_push_loop())
        except RuntimeError as exc:
            logger.warning(f"APOD 定时任务启动失败：{exc}")

    def _config_value(self, key: str, default: Any = None) -> Any:
        value = self.config.get(key, default)
        if isinstance(value, dict) and "value" in value:
            return value["value"]
        return value

    def _ensure_cache_files(self) -> None:
        try:
            self.images_dir.mkdir(parents=True, exist_ok=True)
            if not self.cache_path.exists():
                self.cache_path.write_text("{}\n", encoding="utf-8")
        except Exception as exc:
            logger.warning(f"APOD 缓存目录初始化失败：{exc}")

    @filter.command("apod")
    async def apod(self, event: AstrMessageEvent):
        """在当前聊天中获取今日 APOD。"""
        try:
            self._remember_platform_id(event)
            payload = await self.get_apod_payload(event)
            card_path = await self.render_apod_card(payload)
            yield event.image_result(card_path)
        except Exception as exc:
            yield event.plain_result(f"NASA APOD 获取失败：{exc}")

    @filter.command("apod_test")
    async def apod_test(self, event: AstrMessageEvent):
        """测试向配置的所有 QQ 群主动推送。"""
        self._remember_platform_id(event)
        group_ids = self._target_group_ids()
        if not group_ids:
            yield event.plain_result("请先在插件配置中填写目标群号 target_group_id")
            return

        try:
            payload = await self.get_apod_payload(event)
            card_path = await self.render_apod_card(payload)
            succeeded, failed = [], []
            for group_id in group_ids:
                try:
                    await self._send_group(group_id, card_path)
                    succeeded.append(group_id)
                except Exception as exc:
                    logger.warning(f"APOD 测试推送到群 {group_id} 失败：{exc}")
                    failed.append(group_id)

            parts = []
            if succeeded:
                parts.append("推送成功：" + "、".join(succeeded))
            if failed:
                parts.append(
                    "推送失败：" + "、".join(failed) + "（详细原因请查看 AstrBot 日志）"
                )
            yield event.plain_result("\n".join(parts))
        except Exception as exc:
            yield event.plain_result(f"NASA APOD 获取失败：{exc}")

    def _fetch_apod(self) -> dict:
        last_error: Exception | None = None
        for attempt in range(1, APOD_MAX_ATTEMPTS + 1):
            try:
                response = httpx.get(
                    APOD_API_URL,
                    params={"api_key": self.api_key},
                    timeout=APOD_TIMEOUT_SECONDS,
                    follow_redirects=True,
                )
                if response.status_code != 200:
                    body = response.text[:200].replace("\n", " ")
                    raise RuntimeError(
                        f"HTTP {response.status_code}，响应内容：{body}"
                    )
                try:
                    data = response.json()
                except Exception as exc:
                    raise RuntimeError(f"JSON 解析失败：{exc}") from exc
                if not isinstance(data, dict):
                    raise RuntimeError("API 返回的 JSON 不是对象")
                return data
            except Exception as exc:
                last_error = exc
                logger.warning(
                    f"NASA APOD 请求失败（第 {attempt}/{APOD_MAX_ATTEMPTS} 次）：{exc}"
                )
        raise RuntimeError(str(last_error or "未知错误"))

    def load_cache(self) -> dict:
        try:
            if not self.cache_path.exists():
                return {}
            cache = json.loads(self.cache_path.read_text(encoding="utf-8"))
            return cache if isinstance(cache, dict) else {}
        except Exception as exc:
            logger.warning(f"APOD 缓存读取失败：{exc}")
            return {}

    def save_cache(self, cache: dict) -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(
                json.dumps(cache, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning(f"APOD 缓存保存失败：{exc}")

    def is_cache_hit(self, api_data: dict, cache: dict) -> bool:
        media_url = api_data.get("hdurl") or api_data.get("url") or ""
        return bool(
            cache
            and api_data.get("date") == cache.get("date")
            and media_url == cache.get("media_url")
        )

    async def get_apod_payload(
        self, event: AstrMessageEvent | None = None
    ) -> dict:
        api_data = self._fetch_apod()
        cache = self.load_cache()

        if self.is_cache_hit(api_data, cache):
            logger.info("APOD 缓存命中，复用已翻译内容")
            changed = False
            if self.platform_id and cache.get("platform_id") != self.platform_id:
                cache["platform_id"] = self.platform_id
                changed = True
            if not isinstance(cache.get("pushed_groups"), dict):
                cache["pushed_groups"] = {}
                changed = True
            if cache.get("media_type") == "image":
                too_long = self.is_explanation_too_long(
                    str(cache.get("explanation", ""))
                )
                if cache.get("explanation_too_long") != too_long:
                    cache["explanation_too_long"] = too_long
                    changed = True
                if too_long and cache.get("explanation_zh") != "太长而截断":
                    cache["explanation_zh"] = "太长而截断"
                    changed = True
            if changed:
                self.save_cache(cache)
            return cache

        title = str(api_data.get("title") or "未命名")
        date = str(api_data.get("date") or "")
        explanation = str(api_data.get("explanation") or "")
        media_type = str(api_data.get("media_type") or "unknown")
        media_url = str(api_data.get("hdurl") or api_data.get("url") or "")
        title_zh = ""
        explanation_zh = ""
        explanation_too_long = False
        local_image_path = ""

        if media_type == "image":
            explanation_too_long = self.is_explanation_too_long(explanation)
            if explanation_too_long:
                title_zh, _ = await self.translate_with_llm(event, title, "")
                explanation_zh = "太长而截断"
            else:
                title_zh, explanation_zh = await self.translate_with_llm(
                    event, title, explanation
                )
            if media_url:
                local_image_path = self.download_image_if_needed(date, media_url)

        payload = {
            "date": date,
            "title": title,
            "title_zh": title_zh,
            "explanation": explanation,
            "explanation_zh": explanation_zh,
            "explanation_too_long": explanation_too_long,
            "media_type": media_type,
            "media_url": media_url,
            "local_image_path": local_image_path,
            "pushed_groups": {},
            "platform_id": self.platform_id,
            "raw": api_data,
        }
        self.save_cache(payload)
        return payload

    def is_explanation_too_long(self, explanation: str) -> bool:
        """判断英文说明是否超过单词数限制。"""
        return len(explanation.split()) > self.explanation_word_limit

    async def translate_with_llm(
        self,
        event: AstrMessageEvent | None,
        title: str,
        explanation: str,
    ) -> tuple[str, str]:
        if not bool(self._config_value("translate_enabled", True)):
            return "", explanation

        provider_id = await self._get_translator_provider_id(event)
        if not provider_id:
            logger.warning("未找到可用的 AstrBot LLM Provider，使用英文原文")
            return "", explanation

        if explanation:
            prompt = (
                "请把下面 NASA APOD 的标题和说明翻译成简体中文。"
                "只返回 JSON，不要使用 Markdown，格式必须是："
                '{"title_zh":"...","explanation_zh":"..."}\n\n'
                f"英文标题：{title}\n英文说明：{explanation}"
            )
        else:
            prompt = (
                "请把下面 NASA APOD 标题翻译成简体中文。"
                "只返回 JSON，不要使用 Markdown，格式必须是："
                '{"title_zh":"...","explanation_zh":""}\n\n'
                f"英文标题：{title}"
            )

        try:
            response = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt="你是准确、简洁的天文学翻译助手。",
            )
            text = self._llm_response_text(response).strip()
            if text.startswith("```"):
                text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
            data = json.loads(text)
            return (
                str(data.get("title_zh") or "").strip(),
                str(data.get("explanation_zh") or "").strip(),
            )
        except Exception as exc:
            logger.warning(f"APOD LLM 翻译失败，将使用英文原文：{exc}")
            return "", explanation

    async def _get_translator_provider_id(
        self, event: AstrMessageEvent | None
    ) -> str:
        if self.translator_provider_id:
            return self.translator_provider_id

        if event is not None:
            try:
                umo = event.unified_msg_origin
                provider_id = await self.context.get_current_chat_provider_id(umo=umo)
                if provider_id:
                    return str(provider_id)
            except Exception as exc:
                logger.warning(f"获取当前会话 LLM Provider 失败：{exc}")

        try:
            provider = self.context.get_using_provider()
            if asyncio.iscoroutine(provider):
                provider = await provider
            if isinstance(provider, str):
                return provider
            return str(getattr(provider, "id", "") or "")
        except Exception as exc:
            logger.warning(f"获取全局默认 LLM Provider 失败：{exc}")
            return ""

    @staticmethod
    def _llm_response_text(response: Any) -> str:
        if isinstance(response, str):
            return response
        for name in ("completion_text", "text", "content"):
            value = getattr(response, name, None)
            if isinstance(value, str):
                return value
        if isinstance(response, dict):
            for name in ("completion_text", "text", "content"):
                value = response.get(name)
                if isinstance(value, str):
                    return value
        return str(response)

    def download_image_if_needed(self, date: str, image_url: str) -> str:
        """下载 APOD 图片；已有有效文件时直接复用。"""
        try:
            self.images_dir.mkdir(parents=True, exist_ok=True)
            url_path = urlparse(image_url).path.lower()
            suffix = ".png" if url_path.endswith(".png") else ".webp" if url_path.endswith(".webp") else ".jpg"
            safe_date = re.sub(r"[^0-9-]", "", date) or "apod"
            image_path = self.images_dir / f"{safe_date}{suffix}"
            if image_path.exists() and image_path.stat().st_size > 0:
                return str(image_path)

            response = httpx.get(
                image_url,
                timeout=20.0,
                follow_redirects=True,
            )
            response.raise_for_status()
            image_path.write_bytes(response.content)
            if image_path.stat().st_size <= 0:
                raise RuntimeError("下载到的图片为空")
            return str(image_path)
        except Exception as exc:
            logger.warning(f"APOD 图片下载失败，将尝试使用网络图片：{exc}")
            return ""

    def format_apod_message(self, payload: dict) -> str:
        if payload.get("media_type") != "image":
            return (
                "🌌 NASA 每日天文图\n"
                "今天的 APOD 是视频内容，QQ 暂不支持该推送类型，已跳过图片推送。"
            )

        title = payload.get("title_zh") or payload.get("title") or "未命名"
        lines = [
            "🌌 NASA 每日天文图",
            f"标题：{title}",
            f"日期：{payload.get('date', '')}",
        ]
        if bool(self._config_value("send_explanation", True)):
            if payload.get("explanation_too_long"):
                explanation = "太长而截断"
            else:
                explanation = (
                    payload.get("explanation_zh")
                    or payload.get("explanation")
                    or "暂无说明"
                )
            lines.extend(["说明：", str(explanation)])
        return "\n".join(lines)

    def _card_image_source(self, payload: dict) -> str:
        """优先把本地图片嵌入卡片，文件过大时使用 NASA 图片 URL。"""
        local_path = Path(str(payload.get("local_image_path") or ""))
        try:
            if local_path.is_file() and 0 < local_path.stat().st_size <= 8 * 1024 * 1024:
                suffix = local_path.suffix.lower()
                mime_type = {
                    ".png": "image/png",
                    ".webp": "image/webp",
                }.get(suffix, "image/jpeg")
                encoded = base64.b64encode(local_path.read_bytes()).decode("ascii")
                return f"data:{mime_type};base64,{encoded}"
        except Exception as exc:
            logger.warning(f"读取 APOD 本地图片用于卡片失败：{exc}")
        return str(payload.get("media_url") or "")

    async def render_apod_card(self, payload: dict) -> str:
        """使用 AstrBot HTML 渲染能力生成包含图片和文字的单张卡片。"""
        try:
            template = self.card_template_path.read_text(encoding="utf-8")
        except Exception as exc:
            raise RuntimeError(f"读取 APOD 卡片模板失败：{exc}") from exc

        media_type = str(payload.get("media_type") or "unknown")
        is_image = media_type == "image"
        title = payload.get("title_zh") or payload.get("title") or "未命名"
        show_explanation = is_image and bool(
            self._config_value("send_explanation", True)
        )
        if payload.get("explanation_too_long"):
            explanation = "太长而截断"
        else:
            explanation = (
                payload.get("explanation_zh")
                or payload.get("explanation")
                or "暂无说明"
            )

        data = {
            "is_image": is_image,
            "image_src": self._card_image_source(payload) if is_image else "",
            "title": str(title),
            "date": str(payload.get("date") or ""),
            "show_explanation": show_explanation,
            "explanation": str(explanation),
            "video_notice": (
                "今天的 APOD 是视频内容，QQ 暂不支持该推送类型，"
                "已跳过图片推送。"
            ),
        }
        options = {
            "full_page": True,
            "type": "jpeg",
            "quality": 88,
            "scale": "device",
            "device_scale_factor_level": "normal",
        }
        try:
            output = await self.html_render(
                tmpl=template,
                data=data,
                return_url=False,
                options=options,
            )
        except Exception as exc:
            raise RuntimeError(f"APOD 卡片渲染失败：{exc}") from exc

        if not output:
            raise RuntimeError("APOD 卡片渲染失败：未返回图片")
        output_path = Path(str(output))
        if not output_path.is_file() or output_path.stat().st_size <= 0:
            raise RuntimeError(f"APOD 卡片渲染失败：图片文件无效（{output}）")
        return str(output_path)

    def _remember_platform_id(self, event: AstrMessageEvent) -> None:
        """记录当前机器人平台实例 ID，供主动和定时推送构造 UMO。"""
        try:
            get_platform_id = getattr(event, "get_platform_id", None)
            platform_id = str(get_platform_id() or "").strip() if get_platform_id else ""
            if not platform_id:
                umo = str(event.unified_msg_origin or "")
                parts = umo.rsplit(":", 2)
                platform_id = parts[0].strip() if len(parts) == 3 else ""
            if not platform_id or platform_id == self.platform_id:
                return

            self.platform_id = platform_id
            cache = self.load_cache()
            cache["platform_id"] = platform_id
            self.save_cache(cache)
            logger.info(f"已记录 APOD 主动推送平台 ID：{platform_id}")
        except Exception as exc:
            logger.warning(f"记录 APOD 主动推送平台 ID 失败：{exc}")

    async def _send_group(self, group_id: str, card_path: str) -> None:
        if not self.platform_id:
            raise RuntimeError("未记录 AstrBot 平台 ID，请先在机器人会话中执行 /apod_test")

        umo = f"{self.platform_id}:GroupMessage:{group_id}"
        last_error: Exception | None = None
        for attempt in range(1, 3):
            try:
                # 每个群、每次尝试都创建新的消息链，避免适配器修改已发送的链。
                chain = MessageChain().file_image(card_path)
                sent = await self.context.send_message(umo, chain)
                if not sent:
                    raise RuntimeError(
                        f"AstrBot 未找到平台实例 {self.platform_id}，消息未发送"
                    )
                return
            except Exception as exc:
                last_error = exc
                logger.warning(
                    f"APOD 图片推送到群 {group_id} 失败"
                    f"（第 {attempt}/2 次）：{exc}"
                )
                if attempt < 2:
                    await asyncio.sleep(2)
        raise RuntimeError(str(last_error or "未知发送错误"))

    def has_pushed_today(self, payload: dict, group_id: str) -> bool:
        """判断某个群今天是否已经推送过当前 APOD。"""
        try:
            pushed_groups = payload.get("pushed_groups", {})
            return (
                isinstance(pushed_groups, dict)
                and pushed_groups.get(str(group_id)) == payload.get("date")
            )
        except Exception as exc:
            logger.warning(f"读取 APOD 推送记录失败：{exc}")
            return False

    def mark_pushed(self, payload: dict, group_id: str) -> None:
        """发送成功后记录该群已经推送过当前 APOD。"""
        try:
            pushed_groups = payload.setdefault("pushed_groups", {})
            if not isinstance(pushed_groups, dict):
                pushed_groups = {}
                payload["pushed_groups"] = pushed_groups
            pushed_groups[str(group_id)] = str(payload.get("date") or "")
            self.save_cache(payload)
        except Exception as exc:
            logger.warning(f"保存 APOD 推送记录失败：{exc}")

    def parse_push_time(self, value: str) -> tuple[int, int]:
        """解析每日推送时间，格式：HH:MM，例如 9:00 或 14:30"""
        try:
            value = str(value).strip()
            if ":" not in value:
                raise ValueError("缺少冒号")

            hour_text, minute_text = value.split(":", 1)
            hour = int(hour_text)
            minute = int(minute_text)

            if not 0 <= hour <= 23:
                raise ValueError("小时必须在 0-23 之间")
            if not 0 <= minute <= 59:
                raise ValueError("分钟必须在 0-59 之间")

            return hour, minute
        except Exception as e:
            logger.warning(
                f"每日推送时间配置无效：{value}，将回退到 14:00。错误：{e}"
            )
            return 14, 0

    def _target_group_ids(self) -> list[str]:
        raw_value = str(self._config_value("target_group_id", "") or "").strip()
        if not raw_value:
            return []
        group_ids = re.split(r"[,，;；\s]+", raw_value)
        return list(dict.fromkeys(group_id for group_id in group_ids if group_id))

    def _seconds_until_next_push(self) -> float:
        now = datetime.now()
        target = now.replace(
            hour=self.push_hour,
            minute=self.push_minute,
            second=0,
            microsecond=0,
        )
        if target <= now:
            target += timedelta(days=1)
        return max((target - now).total_seconds(), 1.0)

    async def _daily_push_loop(self) -> None:
        while True:
            try:
                wait_seconds = self._seconds_until_next_push()
                logger.info(
                    f"APOD 下次定时推送将在 {wait_seconds:.0f} 秒后执行"
                )
                await asyncio.sleep(wait_seconds)

                group_ids = self._target_group_ids()
                if not group_ids:
                    logger.warning("APOD 定时推送已跳过：未配置 target_group_id")
                    continue

                payload = await self.get_apod_payload()
                card_path = await self.render_apod_card(payload)

                for group_id in group_ids:
                    if self.has_pushed_today(payload, group_id):
                        logger.info(
                            f"APOD 群 {group_id} 今日已推送，跳过重复发送"
                        )
                        continue
                    try:
                        await self._send_group(group_id, card_path)
                        self.mark_pushed(payload, group_id)
                        logger.info(f"APOD 已推送到群 {group_id}")
                    except Exception as exc:
                        logger.warning(f"APOD 推送到群 {group_id} 失败：{exc}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"APOD 定时推送任务执行失败：{exc}")
                await asyncio.sleep(60)

    async def terminate(self) -> None:
        if self._daily_task and not self._daily_task.done():
            self._daily_task.cancel()
            try:
                await self._daily_task
            except asyncio.CancelledError:
                pass
