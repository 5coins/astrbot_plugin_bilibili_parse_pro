# main.py
# -*- coding: utf-8 -*-

import re
import aiohttp
import asyncio # 引入 asyncio 用于 _component_to_http_url 中的 await fn()

from astrbot.api import logger, sp # 引入 sp 用于获取全局配置
from astrbot.api.event import AstrMessageEvent
from astrbot.api.event.filter import EventMessageType, event_message_type, filter # 引入 filter 用于新命令
from astrbot.api.star import Context, Star, register
from astrbot.api.all import Image, Plain, Reply # 引入 Image, Plain, Reply


# 统一匹配：普通视频页 + b23 短链 + bili2233 兜底
# 例： https://www.bilibili.com/video/BV17x411w7KC
#     https://b23.tv/vg9xOFG
#     https://bili2233.cn/xxxxxx
BILI_LINK_PATTERN = r"(https?://)?(?:www\.)?(?:bilibili\.com/video/(BV[0-9A-Za-z]{10}|av\d+)(?:/|\?|$)|b23\.tv/[A-Za-z0-9_-]+|bili2233\.cn/[A-Za-z0-9_-]+)"

# 卡片（JSON 转义）里的链接形式，如：
# https:\/\/b23.tv\/abc123 或 https:\/\/www.bilibili.com\/video\/BVxxxxxxxxxxx
CARD_ESCAPED_LINK_PATTERN = (
    r"https:\\\\/\\\\/(?:www\\.)?(?:"
    r"bilibili\.com\\\\/video\\\\/(BV[0-9A-Za-z]{10}|av\\d+)(?:\\\\/|\\?|$)"
    r"|b23\.tv\\\\/[A-Za-z0-9_-]+"
    r"|bili2233\.cn\\\\/[A-Za-z0-9_-]+)"
)

# 兜底只抓 ID（更严格：避免把 AV1 编码等误识别为 av 号）
BV_OR_AV_ID_PATTERN = r"(BV[0-9A-Za-z]{10}|av\d{5,})"


@register("bilibili_parse", "功德无量", "B站视频解析并直接发送视频（含b23短链兜底，支持卡片）", "1.3.1")
class Bilibili(Star):
    """
    Bilibili Star: Parses Bilibili video links (including short links and card messages)
    and sends the video directly.
    """

    def __init__(self, context: Context):
        super().__init__(context)
        # 为了让 Image.convert_to_web_link 工作，可能需要配置 callback_api_base
        # 这里从 bot 自身的配置中获取，或者插件配置中获取
        self.callback_api_base = context.get_config().get("callback_api_base")
        logger.info(f"Bilibili 插件初始化，callback_api_base: {self.callback_api_base}")


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
        """格式化文件大小"""
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
        """
        去除 JSON 转义字符，将 `\\\/` 还原为 `/`，`\\\\` 还原为 `\`。
        """
        # 先把 \\ 转义成 \ ，再把 \/ 还原成 /
        return s.replace("\\\\", "\\").replace("\\/", "/")

    # ---------- 工具：是否为“纯视频消息”（非链接/卡片） ----------
    @staticmethod
    def _is_pure_video_event(event: AstrMessageEvent) -> bool:
        """
        尽量宽松地判断：
        - 适配 OneBot CQ 码：[CQ:video,...]
        - 常见结构化 payload 含 "type":"video" / "msgtype":"video" / "type=video"
        - 若文本里已包含 bilibili/b23/bili2233 链接/标识，则不判定为纯视频
        """
        parts = []
        for attr in ("message_str", "raw_message"):
            v = getattr(event, attr, None)
            if v:
                parts.append(str(v))
        msg_obj = getattr(event, "message_obj", None)
        if msg_obj is not None:
            parts.append(str(msg_obj))
            # 一些适配器可能有明确的类型字段
            t = getattr(msg_obj, "type", None)
            if isinstance(t, str) and t.lower() == "video":
                s = " ".join(parts).lower()
                if not any(k in s for k in ("bilibili.com", "b23.tv", "bili2233.cn", " bv")):
                    return True

        s = " ".join(parts).lower()
        if any(k in s for k in ("bilibili.com", "b23.tv", "bili2233.cn", " bv")):
            return False  # 存在 B站痕迹，交给后续流程判定
        if "[cq:video" in s or 'type="video"' in s or "type=video" in s or '"video"' in s:
            return True
        return False

    # ---------- 工具：从事件中抽取链接（纯文本 + 卡片） ----------
    def _extract_bili_url_from_event(self, event: AstrMessageEvent) -> str | None:
        """
        从事件消息中提取 Bilibili 链接。
        尝试从纯文本、卡片字符串（JSON 转义）和兜底 BV/av ID 中匹配。
        """
        candidates_text = []

        # 1) 纯文本来源（不同适配器字段可能不一样，全都兜一下）
        for attr in ("message_str",):
            v = getattr(event, attr, None)
            if v:
                candidates_text.append(v)

        msg_obj = getattr(event, "message_obj", None)
        if msg_obj is not None:
            # astrbot 常见字段
            v = getattr(msg_obj, "message_str", None)
            if v:
                candidates_text.append(v)

            # 2) 卡片对象的字符串化（里面经常是 JSON 转义）
            candidates_text.append(str(msg_obj))

        # 先尝试在“可读文本”里找标准链接
        for txt in candidates_text:
            m = re.search(BILI_LINK_PATTERN, txt)
            if m:
                url = m.group(0)
                if not url.startswith("http"):
                    url = "https://" + url
                return url

        # 再在“卡片字符串”里找 JSON 转义链接
        for txt in candidates_text:
            m = re.search(CARD_ESCAPED_LINK_PATTERN, txt)
            if m:
                url = self._unescape_card_url(m.group(0))
                # 可能是 // 开头的，统一补齐
                if url.startswith("//"):
                    url = "https:" + url
                if not url.startswith("http"):
                    url = "https://" + url
                return url

        # 兜底：只有当文本整体包含 B站相关标识时，才识别裸 ID（避免把 AV1 编码误当 av 号）
        joined_lower = " ".join(candidates_text).lower()
        allow_fallback = any(k in joined_lower for k in ("bilibili", "b23.tv", "bili2233.cn", "哔哩", "b站", " bv"))
        if allow_fallback:
            for txt in candidates_text:
                m = re.search(BV_OR_AV_ID_PATTERN, txt)
                if m:
                    return f"https://www.bilibili.com/video/{m.group(0)}"

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

        item = data["data"][0]  # 假设只取第一个数据项
        return {
            "code": 0,
            "title": data.get("title", "未知标题"),
            "video_url": item.get("video_url", ""),
            "pic": data.get("imgurl", ""),
            "video_size": item.get("video_size", 0),
            "quality": item.get("accept_format", "未知清晰度"),
            "comment": item.get("comment", ""),
        }

    # ---------- 图片回显辅助方法 (从即梦插件中提取) ----------
    async def _component_to_http_url(self, comp) -> str | None:
        """
        尽量把任意图片组件转换为可用于对接 API 的 http(s) 链接。
        优先使用 convert_to_web_link；若缺失，则回退到属性 url/file；
        如果仅有本地 path，可尝试转为回调直链。
        """
        # 1) 新版 Image 可能有 convert_to_web_link
        try:
            fn = getattr(comp, "convert_to_web_link", None)
            if callable(fn):
                # convert_to_web_link 需要 bot 配置中提供 callback_api_base
                # 否则对于本地文件会失败
                url = await fn()
                if url:
                    logger.debug(f"[ImageEcho] Converted to web link: {url}")
                    return url
        except Exception as e:
            logger.debug(f"[ImageEcho] convert_to_web_link 失败，继续回退: {e}")

        # 2) 旧组件字段回退: url / file
        for attr in ("url", "file"):
            try:
                val = getattr(comp, attr, None)
            except Exception:
                val = None
            if isinstance(val, str) and val.startswith("http"):
                logger.debug(f"[ImageEcho] Found http(s) URL in attribute '{attr}': {val}")
                return val

        # 3) 本地路径回退（需要转直链）
        try:
            path_val = getattr(comp, "path", None)
            if isinstance(path_val, str) and path_val:
                logger.debug(f"[ImageEcho] Found local path: {path_val}")
                # 尝试再次通过 Image.fromFileSystem 构造并转换
                img_comp = Image.fromFileSystem(path_val)
                try:
                    url = await img_comp.convert_to_web_link()
                    if url:
                        logger.debug(f"[ImageEcho] Converted local path to web link: {url}")
                        return url
                except Exception as e:
                    logger.warning(f"[ImageEcho] 本地路径 {path_val} 转换为 web link 失败: {e}")
        except Exception as e:
            logger.debug(f"[ImageEcho] 处理本地路径失败: {e}")

        logger.debug("[ImageEcho] 未能将组件转换为 HTTP URL")
        return None

    async def _collect_image_urls_from_event(self, event: AstrMessageEvent) -> list[str]:
        """
        从消息事件中收集所有图片组件的 HTTP URL。
        包括直接发送的图片和回复中引用的图片。
        """
        urls: list[str] = []
        # 检查当前消息中是否包含图片
        if hasattr(event, 'message_obj') and event.message_obj and hasattr(event.message_obj, 'message'):
            for comp in event.message_obj.message:
                if isinstance(comp, Image):
                    url = await self._component_to_http_url(comp)
                    if url:
                        urls.append(url)
                        logger.debug(f"[ImageEcho] Collected direct image URL: {url}")
                # 检查是否是回复消息，并尝试从回复链中获取图片
                elif isinstance(comp, Reply) and getattr(comp, 'chain', None):
                    logger.debug("[ImageEcho] Found Reply component, checking chain...")
                    for r_comp in comp.chain:
                        if isinstance(r_comp, Image):
                            url = await self._component_to_http_url(r_comp)
                            if url:
                                urls.append(url)
                                logger.debug(f"[ImageEcho] Collected replied image URL: {url}")
        return urls

    # ---------- 新增命令：回显图片 ----------
    @filter.command("回显图片")
    async def echo_images(self, event: AstrMessageEvent):
        """
        接收图片（直接发送或引用），并将其原样回显。
        """
        try:
            event.call_llm = False # 防止 LLM 介入
        except Exception:
            pass

        logger.info(f"收到 /回显图片 命令，尝试获取图片...")

        image_urls = await self._collect_image_urls_from_event(event)

        if not image_urls:
            logger.info("[ImageEcho] 未检测到图片。")
            yield event.plain_result("未检测到图片，请直接发送图片或引用包含图片的回复。")
            return

        response_components = [Plain(f"检测到 {len(image_urls)} 张图片，正在回显：")]
        for url in image_urls:
            logger.info(f"[ImageEcho] 回显图片 URL: {url}")
            response_components.append(Image.fromURL(url))
        
        yield event.chain_result(response_components)


    # ---------- 入口：匹配 B 站视频链接（含卡片） ----------
    # 重要：这里不用 @filter.regex，以便卡片消息也能进入，再在函数内做匹配与早退
    @event_message_type(EventMessageType.ALL)
    async def bilibili_parse(self, event: AstrMessageEvent):
        """
        解析 B 站视频并直接发送视频：
        1) 统一从纯文本与卡片里抽取 bilibili.com/video/BV.. | b23.tv | bili2233.cn | 兜底 BV/av；
        2) 若为短链，先展开到最终 URL，再抽取 BV/av；
        3) 优先用 Video.fromURL + event.chain_result 发送原生视频；
        4) 若不支持，回退为 CQ:video；
        5) 最后补发文字说明（避免平台不显示 caption）。
        """
        try:
            # 如果是“纯视频消息”（非链接/卡片），直接早退，不做解析
            if self._is_pure_video_event(event):
                return

            # 从事件中抽取链接（纯文本 + 卡片）
            matched_url = self._extract_bili_url_from_event(event)
            if not matched_url:
                return  # 不是 B 站链接/ID，直接早退

            text = matched_url

            # 如果是短链，先展开
            if any(d in matched_url for d in ("b23.tv", "bili2233.cn")):
                text = await self._expand_url(matched_url)

            # 从（可能已展开的）URL 中提取 BV/av
            # 优先匹配 /video/BVxxxxxx 或 /video/avxxxxxx
            m_bvid = re.search(r"/video/(BV[0-9A-Za-z]{10}|av\d{5,})", text)
            if not m_bvid:
                # 有些重定向会落到 ?bvid= 的中间页，这里再兜一层（已加严格 av 位数）
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

            # 说明文本（有的平台不显示 caption，所以单独补发一条）
            caption = (
                f"🎬 标题: {title}\n"
            )

            # 1) 尝试官方组件方式发送视频
            try:
                from astrbot.api.message_components import Video

                video_comp = Video.fromURL(url=video_url)

                if hasattr(event, "chain_result"):
                    # 使用 chain_result 发送组件，通常更原生
                    yield event.chain_result([video_comp])
                else:
                    # 2) 适配器太老，回退 CQ 码视频
                    logger.warning(
                        "[bilibili_parse] event does not have chain_result, falling back to CQ code."
                    )
                    cq = f"[CQ:video,file={video_url},cover={cover},title={title}]"
                    yield event.plain_result(cq)

            except ImportError:
                # 2) astrbot 版本过低，没有 message_components 模块
                logger.warning(
                    "[bilibili_parse] astrbot.api.message_components not found, falling back to CQ code."
                )
                cq = f"[CQ:video,file={video_url},cover={cover},title={title}]"
                yield event.plain_result(cq)
            except Exception as send_err:
                # 2) 组件发送失败，回退 CQ 码视频
                logger.warning(
                    f"[bilibili_parse] Component-based sending failed, falling back to CQ code: {send_err}"
                )
                cq = f"[CQ:video,file={video_url},cover={cover},title={title}]"
                yield event.plain_result(cq)

            # 3) 补发文字说明
            yield event.plain_result(caption)

        except Exception as e:
            logger.error(f"[bilibili_parse] 处理B站视频解析时发生未预期错误: {e}", exc_info=True)
            yield event.plain_result("解析B站视频时发生内部错误。")

