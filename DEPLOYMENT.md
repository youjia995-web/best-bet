# 云端部署说明

这个项目会在后台定时采集数据，并用 SQLite 保存赔率、回测和状态文件。想免费部署，优先推荐 Railway；它的 Free/Trial 计划支持 0.5GB Volume，可以保存 SQLite 数据。

## 免费推荐：Railway

1. 打开 Railway，选择 New Project。
2. 选择 Deploy from GitHub repo。
3. 选择仓库：`youjia995-web/best-bet`。
4. 部署后进入服务的 Variables，添加：
   - `ADMIN_PASSWORD`：后台操作密码。
   - `ODDS_API_IO_KEY`：备用接口 key；没有也可以先留空。
   - `DATA_DIR=/var/data`
5. 进入服务的 Volumes，添加一个 Volume，挂载路径填写：`/var/data`。
6. 进入 Networking，生成公开访问域名。

Railway 会读取仓库里的 `railway.json`，启动命令是：

```bash
uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}
```

注意：Railway 免费额度有限，Free 计划每月有少量免费 credit，并支持 0.5GB Volume。如果采集频率过高或服务一直运行，可能用完额度后停止服务。

## Render 部署

Render 免费 Web Service 可以运行这个项目，但不能挂载持久磁盘，所以数据库和状态文件可能在重启或重新部署后丢失，而且免费服务空闲后会休眠。它适合演示，不适合长期保存数据。

1. 打开 Render Dashboard，选择 New > Blueprint。
2. 连接 GitHub 仓库：`https://github.com/youjia995-web/best-bet`。
3. 选择仓库根目录的 `render.yaml`。
4. 按提示填写环境变量：
   - `ADMIN_PASSWORD`：后台操作密码。
   - `ODDS_API_IO_KEY`：备用接口 key；没有也可以先留空，主接口仍可运行。
5. 创建服务并等待部署完成。

部署后 Render 会生成一个 `https://...onrender.com` 地址，以后每次推送到 GitHub `main` 分支都会自动重新部署。

## 数据保存位置

Railway 上建议配置 Volume，挂载到 `/var/data`，并设置：

- `DATA_DIR=/var/data`
- SQLite 数据库：`/var/data/odds_monitor.db`
- 数据源状态：`/var/data/data_source_state.json`
- Odds-API 状态：`/var/data/odds_api_io_state.json`

本地运行时不需要额外配置，默认仍使用项目目录下的这些文件。
