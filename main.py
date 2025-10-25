# main.py
# -*- coding: utf-8 -*-

import re
from urllib.parse import urlparse
import aiohttp
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.event.filter import event_message_type, EventMessageType

# ---- 触发 & 提取用的正则 ----
# 触发器：消息里只要包含 bilibili.com 或 b23.tv（带文案也能触发；(?i) 忽略大小写）
TRIGGER_RE = r"(?i)(?:bilibili\.com|b23\.tv)"

# 文本中提取 URL
URL_RE = r"https?://[^\s]+"

# 从最终链接里提取 BV/av
BILI_VIDEO_URL_RE = r"(?i)(?:https?://)?(?:www\.|m\.)?bilibili\.com/video/(BV\w+|av\d+)"

@register("bilibili_parse", "功德无量", "B站视频解析并直接发送（支持b23短链/带文案/命令兜底）", "1.3.0")
class Bilibili(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        logger.info("[bilibili_parse] 插件初始化完成")

    # ---------- HTTP 工具 ----------
    async def _http_get_json(self, url: str):
        try:
            logger.info(f"[bilibili_parse] GET JSON -> {url}")
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=20) as resp:
                    resp.raise_for_status()
                    j = await resp.json()
                    logger.info(f"[bilibili_parse] GET JSON OK, code={j.get('code')}")
                    return j
        except Exception as e:
            logger.error(f"[bilibili_parse] HTTP GET 失败: {e}")
            return None

    async def _follow_redirect(self, url: str) -> str:
        """跟随短链重定向，返回最终URL（用于 b23.tv）"""
        try:
            logger.info(f"[bilibili_parse] 跟随短链: {url}")
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=20, allow_redirects=True) as resp:
                    final_url = str(resp.url)
                    logger.info(f"[bilibili_parse] 短链最终URL: {final_url}")
                    return final_url
        except Exception as e:
            logger.warning(f"[bilibili_parse] 短链跳转失败: {url} -> {e}")
            return url  # 失败就用原始URL

    # ---------- 工具：文件大小格式化 ----------
    @staticmethod
    def _fmt_size(raw) -> str:
        try:
            size = int(raw)
        except Exception:
            return "未知"
        units = ["B", "KB", "MB", "GB", "TB"]
        i = 0
        while size >= 1024 and i < len(units) - 1:
            size /= 1024
            i += 1
        return f"{size:.2f} {units[i]}"

    # ---------- 核心：取视频信息 ----------
    async def get_video_info(self, bvid: str, accept_qn: int = 80):
        """
        调用你的代理 API 获取直链等信息。
        如果后端仅支持 BV，请确保这里传的是 BV。
        """
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

    # ---------- 从文本中找出可用的 bvid ----------
    async def _extract_bvid_from_text(self, text: str) -> str | None:
        """从任意文案中提取URL，处理b23短链，返回 BV... 或 av..."""
        urls = re.findall(URL_RE, text)
        logger.info(f"[bilibili_parse] 提取URL: {urls}")
        if not urls:
            return None

        for u in urls:
            try:
                host = urlparse(u).hostname or ""
            except Exception:
                host = ""

            final_url = u
            # b23.tv 短链：需要跟随重定向拿最终链接
            if "b23.tv" in host.lower():
                final_url = await self._follow_redirect(u)

            # 在最终URL中提取 bvid
            m = re.search(BILI_VIDEO_URL_RE, final_url)
            if m:
                bvid = m.group(1)  # BV... 或 av...
                logger.info(f"[bilibili_parse] 命中视频ID: {bvid}")
                return bvid

        logger.info("[bilibili_parse] 未从URL中提取到视频ID")
        return None

    # ---------- 通用处理 ----------
    async def _handle_text(self, event: AstrMessageEvent, text: str):
        bvid = await self._extract_bvid_from_text(text)
        if not bvid:
            yield event.plain_result("没有在这条消息里找到可解析的B站视频链接哦～")
            return

        info = await self.get_video_info(bvid, 80)
        if not info or info.get("code") != 0:
            msg = info.get("msg", "解析失败") if info else "解析失败"
            yield event.plain_result(f"解析B站视频失败：{msg}")
            return

        title = info["title"]
        video_url = info["video_url"]
        cover = info["pic"]
        size_str = self._fmt_size(info.get("video_size", 0))
        quality = info.get("quality", "未知清晰度")
        comment = info.get("comment", "")

        caption = (
            f"🎬 标题: {title}\n"
            f"📦 大小: {size_str}\n"
            f"👓 清晰度: {quality}\n"
            f"💬 弹幕: {comment}\n"
            f"🔗 直链: {video_url}"
        )

        # 1) 尝试官方组件发送视频（多数适配器支持）
        try:
            from astrbot.api.message_components import Video
            comp = Video.fromURL(url=video_url)
            if hasattr(event, "chain_result"):
                logger.info("[bilibili_parse] 使用 chain_result 发送 Video 组件")
                yield event.chain_result([comp])
            else:
                # 2) 极老适配器：回退 CQ 码
                logger.info("[bilibili_parse] 无 chain_result，回退 CQ:video")
                cq = f"[CQ:video,file={video_url},cover={cover},title={title}]"
                yield event.plain_result(cq)
        except Exception as send_err:
            # 2) 组件失败：回退 CQ 码
            logger.warning(f"[bilibili_parse] 组件方式发送失败，回退CQ:video：{send_err}")
            cq = f"[CQ:video,file={video_url},cover={cover},title={title}]"
            yield event.plain_result(cq)

        # 3) 补发说明文字（避免某些平台不显示 caption）
        yield event.plain_result(caption)

    # ---------- 入口A：消息里包含 bilibili/b23（带文案也能触发） ----------
    @filter.regex(TRIGGER_RE)
    @event_message_type(EventMessageType.ALL)
    async def bilibili_regex(self, event: AstrMessageEvent):
        try:
            text = event.message_obj.message_str
            logger.info(f"[bilibili_parse] regex 触发，收到文本: {text}")
            async for res in self._handle_text(event, text):
                yield res
        except Exception as e:
            logger.error(f"[bilibili_parse] 正则入口异常: {e}", exc_info=True)
            yield event.plain_result(f"处理B站视频链接时发生错误: {e}")

    # ---------- 入口B：命令兜底 /bili <链接或文案> ----------
    @filter.command("bili")
    async def bili_cmd(self, event: AstrMessageEvent):
        try:
            text = event.message_obj.message_str
            # 去掉命令头 "/bili"（不同协议可能是 "bili" 或 "/bili"，这里做个保险切分）
            cleaned = re.sub(r"^\s*/?bili\s*", "", text, flags=re.IGNORECASE)
            logger.info(f"[bilibili_parse] 命令触发，参数: {cleaned}")
            async for res in self._handle_text(event, cleaned or text):
                yield res
        except Exception as e:
            logger.error(f"[bilibili_parse] 命令入口异常: {e}", exc_info=True)
            yield event.plain_result(f"处理B站视频链接时发生错误: {e}")
