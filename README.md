# Scheduled Link Collector (Docker + GitHub Actions)

> ⚠️ 仅可用于你**有权访问和抓取**的数据源。请遵守目标站点条款与当地法律。

这是一个可容器化运行的定时抓取项目，功能：

1. 定时读取一个 JS 源（`SOURCE_URL`）。
2. 解析其中的比赛时间（北京时间）与 `href`。
3. 只保留“当前北京时间前后 3 小时”范围内的候选链接。
4. 访问候选链接页面，提取文本包含“高清直播/蓝光”的 `data-play`，并拼接成完整 URL。
5. 再访问这些 URL，优先提取：
   - `var encodedStr = '...'`
   - 若未找到，则提取 `paps.html?id=...` 的 `id` 参数。
6. 将汇总结果写入 `output/tokens.txt`。
7. 提供 HTTP 接口查看状态和导出 ID。

## 本地运行

```bash
cp .env.example .env
# 修改 .env 中配置

docker build -t scheduled-link-collector .
docker run --rm --env-file .env -p 5000:5000 -v $(pwd)/output:/app/output scheduled-link-collector
```

## 关键环境变量（均有默认值）

- `SOURCE_URL`: 要抓取的 JS 地址（默认已预填你提供的地址）
- `PLAY_LINK_HOST_FILTER`: 仅处理包含该主机的候选 href（默认 `play.sportsteam368.com`）
- `PLAY_HOST_PREFIX`: 将 `data-play` 相对路径拼接的域名前缀（默认 `http://play.sportsteam368.com`）
- `KEYWORDS_REGEX`: 匹配频道文案（默认 `高清直播|蓝光`）
- `SCHEDULE_MINUTES`: 轮询间隔（分钟）
- `TZ_NAME`: 时区（默认 `Asia/Shanghai`）
- `HOST`: HTTP 服务监听地址（默认 `0.0.0.0`）
- `PORT`: HTTP 服务端口（默认 `5000`）

## HTTP 接口

- `GET /`：运行状态、最近一次执行时间、结果数量
- `GET /healthz`：健康检查
- `GET /ids`：JSON 格式 ID 列表
- `GET /ids.txt`：纯文本 ID 列表
- `POST /run-once`：手动触发一次抓取

## 输出

- `output/tokens.txt`：每轮覆盖写入，包含去重后的 token（`encodedStr` 或 `paps.html?id=` 参数）。

## GitHub Actions

仓库内置了 `docker-image.yml`，会在 push/PR 时构建镜像（可选推送到 GHCR）。
