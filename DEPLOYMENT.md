# 云端部署说明

推荐部署到 Render。这个项目会在后台定时采集数据，并用 SQLite 保存赔率、回测和状态文件，所以云端需要持久磁盘。

## Render 部署

1. 打开 Render Dashboard，选择 New > Blueprint。
2. 连接 GitHub 仓库：`https://github.com/youjia995-web/best-bet`。
3. 选择仓库根目录的 `render.yaml`。
4. 按提示填写环境变量：
   - `ADMIN_PASSWORD`：后台操作密码。
   - `ODDS_API_IO_KEY`：备用接口 key；没有也可以先留空，主接口仍可运行。
5. 创建服务并等待部署完成。

部署后 Render 会生成一个 `https://...onrender.com` 地址，以后每次推送到 GitHub `main` 分支都会自动重新部署。

## 数据保存位置

`render.yaml` 已配置 1GB 持久磁盘，挂载到 `/var/data`，并设置：

- `DATA_DIR=/var/data`
- SQLite 数据库：`/var/data/odds_monitor.db`
- 数据源状态：`/var/data/data_source_state.json`
- Odds-API 状态：`/var/data/odds_api_io_state.json`

本地运行时不需要额外配置，默认仍使用项目目录下的这些文件。

## 启动命令

Render 使用：

```bash
uvicorn app:app --host 0.0.0.0 --port $PORT
```
