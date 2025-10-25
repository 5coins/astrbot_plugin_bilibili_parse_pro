import os
import re
# import requests # 移除requests，替换为aiohttp
import aiohttp # 导入aiohttp
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import AstrBotConfig
from astrbot.api import logger # 使用 AstrBot 的 logger
from astrbot.api.event.filter import event_message_type, EventMessageType
from astrbot.api.message_components import *
from astrbot.api.message_components import Video
# 正则表达式模式
BILI_VIDEO_PATTERN = r"(https?:\/\/)?www\.bilibili\.com\/video\/(BV\w+|av\d+)\/?"

@register("bilibili_parse", "功德无量", "一个哔哩哔哩视频解析插件", "1.0.0")
class Bilibili(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    async def get(self, url: str):
        """发送 GET 请求并返回响应 (使用 aiohttp)"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    response.raise_for_status()  # 检查请求是否成功
                    return await response.json()  # 返回 JSON 格式的响应
        except aiohttp.ClientError as e:
            logger.error(f"Bilibili插件 HTTP请求错误: {e}")
            return None
        except Exception as e:
            logger.error(f"Bilibili插件 GET请求未知错误: {e}")
            return None

    @staticmethod
    def get_file_size(size_in_bytes: int):
        """将字节转换为可读的文件大小格式"""
        units = ['B', 'KB', 'MB', 'GB', 'TB']
        index = 0
        size = size_in_bytes

        while size >= 1024 and index < len(units) - 1:
            size /= 1024
            index += 1

        return f"{size:.2f} {units[index]}"

    async def get_video_info(self, bvid: str, accept: int):
        """获取 Bilibili 视频信息"""
        # 注意：你的API地址中 accept 参数是写死的 80，这里 qn 变量实际上没有被用到
        # 如果你想让 accept 参数生效，需要修改你的 API 端点或这里的调用
        # quality_map = {
        #     '1080': 80,
        #     '720': 64,
        #     '480': 32,
        #     '360': 16,
        # }
        # qn = quality_map.get(accept, 80) # 默认为 1080p

        try:
            # 你的 API 地址中 accept=80 是硬编码的，如果需要动态清晰度，需要修改 API 或此处的 URL
            json_data = await self.get(f'http://114.134.188.188:3003/api?bvid={bvid}&accept=80')
            
            if json_data is None:
                return {'code': -1, 'msg': "API 请求失败，请检查网络或API服务"}
            
            if json_data.get('code') != 0:
                return {'code': -1, 'msg': json_data.get('msg', "解析失败，API返回错误")}

            # 确保 data 列表存在且不为空
            if not json_data.get('data') or not json_data['data'][0]:
                return {'code': -1, 'msg': "API返回数据结构异常，未找到视频数据"}

            video_data = json_data['data'][0]
            
            result = {
                'code': 0,
                'msg': '视频解析成功',
                'title': json_data.get('title', '未知标题'),
                'video_url': video_data.get('video_url', ''),
                'pic': json_data.get('imgurl', ''), # 封面图
                'video_size': video_data.get('video_size', 0),
                'quality': video_data.get('accept_format', '未知清晰度'),
                'comment': video_data.get('comment', '') # 弹幕链接
            }
            
            return result

        except Exception as e:
            logger.error(f"Bilibili插件 解析视频信息时发生错误: {str(e)}")
            return {'code': -1, 'msg': f"解析失败: {str(e)}"}

    @filter.regex(BILI_VIDEO_PATTERN)
    @event_message_type(EventMessageType.ALL)
    async def bilibili_parse(self, event: AstrMessageEvent):
        """处理 Bilibili 视频解析请求，并发送视频"""
        try:
            # 从事件中提取消息内容，通常是纯文本消息
            # 如果消息是混合类型，可能需要更复杂的解析
            message_text = event.message_obj.message_str 
            
            match = re.search(BILI_VIDEO_PATTERN, message_text)
            if not match:
                # 理论上 regex filter 已经处理了，这里作为双重检查
                logger.warning(f"Bilibili插件 未匹配到视频链接: {message_text}")
                return

            bvid = match.group(2)  # 提取 BV 号 (或 av 号)
            accept_quality = 80  # 默认接受的清晰度 (根据你的API，目前是硬编码)

            # 获取视频信息
            video_info = await self.get_video_info(bvid, accept_quality)

            if video_info and video_info.get('code') == 0:
                # 成功获取视频信息
                title = video_info['title']
                video_url = video_info['video_url']
                pic = video_info['pic']
                video_size_bytes = video_info['video_size']
                quality = video_info['quality']
                comment_url = video_info['comment']

                # 格式化视频大小
                formatted_video_size = self.get_file_size(video_size_bytes)
                
                # 构建视频消息的描述/标题
                caption = (
                    f"🎬 标题: {title}\n"
                    f"📖 视频大小: {formatted_video_size}\n"
                    f"👓 清晰度: {quality}\n"
                    f"🔗 视频链接: {video_url}\n" # 额外提供链接，以防直接发送视频失败
                    f"💬 弹幕链接: {comment_url}"
                )
                
                # 创建 Video 消息组件
                # AstrBot 的 Video 组件通常需要 url, title, cover_url 和 caption
                video_component = Video(
                    url=video_url,
                    title=title,
                    cover_url=pic,
                    caption=caption
                )
                
                # 发送包含 Video 组件的消息
                yield event.message_components_result([video_component])
            else:
                # 解析失败
                error_msg = video_info.get('msg', '未知解析错误') if video_info else '获取视频信息失败'
                logger.error(f"Bilibili插件 解析视频失败: {error_msg}")
                yield event.plain_result(f"解析B站视频失败: {error_msg}")

        except Exception as e:
            logger.error(f"Bilibili插件 处理消息时发生未预期错误: {str(e)}", exc_info=True)
            yield event.plain_result(f"处理B站视频链接时发生错误: {str(e)}")

