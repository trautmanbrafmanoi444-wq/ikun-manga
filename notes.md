# baozimh.org

Astro 静态壳 + Cloudflare。章节和图源不在 HTML 里。

- 搜索：`GET https://baozimh.org/s?q=`
- 漫画页：`/manga/{slug}`，`data-mid`
- 目录：`GET https://v2.apikk.top/api/manga/get?mid={mid}&mode=all`
- 章节：`GET https://v2.apikk.top/api/v2/chapter/getinfo?m={mid}&c={cid}`
- `info.images.images` 是加密 blob，浏览器 `window.__cimg.r` 解码成 `{order,url}`
- 线路 `line=2` → `https://c-nd2-1.6wm.top{url}`

解码：`decoder.py` 对齐 `window.__cimg.r`。blob 解一次后写入 `data/chapter/`，二次打开不再打加密接口。

嬉皮 m.hipmh.com：
- 搜索：`GET https://hipapi1.s3file.top/v1/search?q=`
- 目录：`GET /v1/manga/chapters?mid=`
- 章节：`GET /v2/chapter?hid=`（前端 hid `m:id-c:cid` 要换成 API hid `c:cid`）
- 图源：`https://hip-tx-1.s3imgs.top` + `/i/{token}/n.webp`
- 解码前缀 `qM9` / 后缀 `Z7`，解开后按 order_id/sid 丢掉一张诱饵图

本地搜索合并包子 + 嬉皮 + Kmoe（kzo.moe）。
- 嬉皮 slug `hip-{mid}`，可在线阅读
- Kmoe slug `kmoe-{id}`，卷目录匿名可拿；mobi/epub 仍要原站登录，目录页跳 `https://kzo.moe/c/{id}.htm`

本地站缓存：
- `data/home/` 首页分区
- `data/search/` 搜索
- `data/slug/` slug→mid
- `data/manga/` 章节目录
- `data/chapter/` 已解码图链

`python3 server.py` → http://127.0.0.1:8788
线上（新加坡 Starter）：https://ikun-manga-sg.onrender.com  
上 Render：见 `deploy/RENDER.md`。VPS：`HOST=0.0.0.0 PORT=8788 python3 server.py`，步骤见 `deploy/README.md`。
首页 / 人气 / 更新 / 上架 / 搜索 / 在线阅读。图片走流，点下载才打 zip。

Kmoe 账号（本地 .secrets/kmoe.env，勿提交）：
- 已注册登录：kmoe@jerfsa.org，uin 11961521，quota_now=300，is_vip=0
- 卷目录登录后可用；getdownurl 会跳 /regphone.php（6 位短信），iCanDown=0
- 无跳过接口。lindaleisd.org 被原站拒收（e403），jerfsa.org 可收验证码
