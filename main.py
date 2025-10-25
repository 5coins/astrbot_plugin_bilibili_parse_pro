# main.py
# -*- coding: utf-8 -*-

import re
import aiohttp
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.event.filter import event_message_type, EventMessageType

# 触发：支持 b23.tv 短链 & bilibili.com/video 链接（消息里带文案也能匹配）
BILI_TRIGGER_PATTERN = (
    r"(?:https?://)?(?:www\.)?bilibili\.com/video/(?:BV\w+|av\d+)(?:[/?#].*)?"
    r"|https?://b23\.tv/[A-Za-z0-9]+"
)

# 提取 BV/av 的正则
BVID_IN_URL = re.compile(r"/video/(BV\w+|av\d+)", re.I)
BVID_BARE = re.compile(r"\b(BV[0-9A-Za-z]{10,})\b")  # 文本里裸 BV 码（可选）


@register("bilibili_parse", "功德无量", "B站视频解析并直接发送视频（b23短链与文案友好）", "1.2.0")
class Bilibili(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    # ========== HTTP ==========

    async def _http_get_json(self, url: str):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=20) as resp:
                    resp.raise_for_status()
                    return await resp.json()
        except Exception as e:
            logger.error(f"[bilibili_parse] HTTP GET 失败: {e}")
            return None

    async def _resolve_b23(self, url: str) -> str | None:
        """解析 b23.tv 短链到最终跳转后的URL（通常是 bilibili.com/video/...）"""
        try:
            async with aiohttp.ClientSession() as session:
                # 先尝试不跟随跳转拿 Location
                async with session.get(url, allow_redirects=False, timeout=15) as resp:
                    # 30x 才会带 Location
                    if 300 <= resp.status < 400:
                        loc = resp.headers.get("Location")
                        if loc:
                            return loc
                # 兜底：跟随跳转，直接取最终URL
                async with session.get(url, allow_redirects=True, timeout=20) as resp2:
                    return str(resp2.url)
        except Exception as e:
            logger.warning(f"[bilibili_parse] 解析 b23 短链失败: {e}")
            return None

    # ========== 工具 ==========

    @staticmethod
    def _fmt_size(raw) -> str:
        try:
            size = int(raw)
        except Exception:
            return "未知"
        if size <= 0:
            return "未知"
        units = ["B", "KB", "MB", "GB", "TB"]
        i = 0
        while size >= 1024 and i < len(units) - 1:
            size /= 1024
            i += 1
        return f"{size:.2f} {units[i]}"

    async def _extract_bvid(self, text: str) -> str | None:
        """
        从任意带文案的文本中提取 BV/av：
        1) 先找 bilibili.com/video/... 里的 BV/av
        2) 再找 b23.tv/xxxx，解析跳转后再取 BV/av
        3) 兜底：文本里裸 BV 码
        """
        # 1) 直接在文本里找 bilibili.com/video 的 BV/av
        m_url = BVID_IN_URL.search(text)
        if m_url:
            return m_url.group(1)

        # 2) 查找 b23.tv 短链并解析
        m_b23 = re.search(r"https?://b23\.tv/[A-Za-z0-9]+", text)
        if m_b23:
            final_url = await self._resolve_b23(m_b23.group(0))
            if final_url:
                m_url2 = BVID_IN_URL.search(final_url)
                if m_url2:
                    return m_url2.group(1)

        # 3) 文本裸 BV 码（有时用户直接贴 BVxxxx）
        m_bare = BVID_BARE.search(text)
        if m_bare:
            return m_bare.group(1)

        return None

    async def get_video_info(self, bvid: str, accept_qn: int = 80):
        """调用你的代理 API 获取直链等信息"""
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

    # ========== 入口 ==========

    @filter.regex(BILI_TRIGGER_PATTERN)
    @event_message_type(EventMessageType.ALL)
    async def bilibili_parse(self, event: AstrMessageEvent):
        """
        支持：b23短链 + 文案、PC/移动端链接、裸BV码
        发送：优先原生视频；失败自动回退 CQ:video；补发说明文字
        """
        try:
            text = event.message_obj.message_str
            bvid = await self._extract_bvid(text)
            if not bvid:
                yield event.plain_result("没有识别到有效的 B站视频链接（已支持 b23 短链与带文案）。")
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

            # 优先：组件方式发送视频
            try:
                from astrbot.api.message_components import Video
                video_comp = Video.fromURL(url=video_url)
                if hasattr(event, "chain_result"):
                    yield event.chain_result([video_comp])
                else:
                    # 适配器过老，退回 CQ:video
                    cq = f"[CQ:video,file={video_url},cover={cover},title={title}]"
                    yield event.plain_result(cq)
            except Exception as send_err:
                logger.warning(f"[bilibili_parse] 组件方式发送失败，转用 CQ:video：{send_err}")
                cq = f"[CQ:video,file={video_url},cover={cover},title={title}]"
                yield event.plain_result(cq)

            # 补发文字说明（避免有的平台不显示 caption）
            yield event.plain_result(caption)

        except Exception as e:
            logger.error(f"[bilibili_parse] 处理异常: {e}", exc_info=True)
            yield event.plain_result(f"处理B站视频链接时发生错误: {e}")
