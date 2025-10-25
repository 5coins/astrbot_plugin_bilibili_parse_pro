# main.py
# -*- coding: utf-8 -*-

import re
import json
import traceback
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
        # ---- dump 开关（默认关闭，避免刷屏）----
        self._dump_enabled = False

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

    # ---------- 通用 JSON 序列化 / 提取工具 ----------
    @staticmethod
    def _to_jsonable(obj):
        try:
            return json.loads(json.dumps(obj, ensure_ascii=False, default=str))
        except Exception:
            try:
                return json.loads(json.dumps(getattr(obj, "__dict__", str(obj)), ensure_ascii=False, default=str))
            except Exception:
                return str(obj)

    @staticmethod
    def _maybe_get(obj, names):
        for n in names:
            if hasattr(obj, n):
                return getattr(obj, n)
        return None

    @staticmethod
    def _truncate_text(s: str, maxlen: int = 3500) -> str:
        if s is None:
            return ""
        s = str(s)
        return s if len(s) <= maxlen else (s[:maxlen] + f"\n... [truncated {len(s)-maxlen} chars]")

    def _extract_card_candidates(self, message_obj):
        """
        从消息对象里尽量揪出“卡片”相关的字段（各平台字段名可能不同）。
        """
        candidates = []
        if not message_obj:
            return candidates

        suspect_keys = {"card", "json", "xml", "ark", "template", "content", "message", "segments", "elements", "data"}

        try:
            d = {}
            if isinstance(message_obj, dict):
                d = message_obj
            else:
                d = getattr(message_obj, "__dict__", {})
                if not isinstance(d, dict):
                    d = {}

            # 平铺搜关键字段
            for k, v in list(d.items()):
                if isinstance(k, str) and k.lower() in suspect_keys:
                    candidates.append({k: v})

            # 字符串形态的 JSON/XML
            for k in ["message_str", "content", "raw", "raw_message"]:
                if hasattr(message_obj, k):
                    val = getattr(message_obj, k)
                    if isinstance(val, str) and (val.strip().startswith("{") or val.strip().startswith("<")):
                        candidates.append({k: val})

            # 分片内部再找
            for key in ["segments", "elements", "message", "data"]:
                segs = d.get(key) or getattr(message_obj, key, None)
                if isinstance(segs, list):
                    for idx, seg in enumerate(segs):
                        if isinstance(seg, dict):
                            hit = {kk: vv for kk, vv in seg.items() if isinstance(kk, str) and kk.lower() in suspect_keys}
                            if hit:
                                candidates.append({f"{key}[{idx}]": hit})
                        else:
                            candidates.append({f"{key}[{idx}]": seg})
        except Exception as e:
            logger.warning(f"[bilibili_parse][dump] 提取卡片字段失败: {e}")

        return candidates

    def _snapshot_event(self, event: AstrMessageEvent):
        """
        将关键字段做一次可序列化快照，便于日志/回显。
        """
        msg = getattr(event, "message_obj", None)
        payload = {
            "meta": {
                "platform": getattr(event, "platform", None),
                "guild_id": getattr(event, "guild_id", None),
                "channel_id": getattr(event, "channel_id", None),
                "user_id": getattr(event, "user_id", None),
                "message_id": getattr(event, "message_id", None),
                "message_type": getattr(event, "message_type", None) or getattr(event, "type", None),
            },
            "text": getattr(msg, "message_str", None),
            "message_obj": self._to_jsonable(msg),
            "raw_event": self._to_jsonable(self._maybe_get(event, ["raw_event", "raw", "original_event", "source_event"])),
            "card_candidates": self._to_jsonable(self._extract_card_candidates(msg)),
        }
        return payload

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

    # ---------- 新增：消息 dump 接收器（匹配任意消息；默认仅写日志） ----------
    @filter.regex(r"[\s\S]*")
    @event_message_type(EventMessageType.ALL)
    async def _debug_dump_any_message(self, event: AstrMessageEvent):
        """
        一个“透明”的消息接收器：
        - 当收到 '#dump on|off|show' 时，切换或回显；
        - 其他情况下若已开启 dump，则把本条消息的快照写入日志；
        - 不会影响你其它业务处理器（默认不回消息）。
        """
        try:
            text = getattr(event.message_obj, "message_str", "") or ""
            cmd = re.match(r"^\s*#dump(?:\s+(on|off|show))?\s*$", text, re.I)

            if cmd:
                action = (cmd.group(1) or "").lower()
                if action == "on":
                    self._dump_enabled = True
                    yield event.plain_result("✅ dump 已开启：后续消息将写入日志（不回消息）。")
                    return
                elif action == "off":
                    self._dump_enabled = False
                    yield event.plain_result("🟡 dump 已关闭。")
                    return
                else:  # show 或无参：仅回显当前消息
                    payload = self._snapshot_event(event)
                    pretty = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
                    pretty = self._truncate_text(pretty, 4000)
                    yield event.plain_result(f"🔎 当前消息 dump 预览（已截断）：\n```json\n{pretty}\n```")
                    # 仍然写日志
                    logger.info(f"[bilibili_parse][dump] {json.dumps(payload, ensure_ascii=False, default=str)}")
                    # 如需同时落盘，可取消注释：
                    # with open("astrbot_msg_dump.jsonl", "a", encoding="utf-8") as f:
                    #     f.write(json.dumps(payload, ensure_ascii=False) + "\n")
                    return

            # 非命令场景：若开启了 dump，则记录日志但不打扰会话
            if self._dump_enabled:
                payload = self._snapshot_event(event)
                logger.info(f"[bilibili_parse][dump] {json.dumps(payload, ensure_ascii=False, default=str)}")
                # 同步落盘可选：
                # with open("astrbot_msg_dump.jsonl", "a", encoding="utf-8") as f:
                #     f.write(json.dumps(payload, ensure_ascii=False) + "\n")

        except Exception as e:
            logger.error(f"[bilibili_parse][dump] 处理异常: {e}\n{traceback.format_exc()}")
            # 出错也尽量不打扰会话；仅在命令时回报
            text = getattr(event.message_obj, "message_str", "") or ""
            if text.strip().startswith("#dump"):
                yield event.plain_result(f"❌ dump 出错：{e}")
