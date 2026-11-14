# Typst 正式模板

`report.typ` 从 `sys.inputs.data` 读取 JSON 数据，导入 `components.typ` 排版。数据中的正文经 `text()` 输出，不作为 Typst 代码执行。

通过插件根目录的 `python tools/preview_template.py` 预览；字体、素材、输入结构和新增模板要求见 [模板规范](../../../docs/template-spec.md)。
