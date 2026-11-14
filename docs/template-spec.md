# 简报模板规范 v1

本规范对应 `template.json` 的 `schemaVersion: 1`。清单校验见 [JSON Schema](../templates/template.schema.json)，完整实例见 [守岸人模板](../templates/shorekeeper/template.json)。模板外观版本使用独立的 `version`，不与插件版本混用。

## 目录与职责

```text
templates/
  template.schema.json
  common/layout.js              共用的数据填充逻辑
  <template-id>/
    template.json
    template.html
    components/                 页面与卡片组件
    styles/                     颜色、字体和布局
    assets/
      images/                   图片目录名可自定
      fonts/
        README.md
        LICENSES/               可公开分发字体的授权
        private/                私有字体，Git 忽略
    copy/closings.json           可选的成组收尾短句
    samples/                    示例数据
    README.md                   模板说明与素材来源
```

每个模板自带自己的字体和素材，不强制统一视觉风格。插件扫描 `templates/*/template.json`；加入有效目录、刷新管理页后即可出现在模板列表，无需改任务调度或发送代码。模板目录 ID 使用小写字母开头的字母、数字、下划线或连字符。

模板只负责展示。群历史读取、模型调用、任务数据库、图片缓存、回执及补发由插件公共代码负责；模板不应自行执行这些操作。只安装可信的模板代码。实际渲染仅提供清单内的本地资源，阻止外部网络请求和任意本地路径读取。

## 清单字段

| 字段 | 含义 |
| --- | --- |
| `schemaVersion` | 固定为 `1`，不支持的版本拒绝加载 |
| `id` / `name` / `version` | 目录标识、界面显示名称和模板版本 |
| `entry` | 模板根目录下的 HTML 入口，如 `template.html` |
| `width` | 整数画布宽度，当前支持 640–1600px |
| `viewportHeight` | 独立开发工具预览视口高度；不是长图高度限制 |
| `styles` | `styles/` 下按顺序加载的 CSS 文件名 |
| `components` | `components/` 下的组件名，不含 `.html` |
| `layout` | 页面组件排列顺序，至少包含 `header`、`content-panel` |
| `assets` | 可选，素材名到 `assets/` 下相对文件路径的映射 |
| `stickers` / `stickerCycle` | 可选，表情名到图片文件的映射及轮换顺序 |
| `stickerAliases` | 可选，独立示例工具的旧表情名兼容映射；正式生成按顺序轮换 |
| `closingCopy` | 可选，收尾短句 JSON 的模板相对路径 |
| `fonts` | 可选，字体文件、字重、角色和回退链 |

所有文件必须位于模板目录内，不允许绝对路径、`..` 或指向目录外的符号链接。素材占位符使用映射名的大写形式，例如 `assets.character` 对应 `{{CHARACTER}}`。素材名应使用简单英文字母。

## HTML 与数据契约

入口保留 `{{STYLES}}`、`{{COMPONENTS}}`、`{{DATA}}`、`{{THEME}}`、`{{RUNTIME}}` 五个槽位，参照守岸人的入口。保留 `#pages` 容器、`#report-data` 与 `#theme-data` JSON 节点。公共运行时从组件创建 `.report-page`，不需要模板复制 JavaScript。

必要组件及节点：

- `header`：可使用 `data-field="report-title"`、`date`、`intro`。
- `content-panel`：包含 `.card-stack`，作为卡片容器。
- `card`：包含 `.card-body[data-field="body"]`；可以使用 `number`、`category`、`title` 字段。
- 可选 `sticker`：根元素为图片，在卡片的 `[data-slot="sticker"]` 中显示。
- 可选收尾组件：使用 `data-field="closing-title"`、`closing-note`；署名可作为模板静态文字。

字段都通过 `textContent` 写入。正文中的 HTML 标签仅显示为文字，不能变成可执行内容。不要对正文使用裁切、隐藏、固定行数、省略号或固定页面高度。

渲染输入的主要结构：

```json
{
  "title": "日常简报",
  "date": "2026.11.14",
  "intro": "今天值得留意的事，放在这里了。",
  "sections": [
    {"label":"今日安排","title":"说明会签到","body":"已报名同学请在九点前到场。"}
  ],
  "closing": {"id":"beside-you","title":"慢慢来，我会陪着你。","note":"把要紧的事放在心上，也记得给自己片刻休息。"}
}
```

标题和日期由代码提供，日期按任务计划时刻及任务时区显示。开场与卡片内容由最后一轮总结生成，并遵循任务人格、个人背景与总结提示词；图片模式不增加一次整理模型调用。来源标识用于后台校验，不作为模板正文输出。备用文字从同一份卡片内容生成。固定收尾属于所选模板，不自动改成其他人格。

## 收尾短句

`closingCopy` 指向非空 JSON 数组，每项包含唯一 `id`、非空 `title` 和 `note`，三者分别不超过 300 个字符，最多 100 组。两段文案成组选择，不拆开随机组合。

正式流程在数据库中按“收件人＋模板＋预览/正式计划”记录上一组，从其他组中随机选择。预览不消耗正式计划的记录；同一次执行选定后固定，重启或补发不重新抽句。只有一组时允许复用。没有收尾组件的模板可省略短句库。

## 字体规范与分发

每个模板的字体放在自己的 `assets/fonts/` 下。清单示例：

```json
{
  "fonts": {
    "faces": [
      {"family":"Digest Body","weight":400,"file":"assets/fonts/private/msyh.ttf","distribution":"private"},
      {"family":"Digest Body","weight":700,"file":"assets/fonts/private/msyhbd.ttf","distribution":"private"}
    ],
    "roles": {
      "body":{"family":"Digest Body","fallback":["Microsoft YaHei","Noto Sans CJK SC","sans-serif"]}
    }
  }
}
```

- `family` 是本模板的 CSS 别名；同一字体的多个字重使用同一别名。`weight` 为 100–900 的整数。
- 角色 `body`、`display`、`latin` 分别生成 `--body-font`、`--display-font`、`--latin-font`。CSS 使用这些变量即可。
- 支持 TTF、OTF、WOFF、WOFF2。TTC 是字体集合，应先提取正确字面并核对字体名，再作为浏览器字体文件使用。
- `distribution` 为 `private` 或 `bundled`。私有文件必须放在 `assets/fonts/private/`，该目录被 Git 忽略；公开字体应附允许分发的授权说明。
- 私有字体只保留在本地模板目录。推送 GitHub 不带字体；明确部署到用户服务器时，再单独复制到服务器同一模板的 `assets/fonts/private/`，不提前上传，不安装为服务器系统字体。
- 采用覆盖或替换目录升级时，应先备份该模板的私有字体，随部署恢复；不能只推 Git 代码后假定字体仍在。
- 禁止通过内嵌字体的 HTML、字体子集或压缩包间接提交私有字体。代码仓库的许可证不代表包含第三方字体的授权。
- 渲染器通过 `@font-face` 加载模板文件，不依赖系统安装；缺失时在模板选择处和生成记录中提示，使用角色定义的回退链。默认回退字体也不存在时，应先准备中文字体再启用图片模式。
- 截图前等待字体和图片解码。相同字体文件可尽量保证字形和换行一致，实际效果仍应在服务器验证。

守岸人的正文和粗体标题使用微软雅黑，收尾大字与署名用华文楷体，日期及英文装饰用 Georgia。文件列表见 [字体说明](../templates/shorekeeper/assets/fonts/README.md)。Noto 仅用于缺失时回退。

## 渲染、限制与失败处理

`digest/rendering.py` 提供公共加载器和 Python Playwright 渲染器，`templates/common/layout.js` 为公共页面填充逻辑。正式运行不依赖 Node.js；Node 工具仅供旧独立样稿开发。

浏览器按需启动并复用，图片渲染单并发，总预算 90 秒。普通内容输出一张随内容增高的 PNG；当前上限为 12000px 高、8 MiB 文件大小。超限、模板失效、浏览器不可用或排版/资源检查失败时，完整回退文字，不缩字或截掉正文。超过这些限制的多图分段暂未实现。

PNG 和模板版本、所选收尾固定在本次执行记录中。截图存放在插件数据目录 `rendered/`；发件箱只存图片文件名和校验值。预览通过已认证的插件 RPC 按执行 ID 读取，不开放任意文件下载。NapCat 发送使用图片字节的 Base64，不依赖容器间共享文件路径。

图片确认送达后才发送文字日程清单。已发送的内容不会重发；图片发送失败按现有流程手动补发，发送结果未知时必须先核对，不会自动再发文字造成重复。缺失或损坏的缓存图会阻止发送。无记录引用、且超过一小时的缓存文件由定时清理回收，待补发的文件保留。

## 新增模板与验证

1. 复制守岸人模板的目录骨架，修改目录名及 `id`、`name`、`version`。
2. 保留必要组件和数据节点，按需要替换 CSS、素材、字体与收尾；不需要立绘或表情时移除相应组件和配置。
3. 在 `README.md` 说明素材来源、字体授权及使用方式。不要带上 `private/`、缓存或真实群消息。
4. 安装插件依赖和本地 Chromium，用正式渲染器测试示例：

```sh
python tools/preview_template.py --template shorekeeper --sample templates/shorekeeper/samples/standard.json
```

默认生成到 `.local/template-preview/preview.png`，不调用模型、不发送 QQ。正式渲染器对清单做 Schema 校验；文本完整性、字体与图片加载、宽高及文件大小也会检查。

5. 验证长标题、长正文、无立绘/表情、缺失字体及中文标点，再刷新管理页选择新模板。可设置 `DIGEST_BROWSER_TESTS=1` 运行真实浏览器测试。

实现参考：[Playwright 截图](https://playwright.dev/python/docs/screenshots)、[OneBot 消息段](https://github.com/botuniverse/onebot-11/blob/master/message/segment.md)。
