# 简报模板规范 v2 · Typst

正式图片输出使用 Typst 独立子进程，不依赖浏览器。`template.json` 的 `schemaVersion` 为 `2`，`engine` 固定为 `typst`。清单通过 [JSON Schema](../templates/template.schema.json) 校验；旧 HTML 清单不会被误当成 Typst 模板运行。

## 目录

```text
templates/
  template.schema.json
  <template-id>/
    template.json
    typst/
      report.typ
      components.typ
    assets/
      fonts/
        README.md
        LICENSES/
        private/          私有字体，不提交 Git
      ...                 本模板的图片素材
    copy/closings.json     可选
    samples/
    README.md
```

模板按自己的目录组织字体和素材。新增模板只需放入符合规范的目录并重新打开管理页，不必修改总结、日程或发送代码。模板是可信代码，请只安装来源可信的模板；正文仅作为 JSON 字符串输入，不拼接成可执行的 Typst 源码。

## 清单

| 字段 | 要求 |
| --- | --- |
| `schemaVersion` / `engine` | `2` / `typst` |
| `id` | 与目录名一致，小写字母开头，可含数字、下划线和连字符 |
| `name` / `version` | 显示名称、模板自身版本，与插件版本独立 |
| `entry` | 模板目录内的 `.typ` 入口，如 `typst/report.typ` |
| `width` | PNG 宽度，640–1600px；实际画布宽度必须匹配 |
| `assets` | 素材名到 `assets/` 内相对路径的映射，可省略 |
| `stickers` / `stickerCycle` | 表情文件映射与循环顺序，可省略 |
| `closingCopy` | 成组收尾 JSON 文件路径，可省略 |
| `fonts` | 字体文件、字体族和角色，可省略；中文模板应明确配置 |

路径均相对模板根目录，不允许绝对路径、`..` 或指向目录外的符号链接。页面通常设置 `height: auto`，不要用固定高度裁掉正文。只使用本地导入和素材，避免渲染时下载外部包。

## 输入数据

通过 `json(bytes(sys.inputs.at("data")))` 读取输入。公共渲染器提供：

```json
{
  "title":"日常简报",
  "date":"2026.11.14",
  "intro":"今天值得留意的事。",
  "sections":[{"label":"安排","title":"说明会签到","body":"已报名同学请在九点前到场。","sticker":"/assets/emotes/0006.png"}],
  "assets":{"character":"/assets/shorekeeper.webp"},
  "fonts":{"body":"Microsoft YaHei","display":"STKaiti","latin":"Georgia"},
  "closing":{"id":"beside-you","title":"慢慢来，我会陪着你。","note":"也记得给自己片刻休息。"}
}
```

没有对应素材、字体角色或收尾库时，`assets`、`fonts` 可以为空，`sticker`、`closing` 可为 `null`，模板应按需处理。标题和日期由程序生成，日期使用计划时刻与任务时区。正文和卡片标题来自模型最后一轮总结；图片与备用文字基于同一份内容，不额外调用模型拆文章。

字符串必须用 `text()` 等内容函数输出，不使用 `eval()` 执行正文。换行应完整保留。守岸人模板提供了可直接参考的组件拆分。

## 字体

每个模板的字体保存在自己的 `assets/fonts/`。`fonts.faces` 示例：

```json
{"family":"Microsoft YaHei","weight":400,"file":"assets/fonts/private/msyh.ttf","distribution":"private"}
```

`family` 必须是真实字体族名称，不再是 CSS 别名；Typst 根据字体族和字重选择字面。`fonts.roles` 将 `body`、`display`、`latin` 映射到字体族及有序 `fallback` 列表，公共渲染器把解析后的真实名称传入 `data.fonts`。

- 优先读取清单中的字体文件，所有角色齐全时不扫描系统字体。
- 角色字体缺失时尝试系统字体与声明的回退列表；仍不可用则回退文字发送，不能悄悄输出缺字方框。
- 建议使用 TTF/OTF；TTC 可先提取正确字面。字体名称、用途、来源及授权写入本模板说明。
- `distribution` 为 `private` 或 `bundled`。私有文件必须放在 `assets/fonts/private/`，不上传 GitHub；可分发字体附授权文件。
- 本地保留字体，明确部署时再单独复制到服务器相同的模板字体目录，无需注册为系统字体。替换目录升级时先备份并恢复私有字体。
- 不通过 HTML、字体子集或压缩包间接提交私有字体。代码的 MIT 许可不自动适用于字体和角色素材。

守岸人继续使用微软雅黑、华文楷体和 Georgia。私有字体不会随公开仓库安装，具体见 [字体说明](../templates/shorekeeper/assets/fonts/README.md)。

## 收尾、输出与资源限制

收尾 JSON 是数组，每项含唯一 `id`、非空 `title`、`note`，每个字段最多 300 字符、最多 100 组。按收件人、模板及预览/正式计划分开记录上一组，成对随机选择并避开连续重复；同次执行及补发不重新抽句。

Typst 以 144 PPI 导出 PNG，即 1pt 对应 2px。先编译矢量结果检查画布，再栅格化；要求单页、宽度与清单相符、高度不超过 12000px、最终图片不超过 8 MiB。超限或编译失败时保留完整文字，不裁正文。

同一时间只运行一个编译进程。整体渲染预算 90 秒，单次编译进程最多 75 秒；完成、超时或取消后等待进程退出，释放其字体、图片和编译器内存。空闲时没有常驻浏览器或 Typst 工作进程。

图片缓存及校验值固定在执行记录中。预览按执行 ID 读取；发件箱通过 Base64 图片字节交给 NapCat。图片确认送达后才发文字日程清单。未知结果不自动重试，也不追加文字造成重复。未被记录引用且超过一小时的缓存可被定期回收。

## 验证新模板

```sh
python -m pip install -r requirements.txt
python tools/preview_template.py --template shorekeeper
python tools/preview_template.py --template shorekeeper --sample templates/shorekeeper/samples/extended.json
python -m unittest discover -s tests -q
```

默认预览输出 `.local/template-preview/preview.png`，不调用模型、不发送 QQ。至少核对短文、长段落、长标题、中文标点、缺失字体及超限回退；不要将真实群消息放入公开样例。

参考：[Typst 自动页高](https://typst.app/docs/reference/layout/page/)、[Typst 图片](https://typst.app/docs/reference/visualize/image/)。
