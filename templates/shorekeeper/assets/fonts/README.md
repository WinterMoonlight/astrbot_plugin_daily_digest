# 守岸人模板字体

当前保留原版字体效果。以下文件仅在本机及用户服务器私有使用，不包含在 GitHub 仓库中，也不内嵌到公开的 HTML 构建产物中。

| 文件 | Family | 用途 |
| --- | --- | --- |
| `private/msyh.ttf` | Microsoft YaHei | 正文、开场、收尾小字 |
| `private/msyhbd.ttf` | Microsoft YaHei | 主标题、事项标题及其他粗体 |
| `private/STKAITI.TTF` | STKaiti | 收尾大字、署名 |
| `private/georgia.ttf` | Georgia | 日期 |
| `private/georgiab.ttf` | Georgia | 英文背景装饰 |

渲染器优先通过 `template.json` 的 `fonts` 配置加载本模板 `private/` 中的字体文件。微软雅黑 TTF 从系统 TTC 中提取并核对了字体名称，保留原字形；其他三份文件直接复制。缺少文件时警告并回退。仅在明确部署服务器时，将字体单独复制到服务器对应模板的 `assets/fonts/private/`。不提前上传、不注册到服务器系统字体目录；GitHub 更新不带这些文件。缺失字体时的回退见 [模板规范](../../../../docs/template-spec.md)。

若本地需要另存副本，使用本目录下的 `private/`，它已被 Git 忽略。不要强制添加到 Git，也不要提交含这些字体的 HTML、压缩包或字体子集。后续新增可公开分发的字体应放在模板自己的字体目录，并附授权说明。
