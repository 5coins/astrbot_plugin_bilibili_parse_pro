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
# 触发器：只要文本里出现 bilibili.com 或 b23.tv 就触发
TRIGGER_RE = r"(?:bilibili\.com|b23\.tv)"

# 文本中提取 URL
URL_RE = r"https?://[^\s]+"

# 从最终链接里提取 BV/av
BILI_VIDEO_URL_RE = r"(?:https?://)?(?:www\.|m\.)?bilibili\.com/video/(BV\w+|av\d+)"

@register("bilibili_parse", "功德无量", "B站视频解析并直接发送（支持b23短链与带文案）", "1.2.0")
class Bilibili(Star):
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
            logger.error(f"[bilibili_parse] HTTP GET JSON失败: {e}")
            return None

    async def _follow_redirect(self, url: str) -> str:
        """跟随短链重定向，返回最终URL（用于 b23.tv）"""
        try:
            async with aiohttp.ClientSession() as session:
                # 只需要拿最终URL，不关心正文
                async with session.get(url, timeout=20, allow_redirects=True) as resp:
                    return str(resp.url)
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
        通过你的代理 API 获取直链等信息。
        注意：如果后端只支持 BV，请确保传的是 BV；如果支持 av 也可原样传。
        """
        api = f"http://114.134.188.188:3003/api?bvid={bvid}&accept={accept_qn}"
        data = await self._http_get_json(api)
        if not data:
            return {"code": -1, "msg": "API 请求失败"}
        if data.get("code") != 0 or not data.get("data"):
            return {"code": -1, "msg": data.get("msg", "解析失败")}

        item = data["data"][0]
        # video_size 可能为字符串，这里不在此处转换，展示时再格式化
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
        if not urls:
            return None

        for u in urls:
            try:
                host = urlparse(u).hostname or ""
            except Exception:
                host = ""

            final_url = u
            # b23.tv 短链：需要跟随重定向拿最终链接
            if "b23.tv" in host:
                final_url = await self._follow_redirect(u)

            # 在最终URL中提取 bvid
            m = re.search(BILI_VIDEO_URL_RE, final_url)
            if m:
                return m.group(1)  # BV... 或 av...

        return None

    # ---------- 入口：匹配包含 bilibili/b23 的消息 ----------
    @filter.regex(TRIGGER_RE)
    @event_message_type(EventMessageType.ALL)
    async def bilibili_parse(self, event: AstrMessageEvent):
        """
        解析 B 站视频并直接发送：
        1) 文本里自动找URL；b23.tv自动跳转到最终链接；
        2) 提取 BV/av -> 调用解析API；
        3) 优先用 Video.fromURL + chain_result 发送；
           不支持则回退 CQ:video；
        4) 补发文字说明（避免平台不显示 caption）。
        """
        try:
            text = event.message_obj.message_str

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
                    yield event.chain_result([comp])
                else:
                    # 2) 极老适配器：回退 CQ 码方式
                    cq = f"[CQ:video,file={video_url},cover={cover},title={title}]"
                    yield event.plain_result(cq)

            except Exception as send_err:
                # 2) 组件失败：回退 CQ 码
                logger.warning(f"[bilibili_parse] 组件方式发送失败，回退CQ:video：{send_err}")
                cq = f"[CQ:video,file={video_url},cover={cover},title={title}]"
                yield event.plain_result(cq)

            # 3) 补发说明文字（避免某些平台不显示 caption）
            yield event.plain_result(caption)

        except Exception as e:
            logger.error(f"[bilibili_parse] 处理异常: {e}", exc_info=True)
            yield event.plain_result(f"处理B站视频链接时发生错误: {e}")
