# Stocker AI

A little help for the everyday work of running your shop.

Stocker AI is a stock and ordering assistant for small shops. It reads your sales records, then:

- forecasts what each product will sell, including weekends, paydays, holidays and promotions;
- tells you what to restock today, what can wait and what to stock less of;
- warns you about stock-outs, expiring batches, overstock and sudden changes in demand;
- helps you plan a purchase, approve it and send it to your suppliers.

An optional AI assistant answers questions about your shop in plain language, by typing or by voice. It can also read photos of receipts and invoices and draft purchase plans. The numbers always come from the app's own calculations; the AI explains them.

Stocker AI runs on your own computer, and your shop's data stays there.

## Getting started

### 1. Install Python (once)

Install Python 3.10 or newer from https://www.python.org/downloads/.

- **Windows:** during the install, tick **Add python.exe to PATH**.
- **Mac:** if you already have Python 3.10 or newer, skip this step.

### 2. Start Stocker AI

- **Mac:** double-click `Start Stocker AI (Mac).command`.
  If the Mac says the developer cannot be verified, right-click the file, choose **Open**, then **Open** again.
- **Windows:** double-click `Start Stocker AI (Windows).bat`.
  If you see "Windows protected your PC", click **More info**, then **Run anyway**.

The first start installs what the app needs. It takes 2–3 minutes and needs an internet connection. After that, the app starts in a few seconds.

Your browser opens Stocker AI by itself. Keep the terminal window open while you use the app, and close it to stop the app.

### 3. Try the demo shop, then import your own sales

A new workspace opens with a demo shop, so every page has something to show. To use your own sales:

1. Open **Import & export → Import sales**.
2. To remove the demo data first, open **Reset my workspace** at the bottom of the page and press **Start empty**.
3. Upload a CSV or Excel file from your till or spreadsheet. It needs a date, a product name and a quantity. Under **Column mapping**, check which column is which, then press **Import and train**.

More columns give you more: price or revenue, category, stock on hand and unit cost. For **Customer habits**, add the time of sale, member type, receipt number or member ID. **Download a sample file** on the same page shows the format.

### 4. Bookmark your shop

Each workspace is private, and its code is part of the page address, for example `http://localhost:8501/?ws=1a2b3c4d5e6f7a8b`. **Bookmark this page**, and open the bookmark to come back to your shop.

Two things to know:

- When the app opens without a code in the address, it starts a new workspace with the demo shop. The launcher does this every time it opens your browser, so use your bookmark to get back to your own shop.
- When a new workspace starts, any workspace that has not changed for 48 hours is deleted. The only exception is a workspace with saved advice still under review (see **Advice evidence & review** below). To keep a copy of your shop, back up its file: `data/workspaces/<code>.db`.

## Turn on the AI assistant (optional)

Everything works without AI except chatting with Stocker AI, planning or drafting a purchase with AI, photo import and voice questions.

You need an API key from one of these providers. Groq and Google Gemini give free keys.

| Provider | Free key | Reads photos | Hears voice |
|---|---|---|---|
| Groq | Yes, at console.groq.com | Yes, with a Llama 4 model (chosen for you) | Yes |
| Google Gemini | Yes, at aistudio.google.com | Yes | Yes |
| OpenRouter | No | Yes, with an image model (chosen for you) | No |
| Anthropic Claude | No | Yes | No |

With OpenRouter or Claude, voice questions still work if a Groq or Gemini key is also saved in `secrets.toml` (see below).

There are two ways to add your key:

- **For this visit only:** open **Settings** in the sidebar, choose the provider and paste the key. It is used only in this browser session and never stored.
- **Every time:** in the `.streamlit` folder, copy `secrets.toml.example` to a new file named `secrets.toml`. Open it in a text editor, put in your key, for example `GROQ_API_KEY = "gsk_..."`, and restart the app. The other names are `GEMINI_API_KEY`, `OPENROUTER_API_KEY` and `ANTHROPIC_API_KEY`.
  `.streamlit` is a hidden folder. On a Mac, press Cmd+Shift+. (period) in Finder to show it. On Windows, tick **Hidden items** under **View** in File Explorer.

A key in `secrets.toml` is shared by everyone who opens the app, so each browser session can ask up to 30 chat questions with it. A key typed under Settings has no limit.

## Find your way around

The sidebar has four everyday sections. Everything else is under **More**.

- **My shop**
  - **Today:** what to restock first (*Start here today*), urgent alerts (*Things to look at*) and how sales are going.
  - **Weekly review:** a summary of your week, with Markdown and Excel downloads. No AI key needed.
  - **Expiry & batches:** expiry dates for each batch, what is due soon, and waste and returns.
- **Plan & order:** four connected steps, from your needs to your suppliers (see below).
- **Ask Stocker AI:** ask about your shop by typing or by voice.
- **Import & export:** import sales from a file or a photo, and export your lists.
- **More**
  - *Insights:* Full shopping list · All alerts · Sales forecast · Customer habits · Advice evidence & review
  - *Shop setup:* Stock & products · Holidays & promotions · Alerts to phone & email
  - *Plans:* Pricing
  - *Help:* Help & contact · About Stocker AI
- **Settings:** your AI key and model, forecasting details, the public-holiday country and **Retrain models**.

## Plan & order

Each step passes its list on to the next, so there are no numbers to copy by hand.

1. **Plan with AI:** describe your budget, space, goals and worries in your own words, for example "Spend at most S$120 tomorrow, small fridge, school event nearby, don't want leftover bread". The AI builds a purchase plan from your sales, stock and costs. The app checks the budget itself: if the plan costs too much, it is cut back and the changes are explained. Then press **Send this plan for review →**.
2. **Review & approve:** check each item and its risks, such as expiry, overstock, missing costs or a sudden change in sales. Change quantities, add comments or reject items. Risky items need a decision before you can approve. Enter your name and approve. If anything changes after approval, the approval is cancelled. Every step is kept in the review history. You can also ask the AI for a fresh draft here.
3. **Final list:** press **Use as my final order list →** and adjust quantities if you need to. Until you approve a purchase, this list shows Stocker AI's own suggestions.
4. **Suppliers:** your final list grouped by supplier, with a message ready to send by email, SMS or WhatsApp.

Nothing is bought, sent or changed in your stock unless you do it yourself.

**AI purchase assistant:** on step 1, switch **Purchase workflow** to **AI purchase assistant**. Under the plan, **Review and purchase** offers two modes:

- **Simulation:** approve the plan, and the app writes a confirmation email but sends nothing.
- **Real order:** open a merchant's website, or an enquiry email to your supplier.

Decline the suggestion to see cheaper, less perishable or priority-only options. No order is placed without you.

## Stock & products

Open **More → Stock & products**.

- **Record stock:** at the top, choose a product and record **Stock in**, **Sold / used** or a **Stock count**. Alerts update straight away. For a delivery with an expiry date, press **Add a delivery with an expiry date →**.
- **Products table:** edit names, categories, stock, prices, costs, lead times and shelf life, then press **Save product changes**.
  - **Perishable?** is filled in for you. A product counts as perishable if it is chilled or frozen, keeps 30 days or less, or is fresh food with no shelf life recorded. Change it in the table to override it. Use **Show** to list only perishable or only non-perishable products.
  - **Location** is filled in for you: Fridge = Shelf A, Dry goods = Shelf B, Household = Shelf C. Places are numbered from 1, frozen first, then by shortest shelf life (for example Ice Cream Tub A1, White Bread B1). Type your own, such as A5 or A-5, to move a product. Clear the cell to let the app choose again.
  - **Lifespan group** comes from the shelf life: Short (up to 30 days), Medium (up to 180 days), Long, or Unknown when no shelf life is recorded.
- **Shelf map:** choose a shelf to see its places as a grid. Click a product to select it in the stock form above. The map never changes your stock.
- **Icons:** each product gets an icon from its name or category (🍞 bread, 🥛 milk, 🍨 ice cream…). Icons are only shown, never saved. They also appear on **Today** and on the alert cards.

## Forecasts

Stocker AI forecasts each product from its own sales and tests the forecast against past weeks.

- Each product gets the method that best predicted its own past sales: weekly pattern and trend, steady average, same-weekday average, or weekly pattern with a monthly payday cycle.
- With 365 days or more of history, a machine-learning model is tried as well. It is used only for products where it is more accurate.
- **More → Sales forecast** shows each product's method, its error on weekly totals, how often a week sold more than the forecast range, and the methods compared.
- **More → Holidays & promotions:** add Singapore or Vietnam public holidays in one click, or add your own promotions and closing days. The forecast removes the effect of past events from the history and applies it to future dates (marked ★ on the forecast).
- **Settings → Public holidays:** choose your country for the machine-learning forecast (Singapore by default), then press **Retrain models**.

## Alerts

Urgent alerts appear on **Today** under *Things to look at*, and all of them are under **More → All alerts**. Each alert says what happened and what to do next.

- The colour shows how urgent it is: 🔴 Critical (act now), 🟠 High (check today), 🟡 Medium (plan a next step), 🔵 Low (keep an eye on it).
- The icon shows the kind of alert:
  - **Stock:** 🚫 Out of stock · ⚠️ Stock-out risk · 🔻 Below reorder point · 💤 Dead stock · 📦 Overstock
  - **Expiry:** 🗑️ Expired batch · ⏰ Batch expires today · ⏳ Batch expiring soon or Expiry risk
  - **Sales:** 📈 Demand spike · 📉 Demand drop · 🔀 Trend shift
  - **Data:** ❔ Insufficient data · 🕰️ Stale data · 📝 Stock levels not set

**Alerts to your phone or email:** in **More → Alerts to phone & email**, turn on a daily summary at a time you choose, by Telegram (free) or email. You can add today's buying list and skip quiet days. **Instant stock-outs** sends a message when a product runs out, or will run out before a new order arrives. Messages are sent only while Stocker AI is running. To get them while it is closed, schedule `python -m stocker.notifier` with cron or Windows Task Scheduler.

## AI features

- **Ask Stocker AI:** ask in plain language, for example "What should I order today?" or "Which items are at risk?". The chat on the Welcome page continues the same conversation.
- **Files from the chat:** ask for a file, for example "Export this week's shopping list to Excel" or "Make a Word report of my risks". A download button appears under the answer. The numbers in the file come from the app, not from the AI.
- **Voice questions:** tap the microphone in the chat box and speak. Your words become the question. The browser asks once for permission to use the microphone.
- **Photo import:** in **Import & export → Import sales**, open the **Photo** tab and upload PNG or JPEG photos of a receipt, a supplier invoice, a shopping list or a product and price list. Press **Read with AI**, check the table, then press **Save to Stocker AI**. Receipts go to your sales, invoices to stock received, shopping lists to the final order list and product lists to your products. There are sample photos in `docs/sample photos/`.

## Export

In **Import & export → Export results**, choose a list: the final order list, suppliers to contact, the purchase list, the full stocking list or risk alerts. Download it as:

- a printable table (HTML, which you can save as PDF);
- a purchase invoice, where you can edit quantities, prices and tax before downloading HTML or Excel with formulas;
- CSV or Excel.

## More pages

- **Full shopping list:** advice for every product: stock more, plan an order, stock less, keep as is, stop reordering or collect more data.
- **Customer habits:** customer segments, when customers buy, members compared with walk-in customers, what sells at which time, weekdays compared with weekends, and baskets. It uses only anonymous purchase data. Most of it needs the optional customer columns in your sales file.
- **Advice evidence & review:** see the evidence behind each piece of advice. Save today's advice, and later compare its forecast with what really sold.
- **Pricing:** a demo list of plans: Free (S$0), Personal (S$18 a month) and Business (S$30 a month). Nothing is charged, and no limit is switched on.
- **Help & contact:** fixes for common problems, and a form that prepares an email to the Stocker AI team for you to send.

## Your data and privacy

- Your shop's data is stored only on this computer, in `data/workspaces`.
- Member and customer IDs are scrambled (hashed) when you import them, so no personal identifiers are stored.
- When you use an AI feature, your question and the shop figures the AI looks up are sent to the AI provider you chose. Photos are sent only when you press **Read with AI**, and voice recordings only when you finish speaking. Neither is stored.
- A key typed under Settings is never saved.

## If something goes wrong

- **"Python 3.10 or newer is required":** install Python (step 1), then start the app again.
- **"Install failed":** check your internet connection and start the app again.
- **The browser did not open:** open the address shown in the terminal window, usually http://localhost:8501.
- **Your shop is gone after you reopen the app:** open your bookmark (see **Bookmark your shop**).
- **The AI rejects your key:** make sure the provider under Settings matches the key, and that you copied the whole key.
- For more help, open **More → Help & contact**.

## For developers

Run the app without the launchers, from the project folder:

```bash
pip install -r requirements.txt
streamlit run app.py
```

Run the tests. No AI key or microphone is needed, because the AI providers are replaced by fakes:

```bash
python -m unittest discover -s tests -v
```

| Path | What it holds |
|---|---|
| `app.py` | Start-up, workspaces, sidebar navigation and page routing |
| `ui.py`, `assets/` | Theme and small display helpers |
| `views/` | One module per sidebar section; `views/common.py` holds shared caching and cards |
| `stocker/` | Analysis and business logic: forecasting, machine learning, risks, reviews, orders, suppliers, batches, photo import and the AI agent. No UI code |
| `tests/` | Unit tests and page tests |
| `preview.py` | The AI purchase assistant with a fixed example plan, no key needed: `streamlit run preview.py` (add `?mode=auto` or `?order=real` to the address) |
| `docs/` | Update notes, `Stocker AI flowcharts.pdf`, sample photos and suggested shelf locations |
| `data/workspaces/` | One SQLite database per workspace |

Settings come from the environment, or from `.streamlit/secrets.toml` where noted:

| Name | Use |
|---|---|
| `GROQ_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY` | Shared AI key (also read from `secrets.toml`) |
| `STOCKER_MODEL` | Use this model instead of the one picked automatically (also read from `secrets.toml`) |
| `TELEGRAM_BOT_TOKEN`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD` | Defaults for owner alerts (also read from `secrets.toml`) |
| `STOCKER_DATA_DIR` | Folder for the workspace databases |
| `STOCKER_AI_LIMIT` | Chat questions per browser session on the shared key (default 30) |

Never put a real `secrets.toml` into version control.

Page results are cached by `data_key()`. An import, a stock movement, retraining or a new day refreshes them.
