# main.py
# -*- coding: utf-8 -*-

import re
import aiohttp
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.event.filter import event_message_type, EventMessageType

# ========== 正则：纯文本直链/ID ==========
# - 普通视频页
# - 短链 b23 / bili2233
# - 直接 BV/av ID
PLAIN_LINK_RE = re.compile(
    r"(https?://(?:www\.)?bilibili\.com/video/(BV[0-9A-Za-z]{10}|av\d+)[^ \n]*)"
    r"|"
    r"(https?://(?:www\.)?(?:b23\.tv|bili2233\.cn)/[A-Za-z0-9_-]+)"
    r"|"
    r"\b(BV[0-9A-Za-z]{10}|av\d+)\b",
    re.IGNORECASE
)

# ========== 正则：JSON 字符串里转义的短链 ==========
# 例如："https:\\/\\/b23.tv\\/abc123"
ESCAPED_CARD_LINK_RE = re.compile(
    r"https:\\\\/\\\\/(?:b23\.tv|bili2233\.cn)\\\\/[A-Za-z0-9_-]+",
    re.IGNORECASE
)

# ========== 从 URL/文本中提取 BV/av ==========
BVID_FROM_URL_RE = re.compile(r"/video/(BV[0-9A-Za-z]{10}|av\d+)", re.IGNORECASE)
BVID_DIRECT_RE  = re.compile(r"\b(BV[0-9A-Za-z]{10}|av\d+)\b", re.IGNORECASE)


@register("bilibili_parse", "功德无量", "B站视频解析并直接发送视频（含卡片短链）", "1.3.0")
class Bilibili(Star):
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
        """跟随短链重定向，返回最终 URL（优先 HEAD，再回退 GET）"""
        try:
            async with aiohttp.ClientSession() as session:
                try:
                    async with session.head(url, allow_redirects=True, timeout=15) as resp:
                        return str(resp.url)
                except Exception:
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
        except Exception:
            return "未知"
        units = ["B", "KB", "MB", "GB", "TB"]
        i = 0
        while size >= 1024 and i < len(units) - 1:
            size /= 1024
            i += 1
        return f"{size:.2f} {units[i]}"

    # ---------- 你的后端 API ----------
    async def get_video_info(self, bvid: str, accept_qn: int = 80):
        """
        通过你的代理 API 获取直链等信息。
        注意：API 参数名为 bvid，这里直接传 BV 或 av(原样)；若后端仅支持 BV，请在后端转换或在此处补充转换。
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

    # ---------- 抽取入口：同时支持纯文本 + 卡片(JSON转义) ----------
    def _extract_raw_target(self, event: AstrMessageEvent) -> str | None:
        """
        返回一个“可用于继续解析”的字符串：
        - 如果是短链：直接返回短链；
        - 如果是视频页：返回完整 URL；
        - 如果只给了 BV/av：返回该 ID；
        """
        text_plain = getattr(event, "message_str", "") or ""
        obj = getattr(event, "message_obj", None)
        obj_str = str(obj) if obj is not None else ""

        # 过滤 reply（尽量严格，避免正文误伤）
        # 你也可以根据平台的结构化字段做更精确的判断
        if re.search(r'"?reply"?', obj_str, re.IGNORECASE):
            return None

        # 1) 先看纯文本
        m_plain = PLAIN_LINK_RE.search(text_plain)
        if m_plain:
            return m_plain.group(0)

        # 2) 再看卡片 JSON 里的转义短链
        m_card = ESCAPED_CARD_LINK_RE.search(obj_str)
        if m_card:
            # 反转义："https:\\/\\/b23.tv\\/xxx" -> "https://b23.tv/xxx"
            unescaped = m_card.group(0).replace("\\\\", "\\").replace("\\/", "/")
            return unescaped

        return None

    def _extract_bvid(self, text: str) -> str | None:
        """从 URL 或任意文本中尽力提取 BV/av"""
        m = BVID_FROM_URL_RE.search(text)
        if m:
            return m.group(1)
        m2 = BVID_DIRECT_RE.search(text)
        if m2:
            return m2.group(1)
        return None

    # ---------- 入口：不再依赖 @filter.regex，只用事件回调兜底 ----------
    @event_message_type(EventMessageType.ALL)
    async def bilibili_parse(self, event: AstrMessageEvent):
        """
        解析 B 站视频并直接发送视频：
        1) 同时检查纯文本与卡片(JSON)中的链接/ID；
        2) 若为 b23/bili2233 短链，先展开再抽取 BV/av；
        3) 优先用官方组件 Video.fromURL 发送原生视频；
        4) 失败则回退 CQ:video；
        5) 最后补发文字说明（避免平台不显示 caption）。
        """
        try:
            raw = self._extract_raw_target(event)
            if not raw:
                return  # 没有任何可用信息

            # 短链需要展开
            if raw.startswith("http"):
                lower = raw.lower()
                if "b23.tv" in lower or "bili2233.cn" in lower:
                    expanded = await self._expand_url(raw)
                    base_for_parse = expanded
                else:
                    base_for_parse = raw
            else:
                # 只有 BV/av
                base_for_parse = raw

            bvid = self._extract_bvid(base_for_parse)
            if not bvid:
                await event.plain_result("暂不支持该链接类型（可能是番剧/直播/专栏）。仅支持普通视频页。")
                return

            info = await self.get_video_info(bvid, 80)
            if not info or info.get("code") != 0:
                msg = info.get("msg", "解析失败") if info else "解析失败"
                await event.plain_result(f"解析B站视频失败：{msg}")
                return

            title = info["title"]
            video_url = info["video_url"]
            cover = info["pic"]
            size_str = self._fmt_size(info.get("video_size", 0))
            quality = info.get("quality", "未知清晰度")
            # comment = info.get("comment", "")

            caption = (
                f"🎬 标题: {title}\n"
                f"📦 大小: {size_str}\n"
                f"👓 清晰度: {quality}\n"
                # f"💬 弹幕: {comment}\n"
                # f"🔗 直链: {video_url}\n"
            )

            # 1) 尝试官方组件方式发送视频
            try:
                from astrbot.api.message_components import Video
                video_comp = Video.fromURL(url=video_url)

                if hasattr(event, "chain_result"):
                    # 官方组件 + 另行补发文本（有的平台不显示 caption）
                    async for _ in event.chain_result([video_comp]):
                        pass
                else:
                    # 2) 适配器太老，回退 CQ 码视频
                    cq = f"[CQ:video,file={video_url},cover={cover},title={title}]"
                    await event.plain_result(cq)

            except Exception as send_err:
                # 2) 组件失败，回退 CQ 码视频
                logger.warning(f"[bilibili_parse] 组件方式发送失败，转用 CQ 码: {send_err}")
                cq = f"[CQ:video,file={video_url},cover={cover},title={title}]"
                await event.plain_result(cq)

            # 3) 补发文字说明
            await event.plain_result(caption)

        except Exception as e:
            logger.error(f"[bilibili_parse] 处理异常: {e}", exc_info=True)
            await event.plain_result(f"处理B站视频链接时发生错误: {e}")
