# main.py
# -*- coding: utf-8 -*-

import re
import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.event.filter import EventMessageType, event_message_type
from astrbot.api.star import Context, Star, register
from astrbot.api.all import Image, Plain, Reply  # 用于图片回显

# 统一匹配：普通视频页 + b23 短链 + bili2233 兜底
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


@register("bilibili_parse", "功德无量", "B站视频解析并直接发送视频（含b23短链兜底，支持卡片）+ 图片回显", "1.4.1")
class Bilibili(Star):
    """
    解析 B 站视频并直接发送；同时支持 /回显图片（把消息里的图片原样回显）。
    全部走一个 ALL 入口，避免命令装饰器在某些适配器上失效的问题。
    """

    def __init__(self, context: Context):
        super().__init__(context)

    # ---------- HTTP 工具 ----------
    async def _http_get_json(self, url: str):
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

    # ---------- 工具：文件大小格式化 ----------
    @staticmethod
    def _fmt_size(raw) -> str:
        try:
            size = int(raw)
        except (ValueError, TypeError):
            return "未知"
        units = ["B", "KB", "MB", "GB", "TB"]
        i = 0
        while size >= 1024 and i < len(units) - 1:
            size /= 1024
            i += 1
        return f"{size:.2f} {units[i]}"

    # ---------- 工具：去掉 JSON 转义 ----------
    @staticmethod
    def _unescape_card_url(s: str) -> str:
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

    # ---------- 链接抽取（纯文本 + 卡片） ----------
    def _extract_bili_url_from_event(self, event: AstrMessageEvent):
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

    # ---------- 代理 API 获取视频信息 ----------
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

    # ---------- 图片：组件转 HTTP URL ----------
    async def _component_to_http_url(self, comp):
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
                return val

        try:
            path_val = getattr(comp, "path", None)
            if isinstance(path_val, str) and path_val:
                img_comp = Image.fromFileSystem(path_val)
                try:
                    url = await img_comp.convert_to_web_link()
                    if url:
                        return url
                except Exception as e:
                    logger.warning(f"[image_echo] 本地路径转直链失败: {e}")
        except Exception:
            pass
        return None

    # ---------- 图片：从事件收集所有图片 URL（含回复链） ----------
    async def _collect_image_urls_from_event(self, event: AstrMessageEvent):
        urls = []
        if hasattr(event, 'message_obj') and event.message_obj and hasattr(event.message_obj, 'message'):
            for comp in event.message_obj.message:
                if isinstance(comp, Image):
                    url = await self._component_to_http_url(comp)
                    if url:
                        urls.append(url)
                elif isinstance(comp, Reply) and getattr(comp, 'chain', None):
                    for r_comp in comp.chain:
                        if isinstance(r_comp, Image):
                            url = await self._component_to_http_url(r_comp)
                            if url:
                                urls.append(url)
        return urls

    # ---------- 单一入口：既处理 /回显图片，又处理 B 站解析 ----------
    @event_message_type(EventMessageType.ALL)
    async def bilibili_parse(self, event: AstrMessageEvent):
        try:
            # 1) 优先处理“/回显图片”命令（不依赖命令装饰器，稳触发）
            msg = (getattr(event, "message_str", "") or "").strip()
            if msg.startswith("/回显图片") or msg.startswith("回显图片"):
                try:
                    event.call_llm = False
                except Exception:
                    pass

                image_urls = await self._collect_image_urls_from_event(event)
                if not image_urls:
                    yield event.plain_result("未检测到图片，请直接发送图片或回复一条带图片的消息后再发 /回显图片")
                    return

                comps = [Plain(f"检测到 {len(image_urls)} 张图片，原样回显：")]
                for u in image_urls:
                    comps.append(Image.fromURL(u))
                yield event.chain_result(comps)
                return  # 回显完成后退出

            # 2) 非“回显图片”命令：处理 B 站解析
            #    如果是“纯视频消息”（非链接/卡片），则不处理（保持和你原逻辑一致）
            if self._is_pure_video_event(event):
                return

            matched_url = self._extract_bili_url_from_event(event)
            if not matched_url:
                return  # 不是 B 站相关消息，早退

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
            cover = info["pic"]
            caption = f"🎬 标题: {title}\n"

            try:
                from astrbot.api.message_components import Video
                video_comp = Video.fromURL(url=video_url)
                if hasattr(event, "chain_result"):
                    yield event.chain_result([video_comp])
                else:
                    cq = f"[CQ:video,file={video_url},cover={cover},title={title}]"
                    yield event.plain_result(cq)
            except ImportError:
                cq = f"[CQ:video,file={video_url},cover={cover},title={title}]"
                yield event.plain_result(cq)
            except Exception as send_err:
                logger.warning(f"[bilibili_parse] 组件发送失败，回退 CQ: {send_err}")
                cq = f"[CQ:video,file={video_url},cover={cover},title={title}]"
                yield event.plain_result(cq)

            yield event.plain_result(caption)

        except Exception as e:
            logger.error(f"[bilibili_parse] 处理消息时出错: {e}", exc_info=True)
            yield event.plain_result("解析/回显时发生内部错误。")
