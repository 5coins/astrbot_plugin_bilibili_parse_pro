# main.py
# -*- coding: utf-8 -*-

import re
import aiohttp
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.event.filter import event_message_type, EventMessageType

# 统一匹配：普通视频页 + b23 短链
# 例： https://www.bilibili.com/video/BV17x411w7KC
#     https://b23.tv/vg9xOFG
BILI_LINK_PATTERN = r"(https?://)?(?:www\.)?(?:bilibili\.com/video/(BV\w+|av\d+)(?:/|\?|$)|b23\.tv/[A-Za-z0-9_-]+)"


@register("bilibili_parse", "功德无量", "B站视频解析并直接发送视频（含b23短链兜底）", "1.2.0")
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
        """跟随短链重定向，返回最终 URL（用于 b23.tv）"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, allow_redirects=True, timeout=20) as resp:
                    # resp.url 为最终跳转后的 URL
                    return str(resp.url)
        except Exception as e:
            logger.error(f"[bilibili_parse] 短链展开失败: {e}")
            return url  # 失败则原样返回，后续再尝试解析

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

    # ---------- 入口：匹配 B 站视频链接（含 b23.tv） ----------
    @filter.regex(BILI_LINK_PATTERN)
    @event_message_type(EventMessageType.ALL)
    async def bilibili_parse(self, event: AstrMessageEvent):
        """
        解析 B 站视频并直接发送视频：
        1) 匹配 bilibili.com/video/BV... 或 b23.tv 短链；
        2) 若为 b23.tv，先展开到最终 URL，再抽取 BV/av；
        3) 优先用 Video.fromURL + event.chain_result 发送原生视频；
        4) 若不支持，回退为 CQ:video；
        5) 最后补发文字说明（避免平台不显示 caption）。
        """
        try:
            text = event.message_obj.message_str
            m = re.search(BILI_LINK_PATTERN, text)
            if not m:
                return

            matched_url = m.group(0)

            # 如果是 b23.tv 短链，先展开
            if "b23.tv" in matched_url:
                expanded = await self._expand_url(matched_url)
                # 把展开后的 URL 作为接下来解析的文本
                text = expanded
            else:
                text = matched_url

            # 从（可能已展开的）URL 中提取 BV/av
            m_bvid = re.search(r"/video/(BV\w+|av\d+)", text)
            if not m_bvid:
                yield event.plain_result("暂不支持该链接类型（可能是番剧/直播/专栏）。仅支持普通视频页。")
                return

            bvid = m_bvid.group(1)
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

            # 说明文本（有的平台不显示 caption，所以单独补发一条）
            caption = (
                f"🎬 标题: {title}\n"
                f"📦 大小: {size_str}\n"
                f"👓 清晰度: {quality}\n"
                f"💬 弹幕: {comment}\n"
                f"🔗 直链: {video_url}"
            )

            # 1) 尝试官方组件方式发送视频
            try:
                from astrbot.api.message_components import Video
                video_comp = Video.fromURL(url=video_url)

                if hasattr(event, "chain_result"):
                    yield event.chain_result([video_comp])
                else:
                    # 2) 适配器太老，回退 CQ 码视频
                    cq = f"[CQ:video,file={video_url},cover={cover},title={title}]"
                    yield event.plain_result(cq)

            except Exception as send_err:
                # 2) 组件失败，回退 CQ 码视频
                logger.warning(f"[bilibili_parse] 组件方式发送失败，转用 CQ 码: {send_err}")
                cq = f"[CQ:video,file={video_url},cover={cover},title={title}]"
                yield event.plain_result(cq)

            # 3) 补发文字说明
            yield event.plain_result(caption)

        except Exception as e:
            logger.error(f"[bilibili_parse] 处理异常: {e}", exc_info=True)
            yield event.plain_result(f"处理B站视频链接时发生错误: {e}")
