# main.py
# -*- coding: utf-8 -*-

import re
import aiohttp
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.event.filter import event_message_type, EventMessageType

# 统一匹配：普通视频页 + b23 短链 + bili2233 兜底
BILI_LINK_PATTERN = r"(https?://)?(?:www\.)?(?:bilibili\.com/video/(BV[0-9A-Za-z]{10}|av\d+)(?:/|\?|$)|b23\.tv/[A-Za-z0-9_-]+|bili2233\.cn/[A-Za-z0-9_-]+)"
CARD_ESCAPED_LINK_PATTERN = (
    r"https:\\\\/\\\\/(?:www\\.)?(?:"
    r"bilibili\.com\\\\/video\\\\/(BV[0-9A-Za-z]{10}|av\\d+)(?:\\\\/|\\?|$)"
    r"|b23\.tv\\\\/[A-Za-z0-9_-]+"
    r"|bili2233\.cn\\\\/[A-Za-z0-9_-]+)"
)
BV_OR_AV_ID_PATTERN = r"(BV[0-9A-Za-z]{10}|av\d+)"

# 自定义的 Jinja2 模板，用于生成 Todo List 图片（支持 CSS）
TMPL_TODO = '''
<div style="font-family: -apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif; font-size: 28px; padding: 24px; line-height: 1.4;">
  <h1 style="margin: 0 0 16px; font-size: 40px; color: #111;">Todo List</h1>
  <ul style="margin: 0; padding-left: 28px;">
  {% for item in items %}
    <li style="margin: 6px 0;">{{ item }}</li>
  {% endfor %}
  </ul>
</div>
'''

# 新增：新闻卡片模板，用于排版类似文章摘要的内容
TMPL_NEWS_CARD = '''
<div style="font-family: -apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif; font-size: 18px; padding: 24px; line-height: 1.6; background-color: #fff; color: #333; max-width: 600px; margin: 0 auto; box-sizing: border-box;">
    {% if image_url %}
    <img src="{{ image_url }}" alt="Cover Image" style="width: 100%; display: block; margin-bottom: 20px; border-radius: 8px;">
    {% endif %}
    <h3 style="margin: 0 0 16px; font-size: 28px; color: #111; font-weight: bold;">{{ title }}</h3>
    <p style="margin: 0 0 16px;">{{ paragraph1 }}</p>
    <p style="margin: 0 0 16px;">{{ paragraph2 }}</p>
    {% if source %}
    <p style="margin: 0; font-size: 14px; color: #666;">来源：{{ source }}</p>
    {% endif %}
</div>
'''

# 新增：简洁新闻模板，用于排版类似“Meta 裁员”这种纯文本新闻
TMPL_NEWS_SIMPLE = '''
<div style="font-family: -apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif; font-size: 20px; line-height: 1.6; padding: 40px; max-width: 700px; margin: 0 auto; background-color: #fff; color: #333; box-sizing: border-box;">
    <h2 style="font-size: 30px; font-weight: bold; margin: 0 0 20px; text-align: center;">{{ title }}</h2>
    <p style="margin: 0 0 20px;">{{ body_text }}</p>
    {% if author %}
    <p style="margin: 0; font-size: 18px; color: #666; text-align: right;">{{ author }}</p>
    {% endif %}
</div>
'''

@register("bilibili_parse", "功德无量", "B站视频解析并直接发送视频（含b23短链兜底，支持卡片）", "1.3.0")
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

    # ---------- 工具：去掉 JSON 转义 ----------
    @staticmethod
    def _unescape_card_url(s: str) -> str:
        # 先把 \\ 转义成 \ ，再把 \/ 还原成 /
        return s.replace("\\\\", "\\").replace("\\/", "/")

    # ---------- 工具：从事件中抽取链接（纯文本 + 卡片） ----------
    def _extract_bili_url_from_event(self, event: AstrMessageEvent) -> str | None:
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

    # ---------- 入口：匹配 B 站视频链接（含卡片） ----------
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
            m_bvid = re.search(r"/video/(BV[0-9A-Za-z]{10}|av\d+)", text)
            if not m_bvid:
                # 有些重定向会落到 ?bvid= 的中间页，这里再兜一层
                m_id = re.search(BV_OR_AV_ID_PATTERN, text)
                if m_id:
                    bvid = m_id.group(0)
                else:
                    # 仍未匹配上，给出提示
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
            logger.error(f"[bilibili_parse] 处理 B 站链接时发生未知错误: {e}")
            # 可以选择在这里发送一个错误消息给用户
            # yield event.plain_result("处理 B 站链接时发生错误，请稍后再试。")

    # ---------- 新增：Todo List 命令 ----------
    @filter.command("todo")
    async def todo_card(self, event: AstrMessageEvent):
        """
        生成 Todo List 图片。

        用法：
        - 直接发送：todo
        - 或携带内容：todo 吃饭 睡觉 | 玩原神
          （支持空格、逗号/中文逗号、竖线分隔）
        """
        # 取原始消息文本
        raw = getattr(event, "message_str", None) \
              or getattr(getattr(event, "message_obj", None), "message_str", "") \
              or ""

        # 把前缀命令去掉，拿到参数部分
        m = re.search(r"^\s*todo\b(.*)$", raw, re.I | re.S)
        rest = m.group(1).strip() if m else ""

        if rest:
            # 支持多种分隔符：空格 / 英文逗号 / 中文逗号 / 竖线
            parts = re.split(r"[,\u3001\uFF0C|\s]+", rest)
            items = [p for p in parts if p]
        else:
            # 默认示例
            items = ["吃饭", "睡觉", "玩原神"]

        # 渲染 HTML -> 图片（框架自带的 html_render）
        url = await self.html_render(TMPL_TODO, {"items": items})

        # 发送图片
        yield event.image_result(url)

    # ---------- 新增：新闻卡片命令 ----------
    @filter.command("news")
    async def news_card(self, event: AstrMessageEvent):
        """
        生成一篇新闻/文章摘要的图片卡片。
        用法：
        news [图片直链]
        三星 Galaxy XR 支持轻松侧载应用且拥有开放引导程序
        三星 Galaxy XR 默认支持侧载 APK 文件，无需连接 PC 或启用开发者模式，同时还拥有开放的引导程序。这使得谷歌的 Android XR 平台成为三大独立 XR 平台中最开放的系统。相比之下，苹果的 visionOS 完全不允许侧载应用，而 Meta 的 Horizon OS 需要注册开发者账户并连接外部设备才能侧载。
        UploadVR 确认，用户可以直接在 Galaxy XR 的内置 Chrome 浏览器中下载 Android APK 文件，只需在设置中给予浏览器安装"未知应用"的权限即可安装。此外，用户甚至可以解锁设备的引导程序，理论上可以安装自定义操作系统。
        来源：UploadVR
        """
        # 提取消息内容
        raw_message = getattr(event, "message_str", "")
        # 移除命令前缀
        content = re.sub(r"^\s*news\s*", "", raw_message, flags=re.I | re.S).strip()

        # 假设内容格式为：
        # [图片直链] (可选)
        # 标题
        # 段落1
        # 段落2
        # 来源：XXX (可选)

        lines = content.split('\n')
        image_url = None
        title = ""
        paragraphs = []
        source = None

        # 尝试解析图片直链
        if lines and lines[0].startswith("http"):
            image_url = lines.pop(0).strip() # 移除第一行作为图片URL

        # 标题是下一行
        if lines:
            title = lines.pop(0).strip()

        # 剩余行作为段落或来源
        current_paragraph = []
        for line in lines:
            line = line.strip()
            if line.startswith("来源："):
                source = line[3:].strip() # 提取来源内容
                break # 来源是最后一部分，找到后停止
            elif line: # 非空行作为段落的一部分
                current_paragraph.append(line)
            else: # 空行表示段落分隔
                if current_paragraph:
                    paragraphs.append(" ".join(current_paragraph))
                    current_paragraph = []
        if current_paragraph: # 处理最后一个段落
            paragraphs.append(" ".join(current_paragraph))


        # 确保至少有两个段落，如果不足则填充空字符串
        paragraph1 = paragraphs[0] if len(paragraphs) > 0 else ""
        paragraph2 = paragraphs[1] if len(paragraphs) > 1 else ""

        # 准备渲染数据
        data = {
            "image_url": image_url,
            "title": title,
            "paragraph1": paragraph1,
            "paragraph2": paragraph2,
            "source": source,
        }

        # 渲染 HTML -> 图片
        url = await self.html_render(TMPL_NEWS_CARD, data)

        # 发送图片
        yield event.image_result(url)

    # ---------- 新增：简洁新闻卡片命令 ----------
    @filter.command("meta_news") # 使用一个不同的命令名，避免与 "news" 混淆
    async def meta_news_card(self, event: AstrMessageEvent):
        """
        生成一篇简洁新闻的图片卡片，适用于纯文本内容。
        排版模仿“Meta 裁员”的图片样式。

        用法：
        meta_news
        Meta 裁员据传基于代码提交量决定
        Hyperbolic CTO 金宇晨透露，Meta 内部人士称此次裁员是基于代码差异行数 (lines of code diffs) 来决定的。这解释了为什么构建模型的员工和新员工受到的冲击最为严重。
        金宇晨
        """
        raw_message = getattr(event, "message_str", "")
        # 移除命令前缀
        content = re.sub(r"^\s*meta_news\s*", "", raw_message, flags=re.I | re.S).strip()

        lines = content.split('\n')
        
        # 按照图片结构解析内容
        title = lines[0].strip() if len(lines) > 0 else "无标题"
        body_text = lines[1].strip() if len(lines) > 1 else "无正文"
        author = lines[2].strip() if len(lines) > 2 else None # 作者是可选的

        # 准备渲染数据
        data = {
            "title": title,
            "body_text": body_text,
            "author": author,
        }

        # 渲染 HTML -> 图片
        url = await self.html_render(TMPL_NEWS_SIMPLE, data)

        # 发送图片
        yield event.image_result(url)

