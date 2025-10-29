# main.py
# -*- coding: utf-8 -*-

import re
import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.event.filter import EventMessageType, event_message_type
from astrbot.api.star import Context, Star, register

# 统一匹配：普通视频页 + b23 短链 + bili2233 兜底
BILI_LINK_PATTERN = r"(https?://)?(?:www\.)?(?:bilibili\.com/video/(BV[0-9A-Za-z]{10}|av\d+)(?:/|\?|$)|b23\.tv/[A-Za-z0-9_-]+|bili2233\.cn/[A-Za-z0-9_-]+)"

# 卡片（JSON 转义）里的链接形式
CARD_ESCAPED_LINK_PATTERN = (
    r"https:\\\\/\\\\/(?:www\\.)?(?:"
    r"bilibili\.com\\\\/video\\\\/(BV[0-9A-Za-z]{10}|av\\d+)(?:\\\\/|\\?|$)"
    r"|b23\.tv\\\\/[A-Za-z0-9_-]+"
    r"|bili2233\.cn\\\\/[A-Za-z0-9_-]+)"
)

# 兜底只抓 ID（避免把 AV1 编码等误识别为 av 号）
BV_OR_AV_ID_PATTERN = r"(BV[0-9A-Za-z]{10}|av\d{5,})"


@register("bilibili_parse", "功德无量",
          "B站视频解析并直接发送视频（含b23短链兜底，支持卡片）",
          "1.4.1")
class Bilibili(Star):
    """
    解析 B 站视频链接并直接发送视频。
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

        # 可能的字段全部兜一遍
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

        # (1) 先找标准链接
        for txt in candidates_text:
            m = re.search(BILI_LINK_PATTERN, txt)
            if m:
                url = m.group(0)
                if not url.startswith("http"):
                    url = "https://" + url
                return url

        # (2) 再找卡片里的转义链接
        for txt in candidates_text:
            m = re.search(CARD_ESCAPED_LINK_PATTERN, txt)
            if m:
                url = self._unescape_card_url(m.group(0))
                if url.startswith("//"):
                    url = "https:" + url
                if not url.startswith("http"):
                    url = "https://" + url
                return url

        # (3) 兜底：只有当文本包含 B 站痕迹时才尝试裸 BV/av
        joined_lower = " ".join(candidates_text).lower()
        allow_fallback = any(k in joined_lower for k in (
            "bilibili", "b23.tv", "bili2233.cn", "哔哩", "b站", " bv"
        ))
        if allow_fallback:
            for txt in candidates_text:
                m = re.search(BV_OR_AV_ID_PATTERN, txt)
                if m:
                    return f"https://www.bilibili.com/video/{m.group(0)}"

        return None

    # ---------- 获取视频直链等信息 ----------
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

    # ---------- 主入口：监听所有消息，自动解析B站链接 ----------
    @event_message_type(EventMessageType.ALL)
    async def bilibili_parse(self, event: AstrMessageEvent):
        """
        解析并发送 B 站视频：
        - 自动识别文本/卡片/短链/裸 ID；
        - 短链先展开；
        - 使用 Video 组件发送视频；
        - 如果组件发送失败，就退回为纯文本链接提示（不做 CQ 回退）。
        """
        try:
            # 如果这是“纯视频消息”（群友直接发了视频文件，而不是B站链接），忽略
            if self._is_pure_video_event(event):
                return

            matched_url = self._extract_bili_url_from_event(event)
            if not matched_url:
                return  # 当前消息不是 B 站相关，直接忽略

            # 短链需要先跟踪重定向
            expanded = matched_url
            if any(d in matched_url for d in ("b23.tv", "bili2233.cn")):
                expanded = await self._expand_url(matched_url)

            # 从最终 URL 里提取 BV 号 / av 号
            m_bvid = re.search(r"/video/(BV[0-9A-Za-z]{10}|av\d{5,})", expanded)
            if not m_bvid:
                m_id = re.search(BV_OR_AV_ID_PATTERN, expanded)
                if m_id:
                    bvid = m_id.group(0)
                else:
                    logger.warning(f"[bilibili_parse] 无法从URL中提取BV/av ID: {expanded}")
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

            try:
                # 使用 AstrBot 的 Video 组件直接发视频
                from astrbot.api.message_components import Video
                video_comp = Video.fromURL(url=video_url)
                yield event.chain_result([video_comp])
            except Exception as send_err:
                # 如果目标平台不支持组件直发，就退成文本链接
                logger.warning(f"[bilibili_parse] 组件发送失败，退回文本链接: {send_err}")
                yield event.plain_result(f"无法以原生视频发送，请使用链接观看：{video_url}")

            # 补发标题（有的平台不显示 caption）
            yield event.plain_result(f"🎬 标题: {title}\n")

        except Exception as e:
            logger.error(
                f"[bilibili_parse] 处理B站视频解析时发生未预期错误: {e}",
                exc_info=True
            )
            yield event.plain_result("解析B站视频时发生内部错误。")
