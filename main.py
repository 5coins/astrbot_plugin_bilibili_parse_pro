# main.py
# -*- coding: utf-8 -*-

import re
import json
import aiohttp
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.event.filter import event_message_type, EventMessageType

# 统一匹配：普通视频页 + b23 短链
# 例： https://www.bilibili.com/video/BV17x411w7KC
#     https://b23.tv/vg9xOFG
BILI_LINK_PATTERN = r"(https?://)?(?:www\.)?(?:bilibili\.com/video/(BV\w+|av\d+)(?:/|\?|$)|b23\.tv/[A-Za-z0-9_-]+)"

CARD_LIKE_TYPES = {"json", "xml", "card", "app", "ark", "rich", "share"}

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

    # ---------- 工具：安全 JSON 序列化 ----------
    @staticmethod
    def _json_default(obj):
        try:
            return obj.__dict__
        except Exception:
            try:
                return str(obj)
            except Exception:
                return "<unserializable>"

    @staticmethod
    def _segment_to_dict(seg):
        """
        将消息片段统一为 {type, data, repr} 的可序列化结构，尽量不丢信息。
        兼容 dict / 具有 to_dict / 具有 __dict__ 的对象。
        """
        # 原本就是 dict
        if isinstance(seg, dict):
            t = (seg.get("type") or seg.get("_type") or "unknown")
            return {
                "type": str(t),
                "data": {k: v for k, v in seg.items() if k not in {"type", "_type"}},
            }

        # 组件自带 to_dict
        to_dict = getattr(seg, "to_dict", None)
        if callable(to_dict):
            try:
                d = to_dict()
                if isinstance(d, dict):
                    t = d.get("type") or d.get("_type") or type(seg).__name__
                    return {
                        "type": str(t),
                        "data": {k: v for k, v in d.items() if k not in {"type", "_type"}},
                    }
            except Exception:
                pass

        # 兜底：读常见属性
        t = getattr(seg, "type", None) or getattr(seg, "_type", None) or type(seg).__name__
        data = {}
        for key in ("data", "attrs", "payload", "content", "extra"):
            if hasattr(seg, key):
                try:
                    data[key] = getattr(seg, key)
                except Exception:
                    pass

        # 再兜底：塞进 __dict__
        try:
            if not data:
                data = getattr(seg, "__dict__", {})
        except Exception:
            data = {}

        return {"type": str(t), "data": data, "repr": repr(seg)}

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
                # f"📦 大小: {size_str}\n"
                # f"👓 清晰度: {quality}\n"
                # f"💬 弹幕: {comment}\n"
                # f"🔗 直链: {video_url}"
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

    # ---------- 新增：消息 dump 接收器 ----------
    @filter.regex(r".*", flags=0)  # 尽量匹配所有文本（包含空串）
    @event_message_type(EventMessageType.ALL)
    async def dump_any_message(self, event: AstrMessageEvent):
        """
        功能：
        - 将所有消息的结构规范化后写入日志，便于后台分析；
        - 当检测到疑似卡片/富文本时，直接回显精简 JSON（超长自动截断）；
        - 若文本以 '#dump' 开头，则无条件回显本次消息的结构（不论是否卡片）。
        """
        try:
            msg_obj = getattr(event, "message_obj", None)
            if not msg_obj:
                return

            message_str = getattr(msg_obj, "message_str", "") or ""
            chain = getattr(msg_obj, "message_chain", None)
            # 统一成 list
            if chain is None:
                chain = []

            norm_chain = []
            try:
                for seg in chain:
                    norm_chain.append(self._segment_to_dict(seg))
            except Exception as seg_err:
                logger.warning(f"[dump] 规范化消息片段失败: {seg_err}")

            raw = {
                "meta": {
                    "platform": getattr(getattr(event, "adapter", None), "platform", None),
                    "message_id": getattr(msg_obj, "message_id", None),
                    "user_id": getattr(msg_obj, "user_id", None) or getattr(msg_obj, "sender_id", None),
                    "group_id": getattr(msg_obj, "group_id", None) or getattr(msg_obj, "channel_id", None),
                    "room_id": getattr(msg_obj, "room_id", None),
                },
                "message_str": message_str,
                "message_chain": norm_chain,
                "extra": getattr(msg_obj, "extra", None),
                "raw_event": getattr(event, "raw_event", None),
            }

            # 后台日志：完整但不过分冗长
            try:
                logger.info("[dump] 收到消息结构: " + json.dumps(raw, ensure_ascii=False, default=self._json_default)[:16000])
            except Exception as log_err:
                logger.warning(f"[dump] 打印日志失败: {log_err}")

            # 检测是否是疑似卡片/富文本
            def _looks_like_card(seg: dict) -> bool:
                t = str(seg.get("type", "")).lower()
                if any(k in t for k in CARD_LIKE_TYPES):
                    return True
                # 次级特征：data 里包含明显的 json/xml 字段
                data = seg.get("data") or {}
                if isinstance(data, dict):
                    # 常见键名探测
                    keys = "json xml app template config meta payload data content"
                    for k in keys.split():
                        if k in data:
                            return True
                return False

            has_card = any(_looks_like_card(s) for s in norm_chain)

            # 手动命令：#dump
            manual_dump = message_str.strip().lower().startswith("#dump")

            # 仅在卡片或手动 dump 时回显，避免刷屏
            if has_card or manual_dump:
                text = json.dumps(raw, ensure_ascii=False, indent=2, default=self._json_default)
                limit = 3800  # 避免超过平台消息长度
                suffix = ""
                if len(text) > limit:
                    text = text[:limit]
                    suffix = "\n...（已截断，完整请看后台日志）"
                title = "收到卡片/富文本消息，原始结构如下：" if has_card else "手动 #dump：本次消息结构如下："
                yield event.plain_result(f"{title}\n```json\n{text}\n```{suffix}")

        except Exception as e:
            logger.error(f"[dump] 处理异常: {e}", exc_info=True)
            # 为了安全，dump 出错默认不回显，避免循环触发
            # 如需提示，可解除下一行注释：
            # yield event.plain_result(f"dump 消息时发生错误: {e}")
