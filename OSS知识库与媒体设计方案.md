# OSS 知识库与图片视频设计方案

> 日期：2026-11-17  
> 适用项目：`F:\微信监控\wx-assist`  
> 当前状态：文本知识库已经切到阿里云 OSS 私有桶 `zhaixingriji-kb-2026`；本方案是在此基础上的增强设计，不推翻现有文本链路。

## 一、结论

阿里云 OSS 更适合做“程序化知识库主仓”，不适合当网盘用。

- 文本仍然是主知识：日记、聊天、收藏文字、公众号全文、复盘，继续走现有的明文 JSON 包和数据库快照。
- 图片、视频作为“媒体资产”单独存放：原图/原视频进 OSS，缩略图和封面单独生成，图片文字识别和视频转写文字进检索。
- 原始图片、原始视频不直接做向量检索；检索只查文字和媒体说明，命中后再按需取媒体签名地址。
- 现有目录名保持不变，新增 `摘星日记/媒体/` 前缀，避免换仓后 Bot 找不到旧正文。
- 视频号、朋友圈仍按你之前定的边界默认排除；如果以后要放开，只改范围开关，不改数据模型。

## 二、OSS 对比微云

| 维度 | 腾讯微云 | 阿里云 OSS | 对知识库的影响 |
|---|---|---|---|
| 产品定位 | 消费级网盘，带客户端、分享、预览 | 对象存储，面向程序读写 | OSS 适合机器人长期自动存取，不适合人工当网盘整理 |
| 目录模型 | 真目录 + 文件 | 扁平对象键，目录只是前缀 | 不能靠“列目录”当索引，必须有清单 |
| 接口 | 微云 MCP，分页小、上传分片 | 原生对象存储接口，支持大文件分段上传 | 视频、批量图片更适合 OSS |
| 额度 | 实测当天 `117401` 会把列举/下载/上传全部封死 | 没有这种“当天额度用尽全接口停”的日限额；按存储、请求、流量计费，另有并发/QPS限制 | OSS 不会因为一天传多了第二天才能用 |
| 访问控制 | 令牌 | RAM 子账号、桶策略、签名地址 | 可做到“私有桶、按前缀授权、临时链接” |
| 文件预览 | 网盘自带预览 | 控制台能力有限，主要靠应用生成预览/签名地址 | 图片视频预览要自己做缩略图 |
| 版本与生命周期 | 较弱 | 支持版本控制、生命周期、事件通知、跨区域复制 | 适合做快照保留、冷热分层、自动清理 |
| 同步能力 | 有同步盘客户端 | 没有同步盘；写入即对象，后写覆盖先写 | 需要自己的清单、去重和补偿任务 |
| 检索能力 | 无向量检索 | 同样没有向量检索 | 两者都只是仓库，语义检索仍在本机/检索网关 |
| 成本结构 | 会员/容量包为主 | 存储量 + 请求次数 + 外网流出流量 | 文本很便宜；视频原件是主要成本 |
| 适合场景 | 个人同步、分享 | 自动备份、知识库、媒体仓、换机恢复 | 本项目应把 OSS 当“仓库”，不把它当搜索引擎 |

一句话：微云像“带客户端的柜子”，OSS 像“可编程的仓库”。知识库要的不是柜子，而是可校验、可去重、可按需取用的仓库。

## 三、总体架构

```text
微信聊天 / 日记 / 收藏 / 公众号 / 文件传输助手
                    │
                    ▼
        采集与清洗（只处理文字和媒体元数据）
                    │
        ┌───────────┴───────────┐
        ▼                       ▼
  文本知识链路              媒体资产链路
  日记正文/聊天/收藏/公众号    原图/原视频 → OSS 媒体/原始
  复盘/数据库快照            缩略图/封面 → OSS 媒体/预览
        │                       │
        ▼                       ▼
  明文 JSON 全量包          图片文字识别 / 视频转写
        │                       │
        └───────────┬───────────┘
                    ▼
          本地语义索引（只索引文字）
                    │
                    ▼
         Bot 回答 / 面板展示 / 其他 AI 工具
```

关键点：

1. OSS 只负责存对象和签名取用，不承担检索。
2. 本地索引是派生层，删了可以从 OSS 文本包和媒体文字重建。
3. 原始媒体永远不塞进向量库；只把识别/转写出来的文字切块入索引。
4. Bot 命中媒体时返回“资产编号 + 缩略图/签名地址”，不直接把大视频塞进上下文。

## 四、OSS 目录设计

保留现有文本前缀，新增媒体前缀：

```text
摘星日记/
  知识库全文/
    知识库全文-YYYYMMDD.json
    知识库全文-合并-YYYYMMDD.json
  正文/
    日记正文-<id>.json
  聊天记录/
    聊天记录清单.json
    增量/
      聊天增量-YYYYMMDD-HHMMSS.json
  复盘/
    YYYY-MM/
      周复盘-....html
      月复盘-....html
      年复盘-....html
  数据库快照/
    日记库-YYYYMMDD.db
  媒体/
    清单.json
    原始/
      图片/
        <sha前2位>/<sha256>.jpg
      视频/
        <sha前2位>/<sha256>.mp4
    预览/
      图片/
        <sha256>.webp
      视频/
        <sha256>.jpg
    文字/
      <sha256>.json
    元数据/
      <sha256>.json
```

设计理由：

- 媒体用内容哈希命名，同一张图被多次转发只存一份，不会因为文件名重复冲突。
- 文本继续用人类可读的名字，方便你和其他 AI 工具直接找。
- 清单文件是“目录索引”，程序启动只读清单，不反复列桶。
- 中文路径保持现有习惯；媒体路径用哈希，避免中文文件名、空格、大小写导致签名地址问题。

## 五、数据模型

不修改现有文本表；新增两张媒体表，`attachment` 继续做附件台账。

### 1. 媒体资产表

```sql
CREATE TABLE media_asset (
    asset_id      TEXT PRIMARY KEY,   -- sha256 前 16 位，稳定去重
    sha256        TEXT NOT NULL UNIQUE,
    kind          TEXT NOT NULL,      -- image / video
    mime          TEXT DEFAULT '',
    bytes         INTEGER DEFAULT 0,
    width         INTEGER DEFAULT 0,
    height        INTEGER DEFAULT 0,
    duration_ms   INTEGER DEFAULT 0,
    original_key  TEXT DEFAULT '',    -- OSS 原始对象键
    preview_key   TEXT DEFAULT '',    -- 缩略图/封面对象键
    text_key      TEXT DEFAULT '',    -- 识别/转写文字对象键
    ocr_text      TEXT DEFAULT '',    -- 图片文字
    asr_text      TEXT DEFAULT '',    -- 视频转写
    caption       TEXT DEFAULT '',    -- 可选的内容说明
    state         TEXT DEFAULT 'uploaded',
    created_at    TEXT NOT NULL,
    last_seen_at  TEXT DEFAULT ''
);
```

### 2. 媒体引用表

```sql
CREATE TABLE media_ref (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id    TEXT NOT NULL,
    source_kind TEXT NOT NULL,        -- chat / diary / favorite / oa / file_transfer
    source_id   TEXT DEFAULT '',      -- 会话、条目、收藏等业务编号
    message_id  TEXT DEFAULT '',
    local_id    INTEGER DEFAULT 0,
    entry_id    INTEGER DEFAULT 0,
    fav_id      INTEGER DEFAULT 0,
    role        TEXT DEFAULT 'original', -- original / cover / inline
    created_at  TEXT NOT NULL
);
```

### 3. 与现有表的关系

- `attachment` 增加可选字段 `asset_id`；旧的附件行不改，新的媒体附件写入资产表。
- `cloud_object` 继续只记录文本对象，不把图片视频混进“文本指针”。
- 日记详情需要图片时，通过 `media_ref.source_kind='diary'` 找资产，不把原图二进制塞进 `full_text`。
- 聊天导出包新增 `media_assets`、`media_refs` 两个只读数组；只带元数据和对象键，不带二进制。

## 六、图片处理方案

### 入库流程

1. 从聊天附件、日记附件、文件传输助手、收藏（非视频号）获取图片。
2. 计算 SHA-256；命中已有资产则只新增一条引用，不重复上传。
3. 原图上传到 `媒体/原始/图片/`。
4. 生成预览图：
   - 长边限制 1280 或 1600，输出 WebP；
   - 保留透明通道时用 PNG；
   - 预览图上传到 `媒体/预览/图片/`。
5. 做图片文字识别：
   - 截图、聊天记录截图、表格截图优先识别；
   - 识别结果写入 `ocr_text`，同时写一份 `媒体/文字/<sha>.json`。
6. 生成元数据：尺寸、格式、大小、来源、时间、SHA、缩略图键；写 `媒体/元数据/<sha>.json`。
7. 本地只保留资产行和引用，不长期保留原图；需要展示时用签名地址。

### 检索规则

- 图片不会以原图进入向量库。
- 只索引 `ocr_text`、`caption` 和来源描述。
- 检索结果里带 `asset_id`；Bot 需要展示时返回缩略图签名地址。
- OCR 文本标注“来自图片识别”，避免和日记正文混淆。

## 七、视频处理方案

### 入库流程

1. 视频原件上传到 `媒体/原始/视频/`，大于 100 MB 用分段上传。
2. 用本地视频工具抽一帧封面，生成缩略图，上传到 `媒体/预览/视频/`。
3. 抽音频，走现有语音转写通道；转写文字写入 `asr_text` 和 `媒体/文字/<sha>.json`。
4. 元数据记录时长、分辨率、编码、大小、封面键、转写状态。
5. 原视频只在 OSS 保存，不进上下文；面板只显示封面和转写摘要。

### 检索规则

- 视频正文不直接进向量库。
- 索引 `asr_text`、标题/来源描述、可选的关键帧文字。
- 命中后返回封面图和签名播放地址；默认只展示封面，用户点开才取原视频。
- 视频号、朋友圈默认不进入这条链路；如果放开，只改范围开关。

## 八、Bot 与面板怎么用

### Bot 回答

1. 用户提问。
2. 本机语义索引召回文本块和媒体文字块。
3. 文本块直接回答；媒体块带 `asset_id`。
4. 需要展示时，后台生成一小时有效的签名地址：
   - 图片 → 缩略图或原图；
   - 视频 → 封面 + 可选播放地址。
5. 回答里只写“有这张图/这段视频”，不把二进制塞进模型上下文。

### 面板展示

- 日记详情：正文下方显示图片缩略图/视频封面，点击打开签名地址。
- 收藏详情：只对允许的类型展示；视频号/朋友圈默认仍不展示正文。
- 媒体库：按时间、来源、类型筛选；显示占用空间和重复引用次数。
- 删除：先删引用；引用为 0 且超过保留期，再删 OSS 对象；有引用的资产不物理删除。

## 九、访问与安全

- 桶保持私有，阻止公共访问。
- RAM 子账号权限收窄到：
  - `oss:ListObjects`（只限桶/前缀）
  - `oss:GetObject`
  - `oss:PutObject`
  - `oss:DeleteObject`（只给后台清理任务）
- 面板和 Bot 不直接暴露 AccessKey；只返回签名地址。
- 签名地址默认 1 小时，图片预览可以更短。
- 不在 JSON 包里写签名地址，只写对象键；签名是临时派生，不是长期数据。
- 媒体文件明文存放，不做服务端加密；依赖私有桶和权限控制。
- 删除媒体走“软删除 + 延迟物理删除”，避免误删还在引用的图片。

## 十、成本与生命周期

- 文本包很小，主要成本是媒体原件，尤其是视频。
- 图片：原图 + 缩略图两份；缩略图必须生成，避免面板加载原图。
- 视频：原件最贵；建议按时间分层：
  - 最近 90 天标准存储；
  - 90 天后转低频存储；
  - 超过一年且引用为 0 的，进入清理候选。
- 数据库快照保留最近 7 份；文本全量包按天保留，合并包按天保留。
- 上传前算哈希去重；同一视频多次转发只花一份存储。
- 清单文件按天更新；不要每次问答都列桶。

## 十一、兼容与迁移

### 必须保持不变

- 现有文本包路径、正文路径、聊天清单、增量、复盘、快照路径不变。
- 现有 `CloudStore`、`body_store`、`restore`、`chat_merge` 的调用方式不变。
- 现有 Bot 问答链路不变；媒体只是新增一条旁路。

### 历史媒体

当前本地附件表只有 3 条：1 个 CSV、1 张图片、1 个视频。图片和视频原件只在旧微云，旧微云当天额度已封死，无法可靠拉回。

所以：

- 新方案先对新产生的媒体生效；
- 历史图片/视频如果本地缓存还能找到，再补录；
- 找不到的不要伪造，不把旧微云死链写进新媒体表。

### 分阶段落地

1. 第一阶段：媒体表 + OSS 前缀 + 原图/原视频上传 + 哈希去重；文本链路不动。
2. 第二阶段：图片缩略图 + 图片文字识别；视频封面 + 转写；媒体文字进本地索引。
3. 第三阶段：面板媒体库、签名地址、引用计数、生命周期清理。
4. 第四阶段：历史媒体补录和成本报表。

每一阶段都要过：

- 文本问答回归不倒退；
- 私有桶匿名访问返回拒绝；
- 同一文件重复上传只产生一条资产；
- 删除引用后原对象不立即丢失；
- 本地临时文件清理干净；
- 不在 C 盘创建文件。

## 十二、范围开关

建议在后台增加四个开关，默认值如下：

| 开关 | 默认 | 说明 |
|---|---|---|
| 普通聊天图片/视频入媒体库 | 开 | 只存资产和文字，不做原图检索 |
| 日记附件图片/视频入媒体库 | 开 | 日记正文仍以文字为主 |
| 收藏图片/视频入媒体库 | 开 | 视频号、视频类型仍排除 |
| 视频号/朋友圈入知识库 | 关 | 沿用你之前明确的要求 |

这样既满足“包括图片视频”，又不会把之前明确不要的视频号和朋友圈偷偷放进来。

## 十三、验收标准

- OSS 上出现 `摘星日记/媒体/清单.json`、`媒体/原始/`、`媒体/预览/`、`媒体/文字/`、`媒体/元数据/`。
- 同一张图片从两个会话各发一次，只产生一个 `media_asset`，`media_ref` 两条。
- 图片详情能显示缩略图；没有原图时不报错，只显示“原件待补”。
- 视频详情能显示封面和转写摘要；不把原视频塞进模型上下文。
- 检索只命中文字块；媒体块带资产编号，不返回二进制。
- 私有桶直接访问被拒绝；签名地址过期后失效。
- 面板和 Bot 的文本问答与当前版本一致，不因媒体改造倒退。
- 本地临时目录不残留原图/原视频；只有聊天库和结构化元数据长期保留。

## 十四、我的建议

先做“媒体资产 + 缩略图 + 文字识别/转写”这一层，不要一上来做图片向量检索。图片和视频的真正价值在文字、来源、时间、人物和场景说明；先把这些抓住，检索就会稳，成本也可控。

如果后面确实需要“以图搜图”或“视频画面检索”，再单独加视觉向量模型，作为第二套索引，不混进现在的文本索引。

## 十五、落地记录（2026-11-17 已完成）

- 表：`media_asset`、`media_ref`、`attachment.asset_id` 已建；生产库与 OSS 均已生效。
- 图片链路：SHA-256 去重 → 原图 `摘星日记/媒体/原始/图片/<sha前2>/` → 缩略图 `预览/图片/` → OCR 文字 `文字/` → 元数据 `元数据/`；真实聊天图片已上云（`bcddab50755740ca`，96×210，OCR“开始”），签名缩略图 GET 200。
- 视频链路：原视频 `媒体/原始/视频/`、封面 `媒体/预览/视频/`、ffmpeg 16k 单声道音频 → 硅基流动 SenseVoice 转写；3 秒测试视频已验证宽高、时长、封面和转写，测试资产随后已从库和 OSS 清理。
- 采集：文件传输助手附件在 `capture` 暂存时直接进媒体库；普通聊天图片/视频在 `wcdb_backend` 入库时把原始 XML 旁路进 `messages.db.media_raw`，后台 `scan_chat_media` 按 md5 回微信缓存取原件；历史已被压成 `[图片]` 的消息没有 md5，不伪造。
- 收藏：普通收藏图片逐账号尝试解密，视频号/朋友圈(type 4/20)一律排除；当前普通收藏 V2 解密仍报 `Padding is incorrect`，接口保留“补扫普通收藏图片”，失败只记 skipped，不阻塞。
- 面板：`/journal/media` 媒体库页（缩略图/视频、OCR/ASR、补扫按钮），`/api/journal/media`、`/api/journal/media/scan|favorites|chat` 已实测 200；缩略图走同源代理 `/api/journal/media/<id>/file?role=preview`，避免浏览器直接访问 OSS 外链失败。
- 检索：媒体正文（OCR/ASR/caption）进 `source="media"` 语义块，关键词兜底也查 `media_asset`；原图/原视频不塞模型上下文。
- 本地：媒体原件上传后只删 `data/journal/attachments` 暂存；聊天缓存原件不动；测试文件已清理。

## 十六、免费期过后的月度费用估算（2026-11-17）

阿里云 OSS 标准存储（本地冗余）按量单价：0.12 元/GB/月；外网下行 0.50 元/GB；读请求 0.01 元/万次、写请求 0.05 元/万次。新用户试用通常给 20GB 标准存储、2GB 外网下行、20 万次请求，试用期常见 3 个月；北京地域一般不叠加“每月 5GB”那一档。

当前云端实测（脚本 `oss2.ObjectIterator`，前缀 `摘星日记/`）：

| 分类 | 对象数 | 字节 | 约 |
|---|---:|---:|---:|
| 知识库全文（含合并包和全量包两份） | 2 | 10,202,253 | 9.73 MB |
| 数据库快照 | 1 | 339,968 | 0.32 MB |
| 复盘 | 5 | 66,340 | 0.06 MB |
| 媒体 | 13 | 50,180 | 0.05 MB |
| 正文 | 6 | 31,014 | 0.03 MB |
| 聊天记录 | 5 | 10,723 | 0.01 MB |
| 合计 | 32 | 10,700,478 | 10.20 MB |

按当前量：存储费约 `0.12 × (10.20/1024) ≈ 0.0012 元/月`；请求费和缩略图流量都不到一分钱。免费期过后，现有文本知识库的实际月费基本可以按 **0 元** 看待（四舍五入到分）。

规模上来后的量级：

| 情景 | 存储费/月 | 说明 |
|---|---:|---|
| 当前 10.2MB | 约 0.001 元 | 文本、复盘、快照、少量图片 |
| 25GB | 约 3 元 | 免费试用 20GB 略超后的量级 |
| 100GB | 约 12 元 | 约 1000 小时中等画质视频，或大量图片原件 |
| 1TB | 约 123 元 | 视频长期积累才会到 |

外网下行单独计：看 10GB 约 5 元、100GB 约 50 元；上传不收费。请求费按 20 万次免费额度之外算，100 万次读约 1 元、100 万次写约 5 元，正常日记/聊天增量远低于这个量。

结论：文本知识库不是消费大头；真正会放大账单的是**视频原件的长期存储**和**反复从云端下载视频产生的下行流量**。图片按当前 20～30KB 级缩略图算，几乎可以忽略；RAG 只读已经落在数据库里的 OCR/转写文字，不会为了问答去下载原视频，因此“媒体参与检索”本身不会明显增加 OSS 流量。

## 十七、图片/视频控制项（2026-11-17 已落地）

后台“系统配置 → 日记与复盘 → 云端存储（阿里云 OSS）”增加两个独立开关：

| 配置项 | `.env` 键 | 默认 | 作用 |
|---|---|---|---|
| 允许上传到 OSS | `JOURNAL_MEDIA_UPLOAD` | 开（`1`） | 关闭后新图片/视频不上云、不建媒体库记录，暂存副本删除；历史资产不自动删除 |
| 允许参与问答检索 | `JOURNAL_MEDIA_RAG` | 关（`0`） | 关闭后 OCR/ASR 文字不进 RAG，关键词兜底也跳过媒体；历史媒体向量块会清理 |

代码落点：
- `src/journal/media.py`：`media_upload_enabled()`、`media_rag_enabled()`；`ingest_media`、`scan_attachments`、`scan_chat_media`、`sync_favorite_images` 均有上传关闭保护。
- `src/journal/capture.py`：关闭上传后，文件助手图片/视频不建档、不暂存。
- `src/journal/recall.py`：`index_attachment`、`index_media_asset`、`index_all` 媒体循环、关键词兜底 SQL 均受 `JOURNAL_MEDIA_RAG` 控制；新增 `purge_media_index()`，关闭开关时清理旧块。
- `src/web/server.py`：`GET/POST /api/journal/cloud` 读写两个开关；关闭检索时调用 `purge_media_index()`。
- `ui/src/components/JournalCloudSection.jsx`：两个复选框、费用口径说明；文案已从“图片和视频不进入知识库”改为“可分别控制上云与检索”。

实测：关闭上传后 `/api/journal/media/scan` 返回 `disabled=True、total=0`；重新打开后恢复 `upload=True`。`tests/test_journal_cloud.py`、`tests/test_journal_media.py` 共 10 项通过。默认策略为“媒体上云保留、默认不参与问答”，以后需要时可只在后台打开检索。


## 十八、统一存储接口：底层换 OSS / COS，上层不动（2026-11-17 已落地）

原始问题：恢复、附件上传、媒体补扫、云端同步这些业务代码里散落着 `weiyun.list`、`client.upload(...)` 之类的旧调用，一换厂商就要改一圈。做法是抽一层抽象基类，把“目录 + 文件”当成唯一契约，各厂商只实现自己的那套 SDK 调用。

### 18.1 接口契约

`src/journal/cloud/base.py`

| 方法 | 语义 |
|---|---|
| `root_dir_key(path)` | 把业务里的“目录/文件”路径翻成该厂商的对象键 |
| `ensure_path(path)` / `ensure_dir(path)` | 建目录（对象存储没有真目录，靠前缀占位或直接忽略） |
| `upload(local, remote)` | 上传本地文件 |
| `download_url(remote)` | 取可下载地址（返回字典或字符串由实现决定） |
| `delete(remote)` | 删除对象 |
| `list_files(prefix, offset, limit)` | **必须支持分页**，返回 `{"file_list": [...], "finish_flag": bool}` |
| `call(name, **kw)` | 兼容层：把旧的 `weiyun.list` 映射到 `list_files` |
| `selftest()` | 写一个 2 字节对象再删掉，验证凭据与桶权限 |

`CloudError` 统一抛错，上层只捕一个异常类型。

### 18.2 三个实现

| 文件 | 类 | 关键点 |
|---|---|---|
| `oss.py` | `OssClient`（`name="oss"`） | `oss2.Bucket`；`ensure_path` 去重复根前缀，避免出现 `摘星日记/摘星日记`；`list_files` 用 `oss2.ObjectIterator` 遍历，按 `offset` 跳过、凑满 `limit` 就置 `more=True` 并 `finish_flag=False` |
| `cos.py` | `CosClient`（`name="cos"`） | `qcloud_cos` SDK；`while True` + `Marker` 翻页，`seen` 计数按 `offset` 跳过，`limit` 截断置 `more`，`NextMarker` 为空即停，防死循环 |
| `weiyun.py` | `Weiyun`（`name="weiyun"`） | 原有微云通道补齐 `list_files`、`selftest`，保持旧行为 |

> 分页是这轮踩过的真坑：OSS 最初忽略 `offset` 且 `finish_flag` 恒 `True`，恢复只看见第一页（2 个全文包）；COS 最初 `while len(files) < page_size` 语义写反，凑满一页就停。两处都已按上表改掉。

### 18.3 厂商注册与切换

`src/journal/cloud/provider.py`

- `ALIASES`：`oss/aliyun/aliyun-oss`、`cos/qcloud/tencent-cos`、`weiyun/tencent`。
- `provider_name()`：显式配置优先，否则按已有凭据自动选。
- `configured_providers()`：给后台下拉框列出三家及 `ready` 状态。
- `make_client(provider="")`：显式指定但缺配置时**直接报错**，不静默回退到别家。
- `cloud_enabled()`、`display_name()` 供开关与文案使用。

### 18.4 实测

| 项 | 结果 |
|---|---|
| `摘星日记/知识库全文` | 2 个对象：`知识库全文-20261117.json`（5,102,722 B）、`知识库全文-合并-20261117.json`（5,102,785 B），`finish_flag=True` |
| `摘星日记/聊天记录` | 11 个对象（10 个增量 + 清单），`limit=200` 一次列全 |
| `GET /api/journal/cloud` | `provider=oss`、名称“阿里云 OSS”，providers 三行：OSS ready / COS 未就绪 / 微云 ready |
| COS | 仅静态与接口层验证，**无真实桶凭据，未连桶** |

切换方式：后台“系统配置 → 日记与复盘 → 云端存储”改厂商，或 `.env` 里 `CLOUD_PROVIDER=oss|cos`。上层 `restore.py`、`media.py`、`chat_merge.py` 一行不用改。

## 十九、历史语音批量回溯（2026-11-17 落地，后台持续推进）

### 19.1 事实与口径

- `messages` 表没有 `local_id/server_id` 字段，但用 `chat_id + timestamp` 能定位回音频；微信音频在 `media_N.db` 的 `VoiceInfo.voice_data`，`chat_name_id` 是 Name2Id 的 rowid。
- `WcdbNativeClient` 必须先 `init()` 再 `open()`，全进程用 `get_shared_client()` 单例。
- `msg_type=34` 共 4,396 条；纯占位（`content=''` 或 `'[语音]'`）4,388 条；带文字的 8 条不计入占位。

### 19.2 队列设计

`src/journal/voice_queue.py`

- `enqueue_message` 允许 `local_id/server_id` 为空；`_process` 只要求 `chat_id` 或 `server_id` 有一个。
- `_claim_one` 用 `BEGIN IMMEDIATE` 把“回收超时 processing → 选一条 → 改 processing”做成原子动作，机器人与手动批量脚本同时跑也不会重复转写同一条。
- `MAX_ATTEMPTS=3`、`PROCESSING_TIMEOUT_SEC=600`。
- `backfill_placeholders()` 只扫 `content='' OR content='[语音]'`，`done/pending` 跳过，`failed` 重置回 `pending`，保证失败项能被新一轮捞起来。
- `queue_stats()` 给后台出 `done/failed/pending/processing` 四态。

`worker.py` 接入：`safe_voice_backfill()` 启动后只登记一次，`safe_voice_queue()` 每 5 秒推进。

### 19.3 并行加速

单进程串行只有约 14 条/分钟，全量要 4 小时以上。加了一路脚本 `_临时/voice_boost.py`：多线程各持一个 `VoiceQueue`，微信读取客户端用锁包一层（sqlite 连接不能多线程共用），识别请求并发走；每条队列操作仍是 `BEGIN IMMEDIATE` 原子抢占。实测吞吐提升到约 30 条/分钟（后追加一路 8 线程继续提速）。

### 19.4 结果与失败口径（2026-11-17 10:15 收敛）

- 最终：`done 4,204 / failed 194 / pending 0`。原来 4,388 条纯占位，现在只剩 194 条仍是 `[语音]`。
- 194 条残留全部是**原件不在微信本地**：192 条“微信语音数据读取失败”，2 条“识别结果为空”（音频本身没有有效人声）。都保留 `failed`，不伪造文字。
- 新增 `PERMANENT_ERRORS`（微信语音数据读取失败 / 缺少会话 ID / 微信语音解码失败）：`backfill_placeholders` 遇到这几类失败直接 `skipped`，不再重置成 `pending` 反复重试，避免每次重启都白跑一遍。
- 回写与上云：`messages.content` 已改成 `[语音] 正文`；`cloud_dirty` 标记后由 `chat_sync` 分批上传“语音更正”增量，实测一轮上云 500 条。
- 识别通道：硅基流动免费模型 `FunAudioLLM/SenseVoiceSmall`；`asr_silicon._env()` 环境变量优先、再读项目根 `.env`，修掉了“`.env` 有键但进程环境没有”的缺口。

### 19.5 并行脚本

`_临时/voice_boost.py` 是一次性加速脚本：多线程 + 客户端读取加锁，队列清空后自动退出。跑完即结束，不需要常驻；日常新语音仍由机器人每 5 秒推进。

## 二十、换机一键恢复接口（2026-11-17 已落地）

### 20.1 后端

`src/journal/cloud/restore.py` 未改内容，靠统一存储接口即可跑：`restore_bundle()` 下载最新全文包 + 全部增量 → `merge_package` 合并 → 写 `恢复说明.json`。

`src/web/server.py` 新增两个接口：

| 接口 | 作用 | 返回 |
|---|---|---|
| `GET /api/journal/restore/latest` | 看云端最新包 | `filename/file_size/file_mtime/has_manifest/manifest_counts/manifest_last_rowid/manifest_exported_at` |
| `POST /api/journal/restore/run` | 执行一键恢复 | `out_dir/merged/increments/counts/message`，明确“只生成新恢复目录，不覆盖正在用的库” |

`run` 已加入 POST 白名单。

### 20.2 前端

`ui/src/components/JournalCloudSection.jsx` 新增“换机一键恢复”区块：`loadRestore()` 随页面加载拉最新包信息并展示“清单游标：聊天 rowid …，最近导出 …”；`runRestore()` 先 `window.confirm` 二次确认，再 POST，完成后显示“恢复完成：聊天 X 条、日记 Y 条、收藏 Z 条、增量 N 个”与恢复目录。按钮文案“从云端恢复 / 恢复中…”。

### 20.3 实测

- 直接调 `restore_bundle(out_dir=...)`：`increments=10`，输出 `知识库全文-恢复合并-20261117-093850.json`，计数 `messages 11120 / diary 6 / favorites 136 / contacts 516 / oa_articles 125 / oa_accounts 23 / sources 440 / reminders 1 / reminder_tasks 15 / answer_rules 1 / reviews 5 / voice_jobs 8 / attachments 1 / cloud_objects 58`。
- 走 HTTP：GET 返回 `知识库全文-合并-20261117.json`、6,598,497 B、`manifest_last_rowid=11121`、`manifest_exported_at=2026-11-17T09:40:12`；POST 返回 `out_dir=data\journal\export\恢复-20261117-094303`、`increments=13`、`merged=知识库全文-恢复合并-20261117-094305.json`、`counts.messages=11121 / voice_jobs=4397 / favorites=136 / contacts=516`。
- `manifest_counts` 为 `None` 属正常：聊天清单本身没有 `counts` 字段。

> 视觉验收说明：自动化浏览器把页面放在后台标签时 `document.visibilityState=hidden`，页面切换动画不推进，内容区停在运行状态页，无法截到新区块。这是自动化环境限制，接口与构建产物均已验证（`dist` 内已含“换机一键恢复”字符串）。

## 二十一、媒体补扫与收藏解密收口（2026-11-17）

### 21.1 新增 gone 终态

`src/journal/media.py`

- `reclassify_gone(db_path)`：把历史 `failed` 里“没有图片/视频 md5”“微信缓存里找不到图片/视频封面”四类**永久性失败**批量改成 `gone`，不再反复重试。
- `scan_chat_media` 开头跑一次归类；扫到“找不到原件”时直接 `mark(mid, 'gone', 原因)`，不是 `failed`。
- `media_stats(db_path)` 返回 `{done, failed, pending, gone, skipped}`，`/api/journal/media/chat`、`/api/journal/media/favorites` 响应都带上 `stats`。

实测：`scan_chat_media(limit=30)` → `total 30 / ok 0 / failed 30`，`stats = {done 8, failed 0, pending 508, gone 326, skipped 0}`。原来 292 条 `failed` 已全部归入 `gone`，失败不再堆积。

### 21.2 收藏解密的两层根因与硬墙

1. `resolve_image_hardlink` 之前不带账号目录，副账号收藏永远找不到原件 → 已改为 `resolve_image_hardlink(md5, account_dir="")`，调用处传 `str(self._data_dir / wxid)`。
2. 收藏缓存文件名就是 md5，很多不在聊天 `image_hardlink_info_v4` 表 → 新增 `V2CacheManager._find_fav_by_md5()`，逐账号在 `business/favorite/{data,mid,thumb}` 下 `rglob(md5)`。

**硬墙**：主账号 `wxid_abh6zf6773ta22_c8de` 能解（`get_keys = ('a6d3ee333a1663c3', 166)`，样本 1137 字节）；副账号 `wxid_cy3xykvv4vsx12_36f7`、`shtoxx_4698`、`wxid_m1kfz21cikoa12_f786` 全部解不开。穷举 kvcomm 里 6 个 uin 候选与 base 变体（含/不含后缀、含/不含 `wxid_`、uin 前后顺序）均无命中，结论是**副账号 V2 密钥当前不可推导**。

实测 `sync_favorite_images(limit=120)`：`{'total': 0, 'ok': 0, 'failed': 0, 'deduplicated': 0, 'skipped': 105, 'reasons': {'缓存原件解不开（多为副账号密钥不可推导）': 105}}`。只能标注“原件不可用”，不伪造图片。
## 二十二、腾讯云 COS 真实接入（2026-11-17）

用户要的不是“代码里有个 cos.py”，而是“这个桶真的能读能写”。本章记录从注册到连桶的全过程与实测结论，至此统一存储接口的 OSS / COS 双厂商从“静态验证”变为“两把钥匙都在、两桶都能通”。

### 22.1 账号与资源清单

| 项目 | 值 |
| --- | --- |
| 登录方式 | 微信扫码（未绑邮箱） |
| 主账号 ID 片段 | 100039462343 |
| COS 服务 | 已开通，领取 50GB 标准存储容量包，效期 180 天，不抵扣流量与请求费 |
| 存储桶 | `zhaixingriji-cos-2026-1331445121` |
| 地域 | `ap-beijing`（公有云园区-中国-北京） |
| 数据冗余 | 多 AZ（创建后不可关闭，费用高于单 AZ；桶内对象仅数 MB，按量费用可忽略） |
| 访问权限 | 私有读写 |
| 默认域名 | `zhaixingriji-cos-2026-1331445121.cos.ap-beijing.myqcloud.com` |

### 22.2 密钥：不用主账号，走最小权限子账号

腾讯云密钥管理页提示“不建议使用主账号 API 访问密钥”，主账号密钥一旦泄露等于交出名下全部云资源控制权。因此没有采用页面上已存在的主账号密钥，而是新建子账号：

- 子账号名 `wx-assist-cos`，访问方式只勾“编程访问”（不开控制台登录，不留密码面）。
- 权限只绑一条预设策略 `QcloudCOSFullAccess`（对象存储全读写），不绑管理员、不绑财务。
- SecretKey 只在创建时展示一次，已直接写入本机 `.env`，含明文的下载文件当场删除。

`.env` 新增四项（代码同时支持 `TENCENT_COS_*` 别名）：

```
COS_SECRET_ID=AKID…（尾号 Qnxu）
COS_SECRET_KEY=（40 位，仅存本机）
COS_REGION=ap-beijing
COS_BUCKET=zhaixingriji-cos-2026-1331445121
COS_PREFIX=摘星日记
```

### 22.3 真实连桶实测

`_临时/cos_selftest.py` 走的是和业务完全相同的 `CosClient`：

- `selftest()` 写入 `摘星日记/_后台连接测试.txt` 再删除 → 通过，说明有写权限。
- `upload()` 上传 34 B 探针到 `摘星日记/知识库全文/cos_probe.txt` → `file_exist=True`。
- `list_files()` 列出该对象，`finish_flag=True` → 分页标志正确。
- `download_url()` 生成预签名链接（`…myqcloud.com/摘星日记/知识库全文/…`，1 小时有效）。
- `delete()` 删除探针 → `deleted=True`，桶不留垃圾。
- `configured_providers()` 三行全部 `ready=True`：阿里云 OSS / 腾讯云 COS / 腾讯微云。

业务级备份也跑了一遍：把 6,615,230 B 的 `知识库全文-合并-20261117.json` 上传到 `摘星日记/知识库全文/`，列举回来大小一致。

界面侧同样验过：面板“系统配置 → 日记与复盘 → 云端存储”里，切换按钮已显示“腾讯云 COS（已配）”，点“测试现有”返回绿字“COS 连接成功，已能列举并写入存储桶（测试模式，未保存）”。当前云仓仍是阿里云 OSS，没有误切主仓。

### 22.4 修掉的一个真坑

COS 的 `LastModified` 返回 ISO 字符串（`2026-11-17T02:36:46.000Z`），`list_files` 里原来直接 `int()` 转换，第一次真连桶就抛 `ValueError`。新增 `_mtime()` 统一把 ISO 串解析成秒，解析失败兜底 0，`list_files` 不再因为时间格式崩掉。

### 22.5 切换口径

只改 `.env` 的 `CLOUD_PROVIDER=oss|cos`，上层业务代码一行不动；两边的桶各存各的，不会自动互相迁移。要在两桶间搬历史，先切到源仓导出全文包，再切到目标仓跑一次“换机一键恢复”的写入路径。主仓仍为 OSS，COS 作为已就绪的备仓与异地备份位。