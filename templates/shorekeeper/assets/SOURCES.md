# 素材与设计参考

## 主立绘

- 当前文件：`shorekeeper.webp`，此前获取并保存在模板内的 Wiki 透明立绘。
- 页尾通过 Typst 布局视窗截取头饰、面部和上半身，透明区域直接显露卡片底色，下缘渐隐。未重新绘制、裁切或覆盖原始图片文件。
- 用户提供的 `守岸人图片/7e84ff51e94dc911e8a7823d44dcf72a.png` 副本 `shorekeeper-bottom.png` 保留作备选，当前不加载。
- 代码许可证不代表第三方角色美术也采用相同许可证。

## 用户提供的 Q 版表情

- 来源：项目外层的 `Q版表情包` 文件夹，共 12 个 WebP 动图。
- 处理：只解码第 0 帧，原尺寸输出为静态透明 PNG。不裁切、不改色、不重绘、不覆盖源文件。
- 成品：`assets/emotes/`；每个文件都是单帧，不是 APNG，也不包含原 WebP 的后续动画帧。
- 清单：`assets/emotes/manifest.json`，记录原始文件名、SHA-256、原动画帧数、成品体积和首帧像素核验结果。
- 模板只内嵌当前简报实际用到的表情，旧 AI 表情图集已不使用。

## 排版参考

本版使用 Typst 独立实现排版，参考以下项目的视觉层次、渐变页眉、留白、分区卡片和编号标签，没有复制其统计图表或报告正文：

- [群聊日常分析插件](https://github.com/SXP-Simon/astrbot_plugin_qq_group_daily_analysis)
- [ATRI 示例](https://github.com/SXP-Simon/astrbot_plugin_qq_group_daily_analysis/blob/main/assets/ATRI-demo.webp)
- [BlueArchive 示例](https://github.com/SXP-Simon/astrbot_plugin_qq_group_daily_analysis/blob/main/assets/BlueArchive-demo.webp)

背景、卡片和装饰线条由本目录的 Typst 布局生成。最终预览是 Typst 编译输出，不是整页 AI 生成图。
