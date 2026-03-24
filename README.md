# Scheduled Link Collector (Docker + GitHub Actions)

> ⚠️ 仅可用于你**有权访问和抓取**的数据源。请遵守目标站点条款与当地法律。

这是一个可容器化运行的定时抓取项目，功能：

1. 定时读取一个 JS 源（`SOURCE_URL`）。
2. 解析比赛信息：联赛、对阵时间、主队、客队。
3. 联赛名前自动加 `JRS` 前缀。
4. 只保留“当前北京时间前后 3 小时”的比赛。
5. 提取并访问候选播放链接，抓取最终 `id`。
   - 可选开启模拟浏览器（Playwright）模式，通过网络请求与资源列表提取 `paps.html?id=...`。
6. 将抓到的 `id` 与比赛信息**一一对应**输出到 `/ids` 和 `/ids.txt`。

## 本地运行

```bash
cp .env.example .env
# 修改 .env 中配置

docker build -t scheduled-link-collector .
docker run --rm --env-file .env -p 5000:5000 -v $(pwd)/output:/app/output scheduled-link-collector
```

## 关键环境变量（均有默认值）

- `SOURCE_URL`: 要抓取的 JS 地址
- `PLAY_LINK_HOST_FILTER`: 仅处理包含该主机的候选 href（默认 `play.sportsteam368.com`）
- `PLAY_HOST_PREFIX`: 将 `data-play` 相对路径拼接的域名前缀（默认 `http://play.sportsteam368.com`）
- `KEYWORDS_REGEX`: 匹配频道文案（默认 `高清直播|蓝光`）
- `SCHEDULE_MINUTES`: 轮询间隔（分钟）
- `TZ_NAME`: 时区（默认 `Asia/Shanghai`）
- `OUTPUT_FILE`: 仅 ID 列表输出文件（默认 `output/tokens.txt`）
- `IDS_FILE`: ID+比赛信息映射输出文件（默认 `output/ids.json`）
- `USE_BROWSER`: 是否启用 Playwright 浏览器抓取（默认 `false`）
- `BROWSER_TIMEOUT_MS`: 浏览器打开页面超时（毫秒，默认 `15000`）
- `HOST`: HTTP 服务监听地址（默认 `0.0.0.0`）
- `PORT`: HTTP 服务端口（默认 `5000`）

## HTTP 接口

- `GET /`：运行状态
- `GET /healthz`：健康检查
- `GET /ids`：JSON 映射（每条包含 `id/league/time/home/away`）
- `GET /ids.txt`：文本映射（`联赛|时间|主队 vs 客队|id`）
- `POST /run-once`：手动触发一次抓取

## 输出

- `output/tokens.txt`：去重后的纯 ID 列表。
- `output/ids.json`：ID 与比赛信息的一一对应映射列表。

## GitHub Actions

仓库内置了 `docker-image.yml`，会在 push/PR 时构建镜像（可选推送到 GHCR）。
