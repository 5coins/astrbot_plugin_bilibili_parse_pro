@filter.regex(BILI_VIDEO_PATTERN)
@event_message_type(EventMessageType.ALL)
async def bilibili_parse(self, event: AstrMessageEvent):
    try:
        message_text = event.message_obj.message_str
        match = re.search(BILI_VIDEO_PATTERN, message_text)
        if not match:
            return

        bvid = match.group(2)
        accept_quality = 80
        video_info = await self.get_video_info(bvid, accept_quality)

        if not video_info or video_info.get('code') != 0:
            err = video_info.get('msg', '未知解析错误') if video_info else '获取视频信息失败'
            yield event.plain_result(f"解析B站视频失败: {err}")
            return

        title = video_info['title']
        video_url = video_info['video_url']
        pic = video_info['pic']
        size_fmt = self.get_file_size(video_info['video_size'])
        quality = video_info['quality']
        comment_url = video_info['comment']

        # 说明文字（有的平台不会把它作为视频的 caption 显示，所以另外发一条文本最稳妥）
        caption = (
            f"🎬 标题: {title}\n"
            f"📦 大小: {size_fmt}\n"
            f"👓 清晰度: {quality}\n"
            f"💬 弹幕: {comment_url}"
        )

        # —— 关键：用文档示例的方式发送视频 ——
        from astrbot.api.message_components import Video

        try:
            # 1) 先发视频
            music = Video.fromURL(url=video_url)
            if hasattr(event, "chain_result"):
                yield event.chain_result([music])
            else:
                # 兜底：部分极老适配器没有 chain_result，就用 CQ 码发送
                cq = f"[CQ:video,file={video_url},cover={pic},title={title}]"
                yield event.plain_result(cq)

            # 2) 再补发说明文字（避免有的平台不显示 caption）
            yield event.plain_result(caption)

        except Exception as send_err:
            # 若发送组件失败，退回纯文本
            logger.error(f"发送视频失败: {send_err}", exc_info=True)
            fallback = (
                f"🎬 标题: {title}\n"
                f"🔗 直链: {video_url}\n"
                f"🖼 封面: {pic}\n"
                f"📦 大小: {size_fmt}\n"
                f"👓 清晰度: {quality}\n"
                f"💬 弹幕: {comment_url}"
            )
            yield event.plain_result(fallback)

    except Exception as e:
        logger.error(f"处理B站视频链接时发生错误: {e}", exc_info=True)
        yield event.plain_result(f"处理B站视频链接时发生错误: {e}")
