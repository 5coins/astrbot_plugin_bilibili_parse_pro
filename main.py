# main.py
# -*- coding: utf-8 -*-

import re

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.event.filter import EventMessageType, event_message_type
from astrbot.api.star import Context, Star, register

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

# 兜底只抓 ID（卡片里可能只有 ID，不含完整链接）
BV_OR_AV_ID_PATTERN = r"(BV[0-9A-Za-z]{10}|av\d+)"


@register("bilibili_parse", "功德无量", "B站视频解析并直接发送视频（含b23短链兜底，支持卡片）", "1.3.0")
class Bilibili(Star):
   """
   Bilibili Star: Parses Bilibili video links (including short links and card messages)
   and sends the video directly.
   """

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

       # 兜底：直接在所有文本里找 BV/av，然后拼成标准视频页
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
           # 从事件中抽取链接（纯文本 + 卡片）
           matched_url = self._extract_bili_url_from_event(event)
           if not matched_url:
               return  # 不是 B 站链接，直接早退

           text = matched_url

           # 如果是短链，先展开
           if any(d in matched_url for d in ("b23.tv", "bili2233.cn")):
               text = await self._expand_url(matched_url)

           # 从（可能已展开的）URL 中提取 BV/av
           # 优先匹配 /video/BVxxxxxx 或 /video/avxxxxxx
           m_bvid = re.search(r"/video/(BV[0-9A-Za-z]{10}|av\d+)", text)
           if not m_bvid:
               # 有些重定向会落到 ?bvid= 的中间页，这里再兜一层
               m_id = re.search(BV_OR_AV_ID_PATTERN, text)
               if m_id:
                   bvid = m_id.group(0)
               else:
                   # 仍未匹配上，给出提示
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
           # size_str = self._fmt_size(info.get("video_size", 0))
           # quality = info.get("quality", "未知清晰度")
           # comment = info.get("comment", "")

           # 说明文本（有的平台不显示 caption，所以单独补发一条）
           caption = (
               f"🎬 标题: {title}\n"
               # f"📦 大小: {size_str}\n"
               # f"👓 清晰度: {quality}\n"
               # f"💬 弹幕: {comment}\n"
               # f"🔗 直链: {video_url}" # 直链可能过长，且不安全，不建议直接发送
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

