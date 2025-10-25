# main.py
# -*- coding: utf-8 -*-

import re
import aiohttp
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.event.filter import event_message_type, EventMessageType

# 统一匹配：普通视频页 + b23 短链（用于文本里直接出现的情况）
# 例： https://www.bilibili.com/video/BV17x411w7KC
#     https://b23.tv/vg9xOFG
BILI_LINK_PATTERN = r"(https?://)?(?:www\.)?(?:bilibili\.com/video/(BV\w+|av\d+)(?:/|\?|$)|b23\.tv/[A-Za-z0-9_-]+)"

# 这个是“卡片消息里那种转义后的短链”：
# 例： https:\/\/b23.tv\/vg9xOFG
CARD_ESCAPED_PATTERN = r"https:\\\\/\\\\/(?:b23\.tv|bili2233\.cn)\\\\/[A-Za-z0-9_-]+"


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

    # ---------- 从事件中提取 B站链接（含卡片/转义 JSON） ----------
    def _extract_bili_url(self, event: AstrMessageEvent):
        """
        返回一个可解析的 B站 URL（可能是 bilibili.com/video/... 或 b23.tv/...）。
        支持两种来源：
        1. 用户直接发的文本消息
        2. 平台的分享卡片（JSON 里带的 https:\/\/b23.tv\/xxxx 这种转义短链）
        """
        # 1. 普通文本（优先）
        #   - event.message_obj.message_str: AstrBot 解析后消息
        #   - event.message_str: 某些适配器上是原始整串
        text_plain = getattr(event.message_obj, "message_str", "") or getattr(event, "message_str", "")
        if text_plain:
            m_plain = re.search(BILI_LINK_PATTERN, text_plain)
            if m_plain:
                return m_plain.group(0)

        # 2. 分享卡片等富文本，通常是 JSON，URL 被转义成 https:\/\/b23.tv\/xxxx
        message_obj_str = str(event.message_obj)
        # 有些平台会把引用/回复也塞进来，如果是回复内容就可以选择跳过
        # （跟你之前的逻辑保持一致，不想触发就直接 return None）
        if re.search(r"reply", message_obj_str, flags=re.IGNORECASE):
            # 如果你希望“回复里的卡片也解析”，可以删掉这段 early return
            pass

        m_card = re.search(CARD_ESCAPED_PATTERN, message_obj_str)
        if m_card:
            raw = m_card.group(0)
            # 还原转义：
            #   https:\\/\\/b23.tv\\/abc123
            # -> https://b23.tv/abc123
            fixed = (
                raw.replace("\\\\", "\\")  # 把 `\\` -> `\`
                   .replace("\\/", "/")    # 把 `\/` -> `/`
                   .replace("\\:", ":")    # 万一出现 `\:` 也顺手修复
                   .replace("\\", "")      # 最后把多余的反斜杠去掉，得到标准 URL
            )
            return fixed

        return None

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

    # ---------- 入口：匹配 B 站视频链接（含 b23.tv、卡片转义） ----------
    @filter.regex(BILI_LINK_PATTERN)
    @event_message_type(EventMessageType.ALL)
    async def bilibili_parse(self, event: AstrMessageEvent):
        """
        解析 B 站视频并直接发送视频：
        1) 从纯文本或卡片(JSON转义)中提取 bilibili.com/video/... 或 b23.tv/...；
        2) 若为 b23.tv 短链，先展开到最终 URL，再抽取 BV/av；
        3) 调你的代理 API 拿直链；
        4) 优先用 Video.fromURL 直接发视频，不行就回退 CQ 码；
        5) 最后补一条文字（有的平台视频消息不显示文字）。
        """
        try:
            # ① 拿到一个候选链接（可能是普通链接，也可能是从卡片解析出来的短链）
            matched_url = self._extract_bili_url(event)
            if not matched_url:
                return

            # ② 如果是 b23.tv 短链，先跟随跳转拿真实视频页 URL
            if "b23.tv" in matched_url:
                expanded = await self._expand_url(matched_url)
                text_for_bvid = expanded
            else:
                text_for_bvid = matched_url

            # ③ 从最终 URL 里抽出 BV/av
            #    只支持普通视频页，不处理番剧/直播等
            m_bvid = re.search(r"/video/(BV\w+|av\d+)", text_for_bvid)
            if not m_bvid:
                yield event.plain_result("暂不支持该链接类型（可能是番剧/直播/专栏）。仅支持普通视频页。")
                return

            bvid = m_bvid.group(1)

            # ④ 调你的后端 API 获取播放直链等信息
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

            # ⑤ 文字说明（单独再发，避免有的平台丢 caption）
            caption = (
                f"🎬 标题: {title}\n"
                # f"📦 大小: {size_str}\n"
                # f"👓 清晰度: {quality}\n"
                # f"💬 弹幕: {comment}\n"
                # f"🔗 直链: {video_url}"
            )

            # ⑥ 先尝试用官方组件发视频
            try:
                from astrbot.api.message_components import Video
                video_comp = Video.fromURL(url=video_url)

                if hasattr(event, "chain_result"):
                    # AstrBot 新接口：链式发送
                    yield event.chain_result([video_comp])
                else:
                    # ⑦ 老适配器：退回 CQ 码
                    cq = f"[CQ:video,file={video_url},cover={cover},title={title}]"
                    yield event.plain_result(cq)

            except Exception as send_err:
                # 组件失败，兜底 CQ 码
                logger.warning(f"[bilibili_parse] 组件方式发送失败，转用 CQ 码: {send_err}")
                cq = f"[CQ:video,file={video_url},cover={cover},title={title}]"
                yield event.plain_result(cq)

            # ⑧ 最后补一条文字信息
            yield event.plain_result(caption)

        except Exception as e:
            logger.error(f"[bilibili_parse] 处理异常: {e}", exc_info=True)
            yield event.plain_result(f"处理B站视频链接时发生错误: {e}")
