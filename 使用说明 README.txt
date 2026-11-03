【v7 新增功能】
左侧 Expiry & batches：实际到期日、批次库存、报损与退货。
左侧 Weekly review：一周经营小结，支持 Markdown / Excel 下载，无需 AI 密钥。
详细操作与旧数据升级方法见 v7更新与使用说明.md。

==============================================================
  Stocker AI — 智能库存管理 / Smart inventory management
==============================================================

【中文】

界面导航（左侧）
  · My shop：今天的概况和提醒
  · Plan & order：采购四步走 —— ① 用 AI 规划（告诉我你的需求）② 审核并批准 AI 草稿 ③ 确定最终采购单 ④ 联系供应商
  · Ask Stocker AI：直接提问，也可以让 AI 导出 CSV / Excel / Word
  · Import & export：导入销售数据、导出结果和发票
  · More（收起）：完整采购建议、全部提醒、销量预测、顾客习惯、建议依据与复盘、库存与商品、节假日与促销、手机/邮箱提醒、帮助与联系
  · Settings（收起）：AI key、预测模型信息、公共假期国家

一、准备（只需一次）
  安装 Python 3.10 或更新版本：https://www.python.org/downloads/
  · Windows 安装时务必勾选 "Add python.exe to PATH"
  · Mac 如果已经有 Python 3.10+ 可跳过

二、启动
  · Mac：双击 "Start Stocker AI (Mac).command"
      第一次如果提示"无法验证开发者"：右键该文件 → 打开 → 再点"打开"
  · Windows：双击 "Start Stocker AI (Windows).bat"
      如果出现"Windows 已保护你的电脑"：点"更多信息" → "仍要运行"

  第一次启动会自动安装所需组件，约 2–3 分钟，需要联网。
  之后每次启动只需几秒，浏览器会自动打开软件页面
  使用期间不要关闭黑色的命令行窗口；关闭它即可退出程序。

三、使用
  · 打开即有一个演示商店，可以直接体验所有功能
  · 导入自己的数据：左侧 "📥 Import data" → 上传 CSV 或 Excel
    （需要 日期、商品、数量 三列；有 时间/会员类型/小票号 可生成用户画像）
  · 换成自己的数据前，可在 Import data 页底部 "Reset my workspace" → "Start empty"

四、AI 助手（可选，免费）
  1. 到 https://console.groq.com 注册并创建一个免费 API key
  2. 方式 A：在程序左侧 "Settings" 里粘贴 key（仅本次有效）
     方式 B：用记事本打开 .streamlit/secrets.toml，把最后一行改成
             GROQ_API_KEY = "你的key"
             （删掉行首的 #），保存后重启程序，以后都不用再填
     注意：.streamlit 是隐藏文件夹。Mac 在访达里按 Cmd+Shift+. 显示；
           Windows 在资源管理器 "查看" 里勾选 "隐藏的项目"。
  不填 key 也可以使用除 AI 对话外的所有功能。

四点五、告诉我你的经营需求（需要 AI key）
  左侧 "Tell me your needs"：用自己的话写下预算、冰箱/库存空间、目标、活动和担心的事，
  例如"明天最多花 S$120，冰箱小，附近学校有活动，不想剩太多面包"。
  AI 会结合你的销量、库存和成本给出采购方案；预算由程序强制检查，超支会自动削减并说明改了什么。
  缺少的数据（如冰箱容量）会列为假设或向你提问。可以修改需求后重新生成。

四点六、AI 采购草稿与人工审核（需要 AI key）
  左侧 "Review AI draft"：AI 起草采购单并解释每项风险（易过期、积压、成本缺失、销量突变等）。
  草稿初始为"待审核，不能执行"。你可以改数量、填写意见或拒绝；有风险的商品必须先选择处理方式。
  填写审核人姓名后点"批准并生成正式采购清单"，才会生成并可下载正式清单。
  批准后再修改任何内容，批准会自动失效，需要重新审核。所有操作都有记录（Review history）。
  整个流程不会改动库存。

四点七、导出结果
  左侧 "Export results"：选择采购清单 / 完整进货清单 / 风险提醒，导出为可打印表格（HTML，可另存 PDF）、
  采购发票（可编辑数量、单价、税率，导出 HTML 或带公式的 Excel）、CSV 或 Excel。
  也可以直接在聊天里让 AI 生成文件，例如"把本周采购清单导出成 Excel"或"做一份风险报告的 Word"，
  下载按钮会出现在 AI 回答下方。文件里的数字都来自软件计算，不是 AI 编写的。
  欢迎页也有聊天框，与 "Ask Stocker AI" 页面共用同一段对话。

四点八、节假日与促销日历、提醒到手机/邮箱
  "More" → "Holidays & promotions"：一键加入新加坡/越南公共假期，或添加自己的促销、停业日。
  预测会去掉历史节假日的影响，并把节日/促销效果用到未来日期；预测页用 ★ 标记，并显示"过去 8 周预测有多准"。
  "More" → "Alerts to phone & email"：每天定时用 Telegram 或邮件发送摘要（紧急提醒、今日采购、
  本周节假日），缺货时可即时提醒。软件运行时自动发送。

五、机器学习预测（自动）
  数据满 12 个月后，每次训练会自动比较"机器学习模型"和"标准模型"，
  只有对某个商品更准时才切换，否则保持标准模型。
  在侧边栏 "Settings" 里可以看到有多少商品在用机器学习，并选择公共假期所在国家（默认新加坡）。
  预测页和每条建议的理由里都会写明用了哪个模型、准确度如何。

六、数据与隐私
  所有数据只保存在本机的 data 文件夹里，不会上传。
  会员号在导入时会被加密处理，不保存任何个人信息。

--------------------------------------------------------------

【English】

1. Install Python 3.10+ from https://www.python.org/downloads/
   (Windows: tick "Add python.exe to PATH").
2. Double-click "Start Stocker AI (Mac).command" or "Start Stocker AI (Windows).bat".
   First run installs everything (2–3 min, needs internet); after that it starts in seconds.
   Mac security prompt: right-click the file → Open. Windows SmartScreen: More info → Run anyway.
3. Your browser opens automatically with a demo shop. Import your own CSV/Excel
   under "📥 Import data". Keep the terminal window open; close it to quit.
4. Optional AI assistant: get a free key at https://console.groq.com and paste it under
   "Settings" in the sidebar, or put  GROQ_API_KEY = "your_key"  in .streamlit/secrets.toml.
4b. "Tell me your needs": describe budget, space, goals and worries in your own words; the AI drafts a
   plan from your data and the app enforces the budget. "Review AI draft": the AI drafts an order and
   explains risks; nothing becomes an official purchase list until a named reviewer approves it, and
   any later edit cancels the approval. Both need an AI key.
4c. Export: "Export results" gives printable tables, an editable purchase invoice (HTML or Excel with
   formulas), CSV and Excel. You can also ask the chatbot for a file ("export the shopping list to
   Excel", "make a Word report of my risks"); the download button appears under its reply.
4d. Holidays & promotions calendar and phone/email alerts: under "More". Events adjust the
   forecast (★ on the Forecast page); daily summaries and instant stock-out alerts go out by Telegram
   or email while the app is running.
4e. The four Plan & order steps are connected: after planning in "Needs", press "Send this plan for
   review"; after approving in "Review", press "Use as my final order list" (the Order list page also
   offers an approved purchase). "Suppliers" then groups that list. No numbers to copy by hand.
4f. AI purchase assistant: on "Needs", switch "Purchase workflow" to "AI purchase assistant". Under
   the plan, "Review and purchase" lets you approve a Simulation (writes a confirmation email, sends
   nothing), or for a Real order open a merchant website or an enquiry email to your supplier. Decline
   to get cheaper, less perishable or priority-only options. Nothing is ordered without you.
4g. Pricing (demo): "More" → "Pricing" lists the Free, Personal (S$18 / month) and Business
   (S$30 / month) plans. It is a list only: nothing is charged and no limit is switched on.
4h. Product icons: on "More" → "Stock & products" each product has an icon (🍞 bread, 🥛 milk…) in the
   Product picker, the Products table and the shelf map. It is picked from the name or category, and only shown.
   The cards on "My shop" → "Today" (Start here today, Things to look at) and "All alerts" show it too,
   and each alert has an icon for its kind (⚠️ stock-out risk, 🗑️ expired batch, 📈 demand spike…).
5. Machine learning: with 12+ months of history, each training run compares an ML model with the
   standard one and switches per product only where ML is more accurate. Set your public-holiday
   country under sidebar "Settings" (default Singapore).
5b. Forecast methods: each product also gets the standard-engine method that back-tests best on its
   own sales (weekly pattern + trend, steady average, same-weekday average, or weekly pattern +
   monthly payday cycle). The Forecast page shows which one, the error on weekly totals and how
   often a week sold above the forecast range.
5c. Photo import: "📥 Import data" → "Photo" tab. Upload PNG/JPEG photos of a receipt, supplier
   invoice, shopping list or product/price list. The AI turns it into a table and suggests a category
   per item; you check it, then it goes to sales history, stock received, the Final order list or
   products. Needs an AI key that can read images (Gemini, Claude, or Llama 4 on Groq).
5d. Voice questions: on "Ask Stocker AI", tap the microphone in the chat box and speak; your words
   become the question (shown with a mic icon) and are answered as if typed. Works with a Groq key
   (Whisper speech-to-text) or a Gemini key. Claude and OpenRouter cannot hear audio: with those,
   also set GROQ_API_KEY or GEMINI_API_KEY in .streamlit/secrets.toml to get the microphone.
   The browser asks once for microphone permission.
5e. Perishable / Non-perishable: "More" → "Stock & products" shows a "Perishable?" column and a filter.
   It is filled in automatically (chilled/frozen, shelf life 30 days or less, or fresh food); change
   it in the table and press Save to override.
5f. Shelf locations: on the same page every product gets a place automatically: Fridge = Shelf A,
   Dry goods = Shelf B, Household = Shelf C, numbered from 1 (frozen first, then the shortest shelf
   life), e.g. Ice Cream Tub A1, White Bread B1. Type your own (A5 or A-5) and press Save to move a
   product; clear the cell to let the app choose again. The shelf map below shows each shelf as a
   grid; click a product to select it in the stock form. "Lifespan group" (Short / Medium / Long)
   comes from the shelf life.
5g. Faster AI answers: the AI is asked to think briefly for chat and photos (a little longer for purchase
   plans), to fetch all the data it needs at once, and with Claude the answer appears while it is being
   written. Numbers still come from the app, so they are as accurate as before.
6. All data stays on this computer, in the "data" folder. Photos are sent to the AI provider you
   chose only when you press "Read with AI"; voice recordings only when you finish speaking.
   Neither is stored.

--------------------------------------------------------------

【开发者 / Developers】
  app.py        启动、工作区、侧边栏导航（约 200 行）
  views/        页面，每个侧边栏板块一个文件（shop, operations, planning, ordering, assistant,
                data, insights, setup, help, advice_review）；views/common.py 放共享的缓存与小组件
  stocker/      分析与业务逻辑（预测、机器学习、风险、审核、供应商、批次、建议复盘……），不依赖界面
  tests/        python -m unittest discover -s tests -v   （116 项，无需 AI key）
  stocker/scan.py  照片导入 / photo import (receipt, invoice, shopping list, product list → database)
  docs/         各次更新的说明；"Stocker AI flowcharts.pdf" = 项目流程图 / project flowcharts (English)
  缓存规则：页面结果按 data_key() 缓存；导入、库存变动、重新训练或跨天都会自动刷新。
