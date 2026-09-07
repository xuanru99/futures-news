# 云端部署指引（GitHub Actions 版）

把简报机器人搬到 GitHub 的服务器上运行：**不依赖你电脑开机，7×24 自动执行**，而且 GitHub 服务器在美国——被墙的彭博、Google News 源也能直接抓，数据源比本机版更全。

代码已经打包成一个 git 仓库（`futures-news/` 文件夹，已做好首次提交），照下面 6 步走完就上线。

---

## 第 1 步：注册 GitHub 账号（免费，约 2 分钟）

1. 打开 https://github.com/signup
2. 输入邮箱 → 设置密码 → 用户名 → 验证邮箱
3. 免费计划选 Free 即可

## 第 2 步：创建仓库

1. 登录后打开 https://github.com/new
2. Repository name 填：`futures-news`
3. 选择 **Private**（私有，别人看不到）
4. **不要**勾选任何 "Add a README" 之类的初始化选项
5. 点 Create repository

## 第 3 步：生成访问令牌（用来把代码推上去）

1. 打开 https://github.com/settings/tokens
2. Generate new token → **Generate new token (classic)**
3. Note 随便填（如 `push`），Expiration 选 **7 days**（只用一次，短一点更安全）
4. 勾选 **repo** 这一个大项
5. 点 Generate token，**复制生成的令牌**（ghp_ 开头，只显示一次）

## 第 4 步：把代码推上去

**方式 A（推荐）**：把第 3 步复制的令牌发给我，我帮你推送（用完即弃，不会保存）。

**方式 B（自己动手）**：在 `futures-news` 文件夹右键 → Git Bash Here，执行：

```bash
git remote add origin https://github.com/你的用户名/futures-news.git
git push -u origin main
```

弹出登录时：用户名填你的 GitHub 用户名，密码处粘贴第 3 步的令牌。

## 第 5 步：配置微信推送的 Secret

1. 打开你的仓库页面 → **Settings** → 左侧 **Secrets and variables** → **Actions**
2. 点 **New repository secret**：
   - Name 填：`SERVERCHAN_KEY`
   - Secret 填：你的 Server酱 SendKey（就是本机版 config.json 里那个 `SCT` 开头的）
3. （可选）如果用 PushPlus，再加一个 `PUSHPLUS_TOKEN`

> Secret 是加密存储的，比写在代码里安全，仓库即使泄露 key 也不会丢。

## 第 6 步：手动跑一次验证

1. 仓库页面 → **Actions** 标签 → 左侧选 **futures-news** → 点右侧 **Run workflow** → Run
2. 等约 1 分钟出现绿色对勾
3. **微信收到消息 = 云端部署成功** ✅

之后每天 **08:00 / 17:00**（北京时间）自动推送，电脑关机也照常。

---

## 部署后须知

- **不用再运行本机的 `register_tasks.bat`**，否则早晚各收两份重复消息（本地版保留作备用即可）
- GitHub 定时任务偶尔会**延迟几分钟**触发（服务器排队），属正常现象
- GitHub 规定：**仓库 60 天没有任何提交会自动暂停定时任务**。如果哪天消息停了，回来找我，我帮你重新激活（提交一个小改动即可）
- 改推送时间：编辑 `.github/workflows/news.yml` 里的 cron（UTC 时间 = 北京时间减 8 小时），提交后生效
- 想改板块、条数：编辑 `config.cloud.json` 提交即可，**云端用的就是这个文件**（本机版用 config.json，两者互不影响）

## 工作原理（一句话版

GitHub Actions 在它的服务器上，按 cron 定时把你仓库里的 `news_bot.py` 拉起来跑一次：抓 RSS → 分类去重 → 通过 Secret 里存的 key 调 Server酱 接口 → 微信收到消息。
