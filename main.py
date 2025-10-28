# main.py
# -*- coding: utf-8 -*-

import re
import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter  # 用于命令装饰器
from astrbot.api.event.filter import EventMessageType, event_message_type
from astrbot.api.star import Context, Star, register
from astrbot.api.all import Image, Plain, Reply

# 统一匹配：普通视频页 + b23 短链 + bili2233 兜底
# 例： https://www.bilibili.com/video/BV17x411w7KC
#     https://b23.tv/vg9xOFG
#     https://bili2233.cn/xxxxxx
BILI_LINK_PATTERN = r"(https?://)?(?:www\.)?(?:bilibili\.com/video/(BV[0-9A-Za-z]{10}|av\d+)(?:/|\?|$)|b23\.tv/[A-Za-z0-9_-]+|bili2233\.cn/[A-Za-z0-9_-]+)"

# 卡片（JSON 转义）里的链接形式
CARD_ESCAPED_LINK_PATTERN = (
    r"https:\\\\/\\\\/(?:www\\.)?(?:"
    r"bilibili\.com\\\\/video\\\\/(BV[0-9A-Za-z]{10}|av\\d+)(?:\\\\/|\\?|$)"
    r"|b23\.tv\\\\/[A-Za-z0-9_-]+"
    r"|bili2233\.cn\\\\/[A-Za-z0-9_-]+)"
)

# 兜底只抓 ID（更严格：避免把 AV1 编码等误识别为 av 号）
BV_OR_AV_ID_PATTERN = r"(BV[0-9A-Za-z]{10}|av\d{5,})"


@register("bilibili_parse", "功德无量",
          "B站视频解析并直接发送视频（含b23短链兜底，支持卡片）+ 图片回显",
          "1.4.1")
class Bilibili(Star):
    """
    解析 B 站视频链接并直接发送视频；支持 /回显图片 指令将消息中的图片原样回显。
    """

    def __init__(self, context: Context):
        super().__init__(context)

    # ---------- HTTP 工具 ----------
    async def _http_get_json(self, url: str):
        """异步 GET JSON"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=20) as resp:
                    resp.raise_for_status()
                    return await resp.json()
        except Exception as e:
            logger.error(f"[bilibili_parse] HTTP GET 失败: {e}")
            return None

    async def _expand_url(self, url: str) -> str:
        """跟随短链重定向，返回最终 URL（用于 b23.tv / bili2233.cn）"""
        try:
            if not url.startswith("http"):
                url = "https://" + url
            async with aiohttp.ClientSession() as session:
                async with session.get(url, allow_redirects=True, timeout=20) as resp:
                    return str(resp.url)
        except Exception as e:
            logger.error(f"[bilibili_parse] 短链展开失败: {e}")
            return url  # 失败则原样返回

    # ---------- 工具：去掉 JSON 转义 ----------
    @staticmethod
    def _unescape_card_url(s: str) -> str:
        """将 `\\\/` 还原为 `/`，`\\\\` 还原为 `\`。"""
        return s.replace("\\\\", "\\").replace("\\/", "/")

    # ---------- 工具：是否为“纯视频消息”（非链接/卡片） ----------
    @staticmethod
    def _is_pure_video_event(event: AstrMessageEvent) -> bool:
        parts = []
        for attr in ("message_str", "raw_message"):
            v = getattr(event, attr, None)
            if v:
                parts.append(str(v))
        msg_obj = getattr(event, "message_obj", None)
        if msg_obj is not None:
            parts.append(str(msg_obj))
            t = getattr(msg_obj, "type", None)
            if isinstance(t, str) and t.lower() == "video":
                s = " ".join(parts).lower()
                if not any(k in s for k in ("bilibili.com", "b23.tv", "bili2233.cn", " bv")):
                    return True
        s = " ".join(parts).lower()
        if any(k in s for k in ("bilibili.com", "b23.tv", "bili2233.cn", " bv")):
            return False
        if "[cq:video" in s or 'type="video"' in s or "type=video" in s or '"video"' in s:
            return True
        return False

    # ---------- 工具：从事件中抽取 B 站链接（纯文本 + 卡片） ----------
    def _extract_bili_url_from_event(self, event: AstrMessageEvent) -> str | None:
        candidates_text = []
        for attr in ("message_str",):
            v = getattr(event, attr, None)
            if v:
                candidates_text.append(v)

        msg_obj = getattr(event, "message_obj", None)
        if msg_obj is not None:
            v = getattr(msg_obj, "message_str", None)
            if v:
                candidates_text.append(v)
            candidates_text.append(str(msg_obj))

        for txt in candidates_text:
            m = re.search(BILI_LINK_PATTERN, txt)
            if m:
                url = m.group(0)
                if not url.startswith("http"):
                    url = "https://" + url
                return url

        for txt in candidates_text:
            m = re.search(CARD_ESCAPED_LINK_PATTERN, txt)
            if m:
                url = self._unescape_card_url(m.group(0))
                if url.startswith("//"):
                    url = "https:" + url
                if not url.startswith("http"):
                    url = "https://" + url
                return url

        joined_lower = " ".join(candidates_text).lower()
        allow_fallback = any(k in joined_lower for k in ("bilibili", "b23.tv", "bili2233.cn", "哔哩", "b站", " bv"))
        if allow_fallback:
            for txt in candidates_text:
                m = re.search(BV_OR_AV_ID_PATTERN, txt)
                if m:
                    return f"https://www.bilibili.com/video/{m.group(0)}"

        return None

    # ---------- 核心：取视频信息 ----------
    async def get_video_info(self, bvid: str, accept_qn: int = 80):
        api = f"http://114.134.188.188:3003/api?bvid={bvid}&accept={accept_qn}"
        data = await self._http_get_json(api)
        if not data:
            return {"code": -1, "msg": "API 请求失败"}
        if data.get("code") != 0 or not data.get("data"):
            return {"code": -1, "msg": data.get("msg", "解析失败")}

        item = data["data"][0]
        return {
            "code": 0,
            "title": data.get("title", "未知标题"),
            "video_url": item.get("video_url", ""),
            "pic": data.get("imgurl", ""),
            "video_size": item.get("video_size", 0),
            "quality": item.get("accept_format", "未知清晰度"),
            "comment": item.get("comment", ""),
        }

    # ---------- 图片：组件 -> HTTP URL ----------
    async def _component_to_http_url(self, comp) -> str | None:
        try:
            fn = getattr(comp, "convert_to_web_link", None)
            if callable(fn):
                url = await fn()
                if url:
                    logger.debug(f"[image_echo] convert_to_web_link -> {url}")
                    return url
        except Exception as e:
            logger.debug(f"[image_echo] convert_to_web_link 失败: {e}")

        for attr in ("url", "file"):
            try:
                val = getattr(comp, attr, None)
            except Exception:
                val = None
            if isinstance(val, str) and val.startswith("http"):
                logger.debug(f"[image_echo] 使用属性 {attr}: {val}")
                return val

        try:
            path_val = getattr(comp, "path", None)
            if isinstance(path_val, str) and path_val:
                img_comp = Image.fromFileSystem(path_val)
                try:
                    url = await img_comp.convert_to_web_link()
                    if url:
                        logger.debug(f"[image_echo] 本地路径转直链成功: {url}")
                        return url
                except Exception as e:
                    logger.warning(f"[image_echo] 本地路径转直链失败: {e}")
        except Exception as e:
            logger.debug(f"[image_echo] 处理本地路径失败: {e}")

        return None

    # ---------- 图片：收集消息中的所有图片 URL（含回复链） ----------
    async def _collect_image_urls_from_event(self, event: AstrMessageEvent) -> list[str]:
        urls: list[str] = []
        if hasattr(event, 'message_obj') and event.message_obj and hasattr(event.message_obj, 'message'):
            for comp in event.message_obj.message:
                if isinstance(comp, Image):
                    url = await self._component_to_http_url(comp)
                    if url:
                        urls.append(url)
                        logger.debug(f"[image_echo] 收集直发图片: {url}")
                elif isinstance(comp, Reply) and getattr(comp, 'chain', None):
                    for r_comp in comp.chain:
                        if isinstance(r_comp, Image):
                            url = await self._component_to_http_url(r_comp)
                            if url:
                                urls.append(url)
                                logger.debug(f"[image_echo] 收集引用图片: {url}")
        return urls

    # ---------- 指令：回显图片 ----------
    @filter.command("回显图片")
    async def echo_images(self, event: AstrMessageEvent):
        try:
            event.call_llm = False
        except Exception:
            pass

        logger.info("[image_echo] 收到 /回显图片 命令，尝试获取图片...")
        image_urls = await self._collect_image_urls_from_event(event)

        if not image_urls:
            yield event.plain_result("未检测到图片，请直接发送图片或引用包含图片的回复。")
            return

        response_components = [Plain(f"检测到 {len(image_urls)} 张图片，原样回显：")]
        for url in image_urls:
            response_components.append(Image.fromURL(url))
        yield event.chain_result(response_components)

    # ---------- 入口：匹配 B 站视频链接（含卡片） ----------
    @event_message_type(EventMessageType.ALL)
    async def bilibili_parse(self, event: AstrMessageEvent):
        """
        解析并发送 B 站视频：
        - 自动识别文本/卡片/短链/裸 ID；
        - 短链先展开；
        - 使用组件发送视频；若组件发送失败，退回为文本提示 + 链接（不再使用 CQ 回退）。
        """
        try:
            if self._is_pure_video_event(event):
                return

            matched_url = self._extract_bili_url_from_event(event)
            if not matched_url:
                return

            text = matched_url
            if any(d in matched_url for d in ("b23.tv", "bili2233.cn")):
                text = await self._expand_url(matched_url)

            m_bvid = re.search(r"/video/(BV[0-9A-Za-z]{10}|av\d{5,})", text)
            if not m_bvid:
                m_id = re.search(BV_OR_AV_ID_PATTERN, text)
                if m_id:
                    bvid = m_id.group(0)
                else:
                    logger.warning(f"[bilibili_parse] 无法从URL中提取BV/av ID: {text}")
                    return
            else:
                bvid = m_bvid.group(1)

            info = await self.get_video_info(bvid, 80)
            if not info or info.get("code") != 0:
                msg = info.get("msg", "解析失败") if info else "解析失败"
                yield event.plain_result(f"解析B站视频失败：{msg}")
                return

            title = info["title"]
            video_url = info["video_url"]

            # 尝试用组件发送视频；失败时仅回退为纯文本链接（不使用 CQ）
            try:
                from astrbot.api.message_components import Video
                video_comp = Video.fromURL(url=video_url)
                yield event.chain_result([video_comp])
            except Exception as send_err:
                logger.warning(f"[bilibili_parse] 组件发送失败，退回文本链接: {send_err}")
                yield event.plain_result(f"无法以原生视频发送，请使用链接观看：{video_url}")

            # 补发文字说明
            yield event.plain_result(f"🎬 标题: {title}\n")

        except Exception as e:
            logger.error(f"[bilibili_parse] 处理B站视频解析时发生未预期错误: {e}", exc_info=True)
            yield event.plain_result("解析B站视频时发生内部错误。")
