# main.py
# -*- coding: utf-8 -*-

import re
import aiohttp
from urllib.parse import urlparse

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.event.filter import event_message_type, EventMessageType

# 触发规则：同时匹配 bilibili.com/m.bilibili.com 的视频页 & b23 短链
TRIGGER_PATTERN = (
    r"(https?://)?(?:www\.)?(?:m\.)?bilibili\.com/video/(?:BV[a-zA-Z0-9]+|av\d+)"
    r"|"
    r"(https?://)?(?:b23\.tv|b23\.wtf|bili2233\.cn)/[A-Za-z0-9]+"
)

# 在正文里抓 URL 的通用正则（尽量耐受中文标点/括号包裹）
URL_GRABBER = r"https?://[^\s\]\)\}<>【】）>]+"

# 你自己的解析 API
API_TEMPLATE = "http://114.134.188.188:3003/api?bvid={bvid}&accept={qn}"


@register("bilibili_parse", "功德无量", "B站视频解析并直接发送视频（含短链+兜底）", "1.2.0")
class Bilibili(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    # -------- 网络工具 --------
    async def _http_get_json(self, url: str):
        headers = {"User-Agent": "Mozilla/5.0"}
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(url, timeout=25) as resp:
                    resp.raise_for_status()
                    return await resp.json()
        except Exception as e:
            logger.error(f"[bilibili_parse] GET JSON 失败: {e}")
            return None

    async def _get_final_url(self, url: str) -> str | None:
        """跟随重定向拿到最终 URL（用于 b23.tv 短链展开）"""
        headers = {"User-Agent": "Mozilla/5.0"}
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(url, timeout=20, allow_redirects=True) as resp:
                    # aiohttp 最终地址
                    return str(resp.url)
        except Exception as e:
            logger.warning(f"[bilibili_parse] 短链展开失败: {e}")
            return None

    # -------- 文本&URL 工具 --------
    @staticmethod
    def _sanitize_url(u: str) -> str:
        # 去除末尾可能的中文/英文括号和句读
        trailing = ")]}>）】。，、!！?？\"'“”"
        return u.rstrip(trailing)

    @staticmethod
    def _extract_urls(text: str) -> list[str]:
        return [m.group(0) for m in re.finditer(URL_GRABBER, text)]

    @staticmethod
    def _parse_bvid_from_url(url: str) -> str | None:
        # 从标准视频页提取 BV/av
        m = re.search(r"/video/(BV[a-zA-Z0-9]+|av\d+)", url)
        return m.group(1) if m else None

    # -------- 格式化 --------
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

    # -------- 业务：取视频信息 --------
    async def get_video_info(self, bvid: str, accept_qn: int = 80):
        api = API_TEMPLATE.format(bvid=bvid, qn=accept_qn)
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

    # -------- 入口：识别 & 发送 --------
    @filter.regex(TRIGGER_PATTERN)
    @event_message_type(EventMessageType.ALL)
    async def bilibili_parse(self, event: AstrMessageEvent):
        """
        支持：
        - 文案包裹/中文括号
        - b23.tv 短链自动展开
        - bilibili.com / m.bilibili.com 视频页
        - 组件方式发视频；失败回退 CQ:video；再补文字说明
        """
        try:
            text = event.message_obj.message_str

            # 1) 从整段文本抓出所有 URL
            urls = [self._sanitize_url(u) for u in self._extract_urls(text)]
            if not urls:
                return  # 没 URL，忽略

            bvid = None
            final_video_page = None

            # 2) 逐个 URL 判断：若是短链先展开，否则直接解析 BV/av
            for u in urls:
                host = urlparse(u).netloc.lower()
                if any(x in host for x in ["b23.tv", "b23.wtf", "bili2233.cn"]):
                    expanded = await self._get_final_url(u)
                    if expanded:
                        bvid = self._parse_bvid_from_url(expanded)
                        final_video_page = expanded
                else:
                    bvid = self._parse_bvid_from_url(u)
                    final_video_page = u

                if bvid:  # 找到就停
                    break

            if not bvid:
                yield event.plain_result("没识别到 B 站视频链接（短链可能失效或非视频页）。请直接发送视频页链接或有效短链。")
                return

            # 3) 拉取直链
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
                f"🔗 页面: {final_video_page or '未知'}\n"
                f"🔗 直链: {video_url}"
            )

            # 4) 优先用组件方式发视频；失败回退 CQ 码；最后补发说明文字
            try:
                from astrbot.api.message_components import Video
                comp = Video.fromURL(url=video_url)

                if hasattr(event, "chain_result"):
                    yield event.chain_result([comp])
                else:
                    cq = f"[CQ:video,file={video_url},cover={cover},title={title}]"
                    yield event.plain_result(cq)

            except Exception as send_err:
                logger.warning(f"[bilibili_parse] 组件方式发送失败，转用 CQ 码: {send_err}")
                cq = f"[CQ:video,file={video_url},cover={cover},title={title}]"
                yield event.plain_result(cq)

            # 补发文本说明（避免某些平台不显示 caption）
            yield event.plain_result(caption)

        except Exception as e:
            logger.error(f"[bilibili_parse] 处理异常: {e}", exc_info=True)
            yield event.plain_result(f"处理B站视频链接时发生错误: {e}")
