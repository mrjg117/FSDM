-- FSDM D1 schema
-- 真相源在 D1；飞书文档表只做给人看的回填展示。
-- 表名统一用 __PREFIX__ 占位：d1.py 执行时替换成 TABLE_PREFIX（= 项目名，如 fsdm_）。
--   自己的项目共用一个库 → 每个项目 TABLE_PREFIX = 各自项目名，表不冲突
--   fork 别人的项目单独库 → TABLE_PREFIX 留空，不改别人代码只换 DATABASE_ID

-- 1. 任务状态：一次触发一行。谁干谁写：
--    Worker 发单写 pending；GHA 起跑写 running（job_start）、干完写 done/failed+计数（job_finish）。
CREATE TABLE IF NOT EXISTS __PREFIX__jobs (
  job_id       TEXT PRIMARY KEY,
  trigger_type TEXT NOT NULL,
  status       TEXT NOT NULL,
  total        INTEGER DEFAULT 0,
  done         INTEGER DEFAULT 0,
  failed       INTEGER DEFAULT 0,
  error        TEXT,
  created_at   INTEGER NOT NULL,
  updated_at   INTEGER NOT NULL
);

-- 2. 文档注册表：去重键。一行 = 一份已生成的文档。
--    doc_key = subject|product|platform|template_id，已有文档走更新不新建。
CREATE TABLE IF NOT EXISTS __PREFIX__documents (
  doc_key      TEXT PRIMARY KEY,
  doc_token    TEXT NOT NULL,
  subject      TEXT NOT NULL,
  product      TEXT NOT NULL,
  platform     TEXT NOT NULL,
  template_id  TEXT NOT NULL,
  created_at   INTEGER NOT NULL,
  updated_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS __PREFIX__idx_documents_scope
  ON __PREFIX__documents(subject, product, platform);

-- 3. 元素级台账：一行 = 一个文档的一个变量。
--    生成抹掉标记后文档里没有变量名痕迹，这是唯一的变量-文档-块关联。
--    职责 = 定位（哪些文档哪些块引用了某变量），不做变化判定（那是 sources 的事）。
CREATE TABLE IF NOT EXISTS __PREFIX__ledger (
  doc_token        TEXT NOT NULL,
  subject          TEXT NOT NULL,
  product          TEXT NOT NULL,
  var_name         TEXT NOT NULL,
  blocks_json      TEXT NOT NULL,     -- ["<标题/正文块id>", ...]
  images_json      TEXT NOT NULL,     -- ["<图片块id>", ...]
  template_block_id TEXT,             -- 母版里对应开始块的 id（模板形状重建用）
  content_hash     TEXT,              -- 模板块内容 hash（判断模板改没改）
  src_token        TEXT,              -- 源素材 attachmentToken（判断素材变没变）
  dst_token        TEXT,              -- 换图后飞书 file_token（当前挂的图）
  width            INTEGER DEFAULT 0,
  height           INTEGER DEFAULT 0,
  replaced_at      INTEGER,           -- 最后替换时间
  status           TEXT DEFAULT 'active',  -- active / deleted（软删除）
  updated_at       INTEGER NOT NULL,
  PRIMARY KEY (doc_token, var_name)
);
-- 按源反推：sources 判出某 (subject,product,var) 变了 → 用此索引拿全部引用文档块。
-- 注意 LIKE '%x%' 前缀通配无法用索引，会全表扫描（rows_read 暴增）。
CREATE INDEX IF NOT EXISTS __PREFIX__idx_ledger_lookup
  ON __PREFIX__ledger(subject, product, var_name);
-- 不建 doc_token 单列索引：主键 (doc_token, var_name) 已覆盖该前缀，
-- 多建只会让每次 upsert 多写一行索引行，纯浪费。

-- 4. 源快照：一行 = 一个唯一源（主体|产品|变量）上次填入的值。
--    同一张图被 N 份文档引用只此一行；比对判变化只读这张表，不按文档扫台账。
--    kind=image 时 src_token 有效（多图逗号分隔）；kind=text 时 value_hash 有效。
CREATE TABLE IF NOT EXISTS __PREFIX__sources (
  subject    TEXT NOT NULL,
  product    TEXT NOT NULL,
  var_name   TEXT NOT NULL,
  kind       TEXT NOT NULL,   -- image / text
  src_token  TEXT,            -- 上次填入的源 attachmentToken 序列（逗号分隔）
  value_hash TEXT,            -- 上次填入文本的 hash（空值也记，空→有 可判出变化）
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (subject, product, var_name)
);
