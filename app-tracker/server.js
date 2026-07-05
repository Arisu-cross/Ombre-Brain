const express = require('express');
const fs = require('fs');

const app = express();
app.use(express.json());
app.use(express.urlencoded({ extended: true }));

// ===== 配置 =====
const PORT = process.env.PORT || 3000;
const AUTH_TOKEN = process.env.AUTH_TOKEN;
const MAX_RECORDS = 200;
// 可选：设置 DATA_FILE 环境变量启用 JSON 文件持久化（如 /data/records.json）
const DATA_FILE = process.env.DATA_FILE || '';

if (!AUTH_TOKEN) {
  console.error('错误：必须设置 AUTH_TOKEN 环境变量，拒绝裸奔启动');
  process.exit(1);
}

// ===== 存储 =====
let records = [];

if (DATA_FILE) {
  try {
    records = JSON.parse(fs.readFileSync(DATA_FILE, 'utf8'));
    if (!Array.isArray(records)) records = [];
    console.log(`从 ${DATA_FILE} 恢复了 ${records.length} 条记录`);
  } catch (e) {
    records = [];
  }
}

let saveTimer = null;
function scheduleSave() {
  if (!DATA_FILE || saveTimer) return;
  saveTimer = setTimeout(() => {
    saveTimer = null;
    fs.writeFile(DATA_FILE, JSON.stringify(records), (err) => {
      if (err) console.error('持久化失败:', err.message);
    });
  }, 1000);
}

// ===== 时间：统一 UTC+8（北京时间）=====
function beijingParts(ts) {
  const d = new Date(ts + 8 * 3600 * 1000);
  const iso = d.toISOString(); // 已偏移8小时，直接取字段
  return {
    date: iso.slice(0, 10),
    time: iso.slice(0, 10) + ' ' + iso.slice(11, 19) + ' +08:00',
  };
}

// ===== 认证：所有业务接口都要 Bearer token =====
function auth(req, res, next) {
  const header = req.get('authorization') || '';
  if (header !== `Bearer ${AUTH_TOKEN}`) {
    return res.status(401).json({ error: 'unauthorized' });
  }
  next();
}

// 健康检查（不含数据，无需认证，方便平台探活）
app.get('/', (req, res) => {
  res.json({ status: 'ok', service: 'app-tracker' });
});

// 上报：POST /report  body: { "app": "App名称" }
app.post('/report', auth, (req, res) => {
  const appName = (req.body && req.body.app) || req.query.app;
  if (!appName || typeof appName !== 'string' || !appName.trim()) {
    return res.status(400).json({ error: 'missing app name' });
  }
  const ts = Date.now();
  const record = { app: appName.trim(), time: beijingParts(ts).time, ts };
  records.push(record);
  if (records.length > MAX_RECORDS) records = records.slice(-MAX_RECORDS);
  scheduleSave();
  res.json({ ok: true, record });
});

// 最近记录：GET /recent?limit=N（默认20，最多200）
app.get('/recent', auth, (req, res) => {
  let limit = parseInt(req.query.limit, 10);
  if (!Number.isFinite(limit) || limit <= 0) limit = 20;
  limit = Math.min(limit, MAX_RECORDS);
  res.json({ count: records.length, records: records.slice(-limit).reverse() });
});

// 当前App：GET /current（最近一条记录）
app.get('/current', auth, (req, res) => {
  if (records.length === 0) {
    return res.json({ app: null, time: null, message: '还没有记录' });
  }
  res.json(records[records.length - 1]);
});

// 今日统计：GET /stats（北京时间的“今天”，各App使用次数）
app.get('/stats', auth, (req, res) => {
  const today = beijingParts(Date.now()).date;
  const counts = {};
  for (const r of records) {
    if (beijingParts(r.ts).date === today) {
      counts[r.app] = (counts[r.app] || 0) + 1;
    }
  }
  const stats = Object.entries(counts)
    .map(([app, count]) => ({ app, count }))
    .sort((a, b) => b.count - a.count);
  res.json({ date: today, total: stats.reduce((s, x) => s + x.count, 0), stats });
});

app.listen(PORT, () => {
  console.log(`App Tracker running on port ${PORT}`);
});
